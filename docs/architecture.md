# 架构

## 产品原则：每平台一条默认路线

每个平台一条默认路线。发版真机测试只覆盖默认路线。其余路线放在进阶文档里。

- **Mac**：Apple Silicon + Tart。安装见 [install-tart-linux.md](install-tart-linux.md)（Debian 13 arm64）。
- **Windows**：VirtualBox + Debian 13。安装见仓库 README 的快速开始。
- **Intel Mac**：暂不支持。相关代码保留。

进阶路线单独成文。需要 macOS 桌面或 Safari 高拟真时用 [build.md](build.md)（Tart + macOS）。下面的架构图属于这条进阶路线。

```
宿主 Mac（Apple Silicon）
 └─ tart 虚拟机（macOS，桥接物理网卡，局域网 IP 固定）
     ├─ mihomo（LaunchDaemon net.<TAG>.mihomo，TUN 接管全部流量）
     │    ├─ 默认：自建前置直连 / 自建前置经 CF（主）/ 两个机场入口（备）→ 固定 ISP 的 SOCKS5 → 固定出口 IP
     │    ├─ 例外：stripe.com / stripe.network / stripecdn.com → 前置直接出（直连 → 经 CF → 备用）
     │    └─ UDP 一律拒绝；DNS 用 fake-ip，DoH 也走固定出口
     ├─ pf（LaunchDaemon net.<TAG>.pf）：物理网卡只许连自建前置的 IP:端口、选定 CF IP:443 和两个机场入口的 TCP 443，
     │    外加局域网 SSH 入站和 DHCP；IPv6 全封
     ├─ 对时（net.<TAG>.timesync）：每 30 分钟经代理读 HTTPS Date（NTP 是 UDP，被封）
     └─ tart-guest-agent：剪贴板共享、tart exec
```

上图为默认 `STRIPE_EXIT_MODE=front`。设置 `tmh-chain` 时，仅 Stripe 改为“`Front` → `Front-CF` → 经机场 WS / ThreeNetworks 到 FrontVPS → Stripe”；四个成员都以 FrontVPS 为末端，无裸机场或 DIRECT 回退。固定 ISP 组及域名规则不变。服务端须先支持经验证的无 SNI REALITY 握手及 CF WebSocket 入站，见 [front.md](front.md)。2026-11-19 起 ex01 已部署经 CF 的自建前置（`Front-CF`）；ex02 仍按 2026-11-17 已部署配置运行，ex01 未动。

## 为什么这样设计

- **两跳**：固定 ISP（如 Webshare 静态住宅/ISP 代理）提供干净、稳定的出口 IP；机场节点负责把流量送出去（宿主所在网络直连 ISP 代理不稳定或不可达）。
- **pf 兜底而不是代理规则兜底**：物理网卡只放行到入口的 443。mihomo 停了、规则写错了，流量都出不去，只会断网。
- **自建前置为主、机场为备**：自建前置（VPS 上的 VLESS + REALITY，见 [front.md](front.md)）自己可控、每台环境一个 UUID；
  机场两个入口做备用。`fallback` 组按顺序自动切换，三个入口都在 pf 白名单里。前置失效时自动切到机场，出口 IP 不变（实测）。
- **Stripe 分流**：部分代理商应 Stripe 要求屏蔽 Stripe（返回 `403` + `X-Webshare-Reason: client_connect_forbidden_host`），因此让相关域名走独立出口。默认从选中的前置直出；`tmh-chain` 经机场再到 FrontVPS。付款出口与普通网站的固定 ISP 仍是不同 IP/地点，连通性验收不代表账号或 KYC 验收。
- **tart**：开源、基于 macOS 自带虚拟化框架，命令行可脚本化，能随机序列号和网卡地址，能克隆快照。

## 客机上的文件

| 位置 | 来源模板 |
|---|---|
| `/usr/local/<TAG>/config.yaml` | `guest/config.yaml` |
| `/usr/local/<TAG>/pf.conf` | `guest/pf.conf` |
| `/usr/local/<TAG>/timesync.sh`、`mihomo` | `guest/timesync.sh`；mihomo 二进制另取 |
| `/Library/LaunchDaemons/net.<TAG>.{pf,mihomo,timesync}.plist` | `guest/net.TAG.*.plist` |
| `/Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist` | 同名模板；以图形用户运行以支持剪贴板，二进制另取 |
| `/etc/ssh/sshd_config.d/100-<TAG>.conf` | `setup-root.sh` 生成 |
| `/etc/sudoers.d/claude` | `setup-root.sh` 生成 |

## 用户进去看到什么

开机自动登录 `claude`，没有首次设置引导（`setup-user.sh` 预写 SetupAssistant 标记），桌面小组件隐藏，
Safari 自动打开 `/usr/local/<TAG>/check.html`：从浏览器的角度检查出口 IP（两个来源）、出口运营商、IP 所在时区、
浏览器时区和语言、WebRTC 泄漏，全部绿色才提示可以登录。系统设置的提醒角标由 `net.<TAG>.nobadge` 每 5 分钟清一次；
苹果系统更新服务器在 mihomo 里被拒绝，不会出现「有可用更新」。检查页还会核对出口 IP 的运营商（ASN）。

## 依赖

这台环境能不能用，取决于下面这些外部东西。任何一项失效，结果都是**断网**（按设计），不会泄漏；
`host/watch-egress.sh` 每小时检查一次出口，失效时弹通知。

