# 代理模式与出口配置（`EXIT_MODE`）

`cchouse` 的核心是**网络限制（fail-closed）+ 环境隔离**，出口代理层与客机隔离层解耦。所有路线都要求用户**自备外部 IP 代理**。

## 支持的出口模式

| 模式 | `EXIT_MODE` | 支持路线 | 适用场景 | 必填代理参数 |
|---|---|---|---|---|
| **单节点模式** | `single` | **仅 v2 Linux** | 海外直连、或手里已有 1 个干净固定出口节点（加密协议 VPS / 可直连 ISP） | `SINGLE_TYPE`、`SINGLE_SERVER`（及 `SINGLE_SERVER_IP`）、`SINGLE_PORT` 及对应协议认证项 |
| **两跳链式模式** | `chain` | **v1 macOS（Tart） & v2 Linux** | 国内无法直连 ISP 代理，需要「前置节点 → 固定 ISP」两跳容灾 | `WEBSHARE_*` + `FRONT_*` + `NODE_*`（见 `instance.env.example`） |

### 单节点支持的协议（`SINGLE_TYPE`）

- 支持：`socks5`、`http`、`vless`、`trojan`、`ss`、`ssh`（纯 TCP 传输）。
- **明确拒绝**：`hysteria2`、`tuic` 等基于 UDP 的代理协议。`cchouse` 默认全局封锁 UDP，`preflight.py` 与 `render.py` 会在渲染阶段直接拒收，避免运行时默默断网。

> **国内直连提示**：如果在国内直连裸 `socks5` 住宅/ISP 代理，明文协议易被干扰或丢包；且若该 ISP 屏蔽 Stripe，单节点模式直接付款会失败。此时可配置可选的 Stripe 专用节点（`STRIPE_EXIT_MODE=node` + `STRIPE_NODE_*`），或改用 `EXIT_MODE=chain`。

## 域名节点的 IP 钉死规则

`cchouse` 客机内**不开直连公网的 DNS/DoH 放行口**，防火墙白名单只放行明确的 `IP:端口`。

- 若 `SINGLE_SERVER` 填写的是域名（例如 `proxy.example.com`）：
  1. 渲染前在**宿主机**解析该域名的 IPv4（A 记录），填入 `SINGLE_SERVER_IP`（或由 `render.py --resolve` 自动解析并提示确认）。
  2. 生成的 `config.yaml` 中 `server:` 写入钉死的 IP，**原域名保留在 `servername` / `sni` / `ws-opts.headers.Host`**，不影响 TLS / REALITY / WS 握手。
  3. `nftables` 白名单仅放行 `meta skuid mihomo` 访问该 `IP:端口`。
- **若节点挂在 CDN 后且 IP 频繁轮换**：需像 v1 `Front-CF` 一样手动选定稳定 IP 钉死；若 IP 变动，重新渲染并更新白名单即可。

## Stripe 独立分流（`STRIPE_EXIT_MODE`）

部分静态 ISP / 住宅代理商（如 Webshare）会在代理侧按规则直接拦截 `*.stripe.com`（SOCKS5 返回 `0x02 connection not allowed by ruleset`），导致 `claude.ai` 结账页无法加载 Stripe.js 或提交卡片失败。

- **`off`（单节点模式默认，链式模式可选）**：不单独分流，Stripe 流量跟主出口走。
- **`node`（仅单节点模式可选）**：额外配置一个 Stripe 专用节点（`STRIPE_NODE_TYPE`、`STRIPE_NODE_SERVER` / `STRIPE_NODE_SERVER_IP`、`STRIPE_NODE_PORT` 及对应协议认证项，口径同 `SINGLE_*`，同样禁止 UDP 协议且遵循域名钉死规则）。`stripe.com` / `stripe.network` / `stripecdn.com` 走该节点，`nftables` 白名单同步放行该节点 `IP:端口`，其余所有流量仍锁死主出口，全程无 `DIRECT`。若未显式设置 `STRIPE_EXIT_MODE` 但填写了 `STRIPE_NODE_SERVER`，自动按 `node` 处理。
- **`front`（链式模式默认）**：`stripe.com` / `stripe.network` / `stripecdn.com` 经前置节点直出（不走 ISP）。
- **`tmh-chain`（链式模式高级选项）**：Stripe 组所有成员（含经机场中转）均以自建前置 FrontVPS 为最终出口，无裸机场回退，需服务端支持无 SNI REALITY（见 [front.md](front.md)）。

### Stripe 可达性自检

- **部署前（宿主机，支持 `socks5`/`http` 单节点）**：运行 `python3 -I scripts/preflight.py --env <instance.env> --check-stripe`，在宿主机探测 `js.stripe.com` / `api.stripe.com` 连通性（不打印凭据）。
- **部署后（客机内，全协议通用）**：`scripts/verify-linux.sh` 会自动探测客机内 `js.stripe.com` 与 `api.stripe.com` 可达性：
  - 任何模式下 Stripe 不通都只输出 `WARN` 提示，**不判失败、不阻止安装**：Stripe 只影响在 claude.ai 网页上付款，已订阅或在其他地方付款的用户不受影响。
  - 单节点（`STRIPE_EXIT_MODE=off`）不通时提示会附解决办法：配置 Stripe 备用节点或改链式。
  - `preflight.py --check-stripe` 同理只给警告，不计入错误。

> **风控提示**：`STRIPE_NODE_*` 建议尽量选择与主出口或支付卡同地区、干净的独享节点（如自建 VPS），避免使用多人共享高风险出口叠加发卡行国家不一致触发 Stripe Radar 拒付。

## 防泄漏硬约束（由脚本强制校验）

1. 生成的 `config.yaml` **绝不允许出现 `DIRECT` 或 `MATCH,DIRECT`**，`render.py` 和 `verify-linux.sh` 均会硬拦截。
2. `dns.ipv6: false`，规则首条为 `NETWORK,UDP,REJECT`。
3. 业务 DoH 强制绑定出口组，无直连回退。
