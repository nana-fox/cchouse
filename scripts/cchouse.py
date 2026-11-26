#!/usr/bin/env python3
"""cchouse 一条命令安装向导（当前子命令：up）。

用法：
  python3 -I scripts/cchouse.py up [--tag cc01] [--route auto|tart-linux|vmware-linux] [--dry-run] [--config-only]

流程（耗时的先下载，配置并行准备，下完一键装好）：
  0. 启动即在后台下载黄金镜像（guest-linux/image-manifest.json 的 URL + SHA-256）与 Claude Code 官方 npm 包（SRI），
     走宿主网络、断点续传、已校验则跳过（scripts/downloads.py）。
  1-2. 下载同时：无回显读取单节点链接 → 解析 → 探测出口 IP/时区 → 生成密钥与 600 权限 instance.env → preflight。
  3. 渲染 + 生成 seed.iso（渲染明文打包后立即删除）。
  4-9. 下载校验完成后：导入/克隆镜像 → 挂 seed.iso 首启注入 → 删明文并联网重启 → verify-linux.sh --fail-closed → 干净快照。
     Tart 用 scripts/vm-tart.sh，VMware(Windows) 用 scripts/vm-vmware.ps1，与 tools/e2e 共用同一套函数。
每步开始/结束/耗时/是否命中缓存写入 <私有目录>/state.json，结束打印耗时表；任一步失败即停，
再次运行同一命令从失败步骤继续，并提示用 scripts/doctor.py 导出脱敏诊断包。
红线：链接和密码不回显、不写日志；明文用完即删；绝不放宽断网保护。
"""
import argparse
import base64
import datetime
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
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import downloads  # noqa: E402
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
    try:
        ans = input(f"{prompt_label}{suffix}: ").strip()
    except EOFError:
        if default_val:
            print(f"\n输入已结束，使用默认值 [{default_val}]。")
            return default_val
        raise StepError("输入已结束，请在终端交互运行或加 --non-interactive") from None
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


UP_STEPS = (
    ("config", "解析节点链接并生成 instance.env"),
    ("preflight", "preflight 自检"),
    ("seed", "渲染客机配置并生成 seed.iso"),
    ("download", "等待黄金镜像下载校验完成"),
    ("import", "导入/克隆黄金镜像"),
    ("firstboot", "挂载 seed.iso 离线首启注入"),
    ("boot", "删除明文 seed.iso 并联网重启"),
    ("verify", "verify-linux.sh --fail-closed 自动验收"),
    ("snapshot", "保存干净快照并交给用户"),
)
CLEAN_SNAPSHOT = "cchouse-clean"


class StepError(RuntimeError):
    """某一步失败（文案不含链接或凭据）。"""


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def detect_vm_backend(route):
    """tart：Apple Silicon 宿主；vmware：Windows 宿主（vmrun + PowerShell）；其余（如 Intel Mac Fusion）先手工。"""
    sys_name = platform.system()
    if route == "tart-linux":
        return "tart" if sys_name == "Darwin" else "manual"
    return "vmware" if sys_name == "Windows" else "manual"


def find_bash():
    if os.name == "nt":
        for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(p).is_file():
                return p
    return shutil.which("bash") or "/bin/bash"


def update_env_value(env_path, key, value):
    """改写 instance.env 中单个非敏感键（GUEST_IP / VMX_PATH），保持 600 权限。"""
    text = Path(env_path).read_text(encoding="utf-8")
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    text = pattern.sub(lambda _m: line, text, count=1) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    write_env_secure(env_path, text)


def _fmt_secs(sec):
    if sec is None:
        return "-"
    sec = float(sec)
    return f"{sec:.1f}s" if sec < 90 else f"{int(sec // 60)}m{int(sec % 60):02d}s"


