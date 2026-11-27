# -*- coding: utf-8 -*-
"""统一工作台本地控制中心（主机 + 局域网协作者总览 + SSE 实时高亮）。

用法：
    python tools/工作台/serve.py [--port 8888] [--open]
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
import train_bridge  # noqa: E402


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
        self.root = root
        self.batches_root = batches_root
        self.port = port
        self.bus = EventBus()
        self._lock = threading.RLock()
        self.procs: dict[str, dict] = {}  # task_id -> {proc, port, args, started_at}
        self.presence: dict[str, dict] = {}
        self.pipeline_job: dict = {
            "running": False,
            "kind": "recognize",
            "batch_id": "",
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
        self._train_proc: subprocess.Popen | None = None
        self.last_check: dict | None = None

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
        ports = {"review": 8904, "annotate": 8900, "choice": 8905, "digit": 8903, "monitor": 8901}
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
        ports = {"review": 8904, "annotate": 8900, "choice": 8905, "digit": 8903, "monitor": 8901}
        if task_id not in ports:
            raise ValueError(f"未知审核任务：{task_id}")
        port = ports[task_id]

        with self._lock:
            info = self.procs.get(task_id)
            need_restart = False
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
                active = core.get_active_batch(self.batches_root)
                if not active:
                    raise ValueError("还没有导入批次，请先在主机导入试卷并跑自动识别")
                rec_dir = Path(active["recognize_dir"])
                if not (rec_dir / "results.json").is_file():
                    raise ValueError("当前批次尚未完成自动识别，请先点「开始自动识别」")
                cmd = [sys.executable, str(self.root / "tools" / "识别验收.py"), "--dir", str(rec_dir), "--port", str(port), "--lan"]
            elif task_id == "annotate":
                m = extra.get("m") or "m1"
                if m not in ("m1", "m2", "m3"):
                    m = "m1"
                (self.root / "data" / m / "images").mkdir(parents=True, exist_ok=True)
                cmd = [sys.executable, str(self.root / "tools" / "标注工具" / "serve.py"), "--m", m, "--port", str(port), "--lan", "--no-open"]
                extra = {**extra, "m": m}
            elif task_id == "choice":
                cmd = [sys.executable, str(self.root / "tools" / "涂黑审核" / "serve.py"), "--port", str(port), "--lan"]
            elif task_id == "monitor":
                run_name = str(extra.get("run") or "").strip()
                cmd = [sys.executable, str(self.root / "tools" / "训练监控" / "serve.py"), "--port", str(port), "--no-open"]
                if run_name:
                    cmd += ["--run", str(self.root / "train" / run_name)]
                extra = {**extra, "run": run_name}
            else:
                cmd = [sys.executable, str(self.root / "tools" / "数字审核" / "serve.py"), "--port", str(port), "--lan"]

            self.bus.publish({"source": "gui", "step": f"service_{task_id}", "status": "running", "message": f"正在启动 {task_id} 服务 (端口 {port})..."})
            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._utf8_env(),
                creationflags=creation,
            )
            self.procs[task_id] = {"proc": proc, "port": port, "cmd": cmd, "extra": extra, "started_at": core.now_str()}

        # 等待端口监听就绪（最多 8 秒）
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self._port_open(port, 0.25):
                self.bus.publish({"source": "gui", "step": f"service_{task_id}", "status": "done", "message": f"{task_id} 审核服务已就绪 (端口 {port})"})
                return {"ok": True, "task_id": task_id, "port": port, "already_running": False}
            if proc.poll() is not None:
                raise RuntimeError(f"审核服务启动后立即退出（退出码 {proc.returncode}），请确认数据目录是否完整")
            time.sleep(0.2)
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
        self.bus.publish({"source": "gui", "step": f"service_{task_id}", "status": "stopped", "message": f"已停止 {task_id} 服务"})
        return {"ok": True, "task_id": task_id}

    def _read_run_stats(self, rec_dir: Path) -> dict | None:
        """读取识别目录的 results.json 摘要（用于结果级健康判定与工作台展示）。"""
        data = core.read_json(rec_dir / "results.json")
        if not isinstance(data, dict):
            return None
        pages = data.get("pages") or []
        stats = dict(data.get("stats") or {})
        if "pages" not in stats:
            stats["pages"] = len(pages)
        if "page_failures" not in stats:
            stats["page_failures"] = sum(bool(p.get("error")) for p in pages)
        failed_items = sum(
            1
            for p in pages
            for it in (p.get("items") or [])
            if it.get("kind") in ("fill", "identity")
            and it.get("status") in ("调用失败", "结构异常", "等待视觉转录", "等待豆包")
        )
        return {
            "status": str(data.get("status") or ""),
            "model": str(data.get("model") or ""),
            "returned_model": data.get("returned_model"),
            "provider_label": str(data.get("provider_label") or ""),
            "settings": data.get("settings") or {},
            "stats": stats,
            "failed_items": failed_items,
            "has_error_log": (rec_dir / "logs" / "errors.log").is_file(),
        }

    def start_pipeline(self, batch_id: str | None = None) -> dict:
        diag_mod = core._load_diag_module(self.root)
        with self._lock:
            if self.pipeline_job["running"]:
                return {"ok": False, "error": "已有自动识别任务正在运行，请等待完成"}
            active = core.set_active_batch(batch_id, self.batches_root) if batch_id else core.get_active_batch(self.batches_root)
            if not active:
                return {"ok": False, "error": "请先导入一批试卷照片"}

        # 启动前强校验：依赖（含真实 import）、模型权重、有效 API Key
        rep = core.check_environment(self.root, self.batches_root, deep=True)
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
                    "m1_boxes": 0,
                    "m2_boxes": 0,
                    "m3_boxes": 0,
                    "ocr_numbers": 0,
                    "choice_items": 0,
                    "fill_items": 0,
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
                str(self.root / "pipeline" / "run.py"),
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
                assert proc.stdout is not None
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
                        if now_m - last_sse_ts >= 1.2:
                            last_sse_ts = now_m
                            p_done = parsed.get("pages_done", 0)
                            p_tot = parsed.get("pages_total", 0)
                            a_ok = parsed.get("api_success", 0)
                            a_tot = parsed.get("grids_total", 0)
                            self.bus.publish({
                                "source": "gui",
                                "step": "recognize",
                                "status": "running",
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
                exit_info = diag_mod.describe_exit_code(rc)
                run_summary = self._read_run_stats(Path(active["recognize_dir"]))
                st = (run_summary or {}).get("stats") or {}
                pages_n = int(st.get("pages") or 0)
                page_fail = int(st.get("page_failures") or 0)
                api_fail = int(st.get("api_failures") or 0)
                failed_items = int((run_summary or {}).get("failed_items") or 0)
                all_failed = pages_n > 0 and page_fail >= pages_n

                if rc != 0 or all_failed:
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
                    if last_error_diag:
                        self.pipeline_job["error_report"] = last_error_diag
                    self.pipeline_job["logs"].append(f"[结束] 退出码 {rc}（{exit_info['title']}）· {msg}")
                self.bus.publish({"source": "gui", "step": "recognize", "status": "done" if final_status == "done" else ("error" if final_status == "error" else "running"), "message": msg})
            except Exception as exc:
                diag = diag_mod.diagnose_exception(exc, default_module="工作台任务调度")
                with self._lock:
                    self.pipeline_job["running"] = False
                    self.pipeline_job["status"] = "error"
                    self.pipeline_job["returncode"] = diag.get("exit_code", 1)
                    self.pipeline_job["exit_info"] = diag_mod.describe_exit_code(diag.get("exit_code", 1))
                    self.pipeline_job["error_report"] = diag
                    self.pipeline_job["logs"].append(f"[异常·{diag['module']}] {diag['title']}：{diag['reason']}")
                self.bus.publish({"source": "gui", "step": "recognize", "status": "error", "message": f"启动识别失败：{diag['title']}"})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "batch_id": active["batch_id"]}

    def start_retry_failed(self, batch_id: str | None = None) -> dict:
        """在后台仅补跑当前批次失败或结构异常的图块（不重复扣费）。"""
        diag_mod = core._load_diag_module(self.root)
        with self._lock:
            if self.pipeline_job["running"]:
                return {"ok": False, "error": "当前已有识别或补跑任务正在运行，请稍候"}
            active = core.set_active_batch(batch_id, self.batches_root) if batch_id else core.get_active_batch(self.batches_root)
            if not active:
                return {"ok": False, "error": "还没有导入批次"}
            rec_dir = Path(active["recognize_dir"])
            if not (rec_dir / "results.json").is_file():
                return {"ok": False, "error": "当前批次尚无识别结果，请先运行自动识别"}
            infer_cfg = core.get_infer_settings(self.batches_root)
            self.pipeline_job.update({
                "running": True,
                "kind": "retry",
                "batch_id": active["batch_id"],
                "status": "running",
                "returncode": None,
                "exit_info": None,
                "error_report": None,
                "logs": [f"[{core.now_str()}] 启动失败图块补跑（仅重跑失败项，不重复扣费）：{active['batch_id']}"],
            })

        def worker():
            import importlib.util
            self.bus.publish({"source": "gui", "step": "recognize", "status": "running", "message": "正在补跑失败图块（不重复扣费）..."})
            try:
                spec = importlib.util.spec_from_file_location("_retry_failed", str(self.root / "tools" / "补跑失败图块.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                def cb(msg: str):
                    with self._lock:
                        self.pipeline_job["logs"].append(f"[补跑] {msg}")
                        if len(self.pipeline_job["logs"]) > 200:
                            self.pipeline_job["logs"] = self.pipeline_job["logs"][-200:]
                    self.bus.publish({"source": "gui", "step": "recognize", "status": "running", "message": msg})

                res = mod.retry_failed_grids(rec_dir, concurrency=infer_cfg["api_concurrency"], progress_cb=cb)
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
                self.bus.publish({"source": "gui", "step": "recognize", "status": "done" if ok else "error", "message": res.get("message") or res.get("error") or "补跑结束"})
            except Exception as exc:
                diag = diag_mod.diagnose_exception(exc, default_module="补跑失败图块")
                with self._lock:
                    self.pipeline_job["running"] = False
                    self.pipeline_job["status"] = "error"
                    self.pipeline_job["returncode"] = diag.get("exit_code", 1)
                    self.pipeline_job["exit_info"] = diag_mod.describe_exit_code(diag.get("exit_code", 1))
                    self.pipeline_job["error_report"] = diag
                self.bus.publish({"source": "gui", "step": "recognize", "status": "error", "message": f"补跑失败：{diag['title']}"})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "batch_id": active["batch_id"]}

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
                spec = importlib.util.spec_from_file_location("_perf_bench", str(self.root / "tools" / "性能压测.py"))
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

    def shutdown_all(self) -> None:
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


def pick_windows_folder() -> str:
    """弹出 Windows 原生文件夹选择对话框（不依赖 tkinter）。"""
    if os.name != "nt":
        return ""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '请选择存放新试卷照片的文件夹'; "
        "$f.ShowNewFolderButton = $false; "
        "$top = New-Object System.Windows.Forms.Form; $top.TopMost = $true; "
        "if ($f.ShowDialog($top) -eq 'OK') { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Output $f.SelectedPath }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return (proc.stdout or "").strip()


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
                    while True:
                        try:
                            ev = q.get(timeout=15)
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
                    page_name = str(body.get("page_name") or "").strip()
                    stem = Path(page_name).stem if page_name else ""
                    if not stem:
                        raise ValueError("缺少试卷文件名")
                    m1_imgs = state.root / "data" / "m1" / "images"
                    m1_imgs.mkdir(parents=True, exist_ok=True)
                    target = m1_imgs / f"{stem}.jpg"
                    if not target.is_file():
                        # 按需从当前活跃批次复制一份（非破坏性），确保单张一定可编辑
                        active = core.get_active_batch(state.batches_root)
                        if active:
                            src_dir = Path(active["images_dir"])
                            for cand in (src_dir / page_name, src_dir / f"{stem}.jpg"):
                                if cand.is_file():
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
                self._send_json(200, {
                    "ok": True,
                    "config": cfg,
                    "infer_settings": core.get_infer_settings(state.batches_root),
                })
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

            if path == "/api/repair/env":
                key = str(body.get("key") or "ARK_API_KEY")
                val = str(body.get("value") or "")
                res = core.save_env_key(key, val, root=state.root)
                state.bus.publish({"source": "gui", "step": "check", "status": "done" if res["ok"] else "error", "message": res.get("message") or res.get("error") or ""})
                self._send_json(200 if res["ok"] else 400, res)
                return

            if path == "/api/repair/agent":
                rep = state.last_check or core.check_environment(state.root, state.batches_root)
                note = str(body.get("note") or "")
                prompt = core.build_agent_prompt(rep, root=state.root, extra_note=note)
                res = core.launch_opencode(state.root, prompt, batches_root=state.batches_root)
                state.bus.publish({"source": "gui", "step": "agent", "status": "done", "message": res["message"]})
                self._send_json(200, res)
                return

            if path == "/api/pick-folder":
                try:
                    folder = pick_windows_folder()
                    self._send_json(200, {"ok": True, "path": folder})
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
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
                state.bus.publish({"source": "gui", "step": "overview", "status": "done", "message": f"已切换到批次：{bid}"})
                self._send_json(200, {"ok": True, "batch": meta})
                return

            if path == "/api/pipeline/run":
                res = state.start_pipeline(body.get("batch_id"))
                self._send_json(200 if res.get("ok") else 400, res)
                return

            if path == "/api/pipeline/retry-failed":
                res = state.start_retry_failed(body.get("batch_id"))
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
