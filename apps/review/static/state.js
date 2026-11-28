const CLIENT_ID = "c_" + Math.random().toString(36).slice(2, 10);
const URL_PARAMS = new URLSearchParams(location.search);
let REVIEWER = (URL_PARAMS.get("reviewer") || localStorage.getItem("reviewUserName") || "").trim();
if (REVIEWER) localStorage.setItem("reviewUserName", REVIEWER);

/* ==================== 游戏风自定义键位（支持主/副双键位） ==================== */
const DEFAULT_KEYBINDS = {
  nextSubmit: ["d", "arrowright"],
  prevPage:   ["a", "arrowleft"],
  toggleMode: ["e", ""],
  fitCanvas:  ["f", ""],
  toggleAns:  ["q", ""],
  nextLowConf:["l", ""],
  deleteBox:  ["delete", "backspace"],
  recognizeBox:["r", ""],
  cls_0:      ["1", ""],
  cls_1:      ["2", ""],
  cls_4:      ["3", ""],
  cls_2:      ["4", ""],
  cls_3:      ["5", ""],
  cls_5:      ["6", ""],
  cls_6:      ["7", ""],
};
const KEYBIND_LABELS = [
  ["nextSubmit", "保存并看下一张试卷"],
  ["prevPage",   "上一张试卷"],
  ["toggleMode", "切换「看答案 / 调框模式」"],
  ["fitCanvas",  "原卷适配窗口（复位缩放）"],
  ["toggleAns",  "开关「显示答案」"],
  ["nextLowConf","一键定位下一个低置信度项"],
  ["deleteBox",  "删除选中框（调框模式，也可右键单击删）"],
  ["recognizeBox","调用 API 识别选中框（调框模式大题/姓名框）"],
  ["cls_0",      "调框类别：姓名学号"],
  ["cls_1",      "调框类别：选择题区"],
  ["cls_4",      "调框类别：非选答题区"],
  ["cls_2",      "调框类别：选择题号"],
  ["cls_3",      "调框类别：选择选项"],
  ["cls_5",      "调框类别：单题框"],
  ["cls_6",      "调框类别：单题题号"],
];

function cloneDefaultKeybinds() {
  const out = {};
  for (const [k, v] of Object.entries(DEFAULT_KEYBINDS)) out[k] = [v[0] || "", v[1] || ""];
  return out;
}
function loadKeybinds() {
  const base = cloneDefaultKeybinds();
  try {
    const raw2 = localStorage.getItem("reviewKeybinds_v2");
    if (raw2) {
      const saved = JSON.parse(raw2);
      for (const k of Object.keys(base)) {
        if (Array.isArray(saved[k])) base[k] = [saved[k][0] || "", saved[k][1] || ""];
        else if (typeof saved[k] === "string") base[k] = [saved[k], ""];
      }
      return base;
    }
    const raw1 = localStorage.getItem("reviewKeybinds_v1");
    if (raw1) {
      const old = JSON.parse(raw1);
      for (const k of Object.keys(base)) {
        if (typeof old[k] === "string") base[k][0] = old[k];
      }
    }
  } catch {}
  return base;
}
function saveKeybinds(kb) {
  localStorage.setItem("reviewKeybinds_v2", JSON.stringify(kb));
}
function fmtKey(k) {
  if (!k) return "未设";
  if (k === " ") return "Space";
  if (k === "delete") return "Del";
  if (k === "backspace") return "Bksp";
  if (k === "arrowright") return "→";
  if (k === "arrowleft") return "←";
  if (k === "arrowup") return "↑";
  if (k === "arrowdown") return "↓";
  return k.length === 1 ? k.toUpperCase() : k;
}
function primaryKeyLabel(act) {
  const pair = state.keybinds[act] || ["", ""];
  return fmtKey(pair[0] || pair[1] || "");
}
function matchActionKey(act, keyLower) {
  if (!keyLower) return false;
  const pair = state.keybinds[act] || ["", ""];
  return pair[0] === keyLower || pair[1] === keyLower;
}

const LAYER_META = {
  m1: {
    label: "版面总区",
    code: "M1",
    color: "#2563eb",
    desc: "整页的三大分区：姓名学号、选择题区、非选答题区（大范围容器框，用虚线与单题区分）。",
  },
  m2: {
    label: "选择细框",
    code: "M2",
    color: "#0891b2",
    desc: "选择题区内部细分的每道小题号与涂卡选项框，平时默认隐藏以保持卷面清爽。",
  },
  m3: {
    label: "非选单题",
    code: "M3",
    color: "#7c3aed",
    desc: "非选答题区内部的每一道填空/简答/计算单题框及其对应印刷题号。",
  },
};
const LAYER_ORDER = ["m1", "m2", "m3"];

