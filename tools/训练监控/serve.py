# -*- coding: utf-8 -*-
"""训练监控 —— 本地服务

自动找训练目录下最新的 results.csv（默认 自动改卷/train/），供网页实时显示：
进度、loss、val 指标、GPU 利用率/显存/功耗/温度、python 进程数。

关键：读 csv 用 Python 的 open()（共享读、不阻止写入），绝不干扰训练。

用法：
    python serve.py                  # 扫 自动改卷/train/
    python serve.py --dir <目录>     # 扫指定目录（看外部历史 run 用）
    python serve.py --port 8902
"""
import argparse
import glob
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))          # tools/训练监控/
PROJ = os.path.dirname(os.path.dirname(ROOT))              # 自动改卷/
import sys
from pathlib import Path
sys.path.insert(0, os.path.join(PROJ, 'pipeline'))
from common.records import read_json, read_events, now
from common.diagnostics import diagnose
PINNED_RUN = None
PORT = 8901
SCAN_DIR = os.path.join(PROJ, "train")
CACHE = {"t": 0.0, "gpu": {}, "py": -1}
CACHE_LOCK = threading.Lock()


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def find_latest_run():
    """优先跟随 active.json / --run 指定的训练；指针失效时回退到最新一份"""
    active = read_json(Path(SCAN_DIR) / 'active.json', {})
    chosen = PINNED_RUN or (active.get('run') if isinstance(active, dict) else None)
    if chosen:
        candidate = Path(chosen).resolve()
        if not candidate.is_relative_to(Path(SCAN_DIR).resolve()):
            raise ValueError('监控运行必须位于扫描目录内')
        # run 目录被删、还没产出指标、或 results.csv 读不了时，不要让整个页面挂掉
        if (candidate / 'results.csv').exists():
            return str(candidate / 'results.csv')
    cands = glob.glob(os.path.join(SCAN_DIR, "**", "results.csv"), recursive=True)
    if not cands:
        return None
    now = time.time()
    active = [c for c in cands if now - _mtime(c) < 300]
    return max(active or cands, key=_mtime)


def read_rows(path):
    """共享读，不锁文件。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [l.strip() for l in fh if l.strip()]
    except OSError:
        return []
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == len(header):
            rows.append(dict(zip(header, parts)))
    return rows


def _num(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def gpu_state():
    """单个字段是 [N/A] 时只丢这个字段，不让三张 GPU 卡片一起变 —。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            encoding="utf-8", errors="replace").stdout.strip()
        p = [x.strip() for x in out.split(",")]
        if len(p) < 5:
            return {}
        keys = ("util", "mem_used", "mem_total", "power", "temp")
        vals = [_num(x) for x in p[:5]]
        return {k: v for k, v in zip(keys, vals) if v is not None}
    except Exception:
        return {}


def py_count():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=6, errors="replace").stdout
        return sum(1 for l in out.splitlines() if "python.exe" in l.lower())
    except Exception:
        return -1


def cached_probe():
    """GPU/进程查询缓存 2 秒，别每 1.5 秒拉起两个子进程。"""
    with CACHE_LOCK:
        if time.time() - CACHE["t"] < 2:
            return CACHE
        CACHE["gpu"] = gpu_state()
        CACHE["py"] = py_count()
        CACHE["t"] = time.time()
        return CACHE


