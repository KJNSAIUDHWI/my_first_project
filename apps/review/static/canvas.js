function resizeCanvas() {
  const rect = host.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(rect.width * dpr));
  const h = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

// 实时监听画布容器尺寸变化（含左右栏 0.18s 展开/收起动画每一帧），彻底杜绝 canvas 像素宽高与 DOM 叠层错位
if (typeof ResizeObserver !== "undefined") {
  let _lastHostW = 0, _lastHostH = 0;
  const _canvasHostRo = new ResizeObserver(() => {
    const r = host.getBoundingClientRect();
    const rw = Math.round(r.width), rh = Math.round(r.height);
    if (rw <= 0 || rh <= 0) return;
    if (rw !== _lastHostW || rh !== _lastHostH) {
      _lastHostW = rw;
      _lastHostH = rh;
      resizeCanvas();
    }
  });
  _canvasHostRo.observe(host);
}

function fitCanvas() {
  if (!state.img) return;
  const rect = host.getBoundingClientRect();
  const pad = 16;
  const availW = Math.max(220, rect.width - pad * 2);
  const availH = Math.max(180, rect.height - pad * 2);
  const sw = availW / state.img.width;
  const sh = availH / state.img.height;
  state.scale = Math.max(0.05, Math.min(sw, sh));
  const paperW = state.img.width * state.scale;
  state.tx = pad + Math.max(0, (availW - paperW) / 2);
  state.ty = (rect.height - state.img.height * state.scale) / 2;
  $("zoomLabel").textContent = Math.round(state.scale * 100) + "%";
}

function screenToImg(sx, sy) { return [(sx - state.tx) / state.scale, (sy - state.ty) / state.scale]; }

function visibleBoxIndices() {
  const out = [];
  state.workingBoxes.forEach((b, idx) => {
    const meta = BOX_META[b.cls];
    if (!meta) return;
    if (state.mode === "edit") {
      if (state.showLayers[meta.stage]) out.push(idx);
    } else {
      if (b.cls === 0 || b.cls === 5) {
        if (b.cls === 5) {
          const bound = findBoundItemForBox(b);
          if (bound && !shouldShowFillInView(bound)) return;
        }
        out.push(idx);
      }
    }
  });
  return out;
}

/** 检测点 (ix, iy) 命中 box 的哪个边/角，返回 'nw'|'ne'|'sw'|'se'|'n'|'s'|'w'|'e'|null */
function hitResizeEdge(box, ix, iy) {
  const [x1, y1, x2, y2] = box;
  const tol = 8 / Math.max(0.15, state.scale);
  const nearL = Math.abs(ix - x1) <= tol;
  const nearR = Math.abs(ix - x2) <= tol;
  const nearT = Math.abs(iy - y1) <= tol;
  const nearB = Math.abs(iy - y2) <= tol;
  const inX = ix >= x1 - tol && ix <= x2 + tol;
  const inY = iy >= y1 - tol && iy <= y2 + tol;
  if (!inX || !inY) return null;
  if (nearL && nearT) return "nw";
  if (nearR && nearT) return "ne";
  if (nearL && nearB) return "sw";
  if (nearR && nearB) return "se";
  if (nearT && ix > x1 && ix < x2) return "n";
  if (nearB && ix > x1 && ix < x2) return "s";
  if (nearL && iy > y1 && iy < y2) return "w";
  if (nearR && iy > y1 && iy < y2) return "e";
  return null;
}

