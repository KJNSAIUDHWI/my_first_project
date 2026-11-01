# -*- coding: utf-8 -*-
"""涂黑答案审核页。

数据：data/choice（images/ + log/ + manifest.csv）
每行一个日志：data/choice/log/<图名>.log，中文事件。
按答案分组，真实鼠标划过已加载图即确认；可调刷块，点图修改A-F或特殊标签。

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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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

# 状态版本号：审核动作后自增，让 /tree 的序列化结果在无人改动时直接复用。
_STATE = {"version": 0}
_TREE_CACHE = None
_IMG_CACHE = OrderedDict()
_IMG_CACHE_BYTES = 0
_IMG_CACHE_LIMIT = 192 * 1024 * 1024
_IMG_LOCK = threading.Lock()


def read_image(path):
    """带内存缓存的读图，按 mtime+大小失效，避免滚动反复读盘。"""
    global _IMG_CACHE_BYTES
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    with _IMG_LOCK:
        hit = _IMG_CACHE.get(path)
        if hit is not None and hit[0] == sig:
            _IMG_CACHE.move_to_end(path)
            return hit[1]
    with open(path, "rb") as fh:
        data = fh.read()
    with _IMG_LOCK:
        old = _IMG_CACHE.pop(path, None)
        if old is not None:
            _IMG_CACHE_BYTES -= len(old[1])
        _IMG_CACHE[path] = (sig, data)
        _IMG_CACHE_BYTES += len(data)
        while _IMG_CACHE_BYTES > _IMG_CACHE_LIMIT and len(_IMG_CACHE) > 1:
            _, (_, dropped) = _IMG_CACHE.popitem(last=False)
            _IMG_CACHE_BYTES -= len(dropped)
    return data


def file_stem_for(stem, row):
    return "%s_%03d" % (stem, int(row))


def subject_for(row):
    subject = (row.get("subject") or "").strip()
    if subject:
        return subject
    parts = row["stem"].split(".")
    if "选择题区" in parts:
        position = parts.index("选择题区")
        if position:
            return parts[position - 1]
    return "未知学科"


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
    names = [file_stem_for(stem, m['row']) for stem, items in manifest.items() for m in items
             if SUBSET is None or (stem, int(m['row'])) in SUBSET]
    def load_state(name):
        events = row_log.read(DATA, name)
        state = row_log.choice_state(events)
        state['proposal'] = next((e for e in reversed(events) if row_log.event_name(e) in ('预标注', 'pred')), {})
        return state
    with ThreadPoolExecutor(max_workers=12) as pool:
        states = dict(zip(names, pool.map(load_state, names)))
    for stem, items in manifest.items():
        for m in items:
            row = int(m["row"])
            if SUBSET is not None and (stem, row) not in SUBSET:
                continue
            name = file_stem_for(stem, row)
            st = states[name]
            rows.append({
                "file": name + ".png",
                "base": DATA,
                "name": name,
                "stem": stem,
                "subject": subject_for(m),
                "row": row,
                "no": int(m["no"]),
                "n_opts": int(m["n_opts"]),
                "pred": st["pred_answer"],
                "answer": st["answer"],
                "reviewed": st["reviewed"],
                "special": st["special"],
                "note": st["note"],
                "round": st["review_count"],
                "uncertain": bool(st['proposal'].get('疑难', False) or st['proposal'].get('特殊建议', False)),
                "crop_issue": bool(st['proposal'].get('裁图异常', False)),
                "special_suggestion": bool(st['proposal'].get('特殊建议', False)),
                "model": st['proposal'].get('模型', ''),
            })
    rows.sort(key=lambda r: (r["stem"], r["row"]))
    ROWS = rows
    INDEX = {r["name"]: r for r in rows}
    _STATE["version"] += 1
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


def valid_answer(value):
    return isinstance(value, str) and all(c in "ABCDEF" for c in value) and value == "".join(sorted(set(value)))


def review(name, answer, rebuild_groups=True):
    r = INDEX[name]
    st = row_log.choice_state(row_log.read(r["base"], name))
    if st["reviewed"] and not st["special"] and answer == st["answer"]:
        return {"ok": True, "unchanged": True}
    n = st["review_count"] + 1
    pred = st["pred_answer"]
    result = "预标注正确" if answer == pred else "人工修正"
    if answer == pred:
        desc = "确认%s" % (answer or "空白")
    else:
        desc = "%s改成%s" % (pred or "空白", answer or "空白")
    row_log.append(r["base"], name, {"事件": "审核", "第几轮": n, "答案": answer,
                                "结果": result, "说明": "第%d次审核：%s" % (n, desc)})
    r.update(answer=answer, reviewed=True, special=False, note="", round=n)
    _STATE["version"] += 1
    if rebuild_groups:
        rebuild()
    return {"ok": True, "unchanged": False}


def mark_special(name, note):
    r = INDEX[name]
    st = row_log.choice_state(row_log.read(r["base"], name))
    answer = st["answer"]
    row_log.append(r["base"], name, {"事件": "特殊", "类型": note, "答案": answer,
                                "说明": "特殊：%s" % note})
    r.update(special=True, note=note, reviewed=True)
    _STATE["version"] += 1
    rebuild()
    return {"ok": True}


def unreview(name):
    r = INDEX[name]
    st = row_log.choice_state(row_log.read(r["base"], name))
    row_log.append(r["base"], name, {"事件": "撤销审核", "撤销第几轮": st["review_count"],
                                "说明": "撤销第%d次审核" % st["review_count"]})
    r.update(reviewed=False, special=False, note="", answer=st["pred_answer"])
    _STATE["version"] += 1
    rebuild()
    return {"ok": True}


PAGE = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>涂黑答案审核</title>
<style>
*{box-sizing:border-box}body{margin:0;font:13px system-ui,"Microsoft YaHei";color:#222;background:white}header{position:sticky;top:0;z-index:20;background:white;padding:6px 10px;border-bottom:1px solid #ccc}.top,.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}h1{font-size:14px;margin:0;flex:1}.bar{color:#666;font-size:12px;margin:3px 0}.nums{display:flex;flex-wrap:wrap;gap:3px;margin:4px 0;max-height:68px;overflow:auto}button,select{cursor:pointer;border:1px solid #aaa;border-radius:3px;background:white;padding:4px 8px;color:#222}button:disabled{opacity:.4;cursor:default}.nums button{min-width:34px;padding:2px 5px;font-size:12px}.nums i{display:block;font-size:10px;color:#888;font-style:normal}.nums .on{background:#111;color:white}.nums .on i{color:#ccc}.brush-controls{display:inline-flex;gap:6px;align-items:center;font-size:12px}.brush-controls input{width:76px}.brush-outline{position:fixed;display:none;z-index:25;pointer-events:none;border:2px solid #1867d6;background:rgba(24,103,214,.10);border-radius:3px}.primary{background:#1867d6;border-color:#1867d6;color:white}#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:4px;padding:6px;align-items:start}.cell{position:relative;height:46px;border:1px solid #aaa;background:#fafafa;cursor:pointer;display:flex;align-items:center;justify-content:center;content-visibility:auto;contain-intrinsic-size:auto 140px auto 46px}.cell img{width:94%;height:38px;object-fit:contain;image-rendering:pixelated}.cell .tag{position:absolute;left:3px;top:1px;font-size:10px;color:#555;background:#ffffffda;padding:0 2px}.cell .pick{position:absolute;right:2px;bottom:2px;cursor:pointer}.cell:has(.pick:checked){outline:2px solid #1867d6;outline-offset:-2px;background:#eaf2ff}.cell.pending{box-shadow:inset 0 0 0 2px #efb541}.cell.save-error{box-shadow:inset 0 0 0 2px #d22}.cell.located{outline:3px solid #e21;z-index:1}.cell.done{background:#d8efdf;border-color:#4b9c68}.cell.done img{opacity:.45}.cell.done .tag{color:#18703a;font-weight:bold}.cell.special{background:#e9ddf5;border-color:#9164b6}.cell .pick{width:11px;height:11px;margin:0}.cell .flags{font-size:8px;max-width:30px;overflow:hidden;white-space:nowrap}.nums .complete{background:#d8efdf;color:#18703a}.nums .complete.on{background:#18703a;color:white}.nums button{min-width:64px}.cell.issue{border-color:#d78b23}.cell .flags{position:absolute;left:3px;bottom:1px;color:#a65e00;font-size:10px}
.cell.moved{background:#ffcf2e;border:2px solid #b45309;opacity:1;animation:movedFlash .9s ease-out;pointer-events:none;z-index:2}
.cell.moved img{display:none}
.cell.moved .flags{display:none}
.cell.moved .tag{left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;background:transparent;color:#5a2d00;font-weight:bold;font-size:15px;letter-spacing:1px}
@keyframes movedFlash{0%{background:#ff4d00;box-shadow:inset 0 0 0 4px #7a1f00;transform:scale(1.12)}70%{background:#ffcf2e;box-shadow:inset 0 0 0 4px #b45309}100%{background:#ffcf2e;box-shadow:none;transform:scale(1)}}#modal{position:fixed;inset:0;z-index:50;background:#0007;display:none;align-items:center;justify-content:center}.mbox{background:white;border-radius:6px;padding:18px;width:min(560px,94vw);box-shadow:0 10px 40px #0005}#mhead{font-size:12px;color:#666;overflow-wrap:anywhere}#mimg{display:block;margin:16px auto;height:180px;width:90%;object-fit:contain;max-height:38vh;image-rendering:pixelated}#mvalue{font-size:26px;width:100px;text-align:center;padding:5px}.mact{display:flex;gap:8px;align-items:center;flex-wrap:wrap}#mstatus{min-height:24px;padding-top:8px;color:#666}.hint{font-size:11px;color:#777}.more{height:46px}
</style></head><body><header>
<div class="top"><h1>涂黑答案审核 · 当前 <span id="cur">—</span></h1><label><input type="checkbox" id="hover" checked onchange="clearBrush();LAST_POINT=null;">划过即确认</label><label><input type="checkbox" id="only" onchange="refreshView()">只看未审</label></div>
<div class="bar" id="bar"></div><div class="nums" id="nums"></div>
<div class="actions"><label>学科 <select id="subject" onchange="refreshView()"><option value="">全部学科</option></select></label><select id="priority" onchange="refreshView()"><option value="all">全部图片</option><option value="uncertain">疑难/改涂建议</option><option value="crop">裁图异常</option><option value="clear">清晰、裁图正常</option></select><button onclick="selectVisible()">选中屏幕可见图片</button><button onclick="clearPicks()">取消选择</button><button id="bulk" class="primary" onclick="confirmSelected()">确认选中正确</button><button onclick="locateUnreviewed()">下一个未审（F）</button><button onclick="hardRefresh()" title="按最新答案把改过值的卡片归到对应分组，并重绘当前组">重排分组（R）</button><button id="retryHover" hidden onclick="retryHover()">重试保存</button><span class="brush-controls"><label>列 <input id="brushCols" type="range" min="1" max="6" step="1" value="1" aria-label="审核块列数" oninput="changeBrush()"></label><label>行 <input id="brushRows" type="range" min="1" max="6" step="1" value="1" aria-label="审核块行数" oninput="changeBrush()"></label><b id="brushSize">1列 × 1行</b></span><span id="batchStatus" role="status"></span></div>
<div class="hint">划过已加载图片即确认，绿色表示已审 · 点图修改/特殊标记 · 撤销在弹窗内 · 蓝框为审核块（鼠标所在图为左上角，仅确认屏幕内已加载图） · 黄色待保存，绿色已保存 · F 定位未审 · R 重排分组 · A / D 切换答案组 · 每组全部展示，绿色已审可跳过</div></header><div id="grid"></div><div id="brushOutline" class="brush-outline" aria-hidden="true"></div>
<div id="modal" role="dialog" aria-modal="true" aria-label="修改涂黑答案"><div class="mbox"><div id="mhead"></div><img id="mimg" alt="答题行放大图"><div class="mact"><input id="mvalue" aria-label="答案（A至F，可多选）" type="text" maxlength="6"><span id="answerOptions"></span><button id="msave" class="primary" onclick="saveModal(false)" disabled>确认 / 保存修改</button><button id="mzero" onclick="saveModal(true)" disabled>确认空白</button><button onclick="closeModal()">关闭</button></div><div style="margin-top:12px;display:flex;gap:6px"><input id="mnote" aria-label="特殊标签" placeholder="特殊标签，例如：裁切不完整、看不清" maxlength="300" style="flex:1;min-width:0"><button id="mspecial" onclick="markSpecial()" disabled>保存特殊标签</button><button onclick="undoReview()">撤销审核</button></div><div id="mstatus" role="status"></div><div class="hint">Enter 保存 · Esc 关闭 · A至F可多选，空白表示未涂</div></div></div>
<script>
let TREE={groups:{},labels:[],reviewed:[]},CUR=null,ACTIVE=null,READY=false,BUSY=false;
const HOVER_PENDING=new Set(),HOVER_QUEUE=new Map(),HOVER_FAILED=new Map(),GROUP_PROGRESS=new Map(),GROUP_BUTTONS=new Map(),ROW_GROUP=new Map(),CELL_BY_ID=new Map();
const GRID_CELLS=[];let GRID_GEOMETRY=null;
let HOVER_TIMER=null,HOVER_FLIGHT=null,PROGRESS_TIMER=null,TOTAL_DONE=0,LAST_POINT=null,LAST_LOCATED=-1;

const $=id=>document.getElementById(id);
async function loadTree(){const r=await fetch('/tree');if(!r.ok)throw Error('读取失败');TREE=await r.json();const select=$('subject'),selected=select.value;select.replaceChildren(new Option('全部学科',''));for(const subject of [...new Set(TREE.subjects)].sort((a,b)=>a.localeCompare(b,'zh-CN')))select.add(new Option(subject,subject));if([...select.options].some(o=>o.value===selected))select.value=selected;indexProgress();renderGroups();}
function indexProgress(){GROUP_PROGRESS.clear();ROW_GROUP.clear();TOTAL_DONE=0;for(const [n,ids] of Object.entries(TREE.groups)){let done=0;for(const i of ids){ROW_GROUP.set(i,n);if(TREE.reviewed[i])done++;}GROUP_PROGRESS.set(n,{total:ids.length,done,pending:ids.length-done});TOTAL_DONE+=done;}}
function groupStats(n){return GROUP_PROGRESS.get(n)||{total:0,done:0,pending:0};}
function updateProgress(){for(const [n,b] of GROUP_BUTTONS){const s=groupStats(n);b.querySelector('i').textContent='已审 '+s.done+'/'+s.total;b.classList.toggle('complete',s.pending===0);b.title='已审 '+s.done+' 张 / 共 '+s.total+' 张 / 待审 '+s.pending+' 张';}$('bar').textContent='合计 '+TREE.total+' 张 / 已审 '+TOTAL_DONE+' / 待审 '+(TREE.total-TOTAL_DONE);currentProgress();}
function scheduleProgress(){if(PROGRESS_TIMER===null)PROGRESS_TIMER=setTimeout(()=>{PROGRESS_TIMER=null;updateProgress();},100);}
function currentProgress(){const subject=$('subject').value;let s=groupStats(CUR);if(subject){const ids=(TREE.groups[CUR]||[]).filter(i=>TREE.subjects[i]===subject),done=ids.filter(i=>TREE.reviewed[i]).length;s={total:ids.length,done,pending:ids.length-done};}$('cur').textContent=(CUR==='__special__'?'特殊':CUR||'空白')+(subject?' · '+subject:'')+' · 已审 '+s.done+'/'+s.total+' · 待审 '+s.pending;}
function renderGroups(){const ns=Object.keys(TREE.groups).sort((a,b)=>a==='__special__'?1:b==='__special__'?-1:a.localeCompare(b));$('nums').replaceChildren();GROUP_BUTTONS.clear();for(const n of ns){const s=groupStats(n),b=document.createElement('button');b.innerHTML='<b>'+(n==='__special__'?'特殊':n||'空白')+'</b><i>已审 '+s.done+'/'+s.total+'</i>';b.title='已审 '+s.done+' 张 / 共 '+s.total+' 张 / 待审 '+s.pending+' 张';b.className=(n===CUR?'on ':'')+(s.pending===0?'complete':'');b.onclick=()=>go(n);$('nums').append(b);GROUP_BUTTONS.set(n,b);}$('bar').textContent='合计 '+TREE.total+' 张 / 已审 '+TOTAL_DONE+' / 待审 '+(TREE.total-TOTAL_DONE);currentProgress();}
function go(n){if(BUSY||ACTIVE!==null)return;CUR=n;LAST_POINT=null;LAST_LOCATED=-1;location.hash='n'+n;renderGroups();render();window.scrollTo(0,0);}
function stepGroup(delta){const ns=Object.keys(TREE.groups).sort((a,b)=>a==='__special__'?1:b==='__special__'?-1:a.localeCompare(b));const pos=ns.indexOf(CUR)+delta;if(pos>=0&&pos<ns.length)go(ns[pos]);}
function refreshView(){if(BUSY)return;render();}
function clearPicks(){document.querySelectorAll('.pick').forEach(p=>p.checked=false);updatePicks();}
function updatePicks(){const n=document.querySelectorAll('.pick:checked').length;$('bulk').textContent=n?'确认选中 '+n+' 张正确':'确认选中正确';$('bulk').disabled=BUSY||!n;}
function selectVisible(){if(BUSY)return;document.querySelectorAll('.cell').forEach(c=>{const p=c.querySelector('.pick'),r=c.getBoundingClientRect();if(p&&!p.disabled&&r.top>=$('grid').getBoundingClientRect().top&&r.top>=$('nums').parentElement.getBoundingClientRect().bottom&&r.bottom<=innerHeight)p.checked=true;});updatePicks();}
function visibleNow(i){const p=$('priority').value,subject=$('subject').value;return (!subject||TREE.subjects[i]===subject)&&(CUR==='__special__'||!$('only').checked||!TREE.reviewed[i])&&(p==='all'||p==='uncertain'&&TREE.uncertain[i]||p==='crop'&&TREE.crop_issue[i]||p==='clear'&&!TREE.uncertain[i]&&!TREE.crop_issue[i]);}
let RENDER_TOKEN=0;
function buildCell(i){
 const c=document.createElement('div');c.className='cell'+(TREE.reviewed[i]?' done':'')+(TREE.special[i]?' special':'')+(TREE.uncertain[i]||TREE.crop_issue[i]?' issue':'');c.title=TREE.files[i]+(TREE.notes[i]?' · '+TREE.notes[i]:'');c.dataset.i=i;c.dataset.pos=GRID_CELLS.length;GRID_CELLS.push(c);CELL_BY_ID.set(i,c);if(HOVER_PENDING.has(i))c.classList.add('pending');if(HOVER_FAILED.has(i))c.classList.add('save-error');
 const img=document.createElement('img');img.alt='答案 '+TREE.labels[i];img.loading='lazy';img.decoding='async';
 const pick=document.createElement('input');pick.type='checkbox';pick.className='pick';pick.dataset.i=i;pick.disabled=true;pick.setAttribute('aria-label','选择图片 '+i);pick.onclick=e=>e.stopPropagation();pick.onchange=updatePicks;
 img.onload=()=>{pick.disabled=BUSY||TREE.reviewed[i]||TREE.special[i];};img.onerror=()=>{pick.checked=false;pick.disabled=true;c.title+=' · 图片加载失败';updatePicks();};img.src='/img?i='+i;
 const tag=document.createElement('span');tag.className='tag';tag.textContent=TREE.special[i]?'特殊':(TREE.reviewed[i]?'✓ ':'')+(TREE.labels[i]||'空白');
 const flags=document.createElement('span');flags.className='flags';flags.textContent=[TREE.uncertain[i]?'疑难':'',TREE.crop_issue[i]?'裁图':''].filter(Boolean).join(' · ');
 c.append(img,tag,flags,pick);c.onclick=()=>{if(!BUSY)openModal(i);};
 return c;
}
// 整组几千张时按帧分批插入：首屏立即出现，后面的批次不阻塞鼠标和滚动。
function render(){
 const token=++RENDER_TOKEN;
 const all=(TREE.groups[CUR]||[]).filter(visibleNow);
 currentProgress();LAST_POINT=null;clearBrush();GRID_CELLS.length=0;GRID_GEOMETRY=null;CELL_BY_ID.clear();
 const grid=$('grid');grid.replaceChildren();
 if(!all.length){grid.textContent='这组没有待审核图片';updatePicks();return;}
 let pos=0;
 const step=()=>{
  if(token!==RENDER_TOKEN)return;
  const end=Math.min(pos+(pos===0?900:300),all.length);
  const frag=document.createDocumentFragment();
  for(;pos<end;pos++)frag.append(buildCell(all[pos]));
  grid.append(frag);
  if(pos<all.length)requestAnimationFrame(step);else updatePicks();
 };
 step();
}
function openModal(i){clearBrush();LAST_POINT=null;ACTIVE=i;READY=false;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=true;$('mhead').textContent=TREE.files[i]+' · '+TREE.models[i]+(TREE.uncertain[i]?' · 模型疑难':'')+(TREE.crop_issue[i]?' · 裁图异常':'');$('mvalue').value=TREE.labels[i];renderAnswerOptions();$('mnote').value=TREE.notes[i]||'';$('mstatus').textContent='图片加载中…';$('modal').style.display='flex';const img=$('mimg');img.onload=()=>{if(ACTIVE!==i)return;READY=true;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=false;$('mstatus').textContent='核对答案，正确可直接确认';$('mvalue').focus();$('mvalue').select();};img.onerror=()=>{$('mstatus').textContent='图片加载失败，不能确认';};img.src='/img?i='+i;}
function closeModal(){if(BUSY)return;$('modal').style.display='none';ACTIVE=null;READY=false;}
async function post(i,value,hover=false){const r=await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({i,value,hover})});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.err||'保存失败');return j;}
function renderAnswerOptions(){const box=$('answerOptions');box.replaceChildren();for(const ch of 'ABCDEF'){const b=document.createElement('button');b.textContent=ch;b.style.background=$('mvalue').value.includes(ch)?'#d8efdf':'white';b.onclick=()=>{const v=$('mvalue').value; $('mvalue').value=v.includes(ch)?v.replace(ch,''):[...v,ch].sort().join('');renderAnswerOptions();};box.append(b);}}
$('mvalue').oninput=renderAnswerOptions;
async function saveModal(blank){if(BUSY||!READY||ACTIVE===null)return;const raw=blank?'':$('mvalue').value.trim().toUpperCase();if(!/^[A-F]*$/.test(raw)){$('mstatus').textContent='答案只允许A至F，可多选或留空';return;}const v=[...new Set(raw)].sort().join('');const i=ACTIVE;BUSY=true;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=true;try{await flushHover();await post(i,v);BUSY=false;applyLocal(i,{answer:v,reviewed:true,special:false,note:''});closeModal();}catch(e){$('mstatus').textContent=e.message;BUSY=false;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=!READY;}}
async function confirmSelected(){if(BUSY)return;const picks=[...document.querySelectorAll('.pick:checked')].filter(p=>!p.disabled).map(p=>({i:Number(p.dataset.i),value:TREE.labels[Number(p.dataset.i)]}));if(!picks.length)return;BUSY=true;document.querySelectorAll('.pick').forEach(p=>p.disabled=true);updatePicks();let done=0;try{await flushHover();for(const p of picks){if(TREE.reviewed[p.i])continue;await post(p.i,p.value);done++;paintSaved(p.i);if(!visibleNow(p.i))hideCell(p.i);$('batchStatus').textContent='已确认 '+done+'/'+picks.length;}}catch(e){$('batchStatus').textContent='已确认 '+done+' 张，停止：'+e.message;}finally{BUSY=false;updatePicks();scheduleProgress();}}


function queueHover(i,cell,img){
 if(!$('hover').checked||BUSY||ACTIVE!==null||TREE.reviewed[i]||TREE.special[i]||HOVER_PENDING.has(i)||HOVER_FAILED.has(i)||!img.complete||!img.naturalWidth)return;
 HOVER_PENDING.add(i);HOVER_QUEUE.set(i,{i,value:TREE.labels[i]});cell.classList.add('pending');
 if(HOVER_TIMER===null&&!HOVER_FLIGHT)HOVER_TIMER=setTimeout(()=>{HOVER_TIMER=null;flushHover();},60);
}
function paintSaved(i){
 if(!TREE.reviewed[i]){TREE.reviewed[i]=true;TOTAL_DONE++;const s=groupStats(ROW_GROUP.get(i));s.done++;s.pending--;}
 updateCell(i);
}
function groupKeyOf(i){return TREE.special[i]?'__special__':(TREE.reviewed[i]?(TREE.labels[i]||''):(TREE.preds[i]||''));}
function recountGroup(n){const ids=TREE.groups[n]||[];let done=0;for(const i of ids)if(TREE.reviewed[i])done++;GROUP_PROGRESS.set(n,{total:ids.length,done,pending:ids.length-done});}
function updateCell(i){
 const c=CELL_BY_ID.get(i);if(!c)return;
 c.classList.toggle('done',!!TREE.reviewed[i]);c.classList.toggle('special',!!TREE.special[i]);c.classList.remove('pending','save-error');
 c.querySelector('.tag').textContent=TREE.special[i]?'特殊':(TREE.reviewed[i]?'✓ ':'')+(TREE.labels[i]||'空白');
 const pick=c.querySelector('.pick'),img=c.querySelector('img');
 if(pick){pick.checked=false;pick.disabled=!!(TREE.reviewed[i]||TREE.special[i]||BUSY||!img||!img.naturalWidth);}
}
function groupLabel(n){return n==='__special__'?'特殊':(n||'空白');}
// 卡片被改到别的答案组时，原位置不直接消失，留一个明显的“已移到 X 组”标记。
function markMoved(i,now){const c=CELL_BY_ID.get(i);if(!c)return;c.classList.remove('pending','save-error');c.classList.add('moved');c.querySelector('.tag').textContent='→ '+groupLabel(now)+'组';const f=c.querySelector('.flags');if(f)f.textContent='';c.title+=' · 已移到 '+groupLabel(now)+' 组';}
function hideCell(i){const c=CELL_BY_ID.get(i);if(!c)return;c.style.visibility='hidden';c.style.pointerEvents='none';}
function showCell(i){const c=CELL_BY_ID.get(i);if(!c)return;c.style.visibility='';c.style.pointerEvents='';}
// 服务端已确认保存后只改本地状态，不再整页重拉 /tree 和重建几千个卡片。
function applyLocal(i,st){
 const wasReviewed=!!TREE.reviewed[i],oldGroup=ROW_GROUP.get(i)||groupKeyOf(i);
 if(st.answer!==undefined)TREE.labels[i]=st.answer;
 if(st.reviewed!==undefined)TREE.reviewed[i]=st.reviewed;
 if(st.special!==undefined)TREE.special[i]=st.special;
 if(st.note!==undefined)TREE.notes[i]=st.note;
 if(wasReviewed!==TREE.reviewed[i])TOTAL_DONE+=TREE.reviewed[i]?1:-1;
 const now=groupKeyOf(i);
 if(now!==oldGroup){
  const arr=TREE.groups[oldGroup];if(arr){const k=arr.indexOf(i);if(k>=0)arr.splice(k,1);}
  if(!TREE.groups[now]){TREE.groups[now]=[];renderGroups();}
  TREE.groups[now].push(i);ROW_GROUP.set(i,now);
  recountGroup(oldGroup);recountGroup(now);
 }
 updateCell(i);
 if(now!==oldGroup){$('batchStatus').textContent='这张已移到 '+groupLabel(now)+' 组';}
 if(now!==CUR)markMoved(i,now);
 else if(visibleNow(i))showCell(i);
 else hideCell(i);
 scheduleProgress();
 return now;
}
function hoverStatus(){const pending=HOVER_PENDING.size,failed=HOVER_FAILED.size;$('retryHover').hidden=!failed;$('batchStatus').textContent=(pending?'正在保存 '+pending+' 张': '划过记录已保存')+(failed?' · '+failed+' 张保存失败，请重试':'');}
async function flushHover(){
 if(HOVER_TIMER!==null){clearTimeout(HOVER_TIMER);HOVER_TIMER=null;}
 if(HOVER_FLIGHT)return HOVER_FLIGHT;
 if(!HOVER_QUEUE.size)return;
 HOVER_FLIGHT=(async()=>{
  while(HOVER_QUEUE.size){
   const items=[...HOVER_QUEUE.values()].slice(0,64);for(const x of items)HOVER_QUEUE.delete(x.i);hoverStatus();
   try{
    const r=await fetch('/hover_batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});const j=await r.json();if(!r.ok||!j.ok||!Array.isArray(j.results))throw Error(j.err||'保存失败');
    const by=new Map(j.results.map(x=>[x.i,x]));
    for(const item of items){const x=by.get(item.i);HOVER_PENDING.delete(item.i);const c=CELL_BY_ID.get(item.i);if(x&&x.ok&&x.reviewed&&!x.special&&x.value===TREE.labels[item.i]){HOVER_FAILED.delete(item.i);paintSaved(item.i);}else{HOVER_FAILED.set(item.i,item);if(c){c.classList.remove('pending');c.classList.add('save-error');}}}
    scheduleProgress();updatePicks();
   }catch(error){
    for(const item of [...items,...HOVER_QUEUE.values()]){HOVER_FAILED.set(item.i,item);HOVER_PENDING.delete(item.i);const c=CELL_BY_ID.get(item.i);if(c){c.classList.remove('pending');c.classList.add('save-error');}}HOVER_QUEUE.clear();
   }
   hoverStatus();
  }
 })();
 try{await HOVER_FLIGHT;}finally{HOVER_FLIGHT=null;}
}
function retryHover(){if(BUSY)return;for(const [i,item] of HOVER_FAILED){if(TREE.reviewed[i])continue;HOVER_QUEUE.set(i,item);HOVER_PENDING.add(i);const c=CELL_BY_ID.get(i);if(c){c.classList.remove('save-error');c.classList.add('pending');}}HOVER_FAILED.clear();flushHover();}
function brushPositions(start,columns,total,width,height){
 const result=[],col=start%columns;
 for(let y=0;y<height;y++)for(let x=0;x<width&&col+x<columns;x++){const pos=start+y*columns+x;if(pos<total)result.push(pos);}
 return result;
}
function brushGeometry(){if(!GRID_GEOMETRY){const style=getComputedStyle($('grid'));GRID_GEOMETRY={columns:style.gridTemplateColumns.trim().split(/\s+/).length,gapY:parseFloat(style.rowGap)||0,gapX:parseFloat(style.columnGap)||0};}return GRID_GEOMETRY;}
function brushValue(id){return Math.max(1,Math.min(6,Number($(id).value)||1));}
function clearBrush(){$('brushOutline').style.display='none';}
function changeBrush(){LAST_POINT=null;clearBrush();const w=brushValue('brushCols'),h=brushValue('brushRows');$('brushSize').textContent=w+'列 × '+h+'行 · 最多'+(w*h)+'张';try{localStorage.setItem('choiceBrush',JSON.stringify([w,h]));}catch(error){}}
function loadBrush(){try{const size=JSON.parse(localStorage.getItem('choiceBrush'));if(Array.isArray(size)&&size.length===2){$('brushCols').value=Math.max(1,Math.min(6,Number(size[0])||1));$('brushRows').value=Math.max(1,Math.min(6,Number(size[1])||1));}}catch(error){}changeBrush();}
function brushAt(cell,submit){
 const geo=brushGeometry(),start=Number(cell.dataset.pos),w=brushValue('brushCols'),h=brushValue('brushRows'),rect=cell.getBoundingClientRect();
 const positions=brushPositions(start,geo.columns,GRID_CELLS.length,w,h),headerBottom=document.querySelector('header').getBoundingClientRect().bottom;
 if(submit){for(const pos of positions){const y=Math.floor(pos/geo.columns)-Math.floor(start/geo.columns),top=rect.top+y*(rect.height+geo.gapY);if(top<headerBottom||top+rect.height>innerHeight)continue;const target=GRID_CELLS[pos];queueHover(Number(target.dataset.i),target,target.querySelector('img'));}return;}
 const cols=Math.min(w,geo.columns-start%geo.columns),rows=positions.length?Math.floor(positions[positions.length-1]/geo.columns)-Math.floor(start/geo.columns)+1:0;
 const outline=$('brushOutline');Object.assign(outline.style,{display:rows?'block':'none',left:rect.left+'px',top:rect.top+'px',width:(cols*(rect.width+geo.gapX)-geo.gapX)+'px',height:Math.min(rows*(rect.height+geo.gapY)-geo.gapY,Math.max(0,innerHeight-rect.top))+'px'});
}
// 仅连接120ms内、同一分组且未发生滚动的真实移动；只确认轨迹实际经过且已加载的卡片。
function samplePath(from,to,visit){const distance=Math.hypot(to.x-from.x,to.y-from.y),steps=Math.max(1,Math.ceil(distance/20));for(let n=0;n<=steps;n++)visit(from.x+(to.x-from.x)*n/steps,from.y+(to.y-from.y)*n/steps);}
function trackHover(e){
 if(!e.isTrusted||!(e.movementX||e.movementY)||!$('hover').checked||BUSY||ACTIVE!==null){LAST_POINT=null;clearBrush();return;}
 // 采样阶段只读 DOM，命中结果收集完再统一写，避免每次 elementFromPoint 都与样式变更交替触发强制重排。
 const now={x:e.clientX,y:e.clientY,t:performance.now()},prev=LAST_POINT;LAST_POINT=now;const seen=new Set(),hit=[];
 samplePath(prev&&now.t-prev.t<=120?prev:now,now,(x,y)=>{const cell=document.elementFromPoint(x,y)?.closest('#grid .cell');if(!cell)return;const i=Number(cell.dataset.i);if(seen.has(i))return;seen.add(i);hit.push(cell);});
 for(const cell of hit)brushAt(cell,true);
 const current=document.elementFromPoint(now.x,now.y)?.closest('#grid .cell');if(current)brushAt(current,false);else clearBrush();
}
$('grid').addEventListener('mousemove',trackHover);$('grid').addEventListener('mouseleave',()=>{LAST_POINT=null;clearBrush();});
document.addEventListener('scroll',()=>{LAST_POINT=null;clearBrush();},true);window.addEventListener('resize',()=>{GRID_GEOMETRY=null;LAST_POINT=null;clearBrush();});
window.addEventListener('beforeunload',e=>{if(HOVER_PENDING.size||HOVER_FAILED.size){e.preventDefault();e.returnValue='';}});
// 按最新答案重排：改过值的卡片从当前组移除，回到它现在所属的分组。
async function hardRefresh(){
 if(BUSY)return;
 const keep=CUR;
 BUSY=true;$('batchStatus').textContent='正在按答案重排…';
 try{
  await flushHover();
  await loadTree();
  const ns=Object.keys(TREE.groups).sort((a,b)=>a==='__special__'?1:b==='__special__'?-1:a.localeCompare(b));
  CUR=ns.includes(keep)?keep:(ns[0]||'');
  BUSY=false;
  render();
  $('batchStatus').textContent='已按答案重排 · '+groupLabel(CUR)+'组 '+((TREE.groups[CUR]||[]).length)+' 张 / 全库已审 '+TOTAL_DONE+' 张';
 }catch(e){BUSY=false;$('batchStatus').textContent='重排失败：'+(e.message||e);}
}
function locateUnreviewed(){if(BUSY||ACTIVE!==null)return;const ids=[...CELL_BY_ID.keys()].filter(i=>!TREE.reviewed[i]&&!HOVER_PENDING.has(i));if(!ids.length){$('batchStatus').textContent='本筛选没有未审图片';return;}const i=ids.find(i=>i>LAST_LOCATED)??ids[0];LAST_LOCATED=i;LAST_POINT=null;document.querySelector('.located')?.classList.remove('located');const c=CELL_BY_ID.get(i);c.classList.add('located');c.scrollIntoView({block:'center'});$('batchStatus').textContent='红框是下一个未审，加载成功后划过确认';}
async function modalAction(path,data){
 if(BUSY||ACTIVE===null||!READY)return;
 BUSY=true;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=true;
 const i=ACTIVE;
 try{await flushHover();const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({i,...data})});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.err||'保存失败');BUSY=false;
  if(path==='/special')applyLocal(i,{special:true,reviewed:true,note:(data.note||'').trim()});
  else{applyLocal(i,{special:false,reviewed:false,note:'',answer:TREE.preds[i]||''});$('batchStatus').textContent='已撤销审核，这张回到 '+groupLabel(ROW_GROUP.get(i))+' 组';}
  closeModal();
 }catch(e){BUSY=false;$('mstatus').textContent=e.message;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=!READY;}
}
function markSpecial(){const note=$('mnote').value.trim();if(!note){$('mstatus').textContent='请先输入特殊标签';return;}return modalAction('/special',{note});}
function undoReview(){return modalAction('/unreview',{});}

$('mvalue').onkeydown=e=>{if(e.key==='Enter')saveModal(false);};document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();return;}if(e.ctrlKey||e.altKey||e.metaKey||BUSY||ACTIVE!==null||e.target.closest('input,textarea,select,[contenteditable]'))return;const k=e.key.toLowerCase();if(k==='r'){e.preventDefault();hardRefresh();return;}if(k==='f'){e.preventDefault();locateUnreviewed();return;}if(k==='a'||k==='d'){e.preventDefault();stepGroup(k==='a'?-1:1);}});$('modal').onclick=e=>{if(e.target===$('modal'))closeModal();};
(async()=>{try{loadBrush();await loadTree();const m=location.hash.startsWith('#n')?decodeURIComponent(location.hash.slice(2)):null;const ns=Object.keys(TREE.groups).sort((a,b)=>a==='__special__'?1:b==='__special__'?-1:a.localeCompare(b));go(m!==null&&ns.includes(m)?m:ns[0]);}catch(e){$('grid').textContent=e.message;}})();
</script></body></html>
"""


