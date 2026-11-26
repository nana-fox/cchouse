#!/usr/bin/env python3
"""把 guest/ (macOS v1) 或 guest-linux/ (Debian 13 v2) 模板按实例参数渲染成可部署的客机文件。

用法：
  python3 -I scripts/render.py [--resolve] <instance.env> <输出目录>

- 默认 GUEST_OS=macos、EXIT_MODE=chain，完全兼容 v1 与 import-existing.py。
- GUEST_OS=linux 时支持 EXIT_MODE=single（单节点）和 EXIT_MODE=chain（前置 -> 固定 ISP）。
- 所有文件先在内存中完整渲染并通过断言，再统一落盘；出错绝不留明文半成品。
- 输出目录权限 700、文件 600，里面是明文凭据，用完立即删除。
"""
import ipaddress
import os
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUEST = ROOT / "guest"
GUEST_LINUX = ROOT / "guest-linux"
PLACEHOLDER = re.compile(r"<([A-Z][A-Z0-9_]*)>")
CHAIN_BEGIN = "# BEGIN STRIPE_FRONT_CHAIN\n"
CHAIN_END = "# END STRIPE_FRONT_CHAIN\n"

SUPPORTED_SINGLE_TYPES = ("socks5", "http", "vless", "trojan", "ss", "ssh")
REJECTED_UDP_TYPES = ("hysteria", "hysteria2", "hy2", "tuic", "wireguard")
DIRECT_RULE_RE = re.compile(r"(?mi)^\s*-\s*[^#\n]*,\s*DIRECT(\s|$)")


def is_ipv4(value):
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def assert_no_direct_rules(config_text, name="config.yaml"):
    if DIRECT_RULE_RE.search(config_text):
        raise ValueError(f"{name}: 检测到禁止的 DIRECT 规则，拒绝输出")


def stripe_members(tag, label, chained):
    names = ([f"{tag}-Front", f"{tag}-Front-CF", f"{tag}-FRONT-via-{label}-WS", f"{tag}-FRONT-via-{label}-ThreeNetworks"]
             if chained else [f"{tag}-Front", f"{tag}-Front-CF", f"{tag}-{label}-WS", f"{tag}-{label}-ThreeNetworks"])
    return f"- name: {tag}-Stripe\n  type: fallback\n  proxies:\n" + "".join(f"  - {n}\n" for n in names)


def stripe_tmh_chain(config, tag, label):
    """Keep the original ISP routes; only Stripe's final hop becomes FrontVPS.

    An IP servername suppresses TLS SNI. The existing REALITY public-key
    authentication remains enabled; the server must explicitly accept empty SNI.
    This avoids transit providers replacing the destination with the camouflage SNI.
    """
    if CHAIN_BEGIN in config or CHAIN_END in config:
        raise ValueError("Stripe chain is already present")
    front = re.search(r"(?m)^- name: " + re.escape(tag) + r"-Front\n((?:[ \t].*\n)+)", config)
    if not front:
        raise ValueError("Cannot locate the existing front proxy")
    body = front.group(1)
    server = re.search(r"(?m)^  server: ([^\n]+)$", body)
    if not server:
        raise ValueError("Front proxy has no server")
    body, count = re.subn(r"(?m)^  servername: [^\n]+$", "  servername: " + server.group(1), body)
    if count != 1 or "dialer-proxy:" in body:
        raise ValueError("Unexpected front proxy structure")
    nodes = CHAIN_BEGIN
    for kind in ("WS", "ThreeNetworks"):
        nodes += f"- name: {tag}-FRONT-via-{label}-{kind}\n" + body + f"  dialer-proxy: {tag}-{label}-{kind}\n"
    nodes += CHAIN_END
    old = stripe_members(tag, label, False)
    if config.count(old) != 1 or config.count("\nproxy-groups:\n") != 1:
        raise ValueError("Unexpected Stripe group structure")
    return config.replace("\nproxy-groups:\n", "\n" + nodes + "proxy-groups:\n", 1).replace(old, stripe_members(tag, label, True), 1)