function edgeToCursor(dir) {
  return { nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize", sw: "nesw-resize", n: "ns-resize", s: "ns-resize", w: "ew-resize", e: "ew-resize" }[dir] || "default";
}

/** 仅命中当前已选中的框（先四边/四角缩放，再框内移动）；未选中的框不拦截左键长按画新框 */
function hitTestSelectedBox(ix, iy) {
  const idx = state.selectedBoxIdx;
  if (idx < 0 || idx >= state.workingBoxes.length) return null;
  const indices = visibleBoxIndices();
  if (!indices.includes(idx)) return null;
  const b = state.workingBoxes[idx];
  if (!b || !b.box || b.box.length !== 4) return null;
  const dir = hitResizeEdge(b.box, ix, iy);
  if (dir) return { kind: "resize", idx, dir };
  const [x1, y1, x2, y2] = b.box;
  if (ix >= x1 && ix <= x2 && iy >= y1 && iy <= y2) {
    return { kind: "move", idx };
  }
  return null;
}

/** 命中任意可见框（用于左键单击选中 & 右键单击删框），严格按面积从小到大排序（小框优先，避免大框抢点） */
function hitTestAnyBox(ix, iy) {
  const indices = visibleBoxIndices();
  const areaOf = (b) => Math.max(0, (b.box[2] - b.box[0]) * (b.box[3] - b.box[1]));
  const inside = indices
    .map(idx => ({ idx, b: state.workingBoxes[idx] }))
    .filter(({ b }) => b && b.box && b.box.length === 4 && ix >= b.box[0] && ix <= b.box[2] && iy >= b.box[1] && iy <= b.box[3])
    .sort((a, b) => areaOf(a.b) - areaOf(b.b));
  if (inside.length) {
    return { idx: inside[0].idx, allInside: inside.map(x => x.idx) };
  }
  const onEdge = indices
    .map(idx => ({ idx, b: state.workingBoxes[idx] }))
    .filter(({ b }) => b && b.box && b.box.length === 4 && Boolean(hitResizeEdge(b.box, ix, iy)))
    .sort((a, b) => areaOf(a.b) - areaOf(b.b));
  if (onEdge.length) {
    return { idx: onEdge[0].idx, allInside: onEdge.map(x => x.idx) };
  }
  return null;
}

function drawCanvasOnly() {
  const rect = host.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const needW = Math.max(1, Math.round(rect.width * dpr));
  const needH = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== needW || canvas.height !== needH) {
    canvas.width = needW;
    canvas.height = needH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!state.img) return;

  ctx.save();
  ctx.translate(state.tx, state.ty);
  ctx.scale(state.scale, state.scale);
  ctx.drawImage(state.img, 0, 0);

  const p = currentPage();
  const inv = 1 / Math.max(0.15, state.scale);
  const pgGrade = (state.mode === "view" && p) ? getPageGrading(p) : null;
  const lockedId = (state.mode === "view") ? getLockedFillItemId() : null;
  const focusId = (state.mode === "view" && !state.subboxTarget)
    ? (lockedId || state.hoverItemId || state.selectedItemId)
    : null;
  const focusItem = (focusId && p)
    ? getPageItems(p).find(it =>
        it.id === focusId
        && (it.kind !== "fill" || shouldShowFillInView(it))
        && it.box && it.box.length === 4
      ) || null
    : null;

  ctx.save();
  if (focusItem) ctx.globalAlpha = 0.32;
  visibleBoxIndices().forEach(idx => {
    const b = state.workingBoxes[idx];
    const meta = BOX_META[b.cls] || { color: "#2563eb", name: String(b.cls) };
    const [x1, y1, x2, y2] = b.box;
    const isSel = state.mode === "edit" && idx === state.selectedBoxIdx;
    ctx.lineWidth = (isSel ? 2.6 : 1.5) * inv;
    ctx.strokeStyle = meta.color;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    if (isSel) {
      ctx.fillStyle = meta.color + "1c";
      ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
      const hs = 5 * inv;
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const pts = [[x1,y1],[mx,y1],[x2,y1],[x2,my],[x2,y2],[mx,y2],[x1,y2],[x1,my]];
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = meta.color;
      ctx.lineWidth = 1.6 * inv;
      pts.forEach(([px, py]) => {
        ctx.fillRect(px - hs, py - hs, hs * 2, hs * 2);
        ctx.strokeRect(px - hs, py - hs, hs * 2, hs * 2);
      });
    }
    if (state.mode === "edit") {
      const isRecogBusy = Boolean(b._uid && state.recognizingUids[b._uid]);
      if (isRecogBusy) {
        ctx.save();
        ctx.fillStyle = "rgba(37, 99, 235, 0.14)";
        ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
        ctx.strokeStyle = "#2563eb";
        ctx.lineWidth = 2.6 * inv;
        ctx.setLineDash([7 * inv, 4 * inv]);
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
        ctx.restore();
      }
      ctx.font = `600 ${Math.max(10, 11.5 * inv)}px sans-serif`;
      const confVal = typeof b.confidence === "number" ? b.confidence : (typeof b.conf === "number" ? b.conf : null);
      const confSuffix = state.showConf ? (confVal !== null ? ` ${Math.round(confVal * 100)}%` : " 人工") : "";
      const recogSuffix = isRecogBusy ? " · 识别中" : (isVlmRecogCls(b.cls) && b._needsRecog ? " · 待识别" : "");
      const tag = meta.name + confSuffix + recogSuffix;
      const tw = ctx.measureText(tag).width + 8 * inv;
      const th = 16 * inv;
      ctx.fillStyle = isRecogBusy
        ? "#2563eb"
        : ((isVlmRecogCls(b.cls) && b._needsRecog)
            ? "#d97706"
            : ((state.showConf && !b.trusted && confVal !== null && confVal < 0.7) ? "#d97706" : meta.color));
      ctx.fillRect(x1, Math.max(0, y1 - th), tw, th);
      ctx.fillStyle = "#fff";
      ctx.fillText(tag, x1 + 4 * inv, Math.max(12 * inv, y1 - 3.5 * inv));
    }
  });
  ctx.restore();

  if (state.mode === "view" && p) {
    // 阅卷模式下绘制选择题大框（M1 cls=1）淡绿边界
    if (pgGrade && pgGrade.has_schema && Array.isArray(pgGrade.choice_zones)) {
      ctx.save();
      if (focusItem) ctx.globalAlpha = 0.34;
      pgGrade.choice_zones.forEach(cz => {
        if (!cz.box || cz.box.length !== 4) return;
        const [zx1, zy1, zx2, zy2] = cz.box;
        ctx.lineWidth = 2.0 * inv;
        ctx.strokeStyle = "rgba(22, 163, 74, 0.72)";
        ctx.setLineDash([6 * inv, 4 * inv]);
        ctx.strokeRect(zx1, zy1, zx2 - zx1, zy2 - zy1);
      });
      ctx.restore();
    }
    // 非焦点项（聚光时整体降亮，避免喧宾夺主）
    ctx.save();
    if (focusItem) ctx.globalAlpha = 0.34;
    getPageItems(p).forEach(it => {
      if (it.kind === "fill" && !shouldShowFillInView(it)) return;
      if (focusItem && it.id === focusItem.id) return;
      const [x1, y1, x2, y2] = it.box || [0, 0, 0, 0];
      if (it.kind === "choice") {
        const cg = pgGrade && pgGrade.items ? pgGrade.items[it.id] : null;
        const low = isLowConfChoice(it);
        ctx.lineWidth = (cg ? 2.0 : 1.4) * inv;
        if (cg && cg.status === "correct") {
          ctx.fillStyle = "rgba(22, 163, 74, 0.12)";
          ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
          ctx.strokeStyle = "#16a34a";
        } else if (cg && cg.status === "partial") {
          ctx.fillStyle = "rgba(234, 88, 12, 0.14)";
          ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
          ctx.strokeStyle = "#ea580c";
        } else if (cg && cg.status === "wrong") {
          ctx.fillStyle = "rgba(220, 38, 38, 0.14)";
          ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
          ctx.strokeStyle = "#dc2626";
        } else {
          ctx.strokeStyle = low ? "#d97706" : "#15803d";
        }
        ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      }
    });
    ctx.restore();

    // 悬停/固定聚光效果：压暗卷面其余区域亮度 + 为当前选中框叠加清晰半透明蒙版与亮边
    if (focusItem) {
      const [fx1, fy1, fx2, fy2] = focusItem.box;
      ctx.save();
      ctx.fillStyle = "rgba(15, 23, 42, 0.22)";
      ctx.beginPath();
      ctx.rect(0, 0, state.img.width, state.img.height);
      ctx.rect(fx1, fy1, fx2 - fx1, fy2 - fy1);
      ctx.fill("evenodd");
      ctx.restore();

      const isPinned = focusItem.id === lockedId;
      const isEdited = Boolean(state.itemEdits[focusItem.id] || ((state.feedback.items || {})[focusItem.id] || {}).correction);
      let fillTint = isPinned ? "rgba(37, 99, 235, 0.19)" : "rgba(37, 99, 235, 0.14)";
      let strokeClr = isPinned ? "#1d4ed8" : "#2563eb";
      let glowClr = "rgba(37, 99, 235, 0.38)";
      if (focusItem.kind === "choice") {
        const cg = pgGrade && pgGrade.items ? pgGrade.items[focusItem.id] : null;
        if (cg && cg.status === "correct") {
          fillTint = "rgba(22, 163, 74, 0.22)"; strokeClr = "#16a34a"; glowClr = "rgba(22, 163, 74, 0.38)";
        } else if (cg && cg.status === "partial") {
          fillTint = "rgba(234, 88, 12, 0.22)"; strokeClr = "#ea580c"; glowClr = "rgba(234, 88, 12, 0.38)";
        } else if (cg && cg.status === "wrong") {
          fillTint = "rgba(220, 38, 38, 0.22)"; strokeClr = "#dc2626"; glowClr = "rgba(220, 38, 38, 0.38)";
        } else {
          fillTint = "rgba(37, 99, 235, 0.18)"; strokeClr = "#2563eb"; glowClr = "rgba(37, 99, 235, 0.38)";
        }
      } else if (isEdited) {
        fillTint = isPinned ? "rgba(22, 163, 74, 0.18)" : "rgba(22, 163, 74, 0.14)";
        strokeClr = "#15803d";
        glowClr = "rgba(22, 163, 74, 0.34)";
      }

      ctx.save();
      ctx.fillStyle = fillTint;
      ctx.fillRect(fx1, fy1, fx2 - fx1, fy2 - fy1);
      ctx.shadowColor = glowClr;
      ctx.shadowBlur = 12 * inv;
      ctx.lineWidth = (isPinned ? 3.2 : 2.6) * inv;
      ctx.strokeStyle = strokeClr;
      ctx.strokeRect(fx1, fy1, fx2 - fx1, fy2 - fy1);
      ctx.restore();
    }
  }

  // 局部真子集小框聚焦重识别：压暗父大框外区域 + 亮起父大框引导边 + 绘制真子集小框
  if (state.subboxTarget && state.subboxTarget.parentBox) {
    const [px1, py1, px2, py2] = state.subboxTarget.parentBox;
    ctx.save();
    ctx.fillStyle = "rgba(15, 23, 42, 0.42)";
    ctx.beginPath();
    ctx.rect(-5000, -5000, state.img.width + 10000, state.img.height + 10000);
    ctx.rect(px1, py1, px2 - px1, py2 - py1);
    ctx.fill("evenodd");
    ctx.lineWidth = 3.0 * inv;
    ctx.strokeStyle = "#38bdf8";
    ctx.setLineDash([8 * inv, 5 * inv]);
    ctx.strokeRect(px1, py1, px2 - px1, py2 - py1);
    ctx.setLineDash([]);
    ctx.restore();
  }
  if (state.drag && state.drag.type === "subbox_draw") {
    const d = state.drag;
    const x1 = Math.min(d.ix0, d.ix1), y1 = Math.min(d.iy0, d.iy1);
    const rw = Math.abs(d.ix1 - d.ix0), rh = Math.abs(d.iy1 - d.iy0);
    ctx.save();
    ctx.fillStyle = "rgba(34, 197, 94, 0.18)";
    ctx.fillRect(x1, y1, rw, rh);
    ctx.strokeStyle = "#16a34a";
    ctx.lineWidth = 2.6 * inv;
    ctx.strokeRect(x1, y1, rw, rh);
    ctx.restore();
  }

  if (state.drag && state.drag.type === "draw") {
    const d = state.drag;
    const meta = BOX_META[state.activeCls] || { color: "#2563eb", name: String(state.activeCls) };
    const x1 = Math.min(d.ix0, d.ix1), y1 = Math.min(d.iy0, d.iy1);
    const rw = Math.abs(d.ix1 - d.ix0), rh = Math.abs(d.iy1 - d.iy0);
    ctx.fillStyle = meta.color + "18";
    ctx.fillRect(x1, y1, rw, rh);
    ctx.strokeStyle = meta.color;
    ctx.lineWidth = 2.2 * inv;
    ctx.setLineDash([6 * inv, 4 * inv]);
    ctx.strokeRect(x1, y1, rw, rh);
    ctx.setLineDash([]);
    if (rw >= 6 * inv && rh >= 6 * inv) {
      ctx.font = `600 ${Math.max(10, 11.5 * inv)}px sans-serif`;
      const tag = `+ ${meta.name}`;
      const tw = ctx.measureText(tag).width + 8 * inv;
      const th = 16 * inv;
      ctx.fillStyle = meta.color;
      ctx.fillRect(x1, Math.max(0, y1 - th), tw, th);
      ctx.fillStyle = "#fff";
      ctx.fillText(tag, x1 + 4 * inv, Math.max(12 * inv, y1 - 3.5 * inv));
    }
  }

  ctx.restore();
}