class Downloader:
    """后台线程下载黄金镜像与 Claude Code 包（走宿主网络，不经过用户节点）。"""

    def __init__(self):
        self.items = {}
        self._last_print = 0.0

    def start(self, key, label, fn):
        if key in self.items:
            return
        prog = downloads.Progress(label)
        rec = {"label": label, "network": downloads.network_label(), "status": "running", "started_at": _now_iso()}

        def run():
            t0 = time.monotonic()
            try:
                res = fn(prog)
                rec["status"] = res
            except Exception as exc:  # noqa: BLE001 - 进度对象带出错误，主线程决定如何处理
                rec["status"] = "failed"
                rec["error"] = str(exc)
                if prog.snapshot()["state"] != "failed":
                    prog.update(state="failed", error=str(exc))
            secs = time.monotonic() - t0
            snap = prog.snapshot()
            fetched = max(0, snap["done"] - prog.start) if rec["status"] == "downloaded" else 0
            rec.update(
                ended_at=_now_iso(),
                seconds=round(secs, 1),
                bytes_total=snap["total"] or snap["done"],
                bytes_downloaded=fetched,
                speed_mb_s=round(fetched / 1048576 / secs, 2) if fetched and secs > 0 else 0,
            )

        th = threading.Thread(target=run, name=f"cchouse-dl-{key}", daemon=True)
        self.items[key] = (prog, th, rec)
        th.start()

    def status_line(self):
        return " | ".join(prog.render() for prog, _th, _rec in self.items.values())

    def active(self):
        return any(th.is_alive() for _p, th, _r in self.items.values())

    def print_status(self):
        if self.items:
            print(f"  [后台下载] {self.status_line()}")
            self.print_slow_hints()

    def print_slow_hints(self, newline=False):
        for prog, _th, rec in self.items.values():
            slow = prog.slow_hint()
            if slow is not None:
                rec["slow_hint_mb_s"] = round(slow, 2)
                print(("\n" if newline else "") + "  " + downloads.slow_download_tip(prog.label, slow), flush=True)

    def wait(self, key):
        prog, th, rec = self.items[key]
        tty = sys.stdout.isatty()
        while th.is_alive():
            th.join(1.0)
            now = time.monotonic()
            if tty:
                print(f"\r  [下载] {self.status_line()}    ", end="", flush=True)
            elif now - self._last_print >= 15:
                print(f"  [下载] {self.status_line()}", flush=True)
                self._last_print = now
            self.print_slow_hints(newline=tty)
        if tty:
            print()
        if rec["status"] == "failed":
            raise StepError(f"{prog.label} 下载失败：{rec.get('error')}")
        return rec

    def records(self):
        return {key: dict(rec) for key, (_p, _t, rec) in self.items.items()}