| 依赖 | 作用 | 失效会怎样 | 要盯的 |
|---|---|---|---|
| 固定 ISP 代理（如 Webshare 静态 ISP） | 提供普通网站的固定出口 IP（Stripe 另行分流） | 普通请求失败；代理商替换 IP 时出口会**静默变成别的 IP** | 到期续费；套餐流量通常是所有 IP 共用，超额全停；IP 被替换要重选出口、重建环境 |
| 前置节点（自建 VPS 为主、机场为备） | 把客机流量送到 ISP；Stripe 默认从所选前置直出，链式模式则经机场到 FrontVPS | 默认模式在入口全部失效时断网；链式模式中 FrontVPS 失效即 Stripe 不通，机场也失效则普通 ISP 链路亦受影响 | VPS 和机场到期续费；入口 IP 变了要重新渲染部署（pf 白名单跟着变）；VPS 上的 xray 不能随手升级（见 front.md） |
| Cloudflare | DNS（1.1.1.1 DoH）、对时（HTTPS Date）、代理组健康检查、验收 | DNS 和对时一起失效；出口 IP 被它拦时整体不可用 | 无费用，出问题时先查这个 |
| ipinfo.io、api.ipify.org、Google STUN | 检查页和 `verify.sh` 的比对数据 | 检查页报红（不代表环境有问题，先看是哪一项） | — |
| 宿主 Mac | 跑虚拟机 | 宿主关机 = 环境不可用 | 断电后自动开机、自动登录（不能开 FileVault）、外置盘挂载、桥接网卡 `BRIDGE_IF` 在线 |
| 家里路由器 | 给客机分固定局域网 IP | IP 变了 SSH 找不到客机（环境本身照常） | 从快照重建后网卡地址变了，要重新绑定 |

**多台环境共用的部分**：网站看到的只有最后一跳出口 IP，每台不同；但前置节点和代理商账户是共用的。
Stripe 默认从所选前置直出；链式模式从 FrontVPS 出站。因此使用同一末端服务器的环境付款出口 IP 相同，切换机场前置不会让链式模式的最终出口改变；普通网站的固定 ISP 出口另行核对。
这是有意接受的代价：Stripe 不允许走 ISP 代理，也不能改走本地家宽（地点差得更远，而且 pf 要放开直连，断网保护就没了）。
前置不通时 Stripe 也不通，等恢复再付款。

### 已测试的版本

| 组件 | 版本 | 备注 |
|---|---|---|
| xray-core（前置服务端） | 26.3.27 | 26.10.21 起与 mihomo 的 REALITY 握手失败，见 front.md |
| tart | 2.41.1 | 宿主 |
| 客机镜像 | `ghcr.io/cirruslabs/macos-tahoe-vanilla:26.6.2` | macOS 26.6.2 |
| mihomo | Meta v1.19.19 darwin-arm64（官方 `mihomo-darwin-arm64-v1.19.19.gz`） | `.gz` sha256 `29fdde431e69e17f4e8e7cda2046402c4cba254a26ad66ce17e37ad946490dc0`；解压后二进制 sha256 `c6f6dab68c9ea54b754d3f1544aa1835f4d3913726aed96914bd23b182ecbb47`（官方哈希，待新机 `verify.sh --fail-closed` 复验） |
| tart-guest-agent | 0.15.0 | sha256 `8bbbffe09d207164cbabc06a2b2f5085e041290e75247d28db0a0e1fa852646e` |

换版本后要在实际环境跑一遍 `verify.sh --fail-closed` 再更新这张表。

## 已知限制

- **开机瞬间的空窗**：2026-11-15 ex01 冷启动实测，网卡拿到 DHCP 地址（04:12:55.464）比 pf 加载（.568）早约 0.1 秒。两者都是开机自启服务，macOS 不保证先后。这时还没有用户登录，可能出网的只有系统服务【推断】。修法待定（候选：让网卡在 pf 加载后才启用）。
- **不自动更新系统**：更新服务器被拒绝，命令行在 macOS 26 上也关不掉自动检查。要更新时在宿主下载完整安装器、校验后拷进客机安装。

- 机型显示为 `VirtualMac2,1`。网页一般读不到，但装进虚拟机的 App 可能读到。
- 静态 ISP 代理的 IP 常被 ip-api 一类数据库标为 proxy。验收和检查页会核对出口 IP 的运营商编号（ASN），确认还是选定的那家 ISP，没被换成机房 IP。
- **各家 IP 地理库可能对同一出口给出不同地点**。ex01 的出口（2026-11-16 实测）：ipinfo 费城、ip-api 和 db-ip 新泽西（美东），ipapi.is 和 ip2location 萨克拉门托、ip.net.coffee 后端洛杉矶（美西）。客机时区只能对上其中一边，第三方检测页可能报「时区不一致」。Anthropic、Stripe 用哪家库不知道。本人决定暂不换出口，保持纽约时区。
- 网站看到的 DNS 解析服务器是 Cloudflare，不是出口 ISP 自己的。家宽用户用 1.1.1.1 很常见，风险低。
- 用 NAT 做初始设置期间，客机经宿主网络连过苹果服务器（激活、更新检查），苹果能看到宿主的真实 IP。这一阶段不登录任何账号。
- ISP 代理链路带宽可能很低（实测约 100 KB/s）。大的公开安装包在宿主下载、核对 sha256 后 `scp` 进客机。
- ISP 代理套餐通常有流量上限，超额可能全停；系统自动更新要关。
- 依赖宿主在线：宿主关机 = 环境不可用。宿主需开「断电后自动开机」、自动登录，虚拟机放外置盘时还要等盘挂载（`host/run-vm.sh` 已处理）。