def normalize_stripe_chain(config, tag):
    """Validate all generated chain fields before importing the base template."""
    if CHAIN_BEGIN not in config and CHAIN_END not in config:
        return config, False
    if config.count(CHAIN_BEGIN) != 1 or config.count(CHAIN_END) != 1:
        raise ValueError("Invalid Stripe chain markers")
    start = config.index(CHAIN_BEGIN)
    end = config.index(CHAIN_END, start) + len(CHAIN_END)
    match = re.search(r"(?m)^- name: " + re.escape(tag) + r"-FRONT-via-([\w-]+)-WS$", config[start:end])
    if not match:
        raise ValueError("Cannot identify Stripe transit nodes")
    label = match.group(1)
    base = config[:start] + config[end:]
    new = stripe_members(tag, label, True)
    if base.count(new) != 1:
        raise ValueError("Unexpected chained Stripe group")
    base = base.replace(new, stripe_members(tag, label, False), 1)
    if stripe_tmh_chain(base, tag, label) != config:
        raise ValueError("Stripe chain differs from the generated configuration")
    return base, True


def load_env(path):
    env = {}
    # 使用 utf-8-sig 兼容 Windows 记事本保存的 UTF-8 BOM
    for n, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            sys.exit(f"{path}:{n}: 不是 KEY=VALUE 格式")
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == "'":
            v = v[1:-1]
        else:
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            v = os.path.expandvars(v)
        env[k.strip()] = v
    if "TAG" in env:
        env["TAG_UPPER"] = env["TAG"].upper()
    return env


def render_text(text, env, name, optional_keys=()):
    opt = set(optional_keys)
    missing = sorted({m for m in PLACEHOLDER.findall(text) if m not in opt and not env.get(m)})
    if missing:
        sys.exit(f"{name}: instance.env 缺少或为空：{', '.join(missing)}")
    return PLACEHOLDER.sub(lambda m: env.get(m.group(1), ""), text)


def effective_stripe_mode(env, guest_os, exit_mode):
    raw = env.get("STRIPE_EXIT_MODE", "").strip().lower()
    if raw:
        return raw
    if guest_os == "linux" and exit_mode == "single":
        if env.get("STRIPE_NODE_SERVER", "").strip():
            return "node"
        return "off"
    return "front"


def resolve_node_server_ip(env, prefix="SINGLE", allow_resolve=False):
    server_key = f"{prefix}_SERVER"
    ip_key = f"{prefix}_SERVER_IP"
    server = env.get(server_key, "").strip()
    if not server:
        sys.exit(f"缺少 {server_key}")
    if is_ipv4(server):
        return server, None
    pinned = env.get(ip_key, "").strip()
    if pinned:
        if not is_ipv4(pinned):
            sys.exit(f"{ip_key} 不是有效 IPv4：{pinned}")
        return pinned, server
    if not allow_resolve:
        sys.exit(
            f"{server_key}={server} 是域名，请在宿主机解析 IPv4 后填入 {ip_key}，"
            "或加 --resolve 参数由 render.py 在宿主解析并钉死 IP"
        )
    try:
        infos = socket.getaddrinfo(server, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        sys.exit(f"宿主机解析 {server} 失败：{exc}")
    ips = sorted({item[4][0] for item in infos})
    if not ips:
        sys.exit(f"宿主机未解析到 {server} 的 IPv4 (A) 记录")
    return ips[0], server


def resolve_single_server_ip(env, allow_resolve=False):
    return resolve_node_server_ip(env, prefix="SINGLE", allow_resolve=allow_resolve)


def write_lf(path, text):
    """兼容 Python 3.9+（Path.write_text 的 newline 参数仅 3.10+ 支持），且在 Windows 上强制 LF。"""
    path.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))


def yaml_quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_kv(key, value, indent=2):
    return f"{' ' * indent}{key}: {value}"


