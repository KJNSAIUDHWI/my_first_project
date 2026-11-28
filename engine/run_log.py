# -*- coding: utf-8 -*-
"""统一运行事件日志（append-only JSONL + run_id + 隐私脱敏 + 历史查询）。

设计目标：
1. 每次识别/补跑分配独立 run_id，落盘到 <rec_dir>/runs/<run_id>/{meta.json, events.jsonl, summary.json}；
2. 线程安全追加 + Windows 并发重试，绝不静默丢日志；
3. 自动脱敏 API Key 与默认脱敏学生 PII（姓名/学号）；
4. 提供 CLI/AI 操作日志落盘（batches/_logs/cli-YYYYMMDD.jsonl）与统一查询函数。
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ_CN = timezone(timedelta(hours=8))
_LOG_LOCK = threading.RLock()

_SECRET_RE = re.compile(
    r"(?i)(Bearer\s+[A-Za-z0-9._\-]{8,}|(?:sk|ark)-[A-Za-z0-9._\-]{8,}|"
    r"(?:api[_-]?key|token|password|secret)\s*[=:]\s*['\"]?[^\s'\",;}{]{6,}['\"]?)"
)


def now_iso() -> str:
    """返回统一带 +08:00 偏移的 ISO8601 时间戳。"""
    return datetime.now(TZ_CN).isoformat(timespec="milliseconds")


def make_run_id(kind: str = "recognize") -> str:
    """生成带时间戳与短随机后缀的唯一 run_id。"""
    short = {"recognize": "rec", "retry": "rty", "ai": "ai", "bench": "bnc"}.get(kind, kind[:4] or "run")
    stamp = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{short}_{uuid.uuid4().hex[:6]}"


def mask_secret(text: str) -> str:
    """脱敏字符串中可能出现的 API Key / Bearer Token / 密码片段。"""
    if not text:
        return ""

    def _repl(m: re.Match) -> str:
        raw = m.group(0)
        if len(raw) <= 10:
            return "[REDACTED]"
        if raw.lower().startswith("bearer "):
            tok = raw[7:].strip()
            return f"Bearer {tok[:4]}****{tok[-4:]}" if len(tok) >= 10 else "Bearer [REDACTED]"
        if raw.lower().startswith(("ark-", "sk-")):
            return f"{raw[:4]}****{raw[-4:]}"
        if "=" in raw:
            k, _ = raw.split("=", 1)
            return f"{k}=[REDACTED]"
        if ":" in raw:
            k, _ = raw.split(":", 1)
            return f"{k}: [REDACTED]"
        return "[REDACTED]"

    return _SECRET_RE.sub(_repl, str(text))


def sanitize_value(val: Any, *, mask_pii: bool = True) -> Any:
    """递归脱敏任意 JSON 结构中的密钥（及可选的学生姓名/学号 PII 字段）。"""
    if isinstance(val, str):
        return mask_secret(val)
    if isinstance(val, list):
        return [sanitize_value(x, mask_pii=mask_pii) for x in val]
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            kl = str(k).lower()
            if any(s in kl for s in ("api_key", "apikey", "password", "secret", "authorization")):
                if isinstance(v, str) and len(v) >= 8:
                    out[k] = f"{v[:4]}****{v[-4:]}"
                elif v:
                    out[k] = "[REDACTED]"
                else:
                    out[k] = ""
            elif mask_pii and kl in ("student_name", "sid", "raw_text") and isinstance(v, str) and v:
                out[k] = "[PII_MASKED]"
            elif mask_pii and kl == "name" and "sid" in {str(x).lower() for x in val.keys()} and isinstance(v, str) and v:
                out[k] = "[PII_MASKED]"
            else:
                out[k] = sanitize_value(v, mask_pii=mask_pii)
        return out
    return val


def safe_append_line(path: Path, line: str, retries: int = 20, delay: float = 0.02) -> bool:
    """线程安全 + 重试追加单行文本；彻底失败时向 stderr 告警而不是静默忽略。"""
    path = Path(path)
    text = line if line.endswith("\n") else line + "\n"
    last_exc: Exception | None = None
    with _LOG_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[WARN] 创建日志目录失败 ({path.parent}): {exc}", file=sys.stderr)
            return False
        for attempt in range(max(1, int(retries))):
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                return True
            except OSError as exc:
                last_exc = exc
                if attempt + 1 < retries:
                    time.sleep(delay)
    print(f"[WARN] 追加日志失败 ({path}): {last_exc}", file=sys.stderr)
    return False


def _atomic_write_json(path: Path, data: Any, retries: int = 40, delay: float = 0.03) -> bool:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[WARN] 创建目录失败 ({path.parent}): {exc}", file=sys.stderr)
        return False
    tmp = path.with_suffix(f"{path.suffix}.writing.{os.getpid()}.{threading.get_ident()}")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
    except OSError as exc:
        print(f"[WARN] 写入临时文件失败 ({tmp}): {exc}", file=sys.stderr)
        return False
    last_exc: Exception | None = None
    for attempt in range(max(1, int(retries))):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(delay)
        except OSError as exc:
            last_exc = exc
            break
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    print(f"[WARN] 原子写入失败 ({path}): {last_exc}", file=sys.stderr)
    return False


def collect_runtime_versions() -> dict[str, str]:
    """采集当前运行的环境版本元数据（供 meta.json 与复现排查）。"""
    out: dict[str, str] = {
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "host": socket.gethostname(),
    }
    for mod_name in ("torch", "ultralytics", "cv2", "paddleocr", "onnxruntime"):
        try:
            mod = __import__(mod_name)
            out[mod_name] = str(getattr(mod, "__version__", "") or "")
        except Exception:
            out[mod_name] = ""
    try:
        import torch  # type: ignore
        out["cuda"] = str(getattr(torch.version, "cuda", "") or "")
        out["cuda_available"] = "true" if torch.cuda.is_available() else "false"
    except Exception:
        out["cuda"] = ""
        out["cuda_available"] = "false"
    return out


class RunLogger:
    """单次识别或补跑运行的结构化事件日志器。"""

    def __init__(
        self,
        rec_dir: Path | str,
        *,
        kind: str = "recognize",
        batch_id: str = "",
        run_id: str | None = None,
        actor: str = "system",
        source: str = "recognizer",
        meta_extra: dict[str, Any] | None = None,
    ):
        self.rec_dir = Path(rec_dir)
        self.kind = kind
        self.batch_id = batch_id or self.rec_dir.parent.name
        self.run_id = run_id or make_run_id(kind)
        self.actor = actor
        self.source = source
        self.started_at = now_iso()
        self._start_mono = time.monotonic()

        self.run_dir = self.rec_dir / "runs" / self.run_id
        self.meta_path = self.run_dir / "meta.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.summary_path = self.run_dir / "summary.json"

        meta = {
            "schema": 1,
            "run_id": self.run_id,
            "kind": self.kind,
            "batch_id": self.batch_id,
            "source": self.source,
            "actor": self.actor,
            "started_at": self.started_at,
            "finished_at": None,
            "status": "running",
            "cwd": str(Path.cwd()),
            "runtime": collect_runtime_versions(),
            **(sanitize_value(meta_extra or {}, mask_pii=True)),
        }
        self._meta = meta
        _atomic_write_json(self.meta_path, meta)

    def emit(
        self,
        event: str,
        message: str = "",
        *,
        level: str = "INFO",
        status: str = "ok",
        module: str = "",
        page_id: str = "",
        grid_id: str = "",
        item_id: str = "",
        attempt_id: str = "",
        duration_ms: float | None = None,
        error_code: str = "",
        detail: dict[str, Any] | None = None,
        source: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": now_iso(),
            "schema": 1,
            "level": str(level or "INFO").upper(),
            "event": str(event),
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "source": source or self.source,
            "actor": actor or self.actor,
            "module": module or self.source,
            "status": status,
            "message": mask_secret(message),
        }
        if attempt_id:
            rec["attempt_id"] = attempt_id
        if page_id:
            rec["page_id"] = page_id
        if grid_id:
            rec["grid_id"] = grid_id
        if item_id:
            rec["item_id"] = item_id
        if duration_ms is not None:
            rec["duration_ms"] = round(float(duration_ms), 2)
        if error_code:
            rec["error_code"] = str(error_code)
        if detail:
            rec["detail"] = sanitize_value(detail, mask_pii=True)
        safe_append_line(self.events_path, json.dumps(rec, ensure_ascii=False))
        return rec

    def finish(
        self,
        status: str = "ok",
        *,
        stats: dict[str, Any] | None = None,
        summary_extra: dict[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        finished_at = now_iso()
        dur_ms = round((time.monotonic() - self._start_mono) * 1000.0, 1)
        summary = {
            "schema": 1,
            "run_id": self.run_id,
            "kind": self.kind,
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "duration_ms": dur_ms,
            "status": status,
            "message": mask_secret(message),
            "stats": sanitize_value(stats or {}, mask_pii=True),
            **(sanitize_value(summary_extra or {}, mask_pii=True)),
        }
        self._meta["finished_at"] = finished_at
        self._meta["status"] = status
        self._meta["duration_ms"] = dur_ms
        _atomic_write_json(self.meta_path, self._meta)
        _atomic_write_json(self.summary_path, summary)
        self.emit(
            "run.end",
            message or f"运行结束：{status}",
            level="ERROR" if status in ("error", "fail", "failed") else ("WARN" if status == "warn" else "INFO"),
            status=status,
            duration_ms=dur_ms,
            detail={"stats_keys": sorted(list((stats or {}).keys()))[:24]},
        )
        return summary


def append_cli_log(
    batches_root: Path | str,
    *,
    command: str,
    argv: list[str] | None = None,
    source: str = "cli",
    actor: str = "user",
    exit_code: int = 0,
    duration_ms: float = 0.0,
    ok: bool = True,
    session_id: str = "",
    batch_id: str = "",
    message: str = "",
    detail: dict[str, Any] | None = None,
) -> Path:
    """将 CLI / AI 帮手操作持久化到 batches/_logs/cli-YYYYMMDD.jsonl（自动脱敏）。"""
    day = datetime.now(TZ_CN).strftime("%Y%m%d")
    log_path = Path(batches_root) / "_logs" / f"cli-{day}.jsonl"
    rec = {
        "ts": now_iso(),
        "schema": 1,
        "level": "INFO" if ok else "ERROR",
        "event": f"{source}.{command}",
        "source": source,
        "actor": actor,
        "command": command,
        "argv": [mask_secret(a) for a in (argv or [])],
        "ok": bool(ok),
        "exit_code": int(exit_code),
        "duration_ms": round(float(duration_ms), 2),
        "session_id": str(session_id or ""),
        "batch_id": str(batch_id or ""),
        "message": mask_secret(message),
        "detail": sanitize_value(detail or {}, mask_pii=True),
    }
    safe_append_line(log_path, json.dumps(rec, ensure_ascii=False))
    return log_path


def list_runs(batches_root: Path | str, batch_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """列出一个或全部批次的历史 run 记录（按开始时间倒序）。"""
    broot = Path(batches_root)
    if not broot.is_dir():
        return []
    targets: list[Path] = []
    if batch_id:
        cand = broot / batch_id / "recognized" / "runs"
        if cand.is_dir():
            targets.append(cand)
    else:
        for bdir in sorted(broot.iterdir()):
            if not bdir.is_dir() or bdir.name.startswith("_"):
                continue
            rdir = bdir / "recognized" / "runs"
            if rdir.is_dir():
                targets.append(rdir)
    items: list[dict[str, Any]] = []
    for rdir in targets:
        for child in rdir.iterdir():
            if not child.is_dir():
                continue
            meta_p = child / "meta.json"
            sum_p = child / "summary.json"
            meta: dict[str, Any] = {}
            summary: dict[str, Any] = {}
            if meta_p.is_file():
                try:
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if sum_p.is_file():
                try:
                    summary = json.loads(sum_p.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if not meta and not summary:
                continue
            row = {
                "run_id": meta.get("run_id") or summary.get("run_id") or child.name,
                "kind": meta.get("kind") or summary.get("kind") or "recognize",
                "batch_id": meta.get("batch_id") or summary.get("batch_id") or rdir.parent.parent.name,
                "started_at": meta.get("started_at") or summary.get("started_at") or "",
                "finished_at": summary.get("finished_at") or meta.get("finished_at") or "",
                "duration_ms": summary.get("duration_ms") or meta.get("duration_ms") or 0,
                "status": summary.get("status") or meta.get("status") or "unknown",
                "message": summary.get("message") or "",
                "stats": summary.get("stats") or {},
                "has_events": (child / "events.jsonl").is_file(),
            }
            items.append(row)
    items.sort(key=lambda x: str(x.get("started_at") or x.get("run_id")), reverse=True)
    return items[: max(1, int(limit))]


def get_run_detail(batches_root: Path | str, run_id: str, batch_id: str | None = None) -> dict[str, Any] | None:
    """按 run_id 读取单个 run 的 meta + summary。"""
    broot = Path(batches_root)
    if not broot.is_dir() or not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    candidates: list[Path] = []
    if batch_id:
        candidates.append(broot / batch_id / "recognized" / "runs" / run_id)
    else:
        for bdir in broot.iterdir():
            if bdir.is_dir() and not bdir.name.startswith("_"):
                candidates.append(bdir / "recognized" / "runs" / run_id)
    for rdir in candidates:
        if rdir.is_dir():
            meta = json.loads((rdir / "meta.json").read_text(encoding="utf-8")) if (rdir / "meta.json").is_file() else {}
            summary = json.loads((rdir / "summary.json").read_text(encoding="utf-8")) if (rdir / "summary.json").is_file() else {}
            ev_count = 0
            ev_p = rdir / "events.jsonl"
            if ev_p.is_file():
                try:
                    with ev_p.open("r", encoding="utf-8", errors="ignore") as fh:
                        ev_count = sum(1 for _ in fh)
                except OSError:
                    pass
            return {
                "run_id": run_id,
                "path": str(rdir),
                "meta": meta,
                "summary": summary,
                "event_count": ev_count,
            }
    return None


def query_events(
    batches_root: Path | str,
    *,
    run_id: str = "",
    batch_id: str = "",
    level: str = "",
    event: str = "",
    page_id: str = "",
    grid_id: str = "",
    q: str = "",
    include_cli: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """跨 run events.jsonl 与 cli-*.jsonl 统一过滤查询（返回最新优先或按时间排序）。"""
    broot = Path(batches_root)
    files: list[Path] = []
    if broot.is_dir():
        if run_id:
            det = get_run_detail(broot, run_id, batch_id=batch_id or None)
            if det:
                ep = Path(det["path"]) / "events.jsonl"
                if ep.is_file():
                    files.append(ep)
        else:
            if batch_id:
                rroot = broot / batch_id / "recognized" / "runs"
                if rroot.is_dir():
                    files.extend(sorted(rroot.glob("*/events.jsonl"), reverse=True))
            else:
                for bdir in sorted(broot.iterdir(), reverse=True):
                    if bdir.is_dir() and not bdir.name.startswith("_"):
                        rroot = bdir / "recognized" / "runs"
                        if rroot.is_dir():
                            files.extend(sorted(rroot.glob("*/events.jsonl"), reverse=True))
            if include_cli:
                ldir = broot / "_logs"
                if ldir.is_dir():
                    files.extend(sorted(ldir.glob("cli-*.jsonl"), reverse=True))

    lvl_u = level.strip().upper()
    ev_f = event.strip().lower()
    page_f = page_id.strip()
    grid_f = grid_id.strip()
    q_low = q.strip().lower()

    matched: list[dict[str, Any]] = []
    for fpath in files:
        try:
            for raw in fpath.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if run_id and str(rec.get("run_id") or "") != run_id:
                    continue
                if batch_id and str(rec.get("batch_id") or "") != batch_id:
                    continue
                if lvl_u and str(rec.get("level") or "").upper() != lvl_u:
                    continue
                if ev_f and ev_f not in str(rec.get("event") or "").lower():
                    continue
                if page_f and str(rec.get("page_id") or "") != page_f:
                    continue
                if grid_f and str(rec.get("grid_id") or "") != grid_f:
                    continue
                if q_low:
                    blob = json.dumps(rec, ensure_ascii=False).lower()
                    if q_low not in blob:
                        continue
                matched.append(rec)
        except OSError:
            continue

    matched.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    total = len(matched)
    off = max(0, int(offset))
    lim = max(1, min(1000, int(limit)))
    return {
        "total": total,
        "offset": off,
        "limit": lim,
        "items": matched[off : off + lim],
    }
