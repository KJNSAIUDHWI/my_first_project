/* ==================== 卷面答案层（手写框内 0% 遮挡 + 右侧水平齐平外挂批注卡） ==================== */
function getIdentityCompactText(it) {
  const edited = getFillDisplayText(it);
  if (state.itemEdits[it.id] || ((state.feedback.items || {})[it.id] || {}).correction) return edited;
  if (it.transcription) {
    const nm = it.transcription.name || "";
    const sid = it.transcription.sid || "";
    if (nm || sid) return [nm, sid].filter(Boolean).join(" · ");
  }
  return edited || "姓名学号";
}

const CIRCLED_NUMS = ["", "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"];
function fmtBlankIdx(k) {
  const n = Number(k);
  if (Number.isInteger(n) && n >= 1 && n < CIRCLED_NUMS.length) return CIRCLED_NUMS[n];
  return `[${k}]`;
}

const _katexHtmlCache = new Map();
function appendLatexText(parent, text) {
  const parts = String(text || "").split(/(\$[^$]+\$)/g);
  for (const part of parts) {
    if (part.startsWith("$") && part.endsWith("$") && part.length > 2 && window.katex) {
      const kspan = document.createElement("span");
      const tex = part.slice(1, -1);
      let cached = _katexHtmlCache.get(tex);
      if (cached === undefined) {
        try {
          cached = window.katex.renderToString(tex, { throwOnError: false });
          if (_katexHtmlCache.size > 400) _katexHtmlCache.clear();
          _katexHtmlCache.set(tex, cached);
        } catch {
          cached = null;
        }
      }
      if (cached) kspan.innerHTML = cached;
      else kspan.textContent = part;
      parent.appendChild(kspan);
    } else {
      parent.appendChild(document.createTextNode(part));
    }
  }
}

function hasUnansweredBlank(it) {
  const ans = it && it.transcription && it.transcription.answers;
  if (!ans || typeof ans !== "object") return false;
  for (const blanks of Object.values(ans)) {
    if (blanks && typeof blanks === "object" && blanks["舍弃"] !== "舍弃") {
      for (const v of Object.values(blanks)) {
        if (!String(v == null ? "" : v).trim()) return true;
      }
    }
  }
  return false;
}

function compareSubKey(a, b) {
  if (a === b) return 0;
  if (a === "无小题号") return -1;
  if (b === "无小题号") return 1;
  const pa = String(a).split(/[^0-9]+/).filter(Boolean).map(Number);
  const pb = String(b).split(/[^0-9]+/).filter(Boolean).map(Number);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    if (pa[i] === undefined) return -1;
    if (pb[i] === undefined) return 1;
    if (pa[i] !== pb[i]) return pa[i] - pb[i];
  }
  return String(a).localeCompare(String(b), "zh-CN");
}

