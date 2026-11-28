async function hotReloadResults(showToast = false) {
  try {
    const data = await api("/api/state");
    if (!data || !data.result) return;
    const prevId = currentPage() ? currentPage().id : null;
    state.result = data.result;
    if (data.results_mtime) state.resultsMtime = data.results_mtime;
    if (data.feedback) state.feedback = data.feedback;
    if (data.locks) state.locks = data.locks;
    if (data.work_mode) state.workMode = data.work_mode;
    if (data.grading) state.grading = data.grading;
    state.pages = (data.result && data.result.pages) || [];
    if (prevId) {
      const idx = state.pages.findIndex(p => p.id === prevId);
      if (idx >= 0) state.pageIdx = idx;
    }
    const cur = currentPage();
    if (cur && !state.boxesModified && !state.itemsModified) {
      state.workingBoxes = JSON.parse(JSON.stringify(getPageBoxes(cur)));
      state.workingItems = JSON.parse(JSON.stringify(getRawPageItems(cur)));
      bindBoxesAndItems(state.workingBoxes, state.workingItems);
    }
    updateLiveRunBanner();
    updateTopbar();
    renderLeftList();
    renderRightPanel();
    draw();
    if (showToast) toast("已同步最新识别结果", 1400);
  } catch {}
}

/* ==================== 页面加载与切换 ==================== */
async function loadAll() {
  const data = await api("/api/state");
  state.result = data.result;
  state.resultsMtime = data.results_mtime || 0;
  state.feedback = data.feedback || { revision: 0, items: {} };
  state.locks = data.locks || {};
  state.workMode = data.work_mode === "iteration" ? "iteration" : "grading";
  state.showUnmatchedFills = state.workMode === "iteration";
  state.grading = data.grading || null;
  state.pages = (data.result && data.result.pages) || [];
  updateLiveRunBanner();

  // 优先找第一张「未审且未被他人锁定」的试卷，避免多人进入时撞车
  let startIdx = state.pages.findIndex(p => {
    const lk = state.locks[p.id];
    const lockedOther = lk && lk.client_id !== CLIENT_ID;
    return !isPageDone(p) && !lockedOther;
  });
  if (startIdx < 0) startIdx = state.pages.findIndex(p => !isPageDone(p));
  if (startIdx < 0) startIdx = 0;
  syncRightSidebarVisibility(true);
  await openPage(startIdx, true, true);
}

async function openPage(idx, skipDirtyWarn = false, skipLockWarn = false) {
  if (!state.pages.length) return;
  idx = Math.max(0, Math.min(state.pages.length - 1, idx));
  const targetPage = state.pages[idx];
  if (!skipLockWarn && targetPage) {
    const lk = state.locks[targetPage.id];
    if (lk && lk.client_id !== CLIENT_ID) {
      if (!confirm(`【${lk.user || "其他成员"}】当前正在审阅「${targetPage.name}」。\n继续进入会接管该页编辑权，确定要打开吗？`)) {
        return;
      }
    }
  }
  if (!skipDirtyWarn && (state.boxesModified || state.itemsModified || Object.keys(state.itemEdits).length > 0)) {
    if (!confirm(`当前页有未保存改动，切换将丢弃。确认切换吗？\n（提示：按 ${primaryKeyLabel("nextSubmit")} 可直接保存并下一张）`)) {
      return;
    }
  }
  const prev = currentPage();
  if (prev && prev.id && prev.id !== targetPage.id) {
    api("/api/release", { client_id: CLIENT_ID, page_id: prev.id }).catch(() => {});
  }

  state.pageIdx = idx;
  const p = currentPage();
  state.workingBoxes = JSON.parse(JSON.stringify(getPageBoxes(p)));
  state.workingItems = JSON.parse(JSON.stringify(getRawPageItems(p)));
  bindBoxesAndItems(state.workingBoxes, state.workingItems);
  state.boxesModified = false;
  state.itemsModified = false;
  state.recognizingUids = {};
  state.recognizingBusy = false;
  state.undoStack = [];
  state.itemEdits = {};
  state.selectedItemId = null;
  state.pinnedItemId = null;
  state.hoverItemId = null;
  state.hoverInPopover = false;
  state.selectedBoxIdx = -1;
  state.editingPopItemId = null;
  state.inlineBlankEdit = null;
  state.inlineChipEdit = null;
  state.multiSelect = null;
  state.subboxTarget = null;
  state.lowConfCursor = -1;
  state.focusedLowConf = null;
  updateSubboxHudUI();

  api("/api/acquire", { page_id: p.id, client_id: CLIENT_ID, user: REVIEWER || "成员", force: true })
    .then(r => { if (r.locks) state.locks = r.locks; renderLeftList(); })
    .catch(() => {});

  updateTopbar();
  renderLeftList();
  renderRightPanel();
  updateDirtyBadge();
  await loadPageImage(p);
}

