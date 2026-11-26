# -*- coding: utf-8 -*-
"""标注工具本地服务（中文事件日志版）。

标签事实源是 data/<m>/log/<图名>.log，一行一个中文事件：
  预标注 / 审核 / 特殊 / 撤销审核 / 旋转 / 调整框
progress.csv 只是投影缓存，可随时从日志重建。

改框在页面暂存，A/D或主动刷新时提交审核结果；子集名单由页面统一刷新。
"""
import os
import sys
import json
import argparse
import csv
import threading
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data")
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
from common import events
from common.lan_collab import ClaimManager, resolve_host, print_access_urls

STATE = {"data": os.path.join(DATA, "m1")}
PROG_LOCK = threading.RLock()
ROT_LOCK = threading.RLock()
CLAIMS = ClaimManager(ttl_seconds=300)
EDIT_CACHE = {}
SCOPE = None  # --subset只显示/编辑本批清单，避免跨轮误审核。


def edit_of(stem):
    """当前图相对预标注的改动量，进程内缓存。"""
    key = (mname(), stem)
    if key not in EDIT_CACHE:
        EDIT_CACHE[key] = events.edit_stats(mname(), stem)
    return EDIT_CACHE[key]


def invalidate(stem):
    EDIT_CACHE.pop((mname(), stem), None)


def mname():
    return os.path.basename(STATE["data"])


def log_dir():
    return os.path.join(STATE["data"], "log")


def images_dir():
    return os.path.join(STATE["data"], "images")


def rot_path():
    return os.path.join(STATE["data"], "rotation.json")


def load_rot():
    with ROT_LOCK:
        p = rot_path()
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                return {}
        return {}


def save_rot(d):
    with ROT_LOCK:
        p = rot_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)


def _claim_key(stem):
    return f"{mname()}::{stem}"


def _locks_for_current():
    prefix = f"{mname()}::"
    snap = CLAIMS.snapshot()
    return {k[len(prefix):]: v for k, v in snap.items() if k.startswith(prefix)}


def _candidate_stems():
    stems = []
    for f in sorted(os.listdir(images_dir())):
        if not f.lower().endswith(".jpg"):
            continue
        stem = os.path.splitext(f)[0]
        if SCOPE is not None and stem not in SCOPE:
            continue
        stems.append(stem)
    return stems


def sync_progress(stem):
    with PROG_LOCK:
        events.sync_progress(mname(), stem)


def api_stats():
    """轻量统计：顶部计数 + 工作量，供前端轮询。"""
    by = events.read_progress(mname())
    counts = {"correct": 0, "fixed": 0, "special": 0, "pending": 0}
    edits = {"moved": 0, "added": 0, "deleted": 0, "total": 0}
    for stem, pr in by.items():
        if SCOPE is not None and stem not in SCOPE:
            continue
        status = pr.get("status", "未标")
        if status == "特殊":
            counts["special"] += 1
        elif status.startswith("已审-"):
            counts["fixed" if status == "已审-修正" else "correct"] += 1
        else:
            counts["pending"] += 1
        e = edit_of(stem)
        for k in ("moved", "added", "deleted", "total"):
            edits[k] += e.get(k, 0)
    return {"counts": counts, "edits": edits}