const BOX_META = {
  0: {
    name: "姓名学号",
    stage: "m1",
    color: "#dc2626",
    aiCapable: true,
    desc: "框选考生姓名、学号或考号条栏。画完选中后按快捷键可直接调用 AI 识别文字。",
  },
  1: {
    name: "选择题区",
    stage: "m1",
    color: "#16a34a",
    isZone: true,
    desc: "框住整块客观选择题的总区域；系统会在该区域内自动细切每道题号与涂卡选项。",
  },
  4: {
    name: "非选答题区",
    stage: "m1",
    color: "#2563eb",
    isZone: true,
    desc: "框住整块非选择题的答题总区域（填空、实验、计算等大区）；具体每一道题请用下方「单题框」单独标出。",
  },
  2: {
    name: "选择题号",
    stage: "m2",
    color: "#db2777",
    desc: "框选选择题区里单个印刷题号（如 1、2、3），用于与旁边的涂卡选项一一配对。",
  },
  3: {
    name: "选择选项",
    stage: "m2",
    color: "#0891b2",
    desc: "框选单道选择题的一排涂卡选项（如 [A][B][C][D]），用于识别考生填涂结果。",
  },
  5: {
    name: "单题框",
    stage: "m3",
    color: "#7c3aed",
    aiCapable: true,
    desc: "框住单独一道填空题或解答题的完整作答区。画完选中后按快捷键可直接调用 AI 识别手写答案。",
  },
  6: {
    name: "单题题号",
    stage: "m3",
    color: "#ea580c",
    desc: "框选单道非选择题的印刷题号（如 13.、20.），让系统自动将题号匹配给对应单题框。",
  },
};
const CLS_ORDER = [0, 1, 4, 2, 3, 5, 6];

const state = {
  keybinds: loadKeybinds(),
  listeningAction: null,
  listeningSlot: 0,
  result: null,
  feedback: { revision: 0, items: {} },
  locks: {},
  pages: [],
  pageIdx: 0,
  mode: "view",          // "view" | "edit"
  activeCls: 4,
  showLayers: { m1: true, m2: false, m3: true },
  showOverlay: true,
  fillDisplayMode: localStorage.getItem("reviewFillDisplayMode") || "hover", // "hover" | "margin"
  showConf: false,
  selectedItemId: null,
  pinnedItemId: null,
  overviewFilter: "all", // "all" | "wrong" | "unanswered" | "edited"
  selectedBoxIdx: -1,
  hoverBoxIdx: -1,
  workingBoxes: [],
  workingItems: [],
  boxesModified: false,
  itemsModified: false,
  recognizingUids: {},
  recognizingBusy: false,
  undoStack: [],
  itemEdits: {},
  editingPopItemId: null,
  hoverItemId: null,
  hoverSource: null, // "canvas" | "overview" | null
  hoverInPopover: false,
  resultsMtime: 0,
  lowConfCursor: -1,
  focusedLowConf: null,
  // 本次会话改框工作量统计
  workload: { moved: 0, added: 0, deleted: 0 },
  // 画布视图变换
  img: null,
  imgKey: "",
  scale: 1,
  tx: 0,
  ty: 0,
  drag: null,
  saving: false,
  // 改试卷/迭代双模式 + 自动判分 + 原地单空编辑 + 局部真子集小框重识别
  workMode: "grading",
  grading: null,
  rowLayouts: {},
  showUnmatchedFills: false,
  inlineBlankEdit: null, // { itemId, sub, blank }
  inlineChipEdit: null,  // { itemId, kind }
  multiSelect: null,     // { itemId, slots: [{sub, blank}] }
  subboxTarget: null,    // { itemId, qno, parentBox, targets, busy }
  flashSlots: {},        // { "itemId|sub:blank": expireTs }
};

const $ = (id) => document.getElementById(id);
const canvas = $("paperCanvas");
const ctx = canvas.getContext("2d");
const fxCanvas = $("fxCanvas");
const fxCtx = fxCanvas ? fxCanvas.getContext("2d") : null;
const host = $("canvasHost");
const overlayLayer = $("overlayLayer");

function toast(msg, ms = 2100) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), ms);
}