def build_proxy_node_lines(env, prefix, node_name, pinned_ip, orig_host):
    k = lambda suffix: f"{prefix}_{suffix}"
    stype = env.get(k("TYPE"), "").strip().lower()
    if stype in REJECTED_UDP_TYPES:
        sys.exit(f"{k('TYPE')}={stype} 是 UDP 协议，cchouse 默认全局禁 UDP，拒绝渲染")
    if stype not in SUPPORTED_SINGLE_TYPES:
        sys.exit(f"不支持的 {k('TYPE')}={stype}，允许值：{', '.join(SUPPORTED_SINGLE_TYPES)}")
    port = env.get(k("PORT"), "").strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        sys.exit(f"{k('PORT')} 无效：{port}")

    lines = [
        f"- name: {node_name}",
        yaml_kv("type", stype),
        yaml_kv("server", pinned_ip),
        yaml_kv("port", port),
        yaml_kv("udp", "false"),
    ]
    raw_tls = env.get(k("TLS"), "").strip().lower()
    if stype in ("socks5", "http"):
        if env.get(k("USER")):
            lines.append(yaml_kv("username", yaml_quote(env[k("USER")])))
        if env.get(k("PASS")):
            lines.append(yaml_kv("password", yaml_quote(env[k("PASS")])))
        if raw_tls == "true":
            if stype == "socks5" and orig_host:
                sys.exit("锁定版 mihomo 的 SOCKS5 TLS 使用 server 字段校验域名证书，不支持域名钉 IP 后启用 TLS")
            lines.append(yaml_kv("tls", "true"))
            sni = env.get(k("SNI")) or orig_host
            if sni:
                lines.append(yaml_kv("sni", sni))
    elif stype == "vless":
        if not env.get(k("UUID")):
            sys.exit(f"{k('TYPE')}=vless 缺少 {k('UUID')}")
        if raw_tls == "false":
            sys.exit(f"{k('TYPE')}=vless 禁止明文（{k('TLS')} 不能为 false）")
        net = env.get(k("NETWORK"), "tcp").strip().lower()
        if net not in ("tcp", "ws"):
            sys.exit(f"{k('TYPE')}=vless 不支持的传输 {k('NETWORK')}={net}，仅支持 tcp 或 ws")
        lines.append(yaml_kv("uuid", env[k("UUID")]))
        lines.append(yaml_kv("tls", "true"))
        lines.append(yaml_kv("skip-cert-verify", "false"))
        if env.get(k("FLOW")):
            lines.append(yaml_kv("flow", env[k("FLOW")]))
        if env.get(k("FINGERPRINT")):
            lines.append(yaml_kv("client-fingerprint", env[k("FINGERPRINT")]))
        sni = env.get(k("SNI")) or orig_host
        if sni:
            lines.append(yaml_kv("servername", sni))
        if env.get(k("PUBLIC_KEY")):
            lines.append("  reality-opts:")
            lines.append(yaml_kv("public-key", env[k("PUBLIC_KEY")], indent=4))
            if env.get(k("SHORT_ID")):
                lines.append(yaml_kv("short-id", env[k("SHORT_ID")], indent=4))
        if net == "ws":
            lines.append(yaml_kv("network", "ws"))
            lines.append("  ws-opts:")
            lines.append(yaml_kv("path", yaml_quote(env.get(k("WS_PATH")) or "/"), indent=4))
            ws_host = env.get(k("WS_HOST")) or sni
            if ws_host:
                lines.append("    headers:")
                lines.append(yaml_kv("Host", ws_host, indent=6))
    elif stype == "trojan":
        if not env.get(k("PASS")):
            sys.exit(f"{k('TYPE')}=trojan 缺少 {k('PASS')}")
        if raw_tls == "false":
            sys.exit(f"{k('TYPE')}=trojan 必须启用 TLS（{k('TLS')} 不能为 false）")
        net = env.get(k("NETWORK"), "tcp").strip().lower()
        if net not in ("tcp", "ws"):
            sys.exit(f"{k('TYPE')}=trojan 不支持的传输 {k('NETWORK')}={net}，仅支持 tcp 或 ws")
        lines.append(yaml_kv("password", yaml_quote(env[k("PASS")])))
        lines.append(yaml_kv("skip-cert-verify", "false"))
        sni = env.get(k("SNI")) or orig_host
        if sni:
            lines.append(yaml_kv("sni", sni))
        if env.get(k("FINGERPRINT")):
            lines.append(yaml_kv("client-fingerprint", env[k("FINGERPRINT")]))
        if net == "ws":
            lines.append(yaml_kv("network", "ws"))
            lines.append("  ws-opts:")
            lines.append(yaml_kv("path", yaml_quote(env.get(k("WS_PATH")) or "/"), indent=4))
            ws_host = env.get(k("WS_HOST")) or sni
            if ws_host:
                lines.append("    headers:")
                lines.append(yaml_kv("Host", ws_host, indent=6))
    elif stype == "ss":
        if not env.get(k("CIPHER")) or not env.get(k("PASS")):
            sys.exit(f"{k('TYPE')}=ss 缺少 {k('CIPHER')} 或 {k('PASS')}")
        lines.append(yaml_kv("cipher", env[k("CIPHER")]))
        lines.append(yaml_kv("password", yaml_quote(env[k("PASS")])))
    elif stype == "ssh":
        if not env.get(k("USER")) or not env.get(k("PASS")):
            sys.exit(f"{k('TYPE')}=ssh 缺少 {k('USER')} 或 {k('PASS')}")
        lines.append(yaml_kv("username", yaml_quote(env[k("USER")])))
        lines.append(yaml_kv("password", yaml_quote(env[k("PASS")])))
        if env.get(k("HOST_KEY")):
            lines.append("  host-key:")
            lines.append(f"  - {yaml_quote(env[k('HOST_KEY')])}")

    return "\n".join(lines), int(port)


