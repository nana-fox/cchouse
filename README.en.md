# cchouse

**A clean, stable, leak-proof environment dedicated to Claude.**
cchouse runs a small isolated virtual machine on your computer. You use Claude on the web and Claude Code inside it, and all of its traffic goes only through your own proxy node. If that node drops, the whole machine goes offline. It will not quietly fall back to your computer's own network.

[![CI](https://github.com/nana-fox/cchouse/actions/workflows/ci.yml/badge.svg)](https://github.com/nana-fox/cchouse/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nana-fox/cchouse?include_prereleases&label=release)](https://github.com/nana-fox/cchouse/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[中文](README.md) | English

![After the automated checks pass, open the network check page on the VM desktop and sign in only after every check passes (example image; the IP is a documentation sample address)](docs/assets/check-all-green.png)

---

## Who it's for

You already have your own proxy node and want to use Claude (the website and Claude Code) steadily, without worrying about:

1. **A drifting IP**: one region today, another tomorrow.
2. **An environment that does not match**: the IP is in the United States, but the computer's time zone is Beijing and the system language is Chinese.
3. **A proxy that fails open**: for the few seconds the node is down, software quietly uses your computer's own network and your real IP leaks.

cchouse uses only the one node you configure, sets the VM's time zone, language, and region to match where that node is, and stays offline when the node is down. It does not switch to another path on its own.

---

## How it works

![An isolated VM inside your computer reaches Claude only through your proxy node. If the node goes offline, the VM goes offline.](docs/assets/how-it-works.en.svg)

- Inside the VM, only traffic to your one node is allowed. Every other path is blocked.
- Your computer itself keeps using the internet as usual.

---

## Quick start

The installer downloads and verifies the matching guest image and official Claude Code package. [Image manifest](guest-linux/image-manifest.json)

### Windows (x64)

Open Windows PowerShell and run this one line to install `v2.0.0-preview.3`.

```powershell
$f = "$env:TEMP\cchouse-bootstrap-$([guid]::NewGuid().ToString('N')).ps1"; iwr https://github.com/nana-fox/cchouse/releases/download/v2.0.0-preview.3/bootstrap-windows.ps1 -UseBasicParsing -OutFile $f -ErrorAction Stop; if ((Get-FileHash $f -Algorithm SHA256).Hash -ne 'd7cb9db54405efa36d8f0624f691a455fe52128d72e08eb9ad2238450ce39576') { throw 'SHA-256 mismatch' }; Unblock-File -LiteralPath $f; powershell -NoProfile -ExecutionPolicy RemoteSigned -File $f -CleanupBootstrap
```

When prompted, paste your node link. The input is hidden. If VirtualBox is missing, the installer installs it automatically. Click **Yes** once when Windows asks for User Account Control (UAC) approval.

### Apple Silicon Mac

Requires Apple Silicon (M series) and macOS 13.0 Ventura or later. Open Terminal and run this one line to install `v2.0.0-preview.3`. The bootstrap verifies and prepares a private Python and Tart if needed.

```bash
f=$(mktemp -t cchouse-bootstrap) && curl -fsSLo "$f" https://github.com/nana-fox/cchouse/releases/download/v2.0.0-preview.3/bootstrap-macos.sh && printf '%s  %s\n' '60a6fe7495320cb61f487ff99bf2063e56c5c23cdcc18f3b477bfaf3f0654f9f' "$f" | shasum -a 256 -c - && /bin/bash "$f" --cleanup-bootstrap
```

When prompted, paste your SOCKS5 or HTTP(S) node link. The input is hidden.

### Open the guest and sign in

The installer configures the guest and runs automated checks, then saves a clean snapshot. If a step fails, fix the issue shown and rerun your platform's original command. Verified downloads and completed steps are reused.

After the automated checks pass, open the network check page on the VM desktop (the picture above). After every network check passes, sign in to Claude in **the browser inside the VM**. That page checks the network only. It does not say anything about the account.

> The authorization link shown when you sign in to Claude Code must also be opened in the browser inside the VM. Do not copy it into the browser on your own computer.

For AI-assisted installation, see [AGENTS.md](AGENTS.md). For manual configuration, see the [advanced installation guide](docs/manual-install.md).

---

## Supported computers

| Your computer | VM software | Status |
|---|---|---|
| Apple Silicon Mac (M series) | Tart | ✅ Public `v2.0.0-preview.2` cold install finished on a Mac mini |
| Windows (x64) | VirtualBox | ✅ `v2.0.0-preview.3` passed two public blind installs on a real Windows 11 PC |
| Intel Mac | — | Not supported for now |

The guest is always Debian 13 + Chromium + Claude Code. A Linux computer is not supported as the host.

**First install with an empty cache** (includes the image and Claude Code downloads)

- **Apple Silicon Mac**: a public `v2.0.0-preview.2` empty-cache install on a Mac mini took **2 minutes 15 seconds**, covering the download, automated verification, and saving a clean snapshot. The download went through the host proxy. The image averaged 20.67 MiB/s, and Claude Code averaged 16.31 MiB/s.
- **Windows 11 (x64)**: two public `v2.0.0-preview.3` blind installs on a real PC, each starting with an empty cache and no VirtualBox, took **10 minutes 21 seconds** and **9 minutes 13 seconds** inside the installer, covering the automatic VirtualBox install, downloads, import, automated verification, and saving a clean snapshot.

---

## What you need

- **One SOCKS5 or HTTP(S) node**. Bring a node with a fixed IP and a stable region. See the [advanced proxy guide](docs/proxy-modes.md) for VLESS, Trojan, Shadowsocks, and two-hop routing.
- **The computer**: 16 GB of memory recommended. Allow at least **7 GB** of free space on Windows (**10 GB** recommended), or at least **9 GB** on Apple Silicon (**12 GB** recommended), including the downloads.

> **About Stripe**: some nodes block the payment service Stripe. That only affects paying on the claude.ai website. It does not affect signing in or day-to-day use. The installer warns you and does not stop the install. The check only tests whether Stripe can be reached. It does not mean a payment will succeed. You can add a node used only for payment. See [docs/proxy-modes.md](docs/proxy-modes.md).

---

## FAQ

<details>
<summary><b>Could this get my account banned?</b></summary>

**No guarantee.** Whether an account stays in good standing depends on many things we do not control.

What we do: traffic goes only through the one node you configure; time zone, language, and region stay consistent with the exit IP; if the node drops, the network drops, and the real IP does not leak; the website and Claude Code both run in the same VM, using only the official website and official Claude Code.

What we do not do: we do not provide a node; we do not spoof a browser fingerprint; we do not sign up, sign in, or pay for you; we do not promise any account outcome.

</details>

<details>
<summary><b>Why a VM, and not a fingerprint browser or a proxy turned on directly?</b></summary>

- **A proxy turned on directly**: when the proxy app drops, many programs fall back to the computer's own network. Command-line tools often ignore the system proxy too. The computer's time zone and language stay local.
- **A fingerprint browser**: it only covers the browser. It does not cover a command-line tool such as Claude Code.
- **A VM**: the whole machine's network is locked at the system level and can only use your node. The website and the command line share one exit, and the environment is configured together with it.

</details>

<details>
<summary><b>Stripe is unreachable. Can I pay?</b></summary>

An unreachable Stripe only affects paying on the claude.ai website. An existing subscription, or a payment made somewhere else, is unaffected. The installer warns you and does not block the install. The check only tests whether Stripe can be reached. It does not mean a payment will succeed. To pay inside the VM, add a node used only for payment. See [docs/proxy-modes.md](docs/proxy-modes.md).

</details>

<details>
<summary><b>Downloads are very slow from inside China. What then?</b></summary>

Installer downloads follow the proxy settings of the host network. When a download is slow, pass `--image-mirror <mirror directory URL>` to use a mirror. The file is still checked against the SHA-256 in the manifest. You can also download the image on the host first and import it offline. See [manual install](docs/manual-install.md).

</details>

<details>
<summary><b>Clash TUN is on, and the first exit check fails?</b></summary>

Wait 1–2 minutes, then rerun the same command from Quick start. The installer resumes and runs verification again. If the guest is already shut down, start that same guest in the VM app first.

If it still fails, check whether the node is reachable and export a diagnostic package using the instructions below.

</details>

<details>
<summary><b>Something went wrong during install. What now?</b></summary>

Use the Python and source that the bootstrap prepared to export a redacted diagnostic package. Follow the instructions for [Mac](docs/operations.md#mac-一键安装后的诊断与卸载) or [Windows](docs/operations.md#windows-一键安装后的诊断与卸载).

**Unzip and check the package before you open a public issue.** Confirm that no credentials or personal information remain, then attach it to an [issue](https://github.com/nana-fox/cchouse/issues). See [docs/operations.md](docs/operations.md).

</details>

---

## Uninstall

Wait for installation to finish, then follow the `remove` instructions for [Mac](docs/operations.md#mac-一键安装后的诊断与卸载) or [Windows](docs/operations.md#windows-一键安装后的诊断与卸载). Use the original instance tag and include the original `--private-dir` if you supplied one.

The installer lists the VM, clean snapshot, private folder, and temporary plaintext it can verify before asking for confirmation. Enter cancels by default. Use `--yes` to skip confirmation. On VirtualBox, uninstall powers the VM off and then runs `VBoxManage unregistervm --delete` for the VM whose name matches the install record exactly. If an older installation lacks a record that verifies ownership, check the VM in your virtualization app and follow the manual cleanup instructions.

The golden image and downloads stay in place by default so other instances can reuse them. If you no longer need them, add `--purge-cache` to list the associated Tart golden image and recognized downloads with their sizes for removal. The installer reports what remains and how to clean it up manually.

## Security design

For people who want the details:

1. **One account, one machine, one exit**: one environment signs in to one account and uses only the one node you configure.
2. **A failure means offline**: if the proxy stops or the config is wrong, the result can only be no network. It never falls back to a direct connection from the computer. The guest firewall denies everything by default and allows only the proxy process to reach the one address of your node. Direct rules are not allowed in the proxy config, and the scripts reject them. UDP and IPv6 are blocked.
3. **The environment matches the exit**: time zone, language, region, and keyboard follow where the exit is. Automated verification checks that the exit IP's time zone matches the VM's time zone.
4. **Closed inside the VM**: only the official website and official Claude Code are used. The sign-in authorization link is opened only in the VM's browser.
5. **Credentials stay out of the repo**: node details live only in a private directory on your computer (readable only by you). Plaintext intermediate files are deleted after use. The repo's commit hook blocks content that looks like credentials.

We will **never**: bundle a node, relax the offline protection in order to "get the network back", or ship a desktop GUI program.

Architecture: [docs/architecture.md](docs/architecture.md). Verification items: [docs/verify.md](docs/verify.md).

---

## Disclaimer

cchouse is only a local environment setup tool. It **does not guarantee account safety**, and it does not provide any proxy service. Follow the laws where you are, and the terms of Anthropic and of the services you use.

## License · Contributing · Changelog

- License: [MIT](LICENSE)
- Contributing: issues and pull requests are welcome. After you change a template or a script, run `python3 -I -m unittest discover -s tests` and add a note in [CHANGELOG.md](CHANGELOG.md). Development rules: [AGENTS.md](AGENTS.md).
- Changelog: [CHANGELOG.md](CHANGELOG.md)
