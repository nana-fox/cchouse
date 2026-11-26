"""VirtualBox backend selection and installer manifest. Never installs VirtualBox."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse as cchouse_mod  # noqa: E402
import downloads  # noqa: E402
import preflight as preflight_mod  # noqa: E402

MANIFEST = ROOT / "guest-linux" / "virtualbox-manifest.json"
PS1 = ROOT / "scripts" / "vm-virtualbox.ps1"
PINNED_SHA = "afd0ff1656cbe04ed88ce619594ae021419d97cce89fa9bdc443e9a963f1713f"
PINNED_URL = "https://download.virtualbox.org/virtualbox/7.2.22/VirtualBox-7.2.22-175245-Win.exe"
# SHA-256 of VBoxGuestAdditions_7.2.22.iso from the 7.2.22 SHA256SUMS. The Win.exe line in that file matches PINNED_SHA.
PINNED_GA_SHA = "fc41354d8b974051fa33ca5a4f9c999a58c6e0dfc595b02a924a544392234b3d"
PINNED_GA_URL = "https://download.virtualbox.org/virtualbox/7.2.22/VBoxGuestAdditions_7.2.22.iso"


def _single_fields():
    keys = (
        "SINGLE_TYPE", "SINGLE_SERVER", "SINGLE_SERVER_IP", "SINGLE_PORT", "SINGLE_USER", "SINGLE_PASS",
        "SINGLE_UUID", "SINGLE_SNI", "SINGLE_FINGERPRINT", "SINGLE_FLOW", "SINGLE_NETWORK", "SINGLE_WS_PATH",
        "SINGLE_WS_HOST", "SINGLE_PUBLIC_KEY", "SINGLE_SHORT_ID", "SINGLE_CIPHER", "SINGLE_TLS", "SINGLE_HOST_KEY",
    )
    fields = {key: "" for key in keys}
    fields["SINGLE_TYPE"] = "trojan"
    fields["SINGLE_SERVER"] = "203.0.113.9"
    fields["SINGLE_SERVER_IP"] = "203.0.113.9"
    fields["SINGLE_PORT"] = "443"
    return fields


class _Body:
    def __init__(self, body):
        self._buf = io.BytesIO(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestVirtualBoxSelection(unittest.TestCase):
    def test_windows_auto_selects_virtualbox_route(self):
        with mock.patch.object(cchouse_mod.platform, "system", return_value="Windows"):
            route = cchouse_mod.detect_route()
            self.assertEqual(route, "virtualbox-linux")
            self.assertEqual(cchouse_mod.detect_vm_backend(route), "virtualbox")

    def test_windows_auto_refuses_to_reuse_vmware_install_state(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "cc05"
            private.mkdir()
            state = private / "state.json"
            original = json.dumps({"vm_created_by_up": True, "vm_backend": "vmware", "vm_name": "claude-cc05"})
            state.write_text(original, encoding="utf-8")
            args = types.SimpleNamespace(
                tag="cc05", route="auto", private_dir=str(private), cache_dir=str(Path(td) / "cache"),
                dry_run=False, vm_backend="auto",
            )
            with mock.patch.object(cchouse_mod.platform, "system", return_value="Windows"), \
                    mock.patch.object(cchouse_mod, "run_pipeline") as pipeline, \
                    self.assertRaisesRegex(SystemExit, "请使用新的 --tag"):
                cchouse_mod.cmd_up(args)
            pipeline.assert_not_called()
            self.assertEqual(state.read_text(encoding="utf-8"), original)

    def test_windows_auto_leaves_legacy_instance_with_only_saved_vm_path(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "cc05"
            private.mkdir()
            env = private / "instance.env"
            original = 'VMX_PATH="C:/old/claude-cc05.vmx"\n'
            env.write_text(original, encoding="utf-8")
            args = types.SimpleNamespace(
                tag="cc05", route="auto", private_dir=str(private), cache_dir=str(Path(td) / "cache"),
                dry_run=False, vm_backend="auto",
            )
            with mock.patch.object(cchouse_mod.platform, "system", return_value="Windows"), \
                    mock.patch.object(cchouse_mod, "run_pipeline") as pipeline, \
                    self.assertRaisesRegex(SystemExit, "请使用新的 --tag"):
                cchouse_mod.cmd_up(args)
            pipeline.assert_not_called()
            self.assertEqual(env.read_text(encoding="utf-8"), original)

    def test_other_hosts_stay_on_their_routes(self):
        self.assertEqual(cchouse_mod.detect_vm_backend("tart-linux", system="Darwin"), "tart")
        self.assertEqual(cchouse_mod.detect_vm_backend("tart-linux", system="Windows"), "manual")
        self.assertEqual(cchouse_mod.detect_vm_backend("virtualbox-linux", system="Darwin"), "manual")
        self.assertEqual(cchouse_mod.detect_vm_backend("virtualbox-linux", system="Linux"), "manual")

    def test_explicit_backend_is_not_overridden(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "cc05"
            args = types.SimpleNamespace(
                tag="cc05", route="virtualbox-linux", private_dir=str(private), cache_dir=str(Path(td) / "cache"),
                dry_run=True, config_only=False, reconfigure=False, no_probe=True, link_stdin=True,
                non_interactive=True, timezone=None, expected_ip=None, image_mirror=None, npm_mirror=None,
                vm_backend="manual",
            )
            ctx = cchouse_mod.UpContext(args, "cc05", "virtualbox-linux", private)
            self.assertEqual(ctx.backend, "manual")
            self.assertIsNone(ctx.vbox_manifest)

    def test_virtualbox_steps_use_the_virtualbox_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "cc05"
            args = types.SimpleNamespace(
                tag="cc05", route="virtualbox-linux", private_dir=str(private), cache_dir=str(Path(td) / "cache"),
                dry_run=True, config_only=False, reconfigure=False, no_probe=True, link_stdin=True,
                non_interactive=True, timezone=None, expected_ip=None, image_mirror=None, npm_mirror=None,
                vm_backend="virtualbox",
            )
            ctx = cchouse_mod.UpContext(args, "cc05", "virtualbox-linux", private)
            ctx.vbox_port_is_free = lambda _port: True
            calls = []
            ctx.virtualbox = lambda op, **kw: calls.append(("virtualbox", op, kw))
            ctx.tart = lambda *a: calls.append(("tart", a))
            cchouse_mod.step_import(ctx)
            cchouse_mod.step_firstboot(ctx)
            cchouse_mod.step_boot(ctx)
            cchouse_mod.step_snapshot(ctx)
            ops = [item[1] for item in calls if item[0] == "virtualbox"]
            self.assertEqual(ops, ["install", "import", "firstboot", "boot", "snapshot"])
            imported = next(item for item in calls if item[1] == "import")
            booted = next(item for item in calls if item[1] == "boot")
            expected = str(cchouse_mod.vbox_preferred_ssh_port("cc05"))
            self.assertEqual(imported[2]["SshPort"], str(ctx.vbox_ssh_port))
            self.assertEqual(imported[2]["SshPort"], expected)
            self.assertEqual(booted[2]["SshPort"], expected)

    def test_apply_net_keeps_router_ip(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "instance.env"
            env.write_text(
                "VMNET_CIDR=192.168.180.0/24\nGUEST_IP=192.168.180.128\nGUEST_IF=ens33\nROUTER_IP=192.168.100.1\n",
                encoding="utf-8",
            )
            ctx = types.SimpleNamespace(
                backend="virtualbox", env_path=env, tag="cc01", vbox_port_is_free=lambda _port: True,
            )
            cchouse_mod.apply_virtualbox_guest_net(ctx)
            text = env.read_text(encoding="utf-8")
            preferred = cchouse_mod.vbox_preferred_ssh_port("cc01")
            self.assertIn("VMNET_CIDR=10.0.2.0/24\n", text)
            self.assertIn("GUEST_IP=127.0.0.1\n", text)
            self.assertIn("GUEST_IF=enp0s3\n", text)
            self.assertIn(f"SSH_PORT={preferred}\n", text)
            self.assertEqual(ctx.vbox_ssh_port, preferred)
            self.assertIn("ROUTER_IP=192.168.100.1\n", text)
            self.assertNotIn("192.168.180", text)

    def test_apply_skips_occupied_preferred_port(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "instance.env"
            env.write_text("ROUTER_IP=192.168.100.1\nVMNET_CIDR=10.0.2.0/24\n", encoding="utf-8")
            preferred = cchouse_mod.vbox_preferred_ssh_port("cc06")
            ctx = types.SimpleNamespace(
                backend="virtualbox", env_path=env, tag="cc06",
                vbox_port_is_free=lambda port: port != preferred,
            )
            cchouse_mod.apply_virtualbox_guest_net(ctx)
            nxt = preferred + 1 if preferred < cchouse_mod.VBOX_SSH_PORT_BASE + cchouse_mod.VBOX_SSH_PORT_SPAN - 1 else cchouse_mod.VBOX_SSH_PORT_BASE
            text = env.read_text(encoding="utf-8")
            self.assertEqual(ctx.vbox_ssh_port, nxt)
            self.assertIn(f"SSH_PORT={nxt}\n", text)
            self.assertIn("ROUTER_IP=192.168.100.1\n", text)

    def test_resolve_keeps_existing_free_port_and_wraps(self):
        preferred = cchouse_mod.vbox_preferred_ssh_port("cc01")
        saved = 2300 if preferred != 2300 else 2301
        kept = cchouse_mod.resolve_vbox_ssh_port("cc01", current=saved, is_free=lambda _port: True)
        self.assertEqual(kept, saved)
        self.assertNotEqual(saved, preferred)
        busy = cchouse_mod.resolve_vbox_ssh_port("cc01", current=saved, is_free=lambda port: port != saved)
        self.assertNotEqual(busy, saved)
        self.assertEqual(busy, preferred if preferred != saved else (preferred + 1 if preferred < 2421 else 2222))
        wrapped = cchouse_mod.choose_vbox_ssh_port("cc01", occupied=(2421,), is_free=lambda _port: True, preferred=2421)
        self.assertEqual(wrapped, 2222)
        skipped = cchouse_mod.choose_vbox_ssh_port("cc01", occupied=(2222,), is_free=lambda _port: True, preferred=2222)
        self.assertEqual(skipped, 2223)
        with self.assertRaises(RuntimeError):
            cchouse_mod.choose_vbox_ssh_port("cc01", preferred=22, is_free=lambda _port: True)
        with self.assertRaises(RuntimeError):
            cchouse_mod.choose_vbox_ssh_port(
                "cc01", occupied=range(2222, 2422), is_free=lambda _port: True, preferred=2222,
            )

    def test_preferred_ports_are_stable_and_spread(self):
        tags = [f"cc{i:02d}" for i in range(1, 17)]
        ports = [cchouse_mod.vbox_preferred_ssh_port(tag) for tag in tags]
        self.assertGreater(len(set(ports)), 1)
        for tag, port in zip(tags, ports):
            self.assertEqual(cchouse_mod.vbox_preferred_ssh_port(tag), port)
            self.assertGreaterEqual(port, cchouse_mod.VBOX_SSH_PORT_BASE)
            self.assertLess(port, cchouse_mod.VBOX_SSH_PORT_BASE + cchouse_mod.VBOX_SSH_PORT_SPAN)

    def test_format_adds_ssh_port_only_for_virtualbox(self):
        net = {"vmnet_cidr": "10.0.2.0/24", "guest_ip": "127.0.0.1", "router_ip": "192.168.100.1", "home_lan_cidr": "192.168.100.0/24"}
        egress = {"timezone": "America/Los_Angeles", "ip": "203.0.113.9", "loc": "US", "asn": "AS1"}
        fields = _single_fields()
        with self.assertRaises(RuntimeError):
            cchouse_mod.format_instance_env(
                "cc01", "virtualbox-linux", "/tmp/cc", "/tmp/k", "ssh-ed25519 AAAA x", fields, net, egress, backend="virtualbox",
            )
        vbox = cchouse_mod.format_instance_env(
            "cc01", "virtualbox-linux", "/tmp/cc", "/tmp/k", "ssh-ed25519 AAAA x", fields, net, egress,
            backend="virtualbox", ssh_port=2301,
        )
        tart = cchouse_mod.format_instance_env(
            "cc01", "tart-linux", "/tmp/cc", "/tmp/k", "ssh-ed25519 AAAA x", fields, net, egress, backend="tart",
        )
        self.assertIn("SSH_PORT=2301\n", vbox)
        self.assertIn("GUEST_IF=enp0s3\n", vbox)
        self.assertNotIn("SSH_PORT=", tart)
        self.assertIn("GUEST_IF=enp0s1\n", tart)
        self.assertIn("ROUTER_IP=192.168.100.1\n", vbox)


class TestVirtualBoxAutoinstall(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.installer = Path(temp.name) / "VirtualBox.exe"
        self.installer.write_bytes(b"synthetic installer")
        self.manifest = {
            "version": "7.2.22",
            "sha256": hashlib.sha256(self.installer.read_bytes()).hexdigest(),
            "url": PINNED_URL,
        }
        self.ctx = types.SimpleNamespace(
            backend="virtualbox", dry_run=False, state={},
            vbox_manifest=self.manifest, vbox_installer=self.installer,
            wait_download=mock.Mock(), virtualbox=mock.Mock(), save_state=mock.Mock(),
        )

    def test_missing_virtualbox_verifies_and_announces_before_launch(self):
        out = io.StringIO()

        def at_launch(*args, **kwargs):
            self.assertIn("将安装 Oracle VirtualBox 7.2.22", out.getvalue())
            self.assertIn("UAC", out.getvalue())

        self.ctx.virtualbox.side_effect = at_launch
        with mock.patch.object(cchouse_mod, "vboxmanage_present", return_value=False), mock.patch("sys.stdout", out):
            cchouse_mod._ensure_virtualbox_installed(self.ctx)
        self.ctx.wait_download.assert_called_once_with("virtualbox")
        self.ctx.virtualbox.assert_called_once_with("install", Installer=self.installer, Sha256=self.manifest["sha256"])

    def test_existing_virtualbox_skips_download_and_install(self):
        with mock.patch.object(cchouse_mod, "vboxmanage_present", return_value=True):
            cchouse_mod._ensure_virtualbox_installed(self.ctx)
        self.ctx.wait_download.assert_not_called()
        self.ctx.virtualbox.assert_not_called()

    def test_bad_hash_stops_before_execution_and_keeps_instance_unclaimed(self):
        self.installer.write_bytes(b"tampered")
        with mock.patch.object(cchouse_mod, "vboxmanage_present", return_value=False):
            with self.assertRaisesRegex(cchouse_mod.StepError, "校验") as failure:
                cchouse_mod.step_import(self.ctx)
        self.assertIn(PINNED_URL, str(failure.exception))
        self.ctx.virtualbox.assert_not_called()
        self.ctx.save_state.assert_not_called()
        self.assertEqual(self.ctx.state, {})

    def test_download_or_uac_failure_returns_official_link_without_retrying_install(self):
        for fail_at in ("wait_download", "virtualbox"):
            with self.subTest(fail_at=fail_at):
                self.ctx.wait_download.reset_mock(side_effect=True)
                self.ctx.virtualbox.reset_mock(side_effect=True)
                getattr(self.ctx, fail_at).side_effect = cchouse_mod.StepError("synthetic failure")
                with mock.patch.object(cchouse_mod, "vboxmanage_present", return_value=False), mock.patch("sys.stdout", io.StringIO()):
                    with self.assertRaisesRegex(cchouse_mod.StepError, "synthetic failure") as failure:
                        cchouse_mod.step_import(self.ctx)
                self.assertIn(PINNED_URL, str(failure.exception))
                self.assertEqual(self.ctx.virtualbox.call_count, int(fail_at == "virtualbox"))
                self.ctx.save_state.assert_not_called()


class TestVirtualBoxManifest(unittest.TestCase):
    def test_shipped_manifest_matches_pinned_oracle_package(self):
        got = downloads.load_virtualbox_installer(MANIFEST)
        self.assertEqual(got["version"], "7.2.22")
        self.assertEqual(got["build"], "175245")
        self.assertEqual(got["filename"], "VirtualBox-7.2.22-175245-Win.exe")
        self.assertEqual(got["sha256"], PINNED_SHA)
        self.assertEqual(got["size_bytes"], 178101352)
        self.assertEqual(got["url"], PINNED_URL)
        self.assertFalse(got["extension_pack"])
        self.assertFalse(got["redistribute"])
        self.assertIn("ADDLOCAL=VBoxApplication", got["silent_args"])
        ga = got["guest_additions"]
        self.assertEqual(ga["filename"], "VBoxGuestAdditions_7.2.22.iso")
        self.assertEqual(ga["sha256"], PINNED_GA_SHA)
        self.assertEqual(ga["size_bytes"], 53207040)
        self.assertEqual(ga["url"], PINNED_GA_URL)
        self.assertNotEqual(ga["sha256"], PINNED_SHA)

    def test_guest_additions_rejects_add_on_and_missing_block(self):
        packed = self._mutate(guest_additions={
            "filename": "VBoxGuestAdditions_7.2.22.iso",
            "sha256": PINNED_GA_SHA,
            "size_bytes": 53207040,
            "url": "https://download.virtualbox.org/virtualbox/7.2.22/Oracle_VirtualBox_Extension_Pack-7.2.22.vbox-extpack",
        })
        with self.assertRaises(downloads.DownloadError):
            self._load_data(packed)
        missing = self._mutate()
        missing.pop("guest_additions")
        with self.assertRaises(downloads.DownloadError):
            self._load_data(missing)

    def test_amd64_ova_installs_guest_additions_and_drops_the_compiler(self):
        golden = (ROOT / "guest-linux" / "image-builder" / "build-debian13-golden.sh").read_text(encoding="utf-8")
        ova = (ROOT / "guest-linux" / "image-builder" / "build-ova-from-cloud-image.ps1").read_text(encoding="utf-8")
        tart = (ROOT / "guest-linux" / "image-builder" / "build-tart-linux-from-cloud-image.sh").read_text(encoding="utf-8")
        aes = (ROOT / "guest-linux" / "image-builder" / "assert-empty-state.sh").read_text(encoding="utf-8")
        doc = (ROOT / "docs" / "install-virtualbox-linux.md").read_text(encoding="utf-8")
        nft = (ROOT / "guest-linux" / "nftables.conf").read_text(encoding="utf-8")
        self.assertIn('if [ "$MIHOMO_ARCH" = "amd64" ]', golden)
        self.assertIn("VBoxLinuxAdditions.run", golden)
        self.assertIn("--nox11", golden)
        self.assertIn("cchouse_unit_dropin vboxadd.service oracle", golden)
        self.assertIn("cchouse_unit_dropin open-vm-tools.service vmware", golden)
        self.assertIn("VBoxClient --clipboard", golden)
        self.assertIn("VBoxClient --vmsvga", golden)
        self.assertNotIn("--draganddrop", golden)
        self.assertNotIn("--seamless", golden)
        self.assertIn("apt-get purge -y build-essential dkms", golden)
        self.assertIn("unattended-upgrades", golden)
        self.assertIn("spice-vdagent", golden)
        self.assertNotIn("modprobe", golden)
        self.assertIn("ConditionVirtualization=oracle", aes)
        self.assertIn("ConditionVirtualization=vmware", aes)
        self.assertIn("modinfo", aes)
        self.assertNotIn("modprobe", aes)
        self.assertIn("unattended-upgrades", aes)
        self.assertIn('cchouse_image_arch" = "amd64"', aes)
        self.assertIn("Get-CchouseGuestAdditionsIso", ova)
        self.assertIn("VBOX_GUEST_ADDITIONS_SHA256=$gaSha", ova)
        self.assertIn("Remove-Item -LiteralPath $gaIso", ova)
        self.assertIn("拒绝 VirtualBox 附加包", ova)
        self.assertNotIn("Oracle_VirtualBox_Extension_Pack", ova)
        self.assertNotIn(".vbox-extpack", ova)
        self.assertNotIn("VBOX_GUEST_ADDITIONS", tart)
        self.assertNotIn("不安装 Guest Additions", doc)
        self.assertIn("unattended-upgrades", doc)
        self.assertIn("不锁定内核包", doc)
        self.assertNotIn("flush ruleset", golden)
        self.assertNotIn("flush ruleset", ova)
        self.assertIn("绝不执行 flush ruleset", nft)
        self.assertNotIn("virtualbox", (ROOT / "guest-linux" / "image-manifest.json").read_text(encoding="utf-8"))

    def _mutate(self, **changes):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data.update(changes)
        return data

    def _load_data(self, data):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "virtualbox-manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return downloads.load_virtualbox_installer(path)

    def test_rejects_bad_sha_foreign_url_name_and_add_on(self):
        bad_sha = self._mutate(sha256="not-a-sha256")
        with self.assertRaises(downloads.DownloadError):
            self._load_data(bad_sha)
        foreign = self._mutate(url="https://example.com/virtualbox/7.2.22/VirtualBox-7.2.22-175245-Win.exe")
        with self.assertRaises(downloads.DownloadError):
            self._load_data(foreign)
        renamed = self._mutate(filename="VirtualBox-9.9.9-1-Win.exe")
        with self.assertRaises(downloads.DownloadError):
            self._load_data(renamed)
        addon = self._mutate(extension_pack=True)
        with self.assertRaises(downloads.DownloadError):
            self._load_data(addon)
        packed = self._mutate(url="https://download.virtualbox.org/virtualbox/7.2.22/Oracle_VM_VirtualBox_Extension_Pack-7.2.22.vbox-extpack")
        with self.assertRaises(downloads.DownloadError):
            self._load_data(packed)
        bridged = self._mutate(silent_args=["--silent", "--msiparams", "ADDLOCAL=VBoxApplication,VBoxNetworkFlt"])
        with self.assertRaises(downloads.DownloadError):
            self._load_data(bridged)

    def test_require_sha256_rejects_local_tamper_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "VirtualBox.exe"
            dest.write_bytes(b"tampered-installer")
            with self.assertRaises(downloads.DownloadError):
                downloads.require_sha256(dest, PINNED_SHA)
            self.assertTrue(dest.exists())
            digest = hashlib.sha256(b"tampered-installer").hexdigest()
            self.assertEqual(downloads.require_sha256(dest, digest), digest)

    def test_fetch_verified_rejects_bad_bytes_without_a_real_download(self):
        body = b"not-the-oracle-installer"
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "VirtualBox.exe"
            with self.assertRaises(downloads.DownloadError):
                downloads.fetch_verified(
                    [PINNED_URL], dest, sha256=PINNED_SHA, size=len(body),
                    opener=lambda url, start, timeout: _Body(body), retries=0,
                )
            self.assertFalse(dest.exists())
            self.assertFalse(Path(str(dest) + ".part").exists())


class TestVirtualBoxScript(unittest.TestCase):
    def test_ps1_is_nat_only_and_uses_system32_ssh(self):
        src = PS1.read_text(encoding="utf-8")
        self.assertIn("ADDLOCAL=VBoxApplication", src)
        self.assertIn("127.0.0.1", src)
        self.assertIn("windows-hostkey.ps1", src)
        self.assertIn("CchouseSshWaitSeconds", src)
        self.assertNotIn("ssh-keyscan", src)
        self.assertNotIn("StrictHostKeyChecking=no", src)
        self.assertIn("emptydrive", src)
        self.assertNotIn("extpack", src.lower())
        self.assertNotIn("Extension_Pack", src)
        self.assertNotIn("extension pack", src.lower())
        self.assertNotIn(r"Program Files\OpenSSH", src)
        self.assertNotIn("& ssh.exe", src)
        self.assertNotIn("flush ruleset", src.lower())
        self.assertIn('^storagecontrollername\\d+="([^"]*IDE[^"]*)"', src)
        self.assertNotIn("$Matches[1] -match", src)
        self.assertNotIn("$CcSshPort = 2222", src)
        self.assertIn("CcSha256 is required", src)
        self.assertIn("firstboot shutdown timed out", src)
        boot = src.split("function Invoke-CchouseVBoxBoot", 1)[1].split("function New-CchouseVBoxSnapshot", 1)[0]
        self.assertLess(boot.find("Resolve-CchouseHostKeyTrust"), boot.find("Start-CchouseVmDetached"))
        self.assertLess(boot.find("Write-CchouseHostKeyTrustMark"), boot.find("Start-CchouseVmDetached"))
        self.assertIn("未经串口核对主机密钥", boot)
        self.assertIn("Resolve-CchouseProbeDecision", boot)
        self.assertLess(boot.index("Resolve-CchouseProbeDecision"), boot.index("Get-CchouseSshVerifyOptions"))
        delete = src.split("function Remove-CchouseVBoxVM", 1)[1].split("function New-CchouseVBoxSnapshot", 1)[0]
        self.assertLess(delete.find("poweroff"), delete.find("unregistervm"))
        self.assertIn("--delete", delete)
        self.assertIn("-cne $Name", delete)
        self.assertIn('"delete" { Remove-CchouseVBoxVM -Name $CcVmName -VmDir $CcVmDir }', src)
        firstboot = src.split("function Invoke-CchouseVBoxFirstboot", 1)[1].split("function Invoke-CchouseVBoxBoot", 1)[0]
        self.assertLess(firstboot.find("Enable-CchouseVBoxSerialLog"), firstboot.find("Start-CchouseVmDetached"))
        self.assertLess(src.rfind("Resolve-CchouseFirstbootStop"), src.find("HARD_POWEROFF=1"))
        self.assertLess(src.find("HARD_POWEROFF=1"), src.find("FIRSTBOOT_STOPPED=OK"))
        self.assertNotIn("Wait-CchousePowerOff $Name 20 | Out-Null", src)

    def test_preflight_reports_virtualbox_route_and_amd64_image(self):
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "preflight.py"), "--route", "virtualbox-linux"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["route"], "virtualbox-linux")
        self.assertIn("vboxmanage_found", payload["host"])
        self.assertEqual(payload["host"]["expected_image"]["filename"], "cchouse-debian13-amd64.ova")

    def test_windows_hostkey_comes_from_serial_not_keyscan(self):
        helper = (ROOT / "scripts" / "windows-hostkey.ps1").read_text(encoding="utf-8")
        vbox = (ROOT / "scripts" / "vm-virtualbox.ps1").read_text(encoding="utf-8")
        firstboot = (ROOT / "guest-linux" / "firstboot.sh").read_text(encoding="utf-8")
        uploader = (ROOT / "tools" / "e2e" / "run-gtr.sh").read_text(encoding="utf-8")
        for name, src in (("helper", helper), ("virtualbox", vbox), ("firstboot", firstboot)):
            self.assertNotIn("ssh-keyscan", src, name)
        self.assertIn(r"C:\Windows\System32\OpenSSH\ssh.exe", helper)
        self.assertIn("$script:CchouseSshWaitSeconds = 180", helper)
        self.assertIn("accept-new", helper)
        self.assertIn("function Write-CchouseHostKeyTrustMark", helper)
        self.assertIn("hostkey.trust", helper)
        self.assertIn("ConnectionAttempts=1", helper)
        self.assertIn("ConnectTimeout=$ConnectTimeout", helper)
        self.assertNotIn("StrictHostKeyChecking=no", helper)
        ssh_fn = helper.split("function Invoke-CchouseSshTrue", 1)[1].split("function ", 1)[0]
        self.assertLess(ssh_fn.find("Test-CchouseHostKeyMismatch"), ssh_fn.find("return"))
        verify = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        self.assertIn('warn "未经串口核对主机密钥"', verify)
        self.assertIn("hostkey.trust", verify)
        self.assertLess(verify.find("warn_unverified_hostkey"), verify.find('log "-- 规则红线与服务"'))
        self.assertIn("CCHOUSE_HOSTKEY", firstboot)
        self.assertIn("/dev/ttyS0", firstboot)
        self.assertIn("timeout 2", firstboot)
        self.assertIn("ssh_host_ed25519_key.pub", firstboot)
        self.assertNotIn("ssh_host_ed25519_key >/dev/ttyS0", firstboot)
        self.assertNotIn("cat /etc/ssh/ssh_host_ed25519_key", firstboot)
        self.assertIn("windows-hostkey.ps1", uploader)
        self.assertIn("windows-native.ps1", uploader)
        tart = (ROOT / "scripts" / "vm-tart.sh").read_text(encoding="utf-8")
        self.assertIn("ssh-keyscan", tart)

    def test_verify_warns_only_for_accept_new_hostkey_trust(self):
        import re
        bash = cchouse_mod.find_bash()
        if not bash or not os.path.isfile(bash):
            self.skipTest("需要 bash")
        src = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        found = re.search(r"^warn_unverified_hostkey\(\) \{\n.*?\n\}$", src, re.S | re.M)
        self.assertIsNotNone(found)
        harness = "set -uo pipefail\nwarn() { printf 'WARN %s\\n' \"$1\"; }\n" + found.group(0) + "\nwarn_unverified_hostkey\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "check.sh"
            script.write_bytes(harness.encode("utf-8"))
            env_file = root / "instance.env"
            env_file.write_bytes(b"TAG=cc01\n")
            trust = root / "logs" / "hostkey.trust"
            trust.parent.mkdir()

            def for_bash(path):
                # Git Bash 不把环境变量里的反斜杠路径当成文件。argv 会被 MSYS 转换，这里不会。
                text = str(path)
                if os.name == "nt" and len(text) >= 2 and text[1] == ":":
                    return "/" + text[0].lower() + text[2:].replace("\\", "/")
                return text

            def run(**extra_env):
                proc = subprocess.run(
                    [bash, str(script)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env={**os.environ, "ENV_FILE": for_bash(env_file), "LC_ALL": "en_US.UTF-8", **extra_env},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return proc.stdout

            trust.write_bytes(b"accept-new\n")
            self.assertEqual(run(), "WARN 未经串口核对主机密钥\n")
            trust.write_bytes(b"serial\n")
            self.assertEqual(run(), "")
            trust.unlink()
            self.assertEqual(run(), "")
            self.assertEqual(run(GUEST_LOCAL="1", CCHOUSE_HOSTKEY_TRUST="accept-new"), "WARN 未经串口核对主机密钥\n")
            self.assertEqual(run(GUEST_LOCAL="1", CCHOUSE_HOSTKEY_TRUST="serial"), "")


if __name__ == "__main__":
    unittest.main()
