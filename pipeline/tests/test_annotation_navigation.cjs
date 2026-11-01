const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const html = fs.readFileSync(path.resolve(__dirname, '../../tools/标注工具/index.html'), 'utf8').replace(/\r\n/g, '\n');
const script = html.slice(html.indexOf('<script>')+8, html.lastIndexOf('</script>'));
const runSource = script.slice(0, script.lastIndexOf('resize();\nloadDatasets()'));
const base = [[5,.5,.5,.4,.3]], modified = [[5,.6,.5,.4,.3]];
async function scenario(filter) {
  const elements = {}, handlers = {}, posts = [], urls=[];
  const filterNodes=['correct','fixed','special','pending'].map(filter=>({dataset:{filter}}));
  const ctx = new Proxy({}, {get:(_, key)=>key==='measureText'?()=>({width:20}):()=>{}});
  function element() {return {style:{},classList:{add(){},remove(){},toggle(){}},getContext:()=>ctx,
    getBoundingClientRect:()=>({width:1000,height:800}),appendChild(){},querySelector:()=>element(),
    addEventListener(){},blur(){},focus(){},showModal(){this.open=true;},close(){this.open=false;}};}
  const initial=filter==='fixed'?modified:base, target=filter==='fixed'?base:modified;
  const kind=boxes=>JSON.stringify(boxes)===JSON.stringify(base)?'已审-正确':'已审-修正';
  const server = {A:{boxes:initial,reviewed:true,status:kind(initial)},B:{boxes:base,reviewed:false,status:'已标'}};
  const list=()=>Object.entries(server).map(([stem,row])=>({stem,name:stem+'.jpg',reviewed:row.reviewed,status:row.status,special:!!row.special,note:row.note||''}));
  const counts=()=>({correct:Object.values(server).filter(r=>r.status==='已审-正确').length,
    fixed:Object.values(server).filter(r=>r.status==='已审-修正').length,pending:1,special:0});
  const sandbox = {console,URLSearchParams,location:{pathname:'/',search:filter?'?filter='+filter:''},Map,
    history:{replaceState:(_,__,url)=>{urls.push(url);sandbox.location.search=url.includes('?')?'?'+url.split('?')[1]:'';}},
    setTimeout:()=>{},window:{addEventListener:(k,f)=>handlers[k]=f,open:()=>{throw Error('Must not open another window');}},
    document:{getElementById:id=>elements[id]??=element(),querySelectorAll:()=>filterNodes,createElement:element},
    Image:class {constructor(){this.width=1000;this.height=800;} set src(v){this._src=v;queueMicrotask(()=>this.onload());}},
    fetch:async(url,opt)=>{
      if(opt) {
        const body=JSON.parse(opt.body);posts.push({url,...body});
        if(url==='/api/special') {
          const row=server[body.stem];row.special=true;row.note=body.note;row.status='特殊';
          row.boxes=body.text.trim().split('\n').filter(Boolean).map(l=>l.split(' ').map(Number));
          return {ok:true,json:async()=>({ok:true})};
        }
        if(url==='/api/review') {
          if(sandbox.failReview) return {ok:false,json:async()=>({error:'模拟写入失败'})};
          const row=server[body.stem];row.boxes=body.text.trim().split('\n').filter(Boolean).map(l=>l.split(' ').map(Number));
          row.reviewed=true;row.status=kind(row.boxes);
          return {ok:true,json:async()=>({status:row.status,review_count:2,last_review:'now',edit:{total:0}})};
        }
      }
      if(url.startsWith('/api/label')) {
        const row=server[new URLSearchParams(url.split('?')[1]).get('stem')];
        return {ok:true,json:async()=>({text:row.boxes.map(b=>b.join(' ')).join('\n'),reviewed:row.reviewed})};
      }
      if(url==='/api/list') return {ok:true,json:async()=>({data:'C:\\fake\\m3',items:list(),counts:counts(),edits:{}})};
      throw Error('Unexpected API '+url);
    }};
  vm.createContext(sandbox);vm.runInContext(runSource,sandbox);
  const exec = code=>vm.runInContext(code,sandbox);
  exec(`state.list = ${JSON.stringify(list())}; state.counts = ${JSON.stringify(counts())}`);
  await exec('loadImage(0)');const frozen=exec('JSON.stringify(state.counts)');
  exec(`state.boxes = ${JSON.stringify(target)}; markChanged()`);await exec('navChain');
  assert.equal(server.A.status,kind(initial));assert.equal(posts.length,0);
  exec(`state.undo.push(${JSON.stringify(initial)}); undo(); redo()`);await exec('navChain');assert.equal(posts.length,0);
  handlers.keydown({key:'ArrowRight',target:{tagName:'BODY'},preventDefault(){}});await exec('navChain');
  assert.equal(exec('state.idx'),1);assert.equal(posts.length,0);assert.equal(server.B.reviewed,false);
  handlers.keydown({key:'ArrowLeft',target:{tagName:'BODY'},preventDefault(){}});await exec('navChain');
  assert.equal(exec('JSON.stringify(state.boxes)'),JSON.stringify(target));assert.equal(posts.length,0);
  let warned=false;handlers.beforeunload({preventDefault(){warned=true;}});assert(warned);
  handlers.keydown({key:'d',target:{tagName:'BODY'}});await exec('navChain');
  assert.equal(posts.length,1);assert.equal(server.A.status,kind(target));assert.equal(exec('state.idx'),1);
  assert.equal(exec('state.list.length'),2);assert.equal(exec('JSON.stringify(state.counts)'),frozen);
  handlers.keydown({key:'ArrowLeft',target:{tagName:'BODY'},preventDefault(){}});await exec('navChain');
  assert.equal(exec('JSON.stringify(state.boxes)'),JSON.stringify(target));assert.equal(exec('state.list[0].status'),kind(target));
  assert(elements.curStatus.innerHTML.includes(kind(target)==='已审-修正'?'手动修改':'预标注正确'));
  // The only/last item must still save even though D cannot change the index.
  exec('state.list = [state.list[0]]; state.idx=0');
  exec(`state.boxes = ${JSON.stringify(initial)}; markChanged()`);
  handlers.keydown({key:'d',target:{tagName:'BODY'}});await exec('navChain');
  assert.equal(exec('state.idx'),0);assert.equal(posts.length,2);assert.equal(server.A.status,kind(initial));assert.equal(exec('isEdited()'),false);
  sandbox.failReview=true;exec(`state.boxes = ${JSON.stringify(target)}; markChanged()`);
  handlers.keydown({key:'d',target:{tagName:'BODY'}});await exec('navChain');
  assert.equal(exec('state.idx'),0);assert.equal(exec('isEdited()'),true);assert.equal(server.A.status,kind(initial));
  sandbox.failReview=false;elements.refreshBtn.onclick();await exec('navChain');
  assert.equal(server.A.status,kind(target));assert.equal(exec('state.drafts.size'),0);
  if(filter==='correct'||filter==='fixed') {assert.equal(exec('state.list.length'),0);assert.equal(exec('state.img'),null);}
  if(filter==='pending') {assert.equal(exec('state.list.length'),1);assert.equal(exec('state.list[0].stem'),'B');}
  elements.allBtn.onclick();await exec('navChain');assert.equal(exec('FILTER'),'');assert.equal(exec('state.list.length'),2);
  assert.equal(urls.at(-1),'/');
  filterNodes.find(n=>n.dataset.filter==='fixed').onclick();await exec('navChain');assert.equal(exec('FILTER'),'fixed');assert.equal(urls.at(-1),'/?filter=fixed');
  // Refresh commits drafts from images other than the displayed one too.
  elements.allBtn.onclick();await exec('navChain');exec(`state.boxes = ${JSON.stringify(initial)}; markChanged()`);
  handlers.keydown({key:'ArrowRight',target:{tagName:'BODY'},preventDefault(){}});await exec('navChain');
  const before=posts.length;elements.refreshBtn.onclick();await exec('navChain');
  assert.equal(posts.length,before+1);assert.equal(server.A.status,kind(initial));assert.equal(server.B.reviewed,false);
  // Label visibility never changes the underlying annotations or submits reviews.
  assert.equal(exec('labelMode'),'m3');
  const mixed = [[0,.1,.1,.1,.1],[5,.5,.5,.4,.3],[6,.5,.5,.04,.03],[4,.8,.8,.1,.1]];
  exec(`state.boxes=${JSON.stringify(mixed)}; state.selected=0; setLabelMode('m3')`);
  assert.equal(exec('JSON.stringify(boxOrder())'),'[1,2]');
  assert.equal(exec('state.selected'),-1);assert.equal(exec('state.cls'),5);
  assert(!elements.classCounts.innerHTML.includes('姓名学号'));
  assert(elements.classCounts.innerHTML.includes('填空题大题号'));
  assert.equal(elements.boxCount.textContent,'(2 / 全部 4)');
  assert.equal(exec('hitBox({x:100,y:80})'),-1);
  const writes=posts.length;
  handlers.keydown({key:'1',target:{tagName:'BODY'}});assert.equal(exec('state.cls'),5);
  handlers.keydown({key:'2',target:{tagName:'BODY'}});assert.equal(exec('state.cls'),6);
  handlers.keydown({key:'7',target:{tagName:'BODY'}});assert.equal(exec('state.cls'),6);
  assert.equal(elements.classKeyHelp.textContent,'1~2');
  for (const [mode, classes] of Object.entries({m1:[0,1,4],m2:[2,3],m3:[5,6],all:[0,1,2,3,4,5,6]})) {
    exec(`setLabelMode('${mode}')`);
    classes.forEach((cls,index)=>{
      handlers.keydown({key:String((index+1)%10),target:{tagName:'BODY'}});
      assert.equal(exec('state.cls'),cls,`${mode} shortcut ${index+1}`);
    });
    if(mode!=='all') {
      handlers.keydown({key:'0',target:{tagName:'BODY'}});
      assert.equal(exec('state.cls'),classes.at(-1));
    }
  }
  handlers.keydown({key:'ArrowRight',target:{tagName:'SELECT'},preventDefault(){throw Error('select navigation intercepted');}});
  elements.labelMode.onchange({target:{value:'all'}});
  assert.equal(exec('boxOrder().length'),4);assert.equal(exec('JSON.stringify(state.boxes)'),JSON.stringify(mixed));
  assert.equal(posts.length,writes);assert(elements.classCounts.innerHTML.includes('姓名学号'));
  exec("setLabelMode('m3')");
  await exec('saveReview(state.list[state.idx], JSON.stringify(state.boxes))');
  assert.equal(posts.at(-1).text.trim().split('\n').length,4);
  assert.equal(exec('new Set(CLASSES.map(c=>c[1])).size'),7);
  assert.equal(exec("Object.hasOwn(LABEL_MODES, 'm4')"),false);
  exec("state.boxes.push([8,.5,.5,.1,.1]); setLabelMode('all')");
  assert.equal(exec("classInfo(8)[0]"),'旧类别 8');
  assert.equal(exec('boxOrder().length'),5);
  exec("state.boxes.pop(); setLabelMode('m3')");
  // A special submission includes edited boxes and consumes its old ordinary draft.
  exec(`state.boxes=${JSON.stringify(modified)}; markChanged(); special()`);
  assert.equal(exec('state.drafts.size'),1);
  elements.specialNote.value='切分有问题';
  await exec('submitSpecial({preventDefault(){}})');
  assert.equal(exec('state.drafts.size'),0);
  const specialStem=exec('state.list[state.idx].stem');
  assert(server[specialStem].special);
  assert.deepEqual(server[specialStem].boxes,modified);
  const afterSpecial=posts.length;
  await exec('flushDrafts()');await exec('go(1)');
  assert.equal(posts.length,afterSpecial);assert(server[specialStem].special);
  await exec("changeFilter('special')");
  assert.equal(posts.length,afterSpecial);assert.equal(exec('state.list.length'),1);
  return filter||'all';
}
(async()=>{const passed=[];for(const filter of ['', 'correct','fixed','pending'])passed.push(await scenario(filter));
  console.log(JSON.stringify({passed,checks:['editing/undo/redo never submit','arrows preserve drafts without submitting','A/D saves and retains subset membership with latest label','last or only image saves at boundary','failed save preserves changes','refresh flushes all drafts then regroups','return-all and filters stay in same tab','M3 defaults and filters drawing/list/hits/shortcuts','common mode restores all labels; saves preserve hidden boxes','seven active class colors; legacy boxes preserved']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