class UpContext:
    def __init__(self, args, tag, route, private_dir):
        self.args = args
        self.tag = tag
        self.route = route
        self.private_dir = private_dir
        self.env_path = private_dir / "instance.env"
        self.state_path = private_dir / "state.json"
        self.rendered = private_dir / "rendered"
        self.seed_iso = private_dir / "seed.iso"
        self.logs = private_dir / "logs"
        self.ip_file = private_dir / "guest_ip.txt"
        self.ssh_key = private_dir / "id_ed25519"
        self.known_hosts = private_dir / "known_hosts"
        self.cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else default_private_root() / "cache"
        self.dry_run = args.dry_run
        self.backend = detect_vm_backend(route) if getattr(args, "vm_backend", "auto") == "auto" else args.vm_backend
        self.vm_name = f"claude-{tag}"
        self.vmx = private_dir / "vm" / self.vm_name / f"{self.vm_name}.vmx"
        self.artifact = downloads.load_image_artifact(ROOT / "guest-linux" / "image-manifest.json", route)
        self.image_path = self.cache_dir / "images" / self.artifact["filename"]
        self.golden_name = f"cchouse-debian13-arm64-{self.artifact['image_version']}"
        self.claude_platform = "linux-arm64" if route == "tart-linux" else "linux-x64"
        self.claude_path = self.cache_dir / f"claude-code-{self.claude_platform}.tgz"
        self.downloader = Downloader()
        self.state = self._load_state()

    # ---- state.json（断点续跑 + 计时） ----
    def _load_state(self):
        data = {}
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data.setdefault("completed_steps", [])
        data.setdefault("timings", {})
        data.setdefault("downloads", {})
        return data

    def save_state(self):
        if self.dry_run:
            return
        self.state["updated_at"] = _now_iso()
        self.state["route"] = self.route
        self.state["downloads"].update(self.downloader.records())
        self.private_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    def done(self, name):
        return f"up:{name}" in self.state["completed_steps"]

    def mark_done(self, name):
        if not self.done(name):
            self.state["completed_steps"].append(f"up:{name}")

    def unmark(self, name):
        self.state["completed_steps"] = [s for s in self.state["completed_steps"] if s != f"up:{name}"]

    # ---- 命令执行 ----
    def run(self, cmd, env=None):
        shown = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
        if self.dry_run:
            print(f"  [dry-run] {shown}")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        print(f"  $ {shown}", flush=True)
        try:
            return subprocess.run([str(c) for c in cmd], check=True, env=env)
        except subprocess.CalledProcessError as exc:
            name = next((Path(str(c)).name for c in cmd if str(c).endswith((".sh", ".ps1", ".py"))), Path(str(cmd[0])).name)
            op = cmd[cmd.index("-CcOp") + 1] if "-CcOp" in cmd else (cmd[2] if name == "vm-tart.sh" else "")
            what = f"{name} {op}".strip()
            raise StepError(f"{what} 退出码 {exc.returncode}") from None
        except FileNotFoundError:
            raise StepError(f"找不到命令：{cmd[0]}") from None

    def tart(self, *op):
        return self.run(["bash", ROOT / "scripts" / "vm-tart.sh", *op])

    def vmware(self, op, **kw):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ROOT / "scripts" / "vm-vmware.ps1", "-CcOp", op]
        for k, v in kw.items():
            cmd += [f"-Cc{k}", v]
        return self.run(cmd)

    def start_downloads(self):
        need_claude = not self.done("firstboot")
        need_image = not self.done("import")
        if self.dry_run:
            if need_image:
                print(f"  [dry-run] 后台下载镜像 {downloads.image_urls(self.artifact, self.args.image_mirror)[0]} -> {self.image_path}（sha256 校验，断点续传）")
            if need_claude:
                print(f"  [dry-run] 后台下载 Claude Code {self.claude_platform} -> {self.claude_path}（npm SRI 校验）")
            return
        if need_image:
            art = self.artifact
            self.downloader.start(
                "image",
                f"镜像 {art['filename']}",
                lambda prog: downloads.fetch_verified(
                    downloads.image_urls(art, self.args.image_mirror), self.image_path, sha256=art["sha256"],
                    size=art.get("size_bytes"), progress=prog, log=self.download_log,
                ),
            )
        if need_claude:
            self.downloader.start(
                "claude",
                f"Claude Code {self.claude_platform}",
                lambda prog: downloads.fetch_claude_code(
                    self.claude_platform, self.claude_path, progress=prog, mirror=self.args.npm_mirror,
                    log=self.download_log,
                )["status"],
            )

    @staticmethod
    def download_log(msg):
        print(f"\n  [下载] {msg}", flush=True)

    def wait_download(self, key):
        if self.dry_run:
            print(f"  [dry-run] 等待 {key} 下载校验完成")
            return False
        if key not in self.downloader.items:
            self.start_downloads()
        if key not in self.downloader.items:
            raise StepError(f"{key} 下载未启动")
        rec = self.downloader.wait(key)
        return rec["status"] == "skipped"

    def delete_plaintext(self):
        shutil.rmtree(self.rendered, ignore_errors=True)
        try:
            self.seed_iso.unlink()
        except FileNotFoundError:
            pass

    def py(self):
        return "python -I" if platform.system() == "Windows" else "python3 -I"

    def rerun_cmd(self):
        parts = [self.py(), "scripts/cchouse.py", "up", "--tag", self.tag]
        if self.args.private_dir:
            parts += ["--private-dir", f'"{self.private_dir}"']
        if self.args.route != "auto":
            parts += ["--route", self.route]
        return " ".join(parts)


