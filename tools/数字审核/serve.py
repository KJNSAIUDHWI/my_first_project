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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
from common import row_log  # noqa: E402
from common.digit_classes import MAX_DIGIT

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
        source = list(csv.DictReader(f))
    with ThreadPoolExecutor(max_workers=12) as pool:
        states = pool.map(lambda r: row_log.digit_state(row_log.read(DATA, stem_of(r['file']))), source)
        for r, st in zip(source, states):
            r["label"] = int(r["label"])
            if not 0 <= r["label"] <= MAX_DIGIT:
                raise ValueError("数字标签超出0~60: %s" % r.get("file", ""))
            r["_stem"] = stem_of(r["file"])
            r["_label"] = int(st["label"]) if st["label"] is not None else r["label"]
            r["_pred"] = st["pred_label"]
            r["_reviewed"] = st["reviewed"]
            r["_special"] = st["special"]
            r["_note"] = st["note"]
            r["_round"] = st["review_count"]
            r["_uncertain"] = st["uncertain"]
            r["_crop_issue"] = st["crop_issue"]
            r["_model"] = st["model"]
            rows.append(r)
    ROWS = rows
    rebuild()


def rebuild():
    global GROUPS
    g = {}
    for i, r in enumerate(ROWS):
        g.setdefault(-1 if r["_special"] else r["_label"], []).append(i)
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


