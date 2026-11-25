# 自建前置节点

前置把客机流量从国内送到固定 ISP 代理（ISP 代理是 SOCKS5，从国内直连明文且不稳）。Stripe 默认从选中的前置直出，也可按实例启用下面的 FrontVPS 链式模式。
自建前置是主入口，机场是备用（见 [architecture.md](architecture.md)）。参数不在仓库里：
私有目录 `~/Library/Application Support/cchouse/<前置名>/front.env`，每台环境把自己那份写进 `instance.env` 的 `FRONT_*`。

## 现行部署（2026-11-15）

| 项 | 内容 |
|---|---|
| 服务端 | xray-core **26.3.27**，独立 systemd 服务 `xray-front.service`，开机自启 |
| 协议 | VLESS + REALITY + `xtls-rprx-vision`，客户端指纹 `chrome`，只认 UUID |
| 端口 | 8443（443 被同机的网站服务占用，不动它） |
| 伪装站 | `www.cisco.com`：支持 TLS 1.3 + X25519 + H2，就近有节点，不在 xray 的高风险名单里 |
| 文件 | 程序 `/usr/local/lib/xray-front/`，配置 `/etc/xray-front/config.json`（root 600，私钥只在这里），服务 `/etc/systemd/system/xray-front.service`，UUID 管理工具 `/usr/local/sbin/xray-front-client` |
| 隔离 | systemd 临时用户、无特权、系统目录只读、内存上限 512M；拒绝去往回环、内网、Tailscale、docker 网段和本机公网 IP（不能被当跳板摸进同机服务）；拒绝 UDP 和发信端口；不记访问日志 |

## 给新环境加 UUID

```bash
ssh -t <前置主机> 'sudo xray-front-client add <TAG>'   # 只打印一次 FRONT_UUID_<TAG>=...，追加到 front.env
```

工具会同时更新 `/etc/xray-front/config.json`（带 `flow: xtls-rprx-vision`）和 `/etc/xray-cfront/config.json`（VLESS+WS，无 `flow`），先校验两份新配置再重启两个服务（只断开前置上的现有连接，自动重连），旧配置留作 `config.json.bak`。
另有 `list`（只列标签）和 `remove <TAG>`。每台环境一个 UUID（两条自建前置入口共用），环境作废就删掉它的 UUID。

## 经 Cloudflare 接入的自建前置（2026-11-19）

前置主机公网 IP 与国内大陆之间 TCP 双向不通（ping 通）时，国内客机直连 `8443`（VLESS + REALITY）会被 fallback 跳过，而 REALITY 不能经过 Cloudflare 七层代理。为此在同机新增独立服务 `xray-cfront.service`，保留原 `xray-front.service`（8443）不改不重启（供 `tmh-chain` 经海外机场中转连接）：

| 项 | 内容 |
|---|---|
| 服务端 | 同 `/usr/local/lib/xray-front/xray`（**26.3.27**），独立 systemd 服务 `xray-cfront.service`，开机自启 |
| 协议 | VLESS + WebSocket（客户端外层 TLS 由 Cloudflare 边缘与网关 Caddy 承载，`flow: ''`，客户端指纹 `chrome`） |
| 监听与反代 | 仅监听 `127.0.0.1:18443`；由同机 `edge-gateway`（Caddy 容器，host 网络）的子域名站点块用 `handle /<随机路径>` 反代（不用 `handle_path`，不记访问日志，其余路径返回普通 200 页面），DNS 开启 Cloudflare 橙云（SSL/TLS `Full (strict)`，WebSockets 开启） |
| 文件与隔离 | 配置 `/etc/xray-cfront/config.json`（root 600，clients 与 `xray-front` 共用每台环境的 UUID 但不带 `flow`），服务 `/etc/systemd/system/xray-cfront.service`；systemd 临时用户、512M 内存上限、出站私网/回环/Tailscale/docker/本机公网 IP/UDP/发信端口封锁与 `xray-front` 完全一致 |
| 客户端参数 | `FRONT_CF_SERVER_IP`（实测选定的固定 Cloudflare Anycast IPv4，写入 `pf.conf` 的 443 白名单）、`FRONT_CF_HOST`（子域名，作 SNI 与 Host 头）、`FRONT_CF_WS_PATH`（随机路径），存私有目录 `front.env` 与实例 `instance.env` |

**网关维护注意**：同机 Caddy 跑在 `edge-gateway` 容器且单文件挂载曾发生 inode 脱节，改网关配置必须按 `infra-ops/README.md` 规程（先改 `infra-ops` 仓库 → `cat >` 原地覆盖宿主机 `/srv/edge-gateway/Caddyfile` → `docker cp` 到容器 `/tmp/Caddyfile.live` → 对 `/tmp/Caddyfile.live` 执行 `validate` 和 `reload`），**禁止对容器默认 `/etc/caddy/Caddyfile` 执行 reload**。首次申请子域名证书时 DNS 须先灰云、确认证书签发后再切橙云。

