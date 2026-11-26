#!/usr/bin/env python3
"""cchouse 安装与卸载向导。

用法：
  python3 -I scripts/cchouse.py up [--tag cc01] [--route auto|tart-linux|vmware-linux]
      [--vm-backend auto|tart|vmware|virtualbox|manual] [--dry-run] [--config-only]
  python3 -I scripts/cchouse.py remove --tag cc01 [--yes] [--purge-cache]

流程（耗时的先下载，配置并行准备，下完一键装好）：
  0. 启动即在后台下载黄金镜像（guest-linux/image-manifest.json 的 URL + SHA-256）与 Claude Code 官方 npm 包（SRI），
     走宿主网络、断点续传、已校验则跳过（scripts/downloads.py）。
  1-2. 下载同时：无回显读取单节点链接 → 解析 → 探测出口 IP/时区 → 生成密钥与 600 权限 instance.env → preflight。
  3. 渲染 + 生成 seed.iso（渲染明文打包后立即删除）。
  4-9. 下载校验完成后：导入/克隆镜像 → 挂 seed.iso 首启注入 → 删明文并联网重启 → verify-linux.sh --fail-closed → 干净快照。
     Apple Silicon 用 scripts/vm-tart.sh，Windows 默认用 scripts/vm-virtualbox.ps1（NAT + 127.0.0.1 端口转发）。
每步开始/结束/耗时/是否命中缓存写入 <私有目录>/state.json，结束打印耗时表；任一步失败即停，
再次运行同一命令从失败步骤继续，并提示用 scripts/doctor.py 导出脱敏诊断包。
红线：链接和密码不回显、不写日志；明文用完即删；绝不放宽断网保护。
"""
import argparse
import base64
import datetime
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
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