def api_list():
    by = events.read_progress(mname())
    rot = load_rot()
    locks = _locks_for_current()
    items = []
    for f in sorted(os.listdir(images_dir())):
        if not f.lower().endswith(".jpg"):
            continue
        stem = os.path.splitext(f)[0]
        if SCOPE is not None and stem not in SCOPE:
            continue
        pr = by.get(stem, {})
        status = pr.get("status", "未标")
        lk = locks.get(stem)
        items.append({"name": f, "stem": stem,
                      "boxes": int(pr.get("boxes", "0") or 0),
                      "mtime": int(os.path.getmtime(os.path.join(images_dir(), f))),
                      "reviewed": status.startswith("已审-"),
                      "status": status,
                      "review_count": int(pr.get("review_count", "0") or 0),
                      "last_review": pr.get("last_review", ""),
                      "reviewer": pr.get("reviewer", ""),
                      "locked_by": lk["user"] if lk else "",
                      "locked_client": lk["client_id"] if lk else "",
                      "edit": edit_of(stem),
                      "rotation": int(rot.get(stem, 0) or 0),
                      "special": status == "特殊",
                      "note": pr.get("note", "")})
    counts = {"correct": 0, "fixed": 0, "special": 0, "pending": 0}
    edits = {"moved": 0, "added": 0, "deleted": 0, "total": 0}
    for x in items:
        if x["special"]:
            counts["special"] += 1
        elif x["reviewed"]:
            counts["fixed" if x["status"] == "已审-修正" else "correct"] += 1
        else:
            counts["pending"] += 1
        for k in ("moved", "added", "deleted", "total"):
            edits[k] += x["edit"].get(k, 0)
    return {"data": STATE["data"], "total": len(items),
            "reviewed": sum(1 for x in items if x["reviewed"]),
            "special": counts["special"], "counts": counts, "edits": edits,
            "locks": locks, "items": items}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=TOOL_DIR, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/datasets":
            out = []
            for d in sorted(os.listdir(DATA)):
                if SCOPE is not None and d != mname():
                    continue
                p = os.path.join(DATA, d)
                img = os.path.join(p, "images")
                if os.path.isdir(img) and any(f.lower().endswith(".jpg") for f in os.listdir(img)):
                    out.append({"name": d,
                                "current": os.path.abspath(p) == os.path.abspath(STATE["data"])})
            return self._json({"datasets": out, "current": os.path.basename(STATE["data"])})
        if u.path == "/api/list":
            return self._json(api_list())
        if u.path == "/api/stats":
            return self._json(api_stats())
        if u.path == "/api/locks":
            return self._json({"ok": True, "locks": _locks_for_current()})
        if u.path == "/api/label":
            q = parse_qs(u.query)
            stem = q.get("stem", [""])[0]
            if SCOPE is not None and stem not in SCOPE:
                return self._json({"error": "不在本批审核清单内"}, 403)
            st = events.load_state(mname(), stem)
            pred_event = next((e for e in reversed(events.read_events(mname(), stem))
                               if e.get("事件") == "预标注" or e.get("e") == "pred"), {})
            name = stem + ".jpg"
            if not os.path.isfile(os.path.join(images_dir(), name)):
                return self._json({"error": "图片不存在"}, 404)
            return self._json({"stem": stem, "file": name, "text": events.to_text(st["boxes"]),
                               "reviewed": st["reviewed"], "special": st["special"],
                               "review_count": st["review_count"],
                               "last_review": st["reviewed_at"],
                               "rotation": st["rotation"], "note": st["note"],
                               "prediction_model": pred_event.get("模型", pred_event.get("model", "未记录")),
                               "prediction_time": pred_event.get("时间", pred_event.get("t", ""))})
        if u.path.startswith("/img/"):
            name = os.path.basename(unquote(u.path[5:]))
            p = os.path.join(images_dir(), name)
            if not os.path.exists(p):
                self.send_error(404)
                return
            with open(p, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()

    def do_POST(self):
        try:
            self._do_post()
        except Exception as e:
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def _do_post(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        body = json.loads(raw) if raw.strip() else {}
        if u.path == "/api/claim":
            client_id = str(body.get("client_id") or "").strip()
            user = str(body.get("user") or "成员").strip() or "成员"
            exclude = [_claim_key(s) for s in (body.get("exclude") or [])]
            with PROG_LOCK:
                by = events.read_progress(mname())
                stems = _candidate_stems()
                candidates = [_claim_key(s) for s in stems]
                def is_done(k):
                    s = k.split("::", 1)[1]
                    st_str = (by.get(s) or {}).get("status", "未标")
                    return st_str == "特殊" or st_str.startswith("已审-")
                res = CLAIMS.claim_next(client_id, user, candidates, is_done, exclude=exclude)
            if res.get("key"):
                stem = res["key"].split("::", 1)[1]
                return self._json({"ok": True, "stem": stem, "user": res["user"],
                                   "ttl": res["ttl"],
                                   "remaining_pending": res.get("remaining_pending", 0),
                                   "locks": _locks_for_current()})
            return self._json({"ok": True, "stem": None, "reason": res.get("reason", "all_done"),
                               "remaining_pending": res.get("remaining_pending", 0),
                               "locked_by_others": res.get("locked_by_others", 0),
                               "locks": _locks_for_current()})
        if u.path == "/api/acquire":
            stem = str(body.get("stem") or "").strip()
            client_id = str(body.get("client_id") or "").strip()
            user = str(body.get("user") or "成员").strip() or "成员"
            force = bool(body.get("force", False))
            if SCOPE is not None and stem not in SCOPE:
                return self._json({"ok": False, "error": "不在本批审核清单内"}, 403)
            res = CLAIMS.acquire(_claim_key(stem), client_id, user, force=force)
            res["locks"] = _locks_for_current()
            return self._json(res, 200 if res.get("ok") else 409)
        if u.path == "/api/heartbeat":
            stem = str(body.get("stem") or "").strip()
            client_id = str(body.get("client_id") or "").strip()
            user = str(body.get("user") or "成员").strip() or "成员"
            res = CLAIMS.heartbeat(_claim_key(stem), client_id, user)
            res["locks"] = _locks_for_current()
            return self._json(res)
        if u.path == "/api/release":
            client_id = str(body.get("client_id") or "").strip()
            stem = body.get("stem")
            res = CLAIMS.release(client_id, _claim_key(stem) if stem else None)
            return self._json(res)
        if SCOPE is not None:
            if u.path == '/api/switch':
                return self._json({"ok": False, "error": "限定批次模式不能切换数据集"}, 403)
            if body.get('stem', '') not in SCOPE:
                return self._json({"ok": False, "error": "不在本批审核清单内"}, 403)
        if u.path == "/api/switch":
            name = body.get("name", "")
            p = os.path.abspath(os.path.join(DATA, name))
            if not p.startswith(os.path.abspath(DATA)) or not os.path.isdir(os.path.join(p, "images")):
                return self._json({"ok": False, "error": "无效目录"}, 400)
            STATE["data"] = p
            EDIT_CACHE.clear()
            if not os.path.exists(os.path.join(p, "progress.csv")):
                with PROG_LOCK:
                    events.rebuild_progress(mname())
            return self._json({"ok": True, "data": p})
        if u.path == "/api/review":
            stem = body.get("stem", "")
            boxes = events.from_text(body.get("text", ""))
            reviewer = str(body.get("reviewer") or "").strip()
            client_id = str(body.get("client_id") or "").strip()
            if not os.path.isfile(os.path.join(images_dir(), stem + ".jpg")):
                return self._json({"error": "图片不存在，无法审核"}, 409)
            ev_data = {"e": "review", "boxes": boxes}
            if reviewer:
                ev_data["reviewer"] = reviewer
            ev = events.append_event(mname(), stem, ev_data)
            sync_progress(stem)
            invalidate(stem)
            if client_id:
                CLAIMS.release(client_id, _claim_key(stem))
            st = events.load_state(mname(), stem)
            return self._json({"ok": True, "unchanged": ev is None,
                               "review_count": st["review_count"],
                               "last_review": st["reviewed_at"],
                               "reviewer": st.get("reviewer", ""),
                               "status": events.status_of(st, len(st["boxes"])),
                               "edit": edit_of(stem)})
        if u.path == "/api/special":
            stem = body.get("stem", "")
            note = str(body.get("note", "")).strip()
            reviewer = str(body.get("reviewer") or "").strip()
            client_id = str(body.get("client_id") or "").strip()
            if not note:
                return self._json({"error": "请填写特殊原因"}, 400)
            event = {"e": "special", "note": note}
            if reviewer:
                event["reviewer"] = reviewer
            if "text" in body:
                event["boxes"] = events.from_text(body["text"])
            events.append_event(mname(), stem, event)
            sync_progress(stem)
            invalidate(stem)
            if client_id:
                CLAIMS.release(client_id, _claim_key(stem))
            return self._json({"ok": True})
        if u.path == "/api/unreview":
            stem = body.get("stem", "")
            st = events.load_state(mname(), stem)
            events.append_event(mname(), stem, {"e": "unreview", "n": st["review_count"]})
            sync_progress(stem)
            invalidate(stem)
            return self._json({"ok": True})
        if u.path == "/api/rotate":
            stem = body.get("stem", "")
            deg = int(body.get("rotation", 0)) % 360
            events.append_event(mname(), stem, {"e": "rotate", "deg": deg})
            rot = load_rot()
            rot[stem] = deg
            save_rot(rot)
            invalidate(stem)
            return self._json({"ok": True, "rotation": deg})
        self.send_error(404)


def main():
    global SCOPE, CLAIMS
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(DATA, "m1"))
    ap.add_argument("--port", type=int, default=8910)
    ap.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1；多人局域网协作传 --lan 或 --host 0.0.0.0")
    ap.add_argument("--lan", action="store_true", help="开启局域网监听（等价于 --host 0.0.0.0），供同一路由器下另一台电脑访问")
    ap.add_argument("--lock-ttl", type=int, default=300, help="多人协作单图锁定超时秒数，默认 300 秒")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument('--subset', help='只审核指定CSV的new_name列，不混入其他阶段或测试页')
    args = ap.parse_args()
    CLAIMS = ClaimManager(ttl_seconds=args.lock_ttl)
    STATE["data"] = os.path.abspath(args.data)
    if args.subset:
        with open(args.subset, encoding='utf-8-sig', newline='') as f:
            SCOPE = {row['new_name'] for row in csv.DictReader(f)}
        if not SCOPE or any(not os.path.isfile(os.path.join(images_dir(), name+'.jpg')) for name in SCOPE):
            raise ValueError('审核清单为空或有图片未入库')
        print('限定审核:', len(SCOPE), '张；清单:', args.subset)
    if not os.path.isdir(os.path.join(STATE["data"], "images")):
        print("找不到 images 目录:", STATE["data"])
        return 1
    if not os.path.exists(os.path.join(STATE["data"], "progress.csv")):
        events.rebuild_progress(mname())
    host = resolve_host(args.host, args.lan)
    url = "http://127.0.0.1:%d/" % args.port
    print("数据目录:", STATE["data"])
    print("日志目录:", log_dir())
    print_access_urls(host, args.port, label="标注工具")
    if not args.no_browser:
        webbrowser.open(url)
    srv = ThreadingHTTPServer((host, args.port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
