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
import os
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
import agent_providers  # noqa: E402
import capture as capture_mod  # noqa: E402
import server as wb_serve  # noqa: E402
from engine import run_log  # noqa: E402


_SENSITIVE_CLI_FLAGS = {"--api-key", "--password", "--token", "--secret", "--key"}


def _sanitize_argv(argv: list[str]) -> list[str]:
    """脱敏 CLI 参数列表，防止把命令行传入的密钥或密码写入 cli-*.jsonl 日志。"""
    out: list[str] = []
    hide_next = False
    for tok in argv:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        low = tok.lower()
        if low in _SENSITIVE_CLI_FLAGS:
            out.append(tok)
            hide_next = True
            continue
        matched_eq = False
        for fl in _SENSITIVE_CLI_FLAGS:
            if low.startswith(fl + "="):
                out.append(tok.split("=", 1)[0] + "=***")
                matched_eq = True
                break
        if matched_eq:
            continue
        out.append(run_log.mask_secret(tok))
    return out


def _review_request(review_url: str, method: str, path: str, payload: dict | None = None, timeout: float = 15.0) -> tuple[bool, int, dict]:
    url = f"{review_url.rstrip('/')}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"X-AutoGrade-Internal": "cli"}
    if data is not None:
        headers["Content-Type"] = "application/json"
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
            argv=_sanitize_argv(sys.argv[1:]),
            source="cli",
            actor="cli",
            exit_code=exit_code,
            duration_ms=dur_ms,
            ok=ok,
            batch_id=str(data.get("batch_id") or (data.get("active_batch") or {}).get("batch_id") or ""),
            message=run_log.mask_secret(error or f"cli {command} {'ok' if ok else 'failed'}"),
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
            {"name": "config", "safety": "mutating", "args": ["get|set|set-vlm|import|export", "--json"], "desc": "查看/修改工作台配置与 VLM 密钥，或整包导入导出配置"},
            {"name": "weights", "safety": "mutating", "args": ["unpack --password <pwd>", "--json"], "desc": "解压加密模型权重包"},
            {"name": "agents", "safety": "read_only", "args": ["list|test", "--deep", "--probe-version", "--json"], "desc": "探测或测试本机可用 CLI Agent（ZCode/OpenCode/CodeBuddy/豆包/Antigravity）"},
            {"name": "capture", "safety": "read_only", "args": ["--target workbench|review", "--mode auto|ui|canvas", "--page", "--item", "--selector", "--crop", "--with-elements", "--som", "--json"], "desc": "后台离屏无遮挡截图并导出交互元素清单"},
            {"name": "ui", "safety": "mutating", "args": ["state|focus|navigate|click", "--target workbench|review", "--selector", "--page", "--item", "--view", "--json"], "desc": "查询前台 UI 焦点或向浏览器投送高亮焦点/跳转指令"},
            {"name": "review", "safety": "mutating", "args": ["status|page|set-text|set-choice|delete-box|re-ocr|confirm|nav", "--review-url", "--json"], "desc": "8904 审阅页同构 CRUD 操作（页级锁防竞争冒险并实时推给前端）"},
        ],
        "agents": agent_providers.detect_all_agents(core.load_config(), deep=False),
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