def do_config(ctx):
    """第一轮逻辑：无回显读取节点链接 → 解析 → 探测出口 → 生成密钥 → 写 600 权限 instance.env。"""
    args = ctx.args
    tag, route, private_dir = ctx.tag, ctx.route, ctx.private_dir
    try:
        if args.link_stdin:
            raw_link = sys.stdin.readline()
            if raw_link == "":
                raise EOFError
            raw_link = raw_link.rstrip("\r\n")
        else:
            raw_link = getpass.getpass("请粘贴单节点代理链接（输入不回显，支持 socks5/http/vless/trojan/ss）: ")
    except EOFError:
        raise StepError("输入已结束，请在终端交互运行，或用 --link-stdin 提供节点链接") from None
    except KeyboardInterrupt:
        raise StepError("已取消输入节点链接") from None

    try:
        single_fields = parse_node_link(raw_link)
    except NodeLinkError as exc:
        raise StepError(f"节点链接校验失败：{exc}") from None
    finally:
        raw_link = ""

    if not single_fields["SINGLE_SERVER_IP"]:
        resolved = resolve_server_ipv4(single_fields["SINGLE_SERVER"])
        if resolved:
            single_fields["SINGLE_SERVER_IP"] = resolved

    print(f"[cchouse up] 路线：{route} | 协议：{single_fields['SINGLE_TYPE']} | 端口：{single_fields['SINGLE_PORT']}")

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
        ctx.downloader.print_status()
        tz = _prompt_with_default("出口时区 TIMEZONE（回车确认）", default_tz)
        expected_ip = _prompt_with_default("预期出口 IP EXPECTED_EGRESS_IP（回车确认）", default_ip)

    try:
        ssh_key, ssh_pubkey = generate_ssh_keypair(private_dir, tag)
    except RuntimeError as exc:
        raise StepError(str(exc)) from None

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
    egress_cfg = {"timezone": tz, "ip": expected_ip, "loc": default_loc, "asn": default_asn}
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
    write_env_secure(ctx.env_path, env_text)
    print(f"[cchouse up] 已生成私有配置（权限 600）：{ctx.env_path}")


def step_config(ctx):
    if ctx.env_path.is_file() and not ctx.args.reconfigure:
        print(f"  已有私有配置 {ctx.env_path}，沿用（更换节点请加 --reconfigure）")
        return True
    do_config(ctx)
    return False


def step_preflight(ctx):
    rc, pf_json, pf_stderr = run_preflight_check(ctx.env_path, ctx.route, check_stripe=(not ctx.args.no_probe))
    if not pf_json:
        msg = f"preflight 无法运行（退出码 {rc}）：{pf_stderr[-300:]}"
        if ctx.dry_run:
            print(f"  [dry-run] {msg}")
            return False
        raise StepError(msg)
    print(f"  状态：{pf_json.get('status')} | ok={pf_json.get('ok')} | next_action={pf_json.get('next_action')}")
    for w in pf_json.get("warnings", []):
        print(f"  WARN  {w}")
    for e in pf_json.get("errors", []):
        print(f"  ERR   {e}")
    if not pf_json.get("ok"):
        if ctx.dry_run:
            print("  [dry-run] preflight 未通过，真实运行会停在这一步")
            return False
        raise StepError(f"preflight 未通过（{pf_json.get('status')}），按上方 ERR 处理后重新运行")
    return False


