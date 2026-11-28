# -*- coding: utf-8 -*-
"""统一工作台本地控制中心（主机 + 局域网协作者总览 + SSE 实时高亮）。

用法：
    python apps/workbench/server.py [--port 8888] [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import core  # noqa: E402
import agent_providers  # noqa: E402
import capture as capture_mod  # noqa: E402
import train_bridge  # noqa: E402


def _load_pricing_mod(root: Path = ROOT):
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    from engine import api_pricing  # noqa: E402
    return api_pricing


def _load_run_log_mod(root: Path = ROOT):
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    from engine import run_log  # noqa: E402
    return run_log


class EventBus:
    """进程内 SSE 广播总线：CLI 或后台任务发事件，所有网页实时高亮。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._recent: list[dict] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
            recent = list(self._recent[-8:])
        for ev in recent:
            try:
                q.put_nowait(ev)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        event = {**event, "ts": event.get("ts") or core.now_str()}
        with self._lock:
            self._recent.append(event)
            if len(self._recent) > 40:
                self._recent = self._recent[-40:]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass


class WorkbenchState:
    """管理子服务进程、后台流水线任务、在线人员与最近自检结果。"""

    def __init__(self, root: Path = ROOT, batches_root: Path = core.DEFAULT_BATCHES_ROOT, port: int = 8888):
        self.root = Path(root).resolve()
        self.batches_root = Path(batches_root).resolve()
        self.batches_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.bus = EventBus()
        self._lock = threading.RLock()
        self.procs: dict[str, dict] = {}  # task_id -> {proc, port, args, started_at}
        self.presence: dict[str, dict] = {}
        self.pipeline_job: dict = {
            "running": False,
            "kind": "recognize",
            "batch_id": "",
            "source": "gui",
            "logs": [],
            "status": "idle",
            "returncode": None,
            "exit_info": None,
            "progress": None,
            "error_report": None,
            "result_summary": None,
        }
        self.benchmark_job: dict = {
            "running": False,
            "logs": [],
            "status": "idle",
            "result": None,
            "error": "",
        }
        self.repair_job: dict = {"running": False, "logs": [], "status": "idle", "result": None}
        self.ai_job: dict = {
            "running": False,
            "provider": "",
            "mode": "plan",
            "status": "idle",
            "session_id": "",
            "response": "",
            "usage": {},
            "logs": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
        }
        self.train_job: dict = {
            "running": False,
            "task": "",
            "run": "",
            "epochs": 0,
            "completed_epochs": 0,
            "metrics": {},
            "logs": [],
            "status": "idle",
            "returncode": None,
        }
        self._pipeline_proc: subprocess.Popen | None = None
        self._pipeline_lease_token: str = ""
        self._train_proc: subprocess.Popen | None = None
        self._ai_proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self.last_check: dict | None = None
        self.ui_states: dict[str, dict] = {}
        self.ai_manager = agent_providers.AiSessionManager(
            self.root,
            self.batches_root,
            publish_cb=self.bus.publish,
            config_loader=lambda: core.load_config(self.batches_root),
            env_builder=self._utf8_env,
        )
        # 若本地尚无官方定价永久缓存，在后台静默抓取一次并永久保存（已有缓存时零网络开销）
        self._ensure_pricing_once_async()

    def _ensure_pricing_once_async(self) -> None:
        try:
            pricing_mod = _load_pricing_mod(self.root)
            if pricing_mod.load_pricing_cache(self.root):
                return
        except Exception:
            return

        def _bg():
            try:
                _load_pricing_mod(self.root).ensure_pricing_cache(self.root, force=False)
            except Exception:
                pass

        threading.Thread(target=_bg, daemon=True, name="pricing-init").start()

    def _estimate_api_cost(self, model: str, stats: dict, provider: str = "") -> dict | None:
        if not model:
            return None
        try:
            pricing_mod = _load_pricing_mod(self.root)
            return pricing_mod.estimate_cost(
                model_id=model,
                prompt_tokens=int(stats.get("prompt_tokens") or 0),
                completion_tokens=int(stats.get("completion_tokens") or 0),
                cached_tokens=int(stats.get("cached_tokens") or 0),
                provider=provider,
                pages=int(stats.get("pages") or stats.get("pages_done") or 0),
                reused=int(stats.get("reused") or 0),
                api_done=int(stats.get("api_done") or stats.get("api_success") or 0),
                root=self.root,
            )
        except Exception:
            return None

    def _utf8_env(self) -> dict:
        return dict(
            os.environ,
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
        )

    def pipeline_job_snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.pipeline_job)
            snap["logs"] = list(self.pipeline_job.get("logs") or [])
            if isinstance(snap.get("progress"), dict):
                snap["progress"] = dict(snap["progress"])
            if isinstance(snap.get("error_report"), dict):
                snap["error_report"] = dict(snap["error_report"])
            if isinstance(snap.get("exit_info"), dict):
                snap["exit_info"] = dict(snap["exit_info"])
            if isinstance(snap.get("result_summary"), dict):
                snap["result_summary"] = dict(snap["result_summary"])
        return snap

    def benchmark_job_snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.benchmark_job)
            snap["logs"] = list(self.benchmark_job.get("logs") or [])
        return snap

    def train_job_snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.train_job)
            snap["logs"] = list(self.train_job.get("logs") or [])
            snap["metrics"] = dict(self.train_job.get("metrics") or {})
        return snap

    def ai_job_snapshot(self) -> dict:
        return self.ai_manager.legacy_snapshot()

    def update_ui_state(self, payload: dict) -> dict:
        cid = str(payload.get("client_id") or "default").strip()[:64] or "default"
        snap = {
            "client_id": cid,
            "view": str(payload.get("view") or "overview")[:32],
            "batch_id": str(payload.get("batch_id") or "")[:96],
            "dialog": str(payload.get("dialog") or "")[:48],
            "scroll_y": int(payload.get("scroll_y") or 0),
            "arch_collapsed": bool(payload.get("arch_collapsed", False)),
            "recent_errors": [str(x)[:200] for x in (payload.get("recent_errors") or [])[:8]] if isinstance(payload.get("recent_errors"), list) else [],
            "_ts": time.time(),
        }
        with self._lock:
            self.ui_states[cid] = snap
        return snap

    def latest_ui_state(self, client_id: str = "") -> dict:
        with self._lock:
            now = time.time()
            stale = [k for k, v in self.ui_states.items() if now - float(v.get("_ts") or 0) > 600]
            for k in stale:
                self.ui_states.pop(k, None)
            if client_id and client_id in self.ui_states:
                d = dict(self.ui_states[client_id])
                d.pop("_ts", None)
                return d
            if not self.ui_states:
                return {}
            latest = dict(max(self.ui_states.values(), key=lambda x: float(x.get("_ts") or 0)))
            latest.pop("_ts", None)
            return latest

    def run_check(self, deep: bool = False) -> dict:
        self.bus.publish({"source": "gui", "step": "check", "status": "running", "message": "正在执行深度依赖导入与模型权重检查..." if deep else "正在检查运行环境与模型权重..."})
        rep = core.check_environment(self.root, self.batches_root, deep=deep)
        self.last_check = rep
        msg = "环境自检全部通过" if rep["ok"] else f"发现 {rep['error_count']} 项需要处理的问题"
        self.bus.publish({"source": "gui", "step": "check", "status": "done" if rep["ok"] else "error", "message": msg})
        return rep

    def _port_open(self, port: int, timeout: float = 0.25) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    def service_snapshot(self) -> dict:
        out = {}
        ports = {"review": 8904, "annotate": 8900, "choice": 8905, "monitor": 8901}
        with self._lock:
            for tid, port in ports.items():
                info = self.procs.get(tid)
                alive = bool(info and info["proc"].poll() is None)
                if not alive:
                    # 也许外部手动启动了同一端口
                    alive = self._port_open(port, timeout=0.12)
                out[tid] = {"running": alive, "port": port, "extra": (info or {}).get("extra") or {}}
        return out

    def start_service(self, task_id: str, extra: dict | None = None) -> dict:
        extra = extra or {}
        ports = {"review": 8904, "annotate": 8900, "choice": 8905, "monitor": 8901}
        if task_id not in ports:
            raise ValueError(f"未知审核任务：{task_id}")
        port = ports[task_id]

        with self._lock:
            info = self.procs.get(task_id)
            need_restart = False
            active_for_review = None
            if task_id == "review":
                active_for_review = core.get_active_batch(self.batches_root)
                if not active_for_review:
                    raise ValueError("还没有导入批次，请先在主机导入试卷并跑自动识别")
                cur_rec_dir = str(Path(active_for_review["recognize_dir"]).resolve())
                if info:
                    prev_rec_dir = str((info.get("extra") or {}).get("recognize_dir") or "")
                    if prev_rec_dir and prev_rec_dir != cur_rec_dir:
                        need_restart = True
            if task_id == "annotate" and extra.get("m") and info:
                prev_m = (info.get("extra") or {}).get("m")
                if prev_m and prev_m != extra.get("m"):
                    need_restart = True
            if task_id == "monitor" and extra.get("run") and info:
                prev_run = (info.get("extra") or {}).get("run")
                if prev_run and prev_run != extra.get("run"):
                    need_restart = True

            if need_restart and info:
                if info["proc"].poll() is None:
                    info["proc"].terminate()
                    try:
                        info["proc"].wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        info["proc"].kill()
                self.procs.pop(task_id, None)
                time.sleep(0.25)
                info = None

            if info and info["proc"].poll() is None and self._port_open(port, 0.2):
                return {"ok": True, "task_id": task_id, "port": port, "already_running": True}
            if not need_restart and self._port_open(port, 0.2):
                return {"ok": True, "task_id": task_id, "port": port, "already_running": True}

            if task_id == "review":
                active = active_for_review or core.get_active_batch(self.batches_root)
                if not active:
                    raise ValueError("还没有导入批次，请先在主机导入试卷并跑自动识别")
                rec_dir = Path(active["recognize_dir"]).resolve()
                if not (rec_dir / "results.json").is_file():
                    raise ValueError("当前批次尚未完成自动识别，请先点「开始自动识别」")
                cmd = [
                    sys.executable,
                    str(self.root / "apps" / "review" / "server.py"),
                    "--dir", str(rec_dir),
                    "--port", str(port),
                    "--hub-port", str(self.port),
                    "--lan",
                ]
                extra = {**extra, "batch_id": active.get("batch_id", ""), "recognize_dir": str(rec_dir)}
            elif task_id == "annotate":
                m = extra.get("m") or "m1"
                if m not in ("m1", "m2", "m3"):
                    m = "m1"
                data_dir = self.root / "data" / m
                (data_dir / "images").mkdir(parents=True, exist_ok=True)
                cmd = [sys.executable, str(self.root / "apps" / "annotator" / "server.py"), "--data", str(data_dir), "--port", str(port), "--lan", "--no-browser"]
                extra = {**extra, "m": m}
            elif task_id == "choice":
                cmd = [sys.executable, str(self.root / "apps" / "choice_review" / "server.py"), "--port", str(port), "--lan"]
            elif task_id == "monitor":
                run_name = str(extra.get("run") or "").strip()
                cmd = [sys.executable, str(self.root / "apps" / "train_monitor" / "server.py"), "--port", str(port), "--no-browser"]
                if run_name:
                    train_root = (self.root / "train").resolve()
                    run_cand = (train_root / run_name).resolve()
                    if run_cand.parent != train_root:
                        raise ValueError("非法的训练记录路径")
                    cmd += ["--run", str(run_cand)]
                extra = {**extra, "run": run_name}
            else:
                raise ValueError(f"不支持的子服务：{task_id}")

            self.bus.publish({"source": "gui", "step": f"service_{task_id}", "status": "running", "message": f"正在启动 {task_id} 服务 (端口 {port})..."})
            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            log_dir = self.batches_root / "_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            err_log_path = log_dir / f"service_{task_id}.log"
            # 追加落盘 + 超过 10 MB 简单轮转，绝不在重启时截断历史日志，且同时保留 stdout 与 stderr
            try:
                if err_log_path.is_file() and err_log_path.stat().st_size > 10 * 1024 * 1024:
                    rot = log_dir / f"service_{task_id}.log.1"
                    if rot.exists():
                        rot.unlink(missing_ok=True)
                    err_log_path.replace(rot)
            except OSError:
                pass
            err_fh = err_log_path.open("a", encoding="utf-8", errors="replace")
            try:
                err_fh.write(f"\n=== [{core.now_str()}] START {task_id} port={port} ===\n")
                err_fh.flush()
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.root),
                    stdout=err_fh,
                    stderr=subprocess.STDOUT,
                    env=self._utf8_env(),
                    creationflags=creation,
                )
            finally:
                err_fh.close()
            self.procs[task_id] = {"proc": proc, "port": port, "cmd": cmd, "extra": extra, "started_at": core.now_str(), "err_log": str(err_log_path)}

        # 等待端口监听就绪（最多 15 秒）
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self._port_open(port, 0.25):
                self.bus.publish({"source": "gui", "step": f"service_{task_id}", "status": "done", "message": f"{task_id} 审核服务已就绪 (端口 {port})"})
                return {"ok": True, "task_id": task_id, "port": port, "already_running": False}
            if proc.poll() is not None:
                with self._lock:
                    if self.procs.get(task_id, {}).get("proc") is proc:
                        self.procs.pop(task_id, None)
                detail = ""
                try:
                    lines = [ln.strip() for ln in err_log_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
                    if lines:
                        detail = f"（{lines[-1][:180]}）"
                except OSError:
                    pass
                raise RuntimeError(f"审核服务启动后立即退出（退出码 {proc.returncode}）{detail}")
            time.sleep(0.2)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self._lock:
            if self.procs.get(task_id, {}).get("proc") is proc:
                self.procs.pop(task_id, None)
        raise RuntimeError(f"等待审核服务端口 {port} 就绪超时")

    def stop_service(self, task_id: str) -> dict:
        with self._lock:
            info = self.procs.pop(task_id, None)
        if info and info["proc"].poll() is None:
            info["proc"].terminate()
            try:
                info["proc"].wait(timeout=3)
            except subprocess.TimeoutExpired:
                info["proc"].kill()
        if info and info.get("err_log"):
            try:
                with Path(info["err_log"]).open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"=== [{core.now_str()}] STOP {task_id} ===\n")
            except OSError:
                pass
        self.bus.publish({"source": "gui", "step": f"service_{task_id}", "status": "stopped", "message": f"已停止 {task_id} 服务"})
        return {"ok": True, "task_id": task_id}

    def _read_run_stats(self, rec_dir: Path) -> dict | None:
        """读取识别目录的 results.json 摘要（用于结果级健康判定与架构图各节点统计）。"""
        data = core.read_json(rec_dir / "results.json")
        if not isinstance(data, dict):
            return None
        pages = data.get("pages") or []
        grids = data.get("grids") or []
        stats = dict(data.get("stats") or {})
        if "pages" not in stats:
            stats["pages"] = len(pages)
        if "pages_total" not in stats:
            stats["pages_total"] = len(pages)
        if "page_failures" not in stats:
            stats["page_failures"] = sum(bool(p.get("error")) for p in pages)
        if "grids" not in stats:
            stats["grids"] = len(grids)

        # 若历史批次或旧 results.json 缺少分阶段节点统计，则按页面 boxes/items 精确反推
        if "m1_boxes" not in stats or "choice_items" not in stats or "m2_ocr_done" not in stats:
            orient_done = sum(1 for p in pages if not p.get("error"))
            m1_boxes = m1_choice = m1_fill = m1_identity = 0
            m2_boxes = m3_boxes = m2_ocr = m2_cnn = m3_ocr = 0
            choice_items = fill_items = identity_items = 0
            for p in pages:
                if p.get("error"):
                    continue
                sc = p.get("stage_counts") if isinstance(p.get("stage_counts"), dict) else None
                boxes = p.get("boxes") or []
                items = p.get("items") or []
                if sc:
                    m1_boxes += int(sc.get("m1") or 0)
                    m1_choice += int(sc.get("m1_choice") or sum(1 for b in boxes if b.get("cls") == 1))
                    m1_fill += int(sc.get("m1_fill") or sum(1 for b in boxes if b.get("cls") == 4))
                    m1_identity += int(sc.get("m1_identity") or sum(1 for b in boxes if b.get("cls") == 0))
                    m2_boxes += int(sc.get("m2") or 0)
                    m3_boxes += int(sc.get("m3") or 0)
                else:
                    for b in boxes:
                        cls = b.get("cls")
                        st = b.get("stage")
                        if cls in (0, 1, 4) or st == "m1":
                            m1_boxes += 1
                            if cls == 1:
                                m1_choice += 1
                            elif cls == 4:
                                m1_fill += 1
                            elif cls == 0:
                                m1_identity += 1
                        elif cls in (2, 3) or st == "m2":
                            m2_boxes += 1
                        elif cls in (5, 6) or st == "m3":
                            m3_boxes += 1
                p_choices = [it for it in items if it.get("kind") == "choice"]
                p_fills = [it for it in items if it.get("kind") == "fill"]
                p_ids = [it for it in items if it.get("kind") == "identity"]
                choice_items += len(p_choices)
                fill_items += len(p_fills)
                identity_items += len(p_ids)
                if sc and "m2_ocr" in sc:
                    m2_ocr += int(sc.get("m2_ocr") or 0)
                    m2_cnn += int(sc.get("m2_cnn") or 0)
                    m3_ocr += int(sc.get("m3_ocr") or 0)
                else:
                    m2_ocr += len(p_choices)
                    m2_cnn += len(p_choices)
                    m3_ocr += sum(1 for it in p_fills if it.get("number") is not None or it.get("number_detail"))

            stats.setdefault("orient_done", orient_done)
            stats.setdefault("m1_pages_done", orient_done)
            stats.setdefault("m1_boxes", m1_boxes)
            stats.setdefault("m1_choice_zones", m1_choice)
            stats.setdefault("m1_fill_zones", m1_fill)
            stats.setdefault("m1_identity_zones", m1_identity)
            stats.setdefault("m2_zones_yolo_done", m1_choice)
            stats.setdefault("m2_zones_done", m1_choice)
            stats.setdefault("m2_boxes", m2_boxes)
            stats.setdefault("m2_ocr_done", m2_ocr)
            stats.setdefault("m2_cnn_done", m2_cnn)
            stats.setdefault("m3_zones_yolo_done", m1_fill)
            stats.setdefault("m3_zones_done", m1_fill)
            stats.setdefault("m3_boxes", m3_boxes)
            stats.setdefault("m3_ocr_done", m3_ocr)
            stats.setdefault("ocr_numbers", m2_ocr + m3_ocr)
            stats.setdefault("choice_items", choice_items)
            stats.setdefault("fill_items", fill_items)
            stats.setdefault("identity_items", identity_items)
            stats.setdefault("tiles_queued", fill_items + identity_items)

        failed_items = sum(
            1
            for p in pages
            for it in (p.get("items") or [])
            if it.get("kind") in ("fill", "identity")
            and it.get("status") in ("调用失败", "结构异常", "等待视觉转录", "等待豆包")
        )
        model_id = str(data.get("model") or "")
        provider_id = str(data.get("provider") or "")
        return {
            "status": str(data.get("status") or ""),
            "model": model_id,
            "returned_model": data.get("returned_model"),
            "provider": provider_id,
            "provider_label": str(data.get("provider_label") or ""),
            "settings": data.get("settings") or {},
            "stats": stats,
            "api_cost": self._estimate_api_cost(model_id, stats, provider=provider_id),
            "failed_items": failed_items,
            "has_error_log": (rec_dir / "logs" / "errors.log").is_file(),
        }

    def start_pipeline(self, batch_id: str | None = None, source: str = "gui") -> dict:
        diag_mod = core._load_diag_module(self.root)
        src = source if source in ("gui", "cli", "ai") else "gui"
        with self._lock:
            if self.pipeline_job["running"]:
                return {"ok": False, "error": "已有自动识别任务正在运行，请等待完成"}
            if self.train_job["running"]:
                return {"ok": False, "error": "当前正在执行模型微调训练，请等训练结束或先停止后再启动识别"}
            if self.benchmark_job["running"]:
                return {"ok": False, "error": "当前正在执行本机测速，请等几秒测速结束后再启动识别"}
            active = core.set_active_batch(batch_id, self.batches_root) if batch_id else core.get_active_batch(self.batches_root)
            if not active:
                return {"ok": False, "error": "请先导入一批试卷照片"}
            lease_res = core.acquire_pipeline_lease(
                "recognize",
                active["batch_id"],
                owner=f"hub:{src}",
                batches_root=self.batches_root,
            )
            if not lease_res.get("ok"):
                return {"ok": False, "error": lease_res.get("error") or "已有任务运行中"}
            lease_token = str(lease_res["lease"]["token"])
            self._pipeline_lease_token = lease_token
            # 原子占位，防止双击在 3 秒深度环境检查窗口内拉起双份识别进程
            self.pipeline_job.update({
                "running": True,
                "kind": "recognize",
                "batch_id": active["batch_id"],
                "source": src,
                "status": "checking",
                "error_report": None,
            })

        def _abort_lease():
            core.release_pipeline_lease(lease_token, self.batches_root)
            with self._lock:
                if self._pipeline_lease_token == lease_token:
                    self._pipeline_lease_token = ""

        # 启动前强校验：依赖（含真实 import）、模型权重、有效 API Key
        try:
            rep = core.check_environment(self.root, self.batches_root, deep=True)
        except Exception as exc:
            diag = diag_mod.diagnose_exception(exc, default_module="启动前环境校验")
            _abort_lease()
            with self._lock:
                self.pipeline_job.update({"running": False, "status": "error", "error_report": diag})
            return {"ok": False, "error": diag["reason"], "diagnostic": diag}
        self.last_check = rep
        if not rep.get("ok"):
            first_err = next((x for x in rep.get("items", []) if x.get("severity") == "error"), None)
            err_mod = (first_err or {}).get("module") or "运行环境"
            err_reason = (first_err or {}).get("detail") or "存在缺失的依赖库或模型权重"
            err_fix = (first_err or {}).get("fix") or "请回到「第 1 步：运行环境」一键修复。"
            err_report = {
                "module": err_mod,
                "title": f"环境未就绪：{err_mod}存在缺失",
                "reason": err_reason,
                "fix": err_fix,
                "exit_code": diag_mod.EXIT_WEIGHTS_MISSING if (first_err or {}).get("category") == "weights" else diag_mod.EXIT_ENV_MISSING,
            }
            _abort_lease()
            with self._lock:
                self.pipeline_job.update({
                    "running": False,
                    "batch_id": active["batch_id"],
                    "status": "error",
                    "returncode": err_report["exit_code"],
                    "exit_info": diag_mod.describe_exit_code(err_report["exit_code"]),
                    "error_report": err_report,
                })
            return {
                "ok": False,
                "error": f"【{err_mod}】{err_reason}。修复建议：{err_fix}",
                "diagnostic": err_report,
            }
        if not rep.get("has_valid_api_key"):
            reason = (
                ".env 中的 API Key 仍是模板占位文字（your_...），未配置真实密钥。"
                if rep.get("placeholder_api_key")
                else "尚未配置视觉大模型 API Key，无法转录填空题和姓名学号。"
            )
            err_report = {
                "module": "视觉大模型 API 配置",
                "title": "请先配置有效的视觉大模型 API Key",
                "reason": reason,
                "fix": "请点击右上角「⚙ 大模型 API」，选择服务商、填入真实密钥并点击「测试连通性」通过后再启动识别。",
                "exit_code": diag_mod.EXIT_API_CONFIG_ERROR,
            }
            _abort_lease()
            with self._lock:
                self.pipeline_job.update({
                    "running": False,
                    "batch_id": active["batch_id"],
                    "status": "error",
                    "returncode": diag_mod.EXIT_API_CONFIG_ERROR,
                    "exit_info": diag_mod.describe_exit_code(diag_mod.EXIT_API_CONFIG_ERROR),
                    "error_report": err_report,
                })
            return {
                "ok": False,
                "error": f"【视觉大模型 API】{reason} 请先点击右上角「⚙ 大模型 API」配置。",
                "diagnostic": err_report,
            }

        infer_cfg = core.get_infer_settings(self.batches_root)
        with self._lock:
            self.pipeline_job = {
                "running": True,
                "kind": "recognize",
                "batch_id": active["batch_id"],
                "logs": [
                    f"[{core.now_str()}] 启动自动识别流水线：{active['batch_id']}（共 {active['total_images']} 张）",
                    f"[{core.now_str()}] 设备策略：{rep.get('effective_device_label')} | YOLO Batch：{infer_cfg['yolo_batch'] or '自动'} | 线程：{infer_cfg['cpu_workers'] or '自动'} | API 并发：{infer_cfg['api_concurrency']}",
                ],
                "status": "running",
                "returncode": None,
                "exit_info": None,
                "progress": {
                    "stage": "准备模型与环境",
                    "message": f"正在初始化检测模型（{rep.get('effective_device_label')}）...",
                    "pages_done": 0,
                    "pages_total": int(active.get("total_images") or 0),
                    "page_failures": 0,
                    "orient_done": 0,
                    "m1_pages_done": 0,
                    "m1_boxes": 0,
                    "m1_choice_zones": 0,
                    "m1_fill_zones": 0,
                    "m1_identity_zones": 0,
                    "m2_zones_yolo_done": 0,
                    "m2_zones_done": 0,
                    "m2_boxes": 0,
                    "m2_ocr_done": 0,
                    "m2_cnn_done": 0,
                    "m3_zones_yolo_done": 0,
                    "m3_zones_done": 0,
                    "m3_boxes": 0,
                    "m3_ocr_done": 0,
                    "ocr_numbers": 0,
                    "choice_items": 0,
                    "fill_items": 0,
                    "identity_items": 0,
                    "tiles_queued": 0,
                    "grids_total": 0,
                    "api_done": 0,
                    "api_success": 0,
                    "api_failures": 0,
                    "api_inflight": 0,
                    "items_with_transcription": 0,
                    "device": rep.get("effective_device") or "auto",
                    "device_label": rep.get("effective_device_label") or "",
                    "elapsed_seconds": 0.0,
                    "pages_per_min": 0.0,
                    "eta_seconds": None,
                },
                "error_report": None,
                "result_summary": None,
            }

        def worker():
            self.bus.publish({"source": "gui", "step": "recognize", "status": "running", "message": f"正在识别批次 {active['batch_id']}（共 {active['total_images']} 张）..."})
            cmd = [
                sys.executable,
                "-B",
                str(self.root / "engine" / "run.py"),
                "--recognize",
                "--input", active["images_dir"],
                "--out", active["recognize_dir"],
                "--device", str(infer_cfg["infer_device"]),
                "--yolo-batch", str(infer_cfg["yolo_batch"]),
                "--workers", str(infer_cfg["cpu_workers"]),
                "--concurrency", str(infer_cfg["api_concurrency"]),
            ]
            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            last_sse_ts = 0.0
            last_error_diag = None
            raw_tail: list[str] = []
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self._utf8_env(),
                    creationflags=creation,
                )
                with self._lock:
                    self._pipeline_proc = proc
                assert proc.stdout is not None
                last_stage_sent = ""
                for line in proc.stdout:
                    text = line.rstrip()
                    if not text:
                        continue
                    # 过滤 ultralytics 已知无害提示，避免挤爆技术日志
                    if "deprecated" in text.lower() and "quantize" in text.lower():
                        continue
                    parsed = None
                    if text.startswith("{") and text.endswith("}"):
                        try:
                            parsed = json.loads(text)
                        except Exception:
                            parsed = None

                    now_m = time.monotonic()
                    if isinstance(parsed, dict) and parsed.get("event") == "progress":
                        stage = str(parsed.get("stage") or "识别中")
                        msg = str(parsed.get("message") or "")
                        if parsed.get("model"):
                            parsed["api_cost"] = self._estimate_api_cost(
                                str(parsed.get("model") or ""),
                                parsed,
                                provider=str(parsed.get("provider") or ""),
                            )
                        if parsed.get("recent_error"):
                            last_error_diag = parsed["recent_error"]
                        with self._lock:
                            self.pipeline_job["progress"] = parsed
                            if last_error_diag:
                                self.pipeline_job["error_report"] = last_error_diag
                            if msg:
                                self.pipeline_job["logs"].append(f"[{stage}] {msg}")
                                if len(self.pipeline_job["logs"]) > 200:
                                    self.pipeline_job["logs"] = self.pipeline_job["logs"][-200:]
                        if stage != last_stage_sent or (now_m - last_sse_ts) >= 0.35:
                            last_sse_ts = now_m
                            last_stage_sent = stage
                            p_done = parsed.get("pages_done", 0)
                            p_tot = parsed.get("pages_total", 0)
                            a_ok = parsed.get("api_success", 0)
                            a_tot = parsed.get("grids_total", 0)
                            self.bus.publish({
                                "source": "gui",
                                "step": "recognize",
                                "status": "running",
                                "batch_id": active["batch_id"],
                                "progress": parsed,
                                "message": f"{stage} · 已检测 {p_done}/{p_tot} 页 · 拼图转录 {a_ok}/{a_tot}",
                            })
                    elif isinstance(parsed, dict) and parsed.get("event") == "error":
                        diag = parsed.get("diagnostic") or {}
                        last_error_diag = diag
                        with self._lock:
                            self.pipeline_job["error_report"] = diag
                            self.pipeline_job["logs"].append(
                                f"[错误·{diag.get('module', '系统')}] {diag.get('title', '')}：{diag.get('reason', '')}（建议：{diag.get('fix', '')}）"
                            )
                    elif isinstance(parsed, dict) and parsed.get("event") == "summary":
                        if parsed.get("recent_error"):
                            last_error_diag = parsed["recent_error"]
                        with self._lock:
                            self.pipeline_job["logs"].append(f"[汇总] 状态：{parsed.get('status', '')}")
                    else:
                        raw_tail.append(text)
                        if len(raw_tail) > 40:
                            raw_tail = raw_tail[-40:]
                        with self._lock:
                            self.pipeline_job["logs"].append(text)
                            if len(self.pipeline_job["logs"]) > 200:
                                self.pipeline_job["logs"] = self.pipeline_job["logs"][-200:]

                rc = proc.wait()
                with self._lock:
                    was_stopped = self.pipeline_job.get("status") == "stopped"
                    self._pipeline_proc = None
                exit_info = diag_mod.describe_exit_code(rc)
                run_summary = self._read_run_stats(Path(active["recognize_dir"]))
                st = (run_summary or {}).get("stats") or {}
                pages_n = int(st.get("pages") or 0)
                page_fail = int(st.get("page_failures") or 0)
                api_fail = int(st.get("api_failures") or 0)
                failed_items = int((run_summary or {}).get("failed_items") or 0)
                all_failed = pages_n > 0 and page_fail >= pages_n

                if was_stopped:
                    final_status = "warn" if pages_n > 0 else "idle"
                    msg = f"自动识别已手动停止（已保全 {pages_n} 页进度，下次点击可断点续跑）。"
                elif rc != 0 or all_failed:
                    final_status = "error"
                    if last_error_diag is None:
                        tail_txt = "\n".join(raw_tail[-8:])
                        if tail_txt:
                            last_error_diag = diag_mod.diagnose_exception(tail_txt, default_module="自动识别流水线")
                        else:
                            last_error_diag = {
                                "module": "自动识别流水线",
                                "title": exit_info["title"],
                                "reason": exit_info["reason"],
                                "fix": exit_info["fix"],
                                "exit_code": rc,
                            }
                    msg = f"自动识别失败：{last_error_diag.get('title') or exit_info['title']}（退出码 {rc}：{exit_info['title']}）"
                elif page_fail > 0 or api_fail > 0 or failed_items > 0:
                    final_status = "warn"
                    parts = []
                    if page_fail > 0:
                        parts.append(f"{page_fail}/{pages_n} 页本地检测失败")
                    if failed_items > 0:
                        parts.append(f"{failed_items} 个手写题块尚未完成转录")
                    elif api_fail > 0:
                        parts.append(f"{api_fail} 个拼图调用失败")
                    msg = "自动识别已结束，但存在待补跑项：" + "，".join(parts) + "。可点击「补跑失败图块」仅重试失败项。"
                else:
                    final_status = "done"
                    msg = f"自动识别全部完成（共 {pages_n} 页，转录 {st.get('items_with_transcription', 0)} 处）！现在可以进入「人工复审」核对结果。"

                with self._lock:
                    self.pipeline_job["running"] = False
                    self.pipeline_job["returncode"] = rc
                    self.pipeline_job["exit_info"] = exit_info
                    self.pipeline_job["status"] = final_status
                    self.pipeline_job["result_summary"] = run_summary
                    if last_error_diag and not was_stopped:
                        self.pipeline_job["error_report"] = last_error_diag
                    self.pipeline_job["logs"].append(f"[结束] 退出码 {rc}（{exit_info['title']}）· {msg}")
                self.bus.publish({"source": src, "step": "recognize", "status": final_status, "message": msg})
            except Exception as exc:
                diag = diag_mod.diagnose_exception(exc, default_module="工作台任务调度")
                with self._lock:
                    self._pipeline_proc = None
                    self.pipeline_job["running"] = False
                    self.pipeline_job["status"] = "error"
                    self.pipeline_job["returncode"] = diag.get("exit_code", 1)
                    self.pipeline_job["exit_info"] = diag_mod.describe_exit_code(diag.get("exit_code", 1))
                    self.pipeline_job["error_report"] = diag
                    self.pipeline_job["logs"].append(f"[异常·{diag['module']}] {diag['title']}：{diag['reason']}")
                self.bus.publish({"source": src, "step": "recognize", "status": "error", "message": f"启动识别失败：{diag['title']}"})
            finally:
                _abort_lease()

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "batch_id": active["batch_id"], "source": src}

    def stop_pipeline(self) -> dict:
        """优雅中止正在运行的自动识别进程（保留已落盘进度，支持断点续跑）。"""
        with self._lock:
            proc = self._pipeline_proc
            if not self.pipeline_job.get("running"):
                return {"ok": True, "message": "当前没有正在运行的识别任务"}
            self.pipeline_job["status"] = "stopped"
            self.pipeline_job["logs"].append(f"[{core.now_str()}] 收到停止指令，正在中止识别进程...")
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=5)
                else:
                    proc.terminate()
                    proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.bus.publish({"source": "gui", "step": "recognize", "status": "stopped", "message": "已停止自动识别（已处理进度已保存）"})
        return {"ok": True, "message": "已停止自动识别"}

    def start_retry_failed(self, batch_id: str | None = None, source: str = "gui") -> dict:
        """在后台仅补跑当前批次失败或结构异常的图块（不重复扣费，JSON/结构异常自动降级单题重试）。"""
        diag_mod = core._load_diag_module(self.root)
        src = source if source in ("gui", "cli", "ai") else "gui"
        with self._lock:
            if self.pipeline_job["running"]:
                return {"ok": False, "error": "当前已有识别或补跑任务正在运行，请稍候"}
            if self.train_job["running"]:
                return {"ok": False, "error": "当前正在执行模型微调训练，请等结束后再补跑"}
            if self.benchmark_job["running"]:
                return {"ok": False, "error": "当前正在执行本机测速，请等结束后再补跑"}
            active = core.set_active_batch(batch_id, self.batches_root) if batch_id else core.get_active_batch(self.batches_root)
            if not active:
                return {"ok": False, "error": "还没有导入批次"}
            rec_dir = Path(active["recognize_dir"])
            if not (rec_dir / "results.json").is_file():
                return {"ok": False, "error": "当前批次尚无识别结果，请先运行自动识别"}
            cur_summary = self._read_run_stats(rec_dir) or {}
            page_fail_n = int((cur_summary.get("stats") or {}).get("page_failures") or 0)
            if page_fail_n > 0:
                # 存在本地检测失败页：直接断点续跑流水线（跳过已成功缓存页与已转录题块，仅补跑失败页）
                return self.start_pipeline(active["batch_id"], source=src)
            lease_res = core.acquire_pipeline_lease(
                "retry",
                active["batch_id"],
                owner=f"hub:{src}",
                batches_root=self.batches_root,
            )
            if not lease_res.get("ok"):
                return {"ok": False, "error": lease_res.get("error") or "已有任务运行中"}
            lease_token = str(lease_res["lease"]["token"])
            self._pipeline_lease_token = lease_token
            infer_cfg = core.get_infer_settings(self.batches_root)
            self.pipeline_job.update({
                "running": True,
                "kind": "retry",
                "batch_id": active["batch_id"],
                "source": src,
                "status": "running",
                "returncode": None,
                "exit_info": None,
                "error_report": None,
                "logs": [f"[{core.now_str()}] 启动失败图块补跑（仅重跑失败项，不重复扣费）：{active['batch_id']}"],
            })

        def worker():
            import importlib.util
            self.bus.publish({"source": src, "step": "recognize", "status": "running", "message": "正在补跑失败图块（JSON/结构类自动单题重试，不重复扣费）..."})
            try:
                spec = importlib.util.spec_from_file_location("_retry_failed", str(self.root / "scripts" / "retry_failed_tiles.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                def cb(msg: str):
                    with self._lock:
                        self.pipeline_job["logs"].append(f"[补跑] {msg}")
                        if len(self.pipeline_job["logs"]) > 200:
                            self.pipeline_job["logs"] = self.pipeline_job["logs"][-200:]
                    self.bus.publish({"source": src, "step": "recognize", "status": "running", "message": msg})

                res = mod.retry_failed_grids(rec_dir, concurrency=infer_cfg["api_concurrency"], progress_cb=cb, actor=src)
                run_summary = self._read_run_stats(rec_dir)
                ok = bool(res.get("ok"))
                rem = int(res.get("remaining_failures") or 0)
                with self._lock:
                    self.pipeline_job["running"] = False
                    self.pipeline_job["returncode"] = 0 if ok else int(res.get("exit_code") or 1)
                    self.pipeline_job["exit_info"] = diag_mod.describe_exit_code(self.pipeline_job["returncode"])
                    self.pipeline_job["status"] = ("done" if rem == 0 else "warn") if ok else "error"
                    self.pipeline_job["result_summary"] = run_summary
                    if not ok:
                        self.pipeline_job["error_report"] = diag_mod.diagnose_exception(res.get("error") or "补跑失败", default_module="补跑失败图块")
                    self.pipeline_job["logs"].append(f"[补跑结束] {res.get('message') or res.get('error') or ''}")
                self.bus.publish({"source": src, "step": "recognize", "status": "done" if ok else "error", "message": res.get("message") or res.get("error") or "补跑结束"})
            except Exception as exc:
                diag = diag_mod.diagnose_exception(exc, default_module="补跑失败图块")
                with self._lock:
                    self.pipeline_job["running"] = False
                    self.pipeline_job["status"] = "error"
                    self.pipeline_job["returncode"] = diag.get("exit_code", 1)
                    self.pipeline_job["exit_info"] = diag_mod.describe_exit_code(diag.get("exit_code", 1))
                    self.pipeline_job["error_report"] = diag
                self.bus.publish({"source": src, "step": "recognize", "status": "error", "message": f"补跑失败：{diag['title']}"})
            finally:
                core.release_pipeline_lease(lease_token, self.batches_root)
                with self._lock:
                    if self._pipeline_lease_token == lease_token:
                        self._pipeline_lease_token = ""

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "batch_id": active["batch_id"], "source": src}

    def start_benchmark(self, samples: int = 4) -> dict:
        """启动本机 Batch/并发性能压测（零 API 费用，自动选出最优参数）。"""
        diag_mod = core._load_diag_module(self.root)
        with self._lock:
            if self.benchmark_job["running"]:
                return {"ok": False, "error": "本机性能压测正在进行中，请稍候完成"}
            if self.pipeline_job["running"] or self.train_job["running"]:
                return {"ok": False, "error": "当前有识别或训练任务正在占用显卡/CPU，请等结束后再测速"}
            self.benchmark_job = {
                "running": True,
                "logs": [f"[{core.now_str()}] 开始本机性能压测（抽测 {samples} 张样卷，纯本地推理不花 API 费用）..."],
                "status": "running",
                "result": None,
                "error": "",
            }

        def worker():
            import importlib.util
            self.bus.publish({"source": "gui", "step": "check", "status": "running", "message": "正在执行本机 Batch/并发性能压测（零 API 费用）..."})
            try:
                spec = importlib.util.spec_from_file_location("_perf_bench", str(self.root / "scripts" / "benchmark_local.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                def cb(msg: str):
                    with self._lock:
                        self.benchmark_job["logs"].append(f"[测速] {msg}")
                    self.bus.publish({"source": "gui", "step": "check", "status": "running", "message": msg})

                res = mod.run_benchmark(self.root, sample_count=max(2, min(8, int(samples))), progress_cb=cb)
                ok = bool(res.get("ok"))
                with self._lock:
                    self.benchmark_job["running"] = False
                    self.benchmark_job["status"] = "done" if ok else "error"
                    self.benchmark_job["result"] = res if ok else None
                    self.benchmark_job["error"] = "" if ok else str(res.get("error") or "压测失败")
                self.bus.publish({"source": "gui", "step": "check", "status": "done" if ok else "error", "message": res.get("summary") or res.get("error") or "压测完成"})
            except Exception as exc:
                diag = diag_mod.diagnose_exception(exc, default_module="本机性能压测")
                with self._lock:
                    self.benchmark_job["running"] = False
                    self.benchmark_job["status"] = "error"
                    self.benchmark_job["error"] = f"【{diag['module']}】{diag['title']}：{diag['reason']}（建议：{diag['fix']}）"
                self.bus.publish({"source": "gui", "step": "check", "status": "error", "message": f"性能压测失败：{diag['title']}"})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def start_training(self, payload: dict) -> dict:
        task = str(payload.get("task") or "").strip()
        if task not in train_bridge.ALL_TASKS:
            return {"ok": False, "error": f"不支持的训练模块：{task}"}
        meta = train_bridge.TASK_META[task]
        hw = train_bridge.detect_hardware()
        purpose = train_bridge._sanitize_purpose(str(payload.get("purpose") or "工作台微调"))
        epochs = int(payload.get("epochs") or meta["default_epochs"])
        imgsz = int(payload.get("imgsz") or meta["default_imgsz"] or 0)
        device = str(payload.get("device") or hw["default_device"]).strip()
        is_cuda = device.lower() != "cpu" and hw["cuda"]
        batch = int(payload.get("batch") or (meta["default_batch_gpu"] if is_cuda else meta["default_batch_cpu"]))
        lr = float(payload.get("lr") or meta["default_lr"])

        from datetime import datetime
        run_name = f"{task}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{purpose}"

        with self._lock:
            if self.train_job["running"]:
                return {"ok": False, "error": f"已有训练任务正在运行：{self.train_job['run']}，请等待完成或先停止"}
            if self.pipeline_job["running"]:
                return {"ok": False, "error": "当前正在执行自动识别，请等识别结束或先停止后再启动训练"}
            if self.benchmark_job["running"]:
                return {"ok": False, "error": "当前正在执行本机测速，请等测速结束后再启动训练"}
            self.train_job = {
                "running": True,
                "task": task,
                "run": run_name,
                "epochs": epochs,
                "completed_epochs": 0,
                "metrics": {},
                "logs": [f"[{core.now_str()}] 启动 {meta['name']} 微调训练：{run_name}（epochs={epochs}, device={device}, batch={batch}）"],
                "status": "running",
                "returncode": None,
            }

        def worker():
            run_dir = self.root / "train" / run_name
            self.bus.publish({"source": "gui", "step": "train", "status": "running", "message": f"开始训练 {meta['name']} ({run_name})..."})
            cmd = [
                sys.executable, "-B", str(HERE / "train_bridge.py"), "run-train",
                "--task", task,
                "--run", run_name,
                "--purpose", purpose,
                "--epochs", str(epochs),
                "--batch", str(batch),
                "--device", device,
                "--lr", str(lr),
            ]
            if imgsz > 0 and task in train_bridge.YOLO_TASKS:
                cmd += ["--imgsz", str(imgsz)]

            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            stop_tail = threading.Event()

            def tail_metrics_and_log():
                log_file = run_dir / "train.log"
                pos = 0
                while not stop_tail.is_set():
                    if log_file.is_file():
                        try:
                            with log_file.open("r", encoding="utf-8", errors="replace") as fh:
                                fh.seek(pos)
                                chunk = fh.read()
                                pos = fh.tell()
                            if chunk:
                                new_lines = [ln.rstrip() for ln in chunk.splitlines() if ln.strip()]
                                if new_lines:
                                    with self._lock:
                                        self.train_job["logs"].extend(new_lines)
                                        if len(self.train_job["logs"]) > 240:
                                            self.train_job["logs"] = self.train_job["logs"][-240:]
                        except Exception:
                            pass
                    metrics = train_bridge._read_latest_metrics(run_dir)
                    if metrics:
                        with self._lock:
                            self.train_job["metrics"] = metrics
                            self.train_job["completed_epochs"] = int(metrics.get("completed_epochs") or 0)
                    stop_tail.wait(2.0)

            tail_thread = threading.Thread(target=tail_metrics_and_log, daemon=True)
            tail_thread.start()

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation,
                )
                with self._lock:
                    self._train_proc = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    text = line.rstrip()
                    if not text:
                        continue
                    with self._lock:
                        self.train_job["logs"].append(text)
                        if len(self.train_job["logs"]) > 240:
                            self.train_job["logs"] = self.train_job["logs"][-240:]
                    self.bus.publish({"source": "gui", "step": "train", "status": "running", "message": text})
                rc = proc.wait()
                stop_tail.set()
                tail_thread.join(timeout=1.5)
                metrics = train_bridge._read_latest_metrics(run_dir)
                with self._lock:
                    was_stopped = self.train_job.get("status") == "stopped"
                    self._train_proc = None
                    self.train_job["running"] = False
                    self.train_job["returncode"] = rc
                    if metrics:
                        self.train_job["metrics"] = metrics
                        self.train_job["completed_epochs"] = int(metrics.get("completed_epochs") or 0)
                    if not was_stopped:
                        self.train_job["status"] = "done" if rc == 0 else "error"
                msg = (
                    f"{meta['name']} 训练已手动停止"
                    if was_stopped
                    else (f"{meta['name']} 训练完成！可在下方候补列表中点击「启用此权重」" if rc == 0 else f"{meta['name']} 训练退出异常（代码 {rc}），请查看日志")
                )
                self.bus.publish({"source": "gui", "step": "train", "status": "done" if rc == 0 else "error", "message": msg})
            except Exception as exc:
                stop_tail.set()
                with self._lock:
                    self._train_proc = None
                    self.train_job["running"] = False
                    self.train_job["status"] = "error"
                    self.train_job["logs"].append(f"异常：{exc}")
                self.bus.publish({"source": "gui", "step": "train", "status": "error", "message": f"启动训练失败：{exc}"})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "run": run_name, "task": task}

    def stop_training(self) -> dict:
        with self._lock:
            proc = self._train_proc
            run_name = self.train_job.get("run") or ""
            if not self.train_job.get("running"):
                return {"ok": True, "message": "当前没有正在运行的训练任务"}
            self.train_job["status"] = "stopped"
            self.train_job["logs"].append(f"[{core.now_str()}] 收到停止指令，正在中止训练进程...")

        # 先尝试温和停止子训练进程（launch.json 记录的 train_pid）
        if run_name:
            launch = core.read_json(self.root / "train" / run_name / "launch.json", {}) or {}
            child_pid = launch.get("train_pid")
            if child_pid:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"], capture_output=True, timeout=5)
                    else:
                        os.kill(int(child_pid), 15)
                except Exception:
                    pass
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.bus.publish({"source": "gui", "step": "train", "status": "stopped", "message": f"已停止训练任务：{run_name}"})
        return {"ok": True, "message": f"已停止训练：{run_name}"}

    def _collect_frozen_context(self, surface: str, ui_state: dict | None = None) -> dict:
        active = core.get_active_batch(self.batches_root)
        rep = self.last_check or core.check_environment(self.root, self.batches_root, deep=False)
        recent_errors: list[str] = []
        if isinstance(ui_state, dict) and isinstance(ui_state.get("recent_errors"), list):
            recent_errors.extend(str(x) for x in ui_state["recent_errors"][:5])
        if self.pipeline_job.get("error_report"):
            er = self.pipeline_job["error_report"]
            recent_errors.append(f"[{er.get('module', '流水线')}] {er.get('title', '')}：{er.get('reason', '')}")
        if active:
            err_log = Path(active["recognize_dir"]) / "logs" / "errors.log"
            if err_log.is_file():
                try:
                    tail = [ln.strip() for ln in err_log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-5:]
                    recent_errors.extend(tail)
                except OSError:
                    pass
        chk_sum = "全部就绪" if rep.get("ok") else f"发现 {rep.get('error_count', 0)} 项错误 / {rep.get('warning_count', 0)} 项提醒"
        rec_status = ""
        if active:
            rs = self._read_run_stats(Path(active["recognize_dir"]))
            if rs:
                rec_status = str(rs.get("status") or "")
        return {
            "surface": surface,
            "batch_id": active.get("batch_id", "") if active else "",
            "rec_status": rec_status,
            "check_summary": chk_sum,
            "ui_state": ui_state or (self.latest_ui_state() if surface == "workbench" else {}),
            "recent_errors": recent_errors[:8],
        }

    def perform_capture(self, payload: dict) -> dict:
        """统一离屏无遮挡截图入口（支持 8888 控制台无头渲染 & 8904 试卷高清合成/无头 UI 渲染）。"""
        target = str(payload.get("target") or "workbench").strip().lower()
        mode = str(payload.get("mode") or "auto").strip().lower()
        page_id = str(payload.get("page_id") or "").strip()
        item_id = str(payload.get("item_id") or "").strip()
        selector = str(payload.get("selector") or "").strip()
        crop = payload.get("crop")
        with_elements = bool(payload.get("with_elements", True))
        som = bool(payload.get("som", False))
        mirror_client = str(payload.get("mirror_client") or "").strip()
        width = int(payload.get("width") or 1440)
        height = int(payload.get("height") or 900)

        if target == "review":
            # 自动模式：若指定了 item_id / page_id 且未指定 CSS selector，默认优先走高清原图合成（快且字迹超清晰）
            use_canvas = (mode == "canvas") or (mode == "auto" and not selector and not som)
            if use_canvas:
                active = core.get_active_batch(self.batches_root)
                if not active:
                    return {"ok": False, "error": "当前没有活跃批次，无法截取审阅试卷图"}
                return capture_mod.capture_review_canvas(
                    Path(active["recognize_dir"]),
                    self.batches_root,
                    page_id=page_id,
                    item_id=item_id,
                    crop=crop,
                    with_elements=with_elements,
                )
            review_port = int(payload.get("review_port") or 8904)
            url = f"http://127.0.0.1:{review_port}/"
            res = capture_mod.capture_headless_ui(
                url,
                self.batches_root,
                target="review",
                mirror_client=mirror_client,
                selector=selector,
                crop=crop,
                width=width,
                height=height,
                with_elements=with_elements,
                som=som,
            )
            if not res.get("ok"):
                # 兜底退回高清原图合成
                active = core.get_active_batch(self.batches_root)
                if active:
                    fb = capture_mod.capture_review_canvas(
                        Path(active["recognize_dir"]),
                        self.batches_root,
                        page_id=page_id,
                        item_id=item_id,
                        crop=crop,
                        with_elements=with_elements,
                    )
                    if fb.get("ok"):
                        fb["fallback_reason"] = res.get("error", "")
                        return fb
            return res

        url = f"http://127.0.0.1:{self.port}/"
        return capture_mod.capture_headless_ui(
            url,
            self.batches_root,
            target="workbench",
            mirror_client=mirror_client,
            selector=selector,
            crop=crop,
            width=width,
            height=height,
            with_elements=with_elements,
            som=som,
        )

    def start_ai_helper(self, payload: dict) -> dict:
        """统一多 Agent、多会话、全权同构 Computer-Use 调度入口（8888 与 8904 共用）。"""
        rl = _load_run_log_mod(self.root)
        cfg = core.load_config(self.batches_root)
        pref = str(payload.get("provider") or cfg.get("ai_helper_provider") or "auto").strip().lower()
        default_mode = str(cfg.get("ai_default_mode") or "yolo").strip().lower()
        mode = str(payload.get("mode") or default_mode or "yolo").strip().lower()
        if mode not in ("yolo", "guarded", "plan"):
            mode = "yolo"
        surface = "review" if str(payload.get("surface") or "").strip().lower() == "review" else "workbench"
        chat_id = str(payload.get("chat_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        raw_prompt = str(payload.get("prompt") or "").strip()
        note = str(payload.get("note") or "").strip()
        user_msg = note or raw_prompt
        ui_state = payload.get("ui_state") if isinstance(payload.get("ui_state"), dict) else None
        attach = [str(x) for x in (payload.get("attach") or []) if isinstance(x, str) and x.strip()]
        attach_elements_path = ""

        # 若用户勾选了「附带当前画面」，在发送瞬间离屏截取实时画面并自动切换精简提示词
        want_shot = bool(payload.get("attach_screenshot", cfg.get("ai_default_screenshot", False)))
        if want_shot and not attach:
            cap_req = {
                "target": surface,
                "mode": "ui" if surface == "workbench" else str(payload.get("screenshot_mode") or "auto"),
                "page_id": str((ui_state or {}).get("page_id") or ""),
                "item_id": str((ui_state or {}).get("selected_item_id") or ""),
                "mirror_client": str((ui_state or {}).get("client_id") or ""),
                "with_elements": True,
            }
            cap_res = self.perform_capture(cap_req)
            if cap_res.get("ok") and cap_res.get("path"):
                attach.append(cap_res["path"])
                attach_elements_path = str(cap_res.get("elements_path") or "")

        frozen_ctx = self._collect_frozen_context(surface, ui_state=ui_state)
        hub_url = f"http://127.0.0.1:{self.port}"
        review_port = int(payload.get("review_port") or 8904)
        review_url = f"http://127.0.0.1:{review_port}"

        # 注意：用户消息本身不提前脱敏，允许用户把要配置的 API Key/密码直接交给本地 Agent 导入；
        # 系统快照与所有落盘日志/SSE/响应均会严格脱敏！
        full_prompt = raw_prompt if raw_prompt else agent_providers.build_computer_use_prompt(
            user_message=user_msg,
            surface=surface,
            root=self.root,
            hub_url=hub_url,
            review_url=review_url,
            context_snapshot=frozen_ctx,
            attached_images=attach,
            attached_elements_path=attach_elements_path,
            mode=mode,
        )

        res = self.ai_manager.submit_turn(
            chat_id=chat_id,
            surface=surface,
            provider=pref,
            mode=mode,
            user_message=user_msg,
            full_prompt=full_prompt,
            session_id=session_id,
            attach=attach,
        )
        if not res.get("ok") and not res.get("headless"):
            # 无任何可用本地 CLI Agent 时，回退脱敏剪贴板
            safe_prompt = agent_providers.scrub_text(full_prompt, agent_providers.extract_inline_secrets(user_msg))
            fallback = core.launch_opencode(self.root, safe_prompt, batches_root=self.batches_root)
            rl.append_cli_log(
                self.batches_root,
                command="ai.fallback",
                source="ai",
                actor="gui",
                ok=True,
                message=fallback.get("message") or "",
                detail={"provider": "opencode" if fallback.get("launched") else "clipboard"},
            )
            self.bus.publish({"source": "ai", "step": "agent", "status": "done", "message": fallback.get("message") or "诊断提示词已复制"})
            return {**fallback, "provider": "opencode" if fallback.get("launched") else "clipboard", "headless": False}

        return {**res, "attached_screenshot": attach[0] if attach else ""}

    def stop_ai_helper(self, chat_id: str = "") -> dict:
        return self.ai_manager.stop_session(chat_id)

    def shutdown_all(self) -> None:
        self._stop_event.set()
        try:
            self.ai_manager.stop_all()
        except Exception:
            pass
        if self.pipeline_job.get("running"):
            try:
                self.stop_pipeline()
            except Exception:
                pass
        if self.train_job.get("running"):
            try:
                self.stop_training()
            except Exception:
                pass
        with self._lock:
            ids = list(self.procs.keys())
        for tid in ids:
            try:
                self.stop_service(tid)
            except Exception:
                pass


def pick_windows_folder(initial_dir: Path | str | None = None, timeout_seconds: int = 120) -> dict:
    """弹出 Windows 原生文件夹选择对话框（默认定位到当前项目路径、强制置顶、优雅处理超时取消）。"""
    if os.name != "nt":
        return {"ok": True, "path": "", "cancelled": True, "message": "当前非 Windows 系统，请手动粘贴文件夹路径"}
    init_p = Path(initial_dir).resolve() if initial_dir else ROOT
    if not init_p.is_dir():
        init_p = ROOT
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '请选择存放新试卷照片的文件夹（默认从当前项目目录展开）'; "
        "$f.ShowNewFolderButton = $true; "
        "if ($env:AGY_PICK_INIT_DIR -and (Test-Path -LiteralPath $env:AGY_PICK_INIT_DIR)) { $f.SelectedPath = $env:AGY_PICK_INIT_DIR }; "
        "$top = New-Object System.Windows.Forms.Form; $top.TopMost = $true; $top.StartPosition = 'Manual'; $top.Location = New-Object System.Drawing.Point(-2000, -2000); $top.ShowInTaskbar = $false; "
        "$top.Show(); $top.Activate(); "
        "$res = $f.ShowDialog($top); $top.Dispose(); "
        "if ($res -eq 'OK') { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Output $f.SelectedPath }"
    )
    env = dict(os.environ, AGY_PICK_INIT_DIR=str(init_p))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10, int(timeout_seconds)),
            env=env,
        )
        chosen = (proc.stdout or "").strip()
        return {"ok": True, "path": chosen, "cancelled": not bool(chosen)}
    except subprocess.TimeoutExpired:
        return {
            "ok": True,
            "path": "",
            "cancelled": True,
            "timeout": True,
            "message": "文件夹选择弹窗超过 120 秒未操作已自动关闭；您可以重新点击选择，或直接粘贴文件夹路径。",
        }
    except Exception as exc:
        return {"ok": False, "path": "", "error": f"打开系统文件夹弹窗失败：{exc}"}