**策略组顺序**：
- `<TAG_UPPER>-Fixed-ISP`：`ISP-via-Front`（直连 REALITY）→ `ISP-via-Front-CF`（经 CF 到前置再到 ISP）→ 两条机场到 ISP。
- `<TAG_UPPER>-Stripe`（默认 `front` 模式）：`Front` → `Front-CF` → 两条机场（`Front-CF` 必须排在机场前面，保证国内直连 8443 不通时 Stripe 仍由自建前置 IP 出站）。
- `<TAG_UPPER>-Stripe`（`tmh-chain` 模式）：`Front` → `Front-CF` → `FRONT-via-WS` → `FRONT-via-ThreeNetworks`（全部以自建前置为末端，单 CF IP 失效时自动回退到“机场 → 前置:8443”，无裸机场回退）。

## Stripe 经机场连接 FrontVPS（2026-11-17）

实例设置 `STRIPE_EXIT_MODE='tmh-chain'` 后，渲染器只替换 Stripe 组：客机 → 机场 WS / ThreeNetworks → FrontVPS → Stripe。两条成员均以 FrontVPS 为末端，不包含裸机场或 DIRECT 回退；固定 ISP 组与既有域名规则保持原样。未设置或设为 `front` 时保持原有行为，其他实例不自动切换。

本次对照：原伪装站 SNI 经两条机场链均超时，同一认证参数经 SSH 管理隧道能握手。服务端 REALITY `serverNames` 保留原域名并增加空字符串，生成的链式客户端以服务器 IP 作为 `servername`（IP 字面量不发送 TLS SNI）后，两条机场链都成功。结果支持“中转按 SNI 改写目标”的推断，尚未直接取得机场内部配置，不能认定已经证明 GFW 封锁。

UUID、REALITY 公钥与 short ID 校验均保留，`skip-cert-verify` 仍为 false；服务端只保留原 8443，没有升级 xray/mihomo，也没有改网站 443。服务端变更有独立 120 秒 systemd 回滚保护，成功探针确认后保留；修改服务端须先保留可恢复的完整私有配置。

ex02 已在客机内分别验证两条链的末端 IP 相同，正式规则下的 `js.stripe.com` 连接命中 Stripe 组及 FrontVPS 链式成员，HTTPS 返回 200。普通流量仍为既定 Comcast 出口。具体地址只留私有台账。

## 升级：先测再升

**xray 26.10.21 和 26.11.9 与 mihomo（1.19.19 和 1.19.34 都试过）REALITY 握手失败**（`REALITY authentication failed`），
26.3.27、26.2.6、25.12.8 正常。升级 xray 或客机 mihomo 之前，先在宿主用同版本 mihomo 起临时客户端实测能连，
再动线上；否则所有走它的环境会切到机场，机场也不通时整体断网。

## 回滚

```bash
sudo systemctl disable --now xray-front
sudo rm -rf /etc/systemd/system/xray-front.service /usr/local/sbin/xray-front-client /etc/xray-front /usr/local/lib/xray-front
sudo systemctl daemon-reload
```

经 CF 的入口（`xray-cfront`）单独回滚：`sudo systemctl disable --now xray-cfront && sudo rm -rf /etc/systemd/system/xray-cfront.service /etc/xray-cfront && sudo systemctl daemon-reload`，再按 infra-ops README 删掉 Caddy 的 `cdn.example.com` 站点块并 reload，最后删 CF 的 `cdn` 记录。`xray-front-client` 已同时维护两份配置，只撤一边时要先改回旧版脚本（`xray-front-client.bak`）。

客机侧：前置一停，mihomo 自动切到机场入口（实测出口不变、Stripe 可用）。彻底不用前置时，从模板删掉 `<TAG_UPPER>-Front` 相关段落和 pf 那一行再部署。

## 宿主 Mac 的独立备用配置（2026-11-19）

宿主 Clash Verge 可导入一份只含 FRONT 经 CF 节点的本地订阅，原订阅都不通时再切过去。模板 `host/tmh-cf-backup-profile.yaml.example`，真实文件只放私有目录 `tmh-front/FRONT-CF-备用.yaml`（含 UUID，不进仓库）。重新生成：

```bash
cd ~/Library/Application\ Support/cchouse/tmh-front && set -a && . ./front.env && set +a && umask 077
sed -e "s|<FRONT_CF_SERVER_IP>|$FRONT_CF_SERVER_IP|" -e "s|<FRONT_UUID_MAC>|$FRONT_UUID_MAC|" \
    -e "s|<FRONT_CF_HOST>|$FRONT_CF_HOST|g" -e "s|<FRONT_CF_WS_PATH>|$FRONT_CF_WS_PATH|" \
    ~/projects/example-org/cchouse/host/tmh-cf-backup-profile.yaml.example > FRONT-CF-备用.yaml
```

UUID 标签 `mac`（`xray-front-client add mac`）；吊销用 `remove mac`。本人决定不拦 Claude / Anthropic（2026-11-19）；注意出口与虚拟机 Stripe 同 IP。

## 代价

- 前置和别的服务同机时，Stripe 看到的是这台机器的 IP；同一前置上的所有环境付款出口相同。
- 非标准端口（8443）比 443 更容易被干扰；有机场兜底。
- 前置单点：它和机场都失效时断网。
