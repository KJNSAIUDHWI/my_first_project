/* ==================== 游戏风键位设置弹窗逻辑（主/副双键位） ==================== */
function renderKeybindDialog() {
  const list = $("kbList");
  list.innerHTML = "";
  KEYBIND_LABELS.forEach(([act, label]) => {
    const row = document.createElement("div");
    row.className = "kb-row";
    row.innerHTML = `<span style="font-size:12.5px;">${label}</span>`;
    const slots = document.createElement("div");
    slots.className = "kb-slots";
    const pair = state.keybinds[act] || ["", ""];
    [0, 1].forEach(slotIdx => {
      const val = pair[slotIdx] || "";
      const isListen = state.listeningAction === act && state.listeningSlot === slotIdx;
      const btn = document.createElement("button");
      btn.className = "kb-btn" + (isListen ? " listening" : "") + (!val && !isListen ? " empty" : "");
      btn.textContent = isListen ? "请按键..." : (val ? fmtKey(val) : (slotIdx === 0 ? "主未设" : "副键位"));
      btn.title = "左键点击重设 · 右键点击清空";
      btn.onclick = () => {
        if (isListen) state.listeningAction = null;
        else { state.listeningAction = act; state.listeningSlot = slotIdx; }
        renderKeybindDialog();
      };
      btn.oncontextmenu = (e) => {
        e.preventDefault();
        state.keybinds[act][slotIdx] = "";
        saveKeybinds(state.keybinds);
        renderKeybindDialog();
        updateTopbar();
        renderRightPanel();
      };
      slots.appendChild(btn);
    });
    row.appendChild(slots);
    list.appendChild(row);
  });
}

$("btnCloseKb").onclick = $("btnDoneKb").onclick = () => {
  state.listeningAction = null;
  $("kbDialog").close();
};
$("btnResetKb").onclick = () => {
  state.keybinds = cloneDefaultKeybinds();
  saveKeybinds(state.keybinds);
  state.listeningAction = null;
  renderKeybindDialog();
  updateTopbar();
  renderRightPanel();
  toast("已恢复默认键位");
};

/* ==================== 交互式同步聚光灯新手引导 ==================== */
const TOUR_STEPS = [
  {
    selector: "#sidebarLeft",
    title: "试卷列表与进度",
    desc: "这里显示整批试卷的审阅进度。多人一起审时，会自动标出谁正在看哪一张；橙色的 <b>低置</b> 是<b>「低置信度」</b>的简称（程序识别把握不高的题或框），点一下可直接跳过去核对。",
    onEnter: () => { $("sidebarLeft").classList.remove("collapsed"); setTimeout(resizeCanvas, 210); },
  },
  {
    selector: "#canvasHost",
    title: "核对原卷与修改答案",
    desc: "填空题默认<b>鼠标滑到题框上</b>即就近弹出答案（顶栏可切「右侧列表」），学生没写的空位会显示<b>「未作答」</b>胶囊，发现认错了点框或点气泡直接改。中键/右键拖图，滚轮缩放，按住 <b>Alt</b> 透视原图。",
    onEnter: () => { if (state.mode !== "view") setMode("view"); },
  },
  {
    selector: "#btnLowConf",
    fallbackSelector: "#topPageTitle",
    title: "一键定位低置信度 (L)",
    desc: "<b>「低置」即低置信度</b>，表示模型对题号、涂卡或标框的识别把握较低。点这里（或按 <b>L</b>）会自动放大跳到对应框并标明原因；核对无误点绿色的 <b>✓ 信任无误</b> 即可消掉橙色提醒。",
    tryLabel: "试着定位一题",
    onTry: () => { jumpNextLowConf(); },
  },
  {
    selector: "#btnModeEdit",
    title: "调整标框 (E)",
    desc: "如果题框画偏了或漏框，点这里（或按 <b>E</b>）进入调框：<b>左键长按拖动</b>直接画新框（大框里面也能直接划），<b>左键单击</b>选中框后可拖边缩放/框内移动，<b>右键单击</b>删框，画完按 <b>R</b> 直接调用 API 识别（Ctrl+Z 撤销）。",
    tryLabel: "切到调框看看",
    onTry: () => { setMode(state.mode === "edit" ? "view" : "edit"); setTimeout(positionTour, 240); },
  },
  {
    selector: "#btnMoreMenu",
    fallbackSelector: "#topPageTitle",
    title: "显示选项与自定义快捷键",
    desc: "点「⚙ 更多」可开关置信度显示、隐藏答案叠加、自定义所有主/副快捷键或再次打开本教程。",
    onEnter: () => { if (state.mode !== "view") setMode("view"); },
    tryLabel: "打开键位设置",
    onTry: () => { $("btnKeybinds").click(); },
  },
  {
    selector: "#btnSubmitNext",
    title: "保存并看下一张 (D)",
    desc: "这一页核对完没问题，点这里（或按 <b>D</b>）保存改动，系统会自动切到下一张还没人审的试卷。",
  },
];

