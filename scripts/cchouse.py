#!/usr/bin/env python3
"""cchouse 安装与卸载向导。

用法：
  python3 -I scripts/cchouse.py up [--tag cc01] [--route auto|tart-linux|virtualbox-linux]
      [--vm-backend auto|tart|virtualbox|manual] [--dry-run] [--config-only]
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
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import downloads  # noqa: E402
import launcher as launcher_mod  # noqa: E402
import preflight as preflight_mod  # noqa: E402
import render as render_mod  # noqa: E402
import share as share_mod  # noqa: E402

REJECTED_SCHEMES = frozenset(set(render_mod.REJECTED_UDP_TYPES) | {"wg", "quic"})
SUPPORTED_SCHEMES = frozenset({"socks5", "socks5h", "socks", "http", "https", "vless", "trojan", "ss"})
ENCRYPTED_NODE_TYPES = frozenset({"vless", "trojan", "ss"})
EXIT_PROXY_TYPES = frozenset({"socks5", "http"})
QUICK_NODE_TYPES = frozenset({"socks5", "http", "vless", "trojan", "ss"})
ADVANCED_NODE_GUIDE = "https://github.com/nana-fox/cchouse/blob/main/docs/proxy-modes.md#单节点支持的协议single_type"
ADVANCED_NODE_MESSAGE = (
    "一键安装支持单个 SOCKS5 / HTTP(S) / VLESS / Trojan / SS 节点，或两跳「前置 + SOCKS5/HTTP 固定出口」。"
    "其他协议或更复杂拓扑请按进阶文档手动配置。"
    f"\n  {ADVANCED_NODE_GUIDE}"
)
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_CIPHER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_TAG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,15}$")
FORBIDDEN_VALUE_CHARS = ("'", "\r", "\n", "\x00")
EXIT_NEED_TTY = 20
LINK_FILE_MAX_BYTES = 65536


class NodeLinkError(ValueError):
    """节点链接解析错误（错误文案绝不回显原始链接或凭据）。"""


def _is_owned_by_current_user(path, st):
    """确认链接文件归当前用户所有（POSIX 核对 uid，Windows 核对文件所有者 SID）。"""
    if os.name != "nt":
        return st.st_uid == os.getuid()
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return False
    sd = ctypes.c_void_p()
    try:
        owner_sid = ctypes.c_void_p()
        rc = advapi32.GetNamedSecurityInfoW(
            str(path), 1, 1, ctypes.byref(owner_sid), None, None, None, ctypes.byref(sd),
        )
        if rc != 0 or not owner_sid.value:
            return False
        # Windows 管理员会话新建文件的 NTFS 所有者可能为 TokenOwner（如 BUILTIN\Administrators），
        # 普通会话为 TokenUser；核对两者任一匹配即可，仍拒绝其他用户拥有的文件。
        for info_class in (1, 4):  # TokenUser=1, TokenOwner=4
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(needed))
            if needed.value == 0:
                continue
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, info_class, buf, needed.value, ctypes.byref(needed)):
                continue
            token_sid = ctypes.c_void_p.from_buffer(buf).value
            if token_sid and advapi32.EqualSid(owner_sid, token_sid):
                return True
        return False
    finally:
        if sd.value:
            kernel32.LocalFree(sd)
        kernel32.CloseHandle(token)


def read_link_file_lines(path):
    """安全读取节点链接文件中所有非空行；绝不在异常或输出中回显文件内容。"""
    p = Path(path).expanduser()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        raise NodeLinkError(f"找不到链接文件：{p}") from None
    except OSError:
        raise NodeLinkError(f"无法访问链接文件：{p}") from None
    if stat.S_ISDIR(st.st_mode):
        raise NodeLinkError(f"链接路径是目录，不是普通文件：{p}")
    if not stat.S_ISREG(st.st_mode):
        raise NodeLinkError(f"链接文件必须是普通文件（不支持符号链接或特殊文件）：{p}")
    if not _is_owned_by_current_user(p, st):
        raise NodeLinkError(f"链接文件不属于当前用户，已拒绝读取：{p}")
    if os.name != "nt" and (st.st_mode & 0o044):
        print(
            f"警告：链接文件 {p} 对同组或其他用户可读，建议执行 chmod 600，用完后删除。",
            file=sys.stderr,
        )
    if st.st_size > LINK_FILE_MAX_BYTES:
        raise NodeLinkError(f"链接文件过大（超过 {LINK_FILE_MAX_BYTES} 字节），请确认路径正确：{p}")
    try:
        raw_bytes = p.read_bytes()
    except OSError:
        raise NodeLinkError(f"无法读取链接文件：{p}") from None
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise NodeLinkError(f"链接文件不是有效的 UTF-8 文本：{p}") from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise NodeLinkError(f"链接文件为空，未找到节点链接：{p}")
    return lines


def read_link_file(path):
    """安全读取节点链接文件第一条非空行；绝不在异常或输出中回显文件内容。"""
    return read_link_file_lines(path)[0]


CLIPBOARD_NOT_LINK_MSG = "剪贴板里不是节点链接，请复制后重试"
CLIPBOARD_UNAVAILABLE_MSG = (
    "无法读取剪贴板（可能当前没有桌面会话或剪贴板工具不可用）。"
    "请改用 --link-file <路径>，或在自己的终端窗口直接运行首页命令并粘贴。"
)


def _read_clipboard_raw():
    """读取系统剪贴板文本；失败抛出 RuntimeError（不回显任何内容）。"""
    sys_name = platform.system()
    if sys_name == "Darwin":
        cmd = ["pbpaste"]
    elif sys_name == "Windows":
        cmd = [
            "powershell", "-NoProfile", "-Command",
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); Get-Clipboard -Raw",
        ]
    else:
        for cand in (["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"]):
            if shutil.which(cand[0]):
                cmd = cand
                break
        else:
            raise RuntimeError("clipboard_tool_missing")
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("clipboard_read_failed") from None
    if proc.returncode != 0:
        raise RuntimeError(f"clipboard_exit_{proc.returncode}")
    return proc.stdout or ""


def read_clipboard_links():
    """从剪贴板读取所有非空行作为节点链接列表；绝不回显剪贴板内容。"""
    try:
        text = _read_clipboard_raw()
    except RuntimeError:
        raise NodeLinkError(CLIPBOARD_UNAVAILABLE_MSG) from None
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise NodeLinkError(CLIPBOARD_NOT_LINK_MSG)
    return lines


def read_clipboard_link():
    """从剪贴板读取第一条非空行作为节点链接；绝不回显剪贴板内容。"""
    return read_clipboard_links()[0]


def clear_clipboard():
    """清空系统剪贴板；返回是否成功。"""
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            proc = subprocess.run(
                ["pbcopy"],
                input=b"",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return proc.returncode == 0
        if sys_name == "Windows":
            proc = subprocess.run(
                ["clip"],
                input=b"",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return proc.returncode == 0
        for cand in (["wl-copy", "--clear"], ["xclip", "-i", "-selection", "clipboard"], ["xsel", "-cb"]):
            if shutil.which(cand[0]):
                proc = subprocess.run(
                    cand,
                    input=b"",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return proc.returncode == 0
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


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


def _to_via_fields(single_fields):
    return {f"SINGLE_VIA_{k[len('SINGLE_'):]}": v for k, v in single_fields.items() if k.startswith("SINGLE_")}


def parse_and_classify_links(raw_links):
    """解析 1 或 2 条节点链接并分类为 (exit_fields, via_fields)；绝不回显原始链接或凭据。

    规则：
    - 1 条：任意支持协议（socks5/http(s)/vless/trojan/ss）→ 单节点。
    - 2 条：其中 vless/trojan/ss 那条是前置，socks5/http(s) 那条是出口；
      两条都是 socks5/http(s) 时按「第一行前置、第二行出口」；
      出口必须是 socks5/http(s)；超过 2 条或两条都是加密协议 → 拒绝并说明。
    """
    links = [s.strip() for s in (raw_links or []) if isinstance(s, str) and s.strip()]
    if not links:
        raise NodeLinkError("节点链接不能为空")
    if len(links) > 2:
        raise NodeLinkError(
            "一键安装最多支持 2 条节点链接（1 条单节点，或 2 条「前置 + 固定出口」）；"
            "三跳及以上请参考 docs/manual-install.md。"
        )
    parsed = [parse_node_link(link) for link in links]
    if len(parsed) == 1:
        return parsed[0], None

    first, second = parsed
    first_enc = first["SINGLE_TYPE"] in ENCRYPTED_NODE_TYPES
    second_enc = second["SINGLE_TYPE"] in ENCRYPTED_NODE_TYPES
    if first_enc and second_enc:
        raise NodeLinkError(
            "两条链接都是加密协议（VLESS / Trojan / SS）。两跳模式要求出口必须是 SOCKS5 或 HTTP(S) 固定 IP 节点。"
        )
    if first_enc and not second_enc:
        front, exit_node = first, second
    elif not first_enc and second_enc:
        front, exit_node = second, first
    else:
        # 两条都是 socks5/http(s)：第一行前置、第二行出口
        front, exit_node = first, second

    if exit_node["SINGLE_TYPE"] not in EXIT_PROXY_TYPES:
        raise NodeLinkError("两跳模式的出口节点必须是 SOCKS5 或 HTTP(S) 协议。")
    return exit_node, _to_via_fields(front)


def detect_route():
    sys_name = platform.system()
    arch = preflight_mod.detect_arch()
    if sys_name == "Darwin" and arch == "arm64":
        return "tart-linux"
    return "virtualbox-linux"


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


def _pick_free_loopback_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _probe_orig_host(fields, prefix):
    server = (fields.get(f"{prefix}_SERVER") or "").strip()
    pinned = (fields.get(f"{prefix}_SERVER_IP") or "").strip()
    if server and not render_mod.is_ipv4(server):
        return server
    return None if pinned else None


def _macos_default_route_is_utun():
    if platform.system() != "Darwin":
        return False
    try:
        out = subprocess.check_output(
            ["/sbin/route", "-n", "get", "1.1.1.1"], text=True, stderr=subprocess.DEVNULL, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines():
        if "interface:" in line:
            return line.split(":", 1)[1].strip().startswith("utun")
    return False


def _macos_primary_physical_interfaces():
    """返回 macOS 上当前拥有有效 IPv4 且状态 active 的物理 en* 网卡列表（仅用于宿主 TUN 回环时的临时探测回退）。"""
    if platform.system() != "Darwin":
        return []
    try:
        out = subprocess.check_output(["/sbin/ifconfig"], text=True, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    ifaces = []
    cur = None
    has_ipv4 = False
    is_active = False
    for line in out.splitlines():
        if line and not line[0].isspace():
            if cur and cur.startswith("en") and has_ipv4 and is_active:
                ifaces.append(cur)
            cur = line.split(":", 1)[0].strip()
            has_ipv4 = False
            is_active = False
        elif cur and cur.startswith("en"):
            if "\tinet " in line and "127.0.0.1" not in line and "169.254." not in line:
                has_ipv4 = True
            if "status: active" in line:
                is_active = True
    if cur and cur.startswith("en") and has_ipv4 and is_active:
        ifaces.append(cur)
    return ifaces


def build_host_probe_config(single_fields, listen_port, via_fields=None, interface_name=None):
    """生成宿主临时 mihomo 最小探测配置：仅 127.0.0.1 mixed-port，无 TUN/控制器/DNS，仅 MATCH,<出口>，绝无 DIRECT。"""
    merged = dict(single_fields)
    if via_fields:
        merged.update(via_fields)
    exit_ip = (merged.get("SINGLE_SERVER_IP") or "").strip()
    if not render_mod.is_ipv4(exit_ip):
        raise StepError("缺少出口节点的 IPv4 地址，无法生成探测配置。")
    exit_orig = _probe_orig_host(merged, "SINGLE")
    if via_fields:
        via_ip = (merged.get("SINGLE_VIA_SERVER_IP") or "").strip()
        if not render_mod.is_ipv4(via_ip):
            raise StepError("缺少前置节点的 IPv4 地址，无法生成探测配置。")
        via_orig = _probe_orig_host(merged, "SINGLE_VIA")
        front_block, _ = render_mod.build_proxy_node_lines(merged, "SINGLE_VIA", "PROBE-Front", via_ip, via_orig)
        exit_block, _ = render_mod.build_proxy_node_lines(
            merged, "SINGLE", "PROBE-Exit", exit_ip, exit_orig, dialer_proxy="PROBE-Front"
        )
        proxy_block = front_block + "\n" + exit_block
    else:
        proxy_block, _ = render_mod.build_proxy_node_lines(merged, "SINGLE", "PROBE-Exit", exit_ip, exit_orig)
    iface_line = f"interface-name: {interface_name}\n" if interface_name else ""
    config_text = (
        "---\n"
        f"mixed-port: {int(listen_port)}\n"
        "bind-address: 127.0.0.1\n"
        "allow-lan: false\n"
        "mode: rule\n"
        "log-level: warning\n"
        "ipv6: false\n"
        f"{iface_line}"
        "proxies:\n"
        f"{proxy_block}\n"
        "rules:\n"
        "- MATCH,PROBE-Exit\n"
    )
    render_mod.assert_no_direct_rules(config_text, "probe-config.yaml")
    return config_text


def build_host_front_probe_config(via_fields, listen_port, interface_name=None):
    """仅用于两跳失败时诊断前置节点本身是否连通的最小 mihomo 配置（无 DIRECT、仅 127.0.0.1）。"""
    via_ip = (via_fields.get("SINGLE_VIA_SERVER_IP") or "").strip()
    if not render_mod.is_ipv4(via_ip):
        raise StepError("缺少前置节点的 IPv4 地址，无法生成探测配置。")
    via_orig = _probe_orig_host(via_fields, "SINGLE_VIA")
    front_block, _ = render_mod.build_proxy_node_lines(via_fields, "SINGLE_VIA", "PROBE-Front", via_ip, via_orig)
    iface_line = f"interface-name: {interface_name}\n" if interface_name else ""
    config_text = (
        "---\n"
        f"mixed-port: {int(listen_port)}\n"
        "bind-address: 127.0.0.1\n"
        "allow-lan: false\n"
        "mode: rule\n"
        "log-level: warning\n"
        "ipv6: false\n"
        f"{iface_line}"
        "proxies:\n"
        f"{front_block}\n"
        "rules:\n"
        "- MATCH,PROBE-Front\n"
    )
    render_mod.assert_no_direct_rules(config_text, "probe-front-config.yaml")
    return config_text


def _write_probe_config_secure(tmp_dir, config_text):
    cfg_path = Path(tmp_dir) / "config.yaml"
    data = config_text.replace("\r\n", "\n").encode("utf-8")
    if os.name != "nt":
        os.chmod(tmp_dir, 0o700)
        fd = os.open(str(cfg_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(cfg_path, 0o600)
    else:
        cfg_path.write_bytes(data)
    return cfg_path


def _wait_loopback_ready(port, proc, wait_seconds=5.0):
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(0.2)
            s.connect(("127.0.0.1", int(port)))
            return True
        except OSError:
            time.sleep(0.1)
        finally:
            s.close()
    return False


def _stop_temp_mihomo(proc):
    output = ""
    if proc is None:
        return output
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                out_bytes, _ = proc.communicate(timeout=3)
                output = (out_bytes or b"").decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                proc.kill()
                out_bytes, _ = proc.communicate(timeout=3)
                output = (out_bytes or b"").decode("utf-8", errors="replace")
        else:
            out_bytes, _ = proc.communicate(timeout=2)
            output = (out_bytes or b"").decode("utf-8", errors="replace")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return output


def _curl_via_local_mihomo(port, url, timeout=15):
    windows = platform.system() == "Windows"
    curl = shutil.which("curl.exe" if windows else "curl")
    if curl is None:
        raise StepError("系统缺少 curl，无法进行出口探测。")
    cmd = [
        curl, "-q", "--silent", "--show-error", "--noproxy", "",
        "--proxy", f"http://127.0.0.1:{int(port)}",
        "--max-time", str(timeout),
        url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 3,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 28, "", "timeout"
    except OSError as exc:
        return 1, "", type(exc).__name__


def _probe_front_reachable(mihomo_bin, via_fields, timeout=8, interface_name=None):
    """快速探测两跳中的前置节点自身是否连通（返回 True/False，绝不泄露凭据）。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="cchouse-probe-front-"))
    proc = None
    try:
        port = _pick_free_loopback_port()
        cfg_text = build_host_front_probe_config(via_fields, port, interface_name=interface_name)
        cfg_path = _write_probe_config_secure(tmp_dir, cfg_text)
        proc = subprocess.Popen(
            [str(mihomo_bin), "-d", str(tmp_dir), "-f", str(cfg_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if not _wait_loopback_ready(port, proc, wait_seconds=4.0):
            return False
        rc, out, _ = _curl_via_local_mihomo(port, "https://www.cloudflare.com/cdn-cgi/trace", timeout=timeout)
        return rc == 0 and "ip=" in out
    except Exception:
        return False
    finally:
        _stop_temp_mihomo(proc)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _classify_probe_failure(mihomo_log, curl_rc, curl_err, has_via, front_ok=None):
    """将探测失败转为大白话原因（绝不回显链接、地址或凭据）。"""
    blob = f"{mihomo_log}\n{curl_err}".lower()
    is_auth = any(k in blob for k in (
        "407", "authentication", "auth fail", "auth error", "username/password",
        "invalid credential", "proxy auth", "socks5 auth", "wrong password",
    ))
    is_tls = any(k in blob for k in (
        "reality", "x509", "certificate", "tls:", "handshake", "bad record mac", "unexpected eof",
    ))
    is_timeout = curl_rc == 28 or any(k in blob for k in ("i/o timeout", "context deadline exceeded", "timed out"))

    if has_via:
        if is_auth:
            return "出口节点认证失败（用户名或密码错误），请检查第二条 SOCKS5/HTTP 出口链接后重试。"
        if front_ok is False:
            if is_tls:
                return "前置节点连不上（TLS / REALITY 握手失败），请检查第一条加密前置链接的公钥（pbk）、SNI 或参数。"
            if is_timeout:
                return "前置节点连接超时，请检查第一条前置链接的地址、端口及当前网络是否可达。"
            return "前置节点连不上，请检查第一条前置链接的地址、端口、协议参数或服务端状态。"
        if is_timeout:
            return "前置节点正常，但出口节点超时，请检查第二条 SOCKS5/HTTP 出口链接是否可用或已过期。"
        return "前置节点正常，但出口节点连不上或认证失败，请检查第二条 SOCKS5/HTTP 出口链接的地址、端口和账号密码。"

    if is_auth:
        return "节点认证失败（用户名或密码错误），请检查节点链接后重试。"
    if is_tls:
        return "节点 TLS / REALITY 握手失败，请检查节点链接中的公钥（pbk）、SNI 或证书配置。"
    if is_timeout:
        return "连接节点超时，请检查节点地址、端口是否可达或网络是否正常。"
    return "无法通过该节点访问外网（连接被拒绝或中断），请检查节点服务、端口与协议参数是否正确。"


def _run_mihomo_egress_probe(mihomo_bin, single_fields, via_fields, timeout, probe_count, probe_interval, interface_name=None):
    tmp_dir = Path(tempfile.mkdtemp(prefix="cchouse-probe-"))
    proc = None
    mihomo_log = ""
    last_rc = 0
    last_err = ""
    try:
        port = _pick_free_loopback_port()
        cfg_text = build_host_probe_config(single_fields, port, via_fields=via_fields, interface_name=interface_name)
        cfg_path = _write_probe_config_secure(tmp_dir, cfg_text)
        try:
            proc = subprocess.Popen(
                [str(mihomo_bin), "-d", str(tmp_dir), "-f", str(cfg_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise StepError(f"启动宿主临时 mihomo 失败（{type(exc).__name__}）") from None

        if not _wait_loopback_ready(port, proc, wait_seconds=5.0):
            mihomo_log = _stop_temp_mihomo(proc)
            proc = None
            raise StepError("宿主临时 mihomo 未能启动监听本地端口，请检查节点参数格式是否正确。")

        first_result = None
        for idx in range(max(1, int(probe_count))):
            if idx > 0 and probe_interval > 0:
                time.sleep(probe_interval)
            rc, out, err = _curl_via_local_mihomo(port, "https://ipinfo.io/json", timeout=timeout)
            last_rc, last_err = rc, err
            if rc != 0 or not out.strip():
                mihomo_log = _stop_temp_mihomo(proc)
                proc = None
                front_ok = _probe_front_reachable(mihomo_bin, via_fields, interface_name=interface_name) if via_fields else None
                raise StepError(_classify_probe_failure(mihomo_log, last_rc, last_err, bool(via_fields), front_ok=front_ok))
            try:
                info = json.loads(out)
                asn = (info.get("org") or "").split(maxsplit=1)
                cur = {
                    "ip": (info.get("ip") or "").strip(),
                    "timezone": (info.get("timezone") or "").strip(),
                    "country": (info.get("country") or "").strip(),
                    "asn": asn[0] if asn else "",
                }
            except (ValueError, AttributeError):
                mihomo_log = _stop_temp_mihomo(proc)
                proc = None
                front_ok = _probe_front_reachable(mihomo_bin, via_fields, interface_name=interface_name) if via_fields else None
                raise StepError(_classify_probe_failure(mihomo_log, last_rc, last_err, bool(via_fields), front_ok=front_ok))

            if not render_mod.is_ipv4(cur["ip"]):
                raise StepError("节点返回的出口地址不是有效 IPv4，请确认节点支持 IPv4 出口。")
            if first_result is None:
                first_result = cur
            elif cur["ip"] != first_result["ip"]:
                raise StepError(
                    "连续 3 次探测得到的出口 IP 不一致，说明这不是固定 IP 节点。"
                    "cchouse 要求固定 IP 出口，请更换固定 IP 节点后重试。"
                )
        return first_result
    finally:
        _stop_temp_mihomo(proc)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def probe_egress_via_proxy(single_fields, via_fields=None, timeout=15, probe_count=3, probe_interval=2.0, mihomo_bin=None):
    """统一用宿主临时 mihomo（与客机同版本）探测出口 IP/国家/ASN/时区；间隔几秒测 3 次，IP 不一致即停。"""
    if mihomo_bin is None:
        try:
            mihomo_bin = downloads.ensure_host_mihomo(
                ROOT / "guest-linux" / "mihomo-manifest.json",
                default_private_root() / "bootstrap",
            )
        except downloads.DownloadError as exc:
            raise StepError(f"准备宿主临时探测程序 mihomo 失败：{exc}") from None

    try:
        return _run_mihomo_egress_probe(
            mihomo_bin, single_fields, via_fields, timeout, probe_count, probe_interval, interface_name=None
        )
    except StepError as first_err:
        msg = str(first_err)
        # 若 macOS 宿主开着 TUN（如 Clash TUN）导致临时 mihomo 走默认路由超时/回环，
        # 仅在临时探测配置中绑定活跃物理网卡重试一次（不修改任何宿主网络或代理设置）。
        if ("不是固定 IP" in msg or "认证失败" in msg or "未能启动监听" in msg):
            raise
        if _macos_default_route_is_utun():
            for iface in _macos_primary_physical_interfaces():
                try:
                    return _run_mihomo_egress_probe(
                        mihomo_bin, single_fields, via_fields, timeout, probe_count, probe_interval, interface_name=iface
                    )
                except StepError as retry_err:
                    if "不是固定 IP" in str(retry_err) or "认证失败" in str(retry_err):
                        raise
                    continue
        raise first_err


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


def format_instance_env(tag, route, private_dir, ssh_key, ssh_pubkey, single_fields, net_cfg, egress_cfg, backend=None, ssh_port=None, via_fields=None):
    if backend == "virtualbox":
        guest_if = VBOX_GUEST_IF
    else:
        guest_if = "enp0s1" if route == "tart-linux" else VBOX_GUEST_IF
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
    stripe_mode = "front" if via_fields else "off"
    lines = [
        "# Generated by scripts/cchouse.py up (600 permissions; keep private, never commit)",
        "GUEST_OS=linux",
        "EXIT_MODE=single",
        f"STRIPE_EXIT_MODE={stripe_mode}",
        "",
        f"TAG={tag}",
        f"VM_NAME=claude-{tag}",
        f"GUEST_IF={guest_if}",
        "MIHOMO_USER=mihomo",
        "",
        f"PRIVATE_DIR={_env_quote_double(private_dir)}",
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
    if via_fields:
        lines += [
            "",
            f"SINGLE_VIA_TYPE={via_fields['SINGLE_VIA_TYPE']}",
            f"SINGLE_VIA_SERVER={_env_quote_single(via_fields['SINGLE_VIA_SERVER'])}",
            f"SINGLE_VIA_SERVER_IP={_env_quote_single(via_fields['SINGLE_VIA_SERVER_IP'])}",
            f"SINGLE_VIA_PORT={_env_quote_single(via_fields['SINGLE_VIA_PORT'])}",
            f"SINGLE_VIA_USER={_env_quote_single(via_fields['SINGLE_VIA_USER'])}",
            f"SINGLE_VIA_PASS={_env_quote_single(via_fields['SINGLE_VIA_PASS'])}",
            f"SINGLE_VIA_UUID={_env_quote_single(via_fields['SINGLE_VIA_UUID'])}",
            f"SINGLE_VIA_SNI={_env_quote_single(via_fields['SINGLE_VIA_SNI'])}",
            f"SINGLE_VIA_FINGERPRINT={_env_quote_single(via_fields['SINGLE_VIA_FINGERPRINT'])}",
            f"SINGLE_VIA_FLOW={_env_quote_single(via_fields['SINGLE_VIA_FLOW'])}",
            f"SINGLE_VIA_NETWORK={_env_quote_single(via_fields['SINGLE_VIA_NETWORK'])}",
            f"SINGLE_VIA_WS_PATH={_env_quote_single(via_fields['SINGLE_VIA_WS_PATH'])}",
            f"SINGLE_VIA_WS_HOST={_env_quote_single(via_fields['SINGLE_VIA_WS_HOST'])}",
            f"SINGLE_VIA_PUBLIC_KEY={_env_quote_single(via_fields['SINGLE_VIA_PUBLIC_KEY'])}",
            f"SINGLE_VIA_SHORT_ID={_env_quote_single(via_fields['SINGLE_VIA_SHORT_ID'])}",
            f"SINGLE_VIA_CIPHER={_env_quote_single(via_fields['SINGLE_VIA_CIPHER'])}",
            f"SINGLE_VIA_TLS={_env_quote_single(via_fields['SINGLE_VIA_TLS'])}",
            f"SINGLE_VIA_HOST_KEY={_env_quote_single(via_fields['SINGLE_VIA_HOST_KEY'])}",
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


def run_preflight_check(env_path, route, check_stripe=True, tart_bin=None):
    cmd = [sys.executable, "-I", str(ROOT / "scripts" / "preflight.py"), "--route", route, "--env", str(env_path)]
    if check_stripe:
        cmd.append("--check-stripe")
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    if tart_bin and (os.path.isabs(str(tart_bin)) or os.sep in str(tart_bin)):
        child_env["CCHOUSE_TART_BIN"] = str(tart_bin)
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


def _print_next_steps(env_path, private_dir, route):
    if route == "tart-linux":
        doc = "docs/install-tart-linux.md"
    else:
        doc = "docs/install-virtualbox-linux.md"
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

    def __init__(self, message, exit_code=1):
        super().__init__(message)
        # subprocess uses negative values for POSIX signals.
        self.exit_code = 128 - exit_code if exit_code < 0 else exit_code


class StateCorruptError(StepError):
    """state.json 损坏或无法解析，绝不覆盖原文件。"""


PROBE_PRESENT = "present"
PROBE_ABSENT = "absent"
PROBE_UNKNOWN = "unknown"


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


VBOX_NAT_CIDR = "10.0.2.0/24"
VBOX_GUEST_IP = "127.0.0.1"
VBOX_GUEST_IF = "enp0s3"
VBOX_SSH_PORT_BASE = 2222
VBOX_SSH_PORT_SPAN = 200  # 2222..2421 inclusive


def vboxmanage_present():
    return preflight_mod.find_vboxmanage() is not None


def detect_vm_backend(route, system=None):
    """Apple Silicon 用 Tart，Windows 用 VirtualBox。"""
    sys_name = platform.system() if system is None else system
    if route == "tart-linux":
        return "tart" if sys_name == "Darwin" else "manual"
    if route != "virtualbox-linux" or sys_name != "Windows":
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
    """改写 instance.env 中单个非敏感键（如 GUEST_IP），保持 600 权限。"""
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
        self._log_lock = threading.Lock()
        self._logs_paused = False

    @contextmanager
    def pause_logs(self):
        with self._log_lock:
            self._logs_paused = True
        try:
            yield
        finally:
            with self._log_lock:
                self._logs_paused = False

    def log(self, msg):
        with self._log_lock:
            if not self._logs_paused:
                print(f"\n  [下载] {msg}", flush=True)

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
        self.artifact = downloads.load_image_artifact(ROOT / "guest-linux" / "image-manifest.json", route)
        self.image_path = self.cache_dir / "images" / self.artifact["filename"]
        self.golden_name = f"cchouse-debian13-arm64-{self.artifact['image_version']}"
        self.claude_platform = "linux-arm64" if route == "tart-linux" else "linux-x64"
        self.claude_path = self.cache_dir / f"claude-code-{self.claude_platform}.tgz"
        self.downloader = Downloader()
        self.state = self._load_state()
        self.tart_bin = _resolve_up_tart_bin(self.state) if self.backend == "tart" else None

    # ---- state.json（断点续跑 + 计时） ----
    def _load_state(self):
        data = {}
        if self.state_path.exists() or self.state_path.is_symlink():
            if not self.state_path.is_file() or self.state_path.is_symlink():
                raise StateCorruptError(
                    f"安装记录文件损坏或无法读取：{self.state_path}。为避免覆盖原记录，已停止操作。"
                    "原文件未被修改；请先检查该文件，或用 --tag 换一个新实例名重新安装。"
                )
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise StateCorruptError(
                    f"安装记录文件 {self.state_path} 已损坏（无法解析 JSON）。为避免覆盖原记录，已停止操作。"
                    "原文件未被修改；请先检查该文件，或用 --tag 换一个新实例名重新安装。"
                ) from None
            if not isinstance(data, dict):
                raise StateCorruptError(
                    f"安装记录文件 {self.state_path} 格式异常（不是有效对象）。为避免覆盖原记录，已停止操作。"
                    "原文件未被修改；请先检查该文件，或用 --tag 换一个新实例名重新安装。"
                )
        data.setdefault("completed_steps", [])
        data.setdefault("timings", {})
        data.setdefault("downloads", {})
        return data

    def save_state(self):
        if self.dry_run:
            return
        now = _now_iso()
        self.state.setdefault("created_at", now)
        self.state["updated_at"] = now
        self.state["route"] = self.route
        self.state["cchouse_version"] = (ROOT / "VERSION").read_text(encoding="ascii").strip()
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
            raise StepError(f"{what} 退出码 {exc.returncode}", exc.returncode) from None
        except FileNotFoundError:
            raise StepError(f"找不到命令：{cmd[0]}") from None

    def tart(self, *op):
        share_cfg = share_mod.normalize_share_state(self.state) if (op and op[0] in ("boot", "gui", "open")) else None
        return self.run(["bash", ROOT / "scripts" / "vm-tart.sh", *op], env=_tart_env(self.tart_bin, share_cfg))

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
                    size=art.get("size_bytes"), progress=prog, log=self.downloader.log,
                ),
            )
        if need_claude:
            self.downloader.start(
                "claude",
                f"Claude Code {self.claude_platform}",
                lambda prog: downloads.fetch_claude_code(
                    self.claude_platform, self.claude_path, progress=prog, mirror=self.args.npm_mirror,
                    log=self.downloader.log,
                )["status"],
            )
        if need_vbox:
            man = self.vbox_manifest
            self.downloader.start(
                "virtualbox",
                f"VirtualBox {man['version']}",
                lambda prog, manifest=man, dest=self.vbox_installer: downloads.fetch_verified(
                    [manifest["url"]], dest, sha256=manifest["sha256"], size=manifest.get("size_bytes"),
                    progress=prog, log=self.downloader.log,
                ),
            )

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


    def doctor_cmd(self, env_arg="", vm_arg=""):
        py_exe = str(Path(sys.executable).resolve())
        doc_script = str(ROOT / "scripts" / "doctor.py")
        if platform.system() == "Windows":
            return f'& "{py_exe}" -I "{doc_script}"{env_arg}{vm_arg}'
        return f'"{py_exe}" -I "{doc_script}"{env_arg}{vm_arg}'


def _stdin_is_tty():
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            if not handle or handle == -1:
                return False
            mode = ctypes.c_uint32()
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        return True
    except Exception:
        return False


def _readme_home_cmd(route):
    readme = ROOT / "README.md"
    if not readme.is_file():
        return None
    heading = "### Windows（x64）" if route == "virtualbox-linux" else "### Apple Silicon Mac"
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError:
        return None
    pos = text.find(heading)
    if pos < 0:
        return None
    m = re.search(r"```(?:powershell|bash)\s*\n([^\n]+)\n```", text[pos:])
    return m.group(1).strip() if m else None


def _home_install_cmd(ctx):
    env_cmd = os.environ.get("CCHOUSE_HOME_CMD", "").strip()
    if env_cmd:
        return env_cmd
    cmd = _readme_home_cmd(ctx.route)
    if not cmd:
        return ctx.rerun_cmd()
    is_win = ctx.route == "virtualbox-linux"
    if ctx.tag != "cc01":
        cmd += f" -Tag '{ctx.tag}'" if is_win else f" --tag '{ctx.tag}'"
    if getattr(ctx.args, "image_mirror", None):
        val = ctx.args.image_mirror
        cmd += f" -ImageMirror '{val}'" if is_win else f" --image-mirror '{val}'"
    if getattr(ctx.args, "npm_mirror", None):
        val = ctx.args.npm_mirror
        cmd += f" -NpmMirror '{val}'" if is_win else f" --npm-mirror '{val}'"
    if getattr(ctx.args, "share", None):
        val = ctx.args.share
        cmd += f" -Share '{val}'" if is_win else f" --share '{val}'"
    if getattr(ctx.args, "read_only", False):
        cmd += " -ShareReadOnly" if is_win else " --share-read-only"
    return cmd


def _need_tty_message(home_cmd):
    return (
        "需要你本人输入节点链接：请在你自己的终端窗口运行下面这条命令（AI 无法也不应替你输入）\n\n"
        f"{home_cmd}"
    )


def _needs_interactive_node_input(ctx):
    return (
        not getattr(ctx.args, "link_stdin", False)
        and not getattr(ctx.args, "link_file", None)
        and not getattr(ctx.args, "link_clipboard", False)
        and (not ctx.env_path.is_file() or bool(getattr(ctx.args, "reconfigure", False)))
    )


def do_config(ctx):
    """第一轮逻辑：无回显读取 1 或 2 条节点链接（或安全读取 --link-clipboard / --link-file）→ 分类解析 → 临时 mihomo 探测出口 → 生成密钥 → 写 600 权限 instance.env。"""
    args = ctx.args
    tag, route, private_dir = ctx.tag, ctx.route, ctx.private_dir
    link_file = getattr(args, "link_file", None)
    link_clipboard = bool(getattr(args, "link_clipboard", False))
    if link_file and link_clipboard:
        raise StepError("--link-file 与 --link-clipboard 不能同时使用，请二选一。")
    if not link_file and not link_clipboard and not args.link_stdin and not _stdin_is_tty():
        raise StepError(_need_tty_message(_home_install_cmd(ctx)), exit_code=EXIT_NEED_TTY)

    raw_links = []
    try:
        if link_clipboard:
            raw_links = read_clipboard_links()
        elif link_file:
            raw_links = read_link_file_lines(link_file)
        elif args.link_stdin:
            raw_text = sys.stdin.read()
            if raw_text.startswith("\ufeff"):
                raw_text = raw_text[1:]
            raw_links = [line.strip() for line in raw_text.splitlines() if line.strip()]
            if not raw_links:
                raise EOFError
        else:
            with ctx.downloader.pause_logs():
                first_link = getpass.getpass("请粘贴节点链接（输入不回显，支持 SOCKS5 / HTTP(S) / VLESS / Trojan / SS）: ")
            raw_links = [first_link]
            # 先解析第一条以确定是否为加密协议，若是则追问是否有第二条出口链接
            first_parsed = parse_node_link(first_link)
            if first_parsed["SINGLE_TYPE"] in ENCRYPTED_NODE_TYPES:
                with ctx.downloader.pause_logs():
                    second_link = getpass.getpass(
                        "如果你的固定 IP 出口是另一条 SOCKS5/HTTP 链接，请粘贴；没有直接回车: "
                    )
                if second_link.strip():
                    raw_links.append(second_link)
    except NodeLinkError as exc:
        if link_clipboard and str(exc) != CLIPBOARD_UNAVAILABLE_MSG:
            raise StepError(CLIPBOARD_NOT_LINK_MSG) from None
        raise StepError(str(exc) if (link_file or link_clipboard) else f"节点链接校验失败：{exc}") from None
    except EOFError:
        raise StepError("输入已结束，请在终端交互运行，或用 --link-clipboard / --link-file / --link-stdin 提供节点链接") from None
    except KeyboardInterrupt:
        raise StepError("已取消输入节点链接") from None

    try:
        single_fields, via_fields = parse_and_classify_links(raw_links)
    except NodeLinkError as exc:
        if link_clipboard and len(raw_links) == 1 and "://" not in raw_links[0]:
            raise StepError(CLIPBOARD_NOT_LINK_MSG) from None
        if link_clipboard and "缺少协议头" in str(exc):
            raise StepError(CLIPBOARD_NOT_LINK_MSG) from None
        raise StepError(f"节点链接校验失败：{exc}") from None
    finally:
        raw_links = []

    if not single_fields["SINGLE_SERVER_IP"]:
        resolved = resolve_server_ipv4(single_fields["SINGLE_SERVER"])
        if resolved:
            single_fields["SINGLE_SERVER_IP"] = resolved
        else:
            raise StepError("宿主机无法解析出口节点的 IPv4 地址。请检查节点地址与 DNS 后重试。")
    if via_fields and not via_fields["SINGLE_VIA_SERVER_IP"]:
        resolved_via = resolve_server_ipv4(via_fields["SINGLE_VIA_SERVER"])
        if resolved_via:
            via_fields["SINGLE_VIA_SERVER_IP"] = resolved_via
        else:
            raise StepError("宿主机无法解析前置节点的 IPv4 地址。请检查前置节点地址与 DNS 后重试。")

    if via_fields:
        print(
            f"[cchouse up] 虚拟机后端：{ctx.backend} | 模式：两跳（前置 {via_fields['SINGLE_VIA_TYPE']} → 出口 {single_fields['SINGLE_TYPE']}）"
        )
    else:
        print(f"[cchouse up] 虚拟机后端：{ctx.backend} | 协议：{single_fields['SINGLE_TYPE']}")

    probed = None
    if not args.no_probe:
        print("[cchouse up] 正在通过宿主临时 mihomo 连续 3 次探测固定出口 IP 与时区（凭据不落盘不回显）...")
        if via_fields:
            probed = probe_egress_via_proxy(single_fields, via_fields=via_fields)
        else:
            probed = probe_egress_via_proxy(single_fields)
    probed = probed or {}
    egress_cfg = {
        "timezone": args.timezone or probed.get("timezone", ""),
        "ip": args.expected_ip or probed.get("ip", ""),
        "loc": getattr(args, "expected_country", None) or probed.get("country", ""),
        "asn": getattr(args, "expected_asn", None) or probed.get("asn", ""),
    }
    if not (all(isinstance(value, str) for value in egress_cfg.values())
            and render_mod.is_ipv4(egress_cfg["ip"])
            and re.fullmatch(r"[A-Z]{2}", egress_cfg["loc"])
            and re.fullmatch(r"AS[0-9]+", egress_cfg["asn"])
            and re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", egress_cfg["timezone"])):
        raise StepError(
            "未获得完整出口信息。请确认节点可用后重试，或按进阶文档填写"
            f"出口 IPv4、国家/地区代码、ASN 和 IANA 时区。\n  {ADVANCED_NODE_GUIDE}"
        )
    print("[cchouse up] 已取得出口 IPv4、国家/地区、ASN 与时区。")

    try:
        ssh_key, ssh_pubkey = generate_ssh_keypair(private_dir, tag)
    except RuntimeError as exc:
        raise StepError(str(exc)) from None

    if route == "tart-linux":
        vmnet_cidr = "192.168.64.0/24"
        guest_ip = "192.168.64.2"
    else:
        vmnet_cidr = VBOX_NAT_CIDR
        guest_ip = VBOX_GUEST_IP

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
        via_fields=via_fields,
    )
    write_env_secure(ctx.env_path, env_text)
    print(f"[cchouse up] 已生成私有配置（权限 600）：{ctx.env_path}")
    if link_clipboard:
        if clear_clipboard():
            print("[cchouse up] 节点配置已保存，已清空剪贴板。")
        else:
            print("[cchouse up] 节点配置已保存，但未能自动清空剪贴板，请手动清空。")
    elif link_file:
        print(f"[cchouse up] 节点配置已保存，现在可以删掉 {link_file} 了（cchouse 不会自动删除该文件）。")


def step_config(ctx):
    if ctx.env_path.is_file() and not ctx.args.reconfigure:
        if getattr(ctx.args, "link_clipboard", False):
            print(f"  已有私有配置 {ctx.env_path}，已忽略 --link-clipboard（更换节点请加 --reconfigure）")
        elif getattr(ctx.args, "link_file", None):
            print(f"  已有私有配置 {ctx.env_path}，已忽略 --link-file（更换节点请加 --reconfigure）")
        else:
            print(f"  已有私有配置 {ctx.env_path}，沿用（更换节点请加 --reconfigure）")
        return True
    do_config(ctx)
    return False


def step_preflight(ctx):
    apply_virtualbox_guest_net(ctx)
    rc, pf_json, pf_stderr = run_preflight_check(
        ctx.env_path, ctx.route, check_stripe=(not ctx.args.no_probe), tart_bin=ctx.tart_bin,
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
    if not ctx.state.get("vm_created_by_up") and ctx.env_path.is_file():
        node_type = _env_raw_value(ctx.env_path, "SINGLE_TYPE")
        if node_type and node_type not in QUICK_NODE_TYPES:
            raise StepError(ADVANCED_NODE_MESSAGE)
    apply_virtualbox_guest_net(ctx)
    ctx.wait_download("claude")
    py = sys.executable
    try:
        ctx.run([py, "-I", ROOT / "scripts" / "render.py", "--resolve", ctx.env_path, ctx.rendered])
        seed_cmd = [py, "-I", ROOT / "scripts" / "make-seed-iso.py", ctx.rendered, ctx.seed_iso, "--claude-tarball", ctx.claude_path]
        if ctx.backend == "virtualbox":
            seed_cmd.append("--poweroff-after-provision")
        ctx.run(seed_cmd)
    finally:
        # 渲染明文只用于打包 ISO，用完即删
        shutil.rmtree(ctx.rendered, ignore_errors=True)
    return False


def step_download(ctx):
    return ctx.wait_download("image")


def _resolve_executable_tart(tart_bin=None):
    """解析并确认 Tart 可执行文件存在且可执行；返回 (exe_path, error_msg)。"""
    if tart_bin:
        cand = str(tart_bin)
        if os.path.isabs(cand) or os.sep in cand:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand, None
            return None, f"找不到或无法执行 Tart 程序：{cand}"
        found = shutil.which(cand) or preflight_mod.find_tart()
        if found and os.path.isfile(found) and os.access(found, os.X_OK):
            return os.path.abspath(found), None
        return None, f"找不到或无法执行 Tart 程序：{cand}"
    found = _resolve_up_tart_bin()
    if found and os.path.isfile(found) and os.access(found, os.X_OK):
        return found, None
    return None, "找不到可执行的 Tart 程序"


def _probe_tart_inventory(tart_bin=None, *, require_home_dir=True):
    """三态查询 Tart 本地清单：先核对可执行文件、TART_HOME 在线可读、list 成功。

    返回 (inventory_dict, error_msg)；error_msg 非空时代表「查不了」。
    """
    exe, err = _resolve_executable_tart(tart_bin)
    if err:
        return None, err
    th = preflight_mod.tart_home_dir(exe)
    explicit_home = bool(os.environ.get("TART_HOME", "").strip()) or preflight_mod._parse_wrapper_tart_home(exe) is not None
    if th.is_dir():
        try:
            os.listdir(th)
        except OSError:
            return None, f"无法读取 Tart 目录：{th}"
    elif require_home_dir or explicit_home:
        return None, f"无法访问 Tart 目录：{th}"
    try:
        proc = subprocess.run(
            [exe, "list", "--source", "local", "--format", "json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None, f"无法运行 Tart 列表查询：{exe}"
    if proc.returncode != 0:
        return None, f"Tart 列表查询失败（退出码 {proc.returncode}）"
    try:
        items = json.loads(proc.stdout or "")
    except (ValueError, TypeError):
        return None, "无法解析 Tart 虚拟机列表"
    if not isinstance(items, list):
        return None, "无法解析 Tart 虚拟机列表"
    inv = {}
    for vm in items:
        if isinstance(vm, dict) and isinstance(vm.get("Name"), str):
            inv[vm["Name"]] = vm
    return inv, None


def _probe_tart_target(name, tart_bin=None, *, inventory=None, inv_error=None, require_home_dir=True):
    """查询单个 Tart 虚拟机或快照：返回 (PROBE_PRESENT|PROBE_ABSENT|PROBE_UNKNOWN, reason, meta)。"""
    if inventory is None and inv_error is None:
        inventory, inv_error = _probe_tart_inventory(tart_bin, require_home_dir=require_home_dir)
    if inv_error:
        return PROBE_UNKNOWN, inv_error, None
    if name in inventory:
        return PROBE_PRESENT, None, inventory[name]
    vm_dir = tart_vm_dir(name, tart_bin)
    try:
        if vm_dir.exists() or vm_dir.is_symlink():
            return PROBE_UNKNOWN, f"Tart 列表中未见 {name}，但目录 {vm_dir} 仍存在", None
    except OSError:
        return PROBE_UNKNOWN, f"无法读取 Tart 目录：{vm_dir}", None
    return PROBE_ABSENT, None, None


def _vbox_unregistered_remnants(private_dir):
    """检查 VirtualBox 实例私有目录 vm/ 下是否有未注册残留；返回问题描述或 None。"""
    if private_dir is None:
        return None
    vm_dir = Path(private_dir) / "vm"
    try:
        if vm_dir.is_symlink():
            return f"私有目录 {vm_dir} 为符号链接，无法确认残留状态"
        if vm_dir.exists():
            if not vm_dir.is_dir():
                return f"私有目录 {vm_dir} 存在异常文件残留"
            if any(vm_dir.iterdir()):
                return f"私有目录 {vm_dir} 下存在未注册的虚拟机残留文件"
    except OSError:
        return f"无法读取私有目录 {vm_dir}"
    return None


def _vbox_cfg_matches(cfg_file, private_dir, vm_name):
    """校验 VirtualBox 注册的 CfgFile 是否属于本实例 private_dir/vm/<vm_name>/<vm_name>.vbox。"""
    if not cfg_file or private_dir is None:
        return True
    expected = _remove_path(Path(private_dir).resolve()) / "vm" / vm_name / f"{vm_name}.vbox"
    actual = _remove_path(cfg_file)
    if os.name == "nt":
        return str(actual).casefold() == str(expected).casefold()
    return actual == expected


def _probe_vbox_status(vm_name, snapshot, private_dir=None, *, allow_missing_vboxmanage=False):
    """三态查询 VirtualBox 虚拟机与快照状态。"""
    if allow_missing_vboxmanage and not vboxmanage_present():
        remnant = _vbox_unregistered_remnants(private_dir)
        if remnant:
            return {
                "vm": PROBE_UNKNOWN, "vm_state": "unknown", "snapshot": PROBE_UNKNOWN,
                "cfg_file": None, "reason": remnant,
            }
        return {
            "vm": PROBE_ABSENT, "vm_state": "missing", "snapshot": PROBE_ABSENT,
            "cfg_file": None, "reason": None,
        }
    try:
        proc = subprocess.run(
            _virtualbox_cmd("status", VmName=vm_name, Snapshot=snapshot),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "vm": PROBE_UNKNOWN, "vm_state": "unknown", "snapshot": PROBE_UNKNOWN,
            "cfg_file": None, "reason": "VBoxManage 查询失败",
        }
    if proc.returncode != 0:
        return {
            "vm": PROBE_UNKNOWN, "vm_state": "unknown", "snapshot": PROBE_UNKNOWN,
            "cfg_file": None, "reason": f"VBoxManage 查询失败（退出码 {proc.returncode}）",
        }
    vm_state = None
    snap_raw = None
    cfg_file = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("VM_STATE="):
            vm_state = line.split("=", 1)[1].strip()
        elif line.startswith("SNAPSHOT_STATE="):
            snap_raw = line.split("=", 1)[1].strip().lower()
        elif line.startswith("CFG_FILE="):
            cfg_file = line.split("=", 1)[1].strip()
    if vm_state in ("running", "running-headless", "stopped"):
        if snap_raw == "present":
            snap_probe = PROBE_PRESENT
        elif snap_raw == "missing":
            snap_probe = PROBE_ABSENT
        else:
            snap_probe = PROBE_UNKNOWN
        return {
            "vm": PROBE_PRESENT, "vm_state": vm_state, "snapshot": snap_probe,
            "cfg_file": cfg_file, "reason": None,
        }
    if vm_state == "missing":
        remnant = _vbox_unregistered_remnants(private_dir)
        if remnant:
            return {
                "vm": PROBE_UNKNOWN, "vm_state": "unknown", "snapshot": PROBE_UNKNOWN,
                "cfg_file": None, "reason": remnant,
            }
        return {
            "vm": PROBE_ABSENT, "vm_state": "missing", "snapshot": PROBE_ABSENT,
            "cfg_file": None, "reason": None,
        }
    reason = (
        "VirtualBox 中的虚拟机处于不可访问（inaccessible）状态"
        if vm_state == "inaccessible"
        else "VBoxManage 返回的状态无法识别"
    )
    return {
        "vm": PROBE_UNKNOWN, "vm_state": "unknown", "snapshot": PROBE_UNKNOWN,
        "cfg_file": cfg_file, "reason": reason,
    }


def probe_vm_state(ctx):
    """三态查询当前实例虚拟机存在性，返回 (PROBE_PRESENT|PROBE_ABSENT|PROBE_UNKNOWN, reason)。"""
    if ctx.dry_run or ctx.backend == "manual":
        return PROBE_ABSENT, None
    has_prior = bool(ctx.state.get("vm_created_by_up") or ctx.done("import") or ctx.done("snapshot") or ctx.state.get("vm_name"))
    if ctx.backend == "tart":
        state, reason, _meta = _probe_tart_target(
            ctx.vm_name, ctx.tart_bin, require_home_dir=(has_prior or bool(ctx.state.get("tart_bin"))),
        )
        return state, reason
    if ctx.backend == "virtualbox":
        snap = ctx.state.get("clean_snapshot") or CLEAN_SNAPSHOT
        res = _probe_vbox_status(ctx.vm_name, snap, ctx.private_dir, allow_missing_vboxmanage=(not has_prior))
        return res["vm"], res["reason"]
    return PROBE_ABSENT, None


def tart_vm_dir(name, tart_bin=None):
    return preflight_mod.tart_home_dir(tart_bin) / "vms" / name


def tart_vm_identity(name, tart_bin=None):
    try:
        info = tart_vm_dir(name, tart_bin).lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino] if stat.S_ISDIR(info.st_mode) else None


def _tart_vm_config_mac(name, tart_bin=None):
    cfg = tart_vm_dir(name, tart_bin) / "config.json"
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("macAddress", "MAC", "mac"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return None


def _tart_vm_mac(name, tart_bin=None):
    exe = tart_bin or _tart_bin()
    try:
        proc = subprocess.run(
            [str(exe), "get", name, "--format", "json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("MAC", "mac", "macAddress"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    for k, v in data.items():
        if str(k).lower() in ("mac", "macaddress") and isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _tart_display_cmd(tart_bin=None):
    exe = tart_bin or _tart_bin()
    if exe == "tart":
        resolved = shutil.which("tart") or preflight_mod.find_tart()
        if resolved:
            exe = os.path.abspath(resolved)
    if exe and (os.path.isabs(str(exe)) or os.sep in str(exe)):
        return f'"{exe}"'
    return str(exe or "tart")


def _tart_unverifiable_ownership_msg(name, tart_bin=None):
    tcmd = _tart_display_cmd(tart_bin)
    return (
        f"无法核验 Tart 虚拟机或快照 {name} 的归属（state.json 缺少目录身份和 MAC 记录），"
        f"已拒绝自动操作。请先运行 {tcmd} list 核对，确认归属无误后再手动清理："
        f'{tcmd} stop "{name}" && {tcmd} delete "{name}"。'
    )


def _verify_tart_ownership(name, state, identity_key, mac_key, tart_bin=None):
    """用同一个 tart 与目录身份双重校验目标虚拟机/快照归属；缺少记录或对不上立即报错，绝不操作。"""
    has_id = identity_key in state and state.get(identity_key) is not None
    has_mac = mac_key in state and bool(state.get(mac_key))
    if not has_id and not has_mac:
        raise StepError(_tart_unverifiable_ownership_msg(name, tart_bin))
    if has_id:
        cur_id = tart_vm_identity(name, tart_bin)
        if cur_id is None or cur_id != state[identity_key]:
            raise StepError(vm_name_conflict(name))
    dir_mac = _tart_vm_config_mac(name, tart_bin)
    if has_mac or dir_mac:
        live_mac = _tart_vm_mac(name, tart_bin)
        if has_mac and (not live_mac or live_mac != str(state[mac_key]).strip().lower()):
            raise StepError(vm_name_conflict(name))
        if dir_mac and (not live_mac or live_mac != dir_mac):
            raise StepError(vm_name_conflict(name))


def vm_name_conflict(name):
    return (f"已存在同名虚拟机或快照 {name}，此安装记录无法确认归属。"
            "请用 --tag 换一个实例名（Mac 首页命令末尾加 --tag cc02），原虚拟机不会被改动。")


def image_identity(artifact):
    fields = ("image_version", "route", "arch", "format", "filename", "size_bytes", "sha256", "source_commit")
    return {key: artifact[key] for key in fields if key in artifact}


def _ensure_virtualbox_installed(ctx):
    man = ctx.vbox_manifest
    if not man:
        raise StepError("缺少 VirtualBox 安装清单")
    if ctx.dry_run:
        ctx.virtualbox("install", Installer=ctx.vbox_installer, Sha256=man["sha256"])
        return
    if vboxmanage_present():
        return
    try:
        ctx.wait_download("virtualbox")
        downloads.require_sha256(ctx.vbox_installer, man["sha256"])
        print(f"  将安装 Oracle VirtualBox {man['version']} 基础包。若弹出 Windows UAC，请确认一次。", flush=True)
        ctx.virtualbox("install", Installer=ctx.vbox_installer, Sha256=man["sha256"])
    except (StepError, downloads.DownloadError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, StepError) else 1
        raise StepError(f"VirtualBox 自动安装失败：{exc}\n  Oracle 官方安装包：{man['url']}", code) from None


def step_import(ctx):
    # 只重建 up 自己建过的虚拟机；同名的既有虚拟机（可能已登录）绝不覆盖
    owned = ctx.state.get("vm_created_by_up") is True
    if ctx.backend == "virtualbox":
        _ensure_virtualbox_installed(ctx)
    vm_state, probe_err = probe_vm_state(ctx)
    if vm_state == PROBE_UNKNOWN:
        raise StepError(f"目前无法确认原虚拟机状态（{probe_err}），已停止导入。")
    exists = vm_state == PROBE_PRESENT
    if exists and not owned:
        raise StepError(vm_name_conflict(ctx.vm_name))
    if ctx.backend == "tart":
        if exists:
            _verify_tart_ownership(ctx.vm_name, ctx.state, "tart_vm_identity", "tart_vm_mac", ctx.tart_bin)
        existed = False
        if not ctx.dry_run:
            g_state, g_err, _ = _probe_tart_target(ctx.golden_name, ctx.tart_bin, require_home_dir=False)
            if g_state == PROBE_UNKNOWN:
                raise StepError(f"无法确认 Tart 黄金镜像状态（{g_err}），已停止导入。")
            existed = g_state == PROBE_PRESENT
        if existed:
            print(f"  本机已有黄金镜像 {ctx.golden_name}，直接克隆（热缓存）")
        else:
            ctx.tart("import-new", ctx.golden_name, ctx.image_path)
        if exists:
            ctx.tart("delete", ctx.vm_name)
        ctx.tart("clone-new", ctx.golden_name, ctx.vm_name)
        updates = dict(
            vm_created_by_up=True, vm_name=ctx.vm_name, vm_backend=ctx.backend,
            golden_name=ctx.golden_name, tart_vm_identity=tart_vm_identity(ctx.vm_name, ctx.tart_bin),
            image_artifact=image_identity(ctx.artifact),
            image_artifact_origin="reused_tart_golden" if existed else "verified_download",
        )
        if ctx.tart_bin:
            updates["tart_bin"] = ctx.tart_bin
        if not ctx.dry_run:
            vm_mac = _tart_vm_mac(ctx.vm_name, ctx.tart_bin)
            if vm_mac:
                updates["tart_vm_mac"] = vm_mac
        ctx.state.update(updates)
        ctx.save_state()
        return existed
    if ctx.backend == "virtualbox":
        apply_virtualbox_guest_net(ctx)
        ctx.virtualbox(
            "import",
            Ova=ctx.image_path,
            VmName=ctx.vm_name,
            VmDir=ctx.private_dir / "vm",
            ReplaceExisting="yes" if owned else "no",
            SshPort=str(ctx.vbox_ssh_port),
            HostKeyLog=str(ctx.hostkey_log),
        )
        ctx.state.update(vm_created_by_up=True, vm_name=ctx.vm_name, vm_backend=ctx.backend,
                         image_artifact=image_identity(ctx.artifact), image_artifact_origin="verified_download")
        ctx.save_state()
        return False
    raise StepError("当前后端不支持自动导入")


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
            HostKeyLog=str(ctx.hostkey_log),
        )
    else:
        raise StepError("当前后端不支持自动首启")
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
            **_vbox_share_kwargs(ctx.state),
        )
        ctx.delete_plaintext()
    else:
        raise StepError("当前后端不支持自动联网重启")
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
        raise StepError(
            f"自动验收未通过（{exc}）；断网保护不会放宽，请按上方 FAIL 项排查代理",
            exc.exit_code,
        ) from None
    return False


def step_snapshot(ctx):
    if ctx.backend == "tart":
        snap = f"{ctx.vm_name}-clean"
        snap_exists = False
        if not ctx.dry_run:
            s_state, s_err, _ = _probe_tart_target(snap, ctx.tart_bin, require_home_dir=True)
            if s_state == PROBE_UNKNOWN:
                raise StepError(f"无法确认干净快照 {snap} 的状态（{s_err}），已停止。")
            snap_exists = s_state == PROBE_PRESENT
        if snap_exists:
            if not ctx.state.get("tart_snapshot_identity") and not ctx.state.get("tart_snapshot_mac"):
                raise StepError(vm_name_conflict(snap))
            _verify_tart_ownership(snap, ctx.state, "tart_snapshot_identity", "tart_snapshot_mac", ctx.tart_bin)
            if not ctx.dry_run:
                ctx.tart("stop", ctx.vm_name)
        else:
            if not ctx.dry_run:
                _verify_tart_ownership(ctx.vm_name, ctx.state, "tart_vm_identity", "tart_vm_mac", ctx.tart_bin)
            ctx.tart("snapshot-new", ctx.vm_name, snap)
        snap_updates = dict(clean_snapshot=snap, tart_snapshot_identity=tart_vm_identity(snap, ctx.tart_bin))
        if ctx.tart_bin:
            snap_updates["tart_bin"] = ctx.tart_bin
        if not ctx.dry_run:
            snap_mac = _tart_vm_mac(snap, ctx.tart_bin)
            if snap_mac:
                snap_updates["tart_snapshot_mac"] = snap_mac
        ctx.state.update(snap_updates)
        ctx.save_state()
        ctx.tart("gui", ctx.vm_name, ctx.logs / "tart-gui.log")
    elif ctx.backend == "virtualbox":
        snap = CLEAN_SNAPSHOT
        if ctx.dry_run:
            ctx.virtualbox("stop", VmName=ctx.vm_name, Force="no", Timeout="60")
            ctx.virtualbox("snapshot", VmName=ctx.vm_name, Snapshot=snap)
            ctx.virtualbox("gui", VmName=ctx.vm_name, **_vbox_share_kwargs(ctx.state))
        else:
            info = {
                "tag": ctx.tag,
                "backend": ctx.backend,
                "vm_name": ctx.vm_name,
                "snapshot": snap,
                "ssh_key": ctx.ssh_key,
                "known_hosts": ctx.known_hosts,
                "env_path": ctx.env_path,
            }
            stopped = _ssh_graceful_poweroff(info) and _wait_stopped(info, 60)
            if not stopped:
                ctx.virtualbox("stop", VmName=ctx.vm_name, Force="no", Timeout="60")
            ctx.virtualbox("snapshot", VmName=ctx.vm_name, Snapshot=snap)
            ctx.state["clean_snapshot"] = snap
            try:
                launcher_mod.create(ctx.tag, ctx.state)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"WARN  创建桌面启动图标失败（不影响客机使用）：{exc}", file=sys.stderr)
            ctx.save_state()
            ctx.virtualbox("gui", VmName=ctx.vm_name, **_vbox_share_kwargs(ctx.state))
    else:
        raise StepError("当前后端不支持自动保存快照")
    ctx.state["clean_snapshot"] = snap
    if not ctx.dry_run:
        if platform.system() == "Darwin":
            try:
                entry, in_path = launcher_mod.install_cli_entry()
                if entry:
                    ctx.state["cli_entry"] = str(entry)
                    ctx.state["cli_entry_in_path"] = bool(in_path)
            except OSError as exc:
                print(f"  WARN  命令入口 ~/.local/bin/cchouse 写入失败：{exc}")
            try:
                paths = launcher_mod.create(ctx.tag, ctx.state)
                if paths:
                    has_desktop = any("Desktop" in p for p in paths)
                    label = "桌面启动入口" if has_desktop else "应用启动入口"
                    print(f"  已创建{label}：{', '.join(paths)}")
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"  WARN  启动入口创建失败：{exc}")
        ctx.save_state()
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
    if name == "seed" and not ctx.done("firstboot") and not ctx.seed_iso.is_file():
        return True
    if ctx.args.reconfigure and name in ("config", "preflight"):
        return True
    return False


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
    # Windows 经 SSH 等非交互会话安装时不弹窗口、客机保持关机，这里不说「已开机」
    print(f"- 客机 {ctx.vm_name} 已就绪，干净快照：{ctx.state.get('clean_snapshot')}")
    share_cfg = share_mod.normalize_share_state(ctx.state)
    if share_cfg["enabled"]:
        mode = "只读" if share_cfg["read_only"] else "读写"
        print(f"- 共享文件夹：{share_cfg['host_path']}（{mode}）→ 客机内 ~/shared")
    launchers = ctx.state.get("launchers") or []
    if launchers:
        desktop = [p for p in launchers if "Desktop" in p]
        print(f"- 启动图标：{(desktop or launchers)[-1]}（双击即可打开客机窗口）")
    cli_entry = ctx.state.get("cli_entry")
    if cli_entry:
        if ctx.state.get("cli_entry_in_path"):
            cli_cmd = "cchouse"
        else:
            cli_cmd = cli_entry
            print(f"- 命令入口已安装到 {cli_entry}（当前 PATH 未包含 ~/.local/bin，可直接运行该完整路径）")
        print(f"- 日常命令：{cli_cmd} open --tag {ctx.tag} | {cli_cmd} stop --tag {ctx.tag} | {cli_cmd} status --tag {ctx.tag} | {cli_cmd} restore --tag {ctx.tag}")
    print("- 请在客机桌面核对 check.html 全绿，然后自行登录 Claude Code。")
    print("- Claude Code 的 OAuth 登录链接只能在客机浏览器里打开，严禁复制到宿主浏览器。")
    print("- cchouse 不会替你登录、注册、订阅或付款。")


def run_pipeline(ctx):
    if getattr(ctx.args, "skip_preflight", False) and ctx.args.config_only:
        steps = UP_STEPS[:1]
    elif ctx.args.config_only:
        steps = UP_STEPS[:2]
    else:
        steps = UP_STEPS
    n = len(steps)
    if ctx.dry_run:
        print(f"[cchouse up] dry-run：路线 {ctx.route} | 虚拟机后端 {ctx.backend} | 不下载、不动虚拟机、不写 state.json")
    downloads_started = False
    for i, (name, title) in enumerate(steps, 1):
        # 节点校验（config/preflight）通过后再启动大文件下载（VirtualBox/镜像/Claude Code），节点不行不白等
        if not downloads_started and name not in ("config", "preflight"):
            ctx.start_downloads()
            downloads_started = True
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
            on_failure(ctx, steps, i, name, title, reason)
            return exc.exit_code if isinstance(exc, StepError) else 1
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
    print("  修复后请原样重跑首页安装命令，会从这一步继续（state.json 断点续跑）。")
    print(f"  如在源码目录手动运行，也可执行：{ctx.rerun_cmd()}")
    print("  需要求助时导出脱敏诊断包：")
    print(f"    {ctx.doctor_cmd(env_arg, vm_arg)}")


def finish_manual(ctx, steps):
    ctx.state.update(status="HUMAN_WAIT", ok=True, next_action="manual_import_and_firstboot")
    print_timing_table(ctx, steps)
    ctx.save_state()
    if ctx.route == "tart-linux":
        doc = "docs/install-tart-linux.md"
    else:
        doc = "docs/install-virtualbox-linux.md"
    print("\n== 当前宿主暂不支持自动导入（默认自动化：Apple Silicon + Tart、Windows + VirtualBox） ==")
    print(f"- 已下载并校验的镜像：{ctx.image_path}")
    print(f"- 首启注入光盘（含明文凭据，首启完成后立即删除）：{ctx.seed_iso}")
    print(f"- 按 {doc} 导入镜像、挂载 seed.iso 首次开机，然后运行：")
    print(f'    bash scripts/verify-linux.sh "{ctx.env_path}" --fail-closed')
    return 0


def _unknown_backend_msg(backend, reason):
    detail = f"（{reason}）" if reason else ""
    if backend == "tart":
        return (
            f"目前无法确认原虚拟机在哪里{detail}。\n"
            "请先接回原磁盘，或在 Tart 中确认原虚拟机可访问，然后重跑这条命令。"
        )
    return (
        f"目前无法确认原虚拟机在哪里{detail}。\n"
        "请先在 VirtualBox 中确认原虚拟机可访问（或检查私有目录下的残留文件），然后重跑这条命令。"
    )


def _conflict_object_msg(name, backend):
    app = "Tart" if backend == "tart" else "VirtualBox"
    return (
        f"找到名为 {name} 的虚拟机或快照，但无法确认它属于这份安装记录。\n"
        f"请先在 {app} 中核对原机；如果只想另装一台，请用 --tag 换一个实例名（例如 --tag cc02）。"
    )


def _extract_env_node_fields(env, prefix):
    suffixes = (
        "TYPE", "SERVER", "SERVER_IP", "PORT", "USER", "PASS", "UUID",
        "SNI", "FINGERPRINT", "FLOW", "NETWORK", "WS_PATH", "WS_HOST",
        "PUBLIC_KEY", "SHORT_ID", "CIPHER", "TLS", "HOST_KEY",
    )
    return {f"{prefix}_{s}": env.get(f"{prefix}_{s}", "") for s in suffixes}


def _verify_saved_egress(ctx):
    """重建前复验保存的节点可用性与出口一致性；不可用或出口漂移即停，绝不打印节点或凭据。"""
    if ctx.args.no_probe:
        return
    try:
        env = render_mod.load_env(ctx.env_path)
    except Exception as exc:
        raise StepError(f"无法读取节点配置文件 {ctx.env_path}：{exc}") from None
    if env.get("EXIT_MODE", "single") != "single":
        return
    if env.get("SINGLE_TYPE", "") not in QUICK_NODE_TYPES:
        return
    fields = _extract_env_node_fields(env, "SINGLE")
    if not fields["SINGLE_SERVER_IP"] and fields["SINGLE_SERVER"]:
        fields["SINGLE_SERVER_IP"] = resolve_server_ipv4(fields["SINGLE_SERVER"])
    if not fields["SINGLE_SERVER_IP"]:
        raise StepError(
            "保存的节点暂时无法验证（无法解析节点地址）。\n"
            "请检查节点，在恢复可用后重跑同一条命令；如果要更换节点，请加 --reconfigure 重新粘贴链接。"
        )
    via_fields = None
    if render_mod.has_single_via(env):
        via_fields = _extract_env_node_fields(env, "SINGLE_VIA")
        if not via_fields["SINGLE_VIA_SERVER_IP"] and via_fields["SINGLE_VIA_SERVER"]:
            via_fields["SINGLE_VIA_SERVER_IP"] = resolve_server_ipv4(via_fields["SINGLE_VIA_SERVER"])
        if not via_fields["SINGLE_VIA_SERVER_IP"]:
            raise StepError(
                "保存的前置节点暂时无法验证（无法解析前置节点地址）。\n"
                "请检查节点，在恢复可用后重跑同一条命令；如果要更换节点，请加 --reconfigure 重新粘贴链接。"
            )
    try:
        probed = probe_egress_via_proxy(fields, via_fields=via_fields) if via_fields else probe_egress_via_proxy(fields)
    except StepError:
        probed = None
    if not probed or not probed.get("ip"):
        raise StepError(
            "保存的节点暂时无法验证（可能已过期或暂时不可用）。\n"
            "请检查节点，在恢复可用后重跑同一条命令；如果要更换节点，请加 --reconfigure 重新粘贴链接。"
        )
    for env_key, probe_key in (
        ("EXPECTED_EGRESS_IP", "ip"),
        ("EXPECTED_EGRESS_LOC", "country"),
        ("EXPECTED_EGRESS_ASN", "asn"),
        ("TIMEZONE", "timezone"),
    ):
        expected = (env.get(env_key) or "").strip()
        actual = (probed.get(probe_key) or "").strip()
        if expected and actual != expected:
            raise StepError(
                "这个节点的出口和上次记录不一致。\n"
                "请先核对你的固定出口；如果要更换节点，请加 --reconfigure 重新粘贴链接。"
            )
    print("  保存的节点连通正常，出口与记录一致，开始重建...")


def _clear_stale_guest_trust(ctx):
    """清理与旧客机绑定的信任与地址文件。"""
    for path in (ctx.known_hosts, ctx.ip_file, ctx.hostkey_log, ctx.logs / "hostkey.trust"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _saved_record_date(ctx):
    """提取旧安装记录的创建日期（YYYY-MM-DD），兼容未记录 created_at 的旧版 state.json。"""
    for key in ("created_at", "updated_at"):
        val = ctx.state.get(key)
        if isinstance(val, str) and re.match(r"^\d{4}-\d{2}-\d{2}", val):
            return val[:10]
    try:
        if ctx.env_path.is_file():
            mtime = ctx.env_path.stat().st_mtime
            return datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        pass
    return ""


def _reset_for_rebuild(ctx, *, keep_config=True):
    """为确认双失后的自动重建重置过期信任记录与完成步骤，保留 instance.env、SSH 密钥和共享设置。"""
    _clear_stale_guest_trust(ctx)
    for key in ("tart_vm_identity", "tart_vm_mac", "tart_snapshot_identity", "tart_snapshot_mac", "failed_step"):
        ctx.state.pop(key, None)
    ctx.state["vm_created_by_up"] = False
    ctx.state["completed_steps"] = ["up:config"] if (keep_config and ctx.env_path.is_file()) else []
    ctx.save_state()


def _recover_from_tart_snapshot(ctx, snap_name):
    """Mac 仅主 VM 丢失、干净快照归属可核验时，从快照克隆回主 VM，再经 boot + verify(--fail-closed) 复验交付。"""
    print(f"旧虚拟机已找不到。正在用已核对的干净快照重建 {ctx.tag}。")
    print("旧虚拟机里的文件和登录状态找不回来，共享文件夹里的文件不受影响。")
    _verify_tart_ownership(snap_name, ctx.state, "tart_snapshot_identity", "tart_snapshot_mac", ctx.tart_bin)
    ctx.tart("clone-new", snap_name, ctx.vm_name)
    updates = dict(
        vm_created_by_up=True,
        vm_name=ctx.vm_name,
        vm_backend="tart",
        clean_snapshot=snap_name,
        tart_vm_identity=tart_vm_identity(ctx.vm_name, ctx.tart_bin),
    )
    if ctx.tart_bin:
        updates["tart_bin"] = ctx.tart_bin
    vm_mac = _tart_vm_mac(ctx.vm_name, ctx.tart_bin)
    if vm_mac:
        updates["tart_vm_mac"] = vm_mac
    ctx.state.update(updates)
    ctx.state.pop("failed_step", None)
    ctx.state["completed_steps"] = ["up:config", "up:preflight", "up:seed", "up:download", "up:import", "up:firstboot"]
    ctx.save_state()


def cmd_up(args):
    tag = (args.tag or "cc01").strip().lower()
    if not SAFE_TAG_RE.fullmatch(tag):
        sys.exit("错误：--tag 仅支持小写字母开头的 2-16 位字母/数字/下划线/短横线（如 cc01）")

    route = detect_route() if args.route == "auto" else args.route
    private_dir = Path(args.private_dir).expanduser() if args.private_dir else (default_private_root() / tag)
    if _is_inside_repo(private_dir) or (args.cache_dir and _is_inside_repo(Path(args.cache_dir).expanduser())):
        sys.exit("错误：私有目录/缓存目录不能放在 cchouse 仓库内，请改用用户私有目录")

    try:
        ctx = UpContext(args, tag, route, private_dir)
    except StateCorruptError as exc:
        sys.exit(str(exc))

    if (ctx.state.get("tag", tag) != tag or ctx.state.get("route", route) != route
            or Path(ctx.state.get("private_dir", private_dir)).resolve() != private_dir.resolve()):
        sys.exit("错误：现有安装记录与当前标签、路线或私有目录不匹配，请使用新的 --tag")
    previous_backend = ctx.state.get("vm_backend")
    if previous_backend and previous_backend != ctx.backend:
        sys.exit("错误：该实例的安装记录属于另一个后端，请使用新的 --tag 创建实例")
    # 早期安装记录没有 vm_backend，但会在私有配置记录旧版虚拟机路径。
    if ctx.route == "virtualbox-linux" and ctx.env_path.is_file() and _env_raw_value(ctx.env_path, "VMX_PATH"):
        sys.exit("错误：该实例包含旧版虚拟机路径，请使用新的 --tag 创建 VirtualBox 实例")

    if _needs_interactive_node_input(ctx) and not _stdin_is_tty():
        print(f"\n{_need_tty_message(_home_install_cmd(ctx))}")
        return EXIT_NEED_TTY

    share_arg = getattr(args, "share", None)
    share_ro = bool(getattr(args, "read_only", False))
    try:
        if share_arg:
            validated_share = share_mod.validate_share_path(share_arg, allow_create=False)
            ctx.state["share"] = share_mod.make_share_state(validated_share, read_only=share_ro, enabled=True)
        elif "share" in ctx.state:
            existing_share = share_mod.normalize_share_state(ctx.state)
            if existing_share["enabled"]:
                validated_share = share_mod.validate_share_path(existing_share["host_path"], allow_create=True)
                ctx.state["share"] = share_mod.make_share_state(
                    validated_share,
                    read_only=(share_ro or existing_share["read_only"]),
                    enabled=True,
                )
        else:
            default_dir = share_mod.default_share_dir(tag)
            validated_share = share_mod.validate_share_path(default_dir, allow_create=True)
            ctx.state["share"] = share_mod.make_share_state(validated_share, read_only=share_ro, enabled=True)
    except share_mod.SharePathError as exc:
        sys.exit(f"错误：{exc}")

    live_run = not args.dry_run and not args.config_only
    if live_run and ctx.backend != "manual":
        has_prior_record = bool(
            ctx.state.get("vm_created_by_up")
            or ctx.done("import")
            or ctx.done("snapshot")
            or ctx.state.get("vm_name")
        )
        cfg_file = None
        if ctx.backend == "tart":
            snap_name = ctx.state.get("clean_snapshot") or f"{ctx.vm_name}-clean"
            require_home = has_prior_record or bool(ctx.state.get("tart_bin"))
            inv, inv_err = _probe_tart_inventory(ctx.tart_bin, require_home_dir=require_home)
            vm_probe, vm_err, _ = _probe_tart_target(
                ctx.vm_name, ctx.tart_bin, inventory=inv, inv_error=inv_err, require_home_dir=require_home,
            )
            snap_probe, snap_err, _ = _probe_tart_target(
                snap_name, ctx.tart_bin, inventory=inv, inv_error=inv_err, require_home_dir=require_home,
            )
        elif ctx.backend == "virtualbox":
            snap_name = ctx.state.get("clean_snapshot") or CLEAN_SNAPSHOT
            vb = _probe_vbox_status(
                ctx.vm_name, snap_name, ctx.private_dir, allow_missing_vboxmanage=(not has_prior_record),
            )
            vm_probe, vm_err, cfg_file = vb["vm"], vb["reason"], vb["cfg_file"]
            snap_probe, snap_err = vb["snapshot"], vb["reason"]
        else:
            snap_name = CLEAN_SNAPSHOT
            vm_probe, vm_err = PROBE_ABSENT, None
            snap_probe, snap_err = PROBE_ABSENT, None

        # 任一对象「查不了」→ 停，不动任何东西
        if vm_probe == PROBE_UNKNOWN or snap_probe == PROBE_UNKNOWN:
            sys.exit(_unknown_backend_msg(ctx.backend, vm_err or snap_err))

        if vm_probe == PROBE_PRESENT:
            owned = (
                ctx.state.get("vm_created_by_up") is True
                and ctx.state.get("vm_name") == ctx.vm_name
                and previous_backend == ctx.backend
            )
            if not owned:
                sys.exit(_conflict_object_msg(ctx.vm_name, ctx.backend))
            if ctx.backend == "tart":
                try:
                    _verify_tart_ownership(ctx.vm_name, ctx.state, "tart_vm_identity", "tart_vm_mac", ctx.tart_bin)
                except StepError as exc:
                    sys.exit(str(exc))
                if snap_probe == PROBE_PRESENT and not ctx.done("snapshot"):
                    try:
                        _verify_tart_ownership(snap_name, ctx.state, "tart_snapshot_identity", "tart_snapshot_mac", ctx.tart_bin)
                    except StepError as exc:
                        sys.exit(str(exc))
            elif ctx.backend == "virtualbox":
                if not _vbox_cfg_matches(cfg_file, ctx.private_dir, ctx.vm_name):
                    sys.exit(_conflict_object_msg(ctx.vm_name, ctx.backend))
            print(f"检测到 {ctx.vm_name} 的安装记录，将沿用已完成步骤。")
        elif snap_probe == PROBE_PRESENT:
            # Mac：VM 没了，但 <vm>-clean 还在
            if not has_prior_record:
                sys.exit(_conflict_object_msg(snap_name, ctx.backend))
            if ctx.backend == "tart":
                try:
                    _verify_tart_ownership(snap_name, ctx.state, "tart_snapshot_identity", "tart_snapshot_mac", ctx.tart_bin)
                except StepError as exc:
                    sys.exit(str(exc))
                if not ctx.env_path.is_file() or not ctx.ssh_key.is_file():
                    sys.exit(
                        f"找到干净快照 {snap_name}，但缺少节点配置或 SSH 密钥。"
                        "请先在 Tart 中核对原快照，或用 --tag 换一个实例名重新安装。"
                    )
                cur_share = share_mod.normalize_share_state(ctx.state)
                if cur_share["enabled"] and not share_arg:
                    share_mod.ensure_share_dir(cur_share["host_path"])
                try:
                    _recover_from_tart_snapshot(ctx, snap_name)
                except StepError as exc:
                    sys.exit(str(exc))
            else:
                sys.exit(_conflict_object_msg(snap_name, ctx.backend))
        elif has_prior_record:
            # VM 与干净快照均确认不存在（含旧版无归属字段记录）→ 自动重建，不再询问
            if ctx.env_path.is_file() and not ctx.args.reconfigure:
                created_date = _saved_record_date(ctx)
                date_hint = f"（旧记录创建日期：{created_date}）" if created_date else ""
                print(f"之前的虚拟机和干净快照已找不到。正在用保存的节点设置重新建一台 {ctx.tag}{date_hint}。")
                print("如果这不是你装的，或想换节点，加 --reconfigure 重新输入。")
                print("旧虚拟机里的文件和登录状态找不回来，共享文件夹里的文件不受影响。")
                try:
                    _verify_saved_egress(ctx)
                except StepError as exc:
                    sys.exit(str(exc))
                cur_share = share_mod.normalize_share_state(ctx.state)
                if cur_share["enabled"] and not share_arg:
                    share_mod.ensure_share_dir(cur_share["host_path"])
                _reset_for_rebuild(ctx, keep_config=True)
            else:
                if ctx.args.reconfigure:
                    print(f"之前的虚拟机和干净快照已找不到，将按 --reconfigure 重新输入节点链接并重建 {ctx.tag}。")
                else:
                    print(f"之前的虚拟机已找不到，且缺少节点设置。请按提示粘贴节点链接重新安装 {ctx.tag}。")
                print("旧虚拟机里的文件和登录状态找不回来，共享文件夹里的文件不受影响。")
                cur_share = share_mod.normalize_share_state(ctx.state)
                if cur_share["enabled"] and not share_arg:
                    share_mod.ensure_share_dir(cur_share["host_path"])
                _reset_for_rebuild(ctx, keep_config=False)

    if live_run:
        cur_share = share_mod.normalize_share_state(ctx.state)
        if cur_share["enabled"] and not share_arg:
            share_mod.ensure_share_dir(cur_share["host_path"])
    return run_pipeline(ctx)


def _resolve_up_tart_bin(state=None):
    """安装阶段解析实际使用的 tart 绝对路径，供 state.json 固定。"""
    if state and state.get("tart_bin"):
        return str(state["tart_bin"])
    override = os.environ.get("CCHOUSE_TART_BIN", "").strip()
    if override and os.path.isfile(override):
        return os.path.abspath(override)
    found = shutil.which("tart") or preflight_mod.find_tart()
    return os.path.abspath(found) if found else None


def _tart_bin(state=None, *, announce_legacy=False):
    if state and state.get("tart_bin"):
        return str(state["tart_bin"])
    override = os.environ.get("CCHOUSE_TART_BIN", "").strip()
    if override:
        chosen = override
        shown = override
    else:
        which_tart = shutil.which("tart")
        if which_tart:
            chosen = "tart"
            shown = which_tart
        else:
            found = preflight_mod.find_tart()
            chosen = found or "tart"
            shown = chosen
    if announce_legacy:
        print(f"[cchouse] 旧安装记录未固定 tart 路径，当前使用：{shown}", file=sys.stderr)
    return chosen


def _tart_env(tart_bin=None, share_cfg=None):
    env = os.environ.copy()
    if tart_bin and (os.path.isabs(str(tart_bin)) or os.sep in str(tart_bin)):
        env["CCHOUSE_TART_BIN"] = str(tart_bin)
    else:
        env.pop("CCHOUSE_TART_BIN", None)
    if share_cfg and share_cfg.get("enabled") and share_cfg.get("host_path"):
        env["CCHOUSE_SHARE_PATH"] = str(share_cfg["host_path"])
        env["CCHOUSE_SHARE_RO"] = "1" if share_cfg.get("read_only") else "0"
    else:
        env.pop("CCHOUSE_SHARE_PATH", None)
        env.pop("CCHOUSE_SHARE_RO", None)
    return env


def _vbox_share_kwargs(state):
    cfg = share_mod.normalize_share_state(state)
    if cfg["enabled"]:
        return {
            "ShareEnabled": "yes",
            "SharePath": cfg["host_path"],
            "ShareReadOnly": "yes" if cfg["read_only"] else "no",
        }
    return {
        "ShareEnabled": "no",
        "SharePath": "",
        "ShareReadOnly": "no",
    }


def _tart_inventory(tart_bin=None):
    exe = tart_bin or _tart_bin()
    proc = subprocess.run(
        [str(exe), "list", "--source", "local", "--format", "json"],
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


def _remove_path(path):
    """用同一种 Windows 路径表示比较归属，并避免把扩展路径传给 PowerShell 路径提供程序。"""
    if os.name == "nt":
        path = os.fspath(path).replace("/", "\\")
        if path[:8].lower() == "\\\\?\\unc\\":
            path = "\\\\" + path[8:]
        elif re.match(r"^\\\\\?\\[a-zA-Z]:\\", path):
            path = path[4:]
    return Path(path)


def _virtualbox_remove_plan(vm_name, snapshot, private_dir, inventory):
    if not isinstance(vm_name, str) or not re.fullmatch(r"claude-[a-z][a-z0-9_-]{1,15}", vm_name):
        raise StepError("state.json 的 VirtualBox 虚拟机名无法精确匹配")
    vm_dir = private_dir / "vm"
    config = vm_dir / vm_name / f"{vm_name}.vbox"
    if _remove_path(config.resolve()) != config:
        raise StepError("VirtualBox 配置或其上级目录为链接，需手动核对")
    label = f"虚拟机 {vm_name}（先关机，再 VBoxManage unregistervm --delete"
    if snapshot:
        label += f"，含快照 {snapshot}"
    return [(f"{label}） {config}", [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "scripts" / "vm-virtualbox.ps1"),
        "-CcVmDir", str(vm_dir), "-CcOp", "delete", "-CcVmName", vm_name,
    ])]


# 新后端在这里注册。未注册的后端保留原实例，以便在对应软件中手动处理。
REMOVE_BACKENDS = {
    "tart": _tart_remove_plan,
    "virtualbox": _virtualbox_remove_plan,
}


def _remove_state(private_dir, root, tag):
    if _remove_path(private_dir.resolve()) != private_dir:
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
    recorded_dir = state.get("private_dir", str(private_dir))
    if state.get("tag", tag) != tag or not isinstance(recorded_dir, str) or _remove_path(recorded_dir) != private_dir:
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
        if _remove_path(dest.parent.resolve()) != dest.parent:
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

    root = _remove_path(default_private_root().resolve())
    requested = _remove_path(args.private_dir).expanduser() if args.private_dir else root / tag
    private_dir = _remove_path(requested.parent.resolve()) / requested.name
    vm_name = f"claude-{tag}"
    cache_dir = root / "cache"
    golden_name = None
    try:
        repo = _remove_path(ROOT.resolve())
        if private_dir in (root, cache_dir, repo) or repo in private_dir.parents or private_dir in repo.parents:
            raise StepError("私有目录是共享目录或仓库路径")
        state = _remove_state(private_dir, root, tag)
        if not state:
            print(f"没有找到 {tag} 的安装记录。")
            _print_remove_remainder(cache_dir, golden_name, False)
            return 0
        share_cfg = share_mod.normalize_share_state(state)

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
        allowed_vm_dirs = {vm_name} if owned and backend == "virtualbox" else set()
        if _remove_path(vm_dir.resolve()) != vm_dir or (vm_dir.exists() and {p.name for p in vm_dir.iterdir()} - allowed_vm_dirs):
            raise StepError("私有目录的 vm/ 下有无法确认归属的项目")

        if state.get("cache_dir"):
            cache_dir = _remove_path(state["cache_dir"])
        if not cache_dir.is_absolute() or _remove_path(cache_dir.resolve()) != cache_dir:
            raise StepError("记录的缓存目录不是可验证的绝对路径")
        if cache_dir == private_dir or private_dir in cache_dir.parents:
            raise StepError("缓存位于实例私有目录中，请先手动移出缓存")

        manifest = json.loads((ROOT / "guest-linux" / "image-manifest.json").read_text(encoding="utf-8"))
        tart_bin = None
        if backend == "tart":
            tart_bin = _tart_bin(state, announce_legacy=not state.get("tart_bin"))
            golden_name = state.get("golden_name", f"cchouse-debian13-arm64-{manifest['image_version']}")
            if not isinstance(golden_name, str) or not re.fullmatch(r"cchouse-debian13-arm64-[a-zA-Z0-9._-]+", golden_name):
                raise StepError("state.json 的 Tart 黄金镜像名无效")
            inventory = _tart_inventory(tart_bin)
            if owned:
                for name, id_key, mac_key in (
                    (vm_name, "tart_vm_identity", "tart_vm_mac"),
                    (snapshot, "tart_snapshot_identity", "tart_snapshot_mac"),
                ):
                    if name in inventory:
                        _verify_tart_ownership(name, state, id_key, mac_key, tart_bin)
        else:
            inventory = {}

        vm_actions = REMOVE_BACKENDS[backend](vm_name, snapshot, private_dir, inventory) if owned else []
        launcher_items = launcher_mod.preview(tag, state)
        golden = inventory.get(golden_name)
        if args.purge_cache and golden:
            if golden.get("Running"):
                raise StepError("Tart 黄金镜像正在运行，请先在 Tart 中关闭它")
        cache_items = _download_cache_items(cache_dir, manifest) if args.purge_cache else []

        print("将删除：")
        for label, _cmd in vm_actions:
            print(f"  - {label}")
        for lpath in launcher_items:
            print(f"  - 启动图标 {lpath}")
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
        if share_cfg["enabled"]:
            print(f"共享文件夹 {share_cfg['host_path']} 将保留，不会被删除。")
        if not args.purge_cache:
            if backend == "tart":
                print("Tart 黄金镜像和共享下载缓存默认保留。")
            else:
                print("黄金镜像和共享下载缓存默认保留。")
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
        if backend == "tart" and owned:
            for name, id_key, mac_key in (
                (vm_name, "tart_vm_identity", "tart_vm_mac"),
                (snapshot, "tart_snapshot_identity", "tart_snapshot_mac"),
            ):
                if name in inventory:
                    _verify_tart_ownership(name, state, id_key, mac_key, tart_bin)

        for _label, cmd in vm_actions:
            run_kwargs = {"check": True}
            if backend == "tart":
                run_kwargs["env"] = _tart_env(tart_bin)
            subprocess.run(cmd, **run_kwargs)
        if launcher_items:
            launcher_mod.remove(tag, state)
        if args.purge_cache:
            if golden:
                subprocess.run([str(tart_bin), "delete", golden_name], check=True)
            for path, _size in cache_items:
                if _remove_path(path.parent.resolve()) != path.parent:
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
    if share_cfg["enabled"]:
        print(f"共享文件夹 {share_cfg['host_path']} 已保留")
    _print_remove_remainder(cache_dir, golden_name, args.purge_cache, tart_bin=tart_bin)
    return 0


def _print_remove_remainder(cache_dir, golden_name, purged, tart_bin=None):
    if purged:
        print("已处理清单中的共享缓存项，未识别的缓存文件可在文件管理器中核对。")
    else:
        print("保留共享黄金镜像和下载缓存。确认其他实例无需使用后，可按下面的路径手动清理。")
    print(f"手动清理下载缓存：在文件管理器打开 {cache_dir}，核对后删除不需要的安装包。")
    if golden_name and not purged:
        if tart_bin and (os.path.isabs(str(tart_bin)) or os.sep in str(tart_bin)):
            tart_cmd = f'"{tart_bin}"'
        else:
            tart_cmd = "tart"
        print(f'手动清理 Tart 黄金镜像：确认无需使用后运行 {tart_cmd} delete "{golden_name}"，或卸载时加 --purge-cache。')
    print("安装器仓库和宿主虚拟机软件仍保留，可分别在文件管理器和系统软件管理界面删除。")


def _load_instance_context(tag, private_dir_arg=None):
    """读取已有实例的私有目录、state.json、backend 与 vm_name，供 open/stop/status/restore 共用。"""
    tag = (tag or "cc01").strip().lower()
    if not SAFE_TAG_RE.fullmatch(tag):
        raise StepError("错误：--tag 仅支持小写字母开头的 2-16 位字母/数字/下划线/短横线（如 cc01）", 2)
    root = _remove_path(default_private_root().resolve())
    requested = _remove_path(private_dir_arg).expanduser() if private_dir_arg else root / tag
    private_dir = _remove_path(requested.parent.resolve()) / requested.name
    state = _remove_state(private_dir, root, tag)
    vm_name = state.get("vm_name") or f"claude-{tag}"
    backend = state.get("vm_backend")
    if not backend:
        route = state.get("route") or detect_route()
        backend = detect_vm_backend(route)
    tart_bin = _tart_bin(state, announce_legacy=bool(state and not state.get("tart_bin"))) if backend == "tart" else None
    return {
        "tag": tag,
        "root": root,
        "private_dir": private_dir,
        "state_path": private_dir / "state.json",
        "env_path": private_dir / "instance.env",
        "ssh_key": private_dir / "id_ed25519",
        "known_hosts": private_dir / "known_hosts",
        "logs": private_dir / "logs",
        "state": state,
        "vm_name": vm_name,
        "backend": backend,
        "tart_bin": tart_bin,
        "snapshot": state.get("clean_snapshot") or (f"{vm_name}-clean" if backend == "tart" else CLEAN_SNAPSHOT),
    }


def _save_state_dict(state_path, state):
    state["updated_at"] = _now_iso()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name("state.json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, state_path)


def _virtualbox_cmd(op, **kw):
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "scripts" / "vm-virtualbox.ps1"), "-CcOp", op,
    ]
    for k, v in kw.items():
        cmd += [f"-Cc{k}", str(v)]
    return cmd


def _tart_cmd(*args):
    return ["bash", str(ROOT / "scripts" / "vm-tart.sh"), *[str(a) for a in args]]


def _backend_status(info):
    """查询后端虚拟机与干净快照状态，返回 (vm_state, snapshot_exists)。

    vm_state: 'running' | 'running-headless' | 'stopped' | 'missing' | 'unknown'
    snapshot_exists: True | False | None（None 表示无法查询）
    """
    backend = info["backend"]
    vm_name = info["vm_name"]
    snapshot = info["snapshot"]
    if backend == "tart":
        inv, inv_err = _probe_tart_inventory(info.get("tart_bin"), require_home_dir=True)
        if inv_err:
            return "unknown", None
        v_probe, _v_err, v_meta = _probe_tart_target(
            vm_name, info.get("tart_bin"), inventory=inv, inv_error=inv_err, require_home_dir=True,
        )
        if v_probe == PROBE_PRESENT:
            running = bool(v_meta and (v_meta.get("Running") is True or str(v_meta.get("State", "")).lower() == "running"))
            vm_state = "running" if running else "stopped"
        elif v_probe == PROBE_ABSENT:
            vm_state = "missing"
        else:
            vm_state = "unknown"
        s_probe, _s_err, _s_meta = _probe_tart_target(
            snapshot, info.get("tart_bin"), inventory=inv, inv_error=inv_err, require_home_dir=True,
        )
        snap_exists = True if s_probe == PROBE_PRESENT else (False if s_probe == PROBE_ABSENT else None)
        return vm_state, snap_exists
    if backend == "virtualbox":
        vb = _probe_vbox_status(vm_name, snapshot, info.get("private_dir"), allow_missing_vboxmanage=False)
        snap_exists = (
            True if vb["snapshot"] == PROBE_PRESENT
            else (False if vb["snapshot"] == PROBE_ABSENT else None)
        )
        return vb["vm_state"], snap_exists
    raise StepError(f"当前后端 {backend} 暂不支持该操作")


def _sync_tart_ip_and_known_hosts(info):
    """Tart 重启或 restore 后 DHCP IP 可能变化；复用既有 known_hosts 中已固定的主机公钥映射到新 IP。"""
    if info["backend"] != "tart":
        return _env_raw_value(info["env_path"], "GUEST_IP") if info["env_path"].is_file() else None
    tart_bin = info.get("tart_bin") or _tart_bin(info.get("state"))
    ip = ""
    for ip_cmd in (
        [str(tart_bin), "ip", info["vm_name"], "--wait", "2"],
        [str(tart_bin), "ip", "--resolver=arp", info["vm_name"], "--wait", "2"],
    ):
        try:
            proc = subprocess.run(ip_cmd, capture_output=True, text=True, timeout=10)
            cand = (proc.stdout or "").strip().splitlines()[0].strip() if proc.stdout and proc.stdout.strip() else ""
            if render_mod.is_ipv4(cand):
                ip = cand
                break
        except (OSError, subprocess.SubprocessError):
            continue
    if not render_mod.is_ipv4(ip):
        return _env_raw_value(info["env_path"], "GUEST_IP") if info["env_path"].is_file() else None
    if info["env_path"].is_file() and _env_raw_value(info["env_path"], "GUEST_IP") != ip:
        update_env_value(info["env_path"], "GUEST_IP", ip)
    kh = info["known_hosts"]
    if kh.is_file():
        lines = kh.read_text(encoding="utf-8").splitlines()
        if not any(line.startswith(f"{ip} ") for line in lines):
            for line in lines:
                parts = line.split()
                if len(parts) >= 3 and parts[1].startswith("ssh-"):
                    lines.append(f"{ip} {parts[1]} {parts[2]}")
                    kh.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    if os.name != "nt":
                        os.chmod(kh, 0o600)
                    break
    return ip


def _ssh_graceful_poweroff(info):
    """经 SSH 执行 sync + sudo -n /usr/bin/systemctl poweroff。返回 True 表示已发送关机命令。"""
    ssh_key = info["ssh_key"]
    env_path = info["env_path"]
    if not ssh_key.is_file() or not env_path.is_file():
        return False
    ip = _sync_tart_ip_and_known_hosts(info) if info["backend"] == "tart" else _env_raw_value(env_path, "GUEST_IP")
    if not ip:
        return False
    port = _env_raw_value(env_path, "SSH_PORT") if info["backend"] == "virtualbox" else None
    kh = info["known_hosts"]
    ssh_bin = "ssh"
    if os.name == "nt":
        sys32_ssh = Path(os.environ.get("WINDIR") or r"C:\Windows") / "System32" / "OpenSSH" / "ssh.exe"
        if sys32_ssh.is_file():
            ssh_bin = str(sys32_ssh)
    cmd = [ssh_bin, "-F", "none", "-i", str(ssh_key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if kh.is_file():
        kh_opt = f'UserKnownHostsFile="{kh.resolve().as_posix()}"' if os.name == "nt" else f'UserKnownHostsFile="{kh}"'
        cmd += ["-o", "StrictHostKeyChecking=yes", "-o", kh_opt]
    else:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    if port:
        cmd += ["-p", str(port)]
    cmd += [f"claude@{ip}", "printf 'CCHOUSE_POWEROFF_SENT\\n'; sync; sudo -n /usr/bin/systemctl poweroff"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        # systemctl poweroff 会断开 SSH 连接（退出码 0 或 255），以回显标记确认确实已连上并触发关机
        return proc.returncode in (0, 255) and "CCHOUSE_POWEROFF_SENT" in (proc.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _wait_stopped(info, timeout):
    deadline = time.monotonic() + max(1, int(timeout))
    while time.monotonic() < deadline:
        vm_state, _ = _backend_status(info)
        if vm_state in ("stopped", "missing"):
            return True
        time.sleep(2)
    vm_state, _ = _backend_status(info)
    return vm_state in ("stopped", "missing")


def cmd_status(args):
    try:
        info = _load_instance_context(args.tag, getattr(args, "private_dir", None))
        if not info["state"]:
            vm_state, snap_exists = "missing", False
        else:
            vm_state, snap_exists = _backend_status(info)
    except StepError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    label = {
        "running": "运行中",
        "running-headless": "运行中（无窗口）",
        "stopped": "已关机",
        "missing": "不存在",
        "unknown": "无法查询",
    }.get(vm_state, vm_state)
    snap_label = (
        f"存在（{info['snapshot']}）" if snap_exists is True
        else ("无法查询" if snap_exists is None else "不存在")
    )
    share_cfg = share_mod.normalize_share_state(info["state"])
    if getattr(args, "json", False):
        print(json.dumps({
            "tag": info["tag"],
            "vm_name": info["vm_name"],
            "backend": info["backend"],
            "state": vm_state,
            "snapshot": info["snapshot"],
            "snapshot_exists": snap_exists,
            "share": share_cfg,
        }, ensure_ascii=False))
    else:
        print(f"实例：{info['tag']}（{info['vm_name']}）")
        print(f"状态：{label}")
        print(f"干净快照：{snap_label}")
        print(share_mod.format_share_status_line(info["state"]))
        if vm_state == "missing" and info["state"]:
            print(f"提示：虚拟机已不存在，重跑首页安装命令即可用保存的设置重建（或运行 cchouse up --tag {info['tag']}）。")
        elif vm_state == "unknown":
            print("提示：目前无法确认原虚拟机在哪里。请先接回原磁盘，或在 Tart / VirtualBox 中确认原虚拟机可访问后重试。")
    return 0


def cmd_open(args):
    try:
        info = _load_instance_context(args.tag, getattr(args, "private_dir", None))
        if not info["state"]:
            raise StepError(f"没有找到 {info['tag']} 的安装记录，请先运行 cchouse up --tag {info['tag']}。")
        vm_state, _ = _backend_status(info)
        if vm_state == "unknown":
            raise StepError("目前无法确认原虚拟机在哪里。请先接回原磁盘，或在 Tart / VirtualBox 中确认原虚拟机可访问后重试。")
        if vm_state == "missing":
            raise StepError(
                f"虚拟机 {info['vm_name']} 已不存在，重跑首页安装命令即可用保存的设置重建"
                f"（或运行 cchouse up --tag {info['tag']}）。"
            )
        share_cfg = share_mod.normalize_share_state(info["state"])
        if info["backend"] == "tart":
            info["logs"].mkdir(parents=True, exist_ok=True)
            subprocess.run(
                _tart_cmd("open", info["vm_name"], info["logs"] / "tart-gui.log"),
                check=True, env=_tart_env(info.get("tart_bin"), share_cfg),
            )
            print("提示：关机请用桌面右键 Safe Shutdown 或 cchouse stop")
        elif info["backend"] == "virtualbox":
            subprocess.run(_virtualbox_cmd("gui", VmName=info["vm_name"], **_vbox_share_kwargs(info["state"])), check=True)
        else:
            raise StepError(f"当前后端 {info['backend']} 暂不支持 open")
    except (StepError, subprocess.CalledProcessError, OSError) as exc:
        print(f"打开失败：{exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, StepError) else 1
    return 0


def cmd_stop(args):
    try:
        info = _load_instance_context(args.tag, getattr(args, "private_dir", None))
        if not info["state"]:
            raise StepError(f"没有找到 {info['tag']} 的安装记录。")
        vm_state, _ = _backend_status(info)
        if vm_state == "unknown":
            raise StepError("目前无法确认原虚拟机状态，已停止关机。请先接回原磁盘或在 Tart / VirtualBox 中核对。")
        if vm_state == "missing":
            raise StepError(f"虚拟机 {info['vm_name']} 不存在。")
        if vm_state == "stopped":
            print(f"客机 {info['vm_name']} 已处于关机状态。")
            return 0
        force = bool(getattr(args, "force", False))
        timeout = int(getattr(args, "timeout", 60))
        if not force:
            if _ssh_graceful_poweroff(info):
                print(f"已向 {info['vm_name']} 发送安全关机命令，等待停机（最多 {timeout}s）...", flush=True)
                if _wait_stopped(info, timeout):
                    print(f"客机 {info['vm_name']} 已安全关机。")
                    return 0
            if info["backend"] == "virtualbox":
                subprocess.run(
                    _virtualbox_cmd("stop", VmName=info["vm_name"], Force="no", Timeout=str(timeout)),
                    check=True,
                )
                print(f"客机 {info['vm_name']} 已安全关机。")
                return 0
            raise StepError(
                f"无法通过 SSH 安全关闭 {info['vm_name']}（或等待超过 {timeout}s）。"
                "请在客机桌面右键选择 Power Off (Safe Shutdown)，或加 --force 强制停机。"
            )
        if info["backend"] == "tart":
            subprocess.run(_tart_cmd("stop", info["vm_name"]), check=True, env=_tart_env(info.get("tart_bin")))
        elif info["backend"] == "virtualbox":
            subprocess.run(
                _virtualbox_cmd("stop", VmName=info["vm_name"], Force="yes", Timeout=str(timeout)),
                check=True,
            )
        else:
            raise StepError(f"当前后端 {info['backend']} 暂不支持 stop")
        print(f"已强制停止客机 {info['vm_name']}。")
    except (StepError, subprocess.CalledProcessError, OSError) as exc:
        print(f"关机失败：{exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, StepError) else 1
    return 0


def cmd_restore(args):
    try:
        info = _load_instance_context(args.tag, getattr(args, "private_dir", None))
        if not info["state"]:
            raise StepError(f"没有找到 {info['tag']} 的安装记录。")
        vm_state, snap_exists = _backend_status(info)
        if vm_state == "unknown" or snap_exists is None:
            raise StepError("目前无法确认原虚拟机或干净快照状态，已停止恢复。请先接回原磁盘或在 Tart / VirtualBox 中核对。")
        if not snap_exists:
            raise StepError(f"找不到干净快照 {info['snapshot']}，无法恢复。")
        if info["backend"] == "tart":
            tart_bin = info.get("tart_bin")
            if vm_state != "missing":
                _verify_tart_ownership(info["vm_name"], info["state"], "tart_vm_identity", "tart_vm_mac", tart_bin)
            _verify_tart_ownership(info["snapshot"], info["state"], "tart_snapshot_identity", "tart_snapshot_mac", tart_bin)
        if not getattr(args, "yes", False):
            try:
                ok = input(
                    f"将把 {info['vm_name']} 恢复到干净快照 {info['snapshot']}（客机内未保存的文件和登录状态会清空，"
                    "节点配置 instance.env 与 SSH 密钥保留）。确认？ [y/N] "
                ).strip().lower() in ("y", "yes")
            except EOFError:
                ok = False
            if not ok:
                print("已取消恢复。")
                return 0
        if vm_state in ("running", "running-headless"):
            stop_ns = argparse.Namespace(tag=info["tag"], private_dir=str(info["private_dir"]), force=False, timeout=60)
            rc = cmd_stop(stop_ns)
            if rc != 0:
                return rc
        if info["backend"] == "tart":
            tart_bin = info.get("tart_bin")
            if vm_state != "missing":
                _verify_tart_ownership(info["vm_name"], info["state"], "tart_vm_identity", "tart_vm_mac", tart_bin)
            _verify_tart_ownership(info["snapshot"], info["state"], "tart_snapshot_identity", "tart_snapshot_mac", tart_bin)
            subprocess.run(
                _tart_cmd("restore", info["vm_name"], info["snapshot"]),
                check=True, env=_tart_env(tart_bin),
            )
            info["state"]["tart_vm_identity"] = tart_vm_identity(info["vm_name"], tart_bin)
            new_mac = _tart_vm_mac(info["vm_name"], tart_bin)
            if new_mac:
                info["state"]["tart_vm_mac"] = new_mac
            _save_state_dict(info["state_path"], info["state"])
        elif info["backend"] == "virtualbox":
            subprocess.run(
                _virtualbox_cmd("restore", VmName=info["vm_name"], Snapshot=info["snapshot"], **_vbox_share_kwargs(info["state"])),
                check=True,
            )
            _save_state_dict(info["state_path"], info["state"])
        else:
            raise StepError(f"当前后端 {info['backend']} 暂不支持 restore")
        share_cfg = share_mod.normalize_share_state(info["state"])
        share_note = "、共享文件夹设置" if share_cfg["enabled"] else ""
        print(f"已将 {info['vm_name']} 恢复到干净快照 {info['snapshot']}（instance.env、SSH 密钥{share_note}已保留）。")
    except (StepError, subprocess.CalledProcessError, OSError) as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, StepError) else 1
    return 0


def _guest_has_share_service(info):
    """检查运行中客机是否已安装 cchouse-share.service；无法连上时返回 None。"""
    ssh_key = info["ssh_key"]
    env_path = info["env_path"]
    if not ssh_key.is_file() or not env_path.is_file():
        return None
    ip = _sync_tart_ip_and_known_hosts(info) if info["backend"] == "tart" else _env_raw_value(env_path, "GUEST_IP")
    if not ip:
        return None
    port = _env_raw_value(env_path, "SSH_PORT") if info["backend"] == "virtualbox" else None
    kh = info["known_hosts"]
    ssh_bin = "ssh"
    if os.name == "nt":
        sys32_ssh = Path(os.environ.get("WINDIR") or r"C:\Windows") / "System32" / "OpenSSH" / "ssh.exe"
        if sys32_ssh.is_file():
            ssh_bin = str(sys32_ssh)
    cmd = [ssh_bin, "-F", "none", "-i", str(ssh_key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if kh.is_file():
        kh_opt = f'UserKnownHostsFile="{kh.resolve().as_posix()}"' if os.name == "nt" else f'UserKnownHostsFile="{kh}"'
        cmd += ["-o", "StrictHostKeyChecking=yes", "-o", kh_opt]
    else:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    if port:
        cmd += ["-p", str(port)]
    cmd += [f"claude@{ip}", "test -f /etc/systemd/system/cchouse-share.service && echo YES || echo NO"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = (proc.stdout or "").strip()
        if out == "YES":
            return True
        if out == "NO":
            return False
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def cmd_share(args):
    try:
        info = _load_instance_context(args.tag, getattr(args, "private_dir", None))
        if not info["state"]:
            raise StepError(f"没有找到 {info['tag']} 的安装记录，请先运行 cchouse up --tag {info['tag']}。")

        path_arg = getattr(args, "path", None)
        off = bool(getattr(args, "off", False))
        read_only = bool(getattr(args, "read_only", False))

        if off and (path_arg or read_only):
            raise StepError("参数冲突：--off 不能与共享路径或 --read-only 同时使用。", 2)

        # 无参数 = 显示当前设置
        if not off and not path_arg:
            if read_only:
                raise StepError("请提供共享文件夹路径（用法：cchouse share --tag <tag> <路径> [--read-only]）。", 2)
            print(share_mod.format_share_status_line(info["state"]))
            return 0

        if off:
            info["state"]["share"] = share_mod.make_share_state(enabled=False)
            _save_state_dict(info["state_path"], info["state"])
            vm_state, _ = _backend_status(info)
            if vm_state in ("running", "running-headless"):
                print("已关闭共享文件夹。虚拟机正在运行，重启后生效（可运行 cchouse stop 后再 cchouse open）。")
            else:
                if info["backend"] == "virtualbox" and vm_state == "stopped":
                    subprocess.run(
                        _virtualbox_cmd("sync-share", VmName=info["vm_name"], **_vbox_share_kwargs(info["state"])),
                        check=True,
                    )
                print("已关闭共享文件夹，下次开机生效。")
            return 0

        try:
            validated = share_mod.validate_share_path(path_arg, allow_create=False)
        except share_mod.SharePathError as exc:
            raise StepError(str(exc), 2) from None

        info["state"]["share"] = share_mod.make_share_state(validated, read_only=read_only, enabled=True)
        _save_state_dict(info["state_path"], info["state"])
        mode = "只读" if read_only else "读写"
        vm_state, _ = _backend_status(info)
        if vm_state in ("running", "running-headless"):
            has_svc = _guest_has_share_service(info)
            if has_svc is False:
                print(
                    "提示：检测到该实例由旧版 cchouse 安装，客机内缺少共享挂载服务（客机无 root 权限无法在线补装），"
                    "建议保留共享目录后用新版重新安装实例。",
                    file=sys.stderr,
                )
            print(f"共享文件夹已设置为：{validated}（{mode}）。虚拟机正在运行，重启后生效（可运行 cchouse stop 后再 cchouse open）。")
        else:
            if info["backend"] == "virtualbox" and vm_state == "stopped":
                subprocess.run(
                    _virtualbox_cmd("sync-share", VmName=info["vm_name"], **_vbox_share_kwargs(info["state"])),
                    check=True,
                )
            print(f"共享文件夹已设置为：{validated}（{mode}），下次开机生效。")
    except (StepError, subprocess.CalledProcessError, OSError) as exc:
        print(f"设置共享文件夹失败：{exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, StepError) else 1
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="cchouse CLI")
    parser.add_argument("--version", action="version", version=f"cchouse {(ROOT / 'VERSION').read_text(encoding='ascii').strip()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="一条命令：后台下载镜像，同时配置节点，下载完自动导入、首启注入、验收并打干净快照")
    p_up.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_up.add_argument("--route", choices=("auto", "tart-linux", "virtualbox-linux"), default="auto")
    p_up.add_argument("--private-dir", default=None, help="覆盖私有目录路径（不能位于仓库内）")
    p_up.add_argument("--cache-dir", default=None, help="镜像与 Claude Code 包缓存目录（默认 <私有根目录>/cache，多实例共用）")
    p_up.add_argument("--image-mirror", default=None, help=f"黄金镜像镜像源目录 URL（也可用环境变量 {downloads.IMAGE_MIRROR_ENV}），SHA-256 仍按 manifest 校验")
    p_up.add_argument("--npm-mirror", default=None, help=f"Claude Code tarball 镜像源（也可用环境变量 {downloads.NPM_MIRROR_ENV}），integrity 仍取官方 registry")
    p_up.add_argument("--timezone", default=None, help="覆盖自动采用的出口时区")
    p_up.add_argument("--expected-ip", default=None, help="覆盖自动采用的预期出口 IP")
    p_up.add_argument("--expected-country", default=None, help="覆盖自动采用的两位出口国家/地区代码")
    p_up.add_argument("--expected-asn", default=None, help="覆盖自动采用的预期出口 ASN（如 AS64500）")
    p_up.add_argument("--link-clipboard", action="store_true", help="从本机剪贴板读取第一条非空行作为节点链接（不回显，保存后自动清空剪贴板）")
    p_up.add_argument("--link-file", default=None, help="从指定普通文本文件读取第一条非空行作为节点链接（不回显、不自动删除文件）")
    p_up.add_argument("--link-stdin", action="store_true", help="从标准输入读取一行节点链接（不回显，适合脚本/测试）")
    p_up.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    p_up.add_argument("--no-probe", action="store_true", help="跳过宿主探测，手工提供 IP、国家、ASN 和时区")
    p_up.add_argument("--vm-backend", choices=("auto", "tart", "virtualbox", "manual"), default="auto",
                      help="虚拟机自动化后端（默认：Apple Silicon→tart；Windows→virtualbox；其余→manual）")
    p_up.add_argument("--reconfigure", action="store_true", help="忽略已有 instance.env，重新输入节点链接")
    p_up.add_argument("--share", default=None, help="自定义共享给客机 ~/shared 的宿主文件夹路径（默认 ~/cchouse-share/<tag>）")
    p_up.add_argument("--read-only", action="store_true", help="以只读方式共享宿主文件夹给客机")
    p_up.add_argument("--config-only", action="store_true", help="只生成 instance.env 并跑 preflight（第一轮行为），不下载不建虚拟机")
    p_up.add_argument("--skip-preflight", action="store_true", help=argparse.SUPPRESS)
    p_up.add_argument("--dry-run", action="store_true", help="只演练：生成配置并跑 preflight，其余步骤只打印命令，不下载、不动虚拟机、不写 state.json")

    p_remove = sub.add_parser("remove", help="预览并卸载指定实例")
    p_remove.add_argument("--tag", required=True, help="安装时的实例短名")
    p_remove.add_argument("--private-dir", help="安装时使用的自定义私有目录")
    p_remove.add_argument("--yes", action="store_true", help="跳过确认提示")
    p_remove.add_argument("--purge-cache", action="store_true", help="同时清理共享下载缓存和对应的黄金镜像")

    p_open = sub.add_parser("open", help="打开客机桌面窗口（已开则不重复开）")
    p_open.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_open.add_argument("--private-dir", default=None, help="安装时使用的自定义私有目录")

    p_stop = sub.add_parser("stop", help="安全关闭客机（经 SSH poweroff；强停需 --force）")
    p_stop.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_stop.add_argument("--private-dir", default=None, help="安装时使用的自定义私有目录")
    p_stop.add_argument("--force", action="store_true", help="强制停机（可能丢失未落盘数据）")
    p_stop.add_argument("--timeout", type=int, default=60, help="等待安全关机超时秒数（默认 60）")

    p_status = sub.add_parser("status", help="查看客机与干净快照状态")
    p_status.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_status.add_argument("--private-dir", default=None, help="安装时使用的自定义私有目录")
    p_status.add_argument("--json", action="store_true", help="以 JSON 格式输出状态")

    p_restore = sub.add_parser("restore", help="安全停机并恢复到干净快照（保留 instance.env 与 SSH 密钥）")
    p_restore.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_restore.add_argument("--private-dir", default=None, help="安装时使用的自定义私有目录")
    p_restore.add_argument("--yes", action="store_true", help="跳过确认提示")

    p_share = sub.add_parser("share", help="查看、修改或关闭宿主共享文件夹（下次开机生效）")
    p_share.add_argument("--tag", default="cc01", help="实例短名（默认 cc01）")
    p_share.add_argument("--private-dir", default=None, help="安装时使用的自定义私有目录")
    p_share.add_argument("path", nargs="?", default=None, help="要共享的宿主文件夹路径（不填则显示当前设置）")
    p_share.add_argument("--read-only", action="store_true", help="以只读方式共享给客机")
    p_share.add_argument("--off", action="store_true", help="关闭共享文件夹")

    args = parser.parse_args()
    if args.command == "up":
        sys.exit(cmd_up(args))
    if args.command == "remove":
        sys.exit(cmd_remove(args))
    if args.command == "open":
        sys.exit(cmd_open(args))
    if args.command == "stop":
        sys.exit(cmd_stop(args))
    if args.command == "status":
        sys.exit(cmd_status(args))
    if args.command == "restore":
        sys.exit(cmd_restore(args))
    if args.command == "share":
        sys.exit(cmd_share(args))


if __name__ == "__main__":
    main()