let _pillClickTimer = null;
function renderFillMarginInto(el, it, containerSource = "pop") {
  el.innerHTML = "";
  const effAns = getEffectiveFillAnswersJS(it);
  const pgGrade = getPageGrading();
  const itemGrade = pgGrade && pgGrade.items ? pgGrade.items[it.id] : null;
  const blankGrades = (itemGrade && itemGrade.blanks) || {};
  const nowTs = Date.now();

  if (effAns && typeof effAns === "object" && Object.keys(effAns).length > 0) {
    const entries = Object.entries(effAns).sort((a, b) => compareSubKey(a[0], b[0]));
    entries.forEach(([sub, blanks]) => {
      const seg = document.createElement("span");
      seg.className = "sub-seg";
      if (sub !== "无小题号") {
        const lbl = document.createElement("span");
        lbl.className = "sub-lbl";
        lbl.textContent = `(${sub})`;
        seg.appendChild(lbl);
      }
      if (blanks && typeof blanks === "object") {
        if (blanks["舍弃"] === "舍弃") {
          const pill = document.createElement("span");
          pill.className = "blank-pill discarded";
          pill.textContent = "含图舍弃";
          seg.appendChild(pill);
        } else {
          Object.entries(blanks)
            .sort((a, b) => (Number(a[0]) || 999) - (Number(b[0]) || 999) || String(a[0]).localeCompare(String(b[0])))
            .forEach(([k, v]) => {
              const rawStr = String(v == null ? "" : v).trim();
              const isBlank = rawStr.length === 0;
              const slotKey = `${sub}:${k}`;
              const bg = blankGrades[slotKey] || null;
              const isEditingThis = Boolean(
                state.inlineBlankEdit &&
                state.inlineBlankEdit.itemId === it.id &&
                String(state.inlineBlankEdit.sub) === String(sub) &&
                String(state.inlineBlankEdit.blank) === String(k) &&
                (!state.inlineBlankEdit.source || state.inlineBlankEdit.source === containerSource)
              );
              const isMulti = isBlankMultiSelected(it.id, sub, k);
              const isFlash = Boolean((state.flashSlots[`${it.id}|${slotKey}`] || 0) > nowTs);

              const pill = document.createElement("span");
              let cls = "blank-pill" + (isBlank ? " unanswered" : "");
              if (bg && bg.status && ["correct", "wrong", "partial", "pending"].includes(bg.status)) {
                cls += " grade-" + bg.status;
              }
              if (isMulti) cls += " multi-sel";
              if (isFlash) cls += " flash-ok";
              pill.className = cls;
              pill.dataset.itemId = it.id;
              pill.dataset.sub = String(sub);
              pill.dataset.blank = String(k);

              const idx = document.createElement("span");
              idx.className = "blank-idx";
              idx.textContent = fmtBlankIdx(k);
              pill.appendChild(idx);

              if (isEditingThis) {
                const inp = document.createElement("input");
                inp.type = "text";
                inp.className = "blank-inline-input";
                inp.value = rawStr;
                inp.placeholder = "输入答案（回车保存 / Tab下一空）";
                inp.onmousedown = (e) => e.stopPropagation();
                inp.onclick = (e) => e.stopPropagation();
                inp.ondblclick = (e) => e.stopPropagation();
                let settled = false;
                const finish = (commit, moveDir = 0) => {
                  if (settled) return;
                  settled = true;
                  if (commit) {
                    commitInlineBlankEdit(it, sub, k, inp.value, moveDir);
                  } else {
                    state.inlineBlankEdit = null;
                    refreshOverviewPanel();
                    draw();
                  }
                };
                inp.onkeydown = (e) => {
                  e.stopPropagation();
                  if (e.key === "Enter") { e.preventDefault(); finish(true, 0); }
                  else if (e.key === "Tab") { e.preventDefault(); finish(true, e.shiftKey ? -1 : 1); }
                  else if (e.key === "Escape") { e.preventDefault(); finish(false, 0); }
                };
                inp.onblur = () => finish(true, 0);
                pill.appendChild(inp);
                setTimeout(() => { try { inp.focus(); inp.select(); } catch {} }, 0);
              } else {
                const val = document.createElement("span");
                if (isBlank) {
                  val.textContent = "未作答";
                } else {
                  appendLatexText(val, rawStr);
                }
                pill.appendChild(val);

                if (bg && bg.status && ["correct", "wrong", "partial", "pending"].includes(bg.status)) {
                  const jb = document.createElement("button");
                  jb.type = "button";
                  jb.className = "blank-judge-btn " + bg.status;
                  jb.textContent = bg.status === "correct"
                    ? `对 +${fmtScoreNum(bg.earned)}`
                    : (bg.status === "partial" ? `半 +${fmtScoreNum(bg.earned)}`
                    : (bg.status === "pending" ? "待核" : "错"));
                  jb.title = (bg.standard ? `参考答案：${bg.standard} · ` : "") + "点击切换人工判分（对/半/错/自动）";
                  jb.onmousedown = (e) => e.stopPropagation();
                  jb.onclick = (e) => { e.stopPropagation(); clearTimeout(_pillClickTimer); cycleBlankJudge(it, slotKey, bg.status); };
                  jb.ondblclick = (e) => e.stopPropagation();
                  pill.appendChild(jb);
                }

                const stdTip = (bg && bg.standard) ? `参考答案：${bg.standard}\n` : "";
                pill.title = `${stdTip}单击：原地改字 · 双击：聚焦大框拉小框精准重识别 · Shift/Ctrl+点：连续多空联合重识别`;
                pill.onmousedown = (e) => {
                  if (e.button === 0 && !spaceDown) {
                    e.stopPropagation();
                    // 鼠标刚按下即静默锁定当前题，不重绘 DOM，避免点击/双击期间气泡溜走
                    if (containerSource === "pop") state.pinnedItemId = it.id;
                    state.selectedItemId = it.id;
                  }
                };
                pill.onclick = (e) => {
                  e.stopPropagation();
                  if (containerSource === "pop") state.pinnedItemId = it.id;
                  state.selectedItemId = it.id;
                  if (e.shiftKey || e.ctrlKey || e.metaKey) {
                    clearTimeout(_pillClickTimer);
                    toggleMultiBlankSelection(it, sub, k, Boolean(e.shiftKey));
                    return;
                  }
                  clearTimeout(_pillClickTimer);
                  _pillClickTimer = setTimeout(() => {
                    state.selectedItemId = it.id;
                    if (containerSource === "pop") state.pinnedItemId = it.id;
                    else if (containerSource === "overview") state.pinnedItemId = null;
                    state.inlineChipEdit = null;
                    state.inlineBlankEdit = { itemId: it.id, sub: String(sub), blank: String(k), source: containerSource };
                    refreshOverviewPanel();
                    draw();
                  }, 185);
                };
                pill.ondblclick = (e) => {
                  e.stopPropagation();
                  clearTimeout(_pillClickTimer);
                  enterSubboxReOcr(it, [{ sub: String(sub), blank: String(k) }]);
                };
              }
              seg.appendChild(pill);
            });
        }
      } else {
        const rawStr = String(blanks == null ? "" : blanks).trim();
        const pill = document.createElement("span");
        pill.className = "blank-pill" + (rawStr ? "" : " unanswered");
        appendLatexText(pill, rawStr || "未作答");
        seg.appendChild(pill);
      }
      el.appendChild(seg);
    });
    return;
  }
  const hasManualEdit = Boolean(
    (state.itemEdits[it.id] && state.itemEdits[it.id].correction !== undefined) ||
    (((state.feedback.items || {})[it.id] || {}).correction)
  );
  if (!hasManualEdit) {
    if (it.status === "等待视觉转录" || it.status === "等待豆包") {
      const pill = document.createElement("span");
      pill.className = "blank-pill pending";
      pill.innerHTML = `${uiIcon("loader", "ui-ico-xs ui-ico-spin")}<span>正在识别中…</span>`;
      el.appendChild(pill);
      return;
    }
    if (it.status === "调用失败" || it.status === "结构异常" || it.error) {
      const pill = document.createElement("span");
      pill.className = "blank-pill failed";
      const reason = it.error || (it.issues && it.issues[0]) || it.status || "识别失败";
      pill.innerHTML = `${uiIcon("alert", "ui-ico-xs")}<span>${escHtml(reason)}</span>`;
      pill.title = "双击直接进入聚焦局部重识别";
      pill.ondblclick = (e) => { e.stopPropagation(); enterSubboxReOcr(it, [{ sub: "无小题号", blank: "1" }]); };
      el.appendChild(pill);
      return;
    }
  }
  const lines = String(getFillDisplayText(it) || "").split("\n").map(s => s.trim()).filter(Boolean);
  if (!lines.length) {
    const pill = document.createElement("span");
    pill.className = "blank-pill unanswered";
    pill.textContent = "未作答";
    pill.onmousedown = (e) => {
      if (e.button === 0 && !spaceDown) {
        e.stopPropagation();
        if (containerSource === "pop") state.pinnedItemId = it.id;
        state.selectedItemId = it.id;
      }
    };
    pill.onclick = (e) => {
      e.stopPropagation();
      if (containerSource === "pop") state.pinnedItemId = it.id;
      else if (containerSource === "overview") state.pinnedItemId = null;
      state.selectedItemId = it.id;
      state.inlineBlankEdit = { itemId: it.id, sub: "无小题号", blank: "1", source: containerSource };
      refreshOverviewPanel();
      draw();
    };
    pill.ondblclick = (e) => { e.stopPropagation(); enterSubboxReOcr(it, [{ sub: "无小题号", blank: "1" }]); };
    el.appendChild(pill);
    return;
  }
  lines.forEach((line, li) => {
    const seg = document.createElement("span");
    seg.className = "sub-seg";
    const pill = document.createElement("span");
    pill.className = "blank-pill";
    appendLatexText(pill, line);
    pill.onmousedown = (e) => {
      if (e.button === 0 && !spaceDown) {
        e.stopPropagation();
        if (containerSource === "pop") state.pinnedItemId = it.id;
        state.selectedItemId = it.id;
      }
    };
    pill.onclick = (e) => {
      e.stopPropagation();
      if (containerSource === "pop") state.pinnedItemId = it.id;
      else if (containerSource === "overview") state.pinnedItemId = null;
      state.selectedItemId = it.id;
      state.inlineBlankEdit = { itemId: it.id, sub: "无小题号", blank: String(li + 1), source: containerSource };
      refreshOverviewPanel();
      draw();
    };
    pill.ondblclick = (e) => { e.stopPropagation(); enterSubboxReOcr(it, [{ sub: "无小题号", blank: String(li + 1) }]); };
    seg.appendChild(pill);
    el.appendChild(seg);
  });
}

