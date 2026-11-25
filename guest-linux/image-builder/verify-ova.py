#!/usr/bin/env python3
"""Validate the cloud OVA's layout, descriptor and SHA-256 manifest."""

import argparse
import hashlib
from pathlib import Path
import re
import tarfile
import xml.etree.ElementTree as ET


OVF = "{http://schemas.dmtf.org/ovf/envelope/1}"
MANIFEST_LINE = rb"SHA256\(([^\n]+)\)= ([0-9a-f]{64})\n"


def verify_ova(path):
    with tarfile.open(path, mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 3 or any(not member.isfile() for member in members):
            raise ValueError("expected exactly three regular OVA members")
        descriptor, manifest, disk = members
        stem = Path(descriptor.name).stem
        if [member.name for member in members] != [
            f"{stem}.ovf", f"{stem}.mf", f"{stem}-disk1.vmdk"
        ]:
            raise ValueError("expected OVF, manifest, then VMDK with matching basenames")

        with archive.extractfile(descriptor) as source:
            root = ET.parse(source).getroot()
        references = root.findall(f"{OVF}References/{OVF}File")
        disk_entries = root.findall(f"{OVF}DiskSection/{OVF}Disk")
        if len(references) != 1 or len(disk_entries) != 1:
            raise ValueError("expected one OVF File entry and one Disk entry")
        reference, disk_entry = references[0], disk_entries[0]
        if (
            reference.get(f"{OVF}href") != disk.name
            or reference.get(f"{OVF}size") != str(disk.size)
            or not reference.get(f"{OVF}id")
            or disk_entry.get(f"{OVF}fileRef") != reference.get(f"{OVF}id")
        ):
            raise ValueError("OVF File/Disk entries do not match the archived VMDK")

        with archive.extractfile(manifest) as source:
            entries = re.fullmatch(MANIFEST_LINE * 2, source.read())
        if entries is None or (entries[1], entries[3]) != (
            descriptor.name.encode("utf-8"), disk.name.encode("utf-8")
        ):
            raise ValueError("manifest must list SHA-256 for the OVF and VMDK")
        for member, expected in zip((descriptor, disk), (entries[2], entries[4])):
            digest = hashlib.sha256()
            with archive.extractfile(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().encode("ascii") != expected:
                raise ValueError(f"SHA-256 mismatch for {member.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ova", type=Path)
    args = parser.parse_args()
    try:
        verify_ova(args.ova)
    except (OSError, ValueError, ET.ParseError, tarfile.TarError) as error:
        parser.exit(1, f"{args.ova.name}: {error}\n")
    print(f"{args.ova.name}: OVF and SHA-256 manifest verified")


if __name__ == "__main__":
    main()