def build_single_linux_config(env, pinned_ip, orig_host, stripe_node=None):
    tag_upper = env["TAG_UPPER"]
    lan_cidr = env.get("HOME_LAN_CIDR", "").strip()
    if not lan_cidr:
        sys.exit("instance.env 缺少 HOME_LAN_CIDR")

    node_name = f"{tag_upper}-Single-Exit"
    proxy_block, main_port = build_proxy_node_lines(env, "SINGLE", node_name, pinned_ip, orig_host)
    targets = [(pinned_ip, main_port)]

    stripe_group_block = ""
    stripe_rules_block = ""
    if stripe_node is not None:
        stripe_pinned_ip, stripe_orig_host = stripe_node
        stripe_node_name = f"{tag_upper}-Stripe-Node"
        stripe_proxy_block, stripe_port = build_proxy_node_lines(
            env, "STRIPE_NODE", stripe_node_name, stripe_pinned_ip, stripe_orig_host
        )
        proxy_block = proxy_block + "\n" + stripe_proxy_block
        targets.append((stripe_pinned_ip, stripe_port))
        stripe_group_block = (
            f"- name: {tag_upper}-Stripe\n"
            f"  type: select\n"
            f"  proxies:\n"
            f"  - {stripe_node_name}\n"
            f"  disable-udp: true\n"
        )
        stripe_rules_block = (
            f"- DOMAIN-SUFFIX,stripe.com,{tag_upper}-Stripe\n"
            f"- DOMAIN-SUFFIX,stripe.network,{tag_upper}-Stripe\n"
            f"- DOMAIN-SUFFIX,stripecdn.com,{tag_upper}-Stripe\n"
        )

    config = f"""---
find-process-mode: off
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
ipv6: false
tun:
  enable: true
  device: cchouse0
  stack: system
  auto-route: true
  auto-redirect: false
  auto-detect-interface: true
  strict-route: false
  route-exclude-address:
  - 192.168.64.0/24
  - {lan_cidr}
  dns-hijack:
  - any:53
  - tcp://any:53
dns:
  enable: true
  listen: 127.0.0.1:1053
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  prefer-h3: false
  respect-rules: true
  default-nameserver:
  - https://1.1.1.1/dns-query#{tag_upper}-Exit
  nameserver:
  - https://1.1.1.1/dns-query#{tag_upper}-Exit
  proxy-server-nameserver:
  - https://1.1.1.1/dns-query#{tag_upper}-Exit
proxies:
{proxy_block}
proxy-groups:
- name: {tag_upper}-Exit
  type: select
  proxies:
  - {node_name}
  disable-udp: true
{stripe_group_block}- name: GLOBAL
  type: select
  proxies:
  - {tag_upper}-Exit
  disable-udp: true
rules:
- NETWORK,UDP,REJECT
{stripe_rules_block}- MATCH,{tag_upper}-Exit
"""
    assert_no_direct_rules(config)
    return config, targets