function loadPageImage(p) {
  return new Promise((resolve) => {
    const src = "/asset/" + p.image;
    if (state.img && state.imgKey === src && state.img.complete) {
      fitCanvas(); draw(); resolve(); return;
    }
    const im = new Image();
    im.onload = () => { state.img = im; state.imgKey = src; fitCanvas(); draw(); resolve(); };
    im.onerror = () => { toast("原卷图片加载失败"); resolve(); };
    im.src = src;
  });
}

function updateTopbar() {
  const p = currentPage();
  $("topPageTitle").textContent = p ? `${state.pageIdx + 1}/${state.pages.length} · ${p.name}` : "无试卷";
  $("leftSummary").textContent = `${state.pages.filter(isPageDone).length}/${state.pages.length}`;
  $("btnPrev").disabled = state.pageIdx <= 0;
  $("btnPrev").textContent = `← 上一张 (${primaryKeyLabel("prevPage")})`;
  $("btnSubmitNext").textContent = state.pageIdx < state.pages.length - 1
    ? `✓ 确认并下一张 (${primaryKeyLabel("nextSubmit")})`
    : `✓ 确认并保存 (${primaryKeyLabel("nextSubmit")})`;
  $("btnModeEdit").textContent = `✎ 调框 (${primaryKeyLabel("toggleMode")})`;
  $("btnFit").textContent = `适配 (${primaryKeyLabel("fitCanvas")})`;

  $("btnModeView").className = "mode-btn" + (state.mode === "view" ? " active" : "");
  $("btnModeEdit").className = "mode-btn" + (state.mode === "edit" ? " edit-active" : "");
  const msPill = $("modeSwitchPill");
  if (msPill) msPill.dataset.mode = state.mode;
  const pgGrade = getPageGrading(p);
  $("btnModeView").textContent = (pgGrade && state.workMode === "grading") ? "👁 看分数" : "👁 看答案";

  const wp = $("revWorkPill");
  if (wp) {
    wp.dataset.work = state.workMode;
    wp.querySelectorAll(".work-pill-btn").forEach(b => b.classList.toggle("active", b.dataset.work === state.workMode));
  }
  const sp = $("topScorePill");
  if (sp) {
    if (pgGrade && pgGrade.has_schema) {
      const sm = pgGrade.summary || {};
      const pend = sm.pending_count ? ` · 待核${sm.pending_count}` : "";
      sp.textContent = `得分 ${fmtScoreNum(sm.total_earned)}/${fmtScoreNum(sm.total_possible)}${pend}`;
      sp.classList.add("show");
    } else {
      sp.classList.remove("show");
    }
  }
  const csvBtn = $("btnExportCsv");
  if (csvBtn) csvBtn.style.display = (state.grading && state.grading.has_schema) ? "" : "none";

  // 未匹配大题号 M3 填空框：改卷看分默认隐藏并给轻提示；迭代模式默认显示并支持一键清理
  const umBtn = $("btnUnmatchedFills");
  if (umBtn && p) {
    const pageUmCnt = getPageItems(p).filter(isUnmatchedFillItem).length;
    const batchUmCnt = state.pages.reduce((s, pg) => s + (pg.items || []).filter(isUnmatchedFillItem).length, 0);
    if (pageUmCnt > 0 || (state.workMode === "iteration" && batchUmCnt > 0)) {
      umBtn.classList.add("show");
      if (state.workMode === "grading") {
        umBtn.classList.add("subtle");
        umBtn.textContent = state.showUnmatchedFills
          ? `👁 显示中(${pageUmCnt}无题号) · 隐藏`
          : `已隐藏 ${pageUmCnt} 无题号框`;
        umBtn.title = "改卷模式下默认隐藏未匹配到大题题号的填空框；点击可临时切换显隐";
      } else {
        umBtn.classList.remove("subtle");
        umBtn.textContent = `🧹 清理无题号框 (页${pageUmCnt}/批${batchUmCnt})`;
        umBtn.title = "迭代模式：一键去除未匹配到大题题号的 M3 填空框（支持当前页或整批，清理前自动备份）";
      }
    } else {
      umBtn.classList.remove("show");
    }
  }

  const fillBtn = $("btnFillDisplayMode");
  if (fillBtn) {
    fillBtn.textContent = state.fillDisplayMode === "hover" ? "💬 悬停" : "📋 侧栏总览";
    fillBtn.classList.toggle("btn-ai-soft", state.fillDisplayMode === "margin");
    fillBtn.style.display = state.mode === "view" ? "" : "none";
  }
  $("canvasHint").textContent = state.mode === "edit"
    ? `左键长按拖画新框 · 单击选中后缩放/移动 · 右键删框 · ${primaryKeyLabel("recognizeBox")} 识别选中框 · Ctrl+Z 撤销`
    : (state.fillDisplayMode === "hover"
        ? `滑过预览 · 点题框📌固定 · 单击胶囊改字 · 双击单空重识 · Alt透视 · ${primaryKeyLabel("toggleMode")} 调框`
        : `右侧总览栏 · 点题头定位原卷 · 单击胶囊改字 · 双击单空重识 · ${primaryKeyLabel("toggleMode")} 调框`);
  updateWorkloadBar();
  updateLowConfButton();
  updateLiveRunBanner();
}