def read_epochs(csv_path):
    """从训练目录的 args.yaml 读总轮数（供进度条/ETA 用）"""
    y = os.path.join(os.path.dirname(csv_path), "args.yaml")
    try:
        with open(y, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("epochs:"):
                    return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return 0


def scan_health(run_dir, rows):
    """扫 NaN / 坏梯度：CSV 里的 nan、train.log 的恢复与跳过、grad_guard.log。

    返回结构给前端直接渲染：哪几轮 nan、几次坏梯度、最近事件列表。
    """
    h = {"nan_epochs": [], "nan_cells": 0, "guard_skips": 0,
         "recoveries": 0, "events": [], "bad": False, "first_bad_epoch": 0}
    seen = set()

    def add(epoch, typ, text):
        key = (epoch, typ, text)
        if key in seen:
            return
        seen.add(key)
        h["events"].append({"epoch": epoch, "type": typ, "text": text})

    # 1) results.csv 里的 nan/inf
    for i, r in enumerate(rows):
        bad_cols = [k for k, v in r.items()
                    if str(v).strip().lower() in ("nan", "inf", "-inf", "-nan", "infinity")]
        if bad_cols:
            ep = i + 1
            h["nan_epochs"].append(ep)
            h["nan_cells"] += len(bad_cols)
            add(ep, "val", "第 %d 轮验证出现 nan：%s" % (ep, ", ".join(c.split("/")[-1] for c in bad_cols[:4])))

    # 2) train.log：恢复循环 / 守护跳过 / 进度行里的 nan loss
    log = os.path.join(run_dir, "train.log")
    if os.path.exists(log):
        cur_ep = 0
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    s = line.strip()
                    m = re.match(r"(\d+)/(\d+)\s", s)
                    if m:
                        cur_ep = int(m.group(1))
                    if "Loss NaN" in s or "NaN/Inf" in s:
                        h["recoveries"] += 1
                        add(cur_ep, "nan", "NaN 恢复：%s" % s[:90])
                    elif "非有限梯度" in s or "non-finite grad" in s.lower():
                        add(cur_ep, "guard", "坏梯度已跳过（%s）" % s[:80])
        except OSError:
            pass

    # 3) grad_guard.log：每行一条跳过记录
    gg = os.path.join(run_dir, "grad_guard.log")
    if os.path.exists(gg):
        try:
            lines = [l.strip() for l in open(gg, encoding="utf-8", errors="replace") if l.strip()]
            h["guard_skips"] = len(lines)
            for l in lines[-5:]:
                m = re.search(r"epoch=(\d+)", l)
                add(int(m.group(1)) if m else 0, "guard", "grad_guard: %s" % l[:80])
        except OSError:
            pass

    failures = read_events(Path(run_dir) / 'numerical_failure.jsonl')
    overflows = read_events(Path(run_dir) / 'amp_overflow.jsonl')
    for e in failures:
        add(e.get('epoch', 0), 'fatal', str(e.get('where')) + ': ' + str(e.get('details')))
    for e in overflows[-10:]:
        add(e.get('epoch', 0), 'amp', 'AMP溢出已跳步，scale ' + str(e.get('scale_before')) + ' → ' + str(e.get('scale_after')))
    h['fatal_count'] = len(failures)
    h['amp_overflows'] = len(overflows)
    h['events'] = h['events'][-30:]
    ep_list = [e for e in h["nan_epochs"]] + [e["epoch"] for e in h["events"] if e["epoch"] > 0]
    h["first_bad_epoch"] = min(ep_list) if ep_list else 0
    h["bad"] = bool(h["nan_epochs"] or h["recoveries"] or h["guard_skips"] or failures)
    return h


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# tqdm 进度行：`      12/120      2.7G     0.98 ... 1024: 77% ━━━ 201/260 8.3it/s 24.4s<7.1s`
# 抓 epoch、batch 进度、已用秒数、本轮到结束的剩余秒数
PROG_RE = re.compile(
    r"(\d+)/(\d+)\s+[\d.]+G.*?(\d+)/(\d+)\s+[\d.]+it/s\s+([\d.]+)s<([\d.]+)s")


def scan_live(run_dir):
    """解析 train.log 最后一条进度行，给「本轮剩余」用。"""
    log = os.path.join(run_dir, "train.log")
    try:
        size = os.path.getsize(log)
        with open(log, "rb") as fh:
            fh.seek(max(0, size - 65536))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return {}
    data = ANSI_RE.sub("", data)
    for line in reversed(re.split(r"[\r\n]+", data)):
        m = PROG_RE.search(line)
        if m:
            ep, eps, step, steps, elapsed, remain = m.groups()
            return {"epoch": int(ep), "epochs": int(eps),
                    "step": int(step), "steps": int(steps),
                    "elapsed": float(elapsed), "remain": float(remain),
                    "pct": round(100.0 * int(step) / max(1, int(steps)), 1)}
    return {}


def state():
    csv_path = find_latest_run()
    probe = cached_probe()
    gpu, py = probe["gpu"], probe["py"]
    now = time.time()
    if not csv_path:
        return {"ok": False,
                "msg": "还没有 results.csv：训练启动后第一轮验证结束才会写入",
                "scan": SCAN_DIR, "gpu": gpu, "py": py, "now": now}
    mtime = _mtime(csv_path)
    if not mtime and not os.path.isdir(os.path.dirname(csv_path)):
        return {"ok": False, "msg": "results.csv 正在重建，稍等…",
                "scan": SCAN_DIR, "gpu": gpu, "py": py, "now": now}
    rows = read_rows(csv_path)
    run_dir = os.path.dirname(csv_path)
    return {
        "ok": True,
        "diagnosis": diagnose(run_dir),
        "heartbeat": read_json(Path(run_dir) / 'heartbeat.json', {}),
        "run": os.path.relpath(run_dir, PROJ),
        "scan": SCAN_DIR,
        "csv_age": round(now - mtime, 1),
        "epochs": read_epochs(csv_path),
        "rows": rows,
        "health": scan_health(run_dir, rows),
        "live": scan_live(run_dir),
        "gpu": gpu,
        "py": py,
        "now": now,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            try:
                body = json.dumps(state(), ensure_ascii=False).encode("utf-8")
            except Exception as e:
                body = json.dumps({"ok": False, "msg": "监控内部错误：%s" % e},
                                  ensure_ascii=False).encode("utf-8")
            self._send(200, body)
        else:
            self._send(404, b"{}")


def main():
    global PORT, SCAN_DIR, PINNED_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(PROJ, "train"), help="results.csv 扫描根目录")
    ap.add_argument("--run", help="固定监控run，不猜最新CSV")
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    PINNED_RUN = args.run
    PORT = args.port
    SCAN_DIR = os.path.abspath(args.dir)

    print("=" * 56)
    print("训练监控: http://127.0.0.1:%d/" % PORT)
    print("扫描目录: %s" % SCAN_DIR)
    print("关掉本窗口即停止（只看不写，不影响训练）")
    print("=" * 56)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print("端口 %d 启动失败：%s" % (PORT, e))
        print("多半是上一个监控窗口还开着。关掉它，或换端口：python serve.py --port 8902")
        try:
            input("按回车退出…")
        except EOFError:
            pass
        return
    if not args.no_browser:
        from common.review_watch import open_edge
        open_edge("http://127.0.0.1:%d/" % PORT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
