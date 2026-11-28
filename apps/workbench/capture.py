# -*- coding: utf-8 -*-
"""后台离屏无遮挡截图与元素坐标导出引擎（供同构 CLI 与 AI 帮手视觉输入）。

支持两条无缝合一的路径：
1. canvas 模式（审阅试卷高清原图 + 实时标注合成，<100ms，不依赖浏览器，支持整页/单题/自定义坐标裁切）；
2. ui 模式（调用本机 Edge/Chrome --headless=new + 原生 CDP 离屏渲染，结合前台同步的 ui_state 镜像，
   即使浏览器最小化或被遮挡也能渲染出与前台 100% 一致的画面，并顺手导出可交互 DOM 元素清单与可选 Set-of-Mark 标号）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURES_SUBDIR = "_captures"
MAX_KEEP_CAPTURES = 80

# M1/M2/M3 标框颜色（RGB）
BOX_COLORS = {
    0: (217, 119, 6),    # 姓名/学号
    1: (22, 163, 74),    # 选择题大区
    2: (37, 99, 235),    # 题号
    3: (16, 185, 129),   # 选项
    4: (124, 58, 237),   # 填空大区
    5: (37, 99, 235),    # 大题框
    6: (147, 51, 234),   # 印刷题号
}


def _captures_dir(batches_root: Path) -> Path:
    d = Path(batches_root) / DEFAULT_CAPTURES_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_old_captures(cap_dir: Path, keep: int = MAX_KEEP_CAPTURES) -> None:
    try:
        files = sorted(
            [p for p in cap_dir.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".json")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep * 2 :]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _load_font(size: int = 18):
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for fp in candidates:
        if os.path.isfile(fp):
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _parse_crop_rect(crop_str: str | list | tuple | None, w: int, h: int) -> tuple[int, int, int, int] | None:
    if not crop_str:
        return None
    if isinstance(crop_str, str):
        parts = [p.strip() for p in crop_str.replace(" ", ",").split(",") if p.strip()]
    else:
        parts = list(crop_str)
    if len(parts) != 4:
        return None
    try:
        vals = [float(v) for v in parts]
    except (TypeError, ValueError):
        return None
    # 若全部 <= 1.0，按归一化比例 [x1, y1, x2, y2] 解释
    if all(0.0 <= v <= 1.0 for v in vals) and (vals[2] > vals[0] and vals[3] > vals[1]):
        x1, y1, x2, y2 = int(vals[0] * w), int(vals[1] * h), int(vals[2] * w), int(vals[3] * h)
    else:
        x1, y1, x2, y2 = int(vals[0]), int(vals[1]), int(vals[2]), int(vals[3])
    x1, x2 = max(0, min(w - 1, min(x1, x2))), max(1, min(w, max(x1, x2)))
    y1, y2 = max(0, min(h - 1, min(y1, y2))), max(1, min(h, max(y1, y2)))
    if (x2 - x1) < 8 or (y2 - y1) < 8:
        return None
    return (x1, y1, x2, y2)


def _item_answer_preview(item: dict[str, Any], fb_entry: dict[str, Any] | None = None) -> str:
    if fb_entry and str(fb_entry.get("correction") or "").strip():
        return "[订正] " + str(fb_entry["correction"]).strip()[:48]
    if fb_entry and str(fb_entry.get("answer") or "").strip():
        return "[订正] " + str(fb_entry["answer"]).strip()[:24]
    if item.get("kind") == "choice":
        ans = item.get("answer") or (item.get("recognition") or {}).get("answer") or ""
        return str(ans)
    tr = item.get("transcription")
    if isinstance(tr, dict):
        if item.get("kind") == "identity":
            return f"{tr.get('name', '')} {tr.get('sid', '')}".strip()
        ans = tr.get("answers") or {}
        if isinstance(ans, dict):
            segs = []
            for sk, blanks in ans.items():
                if isinstance(blanks, dict):
                    vals = "/".join(str(v) for v in blanks.values())
                    segs.append(f"{sk}:{vals}" if sk != "无小题号" else vals)
                else:
                    segs.append(f"{sk}:{blanks}")
            return " | ".join(segs)[:60]
    return str(item.get("status") or "")


def capture_review_canvas(
    recognize_dir: Path,
    batches_root: Path,
    *,
    page_id: str = "",
    item_id: str = "",
    crop: str | list | None = None,
    annotate: bool = True,
    with_elements: bool = True,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """服务端纯离屏高清渲染 8904 试卷整页标注图或单题特写（<100ms，零浏览器依赖）。"""
    rec_dir = Path(recognize_dir).resolve()
    res_path = rec_dir / "results.json"
    if not res_path.is_file():
        return {"ok": False, "error": f"找不到识别结果：{res_path}"}

    results = json.loads(res_path.read_text(encoding="utf-8"))
    fb_path = rec_dir / "feedback.json"
    fb_items = {}
    if fb_path.is_file():
        try:
            fb_items = (json.loads(fb_path.read_text(encoding="utf-8")) or {}).get("items") or {}
        except Exception:
            fb_items = {}

    pages = results.get("pages") or []
    if not pages:
        return {"ok": False, "error": "当前批次没有可截取的试卷页"}

    page = None
    if item_id:
        for p in pages:
            if any(it.get("id") == item_id for it in (p.get("items") or [])):
                page = p
                break
    if page is None and page_id:
        page = next((p for p in pages if p.get("id") == page_id or p.get("name") == page_id), None)
    if page is None:
        page = pages[0]

    pid = str(page.get("id") or "")
    img_rel = str(page.get("image") or "")
    img_path = (rec_dir / img_rel).resolve() if img_rel else Path("")
    if not img_rel or not img_path.is_file():
        return {"ok": False, "error": f"找不到第 {pid} 页工作图：{img_rel}"}

    with Image.open(img_path) as raw_im:
        im = raw_im.convert("RGB")
    w, h = im.size
    items = list(page.get("items") or [])
    boxes = list(page.get("boxes") or [])

    target_item = next((it for it in items if it.get("id") == item_id), None) if item_id else None

    elements: list[dict[str, Any]] = []
    for idx, it in enumerate(items, 1):
        b = it.get("box") or [0, 0, 0, 0]
        if len(b) != 4:
            continue
        fb_it = fb_items.get(it.get("id")) if isinstance(fb_items, dict) else None
        elements.append({
            "index": idx,
            "kind": it.get("kind"),
            "item_id": it.get("id"),
            "number": it.get("number"),
            "status": it.get("status"),
            "issues": it.get("issues") or [],
            "answer_preview": _item_answer_preview(it, fb_it),
            "box": [round(float(v), 1) for v in b],
            "selector": f"[data-item-id='{it.get('id')}']",
        })

    if target_item is not None and not crop:
        # 单题高清特写：从原图裁切（留 24px 边距，确保题号和涂改上下文完整可见）
        x1, y1, x2, y2 = [float(v) for v in target_item["box"]]
        pad = 28
        cx1, cy1 = max(0, int(x1) - pad), max(0, int(y1) - pad)
        cx2, cy2 = min(w, int(x2) + pad), min(h, int(y2) + pad)
        crop_im = im.crop((cx1, cy1, cx2, cy2))
        if annotate:
            # 顶部附带一条信息条，绝不遮挡手写本体
            bar_h = 34
            canvas = Image.new("RGB", (max(crop_im.width, 360), crop_im.height + bar_h), (248, 250, 252))
            canvas.paste(crop_im, (0, bar_h))
            draw = ImageDraw.Draw(canvas)
            font = _load_font(16)
            qno = target_item.get("number")
            hdr = f"{pid} · {target_item['id']} (第{qno}题)" if qno is not None else f"{pid} · {target_item['id']}"
            prev = _item_answer_preview(target_item, fb_items.get(target_item["id"]))
            draw.text((10, 7), f"{hdr} | {prev}"[:90], fill=(15, 23, 42), font=font)
            out_im = canvas
        else:
            out_im = crop_im
        scope = "item"
    else:
        out_im = im.copy()
        if annotate:
            draw = ImageDraw.Draw(out_im)
            font = _load_font(16)
            # 先画底层 M1/M2/M3 框
            for b in boxes:
                coords = b.get("box") or []
                if len(coords) != 4:
                    continue
                cls = int(b.get("cls", 5))
                col = BOX_COLORS.get(cls, (37, 99, 235))
                draw.rectangle([float(v) for v in coords], outline=col, width=2)
            # 再标出每个题目条目与问题红框
            for el in elements:
                x1, y1, x2, y2 = el["box"]
                has_issue = bool(el["issues"]) or el["status"] in ("调用失败", "结构异常", "待复核")
                col = (220, 38, 38) if has_issue else (37, 99, 235)
                draw.rectangle([x1, y1, x2, y2], outline=col, width=3 if has_issue else 2)
                tag = f"#{el['index']} " + (f"Q{el['number']}" if el["number"] is not None else str(el["item_id"]).split("_")[-1])
                if el["answer_preview"]:
                    tag += f": {el['answer_preview'][:18]}"
                tx, ty = max(0, x1), max(0, y1 - 22)
                tw = min(w - tx, max(60, len(tag) * 11 + 12))
                draw.rectangle([tx, ty, tx + tw, ty + 20], fill=col)
                draw.text((tx + 4, ty + 1), tag, fill=(255, 255, 255), font=font)

        rect = _parse_crop_rect(crop, out_im.width, out_im.height)
        if rect:
            out_im = out_im.crop(rect)
            scope = "crop"
        else:
            scope = "page"
            # 整页适度限制最大边为 1800px，兼顾清晰度与视觉模型读取速度
            if max(out_im.size) > 1800:
                out_im.thumbnail((1800, 1800), Image.Resampling.LANCZOS)

    cap_dir = _captures_dir(batches_root)
    _prune_old_captures(cap_dir)
    stem = f"cap_review_{pid}_{int(time.time())}_{uuid.uuid4().hex[:5]}"
    img_out = Path(out_path).resolve() if out_path else (cap_dir / f"{stem}.png")
    img_out.parent.mkdir(parents=True, exist_ok=True)
    out_im.save(img_out, format="PNG")

    elem_path = ""
    if with_elements:
        ep = cap_dir / f"{stem}_elements.json"
        ep.write_text(json.dumps({"target": "review", "page_id": pid, "elements": elements}, ensure_ascii=False, indent=2), encoding="utf-8")
        elem_path = str(ep)

    return {
        "ok": True,
        "mode": "canvas",
        "target": "review",
        "scope": scope,
        "page_id": pid,
        "page_name": page.get("name") or "",
        "item_id": target_item["id"] if target_item else "",
        "path": str(img_out),
        "width": out_im.width,
        "height": out_im.height,
        "elements_path": elem_path,
        "elements_count": len(elements),
        "elements": elements[:60] if with_elements else [],
    }


def find_headless_browser() -> str:
    """查找本机自带的 Edge 或 Chrome 浏览器可执行文件（无需额外安装 Playwright）。"""
    if os.name == "nt":
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    for nm in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(nm)
        if found:
            return found
    return ""


class _MinimalCDP:
    """零第三方依赖的最小 RFC6455 WebSocket + Chrome DevTools Protocol 客户端（仅连 127.0.0.1）。"""

    def __init__(self, ws_url: str, timeout: float = 15.0):
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP WebSocket handshake closed")
            buf += chunk
        header = buf.split(b"\r\n\r\n", 1)[0].decode("latin1", errors="ignore")
        if " 101 " not in header:
            raise RuntimeError(f"CDP WebSocket handshake failed: {header[:120]}")
        self._buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise EOFError("CDP socket closed")
            self._buf += chunk
        out = self._buf[:n]
        self._buf = self._buf[n:]
        return out

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        hdr = bytearray([0x80 | opcode])
        ln = len(payload)
        if ln < 126:
            hdr.append(0x80 | ln)
        elif ln < 65536:
            hdr.append(0x80 | 126)
            hdr.extend(struct.pack("!H", ln))
        else:
            hdr.append(0x80 | 127)
            hdr.extend(struct.pack("!Q", ln))
        hdr.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._recv_exact(2)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        data = self._recv_exact(ln)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        self._id += 1
        cid = self._id
        msg = json.dumps({"id": cid, "method": method, "params": params or {}}).encode("utf-8")
        self._send_frame(msg, opcode=1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op, data = self._recv_frame()
            if op == 9:  # ping -> pong
                self._send_frame(data, opcode=10)
                continue
            if op == 8:
                raise EOFError("CDP closed by peer")
            if op != 1:
                continue
            obj = json.loads(data.decode("utf-8", errors="replace"))
            if obj.get("id") == cid:
                if "error" in obj:
                    raise RuntimeError(str(obj["error"]))
                return obj.get("result") or {}
        raise TimeoutError(f"CDP {method} timed out")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


_DOM_EXTRACT_JS = r"""
(() => {
  const sels = 'button, a, input, select, textarea, [role="button"], .step-tab, .arch-node, .env-node, .task-card, .page-row, .margin-item, .overlay-chip, [data-item-id]';
  const nodes = Array.from(document.querySelectorAll(sels));
  const out = [];
  let idx = 0;
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) < 0.05) continue;
    if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) continue;
    idx += 1;
    let sel = '';
    if (el.id) sel = '#' + el.id;
    else if (el.dataset && el.dataset.itemId) sel = `[data-item-id="${el.dataset.itemId}"]`;
    else if (el.dataset && el.dataset.view) sel = `[data-view="${el.dataset.view}"]`;
    else if (el.dataset && el.dataset.pageId) sel = `[data-page-id="${el.dataset.pageId}"]`;
    else sel = el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
    const txt = (el.innerText || el.value || el.getAttribute('title') || el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    out.push({
      index: idx,
      id: el.id || '',
      tag: el.tagName.toLowerCase(),
      selector: sel,
      text: txt,
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    });
    if (out.length >= 90) break;
  }
  return out;
})()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def capture_headless_ui(
    url: str,
    batches_root: Path,
    *,
    target: str = "workbench",
    mirror_client: str = "",
    selector: str = "",
    crop: str | list | None = None,
    width: int = 1440,
    height: int = 900,
    with_elements: bool = True,
    som: bool = False,
    wait_ms: int = 1200,
    out_path: Path | None = None,
    extra_query: dict[str, str] | None = None,
    full_page: bool = False,
) -> dict[str, Any]:
    """用本机 Edge/Chrome 无头模式离屏渲染目标界面（同步前台 ui_state，不抢焦点），可选裁切、只读视图覆盖与元素清单导出。"""
    browser = find_headless_browser()
    if not browser:
        return {"ok": False, "error": "未检测到本机 Edge 或 Chrome 浏览器，无法执行无头网页离屏渲染"}

    # 拼接镜像查询参数（让网页在加载时自动还原前台同款 Tab/页码/选中框/缩放状态，或按只读预览参数切视图）
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    qs["mirror"] = ["1"]
    if mirror_client:
        qs["mirror_client"] = [mirror_client]
    if isinstance(extra_query, dict):
        for k, v in extra_query.items():
            ks = str(k or "").strip()
            vs = str(v or "").strip()
            if ks and vs:
                qs[ks] = [vs]
    final_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(qs, doseq=True)))

    cap_dir = _captures_dir(batches_root)
    _prune_old_captures(cap_dir)
    stem = f"cap_{target}_ui_{int(time.time())}_{uuid.uuid4().hex[:5]}"
    img_out = Path(out_path).resolve() if out_path else (cap_dir / f"{stem}.png")
    img_out.parent.mkdir(parents=True, exist_ok=True)

    dbg_port = _free_port()
    user_dir = Path(tempfile.mkdtemp(prefix="ag_cap_"))
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        f"--window-size={int(width)},{int(height)}",
        f"--remote-debugging-port={dbg_port}",
        f"--user-data-dir={user_dir}",
        "about:blank",
    ]
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = None
    cdp = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creation)
        ws_url = ""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{dbg_port}/json/list", timeout=1.0) as resp:
                    pages = json.loads(resp.read().decode("utf-8", errors="replace"))
                for p in pages:
                    if p.get("type") == "page" and p.get("webSocketDebuggerUrl"):
                        ws_url = p["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                time.sleep(0.15)
        if not ws_url:
            return {"ok": False, "error": "无头浏览器启动超时，未能建立离屏渲染会话"}

        cdp = _MinimalCDP(ws_url, timeout=15.0)
        cdp.call("Page.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": int(width), "height": int(height), "deviceScaleFactor": 1, "mobile": False,
        })
        cdp.call("Page.navigate", {"url": final_url})

        # 等待页面加载并完成前台状态镜像
        ready_deadline = time.monotonic() + max(2.5, wait_ms / 1000.0 + 3.0)
        while time.monotonic() < ready_deadline:
            res = cdp.call("Runtime.evaluate", {
                "expression": "Boolean(document.readyState === 'complete' && (window.__MIRROR_APPLIED__ !== false))",
                "returnByValue": True,
            })
            if (res.get("result") or {}).get("value") is True:
                break
            time.sleep(0.15)
        time.sleep(max(0.25, min(2.0, wait_ms / 1000.0)))

        elements: list[dict[str, Any]] = []
        if with_elements or som:
            ev = cdp.call("Runtime.evaluate", {"expression": _DOM_EXTRACT_JS, "returnByValue": True})
            val = (ev.get("result") or {}).get("value")
            if isinstance(val, list):
                elements = val

        # 可选：在离屏画布上临时贴小编号（Set-of-Mark），方便 AI 一眼说出要点击/聚焦的编号
        if som and elements:
            som_js = (
                "((els)=>{const host=document.createElement('div');host.id='__ag_som';"
                "host.style.cssText='position:fixed;inset:0;pointer-events:none;z-index:999999';"
                "for(const e of els){const b=document.createElement('div');"
                "b.textContent=String(e.index);"
                "b.style.cssText=`position:fixed;left:${e.rect[0]}px;top:${Math.max(0,e.rect[1]-14)}px;"
                "background:#dc2626;color:#fff;font:700 10px/14px monospace;padding:0 3px;border-radius:3px`;"
                "host.appendChild(b);}document.body.appendChild(host);})(" + json.dumps(elements, ensure_ascii=False) + ");"
            )
            cdp.call("Runtime.evaluate", {"expression": som_js})

        clip: dict[str, Any] | None = None
        if selector:
            sel_js = (
                f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
                "if(!el)return null;const r=el.getBoundingClientRect();"
                "return {x:Math.max(0,r.left-6),y:Math.max(0,r.top-6),width:Math.max(16,r.width+12),height:Math.max(16,r.height+12),scale:1};})()"
            )
            ev_sel = cdp.call("Runtime.evaluate", {"expression": sel_js, "returnByValue": True})
            sel_val = (ev_sel.get("result") or {}).get("value")
            if isinstance(sel_val, dict):
                clip = sel_val
        elif crop:
            rect = _parse_crop_rect(crop, int(width), int(height))
            if rect:
                clip = {"x": rect[0], "y": rect[1], "width": rect[2] - rect[0], "height": rect[3] - rect[1], "scale": 1}

        shot_params: dict[str, Any] = {"format": "png", "fromSurface": True}
        if clip:
            shot_params["clip"] = clip
        elif full_page:
            try:
                lm = cdp.call("Page.getLayoutMetrics", timeout=5.0)
                cs = lm.get("cssContentSize") or lm.get("contentSize") or {}
                full_h = int(min(4200, max(int(height), float(cs.get("height") or height) + 24)))
                if full_h > int(height):
                    cdp.call("Emulation.setDeviceMetricsOverride", {
                        "width": int(width), "height": full_h, "deviceScaleFactor": 1, "mobile": False,
                    })
                    time.sleep(0.18)
                shot_params["captureBeyondViewport"] = True
            except Exception:
                shot_params["captureBeyondViewport"] = True
        shot = cdp.call("Page.captureScreenshot", shot_params, timeout=15.0)
        b64 = shot.get("data") or ""
        if not b64:
            return {"ok": False, "error": "无头渲染截图返回为空"}
        png_bytes = base64.b64decode(b64)
        img_out.write_bytes(png_bytes)

        with Image.open(img_out) as im_chk:
            out_w, out_h = im_chk.size

        elem_path = ""
        if with_elements:
            ep = cap_dir / f"{stem}_elements.json"
            ep.write_text(json.dumps({"target": target, "url": final_url, "elements": elements}, ensure_ascii=False, indent=2), encoding="utf-8")
            elem_path = str(ep)

        return {
            "ok": True,
            "mode": "ui",
            "target": target,
            "url": final_url,
            "selector": selector,
            "path": str(img_out),
            "width": out_w,
            "height": out_h,
            "elements_path": elem_path,
            "elements_count": len(elements),
            "elements": elements[:60] if with_elements else [],
        }
    except Exception as exc:
        return {"ok": False, "error": f"离屏 UI 渲染失败：{exc}"}
    finally:
        if cdp is not None:
            cdp.close()
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(user_dir, ignore_errors=True)
        except Exception:
            pass