/* ==================== 多低置信度项一键循环定位（精确到具体框） ==================== */
function getLowConfTargetsForPage(p, useWorkingBoxes = false) {
  if (!p) return [];
  const targets = [];
  const itemList = useWorkingBoxes ? state.workingItems : getRawPageItems(p);
  itemList.forEach(it => {
    if (isItemTrusted(it)) return;
    if ((it.kind === "choice" && isLowConfChoice(it)) || it.error || (it.issues && it.issues.length > 0)) {
      let desc;
      if (it.kind === "choice") {
        desc = describeLowConfChoice(it);
      } else if (it.kind === "unmatched") {
        desc = `M2选择题标框模型(YOLO) · ${(it.issues || []).join("；") || "未配对框"}`;
      } else {
        desc = `第${it.number || "?"}题 · 手写转写大模型(VLM)：${it.error || (it.issues || []).join("；") || "待核对"}`;
      }
      if (it.box && it.box.length === 4) {
        targets.push({ kind: "item", item: it, box: it.box, label: desc });
      }
    }
  });
  const STAGE_MODEL_NAME = {
    m1: "M1版面标框模型(YOLO)",
    m2: "M2选择题标框模型(YOLO)",
    m3: "M3大题标框模型(YOLO)",
  };
  const boxList = useWorkingBoxes ? state.workingBoxes : (p.boxes || []);
  boxList.forEach((b, idx) => {
    if (b.trusted) return;
    const c = typeof b.confidence === "number" ? b.confidence : (typeof b.conf === "number" ? b.conf : null);
    if (c !== null && c < 0.70 && b.box && b.box.length === 4) {
      const meta = BOX_META[b.cls] || { name: String(b.cls), stage: "m1" };
      const modelName = STAGE_MODEL_NAME[meta.stage] || "YOLO标框模型";
      targets.push({ kind: "box", idx, box: b.box, stage: meta.stage, label: `${modelName} · ${meta.name} ${Math.round(c * 100)}%` });
    }
  });
  // 按原卷从上到下、从左到右排序，符合审阅顺序
  targets.sort((a, b) => (a.box[1] - b.box[1]) || (a.box[0] - b.box[0]));
  return targets;
}

