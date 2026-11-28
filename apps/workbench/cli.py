# -*- coding: utf-8 -*-
"""自动改卷统一工作台 CLI（与网页控制中心统一联动、支持机器可读 JSON 与 AI 帮手接入）。

用法示例：
    python -m apps.workbench serve --open
    python -m apps.workbench check [--deep] [--json]
    python -m apps.workbench status [--json]
    python -m apps.workbench doctor [--json]
    python -m apps.workbench capabilities [--json]
    python -m apps.workbench vlm-test [--json]
    python -m apps.workbench ingest --source D:\\新试卷 --subject 物理 [--name 期中物理] [--dry-run] [--json]
    python -m apps.workbench run [--batch <batch_id>] [--detach] [--json]
    python -m apps.workbench retry [--batch <batch_id>] [--detach] [--json]
    python -m apps.workbench stop [--json]
    python -m apps.workbench benchmark [--samples 4] [--detach] [--json]
    python -m apps.workbench open <review|annotate|choice|monitor> [--m m1] [--no-browser]
    python -m apps.workbench agent-prompt [--mode plan|guarded] [--note ...] [--launch] [--json]
    python -m apps.workbench runs [list|show <run_id>] [--batch <batch_id>] [--json]
    python -m apps.workbench logs [--run <run_id>] [--batch <batch_id>] [--level ERROR] [--page p141] [--json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import core  # noqa: E402
import server as wb_serve  # noqa: E402
from engine import run_log  # noqa: E402


def _hub_url(args) -> str:
    return str(getattr(args, "hub", None) or "http://127.0.0.1:8888").rstrip("/")


def _hub_request(hub: str, method: str, path: str, payload: dict | None = None, timeout: float = 10.0) -> tuple[bool, int, dict]:
    url = f"{hub.rstrip('/')}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            return True, resp.status, body if isinstance(body, dict) else {"value": body}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            body = {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
        return False, exc.code, body if isinstance(body, dict) else {"ok": False, "error": str(body)}
    except Exception as exc:
        return False, 0, {"ok": False, "error": str(exc)}


def _hub_alive(hub: str, timeout: float = 1.5) -> bool:
    ok, code, body = _hub_request(hub, "GET", "/api/health", timeout=timeout)
    return bool(ok and code == 200 and body.get("ok"))


def _emit_output(args, *, command: str, ok: bool, exit_code: int, data: dict[str, Any], error: str = "", t0: float = 0.0, text_lines: list[str] | None = None) -> int:
    dur_ms = round((time.monotonic() - t0) * 1000.0, 1) if t0 else 0.0
    try:
        run_log.append_cli_log(
            core.DEFAULT_BATCHES_ROOT,
            command=command,
            argv=sys.argv[1:],
            source="cli",
            actor="cli",
            exit_code=exit_code,
            duration_ms=dur_ms,
            ok=ok,
            batch_id=str(data.get("batch_id") or (data.get("active_batch") or {}).get("batch_id") or ""),
            message=error or f"cli {command} {'ok' if ok else 'failed'}",
        )
    except Exception:
        pass

    if getattr(args, "json", False):
        envelope: dict[str, Any] = {
            "ok": bool(ok),
            "command": command,
            "exit_code": int(exit_code),
            "duration_ms": dur_ms,
            "error": str(error or ""),
            "data": data,
        }
        # 保留常用顶层兼容键（不覆盖统一信封核心字段）
        for k, v in data.items():
            if k not in envelope:
                envelope[k] = v
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return int(exit_code)

    if not getattr(args, "quiet", False):
        if text_lines:
            for ln in text_lines:
                print(ln)
        elif error:
            print(f"错误：{error}")
    return int(exit_code)


def _poll_hub_pipeline(hub: str, timeout_s: float = 3600.0, quiet: bool = False) -> tuple[bool, int, dict]:
    deadline = time.monotonic() + max(10.0, timeout_s)
    last_msg = ""
    while time.monotonic() < deadline:
        ok, _, ov = _hub_request(hub, "GET", "/api/overview", timeout=5.0)
        if not ok:
            time.sleep(1.0)
            continue
        pj = ov.get("pipeline_job") or {}
        logs = pj.get("logs") or []
        if not quiet and logs and logs[-1] != last_msg:
            last_msg = logs[-1]
            print(last_msg, flush=True)
        if not pj.get("running"):
            rc = int(pj.get("returncode") if pj.get("returncode") is not None else (0 if pj.get("status") in ("done", "warn") else 1))
            return (pj.get("status") in ("done", "warn") and rc == 0), rc, pj
        time.sleep(0.8)
    return False, 1, {"status": "timeout", "error": "等待工作台任务超时"}


def cmd_check(args, t0: float) -> int:
    hub = _hub_url(args)
    core.emit_event("check", "running", "CLI 正在执行环境自检...", hub_url=hub)
    rep = core.check_environment(ROOT, deep=bool(getattr(args, "deep", False)))
    # 回写到在线工作台 Hub，使 GUI last_check 实时同步（R5）
    synced = False
    if _hub_alive(hub):
        ok_s, _, _ = _hub_request(hub, "POST", "/api/check/sync", {"report": rep, "source": "cli"}, timeout=min(10.0, args.timeout))
        synced = ok_s
    if not synced:
        msg = "环境自检全部通过" if rep["ok"] else f"发现 {rep['error_count']} 项异常"
        core.emit_event("check", "done" if rep["ok"] else "error", msg, {"error_count": rep["error_count"]}, hub_url=hub)

    err_cnt = rep["error_count"]
    summary = "全部通过" if rep["ok"] else f"存在 {err_cnt} 项问题"
    lines = [f"环境自检完成（{rep['elapsed_ms']} ms）— {summary}"]
    for it in rep["items"]:
        icon = "[OK]  " if it["severity"] == "ok" else ("[WARN]" if it["severity"] == "warn" else "[ERR] ")
        lines.append(f"  {icon} {it['name']}: {it['detail']}")
    return _emit_output(
        args,
        command="check",
        ok=bool(rep["ok"]),
        exit_code=0 if rep["ok"] else 1,
        data={**rep, "hub_synced": synced},
        error="" if rep["ok"] else f"环境自检发现 {rep['error_count']} 项异常",
        t0=t0,
        text_lines=lines,
    )


def cmd_status(args, t0: float) -> int:
    hub = _hub_url(args)
    from_hub = False
    if _hub_alive(hub):
        ok_h, _, ov = _hub_request(hub, "GET", "/api/overview", timeout=min(10.0, args.timeout))
        if ok_h and isinstance(ov, dict) and "tasks" in ov:
            data = ov
            from_hub = True
        else:
            data = core.collect_tasks_and_leaderboard(root=ROOT)
    else:
        data = core.collect_tasks_and_leaderboard(root=ROOT)
    core.emit_event("overview", "done", "CLI 查询了当前总览进度", hub_url=hub)
    data = {**data, "from_hub": from_hub}

    ab = data.get("active_batch")
    lines = [f"当前活跃批次：{ab['batch_id'] if ab else '无（尚未导入）'}"]
    for t in data.get("tasks") or []:
        run_tag = " [运行中]" if t.get("running") else ""
        lines.append(f"  - {t['name']}{run_tag}: {t['done']}/{t['total']} {t['unit']}（剩余 {t['remaining']}） | {t['extra']}")
    if data.get("online_users"):
        lines.append("在线成员：" + ", ".join(str(u.get("name") or "") for u in data["online_users"]))
    if data.get("leaderboard"):
        lines.append("人员审阅统计：")
        for p in data["leaderboard"]:
            lines.append(f"  - {p['name']}: {p['count']}")
    return _emit_output(args, command="status", ok=True, exit_code=0, data=data, t0=t0, text_lines=lines)


def cmd_doctor(args, t0: float) -> int:
    hub = _hub_url(args)
    probe_v = bool(getattr(args, "probe_zcode", False))
    info = core.collect_machine_info(ROOT, probe_zcode_version=probe_v)
    hub_up = _hub_alive(hub)
    info["hub"] = {"url": hub, "reachable": hub_up}
    info["pipeline_lease"] = core.read_pipeline_lease()
    if hub_up:
        ok_m, _, m_data = _hub_request(hub, "GET", "/api/machine", timeout=min(10.0, args.timeout))
        if ok_m and isinstance(m_data, dict):
            info["services"] = m_data.get("services") or {}
    lines = [
        f"主机：{info['host']} | OS：{info['os']}",
        f"Python：{info['python']['version']} ({info['python']['executable']})",
        f"设备：{info['hardware'].get('recommended_label')}",
        f"ZCode：{'可用 ' + info['tools']['zcode'].get('cli', '') if info['tools']['zcode'].get('available') else '未检测到'}",
        f"工作台 Hub ({hub})：{'在线' if hub_up else '未运行'}",
        f"视觉模型：{info['vlm'].get('provider')} / {info['vlm'].get('model')} ({info['vlm'].get('api_key_masked') or '未配置'})",
    ]
    return _emit_output(args, command="doctor", ok=True, exit_code=0, data=info, t0=t0, text_lines=lines)


def cmd_capabilities(args, t0: float) -> int:
    hub = _hub_url(args)
    info = core.collect_machine_info(ROOT)
    caps = {
        "cli_entry": "python -m apps.workbench",
        "hub_url": hub,
        "hub_reachable": _hub_alive(hub),
        "global_flags": ["--hub <url>", "--json", "--quiet", "--no-color", "--timeout <sec>"],
        "commands": [
            {"name": "serve", "safety": "service", "args": ["--port", "--open"], "desc": "启动工作台 Web 控制中心"},
            {"name": "check", "safety": "read_only", "args": ["--deep", "--json"], "desc": "检查环境、依赖、权重与 API，并同步至 Hub"},
            {"name": "status", "safety": "read_only", "args": ["--json"], "desc": "查看当前批次、审阅进度、子服务与在线成员"},
            {"name": "doctor", "safety": "read_only", "args": ["--probe-zcode", "--json"], "desc": "返回主机 OS/CPU/GPU/Python/ZCode/端口/路径"},
            {"name": "capabilities", "safety": "read_only", "args": ["--json"], "desc": "输出全量 CLI 能力清单与安全级别"},
            {"name": "vlm-test", "safety": "read_only", "args": ["--json"], "desc": "带 64x64 图测试视觉大模型 API 连通性"},
            {"name": "runs", "safety": "read_only", "args": ["list|show <run_id>", "--batch", "--limit", "--json"], "desc": "查询历史识别与补跑 run 记录"},
            {"name": "logs", "safety": "read_only", "args": ["--run", "--batch", "--level", "--event", "--page", "--grid", "-q", "--limit", "--json"], "desc": "按条件查询统一事件日志"},
            {"name": "agent-prompt", "safety": "read_only", "args": ["--mode plan|guarded", "--note", "--launch", "--json"], "desc": "生成脱敏诊断提示词与 ZCode 调用命令"},
            {"name": "open", "safety": "service", "args": ["<review|annotate|choice|monitor>", "--m", "--no-browser", "--json"], "desc": "拉起并打开子服务"},
            {"name": "ingest", "safety": "mutating", "args": ["--source", "--subject", "--name", "--dry-run", "--json"], "desc": "非破坏性导入并去重命名新试卷"},
            {"name": "run", "safety": "long_task", "args": ["--batch", "--detach", "--json"], "desc": "通过 Hub（或共享租约）执行自动识别"},
            {"name": "retry", "safety": "long_task", "args": ["--batch", "--concurrency", "--detach", "--json"], "desc": "仅补跑失败/结构异常题块（自动单题降级）"},
            {"name": "stop", "safety": "mutating", "args": ["--json"], "desc": "停止工作台正在运行的识别/补跑任务"},
            {"name": "benchmark", "safety": "long_task", "args": ["--samples", "--detach", "--json"], "desc": "执行零 API 费用本机性能压测"},
            {"name": "repair-pip", "safety": "mutating", "args": ["[packages...]", "--dry-run", "--json"], "desc": "测速选源安装缺失 Python 依赖"},
        ],
        "tools": info["tools"],
        "ports": info["ports"],
        "project_root": info["project_root"],
    }
    lines = [f"工作台 CLI 能力清单（共 {len(caps['commands'])} 条命令，Hub: {'在线' if caps['hub_reachable'] else '未运行'}）"]
    for c in caps["commands"]:
        lines.append(f"  - {c['name']:13s} [{c['safety']:9s}] {c['desc']}")
    return _emit_output(args, command="capabilities", ok=True, exit_code=0, data=caps, t0=t0, text_lines=lines)


def cmd_vlm_test(args, t0: float) -> int:
    hub = _hub_url(args)
    if _hub_alive(hub):
        ok_h, _, res = _hub_request(hub, "POST", "/api/vlm/test", {}, timeout=max(25.0, args.timeout))
        res_ok = bool(ok_h and res.get("ok"))
    else:
        res = core.test_vlm_settings(root=ROOT)
        res_ok = bool(res.get("ok"))
    lines = [
        f"视觉 API 连通性测试：{'通过' if res_ok else '失败'}",
        f"  模型：{res.get('returned_model') or res.get('requested_model') or '-'} | 延迟：{res.get('latency_ms', '-')} ms | 回复：{res.get('reply') or res.get('error') or ''}",
    ]
    return _emit_output(args, command="vlm-test", ok=res_ok, exit_code=0 if res_ok else 1, data=res, error="" if res_ok else str(res.get("error") or "连通性测试失败"), t0=t0, text_lines=lines)


def cmd_repair_pip(args, t0: float) -> int:
    hub = _hub_url(args)
    rep = core.check_environment(ROOT)
    pkgs = args.packages or rep["missing_packages"]
    if not pkgs:
        return _emit_output(args, command="repair-pip", ok=True, exit_code=0, data={"installed": [], "missing": []}, t0=t0, text_lines=["没有缺失的 Python 依赖包。"])
    if getattr(args, "dry_run", False):
        return _emit_output(args, command="repair-pip", ok=True, exit_code=0, data={"dry_run": True, "packages": pkgs}, t0=t0, text_lines=[f"[dry-run] 将测速选源并安装：{', '.join(pkgs)}"])
    core.emit_event("repair_pip", "running", f"CLI 测速选源安装：{', '.join(pkgs)}", hub_url=hub)
    res = core.install_packages(
        pkgs,
        progress_cb=lambda m: (
            (None if (args.json or args.quiet) else print(m, flush=True)),
            core.emit_event("repair_pip", "running", m, hub_url=hub),
        ),
    )
    core.emit_event("repair_pip", "done" if res["ok"] else "error", "依赖安装完成" if res["ok"] else "依赖安装失败", hub_url=hub)
    ok = bool(res.get("ok"))
    return _emit_output(
        args,
        command="repair-pip",
        ok=ok,
        exit_code=0 if ok else 1,
        data=res,
        error="" if ok else (res.get("stderr") or res.get("stdout") or "依赖安装失败")[-400:],
        t0=t0,
        text_lines=["依赖安装完成。"] if ok else [(res.get("stderr") or res.get("stdout") or "依赖安装失败")[-1200:]],
    )


def cmd_agent_prompt(args, t0: float) -> int:
    hub = _hub_url(args)
    cfg = core.load_config()
    rep = None
    if _hub_alive(hub):
        ok_o, _, ov = _hub_request(hub, "GET", "/api/overview", timeout=5.0)
        if ok_o and isinstance(ov.get("last_check"), dict):
            rep = ov["last_check"]
    if rep is None:
        rep = core.check_environment(ROOT)
    prompt = core.build_agent_prompt(rep, root=ROOT, extra_note=args.note or "")
    mode = getattr(args, "mode", "plan") or "plan"
    zc_spec = core.build_zcode_command(prompt, ROOT, mode=mode, cfg=cfg)
    data = {
        "prompt": prompt,
        "mode": mode,
        "zcode": zc_spec.get("zcode") or core.find_zcode(cfg),
        "zcode_command": zc_spec.get("cmd") if zc_spec.get("ok") else None,
        "zcode_env_patch": zc_spec.get("env_patch") if zc_spec.get("ok") else {},
        "opencode_path": core.find_opencode(cfg) or "",
        "cwd": str(ROOT.resolve()),
    }
    if args.launch:
        if _hub_alive(hub) and zc_spec.get("ok"):
            ok_l, _, res = _hub_request(hub, "POST", "/api/ai/run", {"prompt": prompt, "mode": mode}, timeout=10.0)
            data["launch_result"] = res
            return _emit_output(args, command="agent-prompt", ok=ok_l and bool(res.get("ok")), exit_code=0 if (ok_l and res.get("ok")) else 1, data=data, error="" if res.get("ok") else str(res.get("error") or ""), t0=t0, text_lines=[str(res.get("message") or res.get("error") or "")])
        res = core.launch_opencode(ROOT, prompt)
        core.emit_event("agent", "done", res["message"], hub_url=hub)
        data["launch_result"] = res
        return _emit_output(args, command="agent-prompt", ok=True, exit_code=0, data=data, t0=t0, text_lines=[res["message"]])

    copied = False if args.json else core.copy_to_clipboard(prompt)
    data["copied"] = copied
    lines = [prompt]
    if copied:
        lines.append("\n（已同步复制到系统剪贴板）")
    return _emit_output(args, command="agent-prompt", ok=True, exit_code=0, data=data, t0=t0, text_lines=lines)


def cmd_ingest(args, t0: float) -> int:
    hub = _hub_url(args)
    src = Path(args.source).expanduser().resolve()
    if getattr(args, "dry_run", False):
        if not src.is_dir():
            return _emit_output(args, command="ingest", ok=False, exit_code=1, data={}, error=f"找不到源文件夹：{src}", t0=t0)
        files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in core.IMAGE_EXTS)
        data = {"dry_run": True, "source": str(src), "subject": args.subject, "batch_name": args.name, "image_count": len(files)}
        return _emit_output(args, command="ingest", ok=True, exit_code=0, data=data, t0=t0, text_lines=[f"[dry-run] 发现 {len(files)} 张图片（来源：{src}，学科：{args.subject}）"])

    if _hub_alive(hub):
        ok_h, _, res = _hub_request(
            hub,
            "POST",
            "/api/ingest",
            {"source_dir": str(src), "subject": args.subject, "batch_name": args.name or ""},
            timeout=max(120.0, args.timeout),
        )
        if ok_h and res.get("ok"):
            meta = res.get("batch") or {}
            return _emit_output(args, command="ingest", ok=True, exit_code=0, data=meta, t0=t0, text_lines=[res.get("message") or "", f"  图片目录：{meta.get('images_dir', '')}"])
        return _emit_output(args, command="ingest", ok=False, exit_code=1, data=res, error=str(res.get("error") or "入库失败"), t0=t0)

    core.emit_event("ingest", "running", f"CLI 正在整理入库（学科：{args.subject}）...", hub_url=hub)
    try:
        meta = core.ingest_batch(
            source_dir=args.source,
            subject=args.subject,
            batch_name=args.name,
            progress_cb=lambda m: (
                (None if (args.json or args.quiet) else print(m, flush=True)),
                core.emit_event("ingest", "running", m, hub_url=hub),
            ),
        )
    except Exception as exc:
        return _emit_output(args, command="ingest", ok=False, exit_code=1, data={}, error=str(exc), t0=t0)
    msg = f"入库完成：{meta['batch_id']}（新增 {meta['last_imported']} 张，跳过重复 {meta['last_skipped_dup']} 张）"
    core.emit_event("ingest", "done", msg, {"batch_id": meta["batch_id"]}, hub_url=hub)
    return _emit_output(args, command="ingest", ok=True, exit_code=0, data=meta, t0=t0, text_lines=[msg, f"  图片目录：{meta['images_dir']}"])


def cmd_run(args, t0: float) -> int:
    hub = _hub_url(args)
    if _hub_alive(hub):
        ok_h, _, res = _hub_request(hub, "POST", "/api/pipeline/run", {"batch_id": args.batch, "source": "cli"}, timeout=max(20.0, args.timeout))
        if not ok_h or not res.get("ok"):
            return _emit_output(args, command="run", ok=False, exit_code=1, data=res, error=str(res.get("error") or "工作台拒绝启动识别"), t0=t0)
        if getattr(args, "detach", False):
            return _emit_output(args, command="run", ok=True, exit_code=0, data={**res, "via_hub": True, "detached": True}, t0=t0, text_lines=[f"已通过工作台启动自动识别：{res.get('batch_id')}（后台运行）"])
        done_ok, rc, pj = _poll_hub_pipeline(hub, quiet=bool(args.json or args.quiet))
        return _emit_output(args, command="run", ok=done_ok, exit_code=rc, data={"via_hub": True, "batch_id": res.get("batch_id"), "pipeline_job": pj}, error="" if done_ok else str((pj.get("error_report") or {}).get("reason") or pj.get("status") or ""), t0=t0)

    # 工作台未运行时：获取跨进程任务租约，防止并发撞车
    active = core.set_active_batch(args.batch) if args.batch else core.get_active_batch()
    if not active:
        return _emit_output(args, command="run", ok=False, exit_code=1, data={}, error="尚未导入任何批次，请先运行 ingest 导入试卷", t0=t0)
    lease = core.acquire_pipeline_lease("recognize", active["batch_id"], owner="cli")
    if not lease.get("ok"):
        return _emit_output(args, command="run", ok=False, exit_code=1, data=lease, error=str(lease.get("error") or "已有任务运行中"), t0=t0)
    token = lease["lease"]["token"]
    try:
        infer_cfg = core.get_infer_settings()
        cmd = [
            sys.executable, "-B", str(ROOT / "engine" / "run.py"), "--recognize",
            "--input", active["images_dir"],
            "--out", active["recognize_dir"],
            "--device", str(infer_cfg["infer_device"]),
            "--yolo-batch", str(infer_cfg["yolo_batch"]),
            "--workers", str(infer_cfg["cpu_workers"]),
            "--concurrency", str(infer_cfg["api_concurrency"]),
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=bool(args.json or args.quiet), text=True, encoding="utf-8", errors="replace")
        ok = proc.returncode == 0
        return _emit_output(args, command="run", ok=ok, exit_code=proc.returncode, data={"via_hub": False, "batch_id": active["batch_id"], "returncode": proc.returncode}, error="" if ok else f"自动识别异常退出 ({proc.returncode})", t0=t0)
    finally:
        core.release_pipeline_lease(token)


def cmd_retry(args, t0: float) -> int:
    hub = _hub_url(args)
    if _hub_alive(hub):
        ok_h, _, res = _hub_request(hub, "POST", "/api/pipeline/retry-failed", {"batch_id": args.batch, "source": "cli"}, timeout=max(20.0, args.timeout))
        if not ok_h or not res.get("ok"):
            return _emit_output(args, command="retry", ok=False, exit_code=1, data=res, error=str(res.get("error") or "工作台拒绝启动补跑"), t0=t0)
        if getattr(args, "detach", False):
            return _emit_output(args, command="retry", ok=True, exit_code=0, data={**res, "via_hub": True, "detached": True}, t0=t0, text_lines=[f"已通过工作台启动失败图块补跑：{res.get('batch_id')}（后台运行）"])
        done_ok, rc, pj = _poll_hub_pipeline(hub, quiet=bool(args.json or args.quiet))
        return _emit_output(args, command="retry", ok=done_ok, exit_code=rc, data={"via_hub": True, "batch_id": res.get("batch_id"), "pipeline_job": pj}, error="" if done_ok else str(pj.get("status") or ""), t0=t0)

    active = core.set_active_batch(args.batch) if args.batch else core.get_active_batch()
    if not active:
        return _emit_output(args, command="retry", ok=False, exit_code=1, data={}, error="还没有导入批次", t0=t0)
    lease = core.acquire_pipeline_lease("retry", active["batch_id"], owner="cli")
    if not lease.get("ok"):
        return _emit_output(args, command="retry", ok=False, exit_code=1, data=lease, error=str(lease.get("error") or "已有任务运行中"), t0=t0)
    token = lease["lease"]["token"]
    try:
        from scripts.retry_failed_tiles import retry_failed_grids
        infer_cfg = core.get_infer_settings()
        conc = int(getattr(args, "concurrency", None) or infer_cfg["api_concurrency"])
        res = retry_failed_grids(
            Path(active["recognize_dir"]),
            concurrency=conc,
            progress_cb=(None if (args.json or args.quiet) else (lambda m: print(m, flush=True))),
            actor="cli",
        )
        ok = bool(res.get("ok"))
        return _emit_output(args, command="retry", ok=ok, exit_code=0 if ok else int(res.get("exit_code") or 1), data={**res, "via_hub": False, "batch_id": active["batch_id"]}, error="" if ok else str(res.get("error") or ""), t0=t0, text_lines=[str(res.get("message") or res.get("error") or "")])
    finally:
        core.release_pipeline_lease(token)


def cmd_stop(args, t0: float) -> int:
    hub = _hub_url(args)
    if not _hub_alive(hub):
        return _emit_output(args, command="stop", ok=False, exit_code=1, data={}, error=f"工作台未运行（{hub}）", t0=t0)
    ok_h, _, res = _hub_request(hub, "POST", "/api/pipeline/stop", {}, timeout=min(10.0, args.timeout))
    return _emit_output(args, command="stop", ok=ok_h and bool(res.get("ok")), exit_code=0 if (ok_h and res.get("ok")) else 1, data=res, error="" if res.get("ok") else str(res.get("error") or ""), t0=t0, text_lines=[str(res.get("message") or "")])


def cmd_benchmark(args, t0: float) -> int:
    hub = _hub_url(args)
    samples = max(2, min(8, int(getattr(args, "samples", 4) or 4)))
    if _hub_alive(hub):
        ok_h, _, res = _hub_request(hub, "POST", "/api/benchmark/start", {"samples": samples}, timeout=min(15.0, args.timeout))
        if not ok_h or not res.get("ok"):
            return _emit_output(args, command="benchmark", ok=False, exit_code=1, data=res, error=str(res.get("error") or "无法启动压测"), t0=t0)
        if getattr(args, "detach", False):
            return _emit_output(args, command="benchmark", ok=True, exit_code=0, data={"via_hub": True, "detached": True}, t0=t0, text_lines=["已通过工作台启动本机性能压测"])
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            ok_s, _, st = _hub_request(hub, "GET", "/api/benchmark/status", timeout=5.0)
            bj = (st or {}).get("benchmark_job") or {}
            if ok_s and not bj.get("running"):
                ok_b = bj.get("status") == "done"
                return _emit_output(args, command="benchmark", ok=ok_b, exit_code=0 if ok_b else 1, data={"via_hub": True, "benchmark_job": bj}, error=str(bj.get("error") or ""), t0=t0, text_lines=[str((bj.get("result") or {}).get("summary") or bj.get("error") or "压测结束")])
            time.sleep(0.8)
        return _emit_output(args, command="benchmark", ok=False, exit_code=1, data={}, error="等待压测超时", t0=t0)

    from scripts.benchmark_local import run_benchmark
    res = run_benchmark(ROOT, sample_count=samples, progress_cb=(None if (args.json or args.quiet) else (lambda m: print(m, flush=True))))
    ok = bool(res.get("ok"))
    return _emit_output(args, command="benchmark", ok=ok, exit_code=0 if ok else 1, data={**res, "via_hub": False}, error="" if ok else str(res.get("error") or ""), t0=t0, text_lines=[str(res.get("summary") or res.get("error") or "")])


def cmd_runs(args, t0: float) -> int:
    hub = _hub_url(args)
    action = getattr(args, "action", "list") or "list"
    if action == "show":
        rid = str(getattr(args, "run_id", "") or "").strip()
        if not rid:
            return _emit_output(args, command="runs", ok=False, exit_code=2, data={}, error="show 子命令需要提供 run_id", t0=t0)
        if _hub_alive(hub):
            ok_h, _, det = _hub_request(hub, "GET", f"/api/runs/{urllib.parse.quote(rid)}", timeout=min(10.0, args.timeout))
            if ok_h and det.get("ok"):
                return _emit_output(args, command="runs", ok=True, exit_code=0, data=det, t0=t0, text_lines=[json.dumps(det, ensure_ascii=False, indent=2)])
        det = run_log.get_run_detail(core.DEFAULT_BATCHES_ROOT, rid, batch_id=getattr(args, "batch", None))
        if not det:
            return _emit_output(args, command="runs", ok=False, exit_code=1, data={}, error=f"未找到运行记录：{rid}", t0=t0)
        return _emit_output(args, command="runs", ok=True, exit_code=0, data=det, t0=t0, text_lines=[json.dumps(det, ensure_ascii=False, indent=2)])

    bid = getattr(args, "batch", None)
    lim = int(getattr(args, "limit", 50) or 50)
    runs = None
    if _hub_alive(hub):
        qs = urllib.parse.urlencode({k: v for k, v in [("batch_id", bid), ("limit", lim)] if v})
        ok_h, _, body = _hub_request(hub, "GET", f"/api/runs?{qs}", timeout=min(10.0, args.timeout))
        if ok_h and body.get("ok"):
            runs = body.get("runs") or []
    if runs is None:
        runs = run_log.list_runs(core.DEFAULT_BATCHES_ROOT, batch_id=bid, limit=lim)
    lines = [f"历史运行记录（共 {len(runs)} 条）："]
    for r in runs:
        lines.append(f"  - {r['run_id']} | {r['kind']:9s} | {r['status']:6s} | {r['batch_id']} | {r.get('started_at', '')}")
    return _emit_output(args, command="runs", ok=True, exit_code=0, data={"runs": runs, "count": len(runs)}, t0=t0, text_lines=lines)


def cmd_logs(args, t0: float) -> int:
    hub = _hub_url(args)
    params = {
        "run_id": getattr(args, "run", "") or "",
        "batch_id": getattr(args, "batch", "") or "",
        "level": getattr(args, "level", "") or "",
        "event": getattr(args, "event", "") or "",
        "page_id": getattr(args, "page", "") or "",
        "grid_id": getattr(args, "grid", "") or "",
        "q": getattr(args, "query", "") or "",
        "limit": int(getattr(args, "limit", 100) or 100),
    }
    res = None
    if _hub_alive(hub):
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        ok_h, _, body = _hub_request(hub, "GET", f"/api/logs?{qs}", timeout=min(10.0, args.timeout))
        if ok_h and body.get("ok"):
            res = body
    if res is None:
        res = run_log.query_events(core.DEFAULT_BATCHES_ROOT, **params)
    items = res.get("items") or []
    lines = [f"事件日志（命中 {res.get('total', len(items))} 条，显示 {len(items)} 条）："]
    for it in items:
        lines.append(f"  [{it.get('ts', '')}] [{it.get('level', 'INFO')}] {it.get('event', '')} | {it.get('message', '')}")
    return _emit_output(args, command="logs", ok=True, exit_code=0, data=res, t0=t0, text_lines=lines)


def cmd_open(args, t0: float) -> int:
    hub = _hub_url(args)
    ok_h, _, res = _hub_request(hub, "POST", "/api/service/ensure", {"task_id": args.task, "m": args.m}, timeout=max(12.0, args.timeout))
    if not ok_h or not res.get("ok"):
        err = res.get("error") or f"工作台未运行或拉起失败（{hub}），请先运行 `python -m apps.workbench serve --open`"
        return _emit_output(args, command="open", ok=False, exit_code=1, data=res, error=str(err), t0=t0)
    url = f"http://127.0.0.1:{res['port']}/"
    if not args.no_browser:
        wb_serve.open_app_window(url)
    return _emit_output(args, command="open", ok=True, exit_code=0, data={**res, "url": url}, t0=t0, text_lines=[f"服务已就绪：{url}"])


def main():
    t0 = time.monotonic()
    # 共享全局参数父解析器：无论放在子命令前还是子命令后均有效（彻底消除 --hub 位置坑）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hub", default=argparse.SUPPRESS, help="本地工作台控制中心地址（默认 http://127.0.0.1:8888）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出统一机器可读 JSON")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="静默非关键输出")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="禁用颜色输出")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="HTTP 请求超时（秒）")

    ap = argparse.ArgumentParser(description="自动改卷统一工作台（GUI/CLI 统一 Hub 入口）")
    ap.add_argument("--hub", default="http://127.0.0.1:8888", help="本地工作台控制中心地址")
    ap.add_argument("--json", action="store_true", default=False, help="输出统一机器可读 JSON")
    ap.add_argument("--quiet", action="store_true", default=False, help="静默非关键输出")
    ap.add_argument("--no-color", action="store_true", default=False, help="禁用颜色输出")
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP 请求超时（秒）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", parents=[common], help="启动工作台控制中心")
    p_serve.add_argument("--port", type=int, default=8888)
    p_serve.add_argument("--open", action="store_true")

    p_chk = sub.add_parser("check", parents=[common], help="检查运行环境与模型权重并回写工作台")
    p_chk.add_argument("--deep", action="store_true", help="执行真实 import 深度校验")

    p_pip = sub.add_parser("repair-pip", parents=[common], help="测速选源自动安装缺失 Python 包")
    p_pip.add_argument("packages", nargs="*")
    p_pip.add_argument("--dry-run", action="store_true", help="仅列出待安装包不实际安装")

    p_ag = sub.add_parser("agent-prompt", parents=[common], help="生成脱敏诊断提示词与 ZCode 命令")
    p_ag.add_argument("--launch", action="store_true")
    p_ag.add_argument("--note", default="")
    p_ag.add_argument("--mode", default="plan", choices=["plan", "guarded"])

    p_ing = sub.add_parser("ingest", parents=[common], help="非破坏性导入并统一命名新试卷")
    p_ing.add_argument("--source", required=True, help="原始试卷照片文件夹")
    p_ing.add_argument("--subject", required=True, help="学科（如 物理/数学/化学）")
    p_ing.add_argument("--name", default=None, help="自定义批次名")
    p_ing.add_argument("--dry-run", action="store_true", help="仅预览扫描结果不实际写入")

    p_run = sub.add_parser("run", parents=[common], help="通过 Hub（或共享租约）运行自动识别流水线")
    p_run.add_argument("--batch", default=None)
    p_run.add_argument("--detach", action="store_true", help="提交到 Hub 后立即返回不等待结束")

    p_rty = sub.add_parser("retry", parents=[common], help="仅补跑失败或结构异常题块（自动单题降级）")
    p_rty.add_argument("--batch", default=None)
    p_rty.add_argument("--concurrency", type=int, default=None)
    p_rty.add_argument("--detach", action="store_true")

    sub.add_parser("stop", parents=[common], help="停止工作台正在运行的识别/补跑任务")

    p_bench = sub.add_parser("benchmark", parents=[common], help="运行零 API 费用本机性能压测")
    p_bench.add_argument("--samples", type=int, default=4)
    p_bench.add_argument("--detach", action="store_true")

    sub.add_parser("vlm-test", parents=[common], help="测试视觉大模型 API 连通性（64x64 图）")

    sub.add_parser("status", parents=[common], help="查看当前批次、各审核进度与在线成员")

    p_doc = sub.add_parser("doctor", parents=[common], help="输出本机硬件/Python/ZCode/端口等诊断信息")
    p_doc.add_argument("--probe-zcode", action="store_true", help="同时探测 ZCode 版本号")

    sub.add_parser("capabilities", parents=[common], help="输出 CLI 机器可读能力清单")

    p_runs = sub.add_parser("runs", parents=[common], help="查询历史运行 run 列表或详情")
    p_runs.add_argument("action", nargs="?", default="list", choices=["list", "show"])
    p_runs.add_argument("run_id", nargs="?", default="")
    p_runs.add_argument("--batch", default=None)
    p_runs.add_argument("--limit", type=int, default=50)

    p_logs = sub.add_parser("logs", parents=[common], help="查询统一事件日志（runs + cli/ai）")
    p_logs.add_argument("--run", default="")
    p_logs.add_argument("--batch", default="")
    p_logs.add_argument("--level", default="")
    p_logs.add_argument("--event", default="")
    p_logs.add_argument("--page", default="")
    p_logs.add_argument("--grid", default="")
    p_logs.add_argument("-q", "--query", default="")
    p_logs.add_argument("--limit", type=int, default=100)

    p_op = sub.add_parser("open", parents=[common], help="一键启动并打开指定审核页")
    p_op.add_argument("task", choices=["review", "annotate", "choice", "monitor"])
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
        "retry": cmd_retry,
        "stop": cmd_stop,
        "benchmark": cmd_benchmark,
        "vlm-test": cmd_vlm_test,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "capabilities": cmd_capabilities,
        "runs": cmd_runs,
        "logs": cmd_logs,
        "open": cmd_open,
    }[args.cmd](args, t0)


if __name__ == "__main__":
    raise SystemExit(main())