let tourIdx = -1;
function positionTour() {
  if (tourIdx < 0 || tourIdx >= TOUR_STEPS.length) return;
  const step = TOUR_STEPS[tourIdx];
  let el = document.querySelector(step.selector);
  if (!el || el.offsetWidth === 0 || el.offsetHeight === 0 || getComputedStyle(el).display === "none") {
    el = step.fallbackSelector ? document.querySelector(step.fallbackSelector) : null;
  }
  const spot = $("tourSpot");
  const pop = $("tourPop");
  if (!el) return;
  const r = el.getBoundingClientRect();
  const pad = 5;
  const sl = Math.max(4, r.left - pad);
  const st = Math.max(4, r.top - pad);
  const sw = Math.min(window.innerWidth - sl - 4, r.width + pad * 2);
  const sh = Math.min(window.innerHeight - st - 4, r.height + pad * 2);
  spot.style.display = "block";
  spot.style.left = sl + "px";
  spot.style.top = st + "px";
  spot.style.width = sw + "px";
  spot.style.height = sh + "px";

  pop.style.display = "block";
  const pw = pop.offsetWidth || 345;
  const ph = pop.offsetHeight || 190;
  let pl, pt;
  if (st + sh + 12 + ph <= window.innerHeight - 10) {
    pt = st + sh + 12;
    pl = Math.max(12, Math.min(window.innerWidth - pw - 12, sl + sw / 2 - pw / 2));
  } else if (sl + sw + 14 + pw <= window.innerWidth - 10) {
    pl = sl + sw + 14;
    pt = Math.max(12, Math.min(window.innerHeight - ph - 12, st + 24));
  } else if (sl - 14 - pw >= 10) {
    pl = sl - 14 - pw;
    pt = Math.max(12, Math.min(window.innerHeight - ph - 12, st + 24));
  } else {
    pl = Math.max(12, Math.min(window.innerWidth - pw - 12, (window.innerWidth - pw) / 2));
    pt = Math.max(12, Math.min(window.innerHeight - ph - 12, st - ph - 12 > 10 ? st - ph - 12 : 70));
  }
  pop.style.left = Math.round(pl) + "px";
  pop.style.top = Math.round(pt) + "px";
}

function showTourStep(idx) {
  if (idx < 0 || idx >= TOUR_STEPS.length) { endTour(); return; }
  tourIdx = idx;
  const step = TOUR_STEPS[tourIdx];
  if (step.onEnter) step.onEnter();
  $("tourStepBadge").textContent = `步骤 ${tourIdx + 1} / ${TOUR_STEPS.length}`;
  $("tourTitle").textContent = step.title;
  $("tourDesc").innerHTML = step.desc;
  const tryRow = $("tourTryRow");
  tryRow.innerHTML = "";
  if (step.tryLabel && step.onTry) {
    tryRow.style.display = "flex";
    const b = document.createElement("button");
    b.className = "tour-try-btn";
    b.textContent = step.tryLabel;
    b.onclick = () => step.onTry();
    tryRow.appendChild(b);
  } else {
    tryRow.style.display = "none";
  }
  $("tourPrev").disabled = tourIdx === 0;
  $("tourNext").textContent = tourIdx === TOUR_STEPS.length - 1 ? "✓ 完成引导" : "下一步 →";
  setTimeout(positionTour, 30);
}

function startTour() { showTourStep(0); }
function endTour() {
  tourIdx = -1;
  $("tourSpot").style.display = "none";
  $("tourPop").style.display = "none";
}

$("tourClose").onclick = endTour;
$("tourPrev").onclick = () => showTourStep(tourIdx - 1);
$("tourNext").onclick = () => showTourStep(tourIdx + 1);
window.addEventListener("resize", () => { if (tourIdx >= 0) positionTour(); });