function countPageLowConf(p) {
  return getLowConfTargetsForPage(p, p === currentPage()).length;
}

function getLowConfTargets() {
  return getLowConfTargetsForPage(currentPage(), true);
}

function findNextLowConfPageIdx() {
  if (!state.pages.length) return -1;
  for (let step = 1; step < state.pages.length; step++) {
    const idx = (state.pageIdx + step) % state.pages.length;
    if (countPageLowConf(state.pages[idx]) > 0) return idx;
  }
  return -1;
}

function updateLowConfButton() {
  const btn = $("btnLowConf");
  if (!btn) return;
  const targets = getLowConfTargets();
  const keyTag = primaryKeyLabel("nextLowConf");
  if (targets.length > 0) {
    const cur = state.lowConfCursor >= 0 ? ((state.lowConfCursor % targets.length) + 1) : 0;
    btn.style.display = "";
    btn.textContent = cur > 0 ? `⚠ 低置 ${cur}/${targets.length} (${keyTag})` : `⚠ 低置 (${targets.length}) (${keyTag})`;
  } else if (findNextLowConfPageIdx() >= 0) {
    btn.style.display = "";
    btn.textContent = `⚠ 下页低置 (${keyTag})`;
  } else {
    btn.style.display = "none";
  }
}

function panToBoxOnCanvas(box) {
  if (!state.img || !box || box.length !== 4) return;
  const rect = host.getBoundingClientRect();
  const [x1, y1, x2, y2] = box;
  const bw = Math.max(40, x2 - x1);
  const bh = Math.max(24, y2 - y1);
  const cx = (x1 + x2) / 2;
  const cy = (y1 + y2) / 2;
  // 若当前整页缩得很小看不清手写，温和拉近到舒适可读倍率；否则保留当前缩放直接居中对齐
  if (bw * state.scale < Math.min(220, rect.width * 0.36) || bh * state.scale < 36) {
    const targetScale = Math.min((rect.width * 0.72) / bw, (rect.height * 0.38) / bh);
    state.scale = Math.max(state.scale, Math.max(0.5, Math.min(1.45, targetScale)));
  }
  state.tx = rect.width * 0.5 - cx * state.scale;
  state.ty = rect.height * 0.45 - cy * state.scale;
  $("zoomLabel").textContent = Math.round(state.scale * 100) + "%";
}

function focusBoxOnCanvas(box) {
  if (!state.img || !box || box.length !== 4) return;
  const rect = host.getBoundingClientRect();
  const [x1, y1, x2, y2] = box;
  const bw = Math.max(40, x2 - x1);
  const bh = Math.max(24, y2 - y1);
  const cx = (x1 + x2) / 2;
  const cy = (y1 + y2) / 2;
  // 按目标框尺寸智能计算特写比例：小框（如选择题）放大到清晰可辨，大框保持完整可见
  const idealScale = Math.min((rect.width * 0.42) / bw, (rect.height * 0.36) / bh);
  state.scale = Math.max(0.55, Math.min(2.2, idealScale));
  state.tx = rect.width * 0.5 - cx * state.scale;
  state.ty = rect.height * 0.48 - cy * state.scale;
  $("zoomLabel").textContent = Math.round(state.scale * 100) + "%";
}

function focusTargetAt(targets, cursorIdx) {
  const t = targets[cursorIdx];
  if (!t) return;
  state.lowConfCursor = cursorIdx;
  state.focusedLowConf = {
    ...t,
    index: cursorIdx + 1,
    total: targets.length,
  };
  if (t.kind === "box") {
    state.showLayers[t.stage] = true;
    state.showConf = true;
    $("chkConf").checked = true;
    document.body.classList.add("show-conf");
    state.selectedBoxIdx = t.idx;
    state.selectedItemId = null;
    state.editingPopItemId = null;
    if (state.mode === "edit") renderRightPanel();
  } else {
    state.selectedItemId = t.item.id;
    state.selectedBoxIdx = -1;
    state.editingPopItemId = null;
  }
  focusBoxOnCanvas(t.box);
  updateLowConfButton();
  draw();
  toast(`已定位低置信度框 ${cursorIdx + 1}/${targets.length}：${t.label}`, 2400);
}

