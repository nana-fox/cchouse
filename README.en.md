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

> This is the target flow. The one-command installer (`cchouse.py up`) is still in development; until it is released, follow the [manual install](docs/manual-install.md) (Chinese).

1. **Install the basics and the VM software** (one-time)
   - Mac: [Homebrew](https://brew.sh), Git and Python 3. Then on Apple Silicon: `brew install cirruslabs/cli/tart`; on Intel Mac: free VMware Fusion Pro (requires a Broadcom account to download).
   - Windows (x64): [Git for Windows](https://git-scm.com/download/win), Python 3 (run it as `py -3` in PowerShell), the OpenSSH client (Settings → Optional features), CPU virtualization enabled in BIOS, then free VMware Workstation Pro (requires a Broadcom account to download).
2. **Run one command and paste your proxy link** (first install takes about `<COLD_TOTAL>` including the image download; cold-start measurement pending. Image version, size and SHA-256: [guest-linux/image-manifest.json](guest-linux/image-manifest.json))
   ```bash
   git clone https://github.com/nana-fox/cchouse.git && cd cchouse
   python3 scripts/cchouse.py up      # Windows PowerShell: py -3 scripts/cchouse.py up
   ```
3. **Open the VM desktop and confirm all network checks pass**, then sign in using the browser inside the VM. The page checks the network only, not your account. Open the Claude Code sign-in link only in the VM's browser, never on your host.

---

## Supported computers

| Your computer | VM software | Status |
|---|---|---|
| Apple Silicon Mac | Tart | ✅ Verified on real hardware (2026-11-25, Mac mini) |
| Windows (x64) | VMware Workstation Pro | ✅ Verified on real hardware (2026-11-25) |
| Intel Mac | VMware Fusion Pro | 🟡 Supported, same image as Windows, not yet verified separately |

The guest is always Debian 13 + Chromium + Claude Code. Linux hosts are not supported.

On 2026-11-25, two real machines (Mac mini with Tart, Windows with VMware) passed all 26 automated checks.

First-install time (cold start: nothing downloaded yet): Mac `<COLD_MAC>`, Windows `<COLD_WIN>`. Measurement pending. Image version, download size and SHA-256 are listed in [guest-linux/image-manifest.json](guest-linux/image-manifest.json).

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
- **Downloads are slow.** Download the image on your host first, then import it offline following the manual install.
- **Something failed.** Run `python3 scripts/doctor.py --env <path to instance.env> --vm-ssh` to export a diagnostic zip. It is redacted automatically, but **unzip and review it before attaching it to a public [issue](https://github.com/nana-fox/cchouse/issues)**.
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