def make_handler(state: WorkbenchState):
    html_path = HERE / "index.html"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def _is_host(self) -> bool:
            ip = (self.client_address[0] or "").strip()
            return ip in ("127.0.0.1", "::1", "localhost")

        def _send_json(self, code: int, obj: dict) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _require_host(self) -> bool:
            if not self._is_host():
                self._send_json(403, {"ok": False, "error": "此操作仅限主机（服务端电脑）执行"})
                return False
            return True

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/health":
                self._send_json(200, {"ok": True, "port": state.port, "ts": core.now_str()})
                return

            if path == "/api/vlm/config":
                if not self._require_host():
                    return
                self._send_json(200, {"ok": True, **core.get_vlm_settings(state.root)})
                return

            if path == "/api/pricing":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                vlm_cfg = core.get_vlm_settings(state.root)
                q_model = str(qs.get("model", [vlm_cfg.get("model") or ""])[0] or "").strip()
                q_prov = str(qs.get("provider", [vlm_cfg.get("provider") or ""])[0] or "").strip()
                pricing_mod = _load_pricing_mod(state.root)
                cache = pricing_mod.load_pricing_cache(state.root) or {}
                matched = pricing_mod.lookup_model_price(q_model, provider=q_prov, root=state.root)
                self._send_json(200, {
                    "ok": True,
                    "model": q_model,
                    "provider": q_prov,
                    "price": matched,
                    "cache_exists": bool(cache.get("models")),
                    "crawled_at": str(cache.get("crawled_at") or ""),
                    "model_count": len(cache.get("models") or {}),
                    "providers": cache.get("providers") or {},
                    "cache_path": str(pricing_mod.default_cache_path(state.root)),
                })
                return

            if path == "/api/check":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                deep = qs.get("deep", ["0"])[0] in ("1", "true", "yes")
                rep = state.run_check(deep=deep)
                self._send_json(200, rep)
                return

            if path == "/api/benchmark/status":
                if not self._require_host():
                    return
                self._send_json(200, {"ok": True, "benchmark_job": state.benchmark_job_snapshot()})
                return

            if path == "/api/ai/status":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                cid = str((qs.get("chat_id") or [""])[0] or "").strip()
                cfg_now = core.load_config(state.batches_root)
                ai_snap = state.ai_manager.snapshot_session(cid) if cid else state.ai_job_snapshot()
                self._send_json(200, {
                    "ok": True,
                    "ai_job": ai_snap,
                    "sessions": state.ai_manager.list_sessions(),
                    "zcode": core.find_zcode(cfg_now),
                    "agents": agent_providers.detect_all_agents(cfg_now, deep=False),
                })
                return

            if path == "/api/ai/sessions":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                surf = str((qs.get("surface") or [""])[0] or "").strip() or None
                self._send_json(200, {"ok": True, "sessions": state.ai_manager.list_sessions(surface=surf)})
                return

            if path == "/api/agents":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                deep = qs.get("deep", ["0"])[0] in ("1", "true", "yes")
                probe_v = qs.get("probe_version", ["0"])[0] in ("1", "true", "yes")
                cfg_now = core.load_config(state.batches_root)
                self._send_json(200, agent_providers.detect_all_agents(cfg_now, deep=deep, probe_version=probe_v))
                return

            if path == "/api/ui-state":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                cid = str((qs.get("client_id") or [""])[0] or "").strip()
                self._send_json(200, {"ok": True, "active": state.latest_ui_state(cid)})
                return

            if path == "/api/config/export":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                inc_sec = qs.get("include_secrets", ["0"])[0] in ("1", "true", "yes")
                cfg_now = core.load_config(state.batches_root)
                vlm_now = core.get_vlm_settings(state.root)
                vlm_export = {
                    "provider": vlm_now.get("provider", "doubao"),
                    "base_url": vlm_now.get("base_url", ""),
                    "model": vlm_now.get("model", ""),
                }
                if inc_sec:
                    raw_key = core._read_env_key(state.root, "VLM_API_KEY") or core._read_env_key(state.root, "ARK_API_KEY")
                    if raw_key:
                        vlm_export["api_key"] = raw_key
                else:
                    vlm_export["api_key_masked"] = vlm_now.get("api_key_masked", "")
                self._send_json(200, {
                    "ok": True,
                    "schema_version": 1,
                    "exported_at": core.now_str(),
                    "include_secrets": inc_sec,
                    "workbench": cfg_now,
                    "vlm": vlm_export,
                })
                return

            if path == "/api/machine":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                probe_v = qs.get("probe_version", ["0"])[0] in ("1", "true", "yes")
                info = core.collect_machine_info(state.root, state.batches_root, probe_zcode_version=probe_v)
                info["services"] = state.service_snapshot()
                info["pipeline_lease"] = core.read_pipeline_lease(state.batches_root)
                self._send_json(200, {"ok": True, **info})
                return

            if path == "/api/runs":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                bid = str(qs.get("batch_id", [""])[0] or "").strip() or None
                lim = int(qs.get("limit", ["50"])[0] or 50)
                rl = _load_run_log_mod(state.root)
                runs = rl.list_runs(state.batches_root, batch_id=bid, limit=lim)
                self._send_json(200, {"ok": True, "runs": runs, "count": len(runs)})
                return

            if path.startswith("/api/runs/"):
                if not self._require_host():
                    return
                rid = urllib.parse.unquote(path[len("/api/runs/"):]).strip("/")
                rl = _load_run_log_mod(state.root)
                det = rl.get_run_detail(state.batches_root, rid)
                if not det:
                    self._send_json(404, {"ok": False, "error": f"未找到运行记录：{rid}"})
                    return
                self._send_json(200, {"ok": True, **det})
                return

            if path == "/api/logs":
                if not self._require_host():
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                rl = _load_run_log_mod(state.root)
                res = rl.query_events(
                    state.batches_root,
                    run_id=str(qs.get("run_id", [""])[0] or ""),
                    batch_id=str(qs.get("batch_id", [""])[0] or ""),
                    level=str(qs.get("level", [""])[0] or ""),
                    event=str(qs.get("event", [""])[0] or ""),
                    page_id=str(qs.get("page_id", [""])[0] or ""),
                    grid_id=str(qs.get("grid_id", [""])[0] or ""),
                    q=str(qs.get("q", [""])[0] or ""),
                    limit=int(qs.get("limit", ["200"])[0] or 200),
                    offset=int(qs.get("offset", ["0"])[0] or 0),
                )
                self._send_json(200, {"ok": True, **res})
                return

            if path == "/api/pipeline/errors":
                if not self._require_host():
                    return
                active = core.get_active_batch(state.batches_root)
                log_txt = ""
                log_path = ""
                if active:
                    p = Path(active["recognize_dir"]) / "logs" / "errors.log"
                    log_path = str(p)
                    if p.is_file():
                        log_txt = p.read_text(encoding="utf-8", errors="replace")[-12000:]
                self._send_json(200, {"ok": True, "path": log_path, "content": log_txt})
                return

            if path == "/api/train/overview":
                if not self._require_host():
                    return
                try:
                    data = train_bridge.get_training_overview(state.root, state.batches_root)
                    svcs = state.service_snapshot()
                    data["train_job"] = state.train_job_snapshot()
                    data["monitor_running"] = bool(svcs.get("monitor", {}).get("running"))
                    self._send_json(200, data)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if path == "/api/overview":
                services = state.service_snapshot()
                data = core.collect_tasks_and_leaderboard(
                    root=state.root,
                    batches_root=state.batches_root,
                    services=services,
                    presence=state.presence,
                )
                lan_ip = core.detect_lan_ip()
                lan_url = f"http://{lan_ip}:{state.port}"
                active = data.get("active_batch")
                rec_summary = state._read_run_stats(Path(active["recognize_dir"])) if active else None
                diag_mod = core._load_diag_module(state.root)
                data.update({
                    "is_host": self._is_host(),
                    "lan_ip": lan_ip,
                    "lan_url": lan_url,
                    "port": state.port,
                    "subjects": core.DEFAULT_SUBJECTS,
                    "pipeline_job": state.pipeline_job_snapshot(),
                    "recognition_summary": rec_summary,
                    "benchmark_job": state.benchmark_job_snapshot() if self._is_host() else None,
                    "ai_job": state.ai_job_snapshot() if self._is_host() else None,
                    "ai_sessions": state.ai_manager.list_sessions() if self._is_host() else None,
                    "zcode": core.find_zcode(core.load_config(state.batches_root)) if self._is_host() else None,
                    "agents": agent_providers.detect_all_agents(core.load_config(state.batches_root), deep=False) if self._is_host() else None,
                    "pipeline_lease": core.read_pipeline_lease(state.batches_root) if self._is_host() else None,
                    "infer_settings": core.get_infer_settings(state.batches_root) if self._is_host() else {},
                    "hardware": diag_mod.probe_compute_hardware() if self._is_host() else {},
                    "last_check": state.last_check if self._is_host() else None,
                    "config": core.load_config(state.batches_root) if self._is_host() else {},
                })
                self._send_json(200, data)
                return

            if path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = state.bus.subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while not state._stop_event.is_set():
                        try:
                            ev = q.get(timeout=5)
                            chunk = ("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8")
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    state.bus.unsubscribe(q)
                return

            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                body = self._read_json()
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": f"无效请求：{exc}"})
                return

            # 协作者 & 主机均可调用的接口
            if path == "/api/presence":
                cid = str(body.get("client_id") or self.client_address[0])[:64]
                name = str(body.get("name") or "成员").strip()[:24] or "成员"
                task = str(body.get("task") or "总览").strip()[:24]
                state.presence[cid] = {
                    "name": name,
                    "role": "host" if self._is_host() else "collaborator",
                    "task": task,
                    "seen": time.time(),
                }
                self._send_json(200, {"ok": True})
                return

            if path == "/api/service/ensure":
                try:
                    res = state.start_service(str(body.get("task_id") or ""), extra=body)
                    self._send_json(200, res)
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/open-edit":
                # 从人工复审（整页核对）单张试卷一键进入三合一标框修改子界面
                try:
                    raw_name = str(body.get("page_name") or "").strip()
                    safe_name = Path(raw_name).name if raw_name else ""
                    stem = Path(safe_name).stem if safe_name else ""
                    if not stem or stem in (".", "..") or "/" in stem or "\\" in stem:
                        raise ValueError("非法的试卷文件名")
                    m1_imgs = (state.root / "data" / "m1" / "images").resolve()
                    m1_imgs.mkdir(parents=True, exist_ok=True)
                    target = (m1_imgs / f"{stem}.jpg").resolve()
                    if target.parent != m1_imgs:
                        raise ValueError("非法的目标文件路径")
                    if not target.is_file():
                        # 按需从当前活跃批次复制一份（非破坏性），确保单张一定可编辑
                        active = core.get_active_batch(state.batches_root)
                        if active:
                            src_dir = Path(active["images_dir"]).resolve()
                            for cand_name in (safe_name, f"{stem}.jpg"):
                                cand = (src_dir / cand_name).resolve()
                                if cand.is_relative_to(src_dir) and cand.is_file():
                                    import shutil
                                    shutil.copy2(cand, target)
                                    break
                    res = state.start_service("annotate", extra={"m": "m1"})
                    self._send_json(200, {"ok": True, "port": res["port"], "stem": stem, "synced": target.is_file()})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            # 以下仅主机可执行
            if not self._require_host():
                return

            if path == "/api/event":
                state.bus.publish(body)
                self._send_json(200, {"ok": True})
                return

            if path == "/api/config":
                patch = {}
                if "opencode_path" in body:
                    patch["opencode_path"] = str(body["opencode_path"] or "").strip()
                if "zcode_exe" in body:
                    patch["zcode_exe"] = str(body["zcode_exe"] or "").strip()
                if "zcode_cli" in body:
                    patch["zcode_cli"] = str(body["zcode_cli"] or "").strip()
                if "agent_paths" in body and isinstance(body["agent_paths"], dict):
                    existing_ap = dict(core.load_config(state.batches_root).get("agent_paths") or {})
                    for k, v in body["agent_paths"].items():
                        if k in agent_providers.PROVIDER_META:
                            existing_ap[k] = str(v or "").strip()
                    patch["agent_paths"] = existing_ap
                if "agent_custom_cmd" in body:
                    patch["agent_custom_cmd"] = str(body["agent_custom_cmd"] or "").strip()
                if "ai_helper_provider" in body:
                    prov = str(body["ai_helper_provider"] or "auto").strip().lower()
                    allowed_prov = ("auto",) + agent_providers.PROVIDER_ORDER
                    patch["ai_helper_provider"] = prov if prov in allowed_prov else "auto"
                if "ai_default_mode" in body:
                    dm = str(body["ai_default_mode"] or "yolo").strip().lower()
                    patch["ai_default_mode"] = dm if dm in ("yolo", "guarded", "plan") else "yolo"
                if "ai_default_screenshot" in body:
                    patch["ai_default_screenshot"] = bool(body["ai_default_screenshot"])
                if "infer_device" in body:
                    dev = str(body["infer_device"] or "auto").strip()
                    patch["infer_device"] = dev if dev in ("auto", "cpu", "cuda:0", "cuda") else "auto"
                if "yolo_batch" in body:
                    patch["yolo_batch"] = max(0, min(16, int(body["yolo_batch"] or 0)))
                if "cpu_workers" in body:
                    patch["cpu_workers"] = max(0, min(16, int(body["cpu_workers"] or 0)))
                if "api_concurrency" in body:
                    patch["api_concurrency"] = max(1, min(12, int(body["api_concurrency"] or 4)))
                if "last_benchmark" in body and isinstance(body["last_benchmark"], dict):
                    patch["last_benchmark"] = body["last_benchmark"]
                cfg = core.save_config(patch, state.batches_root)
                state.last_check = core.check_environment(state.root, state.batches_root, deep=False)
                state.bus.publish({"source": str(body.get("source") or "gui"), "step": "check", "status": "done", "message": "工作台配置已更新"})
                self._send_json(200, {
                    "ok": True,
                    "config": cfg,
                    "agents": agent_providers.detect_all_agents(cfg, deep=False),
                    "infer_settings": core.get_infer_settings(state.batches_root),
                })
                return

            if path == "/api/config/import":
                # 一键导入整包配置（工作台参数 + Agent 路径 + VLM 密钥/模型 + 可选自定义单价），自动脱敏返回
                applied: list[str] = []
                wb_part = body.get("workbench") if isinstance(body.get("workbench"), dict) else {}
                if wb_part:
                    allowed_keys = {
                        "opencode_path", "zcode_exe", "zcode_cli", "agent_paths", "agent_custom_cmd",
                        "ai_helper_provider", "ai_default_mode", "ai_default_screenshot",
                        "infer_device", "yolo_batch", "cpu_workers", "api_concurrency",
                    }
                    clean_wb = {k: wb_part[k] for k in wb_part if k in allowed_keys}
                    if clean_wb:
                        core.save_config(clean_wb, state.batches_root)
                        applied.append(f"工作台配置({len(clean_wb)}项)")
                vlm_part = body.get("vlm") if isinstance(body.get("vlm"), dict) else {}
                vlm_res = None
                if vlm_part and any(str(vlm_part.get(k) or "").strip() for k in ("provider", "base_url", "api_key", "model")):
                    vlm_res = core.save_vlm_settings(
                        provider=str(vlm_part.get("provider") or "doubao"),
                        base_url=str(vlm_part.get("base_url") or ""),
                        api_key=str(vlm_part.get("api_key") or ""),
                        model=str(vlm_part.get("model") or ""),
                        root=state.root,
                    )
                    if not vlm_res.get("ok"):
                        self._send_json(400, {"ok": False, "error": vlm_res.get("error") or "导入 VLM 密钥失败"})
                        return
                    applied.append("大模型 API 配置与密钥")
                env_part = body.get("env") if isinstance(body.get("env"), dict) else {}
                if env_part:
                    env_res = core.save_env_keys({str(k): str(v) for k, v in env_part.items() if str(v or "").strip()}, root=state.root)
                    if not env_res.get("ok"):
                        self._send_json(400, env_res)
                        return
                    applied.append(f".env 键值({len(env_part)}项)")
                pricing_part = body.get("pricing_overrides") if isinstance(body.get("pricing_overrides"), list) else []
                if pricing_part:
                    pricing_mod = _load_pricing_mod(state.root)
                    for pr in pricing_part:
                        if isinstance(pr, dict) and pr.get("model"):
                            pricing_mod.save_custom_price_override(
                                model_id=str(pr["model"]),
                                input_per_m=float(pr["input_per_m"]),
                                output_per_m=float(pr["output_per_m"]),
                                cache_hit_per_m=(float(pr["cache_hit_per_m"]) if pr.get("cache_hit_per_m") not in (None, "") else None),
                                root=state.root,
                            )
                    applied.append(f"自定义模型单价({len(pricing_part)}项)")
                state.last_check = core.check_environment(state.root, state.batches_root, deep=False)
                msg = "配置导入完成：" + ("、".join(applied) if applied else "无变更")
                state.bus.publish({"source": str(body.get("source") or "ai"), "step": "check", "status": "done", "message": msg})
                self._send_json(200, {
                    "ok": True,
                    "applied": applied,
                    "message": msg,
                    "vlm": core.get_vlm_settings(state.root),
                    "config": core.load_config(state.batches_root),
                })
                return

            if path == "/api/check/sync":
                rep = body.get("report")
                if isinstance(rep, dict) and "items" in rep:
                    state.last_check = rep
                else:
                    deep = bool(body.get("deep"))
                    state.last_check = core.check_environment(state.root, state.batches_root, deep=deep)
                src = str(body.get("source") or "cli")
                ok_c = bool((state.last_check or {}).get("ok"))
                err_n = int((state.last_check or {}).get("error_count") or 0)
                state.bus.publish({
                    "source": src,
                    "step": "check",
                    "status": "done" if ok_c else "error",
                    "message": "环境自检全部通过（已同步）" if ok_c else f"环境自检发现 {err_n} 项异常（已同步）",
                })
                self._send_json(200, {"ok": True, "last_check": state.last_check})
                return

            if path == "/api/repair/pip":
                rep = core.check_environment(state.root, state.batches_root)
                pkgs = body.get("packages") or rep["missing_packages"]
                state.bus.publish({"source": "gui", "step": "repair_pip", "status": "running", "message": f"正在测速选源并安装：{', '.join(pkgs) or '无'}"})
                res = core.install_packages(pkgs, progress_cb=lambda m: state.bus.publish({"source": "gui", "step": "repair_pip", "status": "running", "message": m}))
                state.bus.publish({"source": "gui", "step": "repair_pip", "status": "done" if res["ok"] else "error", "message": "依赖安装完成" if res["ok"] else "安装失败，建议点「求助 AI」"})
                self._send_json(200, res)
                return

            if path == "/api/repair/weights":
                pwd = str(body.get("password") or "")
                state.bus.publish({"source": "gui", "step": "repair_weights", "status": "running", "message": "正在解压加密模型权重..."})
                res = core.unpack_weights(pwd, root=state.root)
                state.bus.publish({"source": "gui", "step": "repair_weights", "status": "done" if res["ok"] else "error", "message": res.get("message") or res.get("error") or ""})
                self._send_json(200 if res["ok"] else 400, res)
                return

            if path == "/api/vlm/config":
                res = core.save_vlm_settings(
                    provider=str(body.get("provider") or "doubao"),
                    base_url=str(body.get("base_url") or ""),
                    api_key=str(body.get("api_key") or ""),
                    model=str(body.get("model") or ""),
                    root=state.root,
                )
                state.bus.publish({"source": "gui", "step": "check", "status": "done" if res.get("ok") else "error", "message": res.get("message") or res.get("error") or ""})
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/vlm/test":
                res = core.test_vlm_settings(
                    provider=str(body.get("provider") or "doubao"),
                    base_url=str(body.get("base_url") or ""),
                    api_key=str(body.get("api_key") or ""),
                    model=str(body.get("model") or ""),
                    root=state.root,
                )
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/pricing/refresh":
                try:
                    pricing_mod = _load_pricing_mod(state.root)
                    cache = pricing_mod.crawl_and_save_pricing(root=state.root)
                    vlm_cfg = core.get_vlm_settings(state.root)
                    q_model = str(body.get("model") or vlm_cfg.get("model") or "").strip()
                    q_prov = str(body.get("provider") or vlm_cfg.get("provider") or "").strip()
                    matched = pricing_mod.lookup_model_price(q_model, provider=q_prov, root=state.root)
                    ok_provs = sum(1 for p in (cache.get("providers") or {}).values() if p.get("ok"))
                    self._send_json(200, {
                        "ok": True,
                        "price": matched,
                        "crawled_at": str(cache.get("crawled_at") or ""),
                        "model_count": len(cache.get("models") or {}),
                        "ok_providers": ok_provs,
                        "providers": cache.get("providers") or {},
                        "message": f"已抓取 {ok_provs} 家官网，永久保存 {len(cache.get('models') or {})} 个模型定价",
                    })
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if path == "/api/pricing/override":
                try:
                    pricing_mod = _load_pricing_mod(state.root)
                    model_id = str(body.get("model") or "").strip()
                    in_rate = float(body.get("input_per_m"))
                    out_rate = float(body.get("output_per_m"))
                    raw_cache = body.get("cache_hit_per_m")
                    cache_rate = float(raw_cache) if raw_cache not in (None, "") else None
                    saved = pricing_mod.save_custom_price_override(
                        model_id=model_id,
                        input_per_m=in_rate,
                        output_per_m=out_rate,
                        cache_hit_per_m=cache_rate,
                        root=state.root,
                    )
                    self._send_json(200, {"ok": True, "price": saved, "message": f"已永久保存 {model_id} 自定义单价"})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/repair/env":
                key = str(body.get("key") or "VLM_API_KEY")
                val = str(body.get("value") or "")
                res = core.save_env_key(key, val, root=state.root)
                state.bus.publish({"source": "gui", "step": "check", "status": "done" if res["ok"] else "error", "message": res.get("message") or res.get("error") or ""})
                self._send_json(200 if res["ok"] else 400, res)
                return

            if path == "/api/repair/agent":
                res = state.start_ai_helper(body)
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/ai/run":
                res = state.start_ai_helper(body)
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/ai/stop":
                res = state.stop_ai_helper(str(body.get("chat_id") or ""))
                self._send_json(200, res)
                return

            if path == "/api/ai/sessions/new":
                sess = state.ai_manager.create_session(
                    surface=str(body.get("surface") or "workbench"),
                    provider=str(body.get("provider") or "auto"),
                    mode=str(body.get("mode") or "yolo"),
                    title=str(body.get("title") or ""),
                )
                self._send_json(200, {"ok": True, "session": sess, "sessions": state.ai_manager.list_sessions()})
                return

            if path == "/api/ai/sessions/delete":
                cid = str(body.get("chat_id") or "").strip()
                res = state.ai_manager.delete_session(cid)
                self._send_json(200, {**res, "sessions": state.ai_manager.list_sessions()})
                return

            if path == "/api/agents/test":
                prov = str(body.get("provider") or "zcode").strip().lower()
                cfg_now = core.load_config(state.batches_root)
                res = agent_providers.test_agent_provider(prov, cfg_now)
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/ui-state":
                snap = state.update_ui_state(body)
                self._send_json(200, {"ok": True, "active": {k: v for k, v in snap.items() if k != "_ts"}})
                return

            if path == "/api/ui/command":
                ev = {
                    "source": str(body.get("source") or "ai"),
                    "step": "ui_command",
                    "action": str(body.get("action") or "focus").strip()[:32],
                    "target": str(body.get("target") or "").strip()[:160],
                    "view": str(body.get("view") or "").strip()[:32],
                    "label": str(body.get("label") or "").strip()[:120],
                    "client_id": str(body.get("client_id") or "").strip()[:64],
                    "status": "done",
                    "message": str(body.get("label") or f"AI 聚焦界面元素 {body.get('target') or body.get('view') or ''}"),
                }
                state.bus.publish(ev)
                self._send_json(200, {"ok": True, "dispatched": ev})
                return

            if path == "/api/capture":
                res = state.perform_capture(body)
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/pick-folder":
                init_dir = body.get("initial_dir") or state.root
                res = pick_windows_folder(initial_dir=init_dir)
                self._send_json(200 if res.get("ok") else 500, res)
                return

            if path == "/api/ingest":
                source_dir = str(body.get("source_dir") or "").strip()
                subject = str(body.get("subject") or "物理").strip()
                batch_name = str(body.get("batch_name") or "").strip() or None
                try:
                    state.bus.publish({"source": "gui", "step": "ingest", "status": "running", "message": f"正在整理入库（学科：{subject}）..."})
                    meta = core.ingest_batch(
                        source_dir=source_dir,
                        subject=subject,
                        batches_root=state.batches_root,
                        batch_name=batch_name,
                        progress_cb=lambda m: state.bus.publish({"source": "gui", "step": "ingest", "status": "running", "message": m}),
                    )
                    msg = f"入库完成：{meta['batch_id']}，新增 {meta['last_imported']} 张（跳过重复 {meta['last_skipped_dup']} 张）"
                    state.bus.publish({"source": "gui", "step": "ingest", "status": "done", "message": msg})
                    self._send_json(200, {"ok": True, "batch": meta, "message": msg})
                except Exception as exc:
                    state.bus.publish({"source": "gui", "step": "ingest", "status": "error", "message": f"入库失败：{exc}"})
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/batch/select":
                bid = str(body.get("batch_id") or "").strip()
                meta = core.set_active_batch(bid, state.batches_root)
                if not meta:
                    self._send_json(404, {"ok": False, "error": "找不到指定批次"})
                    return
                # 切换批次时关闭旧批次的 8904 服务，防止再次点击进入时停留在旧批次
                with state._lock:
                    rev_info = state.procs.get("review")
                if rev_info:
                    prev_dir = str((rev_info.get("extra") or {}).get("recognize_dir") or "")
                    next_dir = str(Path(meta["recognize_dir"]).resolve())
                    if prev_dir and prev_dir != next_dir:
                        state.stop_service("review")
                state.bus.publish({"source": "gui", "step": "overview", "status": "done", "message": f"已切换到批次：{bid}"})
                self._send_json(200, {"ok": True, "batch": meta})
                return

            if path == "/api/pipeline/run":
                res = state.start_pipeline(body.get("batch_id"), source=str(body.get("source") or "gui"))
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/pipeline/stop":
                res = state.stop_pipeline()
                self._send_json(200, res)
                return

            if path == "/api/pipeline/retry-failed":
                res = state.start_retry_failed(body.get("batch_id"), source=str(body.get("source") or "gui"))
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/benchmark/start":
                res = state.start_benchmark(samples=int(body.get("samples") or 4))
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/service/stop":
                res = state.stop_service(str(body.get("task_id") or ""))
                self._send_json(200, res)
                return

            if path == "/api/train/sync-batch":
                try:
                    res = train_bridge.sync_batch_to_training_data(
                        root=state.root,
                        batches_root=state.batches_root,
                        batch_id=body.get("batch_id"),
                    )
                    state.bus.publish({"source": "gui", "step": "train", "status": "done", "message": res["message"]})
                    self._send_json(200, res)
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/train/start":
                res = state.start_training(body)
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/train/stop":
                res = state.stop_training()
                self._send_json(200, res)
                return

            if path == "/api/train/deploy":
                try:
                    res = train_bridge.deploy_run(
                        task=str(body.get("task") or "").strip(),
                        run_name=str(body.get("run") or "").strip(),
                        root=state.root,
                    )
                    state.bus.publish({"source": "gui", "step": "train", "status": "done", "message": res["message"]})
                    self._send_json(200, res)
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/train/rollback":
                try:
                    res = train_bridge.rollback_model(
                        task=str(body.get("task") or "").strip(),
                        root=state.root,
                    )
                    state.bus.publish({"source": "gui", "step": "train", "status": "done", "message": res["message"]})
                    self._send_json(200, res)
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            if path == "/api/train/monitor":
                try:
                    run_name = str(body.get("run") or state.train_job.get("run") or "").strip()
                    res = state.start_service("monitor", extra={"run": run_name})
                    self._send_json(200, {**res, "run": run_name})
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                return

            self._send_json(404, {"ok": False, "error": "Not found"})

    return Handler


