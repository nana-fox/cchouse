#!/usr/bin/env python3
"""Pure-Python XWD (X11 Window Dump, ZPixmap 24/32-bit) to PNG converter.

Usage:
  python3 tools/e2e/xwd2png.py <input.xwd|input.xwd.gz> <output.png>
"""
import gzip
import struct
import sys
import zlib
from pathlib import Path


def _bit_shift(mask: int) -> int:
    if mask == 0:
        return 0
    shift = 0
    while (mask & 1) == 0:
        mask >>= 1
        shift += 1
    return shift


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def xwd_to_png(raw: bytes) -> bytes:
    if len(raw) < 100:
        raise ValueError("XWD file too small")
    (
        header_size,
        file_version,
        pixmap_format,
        pixmap_depth,
        width,
        height,
        xoffset,
        byte_order,
        bitmap_unit,
        bitmap_bit_order,
        bitmap_pad,
        bits_per_pixel,
        bytes_per_line,
        visual_class,
        red_mask,
        green_mask,
        blue_mask,
        bits_per_rgb,
        colormap_entries,
        ncolors,
    ) = struct.unpack(">20I", raw[:80])

    if file_version != 7:
        raise ValueError(f"Unsupported XWD version: {file_version}")
    if pixmap_format != 2:
        raise ValueError(f"Unsupported XWD pixmap_format: {pixmap_format} (expected 2=ZPixmap)")
    if bits_per_pixel not in (24, 32):
        raise ValueError(f"Unsupported bits_per_pixel: {bits_per_pixel}")

    # Skip header + colormap (12 bytes per XWDColor entry)
    pixel_offset = header_size + ncolors * 12
    pixels = raw[pixel_offset:]
    expected_len = bytes_per_line * height
    if len(pixels) < expected_len:
        raise ValueError(f"Truncated XWD pixel data: got {len(pixels)}, need {expected_len}")

    bpp = bits_per_pixel // 8
    r_shift = _bit_shift(red_mask)
    g_shift = _bit_shift(green_mask)
    b_shift = _bit_shift(blue_mask)

    # Fast path for standard 32-bit little-endian BGRX (standard Xorg TrueColor)
    fast_bgrx = (
        byte_order == 0
        and bpp == 4
        and red_mask == 0x00FF0000
        and green_mask == 0x0000FF00
        and blue_mask == 0x000000FF
    )

    rows = bytearray()
    if fast_bgrx:
        for y in range(height):
            rows.append(0)  # PNG filter type 0 (None)
            row_start = y * bytes_per_line
            row = pixels[row_start : row_start + width * 4]
            # Convert BGRX -> RGB using slices
            rgb = bytearray(width * 3)
            rgb[0::3] = row[2::4]
            rgb[1::3] = row[1::4]
            rgb[2::3] = row[0::4]
            rows.extend(rgb)
    else:
        endian = "<" if byte_order == 0 else ">"
        for y in range(height):
            rows.append(0)
            row_start = y * bytes_per_line
            row_out = bytearray(width * 3)
            for x in range(width):
                off = row_start + x * bpp
                if bpp == 4:
                    val = struct.unpack_from(endian + "I", pixels, off)[0]
                else:
                    b0, b1, b2 = pixels[off], pixels[off + 1], pixels[off + 2]
                    val = (b0 | (b1 << 8) | (b2 << 16)) if byte_order == 0 else ((b0 << 16) | (b1 << 8) | b2)
                row_out[x * 3] = (val & red_mask) >> r_shift
                row_out[x * 3 + 1] = (val & green_mask) >> g_shift
                row_out[x * 3 + 2] = (val & blue_mask) >> b_shift
            rows.extend(row_out)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("Usage: xwd2png.py <input.xwd|input.xwd.gz> <output.png>")
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    data = src.read_bytes()
    if src.suffix == ".gz" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    png = xwd_to_png(data)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(png)
    print(f"OK {dst} ({len(png)} bytes)")


if __name__ == "__main__":
    main()
