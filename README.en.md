# cchouse

**A clean, stable, leak-proof environment dedicated to Claude.**
cchouse runs a small isolated virtual machine on your computer. You use Claude on the web and Claude Code inside it, and all its traffic goes through your own proxy server (node). If the proxy drops, the whole VM goes offline instead of quietly falling back to your local network.

[![CI](https://github.com/nana-fox/cchouse/actions/workflows/ci.yml/badge.svg)](https://github.com/nana-fox/cchouse/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nana-fox/cchouse?include_prereleases&label=release)](https://github.com/nana-fox/cchouse/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[中文](README.md) | English

![After the automated checks pass, open the network check page on the VM desktop and confirm all network checks pass before signing in (example; the IP is a documentation address)](docs/assets/check-all-green.png)

---

## Who it's for

You already have your own proxy server and want to use Claude (web + Claude Code) day to day without worrying about:

1. **A changing public IP**: one region today, another region tomorrow.
2. **A mismatched environment**: a US IP on a machine set to another time zone and language.
3. **Silent fallback**: the proxy drops for a few seconds, apps switch to your local network, and your real IP leaks.

cchouse uses only the one proxy you configure, sets the VM's time zone, language and region to match where it is, and stays offline when the proxy is down.

---

## How it works

```mermaid
flowchart LR
    A["Your computer"] --> B["Isolated VM<br/>(Claude runs only here)"]
    B --> C["Your own proxy server"]
    C --> D["Claude"]
    B -. "any link breaks" .-> X["The whole VM goes offline<br/>never falls back to your local network"]
```

The VM can only reach your one proxy server. Your computer keeps using the internet as usual.

---

## Quick start

This guide is for image version `v2.0.0-preview.2`. The installer uses the repository's manifest to download and verify the image. [Image sizes and SHA-256 checksums](guest-linux/image-manifest.json)

1. **Install the basics and the VM software** (one-time)
   - On Apple Silicon Mac, install [Homebrew](https://brew.sh), Git, and Python 3, then run `brew install cirruslabs/cli/tart`.
   - On Windows (x64), install [Git for Windows](https://git-scm.com/download/win), Python 3 (run it as `py -3` in PowerShell), and the OpenSSH client (Settings → Optional features). Enable CPU virtualization in BIOS and install VMware Workstation Pro (download requires a Broadcom account). The installer uses its `vmrun.exe` and `ovftool.exe`.
2. **Run the installer and paste a single-proxy link**
   ```bash
   git clone https://github.com/nana-fox/cchouse.git
   cd cchouse
   python3 -I scripts/cchouse.py up --tag cc01
   ```

   In Windows PowerShell, run `py -3 -I scripts/cchouse.py up --tag cc01`.

   The link is hidden as you paste it. The installer downloads and verifies the image and official Claude Code package over the host network while it configures the VM and checks prerequisites. On Apple Silicon Mac and Windows, it imports or clones the image, injects the configuration offline, removes the plaintext staging files, reboots, and runs `verify-linux.sh --fail-closed`. After the checks pass, it saves a clean snapshot. [Manual install (Chinese)](docs/manual-install.md)

   If a step fails, address the reported error and rerun the same command from the same directory. Keep the same `--tag` and any custom directory options to reuse completed steps and verified downloads.
3. **Open the VM desktop and confirm all network checks pass**, then sign in using the browser inside the VM. The page checks the network only, not your account. Open the Claude Code sign-in link only in the VM's browser, never on your host.

---

## Supported computers

| Your computer | VM software | Status |
|---|---|---|
| Apple Silicon Mac | Tart | ✅ `preview.2` candidate passed the VM checks on a Mac mini |
| Windows (x64) | VMware Workstation Pro | ✅ Tested on real hardware. `preview.2` first-install timing pending |
| Intel Mac | — | Currently unsupported |

The guest is always Debian 13 + Chromium + Claude Code. Linux hosts are not supported.

**First-install timing with an empty cache** (includes image and Claude Code downloads)

- On **Apple Silicon Mac**, allow about 3–5 minutes through the automated checks. A Mac mini took 3 minutes 43 seconds with `preview.1`. The roughly 757 MB image downloaded over the host proxy at about 7 MiB/s. That image lacked the shutdown permissions, so the run stopped at verification without saving a snapshot.
- **Windows** timing is `<COLD_WIN>` (measurement pending).

---

## What you need

- **One proxy server** (cchouse doesn't provide one): SOCKS5, HTTP(S), VLESS, Trojan or Shadowsocks. UDP-based protocols such as Hysteria2, TUIC and WireGuard are not supported.
- **16 GB+ RAM and 25 GB+ free disk** recommended.

> **Stripe**: some proxies block Stripe. That only affects paying on claude.ai. Installation warns you but doesn't stop. The check only tests connectivity; it doesn't guarantee a payment will go through. See [docs/proxy-modes.md](docs/proxy-modes.md) for an optional payment-only proxy and the advanced two-hop chain mode.

---

## FAQ

- **Will my account get banned?** No guarantee. We use only the proxy you configure, keep the environment consistent with it, block leaks, and use only the official web app and Claude Code. We don't provide proxies, spoof fingerprints, or sign up, sign in or pay for you.
- **Why a VM instead of a fingerprint browser or a plain proxy?** A plain proxy often falls back to direct traffic and CLI tools may ignore it. A fingerprint browser (a browser that manages separate browser profiles and settings) only covers the browser, not Claude Code. A VM locks the network for the whole system, so web and CLI share one exit.
- **Stripe is blocked.** Only paying on claude.ai is affected, and the check only tests connectivity. Add a payment-only proxy if you need it.
- **Downloads are slow.** The installer uses the host's proxy settings. Pass `--image-mirror <mirror directory URL>` to use an image mirror while keeping SHA-256 verification. You can also download the image on the host and import it offline. [Manual install (Chinese)](docs/manual-install.md)
- **The first egress check fails with Clash TUN enabled on the host.** Wait 1–2 minutes and rerun `verify-linux.sh --fail-closed` using the same instance configuration. If it keeps failing, add a direct rule for your proxy node’s IPv4 address in the host’s Clash rules, for example `IP-CIDR,<node IPv4>/32,DIRECT,no-resolve`, before the catch-all rule. Run verification again. Apply the rule on the host.
- **Something failed.** Export a redacted diagnostic zip using the generated `instance.env`. Add `--vm-ssh` to collect guest diagnostics if the VM has started. These examples use the default directory and tag `cc01`.

  ```bash
  python3 -I scripts/doctor.py --env "$HOME/Library/Application Support/cchouse/cc01/instance.env"
  ```

  ```powershell
  py -3 -I scripts/doctor.py --env "$env:LOCALAPPDATA\cchouse\cc01\instance.env"
  ```

  If setup stopped before creating `instance.env`, omit `--env` to collect host diagnostics.

  **Unzip and review the report before attaching it to a public issue.** [GitHub issues](https://github.com/nana-fox/cchouse/issues)
- **Why a Broadcom account for VMware?** VMware is free for personal use, but Broadcom requires an account to download it. We're looking at alternatives that don't need one. Tart doesn't need an account.

---

## Security design

One account, one VM, one exit. Default-deny firewall that only lets the proxy client reach your proxy server, with no direct rules and with UDP and IPv6 blocked, so any failure means offline. Time zone, language and region match the exit and are checked automatically. Everything, including the sign-in link, stays inside the VM. Credentials stay in a private local directory and never enter the repo.

We will **never** bundle proxy servers, relax the offline protection, or ship a desktop GUI app. Details: [docs/architecture.md](docs/architecture.md).

---

## Disclaimer

cchouse is a local environment setup tool. It **does not guarantee account safety** and provides no proxy service. You are responsible for following local laws and the terms of service of Anthropic and any service you use.

## License · Contributing · Changelog

[MIT](LICENSE) · Issues and PRs welcome (see [AGENTS.md](AGENTS.md)) · [CHANGELOG.md](CHANGELOG.md)
