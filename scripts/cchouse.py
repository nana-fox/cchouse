#!/usr/bin/env python3
"""cchouse 一条命令安装向导（当前子命令：up）。

用法：
  python3 -I scripts/cchouse.py up [--tag cc01] [--route auto|tart-linux|vmware-linux]

本轮范围：
  1. 无回显读取用户粘贴的单节点代理链接（socks5/http/https/vless/trojan/ss），拒绝 UDP 协议。
  2. 生成 ed25519 SSH 密钥对，按宿主机架构自动选择路线（Apple Silicon -> tart-linux，其余 -> vmware-linux）。
  3. 对 socks5/http 节点可选探测出口 IP / 时区 / ASN 与 Stripe 可达性（Stripe 不通只提示不拦截）。
  4. 在用户私有目录以 600 权限写出 instance.env（绝不写入仓库），运行 preflight.py 并输出下一步命令。
"""
import argparse
import base64
import getpass
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight as preflight_mod  # noqa: E402
import render as render_mod  # noqa: E402

REJECTED_SCHEMES = frozenset(set(render_mod.REJECTED_UDP_TYPES) | {"wg", "quic"})
SUPPORTED_SCHEMES = frozenset({"socks5", "socks5h", "socks", "http", "https", "vless", "trojan", "ss"})
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_CIPHER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_TAG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,15}$")
FORBIDDEN_VALUE_CHARS = ("'", "\r", "\n", "\x00")


class NodeLinkError(ValueError):
    """节点链接解析错误（错误文案绝不回显原始链接或凭据）。"""


def _check_safe_env_value(val, field_name):
    if val is None:
        return ""
    for ch in FORBIDDEN_VALUE_CHARS:
        if ch in val:
            raise NodeLinkError(f"{field_name} 包含非法字符（不允许单引号、换行或空字节）")
    return val


def _validate_host(host):
    if not host:
        raise NodeLinkError("节点链接缺少服务器地址（host）")
    if ":" in host:
        raise NodeLinkError("cchouse 禁用 IPv6，请使用 IPv4 或含 A 记录的域名节点")
    if not SAFE_HOST_RE.fullmatch(host):
        raise NodeLinkError("节点服务器地址格式不合法")
    return host


def _validate_port(port):
    if port is None:
        raise NodeLinkError("节点链接缺少有效端口（1-65535）")
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise NodeLinkError("节点端口必须是 1-65535 的整数")
    if not (1 <= p <= 65535):
        raise NodeLinkError("节点端口超出范围（仅允许 1-65535）")
    return str(p)


def _decode_b64_flexible(raw):
    cleaned = raw.strip()
    if not cleaned:
        raise NodeLinkError("ss:// 链接 Base64 内容为空")
    # 兼容 URL-safe base64 与缺失填充
    normalized = cleaned.replace("-", "+").replace("_", "/")
    pad = (-len(normalized)) % 4
    normalized += "=" * pad
    try:
        return base64.b64decode(normalized, validate=True).decode("utf-8")
    except Exception:
        raise NodeLinkError("ss:// 链接 Base64 解码失败，请检查链接是否完整")


def _parse_query(query_str):
    pairs = urllib.parse.parse_qsl(query_str, keep_blank_values=True)
    return {k.lower(): v for k, v in pairs}


def _extract_host_port(netloc_hostport, default_port=None):
    # 手动拆 host:port 避免 urlsplit 对非法字符/多冒号行为不一致
    if not netloc_hostport:
        raise NodeLinkError("节点链接缺少服务器地址与端口")
    if netloc_hostport.startswith("["):
        raise NodeLinkError("cchouse 禁用 IPv6，请使用 IPv4 或含 A 记录的域名节点")
    if ":" in netloc_hostport:
        host, port_str = netloc_hostport.rsplit(":", 1)
        if not port_str.isdigit():
            raise NodeLinkError("节点端口必须是 1-65535 的整数")
        return _validate_host(host), _validate_port(int(port_str))
    if default_port is not None:
        return _validate_host(netloc_hostport), _validate_port(default_port)
    raise NodeLinkError("节点链接缺少端口（格式应为 host:port）")