function renderLowConfFocusOverlay() {
  const f = state.focusedLowConf;
  if (!f || !state.img || !f.box || f.box.length !== 4) return;
  const [x1, y1, x2, y2] = f.box;
  const sx1 = x1 * state.scale + state.tx;
  const sy1 = y1 * state.scale + state.ty;
  const sx2 = x2 * state.scale + state.tx;
  const sy2 = y2 * state.scale + state.ty;

  const boxEl = document.createElement("div");
  boxEl.className = "lowconf-focus-box";
  boxEl.style.left = Math.round(sx1 - 3) + "px";
  boxEl.style.top = Math.round(sy1 - 3) + "px";
  boxEl.style.width = Math.max(16, Math.round(sx2 - sx1 + 6)) + "px";
  boxEl.style.height = Math.max(16, Math.round(sy2 - sy1 + 6)) + "px";

  const tag = document.createElement("div");
  tag.className = "lowconf-focus-tag";
  const actionHint = f.kind === "box"
    ? (state.mode === "edit" ? "已选中可调" : "点文字调框")
    : "点文字改答案";
  const txt = document.createElement("span");
  txt.style.cssText = "display:inline-flex;align-items:center;gap:4px;";
  txt.innerHTML = `${uiIcon("alert", "ui-ico-xs")}<span>低置 ${f.index}/${f.total} · ${escHtml(f.label)} · ${escHtml(actionHint)}</span>`;
  tag.appendChild(txt);

  const trustBtn = document.createElement("button");
  trustBtn.className = "lowconf-trust-btn";
  trustBtn.innerHTML = `${uiIcon("check", "ui-ico-xs")}<span>信任无误</span>`;
  trustBtn.title = "确认此处识别无误，立即清除低置信度橙色警告";
  trustBtn.onmousedown = (e) => { e.stopPropagation(); };
  trustBtn.onclick = (e) => {
    e.stopPropagation();
    trustLowConfTarget(f);
  };
  tag.appendChild(trustBtn);
  // 如果靠近画布顶部，把标签移到框下方，避免被顶栏裁切
  if (sy1 < 36) {
    tag.style.bottom = "auto";
    tag.style.top = "calc(100% + 5px)";
  }
  tag.onmousedown = (e) => { if (e.button === 0 && !spaceDown) e.stopPropagation(); };
  tag.onclick = (e) => {
    e.stopPropagation();
    if (f.kind === "box") {
      if (state.mode !== "edit") {
        setMode("edit");
      }
      state.showLayers[f.stage] = true;
      state.showConf = true;
      $("chkConf").checked = true;
      document.body.classList.add("show-conf");
      state.selectedBoxIdx = f.idx;
      renderRightPanel();
      draw();
    } else if (f.item) {
      if (state.mode !== "view") setMode("view");
      openInlineEditor(f.item);
    }
  };
  boxEl.appendChild(tag);
  overlayLayer.appendChild(boxEl);
}

let hoverHideTimer = null;
function getLockedFillItemId() {
  if (state.inlineBlankEdit && state.inlineBlankEdit.itemId && state.inlineBlankEdit.source !== "overview") {
    return state.inlineBlankEdit.itemId;
  }
  if (state.pinnedItemId) return state.pinnedItemId;
  return null;
}
function getActiveFocusItemId() {
  return getLockedFillItemId() || state.hoverItemId || state.selectedItemId || null;
}
function getFloatingPopoverItemId() {
  if (state.mode !== "view" || !state.showOverlay || state.editingPopItemId || state.subboxTarget) return null;
  if (state.inlineBlankEdit && state.inlineBlankEdit.source === "overview") return null;
  const lockedId = getLockedFillItemId();
  if (lockedId) return lockedId;
  if (state.hoverItemId && state.hoverSource !== "overview") return state.hoverItemId;
  return null;
}
let _ovCenterTimer = null;
function centerOverviewCard(itemId, delayMs = 0) {
  clearTimeout(_ovCenterTimer);
  if (state.mode !== "view" || state.fillDisplayMode !== "margin" || !itemId) return;
  const run = () => {
    const body = $("rightBody");
    if (!body) return;
    let targetCard = null;
    const cards = body.querySelectorAll(".ov-card");
    for (let i = 0; i < cards.length; i++) {
      if (cards[i].dataset.itemId === itemId) { targetCard = cards[i]; break; }
    }
    if (!targetCard) return;
    const targetTop = Math.max(0, targetCard.offsetTop - Math.round((body.clientHeight - targetCard.offsetHeight) / 2));
    if (Math.abs(body.scrollTop - targetTop) < 6) return;
    body.scrollTo({ top: targetTop, behavior: "smooth" });
  };
  if (delayMs > 0) _ovCenterTimer = setTimeout(run, delayMs);
  else run();
}