def cmd_config(args, t0: float) -> int:
    hub = _hub_url(args)
    action = getattr(args, "action", "get") or "get"

    if action == "get":
        cfg = core.load_config()
        vlm = core.get_vlm_settings(ROOT)
        infer = core.get_infer_settings()
        agents = agent_providers.detect_all_agents(cfg, deep=False)
        data = {"workbench": cfg, "vlm": vlm, "infer_settings": infer, "agents": agents}
        lines = [
            f"大模型 API：{vlm.get('provider')} / {vlm.get('model')}（密钥：{vlm.get('api_key_masked') or '未配置'}）",
            f"推理参数：device={infer.get('infer_device')} batch={infer.get('yolo_batch')} workers={infer.get('cpu_workers')} api_conc={infer.get('api_concurrency')}",
            f"默认 AI Agent：{agents.get('recommended')}（可用：{', '.join(agents.get('available') or []) or '无'}）",
        ]
        return _emit_output(args, command="config.get", ok=True, exit_code=0, data=data, t0=t0, text_lines=lines)

    if action == "set":
        patch: dict[str, Any] = {"source": "cli"}
        if getattr(args, "infer_device", None) is not None:
            patch["infer_device"] = args.infer_device
        if getattr(args, "yolo_batch", None) is not None:
            patch["yolo_batch"] = int(args.yolo_batch)
        if getattr(args, "cpu_workers", None) is not None:
            patch["cpu_workers"] = int(args.cpu_workers)
        if getattr(args, "api_concurrency", None) is not None:
            patch["api_concurrency"] = int(args.api_concurrency)
        if getattr(args, "agent_provider", None) is not None:
            patch["ai_helper_provider"] = args.agent_provider
        if getattr(args, "ai_mode", None) is not None:
            patch["ai_default_mode"] = args.ai_mode
        if getattr(args, "ai_screenshot", None) is not None:
            patch["ai_default_screenshot"] = str(args.ai_screenshot).lower() in ("1", "true", "yes", "on")
        if getattr(args, "custom_agent_cmd", None) is not None:
            patch["agent_custom_cmd"] = args.custom_agent_cmd
        if getattr(args, "work_mode", None) is not None:
            patch["work_mode"] = args.work_mode
        if getattr(args, "memory_crops", None) is not None:
            patch["memory_crops"] = str(args.memory_crops).lower() in ("1", "true", "yes", "on")
        if getattr(args, "training_db_dir", None) is not None:
            patch["training_db_dir"] = args.training_db_dir
        ap_list = getattr(args, "agent_path", None) or []
        if ap_list:
            ap_map: dict[str, str] = {}
            for item in ap_list:
                if "=" in item:
                    k, v = item.split("=", 1)
                    ap_map[k.strip().lower()] = v.strip()
            if ap_map:
                patch["agent_paths"] = ap_map

        if _hub_alive(hub):
            ok_h, _, res = _hub_request(hub, "POST", "/api/config", patch, timeout=min(10.0, args.timeout))
            if ok_h and res.get("ok"):
                return _emit_output(args, command="config.set", ok=True, exit_code=0, data={**res, "via_hub": True}, t0=t0, text_lines=["工作台配置已更新并同步至界面"])
        patch.pop("source", None)
        if "agent_paths" in patch:
            cur_ap = dict(core.load_config().get("agent_paths") or {})
            cur_ap.update(patch["agent_paths"])
            patch["agent_paths"] = cur_ap
        cfg = core.save_config(patch)
        return _emit_output(args, command="config.set", ok=True, exit_code=0, data={"config": cfg, "via_hub": False}, t0=t0, text_lines=["工作台配置已保存"])

    if action == "set-vlm":
        api_key = str(getattr(args, "api_key", "") or "").strip()
        key_env = str(getattr(args, "api_key_env", "") or "").strip()
        if not api_key and key_env:
            api_key = str(os.environ.get(key_env) or "").strip()
        payload = {
            "provider": str(getattr(args, "provider", "") or core.get_vlm_settings(ROOT).get("provider") or "doubao"),
            "base_url": str(getattr(args, "base_url", "") or ""),
            "model": str(getattr(args, "model", "") or ""),
            "api_key": api_key,
        }
        via_hub = False
        if _hub_alive(hub):
            ok_h, _, res = _hub_request(hub, "POST", "/api/vlm/config", payload, timeout=min(12.0, args.timeout))
            via_hub = ok_h
        else:
            res = core.save_vlm_settings(
                provider=payload["provider"],
                base_url=payload["base_url"],
                api_key=payload["api_key"],
                model=payload["model"],
                root=ROOT,
            )
        if not res.get("ok"):
            return _emit_output(args, command="config.set-vlm", ok=False, exit_code=1, data=res, error=str(res.get("error") or "保存 VLM 配置失败"), t0=t0)
        test_res = None
        if getattr(args, "test", False):
            test_res = core.test_vlm_settings(root=ROOT)
        cfg_safe = res.get("config") or core.get_vlm_settings(ROOT)
        lines = [f"已写入大模型配置：{cfg_safe.get('provider')} / {cfg_safe.get('model')}（密钥：{cfg_safe.get('api_key_masked') or '未变'}）"]
        if test_res is not None:
            lines.append("连通测试：" + ("通过" if test_res.get("ok") else f"失败：{test_res.get('error')}"))
        ok_all = bool(res.get("ok") and (test_res is None or test_res.get("ok")))
        return _emit_output(args, command="config.set-vlm", ok=ok_all, exit_code=0 if ok_all else 1, data={"config": cfg_safe, "test": test_res, "via_hub": via_hub}, error="" if ok_all else str((test_res or {}).get("error") or ""), t0=t0, text_lines=lines)

    if action == "export":
        inc_sec = bool(getattr(args, "include_secrets", False))
        cfg_now = core.load_config()
        vlm_now = core.get_vlm_settings(ROOT)
        vlm_out: dict[str, Any] = {
            "provider": vlm_now.get("provider", "doubao"),
            "base_url": vlm_now.get("base_url", ""),
            "model": vlm_now.get("model", ""),
        }
        if inc_sec:
            rk = core._read_env_key(ROOT, "VLM_API_KEY") or core._read_env_key(ROOT, "ARK_API_KEY")
            if rk:
                vlm_out["api_key"] = rk
        else:
            vlm_out["api_key_masked"] = vlm_now.get("api_key_masked", "")
        bundle = {
            "schema_version": 1,
            "exported_at": core.now_str(),
            "include_secrets": inc_sec,
            "workbench": cfg_now,
            "vlm": vlm_out,
        }
        out_p = getattr(args, "out", "") or ""
        if out_p:
            p = Path(out_p).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
            # 返回给终端/日志的结果不暴露明文密钥
            safe_data = {"path": str(p), "include_secrets": inc_sec, "vlm_model": vlm_out.get("model", "")}
            return _emit_output(args, command="config.export", ok=True, exit_code=0, data=safe_data, t0=t0, text_lines=[f"已导出配置包到：{p}（{'含密钥，请妥善保管勿上传 Git' if inc_sec else '已脱敏不含密钥'}）"])
        # 直接输出到 stdout 时不返回明文密钥
        bundle_safe = {**bundle, "vlm": {k: ("***" if k == "api_key" else v) for k, v in vlm_out.items()}}
        return _emit_output(args, command="config.export", ok=True, exit_code=0, data=bundle_safe, t0=t0, text_lines=[json.dumps(bundle_safe, ensure_ascii=False, indent=2)])

    if action == "import":
        fpath = str(getattr(args, "file", "") or "").strip()
        if not fpath or not Path(fpath).is_file():
            return _emit_output(args, command="config.import", ok=False, exit_code=2, data={}, error=f"找不到配置文件：{fpath}", t0=t0)
        raw_txt = Path(fpath).read_text(encoding="utf-8-sig")
        if Path(fpath).suffix.lower() == ".env" or (not raw_txt.lstrip().startswith("{") and "=" in raw_txt):
            # 支持直接导入 .env 文件（按行 KEY=VALUE）
            env_dict: dict[str, str] = {}
            for ln in raw_txt.splitlines():
                s = ln.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                env_dict[k.strip()] = v.strip().strip('"').strip("'")
            payload = {"env": env_dict, "source": "cli"}
        else:
            payload = json.loads(raw_txt)
            if not isinstance(payload, dict):
                return _emit_output(args, command="config.import", ok=False, exit_code=2, data={}, error="配置文件必须是 JSON 对象或 .env 文件", t0=t0)
            payload["source"] = "cli"

        via_hub = False
        if _hub_alive(hub):
            ok_h, _, res = _hub_request(hub, "POST", "/api/config/import", payload, timeout=min(15.0, args.timeout))
            via_hub = ok_h
        else:
            applied = []
            if isinstance(payload.get("workbench"), dict):
                core.save_config(payload["workbench"])
                applied.append("工作台配置")
            if isinstance(payload.get("vlm"), dict) and any(str(payload["vlm"].get(k) or "").strip() for k in ("provider", "base_url", "api_key", "model")):
                vr = core.save_vlm_settings(
                    provider=str(payload["vlm"].get("provider") or "doubao"),
                    base_url=str(payload["vlm"].get("base_url") or ""),
                    api_key=str(payload["vlm"].get("api_key") or ""),
                    model=str(payload["vlm"].get("model") or ""),
                    root=ROOT,
                )
                if not vr.get("ok"):
                    return _emit_output(args, command="config.import", ok=False, exit_code=1, data=vr, error=str(vr.get("error") or "导入 VLM 失败"), t0=t0)
                applied.append("大模型 API 配置与密钥")
            if isinstance(payload.get("env"), dict) and payload["env"]:
                er = core.save_env_keys({str(k): str(v) for k, v in payload["env"].items() if str(v or "").strip()}, root=ROOT)
                if not er.get("ok"):
                    return _emit_output(args, command="config.import", ok=False, exit_code=1, data=er, error=str(er.get("error") or "导入 .env 失败"), t0=t0)
                applied.append(f".env({len(payload['env'])}项)")
            res = {"ok": True, "applied": applied, "message": "配置导入完成：" + ("、".join(applied) or "无变更"), "vlm": core.get_vlm_settings(ROOT)}

        test_res = core.test_vlm_settings(root=ROOT) if (res.get("ok") and getattr(args, "test", False)) else None
        ok_all = bool(res.get("ok") and (test_res is None or test_res.get("ok")))
        lines = [str(res.get("message") or "配置已导入")]
        if test_res is not None:
            lines.append("连通测试：" + ("通过" if test_res.get("ok") else f"失败：{test_res.get('error')}"))
        return _emit_output(args, command="config.import", ok=ok_all, exit_code=0 if ok_all else 1, data={**res, "test": test_res, "via_hub": via_hub}, error="" if ok_all else str((test_res or res).get("error") or ""), t0=t0, text_lines=lines)

    return _emit_output(args, command="config", ok=False, exit_code=2, data={}, error=f"不支持的 config 操作：{action}", t0=t0)


