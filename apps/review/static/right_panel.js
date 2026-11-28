/* ==================== 右侧栏（调框工具 / 右侧停靠整页答案总览栏） ==================== */
let _revTipTimer = 0;
let _revTipAnchor = null;

function ensureRevHoverTip() {
  let el = $("revHoverTip");
  if (!el) {
    el = document.createElement("div");
    el.id = "revHoverTip";
    el.className = "rev-hover-tip";
    el.setAttribute("role", "tooltip");
    el.setAttribute("aria-hidden", "true");
    document.body.appendChild(el);
  }
  return el;
}

function showRevHoverTip(anchor, opts) {
  if (!anchor || !opts) return;
  const el = ensureRevHoverTip();
  _revTipAnchor = anchor;
  const color = opts.color || "var(--accent)";
  const placement = opts.placement || "left";
  el.style.setProperty("--tip-color", color);
  el.classList.toggle("pos-below", placement === "below");
  el.innerHTML = `
    <div class="rev-tip-head">
      <span class="rev-tip-title">${opts.dot ? `<span class="rev-tip-dot"></span>` : ""}${escHtml(opts.title || "")}</span>
      ${opts.badge ? `<span class="rev-tip-badge">${escHtml(opts.badge)}</span>` : ""}
    </div>
    <div class="rev-tip-body">${escHtml(opts.body || "")}</div>
  `;
  const r = anchor.getBoundingClientRect();
  el.style.left = "-9999px";
  el.style.top = "-9999px";
  el.classList.add("show");
  el.setAttribute("aria-hidden", "false");
  const tw = el.offsetWidth || 248;
  const th = el.offsetHeight || 72;
  let left, top;
  if (placement === "below" || r.top < 56) {
    left = Math.round(r.left + r.width / 2 - tw / 2);
    left = Math.max(10, Math.min(window.innerWidth - tw - 10, left));
    top = Math.round(r.bottom + 8);
    if (top + th > window.innerHeight - 10) top = Math.max(10, Math.round(r.top - th - 8));
  } else {
    left = Math.round(r.left - tw - 10);
    if (left < 10) left = Math.round(Math.min(window.innerWidth - tw - 10, r.right + 10));
    top = Math.round(r.top + r.height / 2 - th / 2);
    top = Math.max(58, Math.min(window.innerHeight - th - 12, top));
  }
  el.style.left = left + "px";
  el.style.top = top + "px";
}

function hideRevHoverTip() {
  clearTimeout(_revTipTimer);
  _revTipTimer = 0;
  _revTipAnchor = null;
  const el = $("revHoverTip");
  if (el) {
    el.classList.remove("show");
    el.setAttribute("aria-hidden", "true");
  }
}

function bindRevHoverTip(el, optsOrFn) {
  if (!el || !optsOrFn) return;
  el._revTipSpec = optsOrFn;
  if (el._revTipBound) return;
  el._revTipBound = true;
  const resolveOpts = () => (typeof el._revTipSpec === "function" ? el._revTipSpec() : el._revTipSpec);
  const open = () => {
    const opts = resolveOpts();
    if (!opts) return;
    clearTimeout(_revTipTimer);
    const tip = $("revHoverTip");
    const alreadyOpen = Boolean(tip && tip.classList.contains("show"));
    if (alreadyOpen) {
      showRevHoverTip(el, opts);
    } else {
      _revTipTimer = setTimeout(() => {
        const curOpts = resolveOpts();
        if (curOpts) showRevHoverTip(el, curOpts);
      }, 85);
    }
  };
  el.addEventListener("mouseenter", open);
  el.addEventListener("focus", open);
  el.addEventListener("mouseleave", hideRevHoverTip);
  el.addEventListener("blur", hideRevHoverTip);
  el.addEventListener("mousedown", hideRevHoverTip);
}

