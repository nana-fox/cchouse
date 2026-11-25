#!/usr/bin/env python3
"""Live E2E report generator (status.json + auto-refreshing report.html).

Subcommands:
  init       <run_dir> --target <mac|gtr> --mode <fast|release> --commit <sha> --vm-name <name>
  step-start <run_dir> <step_id> <title>
  step-end   <run_dir> <step_id> --status <PASS|FAIL|SKIP> [--detail <text>] [--log-file <path>]
  set-meta   <run_dir> <key> <value>
  finish     <run_dir> --status <PASS|FAIL> [--failure-step <title>] [--failure-reason <text>]
"""
import argparse
import html
import json
import os
import time
from datetime import datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "-"
    s = float(seconds)
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    rem = s - m * 60
    return f"{m}m {rem:.1f}s"


def load_status(run_dir: Path) -> dict:
    p = run_dir / "status.json"
    return json.loads(p.read_text(encoding="utf-8"))


def save_and_render(run_dir: Path, data: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = now_iso()
    if data.get("started_epoch"):
        end_ep = data.get("finished_epoch") or time.time()
        data["duration_sec"] = round(end_ep - data["started_epoch"], 2)

    tmp_json = run_dir / ".status.json.tmp"
    tmp_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_json, run_dir / "status.json")

    html_text = render_html(data)
    tmp_html = run_dir / ".report.html.tmp"
    tmp_html.write_text(html_text, encoding="utf-8")
    os.replace(tmp_html, run_dir / "report.html")