def cmd_weights(args, t0: float) -> int:
    hub = _hub_url(args)
    pwd = str(getattr(args, "password", "") or "")
    if not pwd:
        return _emit_output(args, command="weights.unpack", ok=False, exit_code=2, data={}, error="请通过 --password 提供权重包解压密码", t0=t0)
    if _hub_alive(hub):
        ok_h, _, res = _hub_request(hub, "POST", "/api/repair/weights", {"password": pwd}, timeout=max(60.0, args.timeout))
        if ok_h:
            ok = bool(res.get("ok"))
            return _emit_output(args, command="weights.unpack", ok=ok, exit_code=0 if ok else 1, data={**res, "via_hub": True}, error="" if ok else str(res.get("error") or ""), t0=t0, text_lines=[str(res.get("message") or res.get("error") or "")])
    res = core.unpack_weights(pwd, root=ROOT)
    ok = bool(res.get("ok"))
    return _emit_output(args, command="weights.unpack", ok=ok, exit_code=0 if ok else 1, data={**res, "via_hub": False}, error="" if ok else str(res.get("error") or ""), t0=t0, text_lines=[str(res.get("message") or res.get("error") or "")])


def cmd_agents(args, t0: float) -> int:
    action = getattr(args, "action", "list") or "list"
    cfg = core.load_config()
    if action == "test":
        prov = str(getattr(args, "provider", "") or "").strip().lower()
        if not prov:
            return _emit_output(args, command="agents.test", ok=False, exit_code=2, data={}, error="请指定要测试的 Agent 名称（如 zcode/opencode/workbuddy/doubao/antigravity）", t0=t0)
        res = agent_providers.test_agent_provider(prov, cfg)
        ok = bool(res.get("ok"))
        return _emit_output(args, command="agents.test", ok=ok, exit_code=0 if ok else 1, data=res, error="" if ok else str(res.get("error") or ""), t0=t0, text_lines=[f"{prov}: {'可用 (' + str(res.get('version', '')) + ')' if ok else res.get('error')}"])
    res = agent_providers.detect_all_agents(cfg, deep=bool(getattr(args, "deep", False)), probe_version=bool(getattr(args, "probe_version", False)))
    lines = [f"本机 CLI Agent 探测（{'扩展扫描' if res['deep'] else '快探'} · {res['elapsed_ms']}ms，推荐：{res['recommended']}）："]
    for a in res["list"]:
        st = f"✓ {a['cli']}" if a["available"] else "✗ 未检测到"
        ver = f" [{a['version']}]" if a.get("version") else ""
        lines.append(f"  - {a['id']:12s} ({a['label']}): {st}{ver}")
    return _emit_output(args, command="agents.list", ok=True, exit_code=0, data=res, t0=t0, text_lines=lines)