/* ==================== 控件与全局键盘绑定 ==================== */
$("btnMoreMenu").onclick = (e) => {
  e.stopPropagation();
  $("moreMenu").classList.toggle("open");
};
document.addEventListener("click", (e) => {
  const m = $("moreMenu");
  if (m && m.classList.contains("open") && !e.target.closest(".more-wrap")) {
    m.classList.remove("open");
  }
});
$("btnLowConf").onclick = () => jumpNextLowConf();
$("btnGuide").onclick = () => { $("moreMenu").classList.remove("open"); startTour(); };
$("btnToggleLeft").onclick = () => { $("sidebarLeft").classList.toggle("collapsed"); setTimeout(resizeCanvas, 200); };
$("btnToggleRight").onclick = () => { $("moreMenu").classList.remove("open"); $("sidebarRight").classList.toggle("collapsed"); setTimeout(resizeCanvas, 200); };
$("btnKeybinds").onclick = () => {
  $("moreMenu").classList.remove("open");
  state.listeningAction = null;
  renderKeybindDialog();
  $("kbDialog").showModal();
};
$("btnModeView").onclick = () => setMode("view");
$("btnModeEdit").onclick = () => setMode("edit");
$("btnPrev").onclick = () => openPage(state.pageIdx - 1);
$("btnSubmitNext").onclick = () => submitCurrentPage(true);
$("btnFit").onclick = () => { fitCanvas(); draw(); };
$("btnZoomIn").onclick = () => { state.scale = Math.min(8, state.scale * 1.2); $("zoomLabel").textContent = Math.round(state.scale * 100) + "%"; draw(); };
$("btnZoomOut").onclick = () => { state.scale = Math.max(0.08, state.scale / 1.2); $("zoomLabel").textContent = Math.round(state.scale * 100) + "%"; draw(); };
$("chkConf").onchange = (ev) => { state.showConf = ev.target.checked; document.body.classList.toggle("show-conf", state.showConf); draw(); };
$("chkOverlay").onchange = (ev) => { state.showOverlay = ev.target.checked; fitCanvas(); draw(); };
$("btnFillDisplayMode").onclick = () => {
  state.fillDisplayMode = state.fillDisplayMode === "hover" ? "margin" : "hover";
  localStorage.setItem("reviewFillDisplayMode", state.fillDisplayMode);
  state.hoverItemId = null;
  state.hoverInPopover = false;
  let autoCollapsed = false;
  if (state.fillDisplayMode === "margin") {
    const sides = getMarginSidesForPage(currentPage());
    const leftBar = $("sidebarLeft");
    if (leftBar && !leftBar.classList.contains("collapsed") && (sides.hasLeft && sides.hasRight || window.innerWidth < 1500)) {
      leftBar.classList.add("collapsed");
      autoCollapsed = true;
    }
  }
  updateTopbar();
  fitCanvas();
  draw();
  if (autoCollapsed) {
    setTimeout(() => { resizeCanvas(); fitCanvas(); draw(); }, 210);
  }
  toast(state.fillDisplayMode === "hover"
    ? "已切换为「悬停看答案」（鼠标滑到填空题框显示）"
    : "已切换为「左右双侧总览」（左栏贴左、右栏贴右，零跨卷连线）", 2200);
};
$("btnRefreshNow").onclick = () => hotReloadResults(true);