function draw() {
  drawCanvasOnly();
  renderOverlays();
}

function pushUndo() {
  state.undoStack.push({
    boxes: JSON.parse(JSON.stringify(state.workingBoxes)),
    items: JSON.parse(JSON.stringify(state.workingItems)),
    itemEdits: JSON.parse(JSON.stringify(state.itemEdits)),
  });
  if (state.undoStack.length > 50) state.undoStack.shift();
}

function undoBoxEdit() {
  if (!state.undoStack.length || state.recognizingBusy) return;
  const snap = state.undoStack.pop();
  if (Array.isArray(snap)) {
    state.workingBoxes = snap;
  } else {
    state.workingBoxes = snap.boxes || [];
    state.workingItems = snap.items || [];
    state.itemEdits = snap.itemEdits || {};
    bindBoxesAndItems(state.workingBoxes, state.workingItems);
  }
  state.selectedBoxIdx = -1;
  state.boxesModified = true;
  state.itemsModified = true;
  updateDirtyBadge();
  renderLeftList();
  updateTopbar();
  renderRightPanel();
  draw();
  toast("已撤销上一步改动（框与答案同步恢复）", 1500);
}

function deleteBoxAt(idx) {
  if (idx < 0 || idx >= state.workingBoxes.length || state.recognizingBusy) return;
  pushUndo();
  const [removed] = state.workingBoxes.splice(idx, 1);
  let removedItemCount = 0;

  if (removed) {
    const idsToDrop = new Set();
    if (removed.item_id) idsToDrop.add(removed.item_id);
    // 若删除的是 M2 选择-选项框 (cls=3)，且所属 choice 内已无其他选项框，一并清理对应选择题卡片
    if (removed.cls === 3 && removed.box && removed.box.length === 4) {
      const rcx = (removed.box[0] + removed.box[2]) * 0.5;
      const rcy = (removed.box[1] + removed.box[3]) * 0.5;
      state.workingItems.forEach(it => {
        if (it.kind !== "choice" || !it.box || it.box.length !== 4) return;
        const [x1, y1, x2, y2] = it.box;
        if (rcx >= x1 && rcx <= x2 && rcy >= y1 && rcy <= y2) {
          const hasOtherOpt = state.workingBoxes.some(ob => {
            if (ob.cls !== 3 || !ob.box || ob.box.length !== 4) return false;
            const ocx = (ob.box[0] + ob.box[2]) * 0.5;
            const ocy = (ob.box[1] + ob.box[3]) * 0.5;
            return ocx >= x1 && ocx <= x2 && ocy >= y1 && ocy <= y2;
          });
          if (!hasOtherOpt) idsToDrop.add(it.id);
        }
      });
    }
    if (idsToDrop.size > 0) {
      const before = state.workingItems.length;
      state.workingItems = state.workingItems.filter(it => !idsToDrop.has(it.id));
      removedItemCount = before - state.workingItems.length;
      idsToDrop.forEach(id => {
        delete state.itemEdits[id];
        if (state.hoverItemId === id) state.hoverItemId = null;
        if (state.selectedItemId === id) state.selectedItemId = null;
        if (state.editingPopItemId === id) state.editingPopItemId = null;
        if (state.focusedLowConf && state.focusedLowConf.item && state.focusedLowConf.item.id === id) {
          state.focusedLowConf = null;
        }
      });
    }
  }

  state.selectedBoxIdx = -1;
  state.boxesModified = true;
  state.itemsModified = true;
  state.workload.deleted += 1;
  updateDirtyBadge();
  updateWorkloadBar();
  renderLeftList();
  updateTopbar();
  renderRightPanel();
  draw();
  toast(
    removedItemCount > 0
      ? "已删除框并同步移除对应答案卡（Ctrl+Z 可还原）"
      : "已删除框（Ctrl+Z 可撤销）",
    1700
  );
}