def open_app_window(url: str) -> None:
    """优先用 Edge 独立应用模式打开（无地址栏，像桌面软件），失败则回退默认浏览器。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for exe in candidates:
        if os.path.isfile(exe):
            try:
                subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
    import webbrowser
    webbrowser.open(url)


def run_server(port: int = 8888, open_browser: bool = False, batches_root: Path | None = None) -> None:
    state = WorkbenchState(root=ROOT, batches_root=batches_root or core.DEFAULT_BATCHES_ROOT, port=port)
    # 启动即做一次快速自检缓存
    state.last_check = core.check_environment(state.root, state.batches_root)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))
    server.daemon_threads = True
    lan_ip = core.detect_lan_ip()
    print(f"自动改卷工作台已启动：", flush=True)
    print(f"  本机主控：http://127.0.0.1:{port}/", flush=True)
    print(f"  局域网协作：http://{lan_ip}:{port}/", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: open_app_window(f"http://127.0.0.1:{port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.shutdown_all()
        server.server_close()


def main():
    ap = argparse.ArgumentParser(description="自动改卷统一工作台控制中心")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--open", action="store_true", help="启动后自动打开独立应用窗口")
    args = ap.parse_args()
    run_server(port=args.port, open_browser=args.open)


if __name__ == "__main__":
    main()