def parse_node_link(raw_link):
    """解析单节点代理链接为 SINGLE_* 字段字典；绝不在异常中回显原始链接或凭据。"""
    if not isinstance(raw_link, str):
        raise NodeLinkError("节点链接不能为空")
    if any(ch in raw_link for ch in ("\r", "\n", "\x00")):
        raise NodeLinkError("节点链接包含非法换行或空字节")
    link = raw_link.strip()
    if not link:
        raise NodeLinkError("节点链接不能为空")
    if "://" not in link:
        raise NodeLinkError("节点链接缺少协议头（仅支持 socks5:// http:// https:// vless:// trojan:// ss://）")

    scheme, rest = link.split("://", 1)
    scheme = scheme.strip().lower()
    if scheme in REJECTED_SCHEMES:
        raise NodeLinkError(f"不支持 UDP 协议 {scheme}://（cchouse 默认全局禁 UDP，请改用 TCP 类节点：socks5/http/vless/trojan/ss）")
    if scheme not in SUPPORTED_SCHEMES:
        raise NodeLinkError(f"不支持的协议 {scheme}://（仅支持 socks5/http/https/vless/trojan/ss）")

    fields = {
        "SINGLE_TYPE": "",
        "SINGLE_SERVER": "",
        "SINGLE_SERVER_IP": "",
        "SINGLE_PORT": "",
        "SINGLE_USER": "",
        "SINGLE_PASS": "",
        "SINGLE_UUID": "",
        "SINGLE_SNI": "",
        "SINGLE_FINGERPRINT": "chrome",
        "SINGLE_FLOW": "",
        "SINGLE_NETWORK": "tcp",
        "SINGLE_WS_PATH": "",
        "SINGLE_WS_HOST": "",
        "SINGLE_PUBLIC_KEY": "",
        "SINGLE_SHORT_ID": "",
        "SINGLE_CIPHER": "",
        "SINGLE_TLS": "",
        "SINGLE_HOST_KEY": "",
    }

    if scheme == "ss":
        # 去掉 #remark 与可选 ?plugin=...
        body = rest.split("#", 1)[0]
        body_no_q = body.split("?", 1)[0].rstrip("/")
        if "@" in body_no_q:
            userinfo_raw, hostport = body_no_q.rsplit("@", 1)
            if ":" in userinfo_raw and not re.fullmatch(r"[A-Za-z0-9+/=_-]+", userinfo_raw):
                method, password = userinfo_raw.split(":", 1)
                method = urllib.parse.unquote(method)
                password = urllib.parse.unquote(password)
            elif ":" in userinfo_raw:
                method, password = userinfo_raw.split(":", 1)
                method = urllib.parse.unquote(method)
                password = urllib.parse.unquote(password)
            else:
                decoded = _decode_b64_flexible(userinfo_raw)
                if ":" not in decoded:
                    raise NodeLinkError("ss:// 链接凭据段缺少加密方式与密码分隔符")
                method, password = decoded.split(":", 1)
            host, port = _extract_host_port(hostport)
        else:
            decoded = _decode_b64_flexible(body_no_q)
            if "@" not in decoded:
                raise NodeLinkError("ss:// 旧版链接缺少 @host:port")
            cred, hostport = decoded.rsplit("@", 1)
            if ":" not in cred:
                raise NodeLinkError("ss:// 旧版链接缺少加密方式与密码分隔符")
            method, password = cred.split(":", 1)
            host, port = _extract_host_port(hostport)

        if not method or not SAFE_CIPHER_RE.fullmatch(method):
            raise NodeLinkError("ss:// 加密方式（cipher）不合法")
        if not password:
            raise NodeLinkError("ss:// 缺少密码")
        fields["SINGLE_TYPE"] = "ss"
        fields["SINGLE_SERVER"] = host
        fields["SINGLE_PORT"] = port
        fields["SINGLE_CIPHER"] = _check_safe_env_value(method, "SINGLE_CIPHER")
        fields["SINGLE_PASS"] = _check_safe_env_value(password, "SINGLE_PASS")
    else:
        parsed = urllib.parse.urlsplit(link)
        query = _parse_query(parsed.query)
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)
        else:
            userinfo, hostport = "", netloc

        default_port = 80 if scheme == "http" else (443 if scheme == "https" else None)
        host, port = _extract_host_port(hostport, default_port=default_port)
        fields["SINGLE_SERVER"] = host
        fields["SINGLE_PORT"] = port

        if scheme in ("socks5", "socks5h", "socks", "http", "https"):
            fields["SINGLE_TYPE"] = "socks5" if scheme.startswith("socks") else "http"
            if userinfo:
                if ":" in userinfo:
                    u_raw, p_raw = userinfo.split(":", 1)
                    fields["SINGLE_USER"] = _check_safe_env_value(urllib.parse.unquote(u_raw), "SINGLE_USER")
                    fields["SINGLE_PASS"] = _check_safe_env_value(urllib.parse.unquote(p_raw), "SINGLE_PASS")
                else:
                    fields["SINGLE_USER"] = _check_safe_env_value(urllib.parse.unquote(userinfo), "SINGLE_USER")
            if scheme == "https" or query.get("tls", "").lower() in ("1", "true"):
                fields["SINGLE_TLS"] = "true"
            if query.get("sni"):
                fields["SINGLE_SNI"] = _check_safe_env_value(query["sni"], "SINGLE_SNI")

        elif scheme == "vless":
            uuid_val = _check_safe_env_value(urllib.parse.unquote(userinfo), "SINGLE_UUID")
            if not uuid_val or ":" in uuid_val:
                raise NodeLinkError("vless:// 链接缺少有效 UUID")
            security = query.get("security", "").strip().lower()
            if security == "none":
                raise NodeLinkError("vless:// 禁止明文传输（security=none），请使用 TLS 或 REALITY 节点")
            net = query.get("type", "tcp").strip().lower() or "tcp"
            if net not in ("tcp", "ws"):
                raise NodeLinkError(f"vless:// 暂不支持传输协议 type={net}，仅支持 tcp 或 ws")
            pbk = _check_safe_env_value(query.get("pbk", "").strip(), "SINGLE_PUBLIC_KEY")
            if security == "reality" and not pbk:
                raise NodeLinkError("vless:// REALITY 节点缺少公钥参数 pbk")

            fields["SINGLE_TYPE"] = "vless"
            fields["SINGLE_UUID"] = uuid_val
            fields["SINGLE_NETWORK"] = net
            fields["SINGLE_SNI"] = _check_safe_env_value(query.get("sni", "").strip(), "SINGLE_SNI")
            fields["SINGLE_FINGERPRINT"] = _check_safe_env_value(query.get("fp", "chrome").strip() or "chrome", "SINGLE_FINGERPRINT")
            fields["SINGLE_FLOW"] = _check_safe_env_value(query.get("flow", "").strip(), "SINGLE_FLOW")
            fields["SINGLE_PUBLIC_KEY"] = pbk
            fields["SINGLE_SHORT_ID"] = _check_safe_env_value(query.get("sid", "").strip(), "SINGLE_SHORT_ID")
            if net == "ws":
                fields["SINGLE_WS_PATH"] = _check_safe_env_value(query.get("path", "/").strip() or "/", "SINGLE_WS_PATH")
                fields["SINGLE_WS_HOST"] = _check_safe_env_value(query.get("host", "").strip(), "SINGLE_WS_HOST")

        elif scheme == "trojan":
            pwd_val = _check_safe_env_value(urllib.parse.unquote(userinfo), "SINGLE_PASS")
            if not pwd_val:
                raise NodeLinkError("trojan:// 链接缺少密码")
            security = query.get("security", "").strip().lower()
            if security == "none":
                raise NodeLinkError("trojan:// 必须启用 TLS，禁止 security=none")
            net = query.get("type", "tcp").strip().lower() or "tcp"
            if net not in ("tcp", "ws"):
                raise NodeLinkError(f"trojan:// 暂不支持传输协议 type={net}，仅支持 tcp 或 ws")

            fields["SINGLE_TYPE"] = "trojan"
            fields["SINGLE_PASS"] = pwd_val
            fields["SINGLE_NETWORK"] = net
            fields["SINGLE_SNI"] = _check_safe_env_value(query.get("sni", "").strip(), "SINGLE_SNI")
            fields["SINGLE_FINGERPRINT"] = _check_safe_env_value(query.get("fp", "chrome").strip() or "chrome", "SINGLE_FINGERPRINT")
            if net == "ws":
                fields["SINGLE_WS_PATH"] = _check_safe_env_value(query.get("path", "/").strip() or "/", "SINGLE_WS_PATH")
                fields["SINGLE_WS_HOST"] = _check_safe_env_value(query.get("host", "").strip(), "SINGLE_WS_HOST")

    if render_mod.is_ipv4(fields["SINGLE_SERVER"]):
        fields["SINGLE_SERVER_IP"] = fields["SINGLE_SERVER"]
    return fields


