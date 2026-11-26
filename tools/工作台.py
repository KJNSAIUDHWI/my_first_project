# -*- coding: utf-8 -*-
"""自动改卷统一工作台 CLI（与网页控制中心 100% 等价，执行时自动联动高亮网页）。

用法示例：
    python tools/工作台.py serve --open
    python tools/工作台.py check [--json]
    python tools/工作台.py repair-pip
    python tools/工作台.py agent-prompt [--launch]
    python tools/工作台.py ingest --source D:\\新试卷 --subject 物理 [--name 期中物理]
    python tools/工作台.py run [--batch <batch_id>]
    python tools/工作台.py status [--json]
    python tools/工作台.py open <review|annotate|choice|digit> [--m m1]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "工作台"))
import core  # noqa: E402
import serve as wb_serve  # noqa: E402


def cmd_check(args):
    core.emit_event("check", "running", "CLI 正在执行环境自检...", hub_url=args.hub)
    rep = core.check_environment(ROOT)
    msg = "环境自检全部通过" if rep["ok"] else f"发现 {rep['error_count']} 项异常"
    core.emit_event("check", "done" if rep["ok"] else "error", msg, {"error_count": rep["error_count"]}, hub_url=args.hub)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1
    err_cnt = rep["error_count"]
    summary = "全部通过" if rep["ok"] else f"存在 {err_cnt} 项问题"
    print(f"环境自检完成（{rep['elapsed_ms']} ms）— {summary}")
    for it in rep["items"]:
        icon = "[OK]  " if it["severity"] == "ok" else ("[WARN]" if it["severity"] == "warn" else "[ERR] ")
        print(f"  {icon} {it['name']}: {it['detail']}")
    return 0 if rep["ok"] else 1


def cmd_repair_pip(args):
    rep = core.check_environment(ROOT)
    pkgs = args.packages or rep["missing_packages"]
    if not pkgs:
        print("没有缺失的 Python 依赖包。")
        return 0
    core.emit_event("repair_pip", "running", f"CLI 测速选源安装：{', '.join(pkgs)}", hub_url=args.hub)
    res = core.install_packages(pkgs, progress_cb=lambda m: (print(m), core.emit_event("repair_pip", "running", m, hub_url=args.hub)))
    core.emit_event("repair_pip", "done" if res["ok"] else "error", "依赖安装完成" if res["ok"] else "依赖安装失败", hub_url=args.hub)
    if not res["ok"]:
        print(res.get("stderr") or res.get("stdout"))
        return 1
    print("依赖安装完成。")
    return 0


def cmd_agent_prompt(args):
    rep = core.check_environment(ROOT)
    prompt = core.build_agent_prompt(rep, root=ROOT, extra_note=args.note or "")
    if args.launch:
        res = core.launch_opencode(ROOT, prompt)
        core.emit_event("agent", "done", res["message"], hub_url=args.hub)
        print(res["message"])
    else:
        copied = core.copy_to_clipboard(prompt)
        print(prompt)
        if copied:
            print("\n（已同步复制到系统剪贴板）")
    return 0


def cmd_ingest(args):
    core.emit_event("ingest", "running", f"CLI 正在整理入库（学科：{args.subject}）...", hub_url=args.hub)
    meta = core.ingest_batch(
        source_dir=args.source,
        subject=args.subject,
        batch_name=args.name,
        progress_cb=lambda m: (print(m), core.emit_event("ingest", "running", m, hub_url=args.hub)),
    )
    msg = f"入库完成：{meta['batch_id']}（新增 {meta['last_imported']} 张，跳过重复 {meta['last_skipped_dup']} 张）"
    core.emit_event("ingest", "done", msg, {"batch_id": meta["batch_id"]}, hub_url=args.hub)
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    else:
        print(msg)
        print(f"  图片目录：{meta['images_dir']}")
    return 0


def cmd_run(args):
    active = core.set_active_batch(args.batch) if args.batch else core.get_active_batch()
    if not active:
        print("错误：尚未导入任何批次，请先运行 `python tools/工作台.py ingest --source <目录> --subject <学科>`")
        return 1
    core.emit_event("recognize", "running", f"CLI 启动自动识别：{active['batch_id']}", hub_url=args.hub)
    cmd = [
        sys.executable,
        str(ROOT / "pipeline" / "run.py"),
        "--recognize",
        "--input", active["images_dir"],
        "--out", active["recognize_dir"],
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT))
    ok = proc.returncode == 0
    core.emit_event("recognize", "done" if ok else "error", "自动识别完成" if ok else f"自动识别异常退出 ({proc.returncode})", hub_url=args.hub)
    return proc.returncode


def cmd_status(args):
    data = core.collect_tasks_and_leaderboard(root=ROOT)
    core.emit_event("overview", "done", "CLI 查询了当前总览进度", hub_url=args.hub)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    ab = data.get("active_batch")
    print("当前活跃批次：", ab["batch_id"] if ab else "无（尚未导入）")
    for t in data["tasks"]:
        print(f"  - {t['name']}: {t['done']}/{t['total']} {t['unit']}（剩余 {t['remaining']}） | {t['extra']}")
    if data["leaderboard"]:
        print("人员审阅统计：")
        for p in data["leaderboard"]:
            print(f"  - {p['name']}: {p['count']}")
    return 0


def cmd_open(args):
    # 若工作台正在运行，直接通知工作台拉起服务（并高亮联动）
    payload = json.dumps({"task_id": args.task, "m": args.m}).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{args.hub.rstrip('/')}/api/service/ensure",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        url = f"http://127.0.0.1:{res['port']}/"
        print(f"服务已就绪：{url}")
        if not args.no_browser:
            wb_serve.open_app_window(url)
        return 0
    except Exception as exc:
        print(f"工作台未运行或拉起失败（{exc}），请先运行 `python tools/工作台.py serve --open`")
        return 1


def main():
    ap = argparse.ArgumentParser(description="自动改卷统一工作台（GUI/CLI 等价入口）")
    ap.add_argument("--hub", default="http://127.0.0.1:8888", help="本地工作台控制中心地址")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动工作台控制中心")
    p_serve.add_argument("--port", type=int, default=8888)
    p_serve.add_argument("--open", action="store_true")

    p_chk = sub.add_parser("check", help="检查运行环境与模型权重")
    p_chk.add_argument("--json", action="store_true")

    p_pip = sub.add_parser("repair-pip", help="测速选源自动安装缺失 Python 包")
    p_pip.add_argument("packages", nargs="*")

    p_ag = sub.add_parser("agent-prompt", help="生成诊断提示词（可选拉起 opencode）")
    p_ag.add_argument("--launch", action="store_true")
    p_ag.add_argument("--note", default="")

    p_ing = sub.add_parser("ingest", help="非破坏性导入并统一命名新试卷")
    p_ing.add_argument("--source", required=True, help="原始试卷照片文件夹")
    p_ing.add_argument("--subject", required=True, help="学科（如 物理/数学/化学）")
    p_ing.add_argument("--name", default=None, help="自定义批次名")
    p_ing.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="对当前（或指定）批次运行自动识别流水线")
    p_run.add_argument("--batch", default=None)

    p_st = sub.add_parser("status", help="查看当前批次与各审核任务进度")
    p_st.add_argument("--json", action="store_true")

    p_op = sub.add_parser("open", help="一键启动并打开指定审核页")
    p_op.add_argument("task", choices=["review", "annotate", "choice", "digit"])
    p_op.add_argument("--m", default="m1", choices=["m1", "m2", "m3"])
    p_op.add_argument("--no-browser", action="store_true")

    args = ap.parse_args()
    if args.cmd == "serve":
        wb_serve.run_server(port=args.port, open_browser=args.open)
        return 0
    return {
        "check": cmd_check,
        "repair-pip": cmd_repair_pip,
        "agent-prompt": cmd_agent_prompt,
        "ingest": cmd_ingest,
        "run": cmd_run,
        "status": cmd_status,
        "open": cmd_open,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