function jumpNextLowConf() {
  const targets = getLowConfTargets();
  if (!targets.length) {
    const nextIdx = findNextLowConfPageIdx();
    if (nextIdx >= 0) {
      openPage(nextIdx).then(() => {
        const nextTargets = getLowConfTargets();
        if (nextTargets.length) focusTargetAt(nextTargets, 0);
      });
    } else {
      toast("本批试卷没有待排查的低置信度框", 1800);
    }
    return;
  }
  // 本页已经定位到最后一个低置框时，如果还有其他页含低置框，直接串联跳到下一页的第 1 个低置框
  if (state.lowConfCursor >= targets.length - 1) {
    const nextIdx = findNextLowConfPageIdx();
    if (nextIdx >= 0) {
      openPage(nextIdx).then(() => {
        const nextTargets = getLowConfTargets();
        if (nextTargets.length) focusTargetAt(nextTargets, 0);
      });
      return;
    }
  }
  const nextCur = (state.lowConfCursor + 1) % targets.length;
  focusTargetAt(targets, nextCur);
}

function escHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderLeftList() {
  const list = $("pageList");
  list.innerHTML = "";
  state.pages.forEach((p, idx) => {
    const done = isPageDone(p);
    const doneRec = (state.feedback.items || {})[p.id] || null;
    const lowCnt = countPageLowConf(p);
    const itemsCnt = getPageItems(p).length;
    const lk = state.locks[p.id];
    const lockedOther = lk && lk.client_id !== CLIENT_ID;
    const lockedMine = lk && lk.client_id === CLIENT_ID;

    const row = document.createElement("div");
    row.className = "page-row" + (idx === state.pageIdx ? " active" : "");
    row.onclick = () => openPage(idx);

    const shortName = p.name.length > 15 ? p.name.slice(-13) : p.name;
    let badgeHtml = done ? `<span class="badge badge-done">✓ 已审</span>` : `<span class="badge badge-todo">待审</span>`;
    if (lockedOther) badgeHtml = `<span class="badge badge-lock" title="${escHtml(lk.user)} 正在审阅此页">🔒 ${escHtml(lk.user)}</span>`;
    else if (lockedMine && idx === state.pageIdx && !done) badgeHtml = `<span class="badge" style="background:#dbeafe;color:#1d4ed8;">✎ 审阅中</span>`;

    const subParts = [itemsCnt > 0 ? `${itemsCnt}题` : "无答题区"];
    if (done && doneRec && doneRec.reviewer) subParts.push(`${doneRec.reviewer}审`);

    row.innerHTML = `
      <div class="page-row-main">
        <div class="page-row-name">${String(idx + 1).padStart(2, "0")} · ${escHtml(shortName)}</div>
        <div class="page-row-sub"><span>${escHtml(subParts.join(" · "))}</span></div>
      </div>
      ${badgeHtml}
    `;
    if (lowCnt > 0) {
      const subEl = row.querySelector(".page-row-sub");
      const pill = document.createElement("span");
      pill.className = "lowconf-pill-btn";
      pill.textContent = `⚠ ${lowCnt}低置`;
      pill.title = "点击直接定位并放大到该页第 1 个低置信度框";
      pill.onclick = (e) => {
        e.stopPropagation();
        if (idx === state.pageIdx) {
          jumpNextLowConf();
        } else {
          openPage(idx).then(() => {
            const nextTargets = getLowConfTargets();
            if (nextTargets.length) focusTargetAt(nextTargets, 0);
          });
        }
      };
      subEl.appendChild(pill);
    }
    list.appendChild(row);
  });
}

