#!/usr/bin/env python3
"""生成 macOS 自动登录用的 /etc/kcpassword。

用法：python3 -I scripts/make-kcpassword.py <密码文件> <输出文件>

为什么需要：通过 SSH（没有图形会话）运行 `sysadminctl -autologin set` 会报
`SACSetAutoLoginPassword error:22`，原版镜像留下的 kcpassword 还是 admin 的，
开机就停在密码界面。这里按 loginwindow 的格式生成：密码补零到 12 的倍数，
再与固定密钥循环异或。输出文件权限 600；它可直接还原出明文密码，用完删掉。
"""
import os
import sys

KEY = [0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F]


def encode(pw: bytes) -> bytes:
    padded = pw + b"\0" * (12 - len(pw) % 12)
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(padded))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    pw = open(sys.argv[1], "rb").read().rstrip(b"\r\n")
    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(encode(pw))


if __name__ == "__main__":
    main()