function syncFocusHighlightClasses(scrollOverview = false) {
  const lockedId = getLockedFillItemId() || "";
  const focusId = getActiveFocusItemId() || "";
  const hoverId = state.hoverItemId || "";
  const selId = lockedId || state.selectedItemId || "";

  overlayLayer.classList.toggle("has-focus", Boolean(state.mode === "view" && focusId));
  overlayLayer.querySelectorAll(".fill-mini-tag").forEach(el => {
    const id = el.dataset.itemId || "";
    el.classList.toggle("active", Boolean(id && id === focusId));
    el.classList.toggle("pinned", Boolean(id && id === lockedId));
  });
  overlayLayer.querySelectorAll(".overlay-choice, .overlay-identity").forEach(el => {
    const id = el.dataset.itemId || "";
    el.classList.toggle("active", Boolean(id && id === focusId));
  });

  const body = $("rightBody");
  if (!body) return;
  const ovList = body.querySelector(".ov-list");
  let matchedCard = false;
  body.querySelectorAll(".ov-card").forEach(card => {
    const id = card.dataset.itemId || "";
    const isHov = Boolean(hoverId && id === hoverId);
    const isAct = Boolean(selId && id === selId);
    card.classList.toggle("hovered", isHov);
    card.classList.toggle("active", isAct);
    if (isHov || isAct) matchedCard = true;
  });
  if (ovList) ovList.classList.toggle("has-focus", matchedCard);
  if (scrollOverview && selId) {
    centerOverviewCard(selId, 0);
  }
}
function pinFillItem(itemId) {
  clearTimeout(hoverHideTimer);
  state.pinnedItemId = itemId;
  state.selectedItemId = itemId;
  state.hoverItemId = itemId;
  state.hoverSource = "canvas";
  syncHoverPopoverOnly();
  if (state.fillDisplayMode === "margin") {
    centerOverviewCard(itemId, 0);
  }
}
function unpinFillItem() {
  clearTimeout(hoverHideTimer);
  clearTimeout(_ovCenterTimer);
  state.pinnedItemId = null;
  state.selectedItemId = null;
  state.hoverItemId = null;
  state.hoverSource = null;
  state.hoverInPopover = false;
  state.inlineBlankEdit = null;
  syncHoverPopoverOnly();
}
function scheduleHoverClear() {
  if (getLockedFillItemId()) return;
  clearTimeout(hoverHideTimer);
  hoverHideTimer = setTimeout(() => {
    if (!getLockedFillItemId() && !state.hoverInPopover && state.hoverItemId && state.hoverSource !== "overview") {
      state.hoverItemId = null;
      state.hoverSource = null;
      syncHoverPopoverOnly();
    }
  }, 190);
}
function setHoverCanvasItem(itemId) {
  // 已点击固定或正在气泡内编辑时，鼠标滑过其他题框绝不抢焦、绝不销毁气泡
  if (getLockedFillItemId()) return;
  clearTimeout(hoverHideTimer);
  if (state.hoverItemId !== itemId || state.hoverSource !== "canvas") {
    state.hoverItemId = itemId;
    state.hoverSource = "canvas";
    syncHoverPopoverOnly();
    // 开启右侧总览栏时，自动将当前卷面聚焦题平滑居中到右侧列表视野中央
    if (state.fillDisplayMode === "margin") {
      centerOverviewCard(itemId, 80);
    }
  }
}
function setHoverFillItem(itemId) {
  setHoverCanvasItem(itemId);
}
function syncHoverPopoverOnly() {
  // 仅驱动顶层轻量特效画布形变衔接动效（0ms 底图重绘）
  if (typeof drawFxCanvas === "function") drawFxCanvas();
  else drawCanvasOnly();
  syncFocusHighlightClasses(false);
  const p = currentPage();
  const popId = getFloatingPopoverItemId();
  const existingPop = overlayLayer.querySelector(".fill-hover-pop");
  if (!p || !state.img || !popId) {
    if (existingPop) existingPop.remove();
    return;
  }
  const it = getPageItems(p).find(x => x.id === popId && x.kind === "fill");
  if (!it || !it.box || it.box.length !== 4) {
    if (existingPop) existingPop.remove();
    return;
  }
  const [x1, y1, x2, y2] = it.box;
  const rect = getHostMetrics();
  renderFillHoverPopover({
    it,
    sx1: x1 * state.scale + state.tx,
    sy1: y1 * state.scale + state.ty,
    sx2: x2 * state.scale + state.tx,
    sy2: y2 * state.scale + state.ty,
  }, rect, existingPop, true);
}

function renderFillHoverPopover(row, rect, reuseEl = null, animate = false) {
  const { it, sx1, sy1, sx2, sy2 } = row;
  const edited = Boolean(state.itemEdits[it.id] || ((state.feedback.items || {})[it.id] || {}).correction);
  const qno = (it.transcription && it.transcription.question_number) || it.number || "?";
  const isPinned = Boolean(getLockedFillItemId() === it.id);

  const isReuse = Boolean(reuseEl);
  const pop = reuseEl || document.createElement("div");
  pop.innerHTML = "";
  pop.className = "fill-hover-pop"
    + (edited ? " edited" : "")
    + (isPinned ? " pinned" : "")
    + (!animate ? " no-anim" : "")
    + (animate && !isReuse ? " pop-enter" : "");
  const head = document.createElement("div");
  head.className = "fill-hover-pop-head";
  const headLeft = document.createElement("div");
  headLeft.style.cssText = "display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;";
  const badge = document.createElement("span");
  badge.className = "margin-qno";
  badge.textContent = `第 ${qno} 题`;
  headLeft.appendChild(badge);
  if (isPinned) {
    const pinBadge = document.createElement("span");
    pinBadge.className = "pop-pin-badge";
    pinBadge.innerHTML = `${uiIcon("pin", "ui-ico-xs")}<span>已固定</span>`;
    headLeft.appendChild(pinBadge);
  }
  const headRight = document.createElement("div");
  headRight.style.cssText = "display:inline-flex;align-items:center;gap:6px;";
  const hint = document.createElement("span");
  hint.className = "fill-hover-pop-hint";
  const pgG = getPageGrading();
  const itG = pgG && pgG.items ? pgG.items[it.id] : null;
  if (itG) {
    hint.textContent = `得分 ${fmtScoreNum(itG.earned)}/${fmtScoreNum(itG.possible)} · 单击改字 / 双击重识`;
  } else if (isPinned) {
    hint.textContent = edited ? "已订正 · 单击改字 / 双击重识" : "单击胶囊改字 · 双击单空重识";
  } else {
    hint.textContent = "点题框固定 · 单击胶囊直接改字";
  }
  headRight.appendChild(hint);
  if (isPinned) {
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "pop-close-btn";
    closeBtn.innerHTML = uiIcon("x", "ui-ico-xs");
    closeBtn.title = "取消固定 (Esc)";
    closeBtn.onmousedown = (e) => e.stopPropagation();
    closeBtn.onclick = (e) => { e.stopPropagation(); unpinFillItem(); };
    headRight.appendChild(closeBtn);
  }
  head.appendChild(headLeft);
  head.appendChild(headRight);
  pop.appendChild(head);

  const ansEl = document.createElement("div");
  ansEl.className = "margin-ans";
  renderFillMarginInto(ansEl, it, "pop");
  pop.appendChild(ansEl);

  pop.onmouseenter = () => {
    clearTimeout(hoverHideTimer);
    state.hoverInPopover = true;
  };
  pop.onmouseleave = () => {
    state.hoverInPopover = false;
    if (!getLockedFillItemId()) scheduleHoverClear();
  };
  pop.onmousedown = (e) => { if (e.button === 0 && !spaceDown) e.stopPropagation(); };
  pop.onclick = (e) => {
    e.stopPropagation();
    if (!isPinned) {
      pinFillItem(it.id);
    }
  };

  // 新建时先就位再淡入；复用切题时保持旧坐标，测完新宽高后平滑滑向新题位
  if (!isReuse) {
    pop.style.left = "0px";
    pop.style.top = "-9999px";
    overlayLayer.appendChild(pop);
  }
  const pw = pop.offsetWidth || 280;
  const ph = pop.offsetHeight || 72;

  let left = Math.round(sx1);
  if (left + pw > rect.width - 10) left = Math.round(rect.width - pw - 10);
  left = Math.max(10, left);

  let top;
  let posClass = "pos-above";
  if (sy1 - ph - 8 >= 8) {
    top = Math.round(sy1 - ph - 8);
    posClass = "pos-above";
  } else if (sy2 + 8 + ph <= rect.height - 46) {
    top = Math.round(sy2 + 8);
    posClass = "pos-below";
  } else if (sx2 + 10 + pw <= rect.width - 10) {
    left = Math.round(sx2 + 10);
    top = Math.max(8, Math.min(rect.height - ph - 48, Math.round(sy1)));
    posClass = "pos-right";
  } else {
    top = Math.max(8, Math.min(rect.height - ph - 52, Math.round(sy2 + 6)));
    posClass = "pos-below";
  }
  pop.classList.add(posClass);
  if (!isReuse && animate) {
    pop.classList.add("no-anim");
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    void pop.offsetWidth;
    pop.classList.remove("no-anim");
    pop.classList.remove("pop-enter");
  } else {
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }
}

