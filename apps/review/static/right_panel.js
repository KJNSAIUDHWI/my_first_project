/* ==================== 右侧栏（调框工具 / 右侧停靠整页答案总览栏） ==================== */
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
  const body = $("rightBody");
  body.innerHTML = "";
  const p = currentPage();
  if (!p) return;

  if (state.mode === "edit") {
    const secLayer = document.createElement("div");
    secLayer.className = "sec-card";
    secLayer.innerHTML = `
      <div class="sec-title"><span>显示标框层级</span></div>
      <div class="layer-row">
        <label class="chk-label"><input type="checkbox" id="lyM1" ${state.showLayers.m1 ? "checked" : ""}> M1 版面</label>
        <label class="chk-label"><input type="checkbox" id="lyM2" ${state.showLayers.m2 ? "checked" : ""}> M2 选择</label>
        <label class="chk-label"><input type="checkbox" id="lyM3" ${state.showLayers.m3 ? "checked" : ""}> M3 大题</label>
      </div>
    `;
    body.appendChild(secLayer);
    ["m1", "m2", "m3"].forEach(k => {
      secLayer.querySelector("#ly" + k.toUpperCase()).onchange = (e) => { state.showLayers[k] = e.target.checked; draw(); };
    });

    const secCls = document.createElement("div");
    secCls.className = "sec-card";
    secCls.innerHTML = `<div class="sec-title"><span>新建框类别</span></div>`;
    const clsList = document.createElement("div");
    clsList.className = "cls-list";
    CLS_ORDER.forEach(cid => {
      const m = BOX_META[cid];
      const btn = document.createElement("button");
      btn.className = "cls-btn" + (state.activeCls === cid ? " active" : "");
      btn.innerHTML = `
        <span class="cls-left"><span class="cls-dot" style="background:${m.color}"></span>${m.name} <small style="color:var(--muted)">(${m.stage.toUpperCase()})</small></span>
        <span class="cls-key">${primaryKeyLabel("cls_" + cid)}</span>
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
      clsList.appendChild(btn);
    });
    secCls.appendChild(clsList);
    body.appendChild(secCls);

    const secAct = document.createElement("div");
    secAct.className = "sec-card";
    const selBox = state.selectedBoxIdx >= 0 ? state.workingBoxes[state.selectedBoxIdx] : null;
    const selMeta = selBox ? (BOX_META[selBox.cls] || { name: String(selBox.cls) }) : null;
    const selConf = selBox ? (typeof selBox.confidence === "number" ? `${(selBox.confidence * 100).toFixed(1)}%` : "人工绘制") : "";
    const selBound = selBox ? findBoundItemForBox(selBox) : null;
    const selCanVlm = Boolean(selBox && isVlmRecogCls(selBox.cls));
    const selHasAns = Boolean(selBound && selBound.transcription);
    const pendingIndices = getPendingRecogBoxIndices();
    const recogKey = primaryKeyLabel("recognizeBox");
    const busy = state.recognizingBusy;
    const selBtnLabel = busy
      ? `${uiIcon("loader", "ui-ico-sm ui-ico-spin")}<span>正在调用 API…</span>`
      : (selHasAns
          ? `${uiIcon("refresh", "ui-ico-sm")}<span>重新识别此框 (${escHtml(recogKey)})</span>`
          : `${uiIcon("sparkles", "ui-ico-sm")}<span>调用 API 识别此框 (${escHtml(recogKey)})</span>`);

    secAct.innerHTML = `
      <div class="sec-title"><span>快捷操作</span><span>共 ${state.workingBoxes.length} 框</span></div>
      ${selBox ? `<div style="font-size:12px;margin-bottom:7px;color:var(--text);">当前选中：<b>${escHtml(selMeta.name)}</b> <span style="color:var(--muted);">（${escHtml(selConf)}）</span>${selBox._needsRecog ? ` <span style="color:#b45309;font-weight:700;">· 待识别</span>` : ""}</div>` : ""}
      <div style="display:flex;flex-direction:column;gap:6px;">
        ${selCanVlm ? `<button class="${(selBox._needsRecog || !selHasAns) ? "btn-ai" : "btn-ai-soft"}" id="btnRecogSel" ${busy ? "disabled" : ""} style="width:100%;padding:7px 10px;">${selBtnLabel}</button>` : ""}
        ${pendingIndices.length > 0 ? `<button class="btn-ai-soft" id="btnRecogPending" ${busy ? "disabled" : ""} style="width:100%;padding:6px 10px;">${busy ? `${uiIcon("loader", "ui-ico-sm ui-ico-spin")}<span>识别中…</span>` : `${uiIcon("sparkles", "ui-ico-sm")}<span>一键识别本页全部待识别框 (${pendingIndices.length})</span>`}</button>` : ""}
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button id="btnUndoBox" ${state.undoStack.length && !busy ? "" : "disabled"} style="flex:1;">${uiIcon("undo", "ui-ico-sm")}<span>撤销 (Ctrl+Z)</span></button>
          <button class="btn-danger" id="btnDelBox" ${selBox && !busy ? "" : "disabled"} style="flex:1;">${uiIcon("trash", "ui-ico-sm")}<span>${selBound ? "删框并消卡" : "删除框"} (${escHtml(primaryKeyLabel("deleteBox"))})</span></button>
        </div>
      </div>
      <div id="selRecogPreview"></div>
      <div style="margin-top:8px;font-size:11.5px;color:var(--muted);line-height:1.65;">
        • <b>左键长按拖动</b>：任意位置（含蓝色大框内）直接画新框；<b>左键单击</b>：选中框后可拖边/角缩放、拖框内移动（<b>Esc</b> 取消选中）<br>
        • <b>删框即消卡</b>：<b>右键单击框</b>或按 Delete 删框，切回看答案卡片立即消失（<b>Ctrl+Z</b> 连原答案无损恢复）<br>
        • <b>新框直调 API</b>：画完「大题-框 (6)」或「姓名学号 (1)」后按 <b>${recogKey}</b> 直接调用大模型识别<br>
        • <b>平移画布</b>：中键/右键拖动 或 空格+左键拖动
      </div>
    `;
    body.appendChild(secAct);
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
        clearTimeout(hoverHideTimer);
        if (state.hoverItemId !== it.id || state.hoverSource !== "overview") {
          state.hoverItemId = it.id;
          state.hoverSource = "overview";
          syncHoverPopoverOnly();
        }
      };
      card.onmouseleave = () => {
        if (getLockedFillItemId()) return;
        if (state.hoverSource === "overview" && state.hoverItemId === it.id) {
          state.hoverItemId = null;
          state.hoverSource = null;
          syncHoverPopoverOnly();
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