def step_seed(ctx):
    ctx.wait_download("claude")
    py = sys.executable
    try:
        ctx.run([py, "-I", ROOT / "scripts" / "render.py", "--resolve", ctx.env_path, ctx.rendered])
        ctx.run([py, "-I", ROOT / "scripts" / "make-seed-iso.py", ctx.rendered, ctx.seed_iso, "--claude-tarball", ctx.claude_path])
    finally:
        # 渲染明文只用于打包 ISO，用完即删
        shutil.rmtree(ctx.rendered, ignore_errors=True)
    return False


def step_download(ctx):
    return ctx.wait_download("image")


def vm_exists(ctx):
    if ctx.dry_run:
        return False
    if ctx.backend == "tart":
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "vm-tart.sh"), "exists", ctx.vm_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    return ctx.vmx.is_file()


def step_import(ctx):
    # 只重建 up 自己建过的虚拟机；同名的既有虚拟机（可能已登录）绝不覆盖
    if not ctx.state.get("vm_created_by_up") and vm_exists(ctx):
        raise StepError(
            f"已存在同名虚拟机 {ctx.vm_name}（非本次 up 创建），为避免覆盖已登录环境不会删除；"
            "请换 --tag，或确认无用后自行删除再运行"
        )
    if ctx.backend == "tart":
        existed = (not ctx.dry_run) and subprocess.run(
            ["bash", str(ROOT / "scripts" / "vm-tart.sh"), "exists", ctx.golden_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if existed:
            print(f"  本机已有黄金镜像 {ctx.golden_name}，直接克隆（热缓存）")
        else:
            ctx.tart("import", ctx.golden_name, ctx.image_path)
        ctx.state["vm_created_by_up"] = True
        ctx.save_state()
        ctx.tart("clone", ctx.golden_name, ctx.vm_name)
        return existed
    ctx.state["vm_created_by_up"] = True
    ctx.save_state()
    ctx.vmware("import", Vmx=ctx.vmx, Ova=ctx.image_path, VmName=ctx.vm_name, VmDir=ctx.private_dir / "vm")
    if not ctx.dry_run:
        update_env_value(ctx.env_path, "VMX_PATH", _env_quote_double(ctx.vmx))
    return False


def step_firstboot(ctx):
    if not ctx.dry_run and not ctx.seed_iso.is_file():
        raise StepError("seed.iso 不存在（可能已被清理），再次运行会重新生成")
    if not ctx.dry_run:
        ctx.logs.mkdir(parents=True, exist_ok=True)
    if ctx.backend == "tart":
        ctx.tart("firstboot", ctx.vm_name, ctx.seed_iso, ctx.ssh_key, ctx.logs / "tart-firstboot.log")
    else:
        ctx.vmware("firstboot", Vmx=ctx.vmx, SeedIso=ctx.seed_iso)
    return False


def step_boot(ctx):
    if not ctx.dry_run:
        ctx.logs.mkdir(parents=True, exist_ok=True)
    if ctx.backend == "tart":
        ctx.delete_plaintext()
        ctx.tart("boot", ctx.vm_name, ctx.ssh_key, ctx.known_hosts, ctx.ip_file, ctx.logs / "tart-run.log")
    else:
        ctx.vmware("boot", Vmx=ctx.vmx, SeedIso=ctx.seed_iso, Key=ctx.ssh_key, KnownHosts=ctx.known_hosts, IpFile=ctx.ip_file)
        ctx.delete_plaintext()
    if ctx.dry_run:
        return False
    ip = ctx.ip_file.read_text(encoding="utf-8").strip()
    if not render_mod.is_ipv4(ip):
        raise StepError("未拿到客机 IPv4 地址")
    update_env_value(ctx.env_path, "GUEST_IP", ip)
    print(f"  客机已联网：claude@{ip}（明文 seed.iso / rendered 已删除）")
    return False


def step_verify(ctx):
    env = os.environ.copy()
    if os.name == "nt":
        env["PATH"] = r"C:\Windows\System32\OpenSSH;" + env.get("PATH", "")
    try:
        ctx.run([find_bash(), ROOT / "scripts" / "verify-linux.sh", ctx.env_path, "--fail-closed"], env=env)
    except StepError as exc:
        raise StepError(f"自动验收未通过（{exc}）；断网保护不会放宽，请按上方 FAIL 项排查代理") from None
    return False


def step_snapshot(ctx):
    if ctx.backend == "tart":
        snap = f"{ctx.vm_name}-clean"
        ctx.tart("snapshot", ctx.vm_name, snap)
        ctx.tart("gui", ctx.vm_name, ctx.logs / "tart-gui.log")
    else:
        snap = CLEAN_SNAPSHOT
        ctx.vmware("snapshot", Vmx=ctx.vmx, Snapshot=snap)
    ctx.state["clean_snapshot"] = snap
    return False


STEP_FUNCS = {
    "config": step_config,
    "preflight": step_preflight,
    "seed": step_seed,
    "download": step_download,
    "import": step_import,
    "firstboot": step_firstboot,
    "boot": step_boot,
    "verify": step_verify,
    "snapshot": step_snapshot,
}


def needs_redo(ctx, name):
    # 失败时明文 seed.iso 会被删除；首启前恢复运行需重新生成
    return name == "seed" and not ctx.done("firstboot") and not ctx.seed_iso.is_file()


def print_timing_table(ctx, steps):
    ctx.state["downloads"].update(ctx.downloader.records())  # 失败路径先打印后保存，先刷新下载记录
    print("\n== 耗时表 ==")
    print(f"{'步骤':<28}{'耗时':>10}  命中缓存")
    total = 0.0
    for i, (name, title) in enumerate(steps, 1):
        t = ctx.state["timings"].get(name)
        if not t:
            continue
        secs = t.get("seconds")
        total += secs or 0
        mark = {"cached": "是", "fresh": "否", "failed": "失败", "resumed": "上次已完成"}.get(t.get("result"), "-")
        print(f"{i}. {title:<25}{_fmt_secs(secs):>10}  {mark}")
    for rec in ctx.state["downloads"].values():
        mb = (rec.get("bytes_total") or 0) / 1048576
        how = "缓存命中" if rec.get("status") == "skipped" else rec.get("status")
        print(
            f"   下载 {rec.get('label')}: {mb:.0f} MB，{_fmt_secs(rec.get('seconds'))}，"
            f"{rec.get('speed_mb_s', 0)} MB/s，{rec.get('network')}，{how}"
        )
    img = ctx.state["downloads"].get("image", {})
    imp = ctx.state["timings"].get("import", {})
    failed = any(t.get("result") == "failed" for t in ctx.state["timings"].values())
    cold = img.get("status") == "downloaded" and imp.get("result") == "fresh"
    ctx.state["run_kind"] = "incomplete" if failed else ("cold" if cold else "warm")
    kind = {"incomplete": "未完成（有失败步骤）", "cold": "冷启动", "warm": "热缓存/续跑（不代表首次耗时）"}[ctx.state["run_kind"]]
    print(f"合计（本次各步）：{_fmt_secs(total)} | 本次属于：{kind}")


def print_handoff(ctx):
    print("\n== 部署完成，交给你 ==")
    print(f"- 客机 {ctx.vm_name} 已开机，干净快照：{ctx.state.get('clean_snapshot')}")
    print("- 请在客机桌面核对 check.html 全绿，然后自行登录 Claude Code。")
    print("- Claude Code 的 OAuth 登录链接只能在客机浏览器里打开，严禁复制到宿主浏览器。")
    print("- cchouse 不会替你登录、注册、订阅或付款。")


def run_pipeline(ctx):
    steps = UP_STEPS[:2] if ctx.args.config_only else UP_STEPS
    n = len(steps)
    if ctx.dry_run:
        print(f"[cchouse up] dry-run：路线 {ctx.route} | 虚拟机后端 {ctx.backend} | 不下载、不动虚拟机、不写 state.json")
    ctx.start_downloads()
    for i, (name, title) in enumerate(steps, 1):
        if ctx.done(name) and not needs_redo(ctx, name):
            print(f"\n[{i}/{n}] {title}：已完成（state.json），跳过")
            continue
        print(f"\n[{i}/{n}] {title}")
        ctx.downloader.print_status()
        ctx.state.update(step=f"up:{name}", status="RUNNING", ok=False)
        ctx.state.pop("failed_step", None)
        ctx.save_state()
        started, t0 = _now_iso(), time.monotonic()
        try:
            cached = STEP_FUNCS[name](ctx)
        except (StepError, OSError, downloads.DownloadError, KeyboardInterrupt) as exc:
            reason = "用户中断" if isinstance(exc, KeyboardInterrupt) else str(exc)
            ctx.state["timings"][name] = {
                "started_at": started, "ended_at": _now_iso(),
                "seconds": round(time.monotonic() - t0, 1), "result": "failed",
            }
            return on_failure(ctx, steps, i, name, title, reason)
        except BaseException as exc:
            # 非预期异常：同样先删明文、写 FAILED，再原样抛出便于排查（原因只记类型，不记可能含敏感值的消息）
            ctx.state["timings"][name] = {
                "started_at": started, "ended_at": _now_iso(),
                "seconds": round(time.monotonic() - t0, 1), "result": "failed",
            }
            try:
                on_failure(ctx, steps, i, name, title, f"意外错误 {type(exc).__name__}（详见下方 traceback）")
            finally:
                ctx.delete_plaintext()
            raise
        ctx.state["timings"][name] = {
            "started_at": started, "ended_at": _now_iso(),
            "seconds": round(time.monotonic() - t0, 1), "result": "cached" if cached else "fresh",
        }
        ctx.mark_done(name)
        ctx.save_state()
        if name == "download" and ctx.backend == "manual":
            return finish_manual(ctx, steps)

    if ctx.args.config_only:
        ctx.state.update(status="CONFIG_READY", ok=True, next_action="run_cchouse_up")
        ctx.save_state()
        print_timing_table(ctx, steps)
        _print_next_steps(ctx.env_path, ctx.private_dir, ctx.route)
        return 0
    ctx.state.update(step="up:done", status="DONE", ok=True, next_action="human_check_and_login")
    print_timing_table(ctx, steps)
    ctx.save_state()
    if not ctx.dry_run:
        print_handoff(ctx)
    return 0


def on_failure(ctx, steps, i, name, title, reason):
    ctx.delete_plaintext()
    if name == "firstboot":
        ctx.unmark("import")  # 首启半途失败的客机不可信，下次重新克隆/导入
    ctx.state.update(status="FAILED", ok=False, failed_step=f"up:{name}", next_action="rerun_cchouse_up")
    print_timing_table(ctx, steps)
    ctx.save_state()
    env_arg = f' --env "{ctx.env_path}"' if ctx.env_path.is_file() else ""
    vm_arg = " --vm-ssh" if ctx.done("boot") else ""
    print(f"\n✗ 第 {i}/{len(steps)} 步「{title}」失败：{reason}")
    print("  明文 seed.iso / rendered 已删除；后台下载如未完成，已下载部分会保留续传。")
    print("  修复后再次运行同一命令，会从这一步继续（state.json 断点续跑）：")
    print(f"    {ctx.rerun_cmd()}")
    print("  需要求助时导出脱敏诊断包：")
    print(f"    {ctx.py()} scripts/doctor.py{env_arg}{vm_arg}")
    return 1


def finish_manual(ctx, steps):
    ctx.state.update(status="HUMAN_WAIT", ok=True, next_action="manual_import_and_firstboot")
    print_timing_table(ctx, steps)
    ctx.save_state()
    doc = "docs/install-tart-linux.md" if ctx.route == "tart-linux" else "docs/install-vmware-linux.md"
    print("\n== 当前宿主暂不支持自动导入（目前自动化：Apple Silicon + Tart、Windows + VMware Workstation） ==")
    print(f"- 已下载并校验的镜像：{ctx.image_path}")
    print(f"- 首启注入光盘（含明文凭据，首启完成后立即删除）：{ctx.seed_iso}")
    print(f"- 按 {doc} 导入镜像、挂载 seed.iso 首次开机，然后运行：")
    print(f'    bash scripts/verify-linux.sh "{ctx.env_path}" --fail-closed')
    return 0


def cmd_up(args):
    tag = (args.tag or "cc01").strip().lower()
    if not SAFE_TAG_RE.fullmatch(tag):
        sys.exit("错误：--tag 仅支持小写字母开头的 2-16 位字母/数字/下划线/短横线（如 cc01）")

    route = detect_route() if args.route == "auto" else args.route
    private_dir = Path(args.private_dir).expanduser() if args.private_dir else (default_private_root() / tag)
    if _is_inside_repo(private_dir) or (args.cache_dir and _is_inside_repo(Path(args.cache_dir).expanduser())):
        sys.exit("错误：私有目录/缓存目录不能放在 cchouse 仓库内，请改用用户私有目录")

    ctx = UpContext(args, tag, route, private_dir)
    return run_pipeline(ctx)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="cchouse CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="一条命令：后台下载镜像，同时配置节点，下载完自动导入、首启注入、验收并打干净快照")
    p_up.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_up.add_argument("--route", choices=("auto", "tart-linux", "vmware-linux"), default="auto")
    p_up.add_argument("--private-dir", default=None, help="覆盖私有目录路径（不能位于仓库内）")
    p_up.add_argument("--cache-dir", default=None, help="镜像与 Claude Code 包缓存目录（默认 <私有根目录>/cache，多实例共用）")
    p_up.add_argument("--image-mirror", default=None, help=f"黄金镜像镜像源目录 URL（也可用环境变量 {downloads.IMAGE_MIRROR_ENV}），SHA-256 仍按 manifest 校验")
    p_up.add_argument("--npm-mirror", default=None, help=f"Claude Code tarball 镜像源（也可用环境变量 {downloads.NPM_MIRROR_ENV}），integrity 仍取官方 registry")
    p_up.add_argument("--timezone", default=None, help="默认出口时区（非交互模式直接采用）")
    p_up.add_argument("--expected-ip", default=None, help="默认预期出口 IP（非交互模式直接采用）")
    p_up.add_argument("--link-stdin", action="store_true", help="从标准输入读取一行节点链接（不回显，适合脚本/测试）")
    p_up.add_argument("--non-interactive", action="store_true", help="跳过交互问答，使用探测或默认值")
    p_up.add_argument("--no-probe", action="store_true", help="跳过宿主机代理出口与 Stripe 探测")
    p_up.add_argument("--vm-backend", choices=("auto", "tart", "vmware", "manual"), default="auto",
                      help="虚拟机自动化后端（默认按宿主：Apple Silicon→tart，Windows→vmware，其余→manual）")
    p_up.add_argument("--reconfigure", action="store_true", help="忽略已有 instance.env，重新输入节点链接")
    p_up.add_argument("--config-only", action="store_true", help="只生成 instance.env 并跑 preflight（第一轮行为），不下载不建虚拟机")
    p_up.add_argument("--dry-run", action="store_true", help="只演练：生成配置并跑 preflight，其余步骤只打印命令，不下载、不动虚拟机、不写 state.json")

    args = parser.parse_args()
    if args.command == "up":
        sys.exit(cmd_up(args))


if __name__ == "__main__":
    main()