def build_chain_linux_config(env, stripe_mode):
    """复用 guest/config.yaml 单一基线，附加 Linux 差异层并修掉 ISP 跳 DNS 循环依赖。"""
    base = render_text((GUEST / "config.yaml").read_text(encoding="utf-8"), env, "guest/config.yaml")
    if stripe_mode == "tmh-chain":
        base = stripe_tmh_chain(base, env["TAG_UPPER"], env["NODE_LABEL"])
    # Linux 单机交付默认关闭 TCP 控制面，防止客机内普通进程无认证修改内存代理配置
    base = base.replace("external-controller: 127.0.0.1:9090\n", "", 1)
    # Linux 固定 TUN 设备名 cchouse0（供 nftables 正向白名单）并显式关闭 auto-redirect
    base = base.replace(
        "  enable: true\n  stack: system\n  auto-route: true\n",
        "  enable: true\n  device: cchouse0\n  stack: system\n  auto-route: true\n  auto-redirect: false\n",
        1,
    )
    # 避免 ISP 跳为域名时 proxy-server-nameserver 指向 Fixed-ISP 产生循环依赖
    tag_upper = env["TAG_UPPER"]
    base = base.replace(
        f"  proxy-server-nameserver:\n  - https://1.1.1.1/dns-query#{tag_upper}-Fixed-ISP\n",
        f"  proxy-server-nameserver:\n  - https://1.1.1.1/dns-query#{tag_upper}-Stripe\n",
        1,
    )
    if stripe_mode == "off":
        for dom in ("stripe.com", "stripe.network", "stripecdn.com"):
            base = base.replace(f"- DOMAIN-SUFFIX,{dom},{tag_upper}-Stripe\n", "")
    assert_no_direct_rules(base)
    egress_targets = [
        (env["FRONT_SERVER_IP"], int(env["FRONT_PORT"])),
        (env["FRONT_CF_SERVER_IP"], 443),
        (env["NODE_WS_SERVER_IP"], 443),
        (env["NODE_REALITY_SERVER_IP"], 443),
    ]
    for ip, _ in egress_targets:
        if not is_ipv4(ip):
            sys.exit(f"chain 模式入口必须为 IPv4，检测到非 IP：{ip}")
    return base, egress_targets


def set_route_exclude(config_text, nets):
    """把 tun.route-exclude-address 列表整体替换为去重合并后的宿主侧网段（与 SSH 白名单同源）。"""
    pattern = re.compile(r"(  route-exclude-address:\n)(?:  - [^\n]*\n)+")
    if len(pattern.findall(config_text)) != 1:
        sys.exit("config.yaml 中 tun.route-exclude-address 必须恰好出现一次")
    items = "".join(f"  - {n}\n" for n in nets)
    return pattern.sub(lambda m: m.group(1) + items, config_text, count=1)


def format_nft_egress_rules(mihomo_user, targets):
    lines = []
    seen = set()
    for ip, port in targets:
        if (ip, port) in seen:
            continue
        seen.add((ip, port))
        lines.append(
            f'    meta skuid "{mihomo_user}" ip daddr {ip} tcp dport {port} accept'
        )
    return "\n".join(lines)


def commit_files(out, rendered_map):
    """全部在内存中校验通过后再写入磁盘，并清理目标目录中的旧残留文件（如旧 authorized_keys）。"""
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    expected = {name for name, _ in rendered_map}
    for old in list(out.iterdir()):
        if old.is_file() and old.name not in expected:
            old.unlink()
    for rel_name, content in rendered_map:
        dst = out / rel_name
        write_lf(dst, content)
        os.chmod(dst, 0o600)
        print(dst)


def render_macos(env, out, stripe_mode):
    if stripe_mode not in ("front", "tmh-chain"):
        sys.exit("macOS v1 下 STRIPE_EXIT_MODE 必须为 front 或 tmh-chain")
    rendered = []
    for src in sorted(GUEST.iterdir()):
        if not src.is_file():
            continue
        dst_name = src.name.replace(".TAG.", f".{env['TAG']}.")
        content = render_text(src.read_text(encoding="utf-8"), env, src.name)
        if src.name == "config.yaml":
            if stripe_mode == "tmh-chain":
                content = stripe_tmh_chain(content, env["TAG_UPPER"], env["NODE_LABEL"])
            assert_no_direct_rules(content)
        rendered.append((dst_name, content))
    commit_files(out, rendered)