def format_instance_env(tag, route, private_dir, ssh_key, ssh_pubkey, single_fields, net_cfg, egress_cfg, backend=None, ssh_port=None):
    if backend == "virtualbox":
        guest_if = VBOX_GUEST_IF
    else:
        guest_if = "enp0s1" if route == "tart-linux" else "ens33"
    known_hosts = Path(private_dir) / "known_hosts"
    net_lines = [
        f"VMNET_CIDR={net_cfg['vmnet_cidr']}",
        f"GUEST_IP={net_cfg['guest_ip']}",
    ]
    if backend == "virtualbox":
        if ssh_port is None or str(ssh_port).strip() == "":
            raise RuntimeError("VirtualBox 需要 SSH_PORT")
        try:
            port = int(str(ssh_port).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("VirtualBox 需要 SSH_PORT") from exc
        if not _vbox_port_in_span(port):
            raise RuntimeError(
                f"VirtualBox SSH 端口 {port} 不在 {VBOX_SSH_PORT_BASE}–{VBOX_SSH_PORT_BASE + VBOX_SSH_PORT_SPAN - 1}"
            )
        net_lines.append(f"SSH_PORT={port}")
    net_lines += [
        f"ROUTER_IP={net_cfg['router_ip']}",
        f"HOME_LAN_CIDR={net_cfg['home_lan_cidr']}",
        f"TIMEZONE={egress_cfg['timezone']}",
        "GUEST_LOCALE=en_US.UTF-8",
        "GUEST_KEYBOARD=us",
        "GUEST_HOSTNAME=''",
        f"EXPECTED_EGRESS_IP={egress_cfg['ip']}",
        f"EXPECTED_EGRESS_LOC={egress_cfg['loc']}",
        f"EXPECTED_EGRESS_ASN={egress_cfg['asn']}",
    ]
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
        *net_lines,
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


def run_preflight_check(env_path, route, check_stripe=True, vm_backend=None):
    cmd = [sys.executable, "-I", str(ROOT / "scripts" / "preflight.py"), "--route", route, "--env", str(env_path)]
    if vm_backend == "virtualbox":
        cmd += ["--vm-backend", "virtualbox"]
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


def _print_next_steps(env_path, private_dir, route, backend=None):
    if route == "tart-linux":
        doc = "docs/install-tart-linux.md"
    elif backend == "virtualbox":
        doc = "docs/install-virtualbox-linux.md"
    else:
        doc = "docs/install-vmware-linux.md"
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


VBOX_NAT_CIDR = "10.0.2.0/24"
VBOX_GUEST_IP = "127.0.0.1"
VBOX_GUEST_IF = "enp0s3"
VBOX_SSH_PORT_BASE = 2222
VBOX_SSH_PORT_SPAN = 200  # 2222..2421 inclusive
_VMRUN_CANDIDATES = (
    r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
    r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
)


def vmware_present():
    """Windows 上是否已有 VMware Workstation 的 vmrun。"""
    override = os.environ.get("CCHOUSE_VMRUN")
    if override and os.path.isfile(override):
        return True
    for path in _VMRUN_CANDIDATES:
        if os.path.isfile(path):
            return True
    return bool(shutil.which("vmrun") or shutil.which("vmrun.exe"))


def vboxmanage_present():
    """已安装的 VirtualBox。只认 CCHOUSE_VBOXMANAGE 或 Oracle 默认目录。"""
    override = os.environ.get("CCHOUSE_VBOXMANAGE")
    if override and os.path.isfile(override):
        return True
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(key)
        if base and os.path.isfile(os.path.join(base, "Oracle", "VirtualBox", "VBoxManage.exe")):
            return True
    return False


def detect_vm_backend(route, system=None):
    """Apple Silicon 用 Tart，Windows 的 amd64 OVA 用 VirtualBox。

    共享的 amd64 OVA 在镜像清单中的路线名仍为 vmware-linux。
    """
    sys_name = platform.system() if system is None else system
    if route == "tart-linux":
        return "tart" if sys_name == "Darwin" else "manual"
    if route != "vmware-linux" or sys_name != "Windows":
        return "manual"
    return "virtualbox"


def _vbox_port_in_span(port, base=None, span=None):
    base = VBOX_SSH_PORT_BASE if base is None else base
    span = VBOX_SSH_PORT_SPAN if span is None else span
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False
    return base <= value < base + span


def vbox_preferred_ssh_port(tag, base=VBOX_SSH_PORT_BASE, span=VBOX_SSH_PORT_SPAN):
    """同一 tag 稳定落在 2222–2421。空闲检查从这里开始，被占用再往后找。"""
    digest = hashlib.sha256(str(tag).encode("utf-8")).digest()
    offset = int.from_bytes(digest[:2], "big") % span
    return base + offset


def loopback_port_free(port):
    """127.0.0.1 上能否绑定。不设 SO_REUSEADDR，否则 Windows 上端口已被占用时仍可能绑定成功。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _vbox_taken_ports(occupied):
    taken = set()
    for item in occupied or ():
        try:
            taken.add(int(item))
        except (TypeError, ValueError):
            continue
    return taken


def choose_vbox_ssh_port(tag, occupied=(), is_free=None, preferred=None):
    """从 preferred（缺省为 tag 哈希）起在 2222–2421 里找空闲端口，到尾后绕回。"""
    base = VBOX_SSH_PORT_BASE
    span = VBOX_SSH_PORT_SPAN
    last = base + span - 1
    if preferred is None:
        start = vbox_preferred_ssh_port(tag, base=base, span=span)
    else:
        try:
            start = int(preferred)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"VirtualBox SSH 端口 {preferred} 不在 {base}–{last}") from exc
        if not _vbox_port_in_span(start, base, span):
            raise RuntimeError(f"VirtualBox SSH 端口 {start} 不在 {base}–{last}")
    taken = _vbox_taken_ports(occupied)
    probe = loopback_port_free if is_free is None else is_free
    for step in range(span):
        port = base + ((start - base + step) % span)
        if port in taken or not probe(port):
            continue
        return port
    raise RuntimeError(f"VirtualBox 在 {base}–{last} 内没有空闲的 SSH 转发端口")


def _env_raw_value(env_path, key):
    """读取 instance.env 里一个非敏感键。不打印文件内容。"""
    text = Path(env_path).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    if not match:
        return None
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw


def resolve_vbox_ssh_port(tag, current=None, occupied=(), is_free=None):
    """已写入且仍空闲的端口保持不变，避免续跑改口后和虚拟机里的转发规则对不上。"""
    probe = loopback_port_free if is_free is None else is_free
    taken = _vbox_taken_ports(occupied)
    if current is not None and str(current).strip() != "":
        try:
            value = int(str(current).strip())
        except (TypeError, ValueError):
            value = None
        if value is not None and _vbox_port_in_span(value) and value not in taken and probe(value):
            return value
    return choose_vbox_ssh_port(tag, occupied=occupied, is_free=probe)


def apply_virtualbox_guest_net(ctx):
    """把 VirtualBox NAT 网段和本 tag 的 SSH_PORT 写进 instance.env。不改 ROUTER_IP。"""
    if getattr(ctx, "backend", None) != "virtualbox":
        return
    tag = getattr(ctx, "tag", None) or "cc01"
    current = None
    env_path = getattr(ctx, "env_path", None)
    if env_path and Path(env_path).is_file():
        current = _env_raw_value(env_path, "SSH_PORT")
    occupied = getattr(ctx, "vbox_occupied_ports", ()) or ()
    is_free = getattr(ctx, "vbox_port_is_free", None)
    port = resolve_vbox_ssh_port(tag, current=current, occupied=occupied, is_free=is_free)
    ctx.vbox_ssh_port = port
    if not env_path or not Path(env_path).is_file():
        return
    update_env_value(env_path, "VMNET_CIDR", VBOX_NAT_CIDR)
    update_env_value(env_path, "GUEST_IP", VBOX_GUEST_IP)
    update_env_value(env_path, "GUEST_IF", VBOX_GUEST_IF)
    update_env_value(env_path, "SSH_PORT", str(port))


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
        self.hostkey_log = self.logs / "hostkey.serial"
        self.ip_file = private_dir / "guest_ip.txt"
        self.ssh_key = private_dir / "id_ed25519"
        self.known_hosts = private_dir / "known_hosts"
        self.cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else default_private_root() / "cache"
        self.dry_run = args.dry_run
        self.backend = detect_vm_backend(route) if getattr(args, "vm_backend", "auto") == "auto" else args.vm_backend
        self.vbox_manifest = None
        self.vbox_installer = None
        if self.backend == "virtualbox":
            self.vbox_manifest = downloads.load_virtualbox_installer(ROOT / "guest-linux" / "virtualbox-manifest.json")
            self.vbox_installer = self.cache_dir / "virtualbox" / self.vbox_manifest["filename"]
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
        self.state.setdefault("tag", self.tag)
        self.state.setdefault("private_dir", str(self.private_dir.resolve()))
        self.state["cache_dir"] = str(self.cache_dir.resolve())
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

    def virtualbox(self, op, **kw):
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ROOT / "scripts" / "vm-virtualbox.ps1", "-CcOp", op]
        for k, v in kw.items():
            cmd += [f"-Cc{k}", v]
        return self.run(cmd)

    def start_downloads(self):
        need_claude = not self.done("firstboot")
        need_image = not self.done("import")
        need_vbox = self.backend == "virtualbox" and need_image and not vboxmanage_present()
        if self.dry_run:
            if need_image:
                print(f"  [dry-run] 后台下载镜像 {downloads.image_urls(self.artifact, self.args.image_mirror)[0]} -> {self.image_path}（sha256 校验，断点续传）")
            if need_claude:
                print(f"  [dry-run] 后台下载 Claude Code {self.claude_platform} -> {self.claude_path}（npm SRI 校验）")
            if self.backend == "virtualbox" and need_image:
                man = self.vbox_manifest
                if vboxmanage_present():
                    print(f"  [dry-run] 已有 VBoxManage，跳过 VirtualBox 基础包 {man['url']}")
                else:
                    print(f"  [dry-run] 后台下载 VirtualBox 基础包 {man['url']} -> {self.vbox_installer}（sha256 校验，仓库不分发）")
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
        if need_vbox:
            man = self.vbox_manifest
            self.downloader.start(
                "virtualbox",
                f"VirtualBox {man['version']}",
                lambda prog, manifest=man, dest=self.vbox_installer: downloads.fetch_verified(
                    [manifest["url"]], dest, sha256=manifest["sha256"], size=manifest.get("size_bytes"),
                    progress=prog, log=self.download_log,
                ),
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
        chosen = getattr(self.args, "vm_backend", "auto")
        if chosen and chosen != "auto":
            parts += ["--vm-backend", chosen]
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

    print(f"[cchouse up] 虚拟机后端：{ctx.backend} | 协议：{single_fields['SINGLE_TYPE']} | 端口：{single_fields['SINGLE_PORT']}")

    probed = None
    if not args.no_probe and single_fields["SINGLE_TYPE"] in ("socks5", "http"):
        print("[cchouse up] 正在通过代理探测出口 IP 与时区（凭据不落盘不回显）...")
        probed = probe_egress_via_proxy(single_fields)
    probed = probed or {}
    egress_cfg = {
        "timezone": args.timezone or probed.get("timezone") or "America/Los_Angeles",
        "ip": args.expected_ip or probed.get("ip") or "",
        "loc": probed.get("country") or "US",
        "asn": probed.get("asn") or "",
    }
    print(
        f"[cchouse up] 采用出口配置：时区={egress_cfg['timezone']}，"
        f"预期出口 IP={egress_cfg['ip'] or '未设置'}，"
        f"国家={egress_cfg['loc']}，ASN={egress_cfg['asn'] or '未知'}"
    )

    try:
        ssh_key, ssh_pubkey = generate_ssh_keypair(private_dir, tag)
    except RuntimeError as exc:
        raise StepError(str(exc)) from None

    if ctx.backend == "virtualbox":
        vmnet_cidr = VBOX_NAT_CIDR
        guest_ip = VBOX_GUEST_IP
    elif route == "tart-linux":
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
    ssh_port = None
    if ctx.backend == "virtualbox":
        apply_virtualbox_guest_net(ctx)
        ssh_port = ctx.vbox_ssh_port
    env_text = format_instance_env(
        tag=tag,
        route=route,
        private_dir=private_dir,
        ssh_key=ssh_key,
        ssh_pubkey=ssh_pubkey,
        single_fields=single_fields,
        net_cfg=net_cfg,
        egress_cfg=egress_cfg,
        backend=ctx.backend,
        ssh_port=ssh_port,
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
    apply_virtualbox_guest_net(ctx)
    rc, pf_json, pf_stderr = run_preflight_check(
        ctx.env_path, ctx.route, check_stripe=(not ctx.args.no_probe), vm_backend=ctx.backend,
    )
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
    apply_virtualbox_guest_net(ctx)
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
    if ctx.backend == "virtualbox":
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "scripts" / "vm-virtualbox.ps1"), "-CcOp", "exists", "-CcVmName", ctx.vm_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        return proc.returncode == 0
    return ctx.vmx.is_file()


def _ensure_virtualbox_installed(ctx):
    man = ctx.vbox_manifest
    if not man:
        raise StepError("缺少 VirtualBox 安装清单")
    if ctx.dry_run:
        ctx.virtualbox("install", Installer=ctx.vbox_installer, Sha256=man["sha256"])
        return
    if not vboxmanage_present():
        ctx.wait_download("virtualbox")
        try:
            downloads.require_sha256(ctx.vbox_installer, man["sha256"])
        except downloads.DownloadError as exc:
            raise StepError(str(exc)) from None
    ctx.virtualbox("install", Installer=ctx.vbox_installer, Sha256=man["sha256"])


def step_import(ctx):
    # 只重建 up 自己建过的虚拟机；同名的既有虚拟机（可能已登录）绝不覆盖
    owned = ctx.state.get("vm_created_by_up") is True
    if not owned and vm_exists(ctx):
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
        ctx.tart("clone" if owned else "clone-new", ctx.golden_name, ctx.vm_name)
        ctx.state.update(vm_created_by_up=True, vm_name=ctx.vm_name, vm_backend=ctx.backend, golden_name=ctx.golden_name)
        ctx.save_state()
        return existed
    if ctx.backend == "virtualbox":
        apply_virtualbox_guest_net(ctx)
        ctx.state.update(vm_created_by_up=True, vm_name=ctx.vm_name, vm_backend=ctx.backend)
        ctx.save_state()
        _ensure_virtualbox_installed(ctx)
        ctx.virtualbox(
            "import",
            Ova=ctx.image_path,
            VmName=ctx.vm_name,
            VmDir=ctx.private_dir / "vm",
            SshPort=str(ctx.vbox_ssh_port),
            HostKeyLog=str(ctx.hostkey_log),
        )
        return False
    ctx.vmware("import", Vmx=ctx.vmx, Ova=ctx.image_path, VmName=ctx.vm_name, VmDir=ctx.private_dir / "vm",
               ReplaceExisting="yes" if owned else "no")
    ctx.state.update(vm_created_by_up=True, vm_name=ctx.vm_name, vm_backend=ctx.backend)
    ctx.save_state()
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
    elif ctx.backend == "virtualbox":
        ctx.virtualbox(
            "firstboot",
            VmName=ctx.vm_name,
            SeedIso=ctx.seed_iso,
            FirstbootSeconds="56",
            HostKeyLog=str(ctx.hostkey_log),
        )
    else:
        ctx.vmware("firstboot", Vmx=ctx.vmx, SeedIso=ctx.seed_iso, HostKeyLog=str(ctx.hostkey_log))
    return False


def step_boot(ctx):
    if not ctx.dry_run:
        ctx.logs.mkdir(parents=True, exist_ok=True)
    if ctx.backend == "tart":
        ctx.delete_plaintext()
        ctx.tart("boot", ctx.vm_name, ctx.ssh_key, ctx.known_hosts, ctx.ip_file, ctx.logs / "tart-run.log")
    elif ctx.backend == "virtualbox":
        apply_virtualbox_guest_net(ctx)
        ctx.virtualbox(
            "boot",
            VmName=ctx.vm_name,
            SeedIso=ctx.seed_iso,
            Key=ctx.ssh_key,
            KnownHosts=ctx.known_hosts,
            IpFile=ctx.ip_file,
            SshPort=str(ctx.vbox_ssh_port),
            HostKeyLog=str(ctx.hostkey_log),
        )
        ctx.delete_plaintext()
    else:
        ctx.vmware(
            "boot",
            Vmx=ctx.vmx,
            SeedIso=ctx.seed_iso,
            Key=ctx.ssh_key,
            KnownHosts=ctx.known_hosts,
            IpFile=ctx.ip_file,
            HostKeyLog=str(ctx.hostkey_log),
        )
        ctx.delete_plaintext()
    if ctx.dry_run:
        return False
    ip = ctx.ip_file.read_text(encoding="utf-8").strip()
    if not render_mod.is_ipv4(ip):
        raise StepError("未拿到客机 IPv4 地址")
    update_env_value(ctx.env_path, "GUEST_IP", ip)
    if ctx.backend == "virtualbox":
        update_env_value(ctx.env_path, "SSH_PORT", str(ctx.vbox_ssh_port))
    print(f"  客机已联网：claude@{ip}（明文 seed.iso / rendered 已删除）")
    return False


def step_verify(ctx):
    if platform.system() == "Windows":
        verifier = [sys.executable, "-I", ROOT / "scripts" / "verify-linux-native.py"]
    else:
        verifier = [find_bash(), ROOT / "scripts" / "verify-linux.sh"]
    try:
        ctx.run([*verifier, ctx.env_path, "--fail-closed"])
    except StepError as exc:
        raise StepError(f"自动验收未通过（{exc}）；断网保护不会放宽，请按上方 FAIL 项排查代理") from None
    return False


def step_snapshot(ctx):
    if ctx.backend == "tart":
        snap = f"{ctx.vm_name}-clean"
        ctx.tart("snapshot", ctx.vm_name, snap)
        ctx.state["clean_snapshot"] = snap
        ctx.save_state()
        ctx.tart("gui", ctx.vm_name, ctx.logs / "tart-gui.log")
    elif ctx.backend == "virtualbox":
        snap = CLEAN_SNAPSHOT
        ctx.virtualbox("snapshot", VmName=ctx.vm_name, Snapshot=snap)
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
        _print_next_steps(ctx.env_path, ctx.private_dir, ctx.route, ctx.backend)
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
    if ctx.route == "tart-linux":
        doc = "docs/install-tart-linux.md"
    elif ctx.backend == "virtualbox":
        doc = "docs/install-virtualbox-linux.md"
    else:
        doc = "docs/install-vmware-linux.md"
    print("\n== 当前宿主暂不支持自动导入（默认自动化：Apple Silicon + Tart、Windows + VirtualBox） ==")
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
    if ctx.backend == "virtualbox" and ctx.state.get("vm_backend") == "vmware":
        sys.exit("错误：该实例的安装记录属于 VMware，请使用新的 --tag 创建 VirtualBox 实例")
    return run_pipeline(ctx)


def _tart_inventory():
    proc = subprocess.run(
        ["tart", "list", "--source", "local", "--format", "json"],
        check=True, capture_output=True, encoding="utf-8",
    )
    try:
        return {vm["Name"]: vm for vm in json.loads(proc.stdout)}
    except (ValueError, TypeError, KeyError) as exc:
        raise StepError("无法读取 Tart 的本地虚拟机列表") from exc


def _tart_remove_plan(vm_name, snapshot, private_dir, inventory):
    return [
        (f"{label} {name}", ["bash", str(ROOT / "scripts" / "vm-tart.sh"), "delete", name])
        for label, name in (("干净快照", snapshot), ("虚拟机", vm_name))
        if name in inventory
    ]


def _virtualbox_remove_plan(vm_name, snapshot, private_dir, inventory):
    if not isinstance(vm_name, str) or not re.fullmatch(r"claude-[a-z][a-z0-9_-]{1,15}", vm_name):
        raise StepError("state.json 的 VirtualBox 虚拟机名无法精确匹配")
    label = f"虚拟机 {vm_name}（先关机，再 VBoxManage unregistervm --delete"
    if snapshot:
        label += f"，含快照 {snapshot}"
    return [(f"{label}）", [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "scripts" / "vm-virtualbox.ps1"),
        "-CcOp", "delete", "-CcVmName", vm_name,
    ])]


def _vmware_remove_plan(vm_name, snapshot, private_dir, inventory):
    vmx = private_dir / "vm" / vm_name / f"{vm_name}.vmx"
    if vmx.resolve() != vmx:
        raise StepError("VMware VMX 或其上级目录为链接，需手动核对")
    if not vmx.is_file():
        return []
    label = f"虚拟机 {vm_name}（含所有 VMware 内置快照"
    if snapshot:
        label += f"：干净快照 {snapshot}"
    return [(f"{label}） {vmx}", [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "scripts" / "vm-vmware.ps1"), "-CcOp", "delete", "-CcVmx", str(vmx),
    ])]


# 新后端在这里注册。未注册的后端保留原实例，以便在对应软件中手动处理。
REMOVE_BACKENDS = {
    "tart": _tart_remove_plan,
    "vmware": _vmware_remove_plan,
    "virtualbox": _virtualbox_remove_plan,
}


def _remove_state(private_dir, root, tag):
    if private_dir.resolve() != private_dir:
        raise StepError("私有目录为链接")
    if not private_dir.exists():
        return {}
    state_path = private_dir / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise StepError("缺少可验证的 state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except ValueError:
        raise StepError("state.json 无法解析") from None
    if not isinstance(state, dict):
        raise StepError("state.json 格式不正确")
    if state.get("tag", tag) != tag or state.get("private_dir", str(private_dir)) != str(private_dir):
        raise StepError("state.json 记录的 tag 或私有目录不匹配")
    if not {"tag", "private_dir"} <= state.keys():
        if private_dir != root / tag:
            raise StepError("旧版 state.json 缺少自定义私有目录归属")
        if not isinstance(state.get("step"), str) or not state["step"].startswith("up:"):
            raise StepError("state.json 缺少安装记录")
    return state


def _download_cache_items(cache_dir, manifest):
    files = [cache_dir / "images" / Path(item["filename"]).name for item in manifest["artifacts"]]
    files += [cache_dir / f"claude-code-{arch}.tgz" for arch in ("linux-arm64", "linux-x64")]
    items = []
    for dest in files:
        if dest.parent.resolve() != dest.parent:
            continue
        for suffix in ("", ".part", ".verified", ".json"):
            path = dest.with_name(dest.name + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                items.append((path, info.st_size))
    return items


def cmd_remove(args):
    tag = args.tag.strip().lower()
    if not SAFE_TAG_RE.fullmatch(tag):
        print("错误：--tag 仅支持小写字母开头的 2-16 位字母/数字/下划线/短横线（如 cc01）", file=sys.stderr)
        return 2

    root = default_private_root().resolve()
    requested = Path(args.private_dir).expanduser() if args.private_dir else root / tag
    private_dir = requested.parent.resolve() / requested.name
    vm_name = f"claude-{tag}"
    cache_dir = root / "cache"
    golden_name = None
    try:
        if private_dir in (root, cache_dir) or _is_inside_repo(private_dir) or private_dir in ROOT.parents:
            raise StepError("私有目录是共享目录或仓库路径")
        state = _remove_state(private_dir, root, tag)
        if not state:
            print(f"没有找到 {tag} 的安装记录。")
            _print_remove_remainder(cache_dir, golden_name, False)
            return 0

        backend = state.get("vm_backend")
        snapshot = state.get("clean_snapshot")
        owned = state.get("vm_created_by_up") is True
        recorded_vm = state.get("vm_name")
        if owned and recorded_vm is None and backend is None:
            # 旧版 Tart 的 clean 克隆名含完整 VM 名，配合 up 的归属标记恢复身份。
            if state.get("route") == "tart-linux" and snapshot == f"{vm_name}-clean":
                backend, recorded_vm = "tart", vm_name
        if owned and (recorded_vm != vm_name or backend not in REMOVE_BACKENDS):
            raise StepError("state.json 缺少可验证的虚拟机名称或受支持的后端")
        if owned and snapshot is not None and snapshot != (f"{vm_name}-clean" if backend == "tart" else CLEAN_SNAPSHOT):
            raise StepError("state.json 的干净快照名不匹配")

        vm_dir = private_dir / "vm"
        allowed_vm_dirs = {vm_name} if owned and backend in ("vmware", "virtualbox") else set()
        if vm_dir.resolve() != vm_dir or (vm_dir.exists() and {p.name for p in vm_dir.iterdir()} - allowed_vm_dirs):
            raise StepError("私有目录的 vm/ 下有无法确认归属的项目")

        if state.get("cache_dir"):
            cache_dir = Path(state["cache_dir"])
        if not cache_dir.is_absolute() or cache_dir.resolve() != cache_dir:
            raise StepError("记录的缓存目录不是可验证的绝对路径")
        if cache_dir == private_dir or private_dir in cache_dir.parents:
            raise StepError("缓存位于实例私有目录中，请先手动移出缓存")

        manifest = json.loads((ROOT / "guest-linux" / "image-manifest.json").read_text(encoding="utf-8"))
        if backend == "tart":
            golden_name = state.get("golden_name", f"cchouse-debian13-arm64-{manifest['image_version']}")
            if not isinstance(golden_name, str) or not re.fullmatch(r"cchouse-debian13-arm64-[a-zA-Z0-9._-]+", golden_name):
                raise StepError("state.json 的 Tart 黄金镜像名无效")
            inventory = _tart_inventory()
        else:
            inventory = {}

        vm_actions = REMOVE_BACKENDS[backend](vm_name, snapshot, private_dir, inventory) if owned else []
        golden = inventory.get(golden_name)
        if args.purge_cache and golden:
            if golden.get("Running"):
                raise StepError("Tart 黄金镜像正在运行，请先在 Tart 中关闭它")
        cache_items = _download_cache_items(cache_dir, manifest) if args.purge_cache else []

        print("将删除：")
        for label, _cmd in vm_actions:
            print(f"  - {label}")
        if private_dir.exists():
            for name in ("seed.iso", "rendered"):
                residual = private_dir / name
                if residual.exists() or residual.is_symlink():
                    print(f"  - 临时明文 {residual}")
            print(f"  - 私有目录 {private_dir}（含配置、密钥和日志）")
        if args.purge_cache:
            if golden:
                print(f"  - Tart 黄金镜像 {golden_name}（Tart 报告 {golden.get('Size', '?')} GB）")
            for path, size in cache_items:
                print(f"  - 下载缓存 {path}（{size:,} 字节）")
        if not owned:
            print("该记录没有安装器创建虚拟机的标记。手动创建的虚拟机需在虚拟机软件里核对并删除。")
        if not args.purge_cache:
            print("Tart 黄金镜像和共享下载缓存默认保留。")
        else:
            print("共享缓存可能供其他实例使用。")
        if not args.yes:
            try:
                confirmed = input("确认卸载？ [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                confirmed = False
            if not confirmed:
                print("已取消卸载。")
                return 0
        if _remove_state(private_dir, root, tag) != state:
            raise StepError("确认期间实例状态改变，请重新预览")
        if owned and REMOVE_BACKENDS[backend](vm_name, snapshot, private_dir, inventory) != vm_actions:
            raise StepError("确认期间虚拟机路径改变，请重新预览")

        for _label, cmd in vm_actions:
            subprocess.run(cmd, check=True)
        if args.purge_cache:
            if golden:
                subprocess.run(["tart", "delete", golden_name], check=True)
            for path, _size in cache_items:
                if path.parent.resolve() != path.parent:
                    raise StepError("缓存目录在卸载期间改变")
                path.unlink(missing_ok=True)
        if private_dir.exists():
            shutil.rmtree(private_dir)
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError, TypeError, KeyboardInterrupt) as exc:
        why = "用户中断" if isinstance(exc, KeyboardInterrupt) else str(exc)
        print(f"卸载未完成：{why}", file=sys.stderr)
        print(f"请在虚拟机软件中核对客机 {vm_name} 和它的快照，再手动清理确认归属的私有目录 {private_dir}。")
        print(f"可在文件管理器核对共享下载缓存 {cache_dir}，仅删除不再需要的安装包。")
        return 1
    print(f"已完成 {tag} 的卸载。")
    _print_remove_remainder(cache_dir, golden_name, args.purge_cache)
    return 0


def _print_remove_remainder(cache_dir, golden_name, purged):
    if purged:
        print("已处理清单中的共享缓存项，未识别的缓存文件可在文件管理器中核对。")
    else:
        print("保留共享黄金镜像和下载缓存。确认其他实例无需使用后，可按下面的路径手动清理。")
    print(f"手动清理下载缓存：在文件管理器打开 {cache_dir}，核对后删除不需要的安装包。")
    if golden_name and not purged:
        print(f'手动清理 Tart 黄金镜像：确认无需使用后运行 tart delete "{golden_name}"。')
    print("安装器仓库和宿主虚拟机软件仍保留，可分别在文件管理器和系统软件管理界面删除。")


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
    p_up.add_argument("--timezone", default=None, help="覆盖自动采用的出口时区")
    p_up.add_argument("--expected-ip", default=None, help="覆盖自动采用的预期出口 IP")
    p_up.add_argument("--link-stdin", action="store_true", help="从标准输入读取一行节点链接（不回显，适合脚本/测试）")
    p_up.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    p_up.add_argument("--no-probe", action="store_true", help="跳过宿主机代理出口与 Stripe 探测")
    p_up.add_argument("--vm-backend", choices=("auto", "tart", "vmware", "virtualbox", "manual"), default="auto",
                      help="虚拟机自动化后端（默认：Apple Silicon→tart；Windows→virtualbox；其余→manual）")
    p_up.add_argument("--reconfigure", action="store_true", help="忽略已有 instance.env，重新输入节点链接")
    p_up.add_argument("--config-only", action="store_true", help="只生成 instance.env 并跑 preflight（第一轮行为），不下载不建虚拟机")
    p_up.add_argument("--dry-run", action="store_true", help="只演练：生成配置并跑 preflight，其余步骤只打印命令，不下载、不动虚拟机、不写 state.json")

    p_remove = sub.add_parser("remove", help="预览并卸载指定实例")
    p_remove.add_argument("--tag", required=True, help="安装时的实例短名")
    p_remove.add_argument("--private-dir", help="安装时使用的自定义私有目录")
    p_remove.add_argument("--yes", action="store_true", help="跳过确认提示")
    p_remove.add_argument("--purge-cache", action="store_true", help="同时清理共享下载缓存和对应的 Tart 黄金镜像")

    args = parser.parse_args()
    if args.command == "up":
        sys.exit(cmd_up(args))
    if args.command == "remove":
        sys.exit(cmd_remove(args))


if __name__ == "__main__":
    main()