PAGE = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>数字审核</title>
<style>
*{box-sizing:border-box}body{margin:0;font:13px system-ui,"Microsoft YaHei";color:#222;background:white}header{position:sticky;top:0;z-index:20;background:white;padding:6px 10px;border-bottom:1px solid #ccc}.top,.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}h1{font-size:14px;margin:0;flex:1}.bar{color:#666;font-size:12px;margin:3px 0}.nums{display:flex;flex-wrap:wrap;gap:3px;margin:4px 0;max-height:68px;overflow:auto}button,select{cursor:pointer;border:1px solid #aaa;border-radius:3px;background:white;padding:4px 8px;color:#222}button:disabled{opacity:.4;cursor:default}.nums button{min-width:34px;padding:2px 5px;font-size:12px}.nums i{display:block;font-size:10px;color:#888;font-style:normal}.nums .on{background:#111;color:white}.nums .on i{color:#ccc}.brush-controls{display:inline-flex;gap:6px;align-items:center;font-size:12px}.brush-controls input{width:76px}.brush-outline{position:fixed;display:none;z-index:25;pointer-events:none;border:2px solid #1867d6;background:rgba(24,103,214,.10);border-radius:3px}.primary{background:#1867d6;border-color:#1867d6;color:white}#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(47px,1fr));gap:4px;padding:6px;align-items:start}.cell{position:relative;height:46px;border:1px solid #aaa;background:#fafafa;cursor:pointer;display:flex;align-items:center;justify-content:center}.cell img{width:94%;height:38px;object-fit:contain;image-rendering:pixelated}.cell .tag{position:absolute;left:3px;top:1px;font-size:10px;color:#555;background:#ffffffda;padding:0 2px}.cell .pick{position:absolute;right:2px;bottom:2px;cursor:pointer}.cell:has(.pick:checked){outline:2px solid #1867d6;outline-offset:-2px;background:#eaf2ff}.cell.pending{box-shadow:inset 0 0 0 2px #efb541}.cell.save-error{box-shadow:inset 0 0 0 2px #d22}.cell.located{outline:3px solid #e21;z-index:1}.cell.done{background:#d8efdf;border-color:#4b9c68}.cell.done img{opacity:.45}.cell.done .tag{color:#18703a;font-weight:bold}.cell.special{background:#e9ddf5;border-color:#9164b6}.cell .pick{width:11px;height:11px;margin:0}.cell .flags{font-size:8px;max-width:30px;overflow:hidden;white-space:nowrap}.nums .complete{background:#d8efdf;color:#18703a}.nums .complete.on{background:#18703a;color:white}.nums button{min-width:64px}.cell.issue{border-color:#d78b23}.cell .flags{position:absolute;left:3px;bottom:1px;color:#a65e00;font-size:10px}#modal{position:fixed;inset:0;z-index:50;background:#0007;display:none;align-items:center;justify-content:center}.mbox{background:white;border-radius:6px;padding:18px;width:min(560px,94vw);box-shadow:0 10px 40px #0005}#mhead{font-size:12px;color:#666;overflow-wrap:anywhere}#mimg{display:block;margin:16px auto;height:180px;width:90%;object-fit:contain;max-height:38vh;image-rendering:pixelated}#mvalue{font-size:26px;width:100px;text-align:center;padding:5px}.mact{display:flex;gap:8px;align-items:center;flex-wrap:wrap}#mstatus{min-height:24px;padding-top:8px;color:#666}.hint{font-size:11px;color:#777}.more{height:46px}
</style></head><body><header>
<div class="top"><h1>数字审核 · 当前 <span id="cur">—</span></h1><label><input type="checkbox" id="hover" checked onchange="clearBrush();LAST_POINT=null;">划过即确认</label><label><input type="checkbox" id="only" onchange="refreshView()">只看未审</label></div>
<div class="bar" id="bar"></div><div class="nums" id="nums"></div>
<div class="actions"><select id="priority" onchange="refreshView()"><option value="all">全部图片</option><option value="uncertain">模型疑难</option><option value="crop">裁图异常</option><option value="clear">清晰、裁图正常</option></select><button onclick="selectVisible()">选中屏幕可见图片</button><button onclick="clearPicks()">取消选择</button><button id="bulk" class="primary" onclick="confirmSelected()">确认选中正确</button><button onclick="locateUnreviewed()">下一个未审（F）</button><button id="retryHover" hidden onclick="retryHover()">重试保存</button><span class="brush-controls"><label>列 <input id="brushCols" type="range" min="1" max="6" step="1" value="1" aria-label="审核块列数" oninput="changeBrush()"></label><label>行 <input id="brushRows" type="range" min="1" max="6" step="1" value="1" aria-label="审核块行数" oninput="changeBrush()"></label><b id="brushSize">1列 × 1行</b></span><span id="batchStatus" role="status"></span></div>
<div class="hint">划过已加载图片即确认，绿色表示已审 · 点图修改/特殊标记 · 撤销在弹窗内 · 蓝框为审核块（鼠标所在图为左上角，仅确认屏幕内已加载图） · 黄色待保存，绿色已保存 · F 定位未审 · A / D 切换数字 · 每组全部展示，绿色已审可跳过</div></header><div id="grid"></div><div id="brushOutline" class="brush-outline" aria-hidden="true"></div>
<div id="modal" role="dialog" aria-modal="true" aria-label="修改数字"><div class="mbox"><div id="mhead"></div><img id="mimg" alt="题号放大图"><div class="mact"><input id="mvalue" aria-label="题号" type="number" min="0" max="60"><button id="msave" class="primary" onclick="saveModal(false)" disabled>确认 / 保存修改</button><button id="mzero" onclick="saveModal(true)" disabled>非数字</button><button onclick="closeModal()">关闭</button></div><div style="margin-top:12px;display:flex;gap:6px"><input id="mnote" aria-label="特殊标签" placeholder="特殊标签，例如：裁切不完整、看不清" maxlength="300" style="flex:1;min-width:0"><button id="mspecial" onclick="markSpecial()" disabled>保存特殊标签</button><button onclick="undoReview()">撤销审核</button></div><div id="mstatus" role="status"></div><div class="hint">Enter 保存 · Esc 关闭 · 0 表示非数字</div></div></div>
<script>
let TREE={groups:{},labels:[],reviewed:[]},CUR=null,ACTIVE=null,READY=false,BUSY=false;
const HOVER_PENDING=new Set(),HOVER_QUEUE=new Map(),HOVER_FAILED=new Map(),GROUP_PROGRESS=new Map(),GROUP_BUTTONS=new Map(),ROW_GROUP=new Map(),CELL_BY_ID=new Map();
const GRID_CELLS=[];let GRID_GEOMETRY=null;
let HOVER_TIMER=null,HOVER_FLIGHT=null,PROGRESS_TIMER=null,TOTAL_DONE=0,LAST_POINT=null,LAST_LOCATED=-1;

const $=id=>document.getElementById(id);
async function loadTree(){const r=await fetch('/tree');if(!r.ok)throw Error('读取失败');TREE=await r.json();indexProgress();renderGroups();}
function indexProgress(){GROUP_PROGRESS.clear();ROW_GROUP.clear();TOTAL_DONE=0;for(const [n,ids] of Object.entries(TREE.groups)){let done=0;for(const i of ids){ROW_GROUP.set(i,Number(n));if(TREE.reviewed[i])done++;}GROUP_PROGRESS.set(Number(n),{total:ids.length,done,pending:ids.length-done});TOTAL_DONE+=done;}}
function groupStats(n){return GROUP_PROGRESS.get(n)||{total:0,done:0,pending:0};}
function updateProgress(){for(const [n,b] of GROUP_BUTTONS){const s=groupStats(n);b.querySelector('i').textContent='已审 '+s.done+'/'+s.total;b.classList.toggle('complete',s.pending===0);b.title='已审 '+s.done+' 张 / 共 '+s.total+' 张 / 待审 '+s.pending+' 张';}$('bar').textContent='合计 '+TREE.total+' 张 / 已审 '+TOTAL_DONE+' / 待审 '+(TREE.total-TOTAL_DONE);currentProgress();}
function scheduleProgress(){if(PROGRESS_TIMER===null)PROGRESS_TIMER=setTimeout(()=>{PROGRESS_TIMER=null;updateProgress();},100);}
function currentProgress(){const s=groupStats(CUR);$('cur').textContent=(CUR===-1?'特殊':CUR===0?'非数字':CUR)+' · 已审 '+s.done+'/'+s.total+' · 待审 '+s.pending;}
function renderGroups(){const ns=Object.keys(TREE.groups).map(Number).sort((a,b)=>a-b);$('nums').replaceChildren();GROUP_BUTTONS.clear();for(const n of ns){const s=groupStats(n),b=document.createElement('button');b.innerHTML='<b>'+(n===-1?'特殊':n===0?'非数字':n)+'</b><i>已审 '+s.done+'/'+s.total+'</i>';b.title='已审 '+s.done+' 张 / 共 '+s.total+' 张 / 待审 '+s.pending+' 张';b.className=(n===CUR?'on ':'')+(s.pending===0?'complete':'');b.onclick=()=>go(n);$('nums').append(b);GROUP_BUTTONS.set(n,b);}$('bar').textContent='合计 '+TREE.total+' 张 / 已审 '+TOTAL_DONE+' / 待审 '+(TREE.total-TOTAL_DONE);currentProgress();}
function go(n){if(BUSY||ACTIVE!==null)return;CUR=n;LAST_POINT=null;LAST_LOCATED=-1;location.hash='n'+n;renderGroups();render();window.scrollTo(0,0);}
function stepGroup(delta){const ns=Object.keys(TREE.groups).map(Number).sort((a,b)=>a-b);const pos=ns.indexOf(CUR)+delta;if(pos>=0&&pos<ns.length)go(ns[pos]);}
function refreshView(){if(BUSY)return;render();}
function clearPicks(){document.querySelectorAll('.pick').forEach(p=>p.checked=false);updatePicks();}
function updatePicks(){const n=document.querySelectorAll('.pick:checked').length;$('bulk').textContent=n?'确认选中 '+n+' 张正确':'确认选中正确';$('bulk').disabled=BUSY||!n;}
function selectVisible(){if(BUSY)return;document.querySelectorAll('.cell').forEach(c=>{const p=c.querySelector('.pick'),r=c.getBoundingClientRect();if(p&&!p.disabled&&r.top>=$('grid').getBoundingClientRect().top&&r.top>=$('nums').parentElement.getBoundingClientRect().bottom&&r.bottom<=innerHeight)p.checked=true;});updatePicks();}
function render(){const priority=$('priority').value;const all=(TREE.groups[CUR]||[]).filter(i=>(CUR===-1||!$('only').checked||!TREE.reviewed[i])&&(priority==='all'||priority==='uncertain'&&TREE.uncertain[i]||priority==='crop'&&TREE.crop_issue[i]||priority==='clear'&&!TREE.uncertain[i]&&!TREE.crop_issue[i]));currentProgress();LAST_POINT=null;clearBrush();GRID_CELLS.length=0;GRID_GEOMETRY=null;CELL_BY_ID.clear();$('grid').replaceChildren();for(const i of all){const c=document.createElement('div');c.className='cell'+(TREE.reviewed[i]?' done':'')+(TREE.special[i]?' special':'')+(TREE.uncertain[i]||TREE.crop_issue[i]?' issue':'');c.title=TREE.files[i]+(TREE.notes[i]?' · '+TREE.notes[i]:'');c.dataset.i=i;c.dataset.pos=GRID_CELLS.length;GRID_CELLS.push(c);CELL_BY_ID.set(i,c);if(HOVER_PENDING.has(i))c.classList.add('pending');if(HOVER_FAILED.has(i))c.classList.add('save-error');const img=document.createElement('img');img.alt='题号 '+TREE.labels[i];img.loading='lazy';const pick=document.createElement('input');pick.type='checkbox';pick.className='pick';pick.dataset.i=i;pick.disabled=true;pick.setAttribute('aria-label','选择图片 '+i);pick.onclick=e=>e.stopPropagation();pick.onchange=updatePicks;img.onload=()=>{pick.disabled=BUSY||TREE.reviewed[i]||TREE.special[i];};img.onerror=()=>{pick.checked=false;pick.disabled=true;c.title+=' · 图片加载失败';updatePicks();};img.src='/img?i='+i;const tag=document.createElement('span');tag.className='tag';tag.textContent=TREE.special[i]?'特殊':(TREE.reviewed[i]?'✓ ':'')+(TREE.labels[i]===0?'非数字':TREE.labels[i]);const flags=document.createElement('span');flags.className='flags';flags.textContent=[TREE.uncertain[i]?'疑难':'',TREE.crop_issue[i]?'裁图':''].filter(Boolean).join(' · ');c.append(img,tag,flags,pick);c.onclick=()=>{if(!BUSY)openModal(i);};$('grid').append(c);}if(!all.length)$('grid').textContent='这组没有待审核图片';updatePicks();}
function openModal(i){clearBrush();LAST_POINT=null;ACTIVE=i;READY=false;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=true;$('mhead').textContent=TREE.files[i]+' · '+TREE.models[i]+(TREE.uncertain[i]?' · 模型疑难':'')+(TREE.crop_issue[i]?' · 裁图异常':'');$('mvalue').value=TREE.labels[i];$('mnote').value=TREE.notes[i]||'';$('mstatus').textContent='图片加载中…';$('modal').style.display='flex';const img=$('mimg');img.onload=()=>{if(ACTIVE!==i)return;READY=true;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=false;$('mstatus').textContent='核对题号，正确可直接确认';$('mvalue').focus();$('mvalue').select();};img.onerror=()=>{$('mstatus').textContent='图片加载失败，不能确认';};img.src='/img?i='+i;}
function closeModal(){if(BUSY)return;$('modal').style.display='none';ACTIVE=null;READY=false;}
async function post(i,value,hover=false){const r=await fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({i,value,hover})});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.err||'保存失败');return j;}
async function saveModal(zero){if(BUSY||!READY||ACTIVE===null)return;const v=zero?0:Number($('mvalue').value);if((!zero&&$('mvalue').value.trim()==='')||!Number.isInteger(v)||v<0||v>60){$('mstatus').textContent='请输入0到60的整数';return;}const i=ACTIVE;BUSY=true;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=true;try{await flushHover();await post(i,v);await loadTree();BUSY=false;closeModal();render();}catch(e){$('mstatus').textContent=e.message;BUSY=false;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=!READY;}}
async function confirmSelected(){if(BUSY)return;const picks=[...document.querySelectorAll('.pick:checked')].filter(p=>!p.disabled).map(p=>({i:Number(p.dataset.i),value:TREE.labels[Number(p.dataset.i)]}));if(!picks.length)return;BUSY=true;document.querySelectorAll('.pick').forEach(p=>p.disabled=true);updatePicks();let done=0;try{await flushHover();for(const p of picks){if(TREE.reviewed[p.i])continue;await post(p.i,p.value);done++;$('batchStatus').textContent='已确认 '+done+'/'+picks.length;}}catch(e){$('batchStatus').textContent='已确认 '+done+' 张，停止：'+e.message;}finally{BUSY=false;try{await loadTree();render();}catch(e){$('batchStatus').textContent+='；刷新失败，请刷新页面';}}}