def render_html(data: dict) -> str:
    status = data.get("status", "RUNNING")
    target = html.escape(str(data.get("target", "")))
    mode = html.escape(str(data.get("mode", "")))
    refresh_tag = '<meta http-equiv="refresh" content="3">' if status == "RUNNING" else ""

    # First-line failure summary banner if failed
    if status == "FAIL":
        f_step = html.escape(str(data.get("failure_step") or "未知步骤"))
        f_reason = html.escape(str(data.get("failure_reason") or "未提供原因"))
        banner = (
            f'<div class="banner fail">'
            f'<strong>FAIL</strong> · 失败步骤：<code>{f_step}</code> · 原因：{f_reason}'
            f'</div>'
        )
    elif status == "PASS":
        banner = (
            f'<div class="banner pass">'
            f'<strong>PASS</strong> · 目标 <code>{target}</code> ({mode}) 全量验证通过 · '
            f'总耗时 {html.escape(fmt_duration(data.get("duration_sec")))}'
            f'</div>'
        )
    else:
        cur = next((s["title"] for s in reversed(data.get("steps", [])) if s.get("status") == "RUNNING"), "准备中")
        banner = (
            f'<div class="banner running">'
            f'<strong>RUNNING</strong> · 正在执行：{html.escape(cur)} · '
            f'已耗时 {html.escape(fmt_duration(data.get("duration_sec")))}（每 3 秒自动刷新）'
            f'</div>'
        )

    meta = data.get("meta", {})
    meta_items = [
        ("目标", f"{target} ({mode})"),
        ("测试机", data.get("vm_name", "-")),
        ("代码 Commit", data.get("commit", "-")),
        ("镜像来源", meta.get("image_release", "-")),
        ("客机 IP", meta.get("guest_ip", "-")),
        ("真实出口", f"{meta.get('egress_ip', '-')} / {meta.get('egress_loc', '-')} / {meta.get('egress_asn', '-')}"),
        ("Claude Code", meta.get("claude_version", "-")),
        ("开始时间", data.get("started_at", "-")),
        ("总耗时", fmt_duration(data.get("duration_sec"))),
    ]
    cards_html = "\n".join(
        f'<div class="card"><div class="k">{html.escape(k)}</div><div class="v">{html.escape(str(v))}</div></div>'
        for k, v in meta_items
    )

    rows = []
    for idx, step in enumerate(data.get("steps", []), 1):
        st = step.get("status", "RUNNING")
        cls = st.lower()
        dur = fmt_duration(step.get("duration_sec"))
        if st == "RUNNING" and step.get("started_epoch"):
            dur = fmt_duration(time.time() - step["started_epoch"])
        detail = html.escape(str(step.get("detail") or ""))
        log_text = step.get("log") or ""
        log_html = ""
        if log_text:
            open_attr = " open" if st == "FAIL" else ""
            log_html = (
                f'<details{open_attr}><summary>查看日志 ({len(log_text.splitlines())} 行)</summary>'
                f'<pre>{html.escape(log_text)}</pre></details>'
            )
        rows.append(
            f"<tr>"
            f"<td>{idx}</td>"
            f"<td><strong>{html.escape(step.get('title', step.get('id', '')))}</strong></td>"
            f"<td>{html.escape(step.get('started_at', '-'))}</td>"
            f"<td>{html.escape(dur)}</td>"
            f'<td><span class="badge {cls}">{html.escape(st)}</span></td>'
            f"<td>{detail}{log_html}</td>"
            f"</tr>"
        )

    shot_rel = meta.get("screenshot")
    shot_html = ""
    if shot_rel:
        shot_html = (
            f'<h2>客机桌面截图</h2>'
            f'<div class="shot-wrap">'
            f'<a href="{html.escape(shot_rel)}" target="_blank">'
            f'<img src="{html.escape(shot_rel)}" alt="Guest Desktop Screenshot">'
            f'</a></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
{refresh_tag}
<title>cchouse E2E Report - {target} ({status})</title>
<style>
  :root {{ --bg:#0f172a; --panel:#1e293b; --border:#334155; --text:#e2e8f0; --muted:#94a3b8;
           --pass:#22c55e; --fail:#ef4444; --run:#3b82f6; --skip:#64748b; }}
  body {{ margin:0; padding:20px; background:var(--bg); color:var(--text);
          font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC",sans-serif; }}
  .wrap {{ max-width:1180px; margin:0 auto; }}
  .banner {{ padding:14px 18px; border-radius:10px; margin-bottom:18px; font-size:15px; border:1px solid transparent; }}
  .banner.pass {{ background:rgba(34,197,94,.14); border-color:rgba(34,197,94,.45); color:#bbf7d0; }}
  .banner.fail {{ background:rgba(239,68,68,.16); border-color:rgba(239,68,68,.55); color:#fecaca; }}
  .banner.running {{ background:rgba(59,130,246,.15); border-color:rgba(59,130,246,.45); color:#bfdbfe; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; margin-bottom:20px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px 12px; }}
  .card .k {{ font-size:12px; color:var(--muted); margin-bottom:4px; }}
  .card .v {{ font-size:13px; font-weight:600; word-break:break-all; }}
  h2 {{ font-size:16px; margin:20px 0 10px; color:#f8fafc; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel); border-radius:8px; overflow:hidden; }}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:13px; vertical-align:top; }}
  th {{ background:#0b1220; color:var(--muted); font-weight:600; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:700; }}
  .badge.pass {{ background:rgba(34,197,94,.2); color:#4ade80; }}
  .badge.fail {{ background:rgba(239,68,68,.25); color:#f87171; }}
  .badge.running {{ background:rgba(59,130,246,.25); color:#60a5fa; }}
  .badge.skip {{ background:rgba(100,116,139,.25); color:#cbd5e1; }}
  details {{ margin-top:6px; }}
  summary {{ cursor:pointer; color:#93c5fd; font-size:12px; }}
  pre {{ background:#0b1220; padding:10px; border-radius:6px; overflow-x:auto; font-size:12px;
         max-height:360px; overflow-y:auto; white-space:pre-wrap; word-break:break-word; }}
  .shot-wrap {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:12px; text-align:center; }}
  .shot-wrap img {{ max-width:100%; height:auto; border-radius:6px; border:1px solid #475569; }}
  footer {{ margin-top:18px; font-size:12px; color:var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  {banner}
  <div class="cards">{cards_html}</div>
  <h2>执行步骤</h2>
  <table>
    <thead><tr><th>#</th><th>步骤</th><th>开始时间</th><th>耗时</th><th>状态</th><th>说明 / 日志</th></tr></thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  {shot_html}
  <footer>最后更新：{html.escape(data.get("updated_at", "-"))} · 报告目录：{html.escape(data.get("run_dir", ""))}</footer>
</div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("run_dir")
    p_init.add_argument("--target", required=True)
    p_init.add_argument("--mode", required=True)
    p_init.add_argument("--commit", default="unknown")
    p_init.add_argument("--vm-name", default="")

    p_start = sub.add_parser("step-start")
    p_start.add_argument("run_dir")
    p_start.add_argument("step_id")
    p_start.add_argument("title")

    p_end = sub.add_parser("step-end")
    p_end.add_argument("run_dir")
    p_end.add_argument("step_id")
    p_end.add_argument("--status", required=True, choices=("PASS", "FAIL", "SKIP"))
    p_end.add_argument("--detail", default="")
    p_end.add_argument("--log-file", default="")

    p_meta = sub.add_parser("set-meta")
    p_meta.add_argument("run_dir")
    p_meta.add_argument("key")
    p_meta.add_argument("value")

    p_fin = sub.add_parser("finish")
    p_fin.add_argument("run_dir")
    p_fin.add_argument("--status", required=True, choices=("PASS", "FAIL"))
    p_fin.add_argument("--failure-step", default="")
    p_fin.add_argument("--failure-reason", default="")

    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()

    if args.cmd == "init":
        data = {
            "status": "RUNNING",
            "target": args.target,
            "mode": args.mode,
            "commit": args.commit,
            "vm_name": args.vm_name,
            "run_dir": str(run_dir),
            "started_at": now_iso(),
            "started_epoch": time.time(),
            "finished_at": None,
            "finished_epoch": None,
            "duration_sec": 0.0,
            "failure_step": "",
            "failure_reason": "",
            "meta": {},
            "steps": [],
        }
        save_and_render(run_dir, data)
    elif args.cmd == "step-start":
        data = load_status(run_dir)
        data["steps"].append({
            "id": args.step_id,
            "title": args.title,
            "status": "RUNNING",
            "started_at": now_iso(),
            "started_epoch": time.time(),
            "finished_at": None,
            "duration_sec": None,
            "detail": "",
            "log": "",
        })
        save_and_render(run_dir, data)
    elif args.cmd == "step-end":
        data = load_status(run_dir)
        step = next((s for s in reversed(data["steps"]) if s["id"] == args.step_id), None)
        if step is None:
            step = {"id": args.step_id, "title": args.step_id, "started_at": now_iso(), "started_epoch": time.time()}
            data["steps"].append(step)
        now_ep = time.time()
        step["status"] = args.status
        step["finished_at"] = now_iso()
        step["duration_sec"] = round(now_ep - step.get("started_epoch", now_ep), 2)
        if args.detail:
            step["detail"] = args.detail
        if args.log_file and Path(args.log_file).is_file():
            raw = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
            # Keep last 200 lines if huge
            lines = raw.splitlines()
            if len(lines) > 200:
                raw = "\n".join(lines[-200:])
            step["log"] = raw
        if args.status == "FAIL" and not data.get("failure_step"):
            data["failure_step"] = step.get("title", args.step_id)
            data["failure_reason"] = args.detail or "步骤返回非零退出码"
        save_and_render(run_dir, data)
    elif args.cmd == "set-meta":
        data = load_status(run_dir)
        data.setdefault("meta", {})[args.key] = args.value
        save_and_render(run_dir, data)
    elif args.cmd == "finish":
        data = load_status(run_dir)
        data["status"] = args.status
        data["finished_at"] = now_iso()
        data["finished_epoch"] = time.time()
        if args.failure_step:
            data["failure_step"] = args.failure_step
        if args.failure_reason:
            data["failure_reason"] = args.failure_reason
        save_and_render(run_dir, data)


if __name__ == "__main__":
    main()
