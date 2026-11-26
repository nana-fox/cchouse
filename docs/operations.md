# 日常调整

所有会动 pf / mihomo 的操作都按这个顺序：

```bash
# 宿主
python3 -I scripts/render.py "$D/instance.env" "$D/rendered"
scp -i "$D/id_ed25519" "$D/rendered/"{config.yaml,pf.conf,guard.sh} claude@$GUEST_IP:/tmp/$TAG/
# 客机（ssh 进去）
test ! -d /usr/local/<TAG>/guard.lock || { echo '已有 guard，请先核实'; exit 1; }
sudo rm -f /tmp/<TAG>/ready
nohup sudo bash /tmp/<TAG>/guard.sh >/dev/null 2>&1 &     # 120 秒内不 touch ok 就回滚
GUARD_PID=$!
for i in $(seq 1 100); do
  kill -0 "$GUARD_PID" 2>/dev/null || { echo 'guard 未能启动'; exit 1; }
  test -f /tmp/<TAG>/ready && break
  sleep 0.1
done
test -f /tmp/<TAG>/ready || { echo 'guard 备份未就绪'; exit 1; }
sudo install -m 600 /tmp/<TAG>/config.yaml /tmp/<TAG>/pf.conf /usr/local/<TAG>/
sudo pfctl -nf /usr/local/<TAG>/pf.conf && sudo pfctl -f /usr/local/<TAG>/pf.conf
sudo launchctl kickstart -k system/net.<TAG>.mihomo
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc)='   # 出口对了再：
touch /tmp/<TAG>/ok
# 等 guard.sh 退出再清理：它到点只认 /tmp/<TAG>/ok，提前删了目录就会回滚（实测踩过）
while pgrep -f <TAG>/guard.sh >/dev/null; do sleep 5; done; sudo rm -rf /tmp/<TAG>
# 宿主
scripts/verify.sh "$D/instance.env"
rm -rf "$D/rendered"
```

| 想做什么 | 改什么 |
|---|---|
| 换出口 IP | `instance.env` 的 `WEBSHARE_*`、`EXPECTED_EGRESS_IP`；**出口所在地变了就同步改 `TIMEZONE`**，再在客机 `sudo systemsetup -settimezone <TZ>`。跨地区建议从干净快照重建，而不是改旧机 |
| 自建前置换 IP / 换参数 | 改 `instance.env` 的 `FRONT_*`（含经 Cloudflare 入口的 `FRONT_CF_*`），重新渲染部署；新环境加 UUID 见 [front.md](front.md) |
| 机场入口换 IP | 客机会整体断网（按设计）。改 `NODE_*_SERVER_IP` 等参数，重新渲染部署；pf 白名单自动跟着变 |
| 某网站被代理商拦 | 先确认是代理商拦截（Webshare：`403` + `X-Webshare-Reason: client_connect_forbidden_host`），再在 `guest/config.yaml` 的 rules 里把该域名分到入口组。入口已在 pf 白名单，pf 不用改。分流期间开 `host/monitor.sh` 核对 |
| 共享文件夹 | 编辑 `SHARED_DIRS_FILE`（`名字|Mac路径|ro`），关机重开生效；客机里在 `/Volumes/My Shared Files/名字` |
| 大文件安装包 | 宿主下载、对照官方 sha256，`scp` 进客机再装 |
| 关机 | 用 `host/stop-vm.sh`，不要直接 `tart stop`（不等落盘，刚写的文件会丢） |
| 系统更新 | 苹果更新服务器在规则里被拒绝。要更新：宿主下载完整安装器并校验，`scp` 进客机安装；装完跑 `verify.sh` |
| 出口巡检通知 | 先看 `~/Library/Logs/cchouse-<TAG>-watch.log`；「出口变了」先别登录账号，查代理商是不是换了 IP；「断网」查前置和代理是否到期 |
| 看流量走哪条线 | `host/monitor.sh "$D/instance.env"` |

### 仅 Stripe 改为 FrontVPS 链式出口