window.addEventListener("keydown", (ev) => {
  // 正在监听游戏改键位
  if (state.listeningAction) {
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.key === "Escape") {
      state.listeningAction = null;
      renderKeybindDialog();
      return;
    }
    if (["Control", "Shift", "Alt", "Meta"].includes(ev.key)) return;
    const nk = ev.key.toLowerCase();
    // 清除其他槽位上的重复冲突绑定
    for (const [act, pair] of Object.entries(state.keybinds)) {
      if (pair[0] === nk) pair[0] = "";
      if (pair[1] === nk) pair[1] = "";
    }
    state.keybinds[state.listeningAction][state.listeningSlot] = nk;
    saveKeybinds(state.keybinds);
    state.listeningAction = null;
    renderKeybindDialog();
    updateTopbar();
    renderRightPanel();
    return;
  }

  const tag = (ev.target && ev.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;

  // 按住 Alt 临时透视隐藏答案浮层
  if (ev.key === "Alt") {
    ev.preventDefault();
    overlayLayer.classList.add("peek-hide");
    return;
  }
  if (ev.code === "Space") { spaceDown = true; return; }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z" && state.mode === "edit") {
    ev.preventDefault();
    undoBoxEdit();
    return;
  }
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

  if (ev.key === "Escape") {
    if (state.subboxTarget) { cancelSubboxReOcr(); return; }
    if (state.multiSelect) { state.multiSelect = null; updateMultiBlankBarUI(); draw(); return; }
    if (state.inlineBlankEdit || state.inlineChipEdit) {
      state.inlineBlankEdit = null;
      state.inlineChipEdit = null;
      draw();
      return;
    }
    if (tourIdx >= 0) { endTour(); return; }
    if (state.mode === "edit" && state.selectedBoxIdx >= 0) {
      state.selectedBoxIdx = -1;
      renderRightPanel();
      draw();
      return;
    }
    if (state.editingPopItemId || state.selectedItemId || state.focusedLowConf) {
      state.editingPopItemId = null;
      state.selectedItemId = null;
      state.focusedLowConf = null;
      draw();
      return;
    }
  }
  const k = ev.key.toLowerCase();
  if (state.mode === "view" && state.multiSelect && state.multiSelect.slots.length >= 2 && matchActionKey("recognizeBox", k)) {
    ev.preventDefault();
    const p = currentPage();
    const it = p && getPageItems(p).find(x => x.id === state.multiSelect.itemId);
    if (it) enterSubboxReOcr(it, state.multiSelect.slots);
    return;
  }
  if (matchActionKey("nextSubmit", k)) { ev.preventDefault(); submitCurrentPage(true); }
  else if (matchActionKey("prevPage", k)) { ev.preventDefault(); openPage(state.pageIdx - 1); }
  else if (matchActionKey("toggleMode", k)) { ev.preventDefault(); setMode(state.mode === "edit" ? "view" : "edit"); }
  else if (matchActionKey("fitCanvas", k)) { ev.preventDefault(); state.focusedLowConf = null; fitCanvas(); draw(); }
  else if (matchActionKey("toggleAns", k)) {
    ev.preventDefault();
    state.showOverlay = !state.showOverlay;
    $("chkOverlay").checked = state.showOverlay;
    draw();
  } else if (matchActionKey("nextLowConf", k)) {
    ev.preventDefault();
    jumpNextLowConf();
  } else if (state.mode === "edit" && matchActionKey("deleteBox", k)) {
    ev.preventDefault();
    deleteBoxAt(state.selectedBoxIdx);
  } else if (state.mode === "edit" && matchActionKey("recognizeBox", k)) {
    ev.preventDefault();
    recognizeBoxIndices([state.selectedBoxIdx]);
  } else if (state.mode === "edit") {
    for (const cid of CLS_ORDER) {
      if (matchActionKey("cls_" + cid, k)) {
        ev.preventDefault();
        state.activeCls = cid;
        state.showLayers[BOX_META[cid].stage] = true;
        if (state.selectedBoxIdx >= 0 && state.workingBoxes[state.selectedBoxIdx] && state.workingBoxes[state.selectedBoxIdx].cls !== cid) {
          state.selectedBoxIdx = -1;
        }
        renderRightPanel();
        draw();
        break;
      }
    }
  }
});
window.addEventListener("keyup", (ev) => {
  if (ev.code === "Space") spaceDown = false;
  if (ev.key === "Alt") overlayLayer.classList.remove("peek-hide");
});
window.addEventListener("blur", () => {
  spaceDown = false;
  overlayLayer.classList.remove("peek-hide");
});
window.addEventListener("resize", resizeCanvas);

// 每 3 秒静默拉取多人实时状态 + 后台增量识别结果热合并
async function pollLive() {
  if (document.hidden || state.saving) return;
  try {
    const data = await api("/api/live");
    if (!data || !data.ok) return;
    const prevRev = (state.feedback && state.feedback.revision) || 0;
    const nextRev = (data.feedback && data.feedback.revision) || 0;
    const locksChanged = JSON.stringify(data.locks || {}) !== JSON.stringify(state.locks || {});
    const mtimeChanged = Boolean(data.results_mtime && data.results_mtime !== state.resultsMtime);
    if (data.locks) state.locks = data.locks;
    if (data.feedback && nextRev !== prevRev) state.feedback = data.feedback;
    if (data.work_mode && data.work_mode !== state.workMode) {
      state.workMode = data.work_mode;
      state.showUnmatchedFills = state.workMode === "iteration";
      updateTopbar();
      draw();
    }
    if (data.grading) state.grading = data.grading;
    if (mtimeChanged) {
      await hotReloadResults(false);
    } else if (locksChanged || nextRev !== prevRev) {
      renderLeftList();
      updateWorkloadBar();
      $("leftSummary").textContent = `${state.pages.filter(isPageDone).length}/${state.pages.length}`;
    }
  } catch {}
}

setInterval(pollLive, 3000);

setInterval(() => {
  const p = currentPage();
  if (p) api("/api/heartbeat", { page_id: p.id, client_id: CLIENT_ID, user: REVIEWER || "成员" }).catch(() => {});
}, 20000);

window.addEventListener("beforeunload", (ev) => {
  const p = currentPage();
  if (p && navigator.sendBeacon) {
    navigator.sendBeacon("/api/release", JSON.stringify({ client_id: CLIENT_ID, page_id: p.id, token: TOKEN }));
  }
  if (state.boxesModified || state.itemsModified || Object.keys(state.itemEdits).length > 0) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