def cmd_capture(args, t0: float) -> int:
    hub = _hub_url(args)
    target = str(getattr(args, "target", "workbench") or "workbench").lower()
    mode = str(getattr(args, "mode", "auto") or "auto").lower()
    payload = {
        "target": target,
        "mode": mode,
        "page_id": str(getattr(args, "page", "") or ""),
        "item_id": str(getattr(args, "item", "") or ""),
        "selector": str(getattr(args, "selector", "") or ""),
        "crop": getattr(args, "crop", None),
        "with_elements": not bool(getattr(args, "no_elements", False)),
        "som": bool(getattr(args, "som", False)),
        "mirror_client": str(getattr(args, "client_id", "") or ""),
        "review_port": int(urllib.parse.urlparse(str(getattr(args, "review_url", "http://127.0.0.1:8904"))).port or 8904),
    }
    out_p = Path(args.out).resolve() if getattr(args, "out", None) else None

    if target == "review" and (mode == "canvas" or (mode == "auto" and not payload["selector"] and not payload["som"])):
        active = core.get_active_batch()
        if not active:
            return _emit_output(args, command="capture", ok=False, exit_code=1, data={}, error="当前没有活跃批次", t0=t0)
        res = capture_mod.capture_review_canvas(
            Path(active["recognize_dir"]),
            core.DEFAULT_BATCHES_ROOT,
            page_id=payload["page_id"],
            item_id=payload["item_id"],
            crop=payload["crop"],
            with_elements=payload["with_elements"],
            out_path=out_p,
        )
    elif _hub_alive(hub) and out_p is None:
        ok_h, _, res = _hub_request(hub, "POST", "/api/capture", payload, timeout=max(25.0, args.timeout))
        if not ok_h and not res.get("ok"):
            return _emit_output(args, command="capture", ok=False, exit_code=1, data=res, error=str(res.get("error") or "截图失败"), t0=t0)
    else:
        url = str(getattr(args, "review_url", "http://127.0.0.1:8904")) if target == "review" else hub
        res = capture_mod.capture_headless_ui(
            url,
            core.DEFAULT_BATCHES_ROOT,
            target=target,
            mirror_client=payload["mirror_client"],
            selector=payload["selector"],
            crop=payload["crop"],
            with_elements=payload["with_elements"],
            som=payload["som"],
            out_path=out_p,
        )
    ok = bool(res.get("ok"))
    lines = [f"截图完成：{res.get('path')}（{res.get('width')}x{res.get('height')}，元素 {res.get('elements_count', 0)} 个）"] if ok else [str(res.get("error") or "截图失败")]
    return _emit_output(args, command="capture", ok=ok, exit_code=0 if ok else 1, data=res, error="" if ok else str(res.get("error") or ""), t0=t0, text_lines=lines)


def cmd_ui(args, t0: float) -> int:
    hub = _hub_url(args)
    review_url = str(getattr(args, "review_url", "http://127.0.0.1:8904") or "http://127.0.0.1:8904")
    target = str(getattr(args, "target", "workbench") or "workbench").lower()
    action = str(getattr(args, "action", "state") or "state").lower()

    if action == "state":
        if target == "review":
            ok, _, res = _review_request(review_url, "GET", "/api/ui-state", timeout=min(10.0, args.timeout))
        else:
            ok, _, res = _hub_request(hub, "GET", "/api/ui-state", timeout=min(10.0, args.timeout))
        return _emit_output(args, command="ui.state", ok=ok and bool(res.get("ok")), exit_code=0 if (ok and res.get("ok")) else 1, data=res, error="" if ok else str(res.get("error") or "无法读取界面状态"), t0=t0, text_lines=[json.dumps(res.get("active") or {}, ensure_ascii=False, indent=2)])

    payload = {
        "source": "ai",
        "action": action,
        "target": str(getattr(args, "selector", "") or ""),
        "page_id": str(getattr(args, "page", "") or ""),
        "item_id": str(getattr(args, "item", "") or ""),
        "view": str(getattr(args, "view", "") or ""),
        "label": str(getattr(args, "label", "") or ""),
        "client_id": str(getattr(args, "client_id", "") or ""),
        "force_nav": bool(getattr(args, "force_nav", False)),
    }
    if target == "review":
        ok, _, res = _review_request(review_url, "POST", "/api/ui/command", payload, timeout=min(10.0, args.timeout))
    else:
        ok, _, res = _hub_request(hub, "POST", "/api/ui/command", payload, timeout=min(10.0, args.timeout))
    ok_all = bool(ok and res.get("ok"))
    return _emit_output(args, command=f"ui.{action}", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or "投送 UI 指令失败"), t0=t0, text_lines=[f"已向前台 {target} 投送指令：{action} {payload['target'] or payload['item_id'] or payload['page_id'] or payload['view']}"])