在私有 `instance.env` 设置 `STRIPE_EXIT_MODE='tmh-chain'`，前提是 [front.md](front.md) 中服务端已接受空 SNI，且两条“机场 → FrontVPS”探针都成功。渲染后先做 mihomo `-t` 检查，按上面的 guard 流程部署；只改 config.yaml 时不必重载未变化的 pf。

验收要分开看：普通请求仍是固定 ISP；分别通过两条链查询公网 IP 都是 FrontVPS；正式 Stripe 域名请求在代理连接记录中命中 Stripe 组和 FrontVPS 链式成员，TLS 校验正常且资源返回 200。临时测试规则必须在确认前移除，核对最终配置哈希及 pf 未变，全部通过才确认 guard；等待 guard 退出后清理临时材料。`import-existing.py` 会识别模式并验证生成的链式块，意外改动不会被静默当成模板参数导入。

## 从快照重建

```bash
host/stop-vm.sh "$D/instance.env"; "$TART_BIN" delete "$VM_NAME"
"$TART_BIN" clone "$CLEAN_SNAPSHOT" "$VM_NAME"
"$TART_BIN" set "$VM_NAME" --random-serial --random-mac
host/run-vm.sh "$D/instance.env" &
scripts/verify.sh "$D/instance.env" --fail-closed
```

随机网卡地址变了，路由器里的固定 IP 要按新地址重新绑定。

## 丢了什么怎么恢复

| 丢了 | 怎么办 |
|---|---|
| 私有目录（`instance.env`、SSH 密钥、claude 密码） | 凭据从密码管理器重填 `instance.env`；SSH 密钥重新生成后，要在客机桌面上用 claude 密码登录把新公钥写进 `~/.ssh/authorized_keys`；密码管理器里也没有 claude 密码就只能重建 |
| 虚拟机磁盘（含已登录的账号状态） | 从干净快照重建，账号在同一环境里重新登录 |
| 干净快照 | 按 build.md 重建；快照和虚拟机放在同一块外置盘上，盘坏了两个一起没，重要时另备一份 |

## 模板和线上对账

```bash
mkdir -p "$D/live" && scp -i "$D/id_ed25519" claude@$GUEST_IP:'/usr/local/<TAG>/{config.yaml,pf.conf,timesync.sh}' "$D/live/"
# 其余文件同理取回后：
python3 -I scripts/import-existing.py "$D/live" <TAG>
```

没有报错 = 模板结构和线上一致；输出的参数可与 `instance.env` 对比。

`guard.sh` 会清掉上次的确认标记，完成旧配置备份后才写 `ready`。同一实例只运行一个 guard。
如果强制终止进程或重启后留下 `guard.lock`，先确认没有 guard 在运行，再手工移除空锁目录；不要跳过备份直接改网络。

## 出错怎么导诊断包

部署失败、断网、验收不通过或需要把现场发给维护者 / AI 协助排错时，在宿主机仓库根目录一键导出脱敏诊断包（Windows / macOS / Linux 通用）：

```bash
# 仅采集宿主信息 + preflight 自检（客机尚未启动或 SSH 不可达时可用）
python3 -I scripts/doctor.py --env "$D/instance.env"

# 同时尝试通过 SSH 拉取客机 firstboot.log、verify-linux.sh --json、mihomo 状态与 nftables 规则
python3 -I scripts/doctor.py --env "$D/instance.env" --vm-ssh

# 可选：指定输出路径
python3 -I scripts/doctor.py --env "$D/instance.env" --vm-ssh --out "$HOME/Desktop/cchouse-doctor.zip"
```

Windows PowerShell 示例：

```powershell
python -I scripts/doctor.py --env "$env:LOCALAPPDATA\cchouse\<TAG>\instance.env" --vm-ssh
```

**包内内容与安全说明**：