async function recognizeBoxIndices(indices) {
  if (state.recognizingBusy) return;
  const p = currentPage();
  if (!p) return;
  const uniqueIdx = [...new Set((indices || []).filter(i => i >= 0 && i < state.workingBoxes.length))];
  if (!uniqueIdx.length) {
    toast("请先选中一个大题-框 (M3) 或姓名学号框 (M1)", 2200);
    return;
  }
  const targets = [];
  const uidList = [];
  uniqueIdx.forEach(idx => {
    const b = state.workingBoxes[idx];
    if (!b || !isVlmRecogCls(b.cls)) return;
    if (!b._uid) b._uid = nextBoxUid();
    targets.push({
      cls: b.cls,
      box: b.box,
      item_id: b.item_id || "",
      client_uid: b._uid,
    });
    uidList.push(b._uid);
  });
  if (!targets.length) {
    toast("仅「大题-框 (M3)」与「姓名学号 (M1)」支持直接调用视觉大模型 API 识别", 2600);
    return;
  }

  state.recognizingBusy = true;
  uidList.forEach(uid => { state.recognizingUids[uid] = true; });
  renderRightPanel();
  draw();
  toast(`正在调用视觉大模型 API 识别 ${targets.length} 个框…`, 4500);

  try {
    const res = await api("/api/recognize-box", {
      page_id: p.id,
      targets,
    });
    if (res.results_mtime) state.resultsMtime = res.results_mtime;

    let okCnt = 0;
    let firstQno = "";
    (res.recognized || []).forEach(row => {
      const it = row.item;
      if (!it || !it.id) return;
      okCnt += 1;
      if (!firstQno) {
        firstQno = it.kind === "identity"
          ? "姓名学号"
          : `第 ${(it.transcription && it.transcription.question_number) || it.number || "?"} 题`;
      }
      const b = state.workingBoxes.find(x => x._uid === row.client_uid);
      if (b) {
        b.item_id = it.id;
        b._needsRecog = false;
        b._isNew = false;
        b.manual = true;
      }
      const pos = state.workingItems.findIndex(x => x.id === it.id);
      if (pos >= 0) state.workingItems[pos] = it;
      else state.workingItems.push(it);
      // 同步清理旧的手工文字订正，使新识别文字立即呈现
      delete state.itemEdits[it.id];
    });

    if (okCnt > 0) {
      state.boxesModified = true;
      state.itemsModified = true;
    }

    const failCnt = (res.failed || []).length;
    const costObj = res.cost;
    let costTag = "";
    if (costObj && costObj.found && typeof costObj.cost === "number") {
      const sym = costObj.currency === "USD" ? "$" : "¥";
      costTag = ` · 约 ${sym}${costObj.cost < 0.01 ? costObj.cost.toFixed(4) : costObj.cost.toFixed(3)}`;
    } else if (res.usage && (res.usage.prompt_tokens || res.usage.completion_tokens)) {
      costTag = ` · ${(res.usage.prompt_tokens || 0) + (res.usage.completion_tokens || 0)} tok`;
    }
    const secTag = res.elapsed_seconds ? `耗时 ${res.elapsed_seconds}s` : "";
    if (okCnt === 1 && !failCnt) {
      toast(`已识别 ${firstQno}（${secTag}${costTag}），切回「看答案」即可查看`, 3200);
    } else if (okCnt > 0) {
      toast(`已识别 ${okCnt} 个框${failCnt ? `，${failCnt} 个失败` : ""}（${secTag}${costTag}）`, 3400);
    } else {
      toast(`识别失败：${res.error || "大模型未返回有效结果"}`, 4000);
    }
  } catch (err) {
    toast("API 识别失败：" + err.message, 4200);
  } finally {
    uidList.forEach(uid => { delete state.recognizingUids[uid]; });
    state.recognizingBusy = false;
    updateDirtyBadge();
    renderLeftList();
    updateTopbar();
    renderRightPanel();
    draw();
  }
}