def render_linux(env, out, exit_mode, stripe_mode, allow_resolve):
    env = dict(env)
    env.setdefault("MIHOMO_USER", "mihomo")
    env.setdefault("GUEST_LOCALE", "en_US.UTF-8")
    env.setdefault("GUEST_KEYBOARD", "us")
    kb = env["GUEST_KEYBOARD"].strip()
    if not re.fullmatch(r"[a-z]{2,3}", kb):
        sys.exit(f"INVALID_GUEST_KEYBOARD: GUEST_KEYBOARD={kb!r} 仅支持 2-3 位小写 XKB 布局代码（如 us/gb/de/jp）")
    env["GUEST_KEYBOARD"] = kb
    env.setdefault("GUEST_HOSTNAME", "")
    env.setdefault("VMNET_CIDR", "192.168.180.0/24")

    raw_cidrs = [
        env.get("VMNET_CIDR", "").strip(),
        env.get("HOME_LAN_CIDR", "").strip(),
        "192.168.64.0/24",
    ]
    parsed_nets = []
    for cidr in raw_cidrs:
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            sys.exit(f"SSH 白名单网段不是有效 CIDR：{cidr}")
        if net.version != 4:
            sys.exit(f"SSH 白名单仅支持 IPv4 CIDR：{cidr}")
        parsed_nets.append(net)
    collapsed = list(ipaddress.collapse_addresses(parsed_nets))
    env["SSH_NETS"] = ", ".join(str(n) for n in collapsed)

    if exit_mode == "single":
        if stripe_mode not in ("off", "node"):
            sys.exit("EXIT_MODE=single 时 STRIPE_EXIT_MODE 仅支持 off 或 node")
        pinned_ip, orig_host = resolve_node_server_ip(env, prefix="SINGLE", allow_resolve=allow_resolve)
        stripe_node = None
        if stripe_mode == "node":
            stripe_node = resolve_node_server_ip(env, prefix="STRIPE_NODE", allow_resolve=allow_resolve)
        config_text, targets = build_single_linux_config(env, pinned_ip, orig_host, stripe_node=stripe_node)
    elif exit_mode == "chain":
        if stripe_mode not in ("off", "front", "tmh-chain"):
            sys.exit("EXIT_MODE=chain 时 STRIPE_EXIT_MODE 必须为 off、front 或 tmh-chain")
        config_text, targets = build_chain_linux_config(env, stripe_mode)
    else:
        sys.exit("EXIT_MODE 必须为 single 或 chain")

    # 宿主侧网段（客机 NAT / 家庭局域网）必须绕过 TUN，否则 mihomo 启动后宿主 SSH 回包被劫持
    config_text = set_route_exclude(config_text, collapsed)
    env["NFT_EGRESS_RULES"] = format_nft_egress_rules(env["MIHOMO_USER"], targets)

    rendered = [("config.yaml", config_text)]
    for src in sorted(GUEST_LINUX.iterdir()):
        if not src.is_file() or src.name == "image-manifest.json":
            continue
        content = render_text(
            src.read_text(encoding="utf-8"),
            env,
            src.name,
            optional_keys=("GUEST_HOSTNAME",),
        )
        rendered.append((src.name, content))

    if not env.get("SSH_PUBKEY", "").strip():
        sys.exit("GUEST_OS=linux 必填 SSH_PUBKEY（firstboot 会关闭密码登录，缺公钥将无法 SSH 验收）")
    rendered.append(("authorized_keys", env["SSH_PUBKEY"].strip() + "\n"))

    commit_files(out, rendered)


def main():
    args = sys.argv[1:]
    allow_resolve = False
    if "--resolve" in args:
        allow_resolve = True
        args.remove("--resolve")
    if len(args) != 2:
        sys.exit(__doc__)

    env = load_env(args[0])
    if not env.get("TAG"):
        sys.exit("instance.env 缺少 TAG")
    guest_os = env.get("GUEST_OS", "macos").strip().lower()
    exit_mode = env.get("EXIT_MODE", "chain").strip().lower()
    stripe_mode = effective_stripe_mode(env, guest_os, exit_mode)

    out = Path(args[1])
    if guest_os == "macos":
        if exit_mode != "chain":
            sys.exit("v1 macOS 模板当前仅支持 EXIT_MODE=chain")
        render_macos(env, out, stripe_mode)
    elif guest_os == "linux":
        render_linux(env, out, exit_mode, stripe_mode, allow_resolve)
    else:
        sys.exit(f"不支持的 GUEST_OS={guest_os}，允许值：macos、linux")


if __name__ == "__main__":
    main()
