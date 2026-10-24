# -*- coding: utf-8 -*-
"""digit 数字审核页。

labels.csv 只是文件清单；标签和审核状态从每张图的
data/<库>/log/<图名>.log 读。点图改数字，确认时追加一条审核事件。

服务：http://127.0.0.1:8903/
用法：py3136 tools\\数字审核\\serve.py [--data data\\digit] [--port 8903]
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

LOCK = threading.Lock()
ROWS = []
GROUPS = {}
DATA = None
CSV_PATH = None


def stem_of(file):
    return file[:-4] if file.lower().endswith(".png") else file


def load():
    global ROWS, GROUPS
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r["label"] = int(r["label"])
            if not 0 <= r["label"] <= 34:
                raise ValueError("数字标签超出0~34: %s" % r.get("file", ""))
            r["_stem"] = stem_of(r["file"])
            st = row_log.digit_state(row_log.read(DATA, r["_stem"]))
            r["_label"] = int(st["label"]) if st["label"] is not None else r["label"]
            r["_pred"] = st["pred_label"]
            r["_reviewed"] = st["reviewed"]
            r["_round"] = st["review_count"]
            rows.append(r)
    ROWS = rows
    rebuild()


def rebuild():
    global GROUPS
    g = {}
    for i, r in enumerate(ROWS):
        g.setdefault(r["_label"], []).append(i)
    GROUPS = g


def image_path(row):
    img = os.path.join(DATA, "images", row["split"], row["file"])
    if not os.path.isfile(img):
        img = os.path.join(DATA, "images", row["file"])
    return img


def log_review(row, label):
    evs = row_log.read(DATA, row["_stem"])
    st = row_log.digit_state(evs)
    n = st["review_count"] + 1
    base = st["pred_label"] if st["pred_label"] is not None else st["label"]
    if base is None:
        base = label
    result = "预标注正确" if int(label) == int(base) else "人工修正"
    desc = ("题号%d" % label) if int(label) == int(base) else ("%s改成%s" % (base, label))
    row_log.append(DATA, row["_stem"], {
        "事件": "审核", "第几轮": n, "标签": int(label), "结果": result,
        "说明": "第%d次审核：%s" % (n, desc)})
    return n


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>digit 数字审核</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f5f7;color:#222}
 header{position:sticky;top:0;background:#fff;border-bottom:1px solid #dcdcdc;padding:8px 12px;z-index:20;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 h1{font-size:15px;margin:0 0 6px}
 .nums{display:flex;flex-wrap:wrap;gap:4px}
 .nums button{min-width:52px;padding:4px 6px;border:1px solid #bbb;background:#fff;border-radius:4px;cursor:pointer;font-size:13px;line-height:1.25}
 .nums button b{font-weight:600}
 .nums button i{font-style:normal;color:#888;font-size:11px;display:block}
 .nums button.on{background:#1867d6;color:#fff;border-color:#1867d6}
 .nums button.on i{color:#dbe7fb}
 .grid{display:flex;flex-wrap:wrap;gap:10px;padding:12px;align-items:flex-start}
 .cell{background:#fff;border:1px solid #ddd;border-radius:6px;padding:6px;display:flex;flex-direction:column;align-items:center;width:170px}
 .cell.done{opacity:.45}
 .cell .nm{font-size:11px;color:#c00;margin-bottom:3px;word-break:break-all;text-align:center}
 .cell img{max-width:158px;max-height:112px;cursor:pointer;background:#fafafa;border:1px solid #eee;image-rendering:pixelated}
 .cell .row{display:flex;gap:4px;margin-top:5px;width:100%;justify-content:center}
 .cell input{width:52px;font-size:14px;padding:3px 4px;text-align:center;border:1px solid #bbb;border-radius:4px}
 .cell button{font-size:12px;padding:3px 6px;cursor:pointer;border:1px solid #1867d6;background:#1867d6;color:#fff;border-radius:4px}
 .cell button:disabled{opacity:.45;cursor:default}
 .cell button.neg{border-color:#888;background:#888}
 .cell .st{font-size:11px;color:#0a0;height:14px;margin-top:2px}
 .bar{font-size:12px;color:#555;margin-top:6px}
 .bar b{color:#c00}
 .tip{font-size:12px;color:#777;padding:0 12px 20px}
</style></head><body>
<header>
  <h1>digit 数字审核 —— 按当前标注分组（当前 <span id="cur">-</span>）</h1>
  <div class="nums" id="nums"></div>
  <div class="bar" id="bar"></div>
</header>
<div class="grid" id="grid"></div>
<div class="tip">点数字切换分组 · 图片显示成功后才能确认 · 输入0~34后点“改”或回车；标签不变时点“确认本图” · 误裁的标题/残块点“非数字”</div>
<script>
let TREE = {groups:{}, total:0, labels:[], reviewed:[]}, CUR = null;
async function loadTree(){
  const r = await fetch('/tree'); TREE = await r.json();
  const ns = Object.keys(TREE.groups).map(Number).sort((a,b)=>a-b);
  const box = document.getElementById('nums'); box.innerHTML='';
  for(const n of ns){
    const b = document.createElement('button');
    b.innerHTML = '<b>'+n+'</b><i>'+TREE.groups[n].length+' 框</i>';
    b.dataset.n = n;
    if(n===CUR) b.classList.add('on');
    b.onclick = ()=>go(n);
    box.appendChild(b);
  }
  document.getElementById('bar').innerHTML = '合计 <b>'+TREE.total+'</b> 框 / '+ns.length+' 个数字';
}
function go(n){ CUR = n; location.hash = 'n'+n; render(); loadTree(); }
function loadImage(img, idx, status, buttons){
  img.dataset.ready='0';
  buttons.forEach(b=>b.disabled=true);
  status.style.color='#777'; status.textContent='图片加载中…';
  const url = '/img?i='+idx+'&nonce='+Date.now();
  img.onload = ()=>{
    img.dataset.ready='1';
    status.textContent='图片已显示；可确认本图';
    buttons.forEach(b=>b.disabled=false);
  };
  img.onerror = ()=>{
    img.dataset.ready='0';
    status.style.color='#c00'; status.textContent='图片加载失败，不能确认';
  };
  img.src = url;
}
function render(){
  const g = document.getElementById('grid');
  const list = (TREE.groups[CUR]||[]);
  document.getElementById('cur').textContent = CUR;
  g.innerHTML = '';
  if(!list.length){ g.innerHTML = '<div class="tip">这组没有框了</div>'; return; }
  for(const idx of list){
    const rel = TREE.files[idx];
    const c = document.createElement('div'); c.className='cell'+(TREE.reviewed[idx]?' done':'');
    const nm = document.createElement('div'); nm.className='nm'; nm.textContent = rel;
    const img = document.createElement('img');
    const row = document.createElement('div'); row.className='row';
    const inp = document.createElement('input'); inp.type='number'; inp.value = TREE.labels[idx]; inp.min=0; inp.max=34;
    const btn = document.createElement('button'); btn.textContent='改'; btn.disabled=true;
    const confirm = document.createElement('button'); confirm.textContent='确认本图'; confirm.disabled=true;
    const neg = document.createElement('button'); neg.className='neg'; neg.textContent='非数字'; neg.disabled=true;
    const st = document.createElement('div'); st.className='st';
    img.onclick = ()=>{ inp.focus(); inp.select(); };
    const submit = async (value, label, confirmSame=false)=>{
      const v = parseInt(value,10);
      if(isNaN(v) || v<0 || v>34){ st.style.color='#c00'; st.textContent='数字范围为0~34'; return; }
      if(v===TREE.labels[idx] && !confirmSame){ st.style.color='#777'; st.textContent='标签未变化；如需确认请点“确认本图”'; return; }
      if(img.dataset.ready!=='1'){ st.style.color='#c00'; st.textContent='图片尚未成功显示，不能确认'; return; }
      btn.disabled = confirm.disabled = neg.disabled = true;
      const r = await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({i:idx, value:v})});
      const j = await r.json();
      if(j.ok){
        c.classList.add('done'); st.style.color='#0a0'; st.textContent=label;
        await loadTree(); render();
      } else {
        btn.disabled = confirm.disabled = neg.disabled = false;
        st.style.color='#c00'; st.textContent = j.err||'失败';
      }
    };
    btn.onclick = ()=>submit(inp.value, '已改为 '+inp.value);
    confirm.onclick = ()=>submit(TREE.labels[idx], '已确认本图', true);
    neg.onclick = ()=>submit(0, '已标非数字');
    inp.onkeydown = (e)=>{ if(e.key==='Enter') btn.click(); };
    row.appendChild(inp); row.appendChild(btn); row.appendChild(confirm); row.appendChild(neg);
    c.appendChild(nm); c.appendChild(img); c.appendChild(row); c.appendChild(st);
    g.appendChild(c);
    loadImage(img,idx,st,[btn,confirm,neg]);
  }
}
(async function(){
  await loadTree();
  const m = location.hash.match(/^#n(\d+)$/);
  CUR = m ? Number(m[1]) : Number(Object.keys(TREE.groups).sort((a,b)=>a-b)[0]);
  render(); loadTree();
})();
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
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE)
        if u.path == "/tree":
            with LOCK:
                groups = {str(k): v for k, v in GROUPS.items()}
                total = sum(len(v) for v in groups.values())
                files = [os.path.join(r["split"], r["file"]).replace(os.sep, "/") for r in ROWS]
                labels = [r["_label"] for r in ROWS]
                reviewed = [bool(r["_reviewed"]) for r in ROWS]
            return self._send(200, json.dumps(
                {"groups": groups, "total": total, "files": files,
                 "labels": labels, "reviewed": reviewed},
                ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/img":
            i = int((q.get("i") or ["-1"])[0])
            if i < 0 or i >= len(ROWS):
                return self._send(404, "not found")
            p = image_path(ROWS[i])
            if not os.path.isfile(p):
                return self._send(404, "not found")
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "image/png", cache="no-store")
        return self._send(404, "404")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/set":
            return self._send(404, "404")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(200, json.dumps({"ok": False, "err": "bad json"}), "application/json")
        i = body.get("i")
        v = body.get("value")
        if not isinstance(i, int) or not isinstance(v, int) or i < 0 or i >= len(ROWS):
            return self._send(200, json.dumps({"ok": False, "err": "未知条目"}), "application/json")
        if not (0 <= v <= 34):
            return self._send(200, json.dumps({"ok": False, "err": "值要 0~34"}), "application/json")
        with LOCK:
            old = ROWS[i]["_label"]
            try:
                nround = log_review(ROWS[i], v)
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "err": "审核记录写入失败: %s" % e}),
                                  "application/json")
            ROWS[i]["_label"] = v
            ROWS[i]["_reviewed"] = True
            ROWS[i]["_round"] = nround
            rebuild()
        if v != old:
            print("  改标注: %s  %s -> %s" % (ROWS[i]["file"], old, v), flush=True)
        else:
            print("  确认本图: %s  label=%s" % (ROWS[i]["file"], v), flush=True)
        return self._send(200, json.dumps({"ok": True}), "application/json")


def main():
    global DATA, CSV_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "digit"))
    ap.add_argument("--port", type=int, default=8903)
    args = ap.parse_args()
    DATA = args.data
    CSV_PATH = os.path.join(DATA, "labels.csv")
    if not os.path.isfile(CSV_PATH):
        print("找不到标注文件:", CSV_PATH)
        return 1
    load()
    print("digit 数字审核")
    print("  标注文件: %s" % CSV_PATH)
    print("  共 %d 框（标签0~34均可审核，当前0标签 %d 框）"
          % (len(ROWS), len(GROUPS.get(0, []))))
    print("  打开: http://127.0.0.1:%d/" % args.port)
    srv = Server(("127.0.0.1", args.port), H)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