- 诊断包默认包含 `manifest.json`（采集清单与缺失原因）、`host-info.json`（系统版本、CPU 架构、`tart`/`VBoxManage`/SSH 版本）、`preflight.json` 与仅含非敏感结构字段的 `env-summary.json`。
- 加 `--vm-ssh` 时会额外尝试采集 `guest/firstboot.log`、`guest/verify-linux.json`、`guest/mihomo-status.txt` 与 `guest/nft-ruleset.txt`；若客机不可达或某项无权限读取，脚本依然正常生成 zip，并在 `manifest.json` 的 `missing` 字段写明缺哪一项及原因。
- **不会打包 `instance.env` 原文或私钥文件**：所有写入 zip 的文本与 JSON 都会经过统一脱敏，自动抹除 `instance.env` 中的密码、UUID、密钥、节点地址、用户名/密码 URL、Authorization 头与 PEM 私钥块。即便如此，发给外部前仍建议先粗略扫一眼包内文件确认无额外个人备注。

## Windows 一键安装后的诊断与卸载

在 PowerShell 中使用引导脚本准备的 Python 和源码。把下面的 `<工作目录>` 换成安装器输出的工作目录。默认实例名为 `cc01`。

```powershell
$root = Join-Path $env:LOCALAPPDATA 'cchouse\bootstrap'
$python = Get-ChildItem -Path (Join-Path $root 'tools\python-*-embed-amd64\python.exe') -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
$source = '<工作目录>'

# 导出脱敏诊断包
& $python -I (Join-Path $source 'scripts\doctor.py') --env "$env:LOCALAPPDATA\cchouse\cc01\instance.env"

# 预览并卸载实例
& $python -I (Join-Path $source 'scripts\cchouse.py') remove --tag cc01
```

实例尚未生成 `instance.env` 时，导出诊断包可省略 `--env` 参数。卸载自定义实例时使用实际 `--tag` 和原 `--private-dir`。

## 远程部署排障与交接

以下来自 [ex02 实测记录](records/2026-11-16-ex02.md)，适用于控制端经 UU 维护其他用户宿主、再由宿主 SSH 进入客机的流程。

| 现象 | 已有证据与处理 | 下次的判定规则 |
|---|---|---|
| UU 显示映射成功，SSH 卡在 banner exchange | 其他用户本机回环 sshd 能返回主机密钥；转发请求能到达远端，但控制端收不到初始响应。重启控制端 UU 后曾恢复命令、退出码及 SFTP 哈希校验，之后又复发。内部原因未确认 | GUI 的“成功”只算映射状态。用短超时 SSH 输出和退出码验收，传文件再比 SHA256；分清监听、握手、认证、执行四层。已有证据相同时不反复新建端口、换路由或重配宿主网络 |
| 用户在 UU 里无法操作鼠标，连接后直接看到虚拟机 | 只发 Control-Option-Return、看到捕获提示消失，未恢复用户的实际控制；切为“使用主控端鼠标”后用户确认恢复 | 先问清主控端、其他用户宿主、客机哪一层不能动。减少桌面接入，停止旧 agent 的输入；以用户实际移动和点击为准。修改只限当次远控输入方式，不改宿主安全设置 |
| 旧会话一直显示 Working，runtime 却显示 done | 对照 tab → pane → terminal handle 及进程命令行，找到准确的卡住进程，停止后保留标签页与历史 | 不凭单一状态认定仍在执行或已完成。只处理目标会话，不关闭整个工作区，不误杀其他 Codex。中断无效时先核实精确进程身份，再处理 |
| 截图工具结果挤满终端，旧会话请求变得很大 | 记录中出现把图像 base64 当普通文本输出；旧会话曾收到大体积请求的 HTTP 400。截图是否是唯一原因未证实 | 图像按工具的图像块展示，文本只留摘要。优先用 SSH 读日志，不 stringify 整份图像结果，不靠反复截图取数 |
| 宿主多行脚本只跑了前半段却退出 0 | 外层 SSH 从 stdin 读取脚本，内层 `guest()` 的 SSH 又消费同一个 stdin，余下命令被读走 | 只读 guest 调用加 `</dev/null`；有意传数据的调用保留 stdin。复杂脚本先上传再执行，或用正确 shell 引用的 `bash -c`。必须出现末尾结果标记才能认定整段执行完 |
| 改好 guest-agent 后剪贴板仍失败 | 从 root system 服务改到图形用户服务后，重启清除了 `unexpected EOF`；随后又排除了 `C` 编码导致的探针误判 | 先检查用户域与 RPC 身份，再看连接日志、重启加载、两端本地往返和 UTF-8 双向内容，不重复安装已正常的服务 |
| 克隆了已登录机器，却准备标成 clean | 用户已登录账号；Tart 克隆还可能更换 MAC | 先确认是否已登录；已登录的只存实例恢复点，保存源配置并核对身份、权限，恢复时不随机化身份。不能并行启动源机和恢复点 |
| 交接时为了重复日志回传又耗在转发恢复上 | ex02 验收已经通过，重复日志回传再次卡在 SSH 握手，拖住交还 | 先保存最少结果、执行临时访问清理并确认，再关闭映射和远控；完整复盘回本地做。用户要求停止时不继续重连或补测 |

