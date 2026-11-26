"""VirtualBox backend selection and installer manifest. Never installs VirtualBox."""
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse as cchouse_mod  # noqa: E402
import downloads  # noqa: E402
import preflight as preflight_mod  # noqa: E402

MANIFEST = ROOT / "guest-linux" / "virtualbox-manifest.json"
PS1 = ROOT / "scripts" / "vm-virtualbox.ps1"
PINNED_SHA = "afd0ff1656cbe04ed88ce619594ae021419d97cce89fa9bdc443e9a963f1713f"
PINNED_URL = "https://download.virtualbox.org/virtualbox/7.2.22/VirtualBox-7.2.22-175245-Win.exe"


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
    def test_windows_uses_vmware_when_present_else_virtualbox(self):
        self.assertEqual(
            cchouse_mod.detect_vm_backend("vmware-linux", system="Windows", vmware_installed=True),
            "vmware",
        )
        self.assertEqual(
            cchouse_mod.detect_vm_backend("vmware-linux", system="Windows", vmware_installed=False),
            "virtualbox",
        )

    def test_other_hosts_stay_on_their_routes(self):
        self.assertEqual(cchouse_mod.detect_vm_backend("tart-linux", system="Darwin"), "tart")
        self.assertEqual(cchouse_mod.detect_vm_backend("tart-linux", system="Windows", vmware_installed=False), "manual")
        self.assertEqual(cchouse_mod.detect_vm_backend("vmware-linux", system="Darwin", vmware_installed=False), "manual")
        self.assertEqual(cchouse_mod.detect_vm_backend("vmware-linux", system="Linux", vmware_installed=False), "manual")

    def test_explicit_backend_is_not_overridden(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "cc05"
            args = types.SimpleNamespace(
                tag="cc05", route="vmware-linux", private_dir=str(private), cache_dir=str(Path(td) / "cache"),
                dry_run=True, config_only=False, reconfigure=False, no_probe=True, link_stdin=True,
                non_interactive=True, timezone=None, expected_ip=None, image_mirror=None, npm_mirror=None,
                vm_backend="vmware",
            )
            ctx = cchouse_mod.UpContext(args, "cc05", "vmware-linux", private)
            self.assertEqual(ctx.backend, "vmware")
            self.assertIsNone(ctx.vbox_manifest)

    def test_virtualbox_steps_do_not_call_vmware(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "cc05"
            args = types.SimpleNamespace(
                tag="cc05", route="vmware-linux", private_dir=str(private), cache_dir=str(Path(td) / "cache"),
                dry_run=True, config_only=False, reconfigure=False, no_probe=True, link_stdin=True,
                non_interactive=True, timezone=None, expected_ip=None, image_mirror=None, npm_mirror=None,
                vm_backend="virtualbox",
            )
            ctx = cchouse_mod.UpContext(args, "cc05", "vmware-linux", private)
            ctx.vbox_port_is_free = lambda _port: True
            calls = []
            ctx.virtualbox = lambda op, **kw: calls.append(("virtualbox", op, kw))
            ctx.vmware = lambda op, **kw: calls.append(("vmware", op, kw))
            ctx.tart = lambda *a: calls.append(("tart", a))
            cchouse_mod.step_import(ctx)
            cchouse_mod.step_firstboot(ctx)
            cchouse_mod.step_boot(ctx)
            cchouse_mod.step_snapshot(ctx)
            ops = [item[1] for item in calls if item[0] == "virtualbox"]
            self.assertEqual(ops, ["install", "import", "firstboot", "boot", "snapshot"])
            self.assertFalse(any(item[0] == "vmware" for item in calls))
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
                "cc01", "vmware-linux", "/tmp/cc", "/tmp/k", "ssh-ed25519 AAAA x", fields, net, egress, backend="virtualbox",
            )
        vbox = cchouse_mod.format_instance_env(
            "cc01", "vmware-linux", "/tmp/cc", "/tmp/k", "ssh-ed25519 AAAA x", fields, net, egress,
            backend="virtualbox", ssh_port=2301,
        )
        vmware = cchouse_mod.format_instance_env(
            "cc01", "vmware-linux", "/tmp/cc", "/tmp/k", "ssh-ed25519 AAAA x", fields, net, egress, backend="vmware",
        )
        self.assertIn("SSH_PORT=2301\n", vbox)
        self.assertIn("GUEST_IF=enp0s3\n", vbox)
        self.assertNotIn("SSH_PORT=", vmware)
        self.assertIn("GUEST_IF=ens33\n", vmware)
        self.assertIn("ROUTER_IP=192.168.100.1\n", vbox)


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
        self.assertLess(boot.rfind("Test-CchouseOwnForward"), boot.rfind("Get-CchouseSshVerifyOptions"))
        firstboot = src.split("function Invoke-CchouseVBoxFirstboot", 1)[1].split("function Invoke-CchouseVBoxBoot", 1)[0]
        self.assertLess(firstboot.find("Enable-CchouseVBoxSerialLog"), firstboot.find("Start-CchouseVmDetached"))
        self.assertLess(src.rfind("Resolve-CchouseFirstbootStop"), src.find("HARD_POWEROFF=1"))
        self.assertLess(src.find("HARD_POWEROFF=1"), src.find("FIRSTBOOT_STOPPED=OK"))
        self.assertNotIn("Wait-CchousePowerOff $Name 20 | Out-Null", src)

    def test_preflight_virtualbox_skips_missing_vmware(self):
        self.assertEqual(preflight_mod.vmware_linux_host_errors(None, "virtualbox"), [])
        self.assertTrue(any("MISSING_VMWARE" in err for err in preflight_mod.vmware_linux_host_errors(None, None)))
        self.assertEqual(preflight_mod.vmware_linux_host_errors(r"C:\VMware\vmrun.exe", "vmware"), [])
        self.assertFalse(preflight_mod.vmnet_mismatch_applies("virtualbox"))
        self.assertTrue(preflight_mod.vmnet_mismatch_applies(None))
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "preflight.py"), "--route", "vmware-linux", "--vm-backend", "virtualbox"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertNotIn("MISSING_VMWARE", proc.stdout)
        payload = json.loads(proc.stdout)
        self.assertNotIn("MISSING_VMWARE", " ".join(payload.get("errors") or []))

    def test_windows_hostkey_comes_from_serial_not_keyscan(self):
        helper = (ROOT / "scripts" / "windows-hostkey.ps1").read_text(encoding="utf-8")
        vmware = (ROOT / "scripts" / "vm-vmware.ps1").read_text(encoding="utf-8")
        firstboot = (ROOT / "guest-linux" / "firstboot.sh").read_text(encoding="utf-8")
        uploader = (ROOT / "tools" / "e2e" / "run-gtr.sh").read_text(encoding="utf-8")
        for name, src in (("helper", helper), ("vmware", vmware), ("firstboot", firstboot)):
            self.assertNotIn("ssh-keyscan", src, name)
        self.assertIn(r"C:\Windows\System32\OpenSSH\ssh.exe", helper)
        self.assertIn("$script:CchouseSshWaitSeconds = 180", helper)
        self.assertIn("accept-new", helper)
        self.assertIn("ConnectionAttempts=1", helper)
        self.assertIn("ConnectTimeout=$ConnectTimeout", helper)
        self.assertNotIn("StrictHostKeyChecking=no", helper)
        ssh_fn = helper.split("function Invoke-CchouseSshTrue", 1)[1].split("function ", 1)[0]
        self.assertLess(ssh_fn.find("Test-CchouseHostKeyMismatch"), ssh_fn.find("return"))
        self.assertNotIn("ssh-keyscan", vmware)
        self.assertNotIn("StrictHostKeyChecking=no", vmware)
        self.assertNotIn("& ssh.exe", vmware)
        self.assertIn("windows-hostkey.ps1", vmware)
        self.assertIn("CchouseSystem32Ssh", vmware)
        boot = vmware.split("function Invoke-CchouseBoot", 1)[1].split("function New-CchouseSnapshot", 1)[0]
        self.assertLess(boot.find("Get-CchouseHostKeyFromSerialText"), boot.find("Start-VmDetached"))
        self.assertLess(boot.find("getGuestIPAddress"), boot.find("Get-CchouseSshVerifyOptions"))
        self.assertIn("CCHOUSE_HOSTKEY", firstboot)
        self.assertIn("/dev/ttyS0", firstboot)
        self.assertIn("timeout 2", firstboot)
        self.assertIn("ssh_host_ed25519_key.pub", firstboot)
        self.assertNotIn("ssh_host_ed25519_key >/dev/ttyS0", firstboot)
        self.assertNotIn("cat /etc/ssh/ssh_host_ed25519_key", firstboot)
        self.assertIn("windows-hostkey.ps1", uploader)
        tart = (ROOT / "scripts" / "vm-tart.sh").read_text(encoding="utf-8")
        self.assertIn("ssh-keyscan", tart)


if __name__ == "__main__":
    unittest.main()
