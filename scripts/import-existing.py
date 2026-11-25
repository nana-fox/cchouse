#!/usr/bin/env python3
"""从一套已在用的客机文件反推实例参数（instance.env 的客机部分）。

用法：
  python3 -I scripts/import-existing.py <已有客机文件目录> <TAG>

逐行把 guest/ 模板和已有文件对齐，模板里的 <占位符> 位置就是参数值。
结构对不上（行数不同、非占位符内容不同）会报出文件和行号——这也顺带
检查了模板是否还和线上配置一致。结果打印到标准输出，自己重定向到
私有目录（chmod 600），不要写进仓库。
"""
import re
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUEST = ROOT / "guest"
PLACEHOLDER = re.compile(r"<([A-Z][A-Z0-9_]*)>")
normalize_stripe_chain = runpy.run_path(str(Path(__file__).with_name("render.py")))["normalize_stripe_chain"]


def line_regex(tpl_line):
    parts, pos, seen = [], 0, set()
    for m in PLACEHOLDER.finditer(tpl_line):
        parts.append(re.escape(tpl_line[pos:m.start()]))
        name = m.group(1)
        parts.append(f"(?P={name})" if name in seen else f"(?P<{name}>.+?)")
        seen.add(name)
        pos = m.end()
    parts.append(re.escape(tpl_line[pos:]))
    return re.compile("^" + "".join(parts) + "$")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src_dir, tag = Path(sys.argv[1]), sys.argv[2]
    values, errors = {"TAG": tag, "TAG_UPPER": tag.upper()}, []
    for tpl in sorted(GUEST.iterdir()):
        real = src_dir / tpl.name.replace(".TAG.", f".{tag}.")
        if not real.is_file():
            errors.append(f"{real}: 不存在")
            continue
        content = real.read_text()
        if tpl.name == "config.yaml":
            try:
                content, chained = normalize_stripe_chain(content, tag.upper())
                values["STRIPE_EXIT_MODE"] = "tmh-chain" if chained else "front"
            except ValueError as exc:
                errors.append(f"{tpl.name}: {exc}")
                continue
        t_lines, r_lines = tpl.read_text().splitlines(), content.splitlines()
        if len(t_lines) != len(r_lines):
            errors.append(f"{tpl.name}: 行数不同（模板 {len(t_lines)}，现有 {len(r_lines)}）")
            continue
        for n, (t, r) in enumerate(zip(t_lines, r_lines), 1):
            m = line_regex(t).match(r)
            if not m:
                errors.append(f"{tpl.name}:{n}: 与模板不一致")
                continue
            for k, v in m.groupdict().items():
                if values.setdefault(k, v) != v:
                    errors.append(f"{tpl.name}:{n}: {k} 与前面取到的值不同")
    if errors:
        sys.exit("\n".join(errors))
    for k in sorted(values):
        if k != "TAG_UPPER":
            print(f"{k}='{values[k]}'")


if __name__ == "__main__":
    main()