function queueHover(i,cell,img){
 if(!$('hover').checked||BUSY||ACTIVE!==null||TREE.reviewed[i]||TREE.special[i]||HOVER_PENDING.has(i)||HOVER_FAILED.has(i)||!img.complete||!img.naturalWidth)return;
 HOVER_PENDING.add(i);HOVER_QUEUE.set(i,{i,value:TREE.labels[i]});cell.classList.add('pending');
 if(HOVER_TIMER===null&&!HOVER_FLIGHT)HOVER_TIMER=setTimeout(()=>{HOVER_TIMER=null;flushHover();},60);
}
function paintSaved(i){
 if(!TREE.reviewed[i]){TREE.reviewed[i]=true;TOTAL_DONE++;const s=groupStats(ROW_GROUP.get(i));s.done++;s.pending--;}
 const c=CELL_BY_ID.get(i);if(c){c.classList.remove('pending','save-error');c.classList.add('done');const pick=c.querySelector('.pick');pick.checked=false;pick.disabled=true;c.querySelector('.tag').textContent='✓ '+TREE.labels[i];}
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
function changeBrush(){LAST_POINT=null;clearBrush();const w=brushValue('brushCols'),h=brushValue('brushRows');$('brushSize').textContent=w+'列 × '+h+'行 · 最多'+(w*h)+'张';try{localStorage.setItem('digitBrush',JSON.stringify([w,h]));}catch(error){}}
function loadBrush(){try{const size=JSON.parse(localStorage.getItem('digitBrush'));if(Array.isArray(size)&&size.length===2){$('brushCols').value=Math.max(1,Math.min(6,Number(size[0])||1));$('brushRows').value=Math.max(1,Math.min(6,Number(size[1])||1));}}catch(error){}changeBrush();}
function brushAt(cell,submit){
 const geo=brushGeometry(),start=Number(cell.dataset.pos),w=brushValue('brushCols'),h=brushValue('brushRows'),rect=cell.getBoundingClientRect();
 const positions=brushPositions(start,geo.columns,GRID_CELLS.length,w,h),headerBottom=document.querySelector('header').getBoundingClientRect().bottom;
 if(submit){for(const pos of positions){const y=Math.floor(pos/geo.columns)-Math.floor(start/geo.columns),top=rect.top+y*(rect.height+geo.gapY);if(top<headerBottom||top+rect.height>innerHeight)continue;const target=GRID_CELLS[pos];queueHover(Number(target.dataset.i),target,target.querySelector('img'));}return;}
 const cols=Math.min(w,geo.columns-start%geo.columns),rows=positions.length?Math.floor(positions[positions.length-1]/geo.columns)-Math.floor(start/geo.columns)+1:0;
 const outline=$('brushOutline');Object.assign(outline.style,{display:rows?'block':'none',left:rect.left+'px',top:rect.top+'px',width:(cols*(rect.width+geo.gapX)-geo.gapX)+'px',height:Math.min(rows*(rect.height+geo.gapY)-geo.gapY,Math.max(0,innerHeight-rect.top))+'px'});
}
// 仅连接120ms内、同一分组且未发生滚动的真实移动；只确认轨迹实际经过且已加载的卡片。
function samplePath(from,to,visit){const distance=Math.hypot(to.x-from.x,to.y-from.y),steps=Math.max(1,Math.ceil(distance/12));for(let n=0;n<=steps;n++)visit(from.x+(to.x-from.x)*n/steps,from.y+(to.y-from.y)*n/steps);}
function trackHover(e){
 if(!e.isTrusted||!(e.movementX||e.movementY)||!$('hover').checked||BUSY||ACTIVE!==null){LAST_POINT=null;clearBrush();return;}
 const now={x:e.clientX,y:e.clientY,t:performance.now()},prev=LAST_POINT;LAST_POINT=now;const seen=new Set();
 samplePath(prev&&now.t-prev.t<=120?prev:now,now,(x,y)=>{const cell=document.elementFromPoint(x,y)?.closest('#grid .cell');if(!cell)return;const i=Number(cell.dataset.i);if(seen.has(i))return;seen.add(i);brushAt(cell,true);});const current=document.elementFromPoint(now.x,now.y)?.closest('#grid .cell');if(current)brushAt(current,false);else clearBrush();
}
$('grid').addEventListener('mousemove',trackHover);$('grid').addEventListener('mouseleave',()=>{LAST_POINT=null;clearBrush();});
document.addEventListener('scroll',()=>{LAST_POINT=null;clearBrush();},true);window.addEventListener('resize',()=>{GRID_GEOMETRY=null;LAST_POINT=null;clearBrush();});
window.addEventListener('beforeunload',e=>{if(HOVER_PENDING.size||HOVER_FAILED.size){e.preventDefault();e.returnValue='';}});
function locateUnreviewed(){if(BUSY||ACTIVE!==null)return;const ids=[...CELL_BY_ID.keys()].filter(i=>!TREE.reviewed[i]&&!HOVER_PENDING.has(i));if(!ids.length){$('batchStatus').textContent='本筛选没有未审图片';return;}const i=ids.find(i=>i>LAST_LOCATED)??ids[0];LAST_LOCATED=i;LAST_POINT=null;document.querySelector('.located')?.classList.remove('located');const c=CELL_BY_ID.get(i);c.classList.add('located');c.scrollIntoView({block:'center'});$('batchStatus').textContent='红框是下一个未审，加载成功后划过确认';}
async function modalAction(path,data){
 if(BUSY||ACTIVE===null||!READY)return;
 BUSY=true;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=true;
 try{await flushHover();const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({i:ACTIVE,...data})});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.err||'保存失败');await loadTree();BUSY=false;closeModal();render();}
 catch(e){BUSY=false;$('mstatus').textContent=e.message;$('msave').disabled=$('mzero').disabled=$('mspecial').disabled=!READY;}
}
function markSpecial(){const note=$('mnote').value.trim();if(!note){$('mstatus').textContent='请先输入特殊标签';return;}return modalAction('/special',{note});}
function undoReview(){return modalAction('/unreview',{});}