class Server(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


class H(BaseHTTPRequestHandler):
    # 复用连接：全量页一次要几百上千张图，HTTP/1.0 每张都要新建 TCP + 线程。
    protocol_version = "HTTP/1.1"

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
        if u.path == "/tree":
            global _TREE_CACHE
            with LOCK:
                if _TREE_CACHE is not None and _TREE_CACHE[0] == _STATE["version"]:
                    payload = _TREE_CACHE[1]
                else:
                    groups = {}
                    for i, row in enumerate(ROWS):
                        groups.setdefault(group_of(row), []).append(i)
                    data = {"groups": groups, "total": len(ROWS),
                            "files": [r["file"] for r in ROWS],
                            "subjects": [r["subject"] for r in ROWS],
                            "labels": [r["answer"] if r["reviewed"] else r["pred"] for r in ROWS],
                            "preds": [r["pred"] for r in ROWS],
                            "reviewed": [r["reviewed"] for r in ROWS],
                            "special": [r["special"] for r in ROWS],
                            "notes": [r["note"] or ("改涂/特殊建议，需人工决定" if r["special_suggestion"] else "") for r in ROWS],
                            "models": [r["model"] for r in ROWS],
                            "uncertain": [r["uncertain"] for r in ROWS], "crop_issue": [r["crop_issue"] for r in ROWS]}
                    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    _TREE_CACHE = (_STATE["version"], payload)
            return self._send(200, payload, "application/json; charset=utf-8")
        if u.path == "/img":
            try:
                i = int((q.get("i") or ["-1"])[0])
                if not 0 <= i < len(ROWS):
                    raise ValueError()
            except ValueError:
                return self._send(404, "not found")
            p = os.path.join(ROWS[i]["base"], "images", ROWS[i]["file"])
            if not os.path.isfile(p):
                return self._send(404, "not found")
            return self._send(200, read_image(p), "image/png", cache="private, max-age=3600")
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
            image_row = INDEX.get(os.path.splitext(file)[0])
            if image_row is None:
                return self._send(404, "not found")
            p = os.path.join(image_row["base"], "images", file)
            if not file or not os.path.isfile(p):
                return self._send(404, "not found")
            return self._send(200, read_image(p), "image/png", cache="no-store")
        return self._send(404, "404")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(200, json.dumps({"ok": False, "error": "bad json"}), "application/json")
        if not isinstance(body, dict):
            return self._send(200, '{"ok":false,"err":"bad json"}', "application/json")
        if u.path in ("/set", "/special", "/unreview", "/hover_batch"):
            try:
                with LOCK:
                    if u.path == "/hover_batch":
                        items = body.get("items")
                        if not isinstance(items, list) or not 1 <= len(items) <= 64:
                            raise ValueError("批次需1至64行")
                        for item in items:
                            if not isinstance(item, dict) or type(item.get("i")) is not int or not 0 <= item["i"] < len(ROWS) or not valid_answer(item.get("value")):
                                raise ValueError("批次参数错误")
                        results = []
                        for item in items:
                            row = ROWS[item["i"]]
                            try:
                                current = row["answer"] if row["reviewed"] else row["pred"]
                                if not row["reviewed"] and not row["special"] and current == item["value"]:
                                    review(row["name"], current, rebuild_groups=False)
                                results.append({"i": item["i"], "ok": True, "reviewed": row["reviewed"], "special": row["special"], "value": row["answer"] if row["reviewed"] else row["pred"]})
                            except Exception:
                                results.append({"i": item["i"], "ok": False})
                        out = {"ok": True, "results": results}
                    else:
                        i = body.get("i")
                        if type(i) is not int or not 0 <= i < len(ROWS):
                            raise ValueError("未知条目")
                        name = ROWS[i]["name"]
                        if u.path == "/set":
                            value = body.get("value")
                            if not valid_answer(value):
                                raise ValueError("答案只允许A至F且不可重复，按字母排序")
                            out = review(name, value)
                        elif u.path == "/special":
                            note = body.get("note")
                            if not isinstance(note, str) or not note.strip() or len(note) > 300:
                                raise ValueError("特殊标签请填写1到300字")
                            out = mark_special(name, note.strip())
                        else:
                            out = unreview(name)
            except Exception as e:
                out = {"ok": False, "err": str(e)}
            return self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
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
    global DATA, IMAGES, MANIFEST, SUBSET, ONLY_UNREVIEWED, ROWS, INDEX
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", action="append", help="可重复指定多个库，审核写回各自日志")
    ap.add_argument("--port", type=int, default=8905)
    ap.add_argument("--subset", help="只审核清单里的 stem,row")
    ap.add_argument("--unreviewed", action="store_true", help="只显示未审")
    args = ap.parse_args()
    libraries = [os.path.abspath(base) for base in (args.data or [DATA])]
    ONLY_UNREVIEWED = args.unreviewed
    if args.subset:
        SUBSET = set()
        with open(args.subset, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                SUBSET.add((r["stem"], int(r["row"])))
        if not SUBSET:
            raise ValueError("审核清单为空")
    print("正在读日志…")
    combined = []
    for DATA in libraries:
        IMAGES = os.path.join(DATA, "images")
        MANIFEST = os.path.join(DATA, "manifest.csv")
        if not os.path.isfile(MANIFEST):
            raise FileNotFoundError(MANIFEST)
        load()
        combined.extend(ROWS)
    if len({r["name"] for r in combined}) != len(combined):
        raise ValueError("多个库存在同名图片，不能合并审核")
    ROWS = combined
    INDEX = {r["name"]: r for r in ROWS}
    rebuild()
    print("  共 %d 行；已审 %d；特殊 %d" % (
        len(ROWS), sum(1 for r in ROWS if r["reviewed"]), sum(1 for r in ROWS if r["special"])), flush=True)
    print("  打开: http://127.0.0.1:%d/" % args.port, flush=True)
    Server(("127.0.0.1", args.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
