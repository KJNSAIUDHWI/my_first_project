/* ==================== 右侧栏（调框工具 / 可选清单） ==================== */
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
      ? "⏳ 正在调用 API…"
      : (selHasAns ? `↻ 重新识别此框 (${recogKey})` : `✨ 调用 API 识别此框 (${recogKey})`);

    secAct.innerHTML = `
      <div class="sec-title"><span>快捷操作</span><span>共 ${state.workingBoxes.length} 框</span></div>
      ${selBox ? `<div style="font-size:12px;margin-bottom:7px;color:var(--text);">当前选中：<b>${escHtml(selMeta.name)}</b> <span style="color:var(--muted);">（${escHtml(selConf)}）</span>${selBox._needsRecog ? ` <span style="color:#b45309;font-weight:700;">· 待识别</span>` : ""}</div>` : ""}
      <div style="display:flex;flex-direction:column;gap:6px;">
        ${selCanVlm ? `<button class="${(selBox._needsRecog || !selHasAns) ? "btn-ai" : "btn-ai-soft"}" id="btnRecogSel" ${busy ? "disabled" : ""} style="width:100%;padding:7px 10px;">${selBtnLabel}</button>` : ""}
        ${pendingIndices.length > 0 ? `<button class="btn-ai-soft" id="btnRecogPending" ${busy ? "disabled" : ""} style="width:100%;padding:6px 10px;">${busy ? "⏳ 识别中…" : `✨ 一键识别本页全部待识别框 (${pendingIndices.length})`}</button>` : ""}
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          <button id="btnUndoBox" ${state.undoStack.length && !busy ? "" : "disabled"} style="flex:1;">↶ 撤销 (Ctrl+Z)</button>
          <button class="btn-danger" id="btnDelBox" ${selBox && !busy ? "" : "disabled"} style="flex:1;">🗑 ${selBound ? "删框并消卡" : "删除框"} (${primaryKeyLabel("deleteBox")})</button>
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
        renderFillMarginInto(ansHolder, selBound);
      }
      boxDiv.appendChild(ansHolder);
      prevWrap.appendChild(boxDiv);
    }
    return;
  }

  // 看答案模式下的可选右侧汇总（平时默认收起）
  const choices = getPageItems(p).filter(i => i.kind === "choice");
  if (choices.length) {
    const sec = document.createElement("div");
    sec.className = "sec-card";
    sec.innerHTML = `<div class="sec-title"><span>选择题速览（${choices.length} 题）</span></div>`;
    const grid = document.createElement("div");
    grid.className = "choice-grid";
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
      chip.onclick = () => openInlineEditor(it);
      grid.appendChild(chip);
    });
    sec.appendChild(grid);
    body.appendChild(sec);
  }
  const tip = document.createElement("div");
  tip.className = "sec-card";
  tip.style.color = "var(--muted)";
  tip.style.lineHeight = "1.6";
  tip.innerHTML = `所有手写与选择识别结果已直接显示在原卷框上，<b>点击原卷上的任意框即可直接修改答案</b>。平时可收起右侧栏让原卷更大。`;
  body.appendChild(tip);
}

/* ==================== 画布绘制与精准命中检测（整边 + 四角缩放） ==================== */