def detect_route():
    sys_name = platform.system()
    arch = preflight_mod.detect_arch()
    if sys_name == "Darwin" and arch == "arm64":
        return "tart-linux"
    return "vmware-linux"


def default_private_root():
    sys_name = platform.system()
    if sys_name == "Darwin":
        return Path.home() / "Library" / "Application Support" / "cchouse"
    if sys_name == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else (Path.home() / "AppData" / "Local")) / "cchouse"
    return Path.home() / ".local" / "share" / "cchouse"


def _is_inside_repo(path):
    try:
        Path(path).resolve().relative_to(ROOT.resolve())
        return True
    except ValueError:
        return False


def resolve_server_ipv4(server):
    if render_mod.is_ipv4(server):
        return server
    try:
        infos = socket.getaddrinfo(server, None, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted({item[4][0] for item in infos})
        return ips[0] if ips else ""
    except OSError:
        return ""


def probe_egress_via_proxy(fields, timeout=10):
    """对 socks5/http 节点在宿主机轻量探测出口 IP/时区/ASN（绝不打印凭据）。"""
    stype = fields.get("SINGLE_TYPE", "")
    if stype not in ("socks5", "http"):
        return None
    host = fields.get("SINGLE_SERVER_IP") or fields.get("SINGLE_SERVER")
    port_s = fields.get("SINGLE_PORT", "")
    if not host or not port_s.isdigit():
        return None
    port = int(port_s)
    user = fields.get("SINGLE_USER", "")
    pwd = fields.get("SINGLE_PASS", "")
    target_host = "ipinfo.io"
    target_port = 443

    sock = None
    tls_sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        if stype == "socks5":
            sock.sendall(b"\x05\x02\x00\x02" if (user or pwd) else b"\x05\x01\x00")
            resp = sock.recv(2)
            if len(resp) < 2 or resp[0] != 5:
                return None
            if resp[1] == 0x02:
                ub, pb = user.encode("utf-8"), pwd.encode("utf-8")
                sock.sendall(bytes([1, len(ub)]) + ub + bytes([len(pb)]) + pb)
                if sock.recv(2) != b"\x01\x00":
                    return None
            elif resp[1] != 0x00:
                return None
            hb = target_host.encode("ascii")
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", target_port))
            rep = sock.recv(10)
            if len(rep) < 2 or rep[1] != 0x00:
                return None
        else:
            headers = [f"CONNECT {target_host}:{target_port} HTTP/1.1", f"Host: {target_host}:{target_port}"]
            if user or pwd:
                token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
                headers.append(f"Proxy-Authorization: Basic {token}")
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            raw = sock.recv(1024).decode("latin1", errors="ignore")
            first = raw.splitlines()[0] if raw else ""
            parts = first.split()
            if len(parts) < 2 or parts[1] != "200":
                return None

        ctx = ssl.create_default_context()
        tls_sock = ctx.wrap_socket(sock, server_hostname=target_host)
        tls_sock.settimeout(timeout)
        req = f"GET /json HTTP/1.1\r\nHost: {target_host}\r\nUser-Agent: cchouse-up/1\r\nConnection: close\r\n\r\n"
        tls_sock.sendall(req.encode("ascii"))
        chunks = []
        while True:
            data = tls_sock.recv(4096)
            if not data:
                break
            chunks.append(data)
        raw_http = b"".join(chunks).decode("utf-8", errors="replace")
        if "\r\n\r\n" not in raw_http:
            return None
        body = raw_http.split("\r\n\r\n", 1)[1].strip()
        info = json.loads(body)
        org = (info.get("org") or "").split()
        return {
            "ip": (info.get("ip") or "").strip(),
            "timezone": (info.get("timezone") or "").strip(),
            "country": (info.get("country") or "").strip(),
            "asn": org[0] if org else "",
        }
    except Exception:
        return None
    finally:
        for s in (tls_sock, sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def generate_ssh_keypair(private_dir, tag):
    """在私有目录生成或复用 ed25519 SSH 密钥对。"""
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(private_dir, 0o700)
    key_path = private_dir / "id_ed25519"
    pub_path = private_dir / "id_ed25519.pub"
    if not (key_path.is_file() and pub_path.is_file()):
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            raise RuntimeError("未找到 ssh-keygen，请先安装 OpenSSH 客户端")
        proc = subprocess.run(
            [keygen, "-t", "ed25519", "-N", "", "-C", f"cchouse-{tag}", "-f", str(key_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ssh-keygen 生成密钥失败（exit={proc.returncode}）")
    if os.name != "nt" and key_path.is_file():
        os.chmod(key_path, 0o600)
    pubkey = pub_path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    return key_path, pubkey


def _env_quote_double(val):
    escaped = str(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _env_quote_single(val):
    _check_safe_env_value(str(val), "env")
    return f"'{val}'"


def format_instance_env(tag, route, private_dir, ssh_key, ssh_pubkey, single_fields, net_cfg, egress_cfg):
    guest_if = "enp0s1" if route == "tart-linux" else "ens33"
    known_hosts = Path(private_dir) / "known_hosts"
    lines = [
        "# Generated by scripts/cchouse.py up (600 permissions; keep private, never commit)",
        "GUEST_OS=linux",
        "EXIT_MODE=single",
        "STRIPE_EXIT_MODE=off",
        "",
        f"TAG={tag}",
        f"VM_NAME=claude-{tag}",
        f"GUEST_IF={guest_if}",
        "MIHOMO_USER=mihomo",
        "",
        f"PRIVATE_DIR={_env_quote_double(private_dir)}",
        'VMX_PATH=""',
        f"SSH_KEY={_env_quote_double(ssh_key)}",
        f"SSH_KNOWN_HOSTS={_env_quote_double(known_hosts)}",
        f"SSH_PUBKEY={_env_quote_single(ssh_pubkey)}",
        "",
        f"VMNET_CIDR={net_cfg['vmnet_cidr']}",
        f"GUEST_IP={net_cfg['guest_ip']}",
        f"ROUTER_IP={net_cfg['router_ip']}",
        f"HOME_LAN_CIDR={net_cfg['home_lan_cidr']}",
        f"TIMEZONE={egress_cfg['timezone']}",
        "GUEST_LOCALE=en_US.UTF-8",
        "GUEST_KEYBOARD=us",
        "GUEST_HOSTNAME=''",
        f"EXPECTED_EGRESS_IP={egress_cfg['ip']}",
        f"EXPECTED_EGRESS_LOC={egress_cfg['loc']}",
        f"EXPECTED_EGRESS_ASN={egress_cfg['asn']}",
        "",
        f"SINGLE_TYPE={single_fields['SINGLE_TYPE']}",
        f"SINGLE_SERVER={_env_quote_single(single_fields['SINGLE_SERVER'])}",
        f"SINGLE_SERVER_IP={_env_quote_single(single_fields['SINGLE_SERVER_IP'])}",
        f"SINGLE_PORT={_env_quote_single(single_fields['SINGLE_PORT'])}",
        f"SINGLE_USER={_env_quote_single(single_fields['SINGLE_USER'])}",
        f"SINGLE_PASS={_env_quote_single(single_fields['SINGLE_PASS'])}",
        f"SINGLE_UUID={_env_quote_single(single_fields['SINGLE_UUID'])}",
        f"SINGLE_SNI={_env_quote_single(single_fields['SINGLE_SNI'])}",
        f"SINGLE_FINGERPRINT={_env_quote_single(single_fields['SINGLE_FINGERPRINT'])}",
        f"SINGLE_FLOW={_env_quote_single(single_fields['SINGLE_FLOW'])}",
        f"SINGLE_NETWORK={_env_quote_single(single_fields['SINGLE_NETWORK'])}",
        f"SINGLE_WS_PATH={_env_quote_single(single_fields['SINGLE_WS_PATH'])}",
        f"SINGLE_WS_HOST={_env_quote_single(single_fields['SINGLE_WS_HOST'])}",
        f"SINGLE_PUBLIC_KEY={_env_quote_single(single_fields['SINGLE_PUBLIC_KEY'])}",
        f"SINGLE_SHORT_ID={_env_quote_single(single_fields['SINGLE_SHORT_ID'])}",
        f"SINGLE_CIPHER={_env_quote_single(single_fields['SINGLE_CIPHER'])}",
        f"SINGLE_TLS={_env_quote_single(single_fields['SINGLE_TLS'])}",
        f"SINGLE_HOST_KEY={_env_quote_single(single_fields['SINGLE_HOST_KEY'])}",
    ]
    return "\n".join(lines) + "\n"


def write_env_secure(env_path, content):
    env_path = Path(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(env_path.parent, 0o700)
        fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content.replace("\r\n", "\n").encode("utf-8"))
        os.chmod(env_path, 0o600)
    else:
        env_path.write_bytes(content.replace("\r\n", "\n").encode("utf-8"))


def run_preflight_check(env_path, route, check_stripe=True):
    cmd = [sys.executable, "-I", str(ROOT / "scripts" / "preflight.py"), "--route", route, "--env", str(env_path)]
    if check_stripe:
        cmd.append("--check-stripe")
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        timeout=30,
    )
    parsed = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, parsed, proc.stderr.strip()


def _prompt_with_default(prompt_label, default_val):
    suffix = f" [{default_val}]" if default_val else " [可回车留空]"
    ans = input(f"{prompt_label}{suffix}: ").strip()
    return ans if ans else default_val


def _print_next_steps(env_path, private_dir, route):
    doc = "docs/install-tart-linux.md" if route == "tart-linux" else "docs/install-vmware-linux.md"
    py = "python -I" if platform.system() == "Windows" else "python3 -I"
    rendered_dir = Path(private_dir) / "rendered"
    seed_iso = Path(private_dir) / "seed.iso"
    print("\n== 下一步命令 ==")
    print(f"1. 渲染客机配置（如节点为域名可加 --resolve）：")
    print(f'   {py} scripts/render.py --resolve "{env_path}" "{rendered_dir}"')
    print("2. 制作首次开机离线注入光盘（seed.iso）：")
    print(f'   {py} scripts/make-seed-iso.py "{rendered_dir}" "{seed_iso}"')
    print(f"3. 按 {doc} 导入/克隆黄金镜像、挂载 seed.iso 启动首次注入，完成后立即删除 rendered/ 与 seed.iso。")
    print("4. 首次注入完成后运行自动验收（必须退出码 0）：")
    print(f'   bash scripts/verify-linux.sh "{env_path}" --fail-closed')


def cmd_up(args):
    tag = (args.tag or "cc01").strip().lower()
    if not SAFE_TAG_RE.fullmatch(tag):
        sys.exit("错误：--tag 仅支持小写字母开头的 2-16 位字母/数字/下划线/短横线（如 cc01）")

    route = detect_route() if args.route == "auto" else args.route
    private_dir = Path(args.private_dir).expanduser() if args.private_dir else (default_private_root() / tag)
    if _is_inside_repo(private_dir):
        sys.exit("错误：私有目录不能放在 cchouse 仓库内，请改用用户私有目录")

    # 1. 无回显读取节点链接（绝不回显、不写日志）
    if args.link_stdin:
        raw_link = sys.stdin.readline().rstrip("\r\n")
    else:
        try:
            raw_link = getpass.getpass("请粘贴单节点代理链接（输入不回显，支持 socks5/http/vless/trojan/ss）: ")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n已取消。")

    try:
        single_fields = parse_node_link(raw_link)
    except NodeLinkError as exc:
        sys.exit(f"节点链接校验失败：{exc}")
    finally:
        raw_link = ""

    # 2. 若服务器为域名，尝试在宿主机解析并钉死 IPv4
    if not single_fields["SINGLE_SERVER_IP"]:
        resolved = resolve_server_ipv4(single_fields["SINGLE_SERVER"])
        if resolved:
            single_fields["SINGLE_SERVER_IP"] = resolved

    print(f"[cchouse up] 路线：{route} | 协议：{single_fields['SINGLE_TYPE']} | 端口：{single_fields['SINGLE_PORT']}")

    # 3. 可选探测出口信息（仅 socks5/http）
    probed = None
    if not args.no_probe and single_fields["SINGLE_TYPE"] in ("socks5", "http"):
        print("[cchouse up] 正在通过代理探测出口 IP 与时区（凭据不落盘不回显）...")
        probed = probe_egress_via_proxy(single_fields)
        if probed and probed.get("ip"):
            print(
                f"[cchouse up] 探测结果：出口 IP={probed['ip']}，时区={probed.get('timezone') or '未知'}，"
                f"国家={probed.get('country') or '未知'}，ASN={probed.get('asn') or '未知'}"
            )
        else:
            print("[cchouse up] 未自动探测到出口信息，可在下方手动输入或回车留空。")

    default_tz = args.timezone or (probed.get("timezone") if probed else "") or "America/Los_Angeles"
    default_ip = args.expected_ip or (probed.get("ip") if probed else "")
    default_loc = (probed.get("country") if probed else "") or "US"
    default_asn = (probed.get("asn") if probed else "")

    if args.non_interactive:
        tz = default_tz
        expected_ip = default_ip
    else:
        tz = _prompt_with_default("出口时区 TIMEZONE", default_tz)
        expected_ip = _prompt_with_default("预期出口 IP (EXPECTED_EGRESS_IP)", default_ip)

    # 4. 生成 SSH 密钥对
    try:
        ssh_key, ssh_pubkey = generate_ssh_keypair(private_dir, tag)
    except RuntimeError as exc:
        sys.exit(f"错误：{exc}")

    # 5. 网络默认值
    if route == "tart-linux":
        vmnet_cidr = "192.168.64.0/24"
        guest_ip = "192.168.64.2"
    else:
        vmnet_cidr = preflight_mod.detect_vmnet8_cidr() or "192.168.180.0/24"
        try:
            net = ipaddress.ip_network(vmnet_cidr, strict=False)
            guest_ip = str(net.network_address + 128)
        except ValueError:
            guest_ip = "192.168.180.128"

    net_cfg = {
        "vmnet_cidr": vmnet_cidr,
        "guest_ip": guest_ip,
        "router_ip": "192.168.100.1",
        "home_lan_cidr": "192.168.100.0/24",
    }
    egress_cfg = {
        "timezone": tz,
        "ip": expected_ip,
        "loc": default_loc,
        "asn": default_asn,
    }

    env_path = private_dir / "instance.env"
    env_text = format_instance_env(
        tag=tag,
        route=route,
        private_dir=private_dir,
        ssh_key=ssh_key,
        ssh_pubkey=ssh_pubkey,
        single_fields=single_fields,
        net_cfg=net_cfg,
        egress_cfg=egress_cfg,
    )
    write_env_secure(env_path, env_text)
    print(f"[cchouse up] 已生成私有配置（权限 600）：{env_path}")

    # 6. 运行 preflight（含 Stripe 探测：只提示不拦截）
    rc, pf_json, pf_stderr = run_preflight_check(env_path, route, check_stripe=(not args.no_probe))
    print("\n== preflight 自检结果 ==")
    if pf_json:
        print(f"状态：{pf_json.get('status')} | ok={pf_json.get('ok')} | next_action={pf_json.get('next_action')}")
        for w in pf_json.get("warnings", []):
            print(f"  WARN  {w}")
        for e in pf_json.get("errors", []):
            print(f"  ERR   {e}")
    else:
        print(f"preflight 退出码 {rc}：{pf_stderr}")

    _print_next_steps(env_path, private_dir, route)
    return 0 if (pf_json is None or pf_json.get("ok") or pf_json.get("status") == "HUMAN_WAIT") else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="cchouse CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="交互式解析节点链接、生成私有 instance.env 并运行 preflight")
    p_up.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_up.add_argument("--route", choices=("auto", "tart-linux", "vmware-linux"), default="auto")
    p_up.add_argument("--private-dir", default=None, help="覆盖私有目录路径（不能位于仓库内）")
    p_up.add_argument("--timezone", default=None, help="默认出口时区（非交互模式直接采用）")
    p_up.add_argument("--expected-ip", default=None, help="默认预期出口 IP（非交互模式直接采用）")
    p_up.add_argument("--link-stdin", action="store_true", help="从标准输入读取一行节点链接（不回显，适合脚本/测试）")
    p_up.add_argument("--non-interactive", action="store_true", help="跳过交互问答，使用探测或默认值")
    p_up.add_argument("--no-probe", action="store_true", help="跳过宿主机代理出口与 Stripe 探测")

    args = parser.parse_args()
    if args.command == "up":
        sys.exit(cmd_up(args))


if __name__ == "__main__":
    main()