function renderEditBoxActionBar() {
  if (state.mode !== "edit" || !state.img || state.drag) return;
  const idx = state.selectedBoxIdx;
  if (idx < 0 || idx >= state.workingBoxes.length) return;
  const b = state.workingBoxes[idx];
  if (!b || !b.box || b.box.length !== 4) return;
  const rect = host.getBoundingClientRect();
  const [x1, y1, x2, y2] = b.box;
  const sx1 = x1 * state.scale + state.tx;
  const sy1 = y1 * state.scale + state.ty;
  const sx2 = x2 * state.scale + state.tx;
  const sy2 = y2 * state.scale + state.ty;
  if (sx2 < 0 || sy2 < 0 || sx1 > rect.width || sy1 > rect.height) return;

  const canVlm = isVlmRecogCls(b.cls);
  const bound = findBoundItemForBox(b);
  const busy = Boolean(b._uid && state.recognizingUids[b._uid]) || state.recognizingBusy;
  const hasAns = Boolean(bound && bound.transcription);

  const bar = document.createElement("div");
  bar.className = "edit-box-bar";
  bar.onmousedown = (e) => e.stopPropagation();
  bar.onclick = (e) => e.stopPropagation();

  if (canVlm) {
    const recBtn = document.createElement("button");
    recBtn.className = (b._needsRecog || !hasAns) ? "btn-ai" : "btn-ai-soft";
    recBtn.disabled = busy;
    const keyTag = primaryKeyLabel("recognizeBox");
    recBtn.innerHTML = busy
      ? `${uiIcon("loader", "ui-ico-xs ui-ico-spin")}<span>识别中…</span>`
      : (hasAns
          ? `${uiIcon("refresh", "ui-ico-xs")}<span>重认此框 (${escHtml(keyTag)})</span>`
          : `${uiIcon("sparkles", "ui-ico-xs")}<span>API识别 (${escHtml(keyTag)})</span>`);
    recBtn.title = "直接裁切当前框并调用配置的视觉大模型 API 进行识别";
    recBtn.onclick = () => recognizeBoxIndices([idx]);
    bar.appendChild(recBtn);
  }
  const delBtn = document.createElement("button");
  delBtn.className = "btn-danger";
  delBtn.disabled = busy;
  delBtn.innerHTML = `${uiIcon("trash", "ui-ico-xs")}<span>${bound ? "删框并消卡" : "删框"}</span>`;
  delBtn.title = "删除该框并同步移除其对应答案卡片（Ctrl+Z 可撤销）";
  delBtn.onclick = () => deleteBoxAt(idx);
  bar.appendChild(delBtn);

  bar.style.left = "0px";
  bar.style.top = "-9999px";
  overlayLayer.appendChild(bar);
  const bw = bar.offsetWidth || 160;
  const bh = bar.offsetHeight || 30;
  let left = Math.round(Math.max(8, Math.min(rect.width - bw - 8, sx2 - bw)));
  if (sx2 - sx1 < bw) left = Math.round(Math.max(8, Math.min(rect.width - bw - 8, sx1)));
  let top = Math.round(sy1 - bh - 22);
  if (top < 8) top = Math.round(Math.min(rect.height - bh - 52, sy2 + 8));
  bar.style.left = left + "px";
  bar.style.top = Math.max(8, top) + "px";
}

let _overlayAnchorTx = 0;
let _overlayAnchorTy = 0;
function syncOverlayPanTransform() {
  const dx = state.tx - _overlayAnchorTx;
  const dy = state.ty - _overlayAnchorTy;
  overlayLayer.style.transform = (dx === 0 && dy === 0) ? "" : `translate3d(${dx}px, ${dy}px, 0)`;
}