function syncRightSidebarVisibility(triggerResize = true) {
  const sb = $("sidebarRight");
  if (!sb) return;
  const isOverview = state.mode === "view" && state.fillDisplayMode === "margin";
  const shouldOpen = state.mode === "edit" || isOverview;
  sb.classList.toggle("overview-open", isOverview);
  sb.classList.toggle("collapsed", !shouldOpen);
  if (triggerResize) {
    setTimeout(() => { resizeCanvas(); fitCanvas(); draw(); }, 210);
  }
}

function refreshOverviewPanel() {
  if (state.mode !== "view" || state.fillDisplayMode !== "margin") return;
  const body = $("rightBody");
  const prevScroll = body ? body.scrollTop : 0;
  renderRightPanel();
  if (body) body.scrollTop = prevScroll;
}

function syncOverviewHighlightOnly(scrollToSelected = false) {
  if (typeof drawFxCanvas === "function") drawFxCanvas();
  else drawCanvasOnly();
  if (typeof syncFocusHighlightClasses === "function") {
    syncFocusHighlightClasses(scrollToSelected);
  }
}

function compareQuestionItem(a, b) {
  const qa = String((a.transcription && a.transcription.question_number) || a.number || "").trim();
  const qb = String((b.transcription && b.transcription.question_number) || b.number || "").trim();
  const na = (qa.match(/\d+/) || [null])[0];
  const nb = (qb.match(/\d+/) || [null])[0];
  if (na !== null && nb !== null && Number(na) !== Number(nb)) return Number(na) - Number(nb);
  if (na !== null && nb === null) return -1;
  if (na === null && nb !== null) return 1;
  if (qa !== qb) return qa.localeCompare(qb, "zh-CN");
  const ba = a.box || [0, 0, 0, 0];
  const bb = b.box || [0, 0, 0, 0];
  return (ba[1] - bb[1]) || (ba[0] - bb[0]);
}

