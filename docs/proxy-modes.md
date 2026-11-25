# 代理模式与出口配置（`EXIT_MODE`）

`cchouse` 的核心是**网络限制（fail-closed）+ 环境隔离**，出口代理层与客机隔离层解耦。所有路线都要求用户**自备外部 IP 代理**。

## 支持的出口模式

| 模式 | `EXIT_MODE` | 支持路线 | 适用场景 | 必填代理参数 |
|---|---|---|---|---|
| **单节点模式** | `single` | **仅 v2 Linux（VMware）** | 海外直连、或手里已有 1 个干净固定出口节点（加密协议 VPS / 可直连 ISP） | `SINGLE_TYPE`、`SINGLE_SERVER`（及 `SINGLE_SERVER_IP`）、`SINGLE_PORT` 及对应协议认证项 |
| **两跳链式模式** | `chain` | **v1 macOS（Tart） & v2 Linux** | 国内无法直连 ISP 代理，需要「前置节点 → 固定 ISP」两跳容灾 | `WEBSHARE_*` + `FRONT_*` + `NODE_*`（见 `instance.env.example`） |

### 单节点支持的协议（`SINGLE_TYPE`）

- 支持：`socks5`、`http`、`vless`、`trojan`、`ss`、`ssh`（纯 TCP 传输）。
- **明确拒绝**：`hysteria2`、`tuic` 等基于 UDP 的代理协议。`cchouse` 默认全局封锁 UDP，`preflight.py` 与 `render.py` 会在渲染阶段直接拒收，避免运行时默默断网。

> **国内直连提示**：如果在国内直连裸 `socks5` 住宅/ISP 代理，明文协议易被干扰或丢包；且若该 ISP 屏蔽 Stripe，单节点模式无法直接付款。此时建议改用 `EXIT_MODE=chain`。

## 域名节点的 IP 钉死规则

`cchouse` 客机内**不开直连公网的 DNS/DoH 放行口**，防火墙白名单只放行明确的 `IP:端口`。

- 若 `SINGLE_SERVER` 填写的是域名（例如 `proxy.example.com`）：
  1. 渲染前在**宿主机**解析该域名的 IPv4（A 记录），填入 `SINGLE_SERVER_IP`（或由 `render.py --resolve` 自动解析并提示确认）。
  2. 生成的 `config.yaml` 中 `server:` 写入钉死的 IP，**原域名保留在 `servername` / `sni` / `ws-opts.headers.Host`**，不影响 TLS / REALITY / WS 握手。
  3. `nftables` 白名单仅放行 `meta skuid mihomo` 访问该 `IP:端口`。
- **若节点挂在 CDN 后且 IP 频繁轮换**：需像 v1 `Front-CF` 一样手动选定稳定 IP 钉死；若 IP 变动，重新渲染并更新白名单即可。

## Stripe 独立分流（`STRIPE_EXIT_MODE`）

部分静态 ISP 代理商（如 Webshare）会屏蔽 Stripe。

- **`off`（单节点模式默认，链式模式可选）**：不单独分流，Stripe 流量跟主出口走。
- **`front`（链式模式默认）**：`stripe.com` / `stripe.network` / `stripecdn.com` 经前置节点直出（不走 ISP）。
- **`tmh-chain`（链式模式高级选项）**：Stripe 组所有成员（含经机场中转）均以自建前置 FrontVPS 为最终出口，无裸机场回退，需服务端支持无 SNI REALITY（见 [front.md](front.md)）。
- 单节点模式（`EXIT_MODE=single`）只允许 `STRIPE_EXIT_MODE=off`。

## 防泄漏硬约束（由脚本强制校验）

1. 生成的 `config.yaml` **绝不允许出现 `DIRECT` 或 `MATCH,DIRECT`**，`render.py` 和 `verify-linux.sh` 均会硬拦截。
2. `dns.ipv6: false`，规则首条为 `NETWORK,UDP,REJECT`。
3. 业务 DoH 强制绑定出口组，无直连回退。