def cmd_review(args, t0: float) -> int:
    review_url = str(getattr(args, "review_url", "http://127.0.0.1:8904") or "http://127.0.0.1:8904")
    action = str(getattr(args, "action", "status") or "status").lower()
    to = min(30.0, args.timeout)

    if action == "status":
        ok, _, res = _review_request(review_url, "GET", "/api/review/status", timeout=to)
        ok_all = bool(ok and res.get("ok"))
        lines = [f"8904 审阅进度：{res.get('pages_reviewed', 0)}/{res.get('pages_total', 0)} 页 | 异常/待复核题块：{res.get('issue_count', 0)}"] if ok_all else [str(res.get("error") or "8904 未启动")]
        return _emit_output(args, command="review.status", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=lines)

    if action == "page":
        pid = str(getattr(args, "page", "") or "").strip()
        if not pid:
            return _emit_output(args, command="review.page", ok=False, exit_code=2, data={}, error="请提供 --page <page_id>", t0=t0)
        ok, _, res = _review_request(review_url, "GET", f"/api/review/page/{urllib.parse.quote(pid)}", timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.page", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"第 {pid} 页（rev={res.get('page_rev', 0)}）：{len((res.get('page') or {}).get('items') or [])} 个题目条目"])

    if action == "set-text":
        payload = {
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "text": str(getattr(args, "text", "") or ""),
            "label": str(getattr(args, "label", "已订正") or "已订正"),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        if getattr(args, "expected_rev", None) is not None:
            payload["expected_page_rev"] = int(args.expected_rev)
        ok, _, res = _review_request(review_url, "POST", "/api/review/set-text", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.set-text", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已更新 {payload['page_id']}/{payload['item_id']} 订正文字并推送到前台"])

    if action == "set-choice":
        payload = {
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "answer": str(getattr(args, "answer", "") or ""),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        ok, _, res = _review_request(review_url, "POST", "/api/review/set-choice", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.set-choice", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已修改选择题 {payload['item_id']} 为 {payload['answer']} 并推送到前台"])

    if action == "delete-box":
        payload: dict[str, Any] = {
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        if getattr(args, "box_index", None) is not None:
            payload["box_index"] = int(args.box_index)
        ok, _, res = _review_request(review_url, "POST", "/api/review/delete-box", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.delete-box", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已删除标框并同步到前台（剩余 {res.get('remaining_items', 0)} 题）"])

    if action == "confirm":
        payload = {
            "page_id": str(getattr(args, "page", "") or ""),
            "page_status": str(getattr(args, "label", "已确认") or "已确认"),
            "note": str(getattr(args, "text", "") or ""),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        ok, _, res = _review_request(review_url, "POST", "/api/submit-page", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.confirm", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已整页确认 {payload['page_id']} 并同步到前台"])

    if action == "re-ocr":
        pid = str(getattr(args, "page", "") or "")
        iid = str(getattr(args, "item", "") or "")
        ok_p, _, pdet = _review_request(review_url, "GET", f"/api/review/page/{urllib.parse.quote(pid)}", timeout=to)
        if not ok_p or not pdet.get("ok"):
            return _emit_output(args, command="review.re-ocr", ok=False, exit_code=1, data=pdet, error=str(pdet.get("error") or "读取目标页失败"), t0=t0)
        it = next((x for x in ((pdet.get("page") or {}).get("items") or []) if x.get("id") == iid), None)
        if not it:
            return _emit_output(args, command="review.re-ocr", ok=False, exit_code=1, data={}, error=f"找不到题块 {iid}", t0=t0)
        cls = 0 if it.get("kind") == "identity" else 5
        ok, _, res = _review_request(review_url, "POST", "/api/recognize-box", {
            "page_id": pid,
            "targets": [{"cls": cls, "box": it["box"], "item_id": iid}],
        }, timeout=max(60.0, args.timeout))
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.re-ocr", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已重新识别 {iid} 并落盘推送前台"])

    if action == "set-blank":
        payload = {
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "sub": str(getattr(args, "sub", "无小题号") or "无小题号"),
            "blank": str(getattr(args, "blank", "1") or "1"),
            "text": str(getattr(args, "text", "") or ""),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        ok, _, res = _review_request(review_url, "POST", "/api/review/set-blank", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.set-blank", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已更新单空 {payload['item_id']} ({payload['sub']})第{payload['blank']}空"])

    if action == "re-ocr-subbox":
        raw_box = str(getattr(args, "box", "") or "").strip()
        try:
            box_vals = [float(x.strip()) for x in raw_box.split(",") if x.strip()]
            if len(box_vals) != 4:
                raise ValueError
        except ValueError:
            return _emit_output(args, command="review.re-ocr-subbox", ok=False, exit_code=2, data={}, error="请提供 --box x1,y1,x2,y2", t0=t0)
        raw_targets = getattr(args, "target_slot", None) or []
        targets = []
        for ts in raw_targets:
            if ":" in ts:
                s_k, b_k = ts.split(":", 1)
                targets.append({"sub": s_k.strip() or "无小题号", "blank": b_k.strip() or "1"})
        if not targets:
            targets = [{"sub": str(getattr(args, "sub", "无小题号") or "无小题号"), "blank": str(getattr(args, "blank", "1") or "1")}]
        payload = {
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "box": box_vals,
            "targets": targets,
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        ok, _, res = _review_request(review_url, "POST", "/api/review/re-ocr-subbox", payload, timeout=max(60.0, args.timeout))
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.re-ocr-subbox", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"局部真子集小框重识别完成：{json.dumps(res.get('values') or {}, ensure_ascii=False)}"])

    if action == "purge-unmatched":
        payload = {
            "scope": str(getattr(args, "scope", "page") or "page"),
            "page_id": str(getattr(args, "page", "") or ""),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        ok, _, res = _review_request(review_url, "POST", "/api/review/purge-unmatched-fills", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.purge-unmatched", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已清理 {res.get('removed_count', 0)} 个未匹配大题号填空框"])

    if action == "judge-blank":
        payload = {
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "slot": str(getattr(args, "slot", "choice") or "choice"),
            "status": str(getattr(args, "judge", "correct") or "correct"),
            "reviewer": str(getattr(args, "reviewer", "AI助手") or "AI助手"),
        }
        if getattr(args, "score", None) is not None:
            payload["score"] = float(args.score)
        ok, _, res = _review_request(review_url, "POST", "/api/review/judge-blank", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.judge-blank", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已手动改判 {payload['item_id']}/{payload['slot']} -> {payload['status']}"])

    if action == "grading":
        ok, _, res = _review_request(review_url, "GET", "/api/grading", timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.grading", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"批改统计：共 {res.get('page_count', 0)} 页，均分 {res.get('average_score', 0)}"])

    if action == "nav":
        payload = {
            "source": "ai",
            "action": "navigate",
            "page_id": str(getattr(args, "page", "") or ""),
            "item_id": str(getattr(args, "item", "") or ""),
            "force_nav": True,
            "label": str(getattr(args, "label", "") or f"跳转到 {getattr(args, 'page', '')}"),
        }
        ok, _, res = _review_request(review_url, "POST", "/api/ui/command", payload, timeout=to)
        ok_all = bool(ok and res.get("ok"))
        return _emit_output(args, command="review.nav", ok=ok_all, exit_code=0 if ok_all else 1, data=res, error="" if ok_all else str(res.get("error") or ""), t0=t0, text_lines=[f"已推送跳转指令到 8904：{payload['page_id']} {payload['item_id']}"])

    return _emit_output(args, command="review", ok=False, exit_code=2, data={}, error=f"不支持的 review 操作：{action}", t0=t0)


def cmd_schema(args, t0: float) -> int:
    from engine.exam_schema import load_batch_schema, save_batch_schema, summarize_schema, extract_answer_source, parse_schema_via_vlm, grade_batch, export_scores_csv
    hub = _hub_url(args)
    action = str(getattr(args, "action", "get") or "get").lower()
    bid = str(getattr(args, "batch", "") or "").strip()
    batch = (
        next((b for b in core.list_batches(core.DEFAULT_BATCHES_ROOT) if b["batch_id"] == bid), None)
        if bid else core.get_active_batch(core.DEFAULT_BATCHES_ROOT)
    )
    if not batch:
        return _emit_output(args, command=f"schema.{action}", ok=False, exit_code=2, data={}, error="当前没有可用批次，请先导入批次或指定 --batch", t0=t0)
    bdir = core.batch_root_dir(batch)

    if action == "get":
        sc = load_batch_schema(bdir)
        summ = summarize_schema(sc)
        lines = [f"批次 {batch['batch_id']} 标准答案：{'已配置' if summ['configured'] else '未配置（通用模式）'} | 选择 {summ['choice_count']} 题 | 填空 {summ['fill_question_count']} 大题({summ['fill_blank_count']}空) | 满分 {summ['total_possible_score']} | 多选漏选比例 {summ['multi_choice_partial_ratio']}"]
        return _emit_output(args, command="schema.get", ok=True, exit_code=0, data={"batch_id": batch["batch_id"], "schema": sc, "summary": summ}, t0=t0, text_lines=lines)

    if action == "set":
        fpath = str(getattr(args, "file", "") or "").strip()
        partial_ratio = getattr(args, "partial_ratio", None)
        payload: dict[str, Any] = {"batch_id": batch["batch_id"], "source": "cli"}
        if fpath:
            p = Path(fpath)
            if not p.is_file():
                return _emit_output(args, command="schema.set", ok=False, exit_code=2, data={}, error=f"找不到 schema JSON 文件：{fpath}", t0=t0)
            payload["schema"] = json.loads(p.read_text(encoding="utf-8-sig"))
        if partial_ratio is not None:
            payload["multi_choice_partial_ratio"] = float(partial_ratio)
        if "schema" not in payload and "multi_choice_partial_ratio" not in payload:
            return _emit_output(args, command="schema.set", ok=False, exit_code=2, data={}, error="请提供 --file <schema.json> 或 --partial-ratio <0~1>", t0=t0)
        if _hub_alive(hub):
            ok_h, _, res = _hub_request(hub, "POST", "/api/schema", payload, timeout=min(15.0, args.timeout))
            if ok_h and res.get("ok"):
                s = res.get("summary") or {}
                return _emit_output(args, command="schema.set", ok=True, exit_code=0, data={**res, "via_hub": True}, t0=t0, text_lines=[f"已保存并同步标准答案：选择 {s.get('choice_count', 0)} 题 / 填空 {s.get('fill_blank_count', 0)} 空 / 满分 {s.get('total_possible_score', 0)}"])
        if "schema" in payload:
            saved = save_batch_schema(bdir, payload["schema"])
        else:
            existing = load_batch_schema(bdir) or {}
            sc_cfg = dict(existing.get("scoring") or {})
            r = float(payload["multi_choice_partial_ratio"])
            sc_cfg["multi_choice_partial_ratio"] = r
            for c in (existing.get("choices") or {}).values():
                if isinstance(c, dict) and c.get("type") == "multi":
                    c["partial_score"] = round(float(c.get("score") or 6.0) * r, 2)
            existing["scoring"] = sc_cfg
            saved = save_batch_schema(bdir, existing)
        s = summarize_schema(saved)
        return _emit_output(args, command="schema.set", ok=True, exit_code=0, data={"batch_id": batch["batch_id"], "schema": saved, "summary": s, "via_hub": False}, t0=t0, text_lines=[f"已保存标准答案到 {bdir / 'exam_schema.json'}"])

    if action == "parse-pdf":
        fpath = str(getattr(args, "file", "") or "").strip()
        if not fpath or not Path(fpath).is_file():
            return _emit_output(args, command="schema.parse-pdf", ok=False, exit_code=2, data={}, error="请提供 --file <答案PDF或图片路径>", t0=t0)
        manifest = extract_answer_source(Path(fpath), bdir / "answer_source")
        if getattr(args, "vlm", False):
            parsed_sc = parse_schema_via_vlm(manifest, root=ROOT, subject=str(batch.get("subject") or ""), title=batch["batch_id"])
            saved = save_batch_schema(bdir, parsed_sc)
            s = summarize_schema(saved)
            return _emit_output(args, command="schema.parse-pdf", ok=True, exit_code=0, data={"batch_id": batch["batch_id"], "manifest": manifest, "schema": saved, "summary": s}, t0=t0, text_lines=[f"VLM 解析完成：选择 {s['choice_count']} 题 / 填空 {s['fill_blank_count']} 空"])
        return _emit_output(args, command="schema.parse-pdf", ok=True, exit_code=0, data={"batch_id": batch["batch_id"], "manifest": manifest}, t0=t0, text_lines=[f"已提取答案源文件（{manifest['page_count']} 页）：{', '.join(manifest.get('rendered_images') or [])}"])

    if action in ("grade", "export-csv"):
        rec_dir = Path(batch["recognize_dir"])
        res_p = rec_dir / "results.json"
        if not res_p.is_file():
            return _emit_output(args, command=f"schema.{action}", ok=False, exit_code=1, data={}, error="该批次尚未完成识别", t0=t0)
        results = core.read_json(res_p, {}) or {}
        fb = core.read_json(rec_dir / "feedback.json", {}) or {}
        sc = load_batch_schema(bdir)
        if action == "export-csv":
            csv_txt = export_scores_csv(results, fb, sc)
            out_p = Path(getattr(args, "out", "") or (bdir / f"{batch['batch_id']}_成绩单.csv")).resolve()
            out_p.write_text(csv_txt, encoding="utf-8")
            return _emit_output(args, command="schema.export-csv", ok=True, exit_code=0, data={"path": str(out_p)}, t0=t0, text_lines=[f"已导出成绩单：{out_p}"])
        graded = grade_batch(results, fb, sc)
        return _emit_output(args, command="schema.grade", ok=True, exit_code=0, data=graded, t0=t0, text_lines=[f"批次 {batch['batch_id']} 批改完成：{graded['page_count']} 页，均分 {graded['average_score']}（最高 {graded['max_score']} / 最低 {graded['min_score']}）"])

    return _emit_output(args, command="schema", ok=False, exit_code=2, data={}, error=f"不支持的 schema 操作：{action}", t0=t0)


def cmd_memory(args, t0: float) -> int:
    from engine.memory_budget import run_memory_crop_preflight
    bid = str(getattr(args, "batch", "") or "").strip()
    batch = (
        next((b for b in core.list_batches(core.DEFAULT_BATCHES_ROOT) if b["batch_id"] == bid), None)
        if bid else core.get_active_batch(core.DEFAULT_BATCHES_ROOT)
    )
    if not batch:
        return _emit_output(args, command="memory.preflight", ok=False, exit_code=2, data={}, error="当前没有可用批次", t0=t0)
    img_dir = Path(batch["images_dir"])
    imgs = sorted(p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in core.IMAGE_EXTS) if img_dir.is_dir() else []
    infer_cfg = core.get_infer_settings()
    plan = run_memory_crop_preflight(imgs, requested_concurrency=infer_cfg["api_concurrency"], requested_yolo_batch=infer_cfg["yolo_batch"])
    plan["batch_id"] = batch["batch_id"]
    return _emit_output(args, command="memory.preflight", ok=True, exit_code=0, data=plan, t0=t0, text_lines=[plan["reason"]])


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

    p_cfg = sub.add_parser("config", parents=[common], help="查看/修改配置、导入 VLM 密钥或整包导入导出配置")
    p_cfg.add_argument("action", nargs="?", default="get", choices=["get", "set", "set-vlm", "import", "export"])
    p_cfg.add_argument("--provider", default="")
    p_cfg.add_argument("--base-url", default="")
    p_cfg.add_argument("--model", default="")
    p_cfg.add_argument("--api-key", default="")
    p_cfg.add_argument("--api-key-env", default="")
    p_cfg.add_argument("--infer-device", default=None)
    p_cfg.add_argument("--yolo-batch", type=int, default=None)
    p_cfg.add_argument("--cpu-workers", type=int, default=None)
    p_cfg.add_argument("--api-concurrency", type=int, default=None)
    p_cfg.add_argument("--agent-provider", default=None)
    p_cfg.add_argument("--agent-path", action="append", default=[], help="手动配置 Agent 路径，格式如 zcode=D:\\Dev\\Zcode")
    p_cfg.add_argument("--custom-agent-cmd", default=None)
    p_cfg.add_argument("--ai-mode", default=None, choices=["yolo", "guarded", "plan"])
    p_cfg.add_argument("--ai-screenshot", default=None)
    p_cfg.add_argument("--work-mode", default=None, choices=["grading", "iteration"])
    p_cfg.add_argument("--memory-crops", default=None)
    p_cfg.add_argument("--training-db-dir", default=None)
    p_cfg.add_argument("--file", default="", help="import 时指定 JSON 配置包或 .env 文件")
    p_cfg.add_argument("--out", default="", help="export 时输出路径")
    p_cfg.add_argument("--include-secrets", action="store_true", help="export 时包含明文 API Key（用于迁移到另一台电脑）")
    p_cfg.add_argument("--test", action="store_true", help="写入 VLM 密钥后立刻测试视觉连通性")

    p_wt = sub.add_parser("weights", parents=[common], help="解压加密模型权重包")
    p_wt.add_argument("action", nargs="?", default="unpack", choices=["unpack"])
    p_wt.add_argument("--password", required=True)

    p_agts = sub.add_parser("agents", parents=[common], help="快探或测试本机可用 CLI Agent")
    p_agts.add_argument("action", nargs="?", default="list", choices=["list", "test"])
    p_agts.add_argument("provider", nargs="?", default="")
    p_agts.add_argument("--deep", action="store_true", help="可选扩展扫描常见开发目录（默认快探不扫盘）")
    p_agts.add_argument("--probe-version", action="store_true")

    p_cap = sub.add_parser("capture", parents=[common], help="后台离屏无遮挡截图（与前台实时状态一致）并导出元素清单")
    p_cap.add_argument("--target", default="workbench", choices=["workbench", "review"])
    p_cap.add_argument("--mode", default="auto", choices=["auto", "ui", "canvas"])
    p_cap.add_argument("--page", default="")
    p_cap.add_argument("--item", default="")
    p_cap.add_argument("--selector", default="")
    p_cap.add_argument("--crop", default=None, help="裁切区域 x1,y1,x2,y2（像素或 0~1 比例）")
    p_cap.add_argument("--som", action="store_true", help="在离屏图上叠加可交互元素数字标号")
    p_cap.add_argument("--with-elements", action="store_true", default=True, help="附带导出可交互元素清单（默认开启）")
    p_cap.add_argument("--no-elements", action="store_true")
    p_cap.add_argument("--client-id", default="")
    p_cap.add_argument("--review-url", default="http://127.0.0.1:8904")
    p_cap.add_argument("--out", default=None)

    p_ui = sub.add_parser("ui", parents=[common], help="查询前台 UI 状态或向浏览器投送焦点/导航/点击指令")
    p_ui.add_argument("action", nargs="?", default="state", choices=["state", "focus", "navigate", "click", "toast"])
    p_ui.add_argument("--target", default="workbench", choices=["workbench", "review"])
    p_ui.add_argument("--selector", default="")
    p_ui.add_argument("--page", default="")
    p_ui.add_argument("--item", default="")
    p_ui.add_argument("--view", default="")
    p_ui.add_argument("--label", default="")
    p_ui.add_argument("--client-id", default="")
    p_ui.add_argument("--force-nav", action="store_true")
    p_ui.add_argument("--review-url", default="http://127.0.0.1:8904")

    p_rev = sub.add_parser("review", parents=[common], help="8904 审阅页同构数据操作（页级锁 + SSE 实时映射 GUI）")
    p_rev.add_argument("action", nargs="?", default="status", choices=["status", "page", "set-text", "set-blank", "set-choice", "delete-box", "re-ocr", "re-ocr-subbox", "purge-unmatched", "judge-blank", "grading", "confirm", "nav"])
    p_rev.add_argument("--page", default="")
    p_rev.add_argument("--item", default="")
    p_rev.add_argument("--sub", default="无小题号")
    p_rev.add_argument("--blank", default="1")
    p_rev.add_argument("--target-slot", action="append", default=[], help="多空联合重识别目标槽位，格式 sub:blank，可重复")
    p_rev.add_argument("--box", default="", help="大框内真子集小框像素坐标 x1,y1,x2,y2")
    p_rev.add_argument("--scope", default="page", choices=["page", "batch"])
    p_rev.add_argument("--slot", default="choice", help="judge-blank 槽位（choice 或 sub:blank）")
    p_rev.add_argument("--judge", default="correct", choices=["correct", "partial", "wrong", "auto"])
    p_rev.add_argument("--score", type=float, default=None)
    p_rev.add_argument("--text", default="")
    p_rev.add_argument("--answer", default="")
    p_rev.add_argument("--label", default="已订正")
    p_rev.add_argument("--box-index", type=int, default=None)
    p_rev.add_argument("--expected-rev", type=int, default=None)
    p_rev.add_argument("--reviewer", default="AI助手")
    p_rev.add_argument("--review-url", default="http://127.0.0.1:8904")

    p_sc = sub.add_parser("schema", parents=[common], help="试卷标准答案/结构配置、PDF提取与成绩导出")
    p_sc.add_argument("action", nargs="?", default="get", choices=["get", "set", "parse-pdf", "grade", "export-csv"])
    p_sc.add_argument("--batch", default="")
    p_sc.add_argument("--file", default="")
    p_sc.add_argument("--out", default="")
    p_sc.add_argument("--partial-ratio", type=float, default=None, help="多选题漏选给分比例（默认 0.5）")
    p_sc.add_argument("--vlm", action="store_true", help="parse-pdf 时直接调用视觉大模型解析为 JSON")

    p_mem = sub.add_parser("memory", parents=[common], help="改卷内存暂存 Crop 预检与并发预算评估")
    p_mem.add_argument("action", nargs="?", default="preflight", choices=["preflight"])
    p_mem.add_argument("--batch", default="")

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
        "config": cmd_config,
        "weights": cmd_weights,
        "agents": cmd_agents,
        "capture": cmd_capture,
        "ui": cmd_ui,
        "review": cmd_review,
        "schema": cmd_schema,
        "memory": cmd_memory,
    }[args.cmd](args, t0)


if __name__ == "__main__":
    raise SystemExit(main())