### 收尾顺序与状态口径

1. 用最终入口完成必要的启停、网络和剪贴板测试，留下结果摘要。账号登录、付款由人完成，不为补验收重做这些操作。
2. 通道仍可用时，核对本任务临时 sshd 与 caffeinate 的准确 PID，撤销临时授权、停止对应进程；确认回环 SSH 端口关闭。只处理本任务的文件和进程，保留宿主原有代理、锁屏和其他远控配置。
3. 运行文件、私有实例参数、客机维护密钥、密码和恢复点按职责保留；删除已验证不再依赖的渲染目录、下载包和手册副本。guard 必须已退出且运行文件已持久化，才能删部署临时目录。
4. 关闭 UU 临时映射，删除控制端临时维护私钥，断开目标设备；用映射开关、本地端口和 `device status` 分别验收。不要将主控端持有的整个 UU 账号退出或删除。
5. 记录三种独立状态：环境验收、远程访问关闭、远端临时进程/文件清理。关闭映射不等于远端进程退出；最后一项无法确认时保留实情，不自动重新接入。密码库锁定时保留受限私有密码文件，不谎称已归档。

### 下一次的精简流程（规划，尚未脚本化）

这次规则已回写，但“临时拼装宿主引导、逐条远程执行、末尾人工收拢”尚未变成可重复执行的部署入口。提速优先复用现有 `render.py`、客机 setup、`run-vm.sh`、`stop-vm.sh` 和 `verify.sh`，加一层按实例参数串联的薄脚本，不增加远控服务或维护系统。私有实例目录中本次一次性 `bootstrap-nat.sh` 属于历史材料，含旧 system guest-agent 安装方式和实例专属 PID，不能直接作为下一台的模板。

1. **预检并准备**：一次校验宿主路径、磁盘、权限、参数、二进制版本及哈希，生成最小运行包、最终桌面入口和临时访问清理动作。先约定 NAT、账号登录与快照边界，凭据归档尽量在交接前完成。镜像下载期间做控制端渲染和配置检查，只在进度变化或失败时取摘要。
2. **连续安装并可续接**：沿用已验收的连接批量执行；每一阶段写一个结果标记，重复进入先检查标记与实际状态，失败只补当前阶段。避免每一条状态查询都新建 SSH 握手，也不把镜像下载、安装队列或已通过的检查整段重跑。宿主静态信息一次采集，外网与断网探针仍保留必要的独立验证。
3. **合并验收和快照启停**：完整 25 项与编码校准后的剪贴板检查集中做一轮；将保存快照的正常关机与桌面关闭入口验收合并，保存后通过桌面启动入口恢复，再只核对自动登录、服务、固定出口及剪贴板。没有新改动或失败依据时，不再整轮复跑 22/25 项。已登录快照保存源身份，未登录快照才可标 clean。
4. **先交还再本地复盘**：保留最少验收摘要，通道仍在时完成远端临时服务撤权与确认，再关闭映射、删除控制端临时钥匙、断远控。重复日志回传不作为交还条件；项目复盘和 ai-life 登记全部在本地完成。

以上是待实施的串联方案，不能称为已有“一键部署”。下一次用各阶段开始/完成时间区分镜像下载等待、机器执行、人工介入和排障耗时；有记录后再评估节省量，不预先承诺总耗时。
