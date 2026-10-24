# -*- coding: utf-8 -*-
"""方向确认工具 —— 本地服务

用途：逐张翻看 raw/ 里的整页图，判断每张的真实方向（整页图需要顺时针转多少度才正），
记录写入 data/orient/角度.json。

页面交互（浏览器打开 http://127.0.0.1:8899/）：
    R      视图顺时针转 90°
    空格   确认当前图（记录当前视图角度）并跳下一张
    ← →    上一张 / 下一张（不记录）
    X      标记异常（该图不参与方向训练）

角度语义：记录值 = 该图需要顺时针旋转的角度（0/90/180/270），-1 = 异常。
"""
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))            # tools/方向确认/
PROJ = os.path.dirname(os.path.dirname(ROOT))                # 自动改卷/
RAW = os.path.join(PROJ, "data", "raw")
DATA = os.path.join(PROJ, "data", "orient")
LABEL_FILE = os.path.join(DATA, "角度.json")
PORT = 8899
EXTS = (".jpg", ".jpeg", ".png", ".bmp")


def list_images():
    out = []
    for dirpath, _dirs, files in os.walk(RAW):
        for f in files:
            if f.lower().endswith(EXTS):
                rel = os.path.relpath(os.path.join(dirpath, f), RAW).replace("\\", "/")
                out.append(rel)
    out.sort()
    return out


def load_labels():
    if os.path.exists(LABEL_FILE):
        with open(LABEL_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_labels(d):
    os.makedirs(DATA, exist_ok=True)
    tmp = LABEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, LABEL_FILE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
        elif u.path == "/api/list":
            payload = {"files": list_images(), "labels": load_labels()}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/img":
            rel = unquote(parse_qs(u.query).get("f", [""])[0])
            full = os.path.abspath(os.path.join(RAW, rel))
            if not full.startswith(os.path.abspath(RAW)) or not os.path.isfile(full):
                self._send(404, b"{}")
                return
            with open(full, "rb") as fh:
                data = fh.read()
            low = full.lower()
            ctype = "image/jpeg" if low.endswith((".jpg", ".jpeg")) else ("image/bmp" if low.endswith(".bmp") else "image/png")
            self._send(200, data, ctype)
        else:
            self._send(404, b"{}")

    def do_POST(self):
        if urlparse(self.path).path != "/api/label":
            self._send(404, b"{}")
            return
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        labels = load_labels()
        labels[str(req["file"])] = int(req["angle"])
        save_labels(labels)
        body = {"ok": True, "done": len(labels), "total": len(list_images())}
        self._send(200, json.dumps(body).encode("utf-8"))


def main():
    if not os.path.isdir(RAW):
        print("找不到图片目录:", RAW)
        print("请确认 data/raw 已存在。")
        return 1
    os.makedirs(DATA, exist_ok=True)
    total = len(list_images())
    done = len(load_labels())
    print("=" * 56)
    print("方向确认工具")
    print("  图片目录:", RAW, "(%d 张)" % total)
    print("  标注文件:", LABEL_FILE, "(已标 %d)" % done)
    print("  地址: http://127.0.0.1:%d/" % PORT)
    print("  关闭本窗口即停止服务（标注是实时存盘的，不丢）")
    print("=" * 56)
    threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d/" % PORT)).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