async function api(path, body = null) {
  const opts = body === null
    ? { method: "GET" }
    : {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Review-Token": TOKEN },
        body: JSON.stringify(body),
      };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

function currentPage() { return state.pages[state.pageIdx] || null; }
function isPageDone(p) {
  if (!p) return false;
  const rec = (state.feedback.items || {})[p.id];
  return Boolean(rec && rec.label);
}
function fmtScoreNum(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return "0";
  return Math.abs(n - Math.round(n)) < 1e-6 ? String(Math.round(n)) : n.toFixed(1).replace(/\.0$/, "");
}
function getPageGrading(p) {
  const pg = p || currentPage();
  if (!pg || !state.grading || !state.grading.has_schema) return null;
  return (state.grading.pages || []).find(x => x.page_id === pg.id) || null;
}
function isUnmatchedFillItem(it) {
  if (!it || it.kind !== "fill") return false;
  if (Array.isArray(it.issues) && it.issues.includes("大题未配到题号框")) return true;
  const qtr = String((it.transcription && it.transcription.question_number) || "").trim();
  const hasNum = (it.number !== null && it.number !== undefined && String(it.number).trim() !== "") || /^\d+$/.test(qtr);
  return !hasNum;
}
function shouldShowFillInView(it) {
  if (!it || it.kind !== "fill") return true;
  if (!isUnmatchedFillItem(it)) return true;
  if (state.workMode === "iteration") return true;
  return Boolean(state.showUnmatchedFills);
}
function getEffectiveFillAnswersJS(it) {
  const merged = {};
  const base = it && it.transcription && it.transcription.answers;
  if (base && typeof base === "object") {
    for (const [sub, blanks] of Object.entries(base)) {
      if (blanks && typeof blanks === "object") {
        merged[sub] = { ...blanks };
      } else {
        merged[sub] = { "1": String(blanks == null ? "" : blanks) };
      }
    }
  }
  const fbOv = (((state.feedback.items || {})[it.id] || {}).answers_override);
  if (fbOv && typeof fbOv === "object") {
    for (const [sub, blanks] of Object.entries(fbOv)) {
      if (blanks && typeof blanks === "object") {
        merged[sub] = { ...(merged[sub] || {}), ...blanks };
      }
    }
  }
  const localOv = state.itemEdits[it.id] && state.itemEdits[it.id].answers_override;
  if (localOv && typeof localOv === "object") {
    for (const [sub, blanks] of Object.entries(localOv)) {
      if (blanks && typeof blanks === "object") {
        merged[sub] = { ...(merged[sub] || {}), ...blanks };
      }
    }
  }
  return Object.keys(merged).length ? merged : null;
}
function getOrderedFillSlotsJS(it) {
  const ans = getEffectiveFillAnswersJS(it);
  if (!ans) return [];
  const slots = [];
  Object.entries(ans).sort((a, b) => compareSubKey(a[0], b[0])).forEach(([sub, blanks]) => {
    if (!blanks || typeof blanks !== "object" || blanks["舍弃"] === "舍弃") return;
    Object.keys(blanks)
      .sort((a, b) => (Number(a) || 999) - (Number(b) || 999) || String(a).localeCompare(String(b)))
      .forEach(bk => slots.push({ sub: String(sub), blank: String(bk) }));
  });
  return slots;
}
function isBlankMultiSelected(itemId, sub, blank) {
  const ms = state.multiSelect;
  if (!ms || ms.itemId !== itemId) return false;
  return ms.slots.some(s => s.sub === String(sub) && s.blank === String(blank));
}
function updateMultiBlankBarUI() {
  const bar = $("multiBlankBar");
  const txt = $("multiBlankText");
  if (!bar) return;
  const ms = state.multiSelect;
  const show = Boolean(ms && ms.slots && ms.slots.length >= 2 && !state.subboxTarget);
  bar.classList.toggle("show", show);
  if (show && txt) {
    const labels = ms.slots.map(s => (s.sub === "无小题号" ? "" : `(${s.sub})`) + fmtBlankIdx(s.blank)).join("、");
    txt.textContent = `已连续选中 ${ms.slots.length} 空 [${labels}] · 请将对应印刷小题号一并框入`;
  }
}
function updateSubboxHudUI() {
  const hud = $("subboxHud");
  const txt = $("subboxHudText");
  if (!hud) return;
  const st = state.subboxTarget;
  hud.classList.toggle("show", Boolean(st));
  if (st && txt) {
    const labels = st.targets.map(s => (s.sub === "无小题号" ? "" : `(${s.sub})`) + fmtBlankIdx(s.blank)).join("、");
    if (st.busy) {
      txt.innerHTML = `${uiIcon("loader", "ui-ico-sm ui-ico-spin")} 正在高精度局部重识别 第 ${escHtml(st.qno || "?")} 题 ${escHtml(labels)} ...`;
    } else if (st.targets.length === 1) {
      txt.innerHTML = `${uiIcon("crosshair", "ui-ico-sm")} 单空聚焦重识别：第 ${escHtml(st.qno || "?")} 题 ${escHtml(labels)} — 在亮起大框内拖出该横线手写区即自动填入`;
    } else {
      txt.innerHTML = `${uiIcon("crosshair", "ui-ico-sm")} 联合重识别 (${st.targets.length} 空)：第 ${escHtml(st.qno || "?")} 题 ${escHtml(labels)} — 框选时请将对应印刷小题号一并包入`;
    }
  }
  updateMultiBlankBarUI();
}
function enterSubboxReOcr(it, targets) {
  if (!it || !it.box || it.box.length !== 4 || !targets || !targets.length) return;
  if (state.mode !== "view") setMode("view");
  state.inlineBlankEdit = null;
  state.inlineChipEdit = null;
  state.editingPopItemId = null;
  state.multiSelect = null;
  const qno = String((it.transcription && it.transcription.question_number) || it.number || "?");
  state.subboxTarget = {
    itemId: it.id,
    qno,
    parentBox: [...it.box],
    targets: targets.map(t => ({ sub: String(t.sub || "无小题号"), blank: String(t.blank || "1") })),
    busy: false,
  };
  state.selectedItemId = it.id;
  state.hoverItemId = it.id;
  focusBoxOnCanvas(it.box);
  updateSubboxHudUI();
  draw();
}
function cancelSubboxReOcr() {
  if (!state.subboxTarget) return;
  state.subboxTarget = null;
  if (state.drag && state.drag.type === "subbox_draw") state.drag = null;
  updateSubboxHudUI();
  refreshOverviewPanel();
  draw();
}
async function submitSubboxReOcr(box) {
  const st = state.subboxTarget;
  const p = currentPage();
  if (!st || !p || st.busy) return;
  st.busy = true;
  updateSubboxHudUI();
  draw();
  try {
    const res = await api("/api/review/re-ocr-subbox", {
      page_id: p.id,
      item_id: st.itemId,
      box,
      targets: st.targets,
      reviewer: REVIEWER || "审阅员",
    });
    if (res.feedback) state.feedback = res.feedback;
    if (res.grading) state.grading = res.grading;
    const nowTs = Date.now() + 2200;
    st.targets.forEach(t => {
      state.flashSlots[`${st.itemId}|${t.sub}:${t.blank}`] = nowTs;
    });
    const preview = Object.values(res.values || {}).map(v => v || "[空白]").join(" / ");
    toast(`局部重识别已回填：${preview}（${res.seconds || 0}s）`, 2800);
    state.subboxTarget = null;
    updateSubboxHudUI();
    updateTopbar();
    renderLeftList();
    refreshOverviewPanel();
    draw();
    setTimeout(() => { refreshOverviewPanel(); draw(); }, 2300);
  } catch (err) {
    st.busy = false;
    updateSubboxHudUI();
    toast("局部重识别失败：" + err.message, 3800);
    draw();
  }
}
function toggleMultiBlankSelection(it, sub, blank, isShift) {
  const slots = getOrderedFillSlotsJS(it);
  if (!slots.length) return;
  const subS = String(sub), bkS = String(blank);
  const clickedIdx = slots.findIndex(s => s.sub === subS && s.blank === bkS);
  if (clickedIdx < 0) return;

  if (!state.multiSelect || state.multiSelect.itemId !== it.id) {
    state.multiSelect = { itemId: it.id, slots: [{ sub: subS, blank: bkS }] };
    updateMultiBlankBarUI();
    draw();
    return;
  }
  const curIdxs = state.multiSelect.slots
    .map(s => slots.findIndex(x => x.sub === s.sub && x.blank === s.blank))
    .filter(i => i >= 0)
    .sort((a, b) => a - b);

  if (isShift && curIdxs.length) {
    const lo = Math.min(curIdxs[0], clickedIdx);
    const hi = Math.max(curIdxs[curIdxs.length - 1], clickedIdx);
    state.multiSelect.slots = slots.slice(lo, hi + 1);
  } else {
    const exists = curIdxs.includes(clickedIdx);
    if (exists) {
      // 只允许从两端移除，保持连续性
      if (clickedIdx === curIdxs[0]) curIdxs.shift();
      else if (clickedIdx === curIdxs[curIdxs.length - 1]) curIdxs.pop();
      else {
        toast("多空联合识别须保持几何连续：已收敛为单选此空", 2200);
        curIdxs.splice(0, curIdxs.length, clickedIdx);
      }
    } else if (!curIdxs.length || clickedIdx === curIdxs[0] - 1 || clickedIdx === curIdxs[curIdxs.length - 1] + 1) {
      curIdxs.push(clickedIdx);
      curIdxs.sort((a, b) => a - b);
    } else {
      // 自动连续包选区间（保证几何连续性！）
      const lo = Math.min(curIdxs[0], clickedIdx);
      const hi = Math.max(curIdxs[curIdxs.length - 1], clickedIdx);
      curIdxs.splice(0, curIdxs.length);
      for (let k = lo; k <= hi; k++) curIdxs.push(k);
      toast("已自动连续选中中间空位以保持几何连续性", 2000);
    }
    state.multiSelect = curIdxs.length ? { itemId: it.id, slots: curIdxs.map(k => slots[k]) } : null;
  }
  updateMultiBlankBarUI();
  refreshOverviewPanel();
  draw();
}
async function commitInlineBlankEdit(it, sub, blank, nextVal, moveDir = 0) {
  const p = currentPage();
  if (!p || !it) return;
  const subS = String(sub), bkS = String(blank);
  const valStr = String(nextVal == null ? "" : nextVal).trim();
  const prevMap = getEffectiveFillAnswersJS(it) || {};
  const prevVal = String((prevMap[subS] && prevMap[subS][bkS]) || "").trim();
  const prevSrc = (state.inlineBlankEdit && state.inlineBlankEdit.source) || "pop";
  state.inlineBlankEdit = null;
  if (valStr !== prevVal) {
    try {
      const res = await api("/api/review/set-blank", {
        page_id: p.id,
        item_id: it.id,
        sub: subS,
        blank: bkS,
        text: valStr,
        reviewer: REVIEWER || "审阅员",
      });
      if (res.feedback) state.feedback = res.feedback;
      if (res.grading) state.grading = res.grading;
      state.flashSlots[`${it.id}|${subS}:${bkS}`] = Date.now() + 1400;
      toast(`已保存 (${subS === "无小题号" ? "" : subS + "·"}${fmtBlankIdx(bkS)})`, 1400);
    } catch (err) {
      toast("保存失败：" + err.message, 3000);
    }
  }
  if (moveDir !== 0) {
    const slots = getOrderedFillSlotsJS(it);
    const curI = slots.findIndex(s => s.sub === subS && s.blank === bkS);
    const nxtI = curI + moveDir;
    if (curI >= 0 && nxtI >= 0 && nxtI < slots.length) {
      state.inlineBlankEdit = { itemId: it.id, sub: slots[nxtI].sub, blank: slots[nxtI].blank, source: prevSrc };
    }
  } else if (prevSrc === "overview" && !state.pinnedItemId && state.selectedItemId === it.id) {
    state.selectedItemId = null;
  }
  updateTopbar();
  renderLeftList();
  refreshOverviewPanel();
  draw();
}
async function cycleBlankJudge(it, slotKey, curStatus) {
  const p = currentPage();
  if (!p || !it) return;
  const order = ["correct", "partial", "wrong", "auto"];
  const idx = order.indexOf(curStatus);
  const nextStatus = order[(idx + 1) % order.length];
  try {
    const res = await api("/api/review/judge-blank", {
      page_id: p.id,
      item_id: it.id,
      slot: slotKey,
      status: nextStatus,
      reviewer: REVIEWER || "审阅员",
    });
    if (res.feedback) state.feedback = res.feedback;
    if (res.grading) state.grading = res.grading;
    const lbl = { correct: "已判对", partial: "已判半分", wrong: "已判错", auto: "已恢复自动判分" }[nextStatus];
    toast(lbl, 1400);
    updateTopbar();
    refreshOverviewPanel();
    draw();
  } catch (err) {
    toast("改判失败：" + err.message, 2500);
  }
}
async function switchWorkMode(nextMode) {
  const wm = nextMode === "iteration" ? "iteration" : "grading";
  state.workMode = wm;
  state.showUnmatchedFills = (wm === "iteration");
  try {
    await api("/api/work-mode", { work_mode: wm });
    hubApi("/config", { work_mode: wm }).catch(() => {});
  } catch {}
  updateTopbar();
  renderRightPanel();
  draw();
  toast(
    wm === "grading"
      ? "已切回改卷模式：调框仅本批临时生效，不会写入训练库"
      : "已切至模型迭代模式：确认保存时将自动建目录并回流 M1/M2/M3 训练数据",
    2800
  );
}
function getChoiceConfs(it) {
  const r = it.recognition || {};
  const nc = typeof r.number_conf === "number" ? r.number_conf
    : (typeof r.digit_conf === "number" ? r.digit_conf
    : (typeof r.num_conf === "number" ? r.num_conf
    : (typeof r.number_confidence === "number" ? r.number_confidence : null)));
  const oc = typeof r.choice_conf === "number" ? r.choice_conf
    : (typeof r.opt_conf === "number" ? r.opt_conf
    : (typeof r.confidence === "number" ? r.confidence : null));
  return { nc, oc };
}
function isItemTrusted(it) {
  if (!it) return false;
  const edit = state.itemEdits[it.id];
  if (edit && (edit.trusted || edit.label === "已订正" || edit.answer !== undefined || edit.correction !== undefined)) return true;
  const fb = (state.feedback.items || {})[it.id];
  if (fb && (fb.trusted || fb.label === "已订正" || fb.answer !== undefined || (fb.correction && fb.correction.length > 0))) return true;
  return false;
}
function isChoiceRuleInferred(it) {
  return Boolean(it && it.kind === "choice" && it.recognition && it.recognition.no_source === "sequence");
}
function isLowConfChoice(it) {
  if (isItemTrusted(it)) return false;
  if (isChoiceRuleInferred(it)) return true;
  const { nc, oc } = getChoiceConfs(it);
  if (nc !== null && nc < 0.7) return true;
  if (oc !== null && oc < 0.7) return true;
  return Boolean(it.issues && it.issues.length > 0);
}
function choiceConfText(it) {
  const { nc, oc } = getChoiceConfs(it);
  const parts = [];
  if (isChoiceRuleInferred(it)) parts.push("规则补号");
  else if (nc !== null) parts.push("号" + Math.round(nc * 100) + "%");
  if (oc !== null) parts.push("涂" + Math.round(oc * 100) + "%");
  return parts.join(" ");
}
function describeLowConfChoice(it) {
  const { nc, oc } = getChoiceConfs(it);
  const r = it.recognition || {};
  const qno = (it.number && it.number !== "?") ? `第${it.number}题` : "题号未认出";
  const reasons = [];
  if (r.no_source === "sequence") {
    const rawStr = (r.no_raw && r.no_raw > 0) ? r.no_raw : "?";
    reasons.push(`几何连续性规则推算补全（非OCR识别，PP-OCRv6原读${rawStr}）`);
  } else if (nc !== null && nc < 0.7) {
    reasons.push(`题号OCR(PP-OCRv6) ${Math.round(nc * 100)}%`);
  }
  if (oc !== null && oc < 0.7) {
    reasons.push(`涂卡识别模型(CNN) ${Math.round(oc * 100)}%`);
  }
  if (!reasons.length && it.issues && it.issues.length) {
    reasons.push(`选择题识别模型：${it.issues.join("；")}`);
  }
  const okParts = [];
  if (nc !== null && nc >= 0.7 && r.no_source !== "sequence") okParts.push(`题号${Math.round(nc * 100)}%`);
  if (oc !== null && oc >= 0.7) okParts.push(`涂卡${Math.round(oc * 100)}%`);
  return `${qno} · ${reasons.join(" + ")}${okParts.length ? `（${okParts.join(" ")}正常）` : ""}`;
}
function trustLowConfTarget(target) {
  if (!target) return;
  if (target.kind === "box") {
    if (state.workingBoxes[target.idx]) {
      state.workingBoxes[target.idx].trusted = true;
      state.boxesModified = true;
    }
  } else if (target.item) {
    const it = target.item;
    state.itemEdits[it.id] = { ...(state.itemEdits[it.id] || {}), trusted: true, label: "已确认" };
  }
  state.focusedLowConf = null;
  state.editingPopItemId = null;
  state.selectedItemId = null;
  state.lowConfCursor = -1;
  updateDirtyBadge();
  renderLeftList();
  updateTopbar();
  renderRightPanel();
  draw();
  toast("已标记为信任，低置信度提醒已清除（按 D 保存）", 1900);
}
function getChoiceDisplayAnswer(it) {
  if (state.itemEdits[it.id] && state.itemEdits[it.id].answer !== undefined) return state.itemEdits[it.id].answer;
  const fb = (state.feedback.items || {})[it.id];
  if (fb && fb.answer !== undefined) return fb.answer;
  if (fb && fb.correction) return fb.correction;
  return it.answer || "—";
}
function formatFillAnswers(it) {
  if (it.kind === "identity") {
    return it.transcription ? `姓名：${it.transcription.name || "未作答"}  学号：${it.transcription.sid || "未作答"}` : "";
  }
  const ans = it.transcription && it.transcription.answers;
  if (!ans || typeof ans !== "object") {
    if (it.status === "等待视觉转录" || it.status === "等待豆包") return "";
    return it.error ? "[识别异常] " + it.error : "";
  }
  const lines = [];
  for (const [sub, blanks] of Object.entries(ans)) {
    if (blanks && typeof blanks === "object") {
      if (blanks["舍弃"] === "舍弃") lines.push(`(${sub}) [含图舍弃]`);
      else {
        const vals = Object.entries(blanks).map(([k, v]) => {
          const text = String(v == null ? "" : v).trim();
          return `${k}:${text || "未作答"}`;
        }).join("  ");
        lines.push(sub === "无小题号" ? vals : `(${sub}) ${vals}`);
      }
    } else lines.push(`(${sub}) ${String(blanks)}`);
  }
  return lines.join("\n");
}
function getFillDisplayText(it) {
  if (state.itemEdits[it.id] && state.itemEdits[it.id].correction !== undefined) return state.itemEdits[it.id].correction;
  const fb = (state.feedback.items || {})[it.id];
  if (fb && typeof fb.correction === "string" && fb.correction.length > 0) return fb.correction;
  return formatFillAnswers(it);
}
function renderLatexInto(el, rawText) {
  el.innerHTML = "";
  const lines = String(rawText || "").split("\n");
  lines.forEach((line, idx) => {
    if (idx > 0) el.appendChild(document.createElement("br"));
    const parts = line.split(/(\$[^$]+\$)/g);
    for (const part of parts) {
      if (part.startsWith("$") && part.endsWith("$") && part.length > 2 && window.katex) {
        const span = document.createElement("span");
        try { window.katex.render(part.slice(1, -1), span, { throwOnError: false }); }
        catch { span.textContent = part; }
        el.appendChild(span);
      } else {
        el.appendChild(document.createTextNode(part));
      }
    }
  });
}

function updateDirtyBadge() {
  const dirty = state.boxesModified || state.itemsModified || Object.keys(state.itemEdits).length > 0;
  $("dirtyPill").classList.toggle("show", dirty);
}

function updateWorkloadBar() {
  const donePages = state.pages.filter(isPageDone);
  const mineCnt = donePages.filter(p => {
    const rec = (state.feedback.items || {})[p.id];
    if (!REVIEWER) return true;
    return rec && (rec.reviewer || "") === REVIEWER;
  }).length;
  $("wlPages").textContent = `${donePages.length}/${state.pages.length}`;
  $("wlMine").textContent = String(mineCnt);
  $("wlMove").textContent = String(state.workload.moved);
  $("wlAdd").textContent = String(state.workload.added);
  $("wlDel").textContent = String(state.workload.deleted);
  const hasEdits = (state.workload.moved + state.workload.added + state.workload.deleted) > 0;
  const editPart = $("wlEditPart");
  if (editPart) editPart.classList.toggle("show", state.mode === "edit" || hasEdits);
}

function isResultInProgress(res) {
  if (!res) return false;
  const st = String(res.status || "");
  if (st.includes("处理中") || st.includes("转录中") || st.includes("合并中")) return true;
  const pages = res.pages || [];
  return pages.some(p => (p.items || []).some(it => it.status === "等待视觉转录" || it.status === "等待豆包"));
}

function updateLiveRunBanner() {
  const banner = $("liveRunBanner");
  const txt = $("liveRunBannerText");
  if (!banner || !txt || !state.result) return;
  const res = state.result;
  const st = String(res.status || "");
  const stats = res.stats || {};
  const pagesN = (res.pages || []).length;
  const transN = stats.items_with_transcription !== undefined
    ? stats.items_with_transcription
    : (res.pages || []).reduce((acc, p) => acc + (p.items || []).filter(i => i.transcription).length, 0);

  if (st.includes("识别失败")) {
    banner.className = "live-run-banner show err";
    txt.innerHTML = `${uiIcon("alert", "ui-ico-sm")} ${escHtml(st)}（共 ${pagesN} 页，其中 ${stats.page_failures || 0} 页失败）。请返回工作台查看中文错误诊断与修复建议。`;
  } else if (isResultInProgress(res)) {
    banner.className = "live-run-banner show";
    txt.innerHTML = `${uiIcon("zap", "ui-ico-sm")} 后台自动识别与实时合并进行中（当前已检出 ${pagesN} 页，已出转录 ${transN} 处）… 新出答案会自动刷新，建议等本页答案完整后再点「确认」。`;
  } else if (st.includes("待补跑")) {
    banner.className = "live-run-banner show";
    txt.innerHTML = `${uiIcon("alert", "ui-ico-sm")} 当前批次状态：${escHtml(st)}（可返回工作台点击「补跑失败图块」仅重试失败项，不重复扣费）。`;
  } else {
    banner.className = "live-run-banner";
  }
}

let _boxUidSeq = 1;
function nextBoxUid() { return "bx_" + (_boxUidSeq++); }
function isVlmRecogCls(cls) { return cls === 0 || cls === 5; }
function boxIoU(a, b) {
  if (!a || !b || a.length !== 4 || b.length !== 4) return 0;
  const ix1 = Math.max(a[0], b[0]), iy1 = Math.max(a[1], b[1]);
  const ix2 = Math.min(a[2], b[2]), iy2 = Math.min(a[3], b[3]);
  const inter = Math.max(0, ix2 - ix1) * Math.max(0, iy2 - iy1);
  if (inter <= 0) return 0;
  const areaA = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
  const areaB = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
  const u = areaA + areaB - inter;
  return u > 0 ? inter / u : 0;
}

function getPageBoxes(p) {
  if (!p) return [];
  const fbRec = (state.feedback.items || {})[p.id];
  if (fbRec && Array.isArray(fbRec.boxes)) return fbRec.boxes;
  return p.boxes || [];
}

function getRawPageItems(p) {
  if (!p) return [];
  const list = p.items || [];
  const fbRec = (state.feedback.items || {})[p.id];
  if (fbRec && Array.isArray(fbRec.deleted_item_ids) && fbRec.deleted_item_ids.length) {
    const delSet = new Set(fbRec.deleted_item_ids);
    return list.filter(it => !delSet.has(it.id));
  }
  return list;
}

function getPageItems(p) {
  if (!p) return [];
  if (p === currentPage()) return state.workingItems;
  return getRawPageItems(p);
}

function bindBoxesAndItems(boxes, items) {
  const itemById = new Map();
  (items || []).forEach(it => { if (it && it.id) itemById.set(it.id, it); });
  const claimedIds = new Set();

  (boxes || []).forEach(b => {
    if (!b._uid) b._uid = nextBoxUid();
    if (b.item_id && itemById.has(b.item_id) && !claimedIds.has(b.item_id)) {
      claimedIds.add(b.item_id);
    } else if (b.item_id && !itemById.has(b.item_id)) {
      delete b.item_id;
    }
  });

  // 为尚未绑定的 fill(cls=5)、identity(cls=0)、unmatched(cls=2/3/6) 按最高 IoU 精准 1:1 绑定
  const pairKind = (kind, allowedCls, minIoU) => {
    const candidates = [];
    (items || []).forEach(it => {
      if (!it || it.kind !== kind || claimedIds.has(it.id) || !it.box || it.box.length !== 4) return;
      (boxes || []).forEach((b, bi) => {
        if (b.item_id || !allowedCls.includes(b.cls) || !b.box || b.box.length !== 4) return;
        const ov = boxIoU(b.box, it.box);
        if (ov >= minIoU) candidates.push({ ov, bi, it });
      });
    });
    candidates.sort((a, b) => b.ov - a.ov);
    candidates.forEach(({ bi, it }) => {
      const b = boxes[bi];
      if (!b || b.item_id || claimedIds.has(it.id)) return;
      b.item_id = it.id;
      claimedIds.add(it.id);
    });
  };
  pairKind("fill", [5], 0.50);
  pairKind("identity", [0], 0.50);
  pairKind("unmatched", [2, 3, 6], 0.65);
}

function findBoundItemForBox(b) {
  if (!b || !b.item_id) return null;
  return state.workingItems.find(it => it.id === b.item_id) || null;
}

function getPendingRecogBoxIndices() {
  const out = [];
  state.workingBoxes.forEach((b, idx) => {
    if (!isVlmRecogCls(b.cls)) return;
    if (b._uid && state.recognizingUids[b._uid]) return;
    const bound = findBoundItemForBox(b);
    if (b._needsRecog || !bound || (!bound.transcription && bound.status !== "等待视觉转录" && bound.status !== "等待豆包")) {
      out.push(idx);
    }
  });
  return out;
}