$('mvalue').onkeydown=e=>{if(e.key==='Enter')saveModal(false);};document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();return;}if(e.ctrlKey||e.altKey||e.metaKey||BUSY||ACTIVE!==null||e.target.closest('input,textarea,select,[contenteditable]'))return;const k=e.key.toLowerCase();if(k==='f'){e.preventDefault();locateUnreviewed();return;}if(k==='a'||k==='d'){e.preventDefault();stepGroup(k==='a'?-1:1);}});$('modal').onclick=e=>{if(e.target===$('modal'))closeModal();};
(async()=>{try{loadBrush();await loadTree();const m=location.hash.match(/^#n(-?\d+)$/);const ns=Object.keys(TREE.groups).map(Number).sort((a,b)=>a-b);go(m&&ns.includes(Number(m[1]))?Number(m[1]):ns[0]);}catch(e){$('grid').textContent=e.message;}})();
</script></body></html>
"""


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
                 "labels": labels, "reviewed": reviewed, "max_label": MAX_DIGIT,
                 "uncertain": [r['_uncertain'] for r in ROWS], "crop_issue": [r['_crop_issue'] for r in ROWS],
                 "special": [r['_special'] for r in ROWS], "notes": [r['_note'] for r in ROWS],
                 "models": [r['_model'] for r in ROWS]},
                ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/img":
            i = int((q.get("i") or ["-1"])[0])
            if i < 0 or i >= len(ROWS):
                return self._send(404, "not found")
            p = image_path(ROWS[i])
            if not os.path.isfile(p):
                return self._send(404, "not found")
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "image/png", cache="private, max-age=3600")
        return self._send(404, "404")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/set", "/special", "/unreview", "/hover_batch"):
            return self._send(404, "404")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(200, json.dumps({"ok": False, "err": "bad json"}), "application/json")
        if not isinstance(body, dict):
            return self._send(200, '{"ok":false,"err":"bad json"}', "application/json")
        if u.path == "/hover_batch":
            items = body.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= 64 or any(
                not isinstance(x, dict) or type(x.get("i")) is not int or not 0 <= x["i"] < len(ROWS)
                or type(x.get("value")) is not int or not 0 <= x["value"] <= MAX_DIGIT for x in items):
                return self._send(200, '{"ok":false,"err":"invalid batch"}', "application/json")
            results = []
            with LOCK:
                for item in items:
                    i, value = item["i"], item["value"]
                    row = ROWS[i]
                    try:
                        if not row["_reviewed"] and not row["_special"] and row["_label"] == value:
                            nround = log_review(row, value)
                            row.update(_reviewed=True, _round=nround)
                        results.append({"i": i, "ok": True, "reviewed": row["_reviewed"], "special": row["_special"], "value": row["_label"]})
                    except Exception:
                        results.append({"i": i, "ok": False})
            # 划过只确认原标签，分组成员不变，无须重建整库分组。
            return self._send(200, json.dumps({"ok": True, "results": results}), "application/json")
        i = body.get("i")
        v = body.get("value")
        if type(i) is not int or i < 0 or i >= len(ROWS):
            return self._send(200, json.dumps({"ok": False, "err": "未知条目"}), "application/json")
        if u.path == "/set" and (type(v) is not int or not 0 <= v <= MAX_DIGIT):
            return self._send(200, json.dumps({"ok": False, "err": "值要 0~60"}), "application/json")
        with LOCK:
            row = ROWS[i]
            if u.path == "/special":
                note = body.get("note")
                if not isinstance(note, str) or not note.strip() or len(note) > 300:
                    return self._send(200, json.dumps({"ok": False, "err": "特殊标签请填写1到300字"}, ensure_ascii=False), "application/json")
                row_log.append(DATA, row["_stem"], {"事件": "特殊", "类型": note.strip(), "标签": row["_label"], "说明": "人工标记特殊，不进入训练"})
                row.update(_special=True, _note=note.strip(), _reviewed=True)
                rebuild()
                return self._send(200, '{"ok":true}', "application/json")
            if u.path == "/unreview":
                row_log.append(DATA, row["_stem"], {"事件": "撤销审核", "说明": "人工撤销数字审核或特殊标记"})
                row.update(_special=False, _note="", _reviewed=False)
                rebuild()
                return self._send(200, '{"ok":true}', "application/json")
            if body.get("hover") and (row["_reviewed"] or row["_special"] or row["_label"] != v):
                return self._send(200, '{"ok":true,"unchanged":true}', "application/json")
            old = ROWS[i]["_label"]
            try:
                nround = log_review(ROWS[i], v)
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "err": "审核记录写入失败: %s" % e}),
                                  "application/json")
            ROWS[i]["_label"] = v
            ROWS[i]["_reviewed"] = True
            ROWS[i]["_special"] = False
            ROWS[i]["_note"] = ""
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
    print("  共 %d 框（标签0~60均可审核，当前0标签 %d 框）"
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