function renderOverlays() {
  _overlayAnchorTx = state.tx;
  _overlayAnchorTy = state.ty;
  overlayLayer.style.transform = "";
  overlayLayer.innerHTML = "";
  const p = currentPage();
  if (!p || !state.img) return;
  if (state.mode !== "view" || !state.showOverlay) {
    renderEditBoxActionBar();
    renderLowConfFocusOverlay();
    return;
  }
  // 局部真子集小框聚焦重识别时，清空其他干扰浮层（避免大框汇总条或卡片遮挡框选视野）
  if (state.subboxTarget) {
    return;
  }

  const rect = getHostMetrics();
  const fillRows = [];
  const pgGrade = getPageGrading(p);
  const focusId = getActiveFocusItemId();
  overlayLayer.classList.toggle("has-focus", Boolean(focusId));
  const frag = document.createDocumentFragment();
  const cullPad = 480;

  // 阅卷模式下，在每个 M1 选择题大框外沿渲染汇总统计标签（单选对错/多选对漏错/区得分）
  if (pgGrade && pgGrade.has_schema && Array.isArray(pgGrade.choice_zones)) {
    pgGrade.choice_zones.forEach(cz => {
      if (!cz.box || cz.box.length !== 4) return;
      const zx1 = cz.box[0] * state.scale + state.tx;
      const zy1 = cz.box[1] * state.scale + state.ty;
      const zx2 = cz.box[2] * state.scale + state.tx;
      const zy2 = cz.box[3] * state.scale + state.ty;
      if (zx2 < -cullPad || zx1 > rect.width + cullPad || zy2 < -cullPad || zy1 > rect.height + cullPad) return;
      const zb = document.createElement("div");
      zb.className = "choice-zone-badge";
      zb.innerHTML = `<span>${escHtml(cz.label || "")}</span><span class="cz-score">${escHtml(cz.total_label || "")}</span>`;
      zb.style.left = Math.max(8, Math.round(zx1)) + "px";
      zb.style.top = Math.max(8, Math.round(zy1 - 26)) + "px";
      frag.appendChild(zb);
    });
  }

  getPageItems(p).forEach(it => {
    if (it.kind === "fill" && !shouldShowFillInView(it)) return;
    const [x1, y1, x2, y2] = it.box || [0, 0, 0, 0];
    const sx1 = x1 * state.scale + state.tx;
    const sy1 = y1 * state.scale + state.ty;
    const sx2 = x2 * state.scale + state.tx;
    const sy2 = y2 * state.scale + state.ty;
    if (sx2 < -cullPad || sy2 < -cullPad || sx1 > rect.width + cullPad || sy1 > rect.height + cullPad) return;

    const bw = sx2 - sx1;
    const bh = sy2 - sy1;

    if (it.kind === "choice") {
      if (bh < 6 || bw < 16) return;
      const cg = pgGrade && pgGrade.items ? pgGrade.items[it.id] : null;
      const isEditingChip = Boolean(state.inlineChipEdit && state.inlineChipEdit.itemId === it.id);
      if (isEditingChip) {
        const inp = document.createElement("input");
        inp.type = "text";
        inp.className = "chip-inline-input";
        inp.value = getChoiceDisplayAnswer(it) === "—" ? "" : getChoiceDisplayAnswer(it);
        inp.placeholder = "A/BD";
        inp.style.left = Math.round(sx1) + "px";
        inp.style.top = Math.max(4, Math.round(sy1 - 2)) + "px";
        inp.style.width = "76px";
        inp.style.textTransform = "uppercase";
        inp.onmousedown = (e) => e.stopPropagation();
        inp.onclick = (e) => e.stopPropagation();
        let done = false;
        const finish = async (save) => {
          if (done) return;
          done = true;
          state.inlineChipEdit = null;
          if (save) {
            const nv = inp.value.trim().toUpperCase();
            if (nv && nv !== getChoiceDisplayAnswer(it)) {
              try {
                const res = await api("/api/review/set-choice", { page_id: p.id, item_id: it.id, answer: nv, reviewer: REVIEWER || "审阅员" });
                if (res.feedback) state.feedback = res.feedback;
                await hotReloadResults(false);
                toast(`第 ${it.number || "?"} 题已改为 ${nv}`, 1500);
                return;
              } catch (err) { toast(err.message, 2500); }
            }
          }
          draw();
        };
        inp.onkeydown = (e) => {
          e.stopPropagation();
          if (e.key === "Enter") { e.preventDefault(); finish(true); }
          if (e.key === "Escape") { e.preventDefault(); finish(false); }
        };
        inp.onblur = () => finish(true);
        overlayLayer.appendChild(inp);
        setTimeout(() => { try { inp.focus(); inp.select(); } catch {} }, 0);
        return;
      }

      const chip = document.createElement("div");
      const low = isLowConfChoice(it);
      const edited = Boolean(
        (state.itemEdits[it.id] && state.itemEdits[it.id].answer !== undefined) ||
        (((state.feedback.items || {})[it.id] || {}).answer !== undefined)
      );
      let gCls = "";
      if (cg && cg.status) gCls = " grade-" + cg.status;
      chip.dataset.itemId = it.id;
      chip.className = "overlay-chip overlay-choice"
        + gCls
        + (low && !gCls ? " low-conf" : "")
        + (edited ? " edited" : "")
        + (focusId && focusId === it.id ? " active" : "");
      const ans = getChoiceDisplayAnswer(it);
      const conf = state.showConf ? (" " + choiceConfText(it)) : "";
      const scTag = cg ? (cg.status === "correct" ? ` +${fmtScoreNum(cg.earned)}` : (cg.status === "partial" ? ` +${fmtScoreNum(cg.earned)}` : ` 0`)) : "";
      chip.textContent = `${it.number || "?"}:${ans}${scTag}${conf}`;
      const h = Math.max(15, Math.min(24, Math.floor(bh * 1.05)));
      const fs = Math.max(11.5, Math.min(15, Math.round(h * 0.72)));
      chip.style.height = h + "px";
      chip.style.fontSize = fs + "px";
      chip.style.left = Math.round(sx1) + "px";
      chip.style.top = Math.round(sy1 + (bh - h) / 2) + "px";
      const ruleTip = isChoiceRuleInferred(it) ? " · 题号由几何连续性规则推算补全（非OCR）" : "";
      const stdTip = cg ? ` · 标准答案 ${cg.standard}（得分 ${fmtScoreNum(cg.earned)}/${fmtScoreNum(cg.possible)}）` : "";
      chip.title = `第 ${it.number || "?"} 题：${ans}${stdTip}${ruleTip}（单击原地改选项）`;
      chip.onmouseenter = () => setHoverCanvasItem(it.id);
      chip.onmouseleave = () => scheduleHoverClear();
      chip.onmousedown = (e) => { if (e.button === 0 && !spaceDown) e.stopPropagation(); };
      chip.onclick = (e) => {
        e.stopPropagation();
        state.inlineBlankEdit = null;
        state.inlineChipEdit = { itemId: it.id, kind: "choice" };
        draw();
      };
      frag.appendChild(chip);
    } else if (it.kind === "identity" || (it.kind === "fill" && bh > bw * 1.4 && bw < 95)) {
      const isEditingChip = Boolean(state.inlineChipEdit && state.inlineChipEdit.itemId === it.id);
      if (isEditingChip) {
        const inp = document.createElement("input");
        inp.type = "text";
        inp.className = "chip-inline-input";
        inp.value = getIdentityCompactText(it);
        inp.style.left = Math.round(Math.max(4, sx1)) + "px";
        inp.style.top = Math.round(Math.max(4, sy1 - 24)) + "px";
        inp.style.width = "190px";
        inp.onmousedown = (e) => e.stopPropagation();
        inp.onclick = (e) => e.stopPropagation();
        let done = false;
        const finish = async (save) => {
          if (done) return;
          done = true;
          state.inlineChipEdit = null;
          if (save && inp.value.trim()) {
            try {
              const res = await api("/api/review/set-text", { page_id: p.id, item_id: it.id, text: inp.value.trim(), reviewer: REVIEWER || "审阅员" });
              if (res.feedback) state.feedback = res.feedback;
              toast("姓名学号已更新", 1400);
            } catch (err) { toast(err.message, 2500); }
          }
          draw();
        };
        inp.onkeydown = (e) => {
          e.stopPropagation();
          if (e.key === "Enter") { e.preventDefault(); finish(true); }
          if (e.key === "Escape") { e.preventDefault(); finish(false); }
        };
        inp.onblur = () => finish(true);
        overlayLayer.appendChild(inp);
        setTimeout(() => { try { inp.focus(); inp.select(); } catch {} }, 0);
        return;
      }
      const chip = document.createElement("div");
      const edited = Boolean(state.itemEdits[it.id] || ((state.feedback.items || {})[it.id] || {}).correction);
      chip.dataset.itemId = it.id;
      chip.className = "overlay-chip overlay-identity"
        + (edited ? " edited" : "")
        + (focusId && focusId === it.id ? " active" : "");
      chip.textContent = getIdentityCompactText(it);
      chip.style.left = Math.round(Math.max(4, sx1)) + "px";
      chip.style.top = Math.round(Math.max(4, sy1 - 26)) + "px";
      chip.title = "单击原地修改姓名/学号";
      chip.onmouseenter = () => setHoverCanvasItem(it.id);
      chip.onmouseleave = () => scheduleHoverClear();
      chip.onmousedown = (e) => { if (e.button === 0 && !spaceDown) e.stopPropagation(); };
      chip.onclick = (e) => {
        e.stopPropagation();
        state.inlineBlankEdit = null;
        state.inlineChipEdit = { itemId: it.id, kind: "identity" };
        draw();
      };
      frag.appendChild(chip);
    } else if (it.kind === "fill") {
      fillRows.push({ it, sx1, sy1, sx2, sy2, midY: (sy1 + sy2) / 2 });
    }
  });

  if (fillRows.length) {
    // 无论右侧总览栏是否开启，卷面右上角题号徽章均支持悬停弹出气泡 + 点击固定编辑
    // 两阶段批量布局（先全部挂载、再一次性测宽高、再一次性写位置），彻底杜绝 N 次强制同步回流
    const lockedId = getLockedFillItemId();
    const popId = getFloatingPopoverItemId();
    let popRow = null;
    const tagEntries = [];
    fillRows.forEach(row => {
      const { it } = row;
      const edited = Boolean(state.itemEdits[it.id] || ((state.feedback.items || {})[it.id] || {}).correction);
      const isPending = !edited && !it.transcription && (it.status === "等待视觉转录" || it.status === "等待豆包");
      const isFailed = !edited && !it.transcription && (it.status === "调用失败" || it.status === "结构异常" || Boolean(it.error));
      const hasBlank = !edited && hasUnansweredBlank(it);
      const isActive = Boolean(focusId && it.id === focusId);
      const isPinned = Boolean(lockedId && it.id === lockedId);
      if (popId && it.id === popId) popRow = row;

      const qno = (it.transcription && it.transcription.question_number) || it.number || "?";
      const tag = document.createElement("div");
      tag.dataset.itemId = it.id;
      tag.className = "fill-mini-tag"
        + (edited ? " edited" : (isFailed ? " failed" : (isPending ? " pending" : "")))
        + (isActive ? " active" : "")
        + (isPinned ? " pinned" : "");
      const icoHtml = isPinned
        ? uiIcon("pin", "ui-ico-xs")
        : (edited ? uiIcon("check", "ui-ico-xs")
        : (isFailed ? uiIcon("alert", "ui-ico-xs")
        : (isPending ? uiIcon("loader", "ui-ico-xs ui-ico-spin") : "")));
      tag.innerHTML = `${icoHtml}<span>${escHtml(qno)}</span>`;
      if (hasBlank) {
        const dot = document.createElement("span");
        dot.className = "mini-dot";
        dot.title = "含学生未作答空位";
        tag.appendChild(dot);
      }
      tag.title = `第 ${qno} 题（悬停预览，点击固定改字）`;
      tag.onmousedown = (e) => { if (e.button === 0 && !spaceDown) e.stopPropagation(); };
      tag.onmouseenter = () => setHoverCanvasItem(it.id);
      tag.onmouseleave = () => scheduleHoverClear();
      tag.onclick = (e) => { e.stopPropagation(); pinFillItem(it.id); };
      frag.appendChild(tag);
      tagEntries.push({ tag, row });
    });

    overlayLayer.appendChild(frag);
    // 一次性读取全部标签尺寸（仅 1 次 layout）
    const dims = tagEntries.map(e => [e.tag.offsetWidth || 28, e.tag.offsetHeight || 18]);
    // 一次性写入全部位置
    for (let i = 0; i < tagEntries.length; i++) {
      const { tag, row } = tagEntries[i];
      const [tw, th] = dims[i];
      const insideLeft = Math.max(Math.round(row.sx1 + 2), Math.round(row.sx2 - tw - 3));
      const insideTop = (row.sy2 - row.sy1 >= th + 4)
        ? Math.round(row.sy1 + 2)
        : Math.round(row.sy1 + (row.sy2 - row.sy1 - th) / 2);
      tag.style.left = insideLeft + "px";
      tag.style.top = insideTop + "px";
    }
    if (popRow) {
      renderFillHoverPopover(popRow, rect);
    }
  } else {
    overlayLayer.appendChild(frag);
  }

  // 仅保留原地胶囊直改，不再弹出蠢蠢的大文本框
  renderLowConfFocusOverlay();
}