function renderRightPanel() {
  hideRevHoverTip();
  const body = $("rightBody");
  body.innerHTML = "";
  const p = currentPage();
  if (!p) return;

  if (state.mode === "edit") {
    const recogKey = primaryKeyLabel("recognizeBox");
    const delKey = primaryKeyLabel("deleteBox");
    const fitKey = primaryKeyLabel("fitCanvas");
    const submitKey = primaryKeyLabel("nextSubmit");
    const isIter = state.workMode === "iteration";

    const scopeBar = document.createElement("div");
    scopeBar.className = "edit-scope-bar" + (isIter ? " iter" : "");
    scopeBar.innerHTML = isIter
      ? `<span class="edit-scope-left">${uiIcon("layers", "ui-ico-xs")}<span>改动将同步回流训练库</span></span><span class="edit-scope-tag">非临时</span>`
      : `<span class="edit-scope-left">${uiIcon("crop", "ui-ico-xs")}<span>临时调框 · 仅本批生效</span></span><span class="edit-scope-tag">改卷模式</span>`;
    bindRevHoverTip(scopeBar, isIter
      ? {
          title: "模型迭代模式 · 持久回流",
          badge: `按 ${submitKey} 写入`,
          color: "#2563eb",
          dot: true,
          body: "调框改动不只是本批临时生效：确认保存后会自动创建或更新 data/m1、data/m2、data/m3 训练文件夹，保存原图/裁图与标注日志，供后续模型训练使用。",
        }
      : {
          title: "改卷模式 · 临时修正",
          badge: "不写训练库",
          color: "#15803d",
          dot: true,
          body: "用于处理偶发框错位或漏框，只对当前批次识别与判分生效，不会写入训练库也不会创建训练目录；如需回流训练数据，请在 8888 工作台或「更多」菜单切到迭代模式。",
        }
    );
    body.appendChild(scopeBar);

    const secLayer = document.createElement("div");
    secLayer.className = "sec-card";
    secLayer.innerHTML = `
      <div class="sec-title"><span>显示图层</span><span class="sec-hint">${uiIcon("help", "ui-ico-xs")}<span>悬停看说明</span></span></div>
      <div class="layer-row" id="editLayerRow"></div>
    `;
    const layerRow = secLayer.querySelector("#editLayerRow");
    LAYER_ORDER.forEach(k => {
      const lm = LAYER_META[k];
      const lbl = document.createElement("label");
      lbl.className = "layer-pill" + (state.showLayers[k] ? " checked" : "");
      lbl.innerHTML = `<input type="checkbox" id="ly${k.toUpperCase()}" ${state.showLayers[k] ? "checked" : ""}><span>${escHtml(lm.label)}</span>`;
      const cb = lbl.querySelector("input");
      cb.onchange = (e) => {
        state.showLayers[k] = e.target.checked;
        lbl.classList.toggle("checked", e.target.checked);
        draw();
      };
      bindRevHoverTip(lbl, {
        title: lm.label,
        badge: lm.code + " 图层",
        color: lm.color,
        dot: true,
        body: lm.desc,
      });
      layerRow.appendChild(lbl);
    });
    body.appendChild(secLayer);

    const secCls = document.createElement("div");
    secCls.className = "sec-card";
    secCls.innerHTML = `<div class="sec-title"><span>新建框类别</span><span class="sec-hint">按数字键快切</span></div>`;
    LAYER_ORDER.forEach(stageKey => {
      const lm = LAYER_META[stageKey];
      const cids = CLS_ORDER.filter(cid => BOX_META[cid] && BOX_META[cid].stage === stageKey);
      if (!cids.length) return;
      const grp = document.createElement("div");
      grp.className = "cls-group";
      const gHead = document.createElement("div");
      gHead.className = "cls-group-head";
      gHead.innerHTML = `<span>${escHtml(lm.label)}</span>`;
      grp.appendChild(gHead);
      const clsList = document.createElement("div");
      clsList.className = "cls-list";
      cids.forEach(cid => {
        const m = BOX_META[cid];
        const btn = document.createElement("button");
        btn.type = "button";
        btn.dataset.clsId = String(cid);
        btn.className = "cls-btn" + (state.activeCls === cid ? " active" : "");
        btn.style.setProperty("--cls-color", m.color);
        btn.innerHTML = `
          <span class="cls-left"><span class="cls-dot${m.isZone ? " zone" : ""}" style="background:${m.color}"></span><span class="cls-name">${escHtml(m.name)}</span></span>
          <span class="cls-right">${m.aiCapable ? `<span class="cls-ai-tag">AI</span>` : ""}<span class="cls-key">${escHtml(primaryKeyLabel("cls_" + cid))}</span></span>
        `;
        btn.onclick = () => {
          state.activeCls = cid;
          state.showLayers[m.stage] = true;
          if (state.selectedBoxIdx >= 0 && state.workingBoxes[state.selectedBoxIdx] && state.workingBoxes[state.selectedBoxIdx].cls !== cid) {
            state.selectedBoxIdx = -1;
          }
          renderRightPanel();
          draw();
        };
        bindRevHoverTip(btn, {
          title: m.name,
          badge: m.aiCapable ? `${lm.label} · 按 ${recogKey} 识别` : lm.label,
          color: m.color,
          dot: true,
          body: m.desc,
        });
        clsList.appendChild(btn);
      });
      grp.appendChild(clsList);
      secCls.appendChild(grp);
    });
    body.appendChild(secCls);

    const secAct = document.createElement("div");
    secAct.className = "sec-card";
    const selBox = state.selectedBoxIdx >= 0 ? state.workingBoxes[state.selectedBoxIdx] : null;
    const selMeta = selBox ? (BOX_META[selBox.cls] || { name: String(selBox.cls), color: "#64748b" }) : null;
    const selConf = selBox ? (typeof selBox.confidence === "number" ? `${(selBox.confidence * 100).toFixed(1)}%` : "人工绘制") : "";
    const selBound = selBox ? findBoundItemForBox(selBox) : null;
    const selCanVlm = Boolean(selBox && isVlmRecogCls(selBox.cls));
    const selHasAns = Boolean(selBound && selBound.transcription);
    const pendingIndices = getPendingRecogBoxIndices();
    const busy = state.recognizingBusy;
    const selBtnLabel = busy
      ? `${uiIcon("loader", "ui-ico-sm ui-ico-spin")}<span>正在调用 API…</span>`
      : (selHasAns
          ? `${uiIcon("refresh", "ui-ico-sm")}<span>重新识别此框 (${escHtml(recogKey)})</span>`
          : `${uiIcon("sparkles", "ui-ico-sm")}<span>调用 API 识别此框 (${escHtml(recogKey)})</span>`);

    secAct.innerHTML = `
      <div class="sec-title"><span>快捷操作</span><span>共 ${state.workingBoxes.length} 框</span></div>
      ${selBox ? `<div class="sel-box-pill"><span class="cls-dot${selMeta.isZone ? " zone" : ""}" style="--cls-color:${selMeta.color};background:${selMeta.color}"></span><b>${escHtml(selMeta.name)}</b>${state.showConf ? `<span style="color:var(--muted);">（${escHtml(selConf)}）</span>` : ""}${selBox._needsRecog ? `<span style="color:#b45309;font-weight:700;">· 待识别</span>` : ""}</div>` : ""}
      <div style="display:flex;flex-direction:column;gap:6px;">
        ${selCanVlm ? `<button class="${(selBox._needsRecog || !selHasAns) ? "btn-ai" : "btn-ai-soft"}" id="btnRecogSel" ${busy ? "disabled" : ""} style="width:100%;padding:7px 10px;">${selBtnLabel}</button>` : ""}
        ${pendingIndices.length > 0 ? `<button class="btn-ai-soft" id="btnRecogPending" ${busy ? "disabled" : ""} style="width:100%;padding:6px 10px;">${busy ? `${uiIcon("loader", "ui-ico-sm ui-ico-spin")}<span>识别中…</span>` : `${uiIcon("sparkles", "ui-ico-sm")}<span>一键识别全部待识别框 (${pendingIndices.length})</span>`}</button>` : ""}
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button id="btnUndoBox" ${state.undoStack.length && !busy ? "" : "disabled"} style="flex:1;">${uiIcon("undo", "ui-ico-sm")}<span>撤销 (Ctrl+Z)</span></button>
          <button class="btn-danger" id="btnDelBox" ${selBox && !busy ? "" : "disabled"} style="flex:1;">${uiIcon("trash", "ui-ico-sm")}<span>${selBound ? "删框消卡" : "删除框"} (${escHtml(delKey)})</span></button>
        </div>
      </div>
      <div id="selRecogPreview"></div>
      <div class="edit-tip-wrap">
        <div class="edit-tip-head"><span>操作说明</span><span>悬停查看详情</span></div>
        <div class="edit-tip-grid" id="editTipGrid"></div>
      </div>
    `;
    const tipDefs = [
      {
        label: "拖画新框",
        icon: "crop",
        title: "左键长按拖动 · 画新框",
        badge: "含大框内",
        color: "#2563eb",
        body: `在任意位置（含蓝色非选答题区内部）按住左键拖动即可画出当前类别新框；画完「单题框」或「姓名学号」按 ${recogKey} 可直调 AI 识别。`,
      },
      {
        label: "单击调框",
        icon: "crosshair",
        title: "左键单击 · 选中与微调",
        badge: "Esc 取消",
        color: "#7c3aed",
        body: "单击任意框选中后，可拖四边/四角缩放或拖框内移动；多个框重叠时重复单击可循环切换。",
      },
      {
        label: "右键删框",
        icon: "trash",
        title: "右键单击 · 删框并消卡",
        badge: `或按 ${delKey}`,
        color: "#dc2626",
        body: "短按右键点击框（或选中后按 Delete）直接删框，切回看答案对应卡片同步消失；按 Ctrl+Z 可连同原答案无损恢复。",
      },
      {
        label: "平移缩放",
        icon: "search",
        title: "平移与缩放画布",
        badge: `${fitKey} 复位`,
        color: "#0891b2",
        body: `中键拖动、右键拖动或按住空格+左键拖动平移画布；滚轮缩放，双击空白处或按 ${fitKey} 适配整页。`,
      },
    ];
    const tipGrid = secAct.querySelector("#editTipGrid");
    tipDefs.forEach(td => {
      const pill = document.createElement("span");
      pill.className = "edit-tip-pill";
      pill.tabIndex = 0;
      pill.innerHTML = `${uiIcon(td.icon, "ui-ico-xs")}<span>${escHtml(td.label)}</span>`;
      bindRevHoverTip(pill, {
        title: td.title,
        badge: td.badge,
        color: td.color,
        dot: true,
        body: td.body,
      });
      tipGrid.appendChild(pill);
    });
    body.appendChild(secAct);
    if (!body._tipScrollBound) {
      body._tipScrollBound = true;
      body.addEventListener("scroll", hideRevHoverTip, { passive: true });
    }
    secAct.querySelector("#btnUndoBox").onclick = undoBoxEdit;
    secAct.querySelector("#btnDelBox").onclick = () => deleteBoxAt(state.selectedBoxIdx);
    const recSelBtn = secAct.querySelector("#btnRecogSel");
    if (recSelBtn) recSelBtn.onclick = () => recognizeBoxIndices([state.selectedBoxIdx]);
    const recPendBtn = secAct.querySelector("#btnRecogPending");
    if (recPendBtn) recPendBtn.onclick = () => recognizeBoxIndices(getPendingRecogBoxIndices());

    // 若当前选中框已有关联识别结果，直接在此预览，无需反复切模式
    const prevWrap = secAct.querySelector("#selRecogPreview");
    if (prevWrap && selBound && (selBound.kind === "fill" || selBound.kind === "identity")) {
      const boxDiv = document.createElement("div");
      boxDiv.className = "recog-preview-box";
      const qno = selBound.kind === "identity"
        ? "姓名学号"
        : `第 ${(selBound.transcription && selBound.transcription.question_number) || selBound.number || "?"} 题`;
      const head = document.createElement("div");
      head.className = "recog-preview-head";
      head.innerHTML = `<span>已关联答案 · ${escHtml(qno)}</span><span style="color:var(--muted);font-weight:500;">${escHtml(selBound.status || "")}</span>`;
      boxDiv.appendChild(head);
      const ansHolder = document.createElement("div");
      ansHolder.className = "margin-ans";
      if (selBound.kind === "identity") {
        ansHolder.textContent = getIdentityCompactText(selBound);
      } else {
        renderFillMarginInto(ansHolder, selBound, "edit");
      }
      boxDiv.appendChild(ansHolder);
      prevWrap.appendChild(boxDiv);
    }
    return;
  }

  // 看答案模式：右侧停靠「整页答案总览栏」（按题号升序整齐排列，零几何 if/else、双向联动定位）
  const pgGrade = getPageGrading(p);
  const allFills = getPageItems(p)
    .filter(it => {
      if (it.kind !== "fill" || !shouldShowFillInView(it)) return false;
      const b = it.box || [0, 0, 0, 0];
      const bw = b[2] - b[0], bh = b[3] - b[1];
      if (bh > bw * 1.4 && bw < 95) return false; // 竖条姓名栏单独处理
      return true;
    })
    .sort(compareQuestionItem);

  const isEditedFill = (it) => Boolean(state.itemEdits[it.id] || ((state.feedback.items || {})[it.id] || {}).correction);
  const hasWrongOrPending = (it) => {
    const itG = pgGrade && pgGrade.items ? pgGrade.items[it.id] : null;
    if (!itG || !itG.blanks) return false;
    return Object.values(itG.blanks).some(bg => bg && ["wrong", "partial", "pending"].includes(bg.status));
  };

  const cntAll = allFills.length;
  const cntWrong = allFills.filter(hasWrongOrPending).length;
  const cntUnans = allFills.filter(it => !isEditedFill(it) && hasUnansweredBlank(it)).length;
  const cntEdited = allFills.filter(isEditedFill).length;

  // 若当前筛选条件已无数据则自动回退到全部
  if ((state.overviewFilter === "wrong" && cntWrong === 0)
    || (state.overviewFilter === "unanswered" && cntUnans === 0)
    || (state.overviewFilter === "edited" && cntEdited === 0)) {
    state.overviewFilter = "all";
  }

  const filteredFills = allFills.filter(it => {
    if (state.overviewFilter === "wrong") return hasWrongOrPending(it);
    if (state.overviewFilter === "unanswered") return !isEditedFill(it) && hasUnansweredBlank(it);
    if (state.overviewFilter === "edited") return isEditedFill(it);
    return true;
  });

  const header = document.createElement("div");
  header.className = "ov-header";
  const sm = pgGrade && pgGrade.has_schema ? (pgGrade.summary || {}) : null;
  header.innerHTML = `
    <div class="ov-head-top">
      <div class="ov-head-title">
        ${uiIcon("panelRight")}
        <span>整页答案总览</span>
        <span class="ov-count-pill">${cntAll} 题</span>
        ${sm ? `<span class="ov-score-pill">${fmtScoreNum(sm.total_earned)}/${fmtScoreNum(sm.total_possible)}分</span>` : ""}
      </div>
      <button type="button" class="ov-close-btn" id="btnCloseOverviewDock" title="收起右侧总览栏"><span>收起</span>${uiIcon("x", "ui-ico-xs")}</button>
    </div>
    <div class="ov-filters" id="ovFilterBar"></div>
  `;
  body.appendChild(header);

  const closeDockBtn = header.querySelector("#btnCloseOverviewDock");
  if (closeDockBtn) {
    closeDockBtn.onclick = () => {
      if (state.fillDisplayMode !== "hover") {
        $("btnFillDisplayMode").click();
      } else {
        $("sidebarRight").classList.add("collapsed");
        setTimeout(() => { resizeCanvas(); fitCanvas(); draw(); }, 210);
      }
    };
  }

  const filterBar = header.querySelector("#ovFilterBar");
  const filterDefs = [{ key: "all", label: `全部 (${cntAll})` }];
  if (pgGrade && pgGrade.has_schema && cntWrong > 0) filterDefs.push({ key: "wrong", label: `错/待核 (${cntWrong})` });
  if (cntUnans > 0) filterDefs.push({ key: "unanswered", label: `未作答 (${cntUnans})` });
  if (cntEdited > 0) filterDefs.push({ key: "edited", label: `已改 (${cntEdited})` });
  filterDefs.forEach(fd => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ov-filter-btn" + (state.overviewFilter === fd.key ? " active" : "");
    b.textContent = fd.label;
    b.onclick = () => {
      state.overviewFilter = fd.key;
      renderRightPanel();
    };
    filterBar.appendChild(b);
  });

  const focusId = getLockedFillItemId() || state.hoverItemId || state.selectedItemId || "";
  const list = document.createElement("div");
  list.className = "ov-list" + (focusId && filteredFills.some(x => x.id === focusId) ? " has-focus" : "");
  if (!filteredFills.length) {
    const empty = document.createElement("div");
    empty.className = "ov-empty";
    empty.textContent = cntAll ? "当前筛选条件下没有题目" : "本页暂无填空/简答题框";
    list.appendChild(empty);
  } else {
    filteredFills.forEach(it => {
      const edited = isEditedFill(it);
      const qno = (it.transcription && it.transcription.question_number) || it.number || "?";
      const itG = pgGrade && pgGrade.items ? pgGrade.items[it.id] : null;
      const card = document.createElement("div");
      card.dataset.itemId = it.id;
      card.className = "ov-card"
        + (edited ? " edited" : "")
        + ((state.selectedItemId === it.id || state.pinnedItemId === it.id) ? " active" : "")
        + (state.hoverItemId === it.id ? " hovered" : "");

      const cHead = document.createElement("div");
      cHead.className = "ov-card-head";
      cHead.title = "点击将左侧原卷居中定位到本题";

      const leftInfo = document.createElement("div");
      leftInfo.style.cssText = "display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;";
      const qBadge = document.createElement("span");
      qBadge.className = "margin-qno";
      qBadge.textContent = `第 ${qno} 题`;
      leftInfo.appendChild(qBadge);
      if (itG) {
        const sc = document.createElement("span");
        const full = Number(itG.earned) >= Number(itG.possible) && Number(itG.possible) > 0;
        const zero = Number(itG.earned) <= 0;
        sc.className = "ov-score-tag " + (full ? "full" : (zero ? "zero" : "part"));
        sc.textContent = `${fmtScoreNum(itG.earned)}/${fmtScoreNum(itG.possible)}`;
        leftInfo.appendChild(sc);
      } else if (edited) {
        const ed = document.createElement("span");
        ed.className = "ov-score-tag full";
        ed.textContent = "已订正";
        leftInfo.appendChild(ed);
      }
      cHead.appendChild(leftInfo);

      const locBtn = document.createElement("span");
      locBtn.className = "ov-locate-btn";
      locBtn.innerHTML = `${uiIcon("crosshair", "ui-ico-xs")}<span>定位</span>`;
      cHead.appendChild(locBtn);

      cHead.onclick = (e) => {
        e.stopPropagation();
        state.pinnedItemId = null;
        state.selectedItemId = it.id;
        state.hoverItemId = it.id;
        state.hoverSource = "overview";
        panToBoxOnCanvas(it.box);
        draw();
        syncOverviewHighlightOnly(false);
      };
      card.appendChild(cHead);

      const ansEl = document.createElement("div");
      ansEl.className = "margin-ans";
      renderFillMarginInto(ansEl, it, "overview");
      card.appendChild(ansEl);

      card.onmouseenter = () => {
        if (getLockedFillItemId()) return;
        clearHoverTimer();
        if (state.hoverItemId !== it.id || state.hoverSource !== "overview") {
          state.hoverItemId = it.id;
          state.hoverSource = "overview";
          syncHoverPopoverOnly();
        }
      };
      card.onmouseleave = () => {
        if (getLockedFillItemId()) return;
        if (state.hoverSource === "overview" && state.hoverItemId === it.id) {
          scheduleOverviewHoverClear(it.id);
        }
      };
      list.appendChild(card);
    });
  }
  body.appendChild(list);

  // 选择题紧凑速览（默认折叠在底部，不干扰填空主清单）
  const choices = getPageItems(p).filter(i => i.kind === "choice");
  if (choices.length) {
    const det = document.createElement("details");
    det.className = "ov-choice-details";
    const sum = document.createElement("summary");
    sum.textContent = `选择题速览（${choices.length} 题 · 点击展开）`;
    det.appendChild(sum);
    const grid = document.createElement("div");
    grid.className = "choice-grid";
    grid.style.marginTop = "8px";
    choices.forEach(it => {
      const chip = document.createElement("div");
      const edited = Boolean(
        (state.itemEdits[it.id] && state.itemEdits[it.id].answer !== undefined) ||
        (((state.feedback.items || {})[it.id] || {}).answer !== undefined)
      );
      chip.className = "choice-chip" + (isLowConfChoice(it) ? " low-conf" : "") + (edited ? " edited" : "");
      chip.innerHTML = `
        <div class="choice-top"><span class="choice-no">${escHtml(it.number || "?")}</span><span class="choice-ans">${escHtml(getChoiceDisplayAnswer(it))}</span></div>
        <div class="choice-conf">${escHtml(choiceConfText(it) || "—")}</div>
      `;
      chip.onclick = () => {
        panToBoxOnCanvas(it.box);
        openInlineEditor(it);
      };
      grid.appendChild(chip);
    });
    det.appendChild(grid);
    body.appendChild(det);
  }
}

/* ==================== 画布绘制与精准命中检测（整边 + 四角缩放） ==================== */
