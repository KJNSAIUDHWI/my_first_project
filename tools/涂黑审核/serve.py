# -*- coding: utf-8 -*-
"""涂黑答案审核页。

数据：data/choice（images/ + log/ + manifest.csv）
每行一个日志：data/choice/log/<图名>.log，中文事件。
按当前答案分组，点卡片弹窗，选 A-F 后确认；特殊行单独标记。

服务：http://127.0.0.1:8905/
用法：py3136 tools\\涂黑审核\\serve.py [--data data\\choice] [--port 8905] [--subset 清单.csv] [--unreviewed]
"""
import argparse
import csv
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
from common import row_log  # noqa: E402

DATA = os.path.join(ROOT, "data", "choice")
IMAGES = os.path.join(DATA, "images")
MANIFEST = os.path.join(DATA, "manifest.csv")
LOCK = threading.RLock()

ROWS = []
INDEX = {}
GROUPS = {}
SUBSET = None
ONLY_UNREVIEWED = False
SPECIAL = "__special__"


def file_stem_for(stem, row):
    return "%s_%03d" % (stem, int(row))


def group_of(r):
    if r["special"]:
        return SPECIAL
    if r["reviewed"]:
        return r["answer"]
    return r["pred"]


def load():
    global ROWS, INDEX, GROUPS
    manifest = {}
    with open(MANIFEST, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            manifest.setdefault(r["stem"], []).append(r)
    rows = []
    for stem, items in manifest.items():
        for m in items:
            row = int(m["row"])
            if SUBSET is not None and (stem, row) not in SUBSET:
                continue
            name = file_stem_for(stem, row)
            st = row_log.choice_state(row_log.read(DATA, name))
            rows.append({
                "file": name + ".png",
                "name": name,
                "stem": stem,
                "row": row,
                "no": int(m["no"]),
                "n_opts": int(m["n_opts"]),
                "pred": st["pred_answer"],
                "answer": st["answer"],
                "reviewed": st["reviewed"],
                "special": st["special"],
                "note": st["note"],
                "round": st["review_count"],
            })
    rows.sort(key=lambda r: (r["stem"], r["row"]))
    ROWS = rows
    INDEX = {r["name"]: r for r in rows}
    rebuild()


def rebuild():
    global GROUPS
    g = {}
    for r in ROWS:
        if ONLY_UNREVIEWED and r["reviewed"]:
            continue
        g.setdefault(group_of(r), []).append(r["name"])
    for v in g.values():
        v.sort()
    GROUPS = g


def view(r):
    return {k: r[k] for k in ("file", "name", "stem", "row", "no", "n_opts",
                              "pred", "answer", "reviewed", "special", "note", "round")}


def review(name, answer):
    r = INDEX[name]
    st = row_log.choice_state(row_log.read(DATA, name))
    if st["reviewed"] and not st["special"] and answer == st["answer"]:
        return {"ok": True, "unchanged": True}
    n = st["review_count"] + 1
    pred = st["pred_answer"]
    result = "预标注正确" if answer == pred else "人工修正"
    if answer == pred:
        desc = "确认%s" % (answer or "空白")
    else:
        desc = "%s改成%s" % (pred or "空白", answer or "空白")
    row_log.append(DATA, name, {"事件": "审核", "第几轮": n, "答案": answer,
                                "结果": result, "说明": "第%d次审核：%s" % (n, desc)})
    r.update(answer=answer, reviewed=True, special=False, note="", round=n)
    rebuild()
    return {"ok": True, "unchanged": False}


def mark_special(name, note):
    r = INDEX[name]
    st = row_log.choice_state(row_log.read(DATA, name))
    answer = st["answer"]
    row_log.append(DATA, name, {"事件": "特殊", "类型": note, "答案": answer,
                                "说明": "特殊：%s" % note})
    r.update(special=True, note=note, reviewed=True)
    rebuild()
    return {"ok": True}


def unreview(name):
    r = INDEX[name]
    st = row_log.choice_state(row_log.read(DATA, name))
    row_log.append(DATA, name, {"事件": "撤销审核", "撤销第几轮": st["review_count"],
                                "说明": "撤销第%d次审核" % st["review_count"]})
    r.update(reviewed=False, special=False, note="", answer=st["pred_answer"])
    rebuild()
    return {"ok": True}


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>涂黑答案审核</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#fff;color:#222}
 header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:5px 10px;z-index:20}
 .top{display:flex;align-items:center;gap:10px}
 h1{font-size:13px;margin:0;font-weight:600;flex:1}
 .top label{font-size:12px;color:#555}
 .bar{font-size:12px;color:#555;margin-top:2px}
 .bar b{color:#000}
 .groups{display:flex;flex-wrap:wrap;gap:3px;margin-top:4px;max-height:56px;overflow-y:auto}
 .groups button{min-width:34px;padding:1px 4px;border:1px solid #999;background:#fff;border-radius:2px;cursor:pointer;font-size:12px;line-height:1.15;color:#222}
 .groups button i{font-style:normal;color:#999;font-size:10px;display:block}
 .groups button.on{background:#111;color:#fff;border-color:#111}
 .groups button.on i{color:#bbb}
 #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:4px;padding:6px}
 .cell{position:relative;border:1px solid #222;background:#fff;cursor:pointer;line-height:0}
 .cell img{width:100%;display:block;image-rendering:pixelated}
 .cell::after{content:"";position:absolute;inset:0;pointer-events:none;background:transparent}
 .cell.done::after{background:rgba(26,159,82,.30)}
 .cell.edited::after{background:rgba(29,111,214,.30)}
 .cell.note::after{background:rgba(138,63,209,.35)}
 .cell .tag{position:absolute;z-index:2;left:2px;top:2px;background:rgba(255,255,255,.9);padding:0 3px;font-size:10px;line-height:1.4;color:#333}
 #modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:50}
 .mbox{background:#fff;border-radius:6px;padding:14px;max-width:94vw;max-height:92vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.3)}
 .mhead{font-size:13px;color:#555;margin-bottom:8px}
 #mimg{display:block;width:min(760px,86vw);image-rendering:pixelated;border:1px solid #222}
 .mopts{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}
 .mopts button{width:54px;height:44px;font-size:20px;border:1px solid #333;background:#fff;border-radius:4px;cursor:pointer;color:#222}
 .mopts button.on{background:#111;color:#fff}
 .mact{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .mact button{padding:7px 14px;font-size:13px;border:1px solid #333;background:#fff;border-radius:4px;cursor:pointer}
 .mact button.ok{background:#1867d6;border-color:#1867d6;color:#fff}
 .mact button.warn{background:#8a3fd1;border-color:#8a3fd1;color:#fff}
 .mact button:disabled{opacity:.45;cursor:default}
 .mst{font-size:12px;color:#555;min-height:18px;margin-top:6px}
 .hint{font-size:11px;color:#888;margin-top:6px}
</style></head><body>
<header>
  <div class="top">
    <h1>涂黑答案审核（当前 <span id="cur">-</span>）</h1>
    <label><input type="checkbox" id="only"> 只看未审</label>
  </div>
  <div class="bar" id="bar"></div>
  <div class="groups" id="groups"></div>
</header>
<div id="grid"></div>
<div id="modal"><div class="mbox">
  <div class="mhead" id="mhead"></div>
  <img id="mimg" alt="">
  <div class="mopts" id="mopts"></div>
  <div class="mact">
    <button class="ok" id="mconfirm" disabled>确认本图</button>
    <button id="mblank" disabled>确认空白</button>
    <button class="warn" id="mspecial" disabled>特殊</button>
    <button id="munreview">撤销审核</button>
    <button id="mclose">关闭</button>
  </div>
  <div class="mst" id="mst"></div>
  <div class="hint">图片显示成功后才能确认；A-F 可多选，按字母顺序保存。</div>
</div></div>
<script>
let STATE = {rows:{}, groups:{}, cur:null, only:false, sel:new Set(), curName:null};
const SPECIAL='__special__';
async function load(){
  const only = document.getElementById('only').checked;
  const r = await fetch('/api/list?unreviewed='+(only?'1':'0'));
  const d = await r.json();
  STATE.rows = {}; for(const x of d.rows) STATE.rows[x.name]=x;
  STATE.groups = d.groups; STATE.only = only;
  const keys = Object.keys(d.groups).sort((a,b)=>{
    if(a===SPECIAL) return 1; if(b===SPECIAL) return -1;
    return a.localeCompare(b);
  });
  if(!keys.includes(STATE.cur)) STATE.cur = keys[0]||null;
  renderGroups(keys, d);
  render();
}
function renderGroups(keys, d){
  const box = document.getElementById('groups'); box.innerHTML='';
  for(const k of keys){
    const b = document.createElement('button');
    b.innerHTML = '<b>'+(k===SPECIAL?'特殊':(k||'空'))+'</b><i>'+d.groups[k].length+'</i>';
    if(k===STATE.cur) b.classList.add('on');
    b.onclick = ()=>{ STATE.cur=k; load(); };
    box.appendChild(b);
  }
  document.getElementById('bar').innerHTML = '合计 <b>'+d.total+'</b> 行 / 已审 <b>'+d.done+'</b> / 特殊 <b>'+d.special+'</b>';
}
function render(){
  document.getElementById('cur').textContent = STATE.cur===SPECIAL?'特殊':(STATE.cur||'空');
  const g = document.getElementById('grid'); g.innerHTML='';
  const list = STATE.groups[STATE.cur]||[];
  for(const name of list){
    const r = STATE.rows[name]; if(!r) continue;
    const c = document.createElement('div');
    c.className = 'cell'+(r.special?' note':(r.reviewed?(r.answer===r.pred?' done':' edited'):''));
    const img = document.createElement('img');
    img.loading='lazy'; img.src='/api/img?file='+encodeURIComponent(r.file);
    const tag = document.createElement('div'); tag.className='tag';
    tag.textContent = (r.reviewed?(r.special?'特':r.answer||'空'):(r.pred||'空'));
    c.appendChild(img); c.appendChild(tag);
    c.onclick = ()=>openModal(name);
    g.appendChild(c);
  }
}
function openModal(name){
  const r = STATE.rows[name]; if(!r) return;
  STATE.curName = name; STATE.sel = new Set((r.reviewed&&!r.special?r.answer:r.answer).split(''));
  document.getElementById('mhead').textContent = r.file+' · 第'+r.no+'题 · 预测 '+(r.pred||'空');
  const mo = document.getElementById('mopts'); mo.innerHTML='';
  for(const ch of 'ABCDEF'){
    const b=document.createElement('button'); b.textContent=ch;
    if(STATE.sel.has(ch)) b.classList.add('on');
    b.onclick=()=>{ if(STATE.sel.has(ch)) STATE.sel.delete(ch); else STATE.sel.add(ch); b.classList.toggle('on'); sync(); };
    mo.appendChild(b);
  }
  const img=document.getElementById('mimg');
  img.dataset.ready='0';
  ['mconfirm','mblank','mspecial'].forEach(id=>document.getElementById(id).disabled=true);
  document.getElementById('mst').textContent='图片加载中…';
  img.onload=()=>{ img.dataset.ready='1'; ['mconfirm','mblank','mspecial'].forEach(id=>document.getElementById(id).disabled=false); document.getElementById('mst').textContent='图片已显示'; };
  img.onerror=()=>{ img.dataset.ready='0'; document.getElementById('mst').textContent='图片加载失败，不能确认'; };
  img.src='/api/img?file='+encodeURIComponent(r.file);
  document.getElementById('munreview').disabled = !r.reviewed;
  document.getElementById('modal').style.display='flex';
  sync();
}
function sync(){
  const arr=[...STATE.sel].filter(c=>'ABCDEF'.includes(c)).sort();
  document.getElementById('mst').textContent = '当前：'+(arr.join('')||'空白');
}
async function post(path, body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return await r.json();
}
function closeModal(){ document.getElementById('modal').style.display='none'; }
document.getElementById('mclose').onclick=closeModal;
document.getElementById('mconfirm').onclick=async()=>{
  const arr=[...STATE.sel].sort().join('');
  const j=await post('/api/review',{name:STATE.curName,answer:arr});
  if(!j.ok){ document.getElementById('mst').textContent=j.error||'提交失败'; return; }
  closeModal(); await load();
};
document.getElementById('mblank').onclick=async()=>{
  const j=await post('/api/review',{name:STATE.curName,answer:''});
  if(!j.ok){ document.getElementById('mst').textContent=j.error||'提交失败'; return; }
  closeModal(); await load();
};
document.getElementById('mspecial').onclick=async()=>{
  const note=prompt('特殊原因（例如：学生改过答案 / 乱涂看不清 / 不使用）');
  if(!note) return;
  const j=await post('/api/special',{name:STATE.curName,note});
  if(!j.ok){ document.getElementById('mst').textContent=j.error||'提交失败'; return; }
  closeModal(); await load();
};
document.getElementById('munreview').onclick=async()=>{
  const j=await post('/api/unreview',{name:STATE.curName});
  if(!j.ok){ document.getElementById('mst').textContent=j.error||'失败'; return; }
  closeModal(); await load();
};
document.getElementById('only').onchange=load;
load();
</script></body></html>"""


class Server(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE)
        if u.path == "/api/list":
            with LOCK:
                only = (q.get("unreviewed") or ["0"])[0] == "1"
                rows = [view(r) for r in ROWS if not (only and r["reviewed"])]
                groups = {}
                for r in rows:
                    groups.setdefault(group_of(r), []).append(r["name"])
                total = len(ROWS)
                done = sum(1 for r in ROWS if r["reviewed"])
                special = sum(1 for r in ROWS if r["special"])
            return self._send(200, json.dumps(
                {"rows": rows, "groups": groups, "total": total, "done": done, "special": special},
                ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/img":
            file = os.path.basename((q.get("file") or [""])[0])
            p = os.path.join(IMAGES, file)
            if not file or not os.path.isfile(p):
                return self._send(404, "not found")
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "image/png", cache="no-store")
        return self._send(404, "404")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(200, json.dumps({"ok": False, "error": "bad json"}), "application/json")
        name = body.get("name", "")
        if name not in INDEX:
            return self._send(200, json.dumps({"ok": False, "error": "未知条目"}), "application/json")
        with LOCK:
            try:
                if u.path == "/api/review":
                    answer = body.get("answer", "")
                    if any(c not in "ABCDEF" for c in answer):
                        raise ValueError("非法答案")
                    out = review(name, answer)
                elif u.path == "/api/special":
                    note = (body.get("note") or "").strip()
                    if not note:
                        raise ValueError("特殊原因不能为空")
                    out = mark_special(name, note)
                elif u.path == "/api/unreview":
                    out = unreview(name)
                else:
                    return self._send(404, "404")
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}),
                                  "application/json")
        return self._send(200, json.dumps(out, ensure_ascii=False), "application/json")


def main():
    global DATA, IMAGES, MANIFEST, SUBSET, ONLY_UNREVIEWED
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "choice"))
    ap.add_argument("--port", type=int, default=8905)
    ap.add_argument("--subset", help="只审核清单里的 stem,row")
    ap.add_argument("--unreviewed", action="store_true", help="只显示未审")
    args = ap.parse_args()
    DATA = os.path.abspath(args.data)
    IMAGES = os.path.join(DATA, "images")
    MANIFEST = os.path.join(DATA, "manifest.csv")
    ONLY_UNREVIEWED = args.unreviewed
    if args.subset:
        SUBSET = set()
        with open(args.subset, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                SUBSET.add((r["stem"], int(r["row"])))
        if not SUBSET:
            raise ValueError("审核清单为空")
    if not os.path.isfile(MANIFEST):
        print("找不到 manifest:", MANIFEST)
        return 1
    print("正在读日志…")
    load()
    print("  共 %d 行；已审 %d；特殊 %d" % (
        len(ROWS), sum(1 for r in ROWS if r["reviewed"]), sum(1 for r in ROWS if r["special"])))
    print("  打开: http://127.0.0.1:%d/" % args.port)
    Server(("127.0.0.1", args.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