function openInlineEditor(it) {
  if (!it) return;
  state.selectedItemId = it.id;
  state.hoverItemId = it.id;
  state.hoverSource = "canvas";
  state.editingPopItemId = null;
  if (it.kind === "choice" || it.kind === "identity") {
    state.inlineBlankEdit = null;
    state.inlineChipEdit = { itemId: it.id, kind: it.kind };
  } else if (it.kind === "fill") {
    state.pinnedItemId = it.id;
    state.inlineChipEdit = null;
    const slots = getOrderedFillSlotsJS(it);
    if (slots.length) {
      state.inlineBlankEdit = { itemId: it.id, sub: slots[0].sub, blank: slots[0].blank, source: "pop" };
    }
    refreshOverviewPanel();
    centerOverviewCard(it.id, 0);
  }
  draw();
}

function renderInlinePop(it) {
  const rect = host.getBoundingClientRect();
  const [x1, y1, x2, y2] = it.box || [0, 0, 0, 0];
  let left = Math.round(x1 * state.scale + state.tx);
  let top = Math.round(y2 * state.scale + state.ty + 6);
  if (left + 330 > rect.width) left = Math.max(8, rect.width - 336);
  if (top + 170 > rect.height) top = Math.max(8, Math.round(y1 * state.scale + state.ty - 165));

  const pop = document.createElement("div");
  pop.className = "inline-pop";
  pop.style.left = Math.max(8, left) + "px";
  pop.style.top = Math.max(8, top) + "px";
  pop.onmousedown = (e) => e.stopPropagation();
  pop.onclick = (e) => e.stopPropagation();

  if (it.kind === "choice") {
    const cur = getChoiceDisplayAnswer(it);
    const showTrust = isLowConfChoice(it);
    const ruleBanner = isChoiceRuleInferred(it)
      ? `<div style="font-size:11.5px;color:#b45309;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;padding:4px 7px;margin-bottom:6px;">规则推算补全（非OCR识别）：PP-OCRv6 原读 ${escHtml(it.recognition.no_raw > 0 ? it.recognition.no_raw : "?")}，按几何连续性规则补为第 ${escHtml(it.number)} 题</div>`
      : (showTrust ? `<div style="font-size:11.5px;color:#b45309;background:#fff7ed;border:1px solid #fed7aa;border-radius:6px;padding:4px 7px;margin-bottom:6px;">${escHtml(describeLowConfChoice(it))}</div>` : "");
    pop.innerHTML = `
      <div class="inline-pop-head">
        <span>修改第 ${escHtml(it.number || "?")} 题答案</span>
        <button id="popClose" style="padding:3px 6px;font-size:11px;">${uiIcon("x", "ui-ico-xs")}</button>
      </div>
      ${ruleBanner}
      <input id="popInput" placeholder="输入选项如 A / BD" style="font-weight:700;text-transform:uppercase;">
      <div class="inline-pop-foot">
        <span>${escHtml(choiceConfText(it) || "回车保存")}</span>
        <div style="display:flex;gap:6px;">
          ${showTrust ? `<button id="popTrust" style="padding:3px 9px;background:#15803d;color:#fff;border-color:#15803d;font-weight:600;">${uiIcon("check", "ui-ico-xs")}<span>信任无误</span></button>` : ""}
          <button class="btn-primary" id="popSave" style="padding:3px 10px;">确定</button>
        </div>
      </div>
    `;
    overlayLayer.appendChild(pop);
    const inp = pop.querySelector("#popInput");
    inp.value = cur === "—" ? "" : cur;
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
    const closePop = () => { state.editingPopItemId = null; state.selectedItemId = null; draw(); };
    const commit = () => {
      const nv = inp.value.trim().toUpperCase();
      const wasTrusted = Boolean(state.itemEdits[it.id] && state.itemEdits[it.id].trusted);
      if (nv && nv !== (it.answer || "")) {
        state.itemEdits[it.id] = { ...(wasTrusted ? { trusted: true } : {}), answer: nv, correction: nv, label: "已订正" };
      } else if (wasTrusted) {
        state.itemEdits[it.id] = { trusted: true, label: "已确认" };
      } else {
        delete state.itemEdits[it.id];
      }
      state.editingPopItemId = null;
      state.selectedItemId = null;
      updateDirtyBadge();
      renderLeftList();
      updateTopbar();
      renderRightPanel();
      draw();
    };
    pop.querySelector("#popSave").onclick = commit;
    pop.querySelector("#popClose").onclick = closePop;
    const tBtn = pop.querySelector("#popTrust");
    if (tBtn) tBtn.onclick = () => trustLowConfTarget({ kind: "item", item: it });
    inp.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") closePop();
    };
  } else {
    const title = it.kind === "identity" ? "姓名学号" : ("第 " + ((it.transcription && it.transcription.question_number) || it.number || "?") + " 题");
    const cur = getFillDisplayText(it);
    const showTrust = !isItemTrusted(it) && Boolean(it.error || (it.issues && it.issues.length > 0));
    pop.innerHTML = `
      <div class="inline-pop-head">
        <span>修改 ${escHtml(title)} 识别文字</span>
        <button id="popClose" style="padding:3px 6px;font-size:11px;">${uiIcon("x", "ui-ico-xs")}</button>
      </div>
      <textarea id="popTa" rows="4"></textarea>
      <div class="inline-pop-foot">
        <span>Ctrl+Enter 确定 · Esc 取消</span>
        <div style="display:flex;gap:6px;">
          ${showTrust ? `<button id="popTrust" style="padding:3px 9px;background:#15803d;color:#fff;border-color:#15803d;font-weight:600;">${uiIcon("check", "ui-ico-xs")}<span>信任无误</span></button>` : ""}
          <button class="btn-primary" id="popSave" style="padding:3px 10px;">确定</button>
        </div>
      </div>
    `;
    overlayLayer.appendChild(pop);
    const ta = pop.querySelector("#popTa");
    ta.value = cur;
    setTimeout(() => { ta.focus(); }, 0);
    const closePop = () => { state.editingPopItemId = null; state.selectedItemId = null; draw(); };
    const commit = () => {
      const nv = ta.value.trim();
      const wasTrusted = Boolean(state.itemEdits[it.id] && state.itemEdits[it.id].trusted);
      if (nv !== formatFillAnswers(it).trim()) {
        state.itemEdits[it.id] = { ...(wasTrusted ? { trusted: true } : {}), correction: nv, label: "已订正" };
      } else if (wasTrusted) {
        state.itemEdits[it.id] = { trusted: true, label: "已确认" };
      } else {
        delete state.itemEdits[it.id];
      }
      state.editingPopItemId = null;
      state.selectedItemId = null;
      updateDirtyBadge();
      renderLeftList();
      updateTopbar();
      renderRightPanel();
      draw();
    };
    pop.querySelector("#popSave").onclick = commit;
    pop.querySelector("#popClose").onclick = closePop;
    const tBtn = pop.querySelector("#popTrust");
    if (tBtn) tBtn.onclick = () => trustLowConfTarget({ kind: "item", item: it });
    ta.onkeydown = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") closePop();
    };
  }
}

