import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


VERIFIER = (
    Path(__file__).resolve().parents[1] / "guest-linux" / "image-builder" / "verify-ova.py"
)


class VerifyOvaTests(unittest.TestCase):
    def test_archive_manifest_and_disk_reference(self):
        disk_name = "fixture-disk1.vmdk"
        descriptor = (
            '<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" '
            'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">'
            f'<References><File ovf:href="{disk_name}" ovf:id="file1" ovf:size="4"/>'
            '</References><DiskSection><Disk ovf:fileRef="file1"/></DiskSection>'
            '</Envelope>'
        ).encode()
        expected_disk = b"disk"
        cases = (
            (descriptor, expected_disk, ""),
            (descriptor, b"junk", f"SHA-256 mismatch for {disk_name}"),
            (descriptor.replace(b'ovf:size="4"', b'ovf:size="5"'), expected_disk,
             "OVF File/Disk entries do not match"),
        )
        with tempfile.TemporaryDirectory() as directory:
            ova = Path(directory) / "fixture.ova"
            for ovf, disk, error in cases:
                with self.subTest(error=error):
                    manifest = (
                        f"SHA256(fixture.ovf)= {hashlib.sha256(ovf).hexdigest()}\n"
                        f"SHA256({disk_name})= {hashlib.sha256(expected_disk).hexdigest()}\n"
                    ).encode()
                    with tarfile.open(ova, "w", format=tarfile.USTAR_FORMAT) as archive:
                        for name, data in (("fixture.ovf", ovf), ("fixture.mf", manifest),
                                           (disk_name, disk)):
                            member = tarfile.TarInfo(name)
                            member.size = len(data)
                            archive.addfile(member, io.BytesIO(data))
                    result = subprocess.run(
                        [sys.executable, "-I", str(VERIFIER), str(ova)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, int(bool(error)), result.stderr)
                    if error:
                        self.assertIn(error, result.stderr)
