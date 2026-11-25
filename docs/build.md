# 新建一台环境

每步做完核对再往下。标 🧑 的是必须由人做或确认的点。

## 0. 前提

- 已测试的版本见 [architecture.md#已测试的版本](architecture.md#已测试的版本)。
- Apple Silicon Mac，已装 tart（`brew install cirruslabs/cli/tart`，或官方 release 放外置盘 + `host/tart-wrapper.sh.example`）。
- 磁盘：每台约 30 GB（APFS 克隆共享块，实际增量更小）；本机内置盘空间不足或需多机测试时，可将 `TART_HOME` 指到外置 SSD（见 `host/tart-wrapper.sh.example`）。
- 准备好 `mihomo`（`darwin-arm64`）和 `tart-guest-agent` 二进制，并在宿主机解压后核对 [architecture.md#已测试的版本](architecture.md#已测试的版本) 中的官方 sha256：
  ```bash
  # 示例：下载已测版本并解压到临时目录，核对 sha256（以 architecture.md 为准）
  curl -fLO https://github.com/MetaCubeX/mihomo/releases/download/v1.19.19/mihomo-darwin-arm64-v1.19.19.gz
  gunzip -c mihomo-darwin-arm64-v1.19.19.gz > mihomo && chmod +x mihomo
  curl -fLO https://github.com/cirruslabs/tart-guest-agent/releases/download/v0.15.0/tart-guest-agent-darwin-all.tar.gz
  tar -xzf tart-guest-agent-darwin-all.tar.gz tart-guest-agent
  shasum -a 256 mihomo tart-guest-agent
  ```
- 复制旧实例参数后，逐项核对本机实际的 `TART_BIN`、`VM_STORAGE`、`RUN_LOG`、SSH 路径和 `NETWORK_MODE`。不要把另一台机器的外置盘等待路径带进来；ex02 曾因此在已有虚拟机文件的情况下等错目录。远程只部署运行所需文件，源码和操作手册留控制端。
- Git 中的脚本带可执行位，临时上传后仍可能变为普通文件。先检查权限；需要时用 `/bin/bash <脚本> <参数>` 执行，不能把 `Permission denied` 当成脚本已运行。桌面 `.command` 入口本身须可执行，并指向最终保留的运行文件。

## 1. 🧑 选出口和入口，填 instance.env

1. **出口**：一个没被别的账号用过的固定 ISP IP。先在客机外用 ipinfo / ip-api 查归属地、运营商、是否标为 proxy，把**归属地时区**记下来作为 `TIMEZONE`，ipinfo 的 `org` 第一段（如 `AS33659`）作为 `EXPECTED_EGRESS_ASN`。
   再多查几家地理库（ip-api、db-ip、ipapi.is、ip2location），**优先选各家结果都在同一时区的 IP**；有分歧的要记下来，见 [architecture.md#已知限制](architecture.md#已知限制)。
2. **入口**（v1 macOS 路线当前仅支持 `EXIT_MODE=chain`，`STRIPE_EXIT_MODE` 为 `front` 或 `tmh-chain`）：在自建前置上给这台环境加一个 UUID（`xray-front-client add <tag>`，REALITY 和经 Cloudflare 两个入口同时生效，见 [front.md](front.md)）；将 `FRONT_*` 与 `FRONT_CF_*`（服务器 IP、域名、WS 路径，取自部署前置时保存的私有配置记录，见 [front.md](front.md)）填进 `instance.env`，缺了 `render.py` 会直接报错；备用的两个机场入口（WS+TLS 一个、Reality 一个）。国内直连不通 FRONT 时，实际在用的是 `Front-CF`，部署后确认 `Fixed-ISP` 与 `Stripe` 两组选中的都是它。
3. 按 [README 快速上手](../README.md#快速上手命令行) 建私有目录，填 `instance.env`。`EXPECTED_EGRESS_IP` = 出口 IP。
4. 生成宿主到客机的 SSH 密钥：`ssh-keygen -t ed25519 -N '' -f "$D/id_ed25519"`。
5. 生成 claude 账户密码，存进私有目录（600）和密码管理器：
   `python3 -I -c 'import secrets,string as s;print("".join(secrets.choice(s.ascii_letters+s.digits) for _ in range(20)))' > "$D/claude-password.txt"`

## 2. 建虚拟机

```bash
. "$D/instance.env"
# 已测基础镜像版本见 architecture.md#已测试的版本
"$TART_BIN" clone ghcr.io/cirruslabs/macos-tahoe-vanilla:26.6.2 "$VM_NAME"
"$TART_BIN" set "$VM_NAME" --cpu 4 --memory 6144 --disk-size 80 --random-serial --random-mac
```

> **多机 / 重复测试省空间建议**：首次可先拉一份未改动的原版基础镜像留作本地底包（例如 `"$TART_BIN" clone ghcr.io/cirruslabs/macos-tahoe-vanilla:26.6.2 macos-tahoe-vanilla-26.6.2`），后续新建实例直接从本地底包 `tart clone`，借助 APFS 写时复制（CoW）几乎不额外占磁盘空间。
> **注意区分**：已配置完成并删除 `admin` 的成品快照（`CLEAN_SNAPSHOT`）以及已登录账号的恢复点（`RECOVERY_SNAPSHOT`）都**不能**当作新机原版底包来重跑本手册第 2–6 步。

## 3. 先用 NAT 模式做系统设置（不登录任何账号）

```bash
"$TART_BIN" run "$VM_NAME" &          # 默认 NAT，此时流量走宿主网络
"$TART_BIN" ip "$VM_NAME"             # 拿到 NAT IP；初始账户 admin / admin
```

这一步只做系统层面的事，**不登录任何账号、不打开 Claude**。这期间客机走宿主网络，苹果能看到宿主 IP，所以越短越好。

## 4. 部署客机文件

```bash
python3 -I scripts/render.py "$D/instance.env" "$D/rendered"
python3 -I scripts/make-kcpassword.py "$D/claude-password.txt" "$D/rendered/kcpassword"   # 自动登录用
cp "$D/id_ed25519.pub" "$D/rendered/authorized_keys"
cp <mihomo 二进制> <tart-guest-agent 二进制> "$D/rendered/"
# 先把宿主公钥放进 admin（初始密码 admin），后面都用密钥
ssh admin@<NAT IP> 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys' < "$D/id_ed25519.pub"
scp -i "$D/id_ed25519" -r "$D/rendered" admin@<NAT IP>:/tmp/$TAG
```

在客机里跑 `setup-root.sh`。**注意**：pf 一加载，加载前建立的 SSH 连接就收不到包了（pf 只给新连接建状态），
会话会卡住但脚本照常跑完。所以要放后台跑，再用新连接、以 `claude` 身份回来确认（ex01 是前台跑、卡住后用新连接确认的；下面的后台写法是据此整理的推荐做法）：

```bash
# 以 admin 登录，密码从标准输入给，不出现在命令行
ssh -i "$D/id_ed25519" admin@<NAT IP> \
  'PW=$(cat); echo admin | sudo -S -v; sudo -n nohup bash /tmp/<TAG>/setup-root.sh "$PW" > /tmp/setup-root.log 2>&1 &' \
  < "$D/claude-password.txt"
# 120 秒内（guard.sh 在倒计时）用新连接确认出口，对了再解除回滚：
ssh -i "$D/id_ed25519" claude@<NAT IP> 'curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E "^(ip|loc)="'
ssh -i "$D/id_ed25519" claude@<NAT IP> 'sudo -n touch /tmp/<TAG>/ok'
```

`setup-root.sh` 之后 SSH 只允许 `claude` 用密钥登录，admin 再也连不上，后面都用 `claude`。

```bash
# 以 claude 运行用户设置（跳过首次登录引导、隐藏桌面小组件、区域单位）——必须在 claude 第一次图形登录之前
ssh ... claude@<NAT IP> 'sudo -n cat /tmp/<TAG>/setup-user.sh | bash'
# 装 tart-guest-agent（原版镜像没有 /usr/local/bin）
ssh ... claude@<NAT IP> 'sudo -n install -d -m 755 /usr/local/bin &&
  sudo -n install -m 755 /tmp/<TAG>/tart-guest-agent /usr/local/bin/ &&
  sudo -n install -m 644 /tmp/<TAG>/org.cirruslabs.tart-guest-agent.plist /Library/LaunchAgents/'
# 必须在图形用户会话运行才能共享剪贴板；首次自动登录会加载这个 LaunchAgent。
# 如用户已经登录桌面，再通过 root 在对应 gui/<UID> 域 bootstrap；不要装到 system 域。
```

如果把已运行的 root 系统服务改成用户服务，停止并移除旧服务后正常重启客机，再检查 GUI agent、`tart exec ... id -un` 和双向剪贴板。ex02 改域后未重启时曾反复出现 `unexpected EOF`；重启后该错误消失。服务 running 仍不代替剪贴板实测，编码校准见 [verify.md](verify.md#剪贴板探针的编码与证据)。

## 5. 🧑 切桥接

若仅需在宿主使用客机、无需局域网其他设备直连客机，可保留 NAT：在实例参数写 `NETWORK_MODE=nat`，
跳过本节的桥接切换，之后仍用 `host/run-vm.sh` 启动。该模式下验收和关机会通过 Tart 查询当前客机地址。
未设置 `NETWORK_MODE` 的旧实例保持桥接行为。两种模式都必须完成网络和重启验收。

1. 此时仍是 NAT：用 `host/stop-vm.sh "$D/instance.env" <当前 NAT IP>` 关机，第二个参数只覆盖本次连接地址。不要拿桥接的 `GUEST_IP` 关机。**不要直接 `tart stop`**：它不等客机落盘，刚写的文件会丢（实测几十秒内装的文件重启后全没了）。脚本无法确认安全关机时退出并保留运行，改由客机桌面关机。
2. 之后都用 `host/run-vm.sh "$D/instance.env"` 启动（桥接 `BRIDGE_IF`）。
3. 客机拿到的局域网 IP：`"$TART_BIN" ip --resolver=arp "$VM_NAME"`，写进 `GUEST_IP`；建议在路由器里按网卡地址固定。
4. 开机应自动登录到桌面，Safari 自动打开出口检查页，全部绿色。

## 6. 删掉初始 admin

确认 `ssh -i "$D/id_ed25519" claude@$GUEST_IP` 能登录后：

```bash
ssh ... claude@$GUEST_IP 'PW=$(cat); sudo -n bash /tmp/<TAG>/remove-admin.sh "$PW"' < "$D/claude-password.txt"
ssh ... claude@$GUEST_IP 'sudo -n rm -rf /tmp/<TAG>; sync'   # 客机里的渲染文件含凭据
rm -rf "$D/rendered"
```

`remove-admin.sh` 会先用 admin 给 claude 授予 Secure Token（否则 admin 是唯一持有者，删不掉），再删 admin 和镜像自带的 `admin-nopasswd` sudo 规则。

## 7. 验收

```bash
scripts/verify.sh "$D/instance.env" --fail-closed
```

全部 PASS 后，🧑 按 [verify.md](verify.md) 做手工项。

交付桌面启停入口时，实际用入口完成一次正常关机和重新启动，再确认自动登录、固定出口、Safari 启动和中英文双向复制。ex02 使用的是手动桌面入口；这不等于安装了宿主开机自启。

## 8. 存干净快照

```bash
host/stop-vm.sh "$D/instance.env"
"$TART_BIN" clone "$VM_NAME" "$VM_NAME-clean-$(date +%Y%m%d)"
```

名字写进 `CLEAN_SNAPSHOT`。到这里为止，环境里没有登录过任何账号。

如果用户已经登录账号，保存的是该实例专用的恢复点，不能标为干净模板；用 `RECOVERY_SNAPSHOT` 单独记录，并保存源机的 `config.json`。Tart 克隆可能重新生成 MAC，恢复点应核对并保留源机配置；恢复已登录实例时不要随机化序列号或 MAC，也不要与原机同时运行。恢复点含登录状态，目录权限保持 700，磁盘和配置文件保持 600。

## 9. 开机自启（可选）

`~/Library/LaunchAgents/com.cchouse.<TAG>.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.cchouse.TAG</string>
<key>ProgramArguments</key><array>
  <string>/Users/你/projects/cchouse/host/run-vm.sh</string>
  <string>/Users/你/Library/Application Support/cchouse/TAG/instance.env</string>
</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><false/>
</dict></plist>
```

`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cchouse.<TAG>.plist`。
建议在 plist 里加 `StandardErrorPath`（如 `~/Library/Logs/cchouse-<TAG>.log`），自启失败时才看得到原因。
**`RUN_LOG` 不要放外置盘**：launchd 启动的 bash 没有外置盘写权限，会报 `Operation not permitted` 并退出（tart 自己读写外置盘上的虚拟机文件不受影响）。
LaunchAgent 要宿主用户登录后才跑：宿主需开自动登录（FileVault 开着时不能自动登录）。

## 10. 出口巡检（建议）

`host/watch-egress.sh` 每小时 SSH 进客机看一次出口，不对或断网就弹系统通知，日志在 `~/Library/Logs/cchouse-<TAG>-watch.log`。
LaunchAgent `~/Library/LaunchAgents/com.cchouse.<TAG>.watch.plist` 写法同第 9 步，`ProgramArguments` 用
`/bin/bash`、`host/watch-egress.sh`、`instance.env` 三项，加 `<key>StartInterval</key><integer>3600</integer>`。
脚本里的 SSH 带 `-F none`：launchd 下读不到 `~/.ssh/config` 里 Include 的文件会直接失败（实测）。

## 两个坑

- TUN 的 `strict-route` 要关，同时局域网网段写进 `route-exclude-address`，否则 SSH 回不来。
- `config.yaml` 开头 `---` 前面不能有任何内容，否则 TUN 不生效。
