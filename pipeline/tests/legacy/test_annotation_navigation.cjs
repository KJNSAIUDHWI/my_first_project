// Run the page's actual navigation/confirmation functions without a browser or server.
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const html = fs.readFileSync(process.argv[2] || path.resolve(__dirname, '../../tools/标注工具/index.html'), 'utf8');
const source = html.slice(html.indexOf('let imageLoadId ='), html.indexOf('function fmtTime('));
const confirm = html.slice(html.indexOf('async function confirmCurrent()'), html.indexOf('async function cancelReview()'));
const images = [], els = {}, posts = [], drawn = [];
const context = {
  state: { readonly: true, dirty: false, idx: 0, list: [
    {name:'A.jpg',stem:'A',reviewed:true,legacy_review:true},
    {name:'B.jpg',stem:'B',reviewed:true,legacy_review:true}], boxes:[],img:null,displayed:null },
  saveChain: Promise.resolve(), save: async()=>{}, autoReviewCurrent:async()=>{}, isEdited:()=>false,
  document:{getElementById:id=>(els[id]??={})},
  fetch:async(url,opt)=>{
    if(opt){ posts.push(JSON.parse(opt.body)); return {ok:true,json:async()=>({review_count:2})}; }
    const name=url.includes('stem=A')?'A':'B';
    return {ok:true,json:async()=>({text:name,image_sha256:'hash-'+name,review_state:'reviewed_legacy'})};
  },
  parseLabel:text=>[text], labelText:()=>context.state.boxes.join(),
  fit:()=>{},draw:()=>drawn.push(context.state.img?.src || null),updateStatus:()=>{},
  toast:()=>{},recount:()=>{},renderBoxList:()=>{},clearTimeout:()=>{},ctx:{fillText:()=>{}},
  Image:class{constructor(){images.push(this)}}
};
vm.createContext(context);vm.runInContext(source+'\n'+confirm,context);
(async()=>{
  const results=[];
  await vm.runInContext('goTo(0,false)',context);
  await vm.runInContext('confirmCurrent()',context);
  assert.equal(posts.length,0);results.push('loading cannot confirm');
  images[0].onload();assert.equal(context.state.displayed,'hash-A');
  await vm.runInContext('goTo(1,false)',context);
  assert.equal(context.state.displayed,null);assert.equal(context.state.img,null);
  assert.equal(context.state.boxes.length,1);assert.equal(context.state.selected,-1);
  assert.equal(context.state.undo.length,0);assert.equal(context.state.redo.length,0);results.push('navigation clears edit and display identity');
  images[1].onload();images[0].onload();
  assert.equal(context.state.list[context.state.idx].stem,'B');assert.equal(context.state.displayed,'hash-B');
  assert.equal(context.state.boxes[0],'B');assert.equal(context.state.img,images[1]);results.push('late onload ignored');
  images[0].onerror();assert.equal(context.state.img,images[1]);results.push('late onerror ignored');
  await vm.runInContext('confirmCurrent()',context);assert.equal(posts.length,1);
  assert.equal(posts[0].stem,'B');assert.equal(posts[0].displayed_sha256,'hash-B');results.push('normal confirmation uses current image');
  images[1].onerror();assert.equal(context.state.displayed,null);assert.equal(context.state.img,null);
  await vm.runInContext('confirmCurrent()',context);assert.equal(posts.length,1);results.push('failed image cannot confirm');
  let release; context.fetch=async()=>({ok:true,json:()=>new Promise(resolve=>release=resolve)});
  const pending=vm.runInContext('goTo(0,false)',context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(context.state.boxes.length,0);assert.equal(context.state.selected,-1);
  assert.equal(context.state.undo.length,0);assert.equal(context.state.redo.length,0);
  assert.equal(context.state.displayed,null);assert.equal(context.state.img,null);
  release({text:'A',image_sha256:'hash-A',review_state:'reviewed_legacy'});await pending;
  results.push('metadata wait contains no old editable boxes');
  console.log(JSON.stringify({passed:true,cases:results},null,2));
})().catch(e=>{console.error(e);process.exitCode=1});
