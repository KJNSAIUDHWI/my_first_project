let _hostMetrics = { left: 0, top: 0, width: 0, height: 0 };
function syncHostMetrics() {
  const r = host.getBoundingClientRect();
  _hostMetrics = { left: r.left, top: r.top, width: r.width, height: r.height };
  return _hostMetrics;
}
function getHostMetrics() {
  if (_hostMetrics.width <= 0 || _hostMetrics.height <= 0) return syncHostMetrics();
  return _hostMetrics;
}

function ensureCanvasBuffer(c, context, wCss, hCss, dpr) {
  if (!c || !context) return;
  const needW = Math.max(1, Math.round(wCss * dpr));
  const needH = Math.max(1, Math.round(hCss * dpr));
  if (c.width !== needW || c.height !== needH) {
    c.width = needW;
    c.height = needH;
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function resizeCanvas() {
  const rect = syncHostMetrics();
  const dpr = window.devicePixelRatio || 1;
  ensureCanvasBuffer(canvas, ctx, rect.width, rect.height, dpr);
  ensureCanvasBuffer(fxCanvas, fxCtx, rect.width, rect.height, dpr);
  draw();
}

// 实时监听画布容器尺寸变化（含左右栏 0.18s 展开/收起动画每一帧），彻底杜绝 canvas 像素宽高与 DOM 叠层错位
if (typeof ResizeObserver !== "undefined") {
  let _lastHostW = 0, _lastHostH = 0;
  const _canvasHostRo = new ResizeObserver(() => {
    const r = syncHostMetrics();
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
  const rect = syncHostMetrics();
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

/* 底层静态画布：绘制原卷图片 + 题框边界（仅在翻页/缩放/平移/改框时重绘，悬停切题零重绘！） */
function drawBaseCanvas() {
  const rect = getHostMetrics();
  const dpr = window.devicePixelRatio || 1;
  ensureCanvasBuffer(canvas, ctx, rect.width, rect.height, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!state.img) return;

  ctx.save();
  ctx.translate(state.tx, state.ty);
  ctx.scale(state.scale, state.scale);
  ctx.drawImage(state.img, 0, 0);

  const p = currentPage();
  const inv = 1 / Math.max(0.15, state.scale);
  const pgGrade = (state.mode === "view" && p) ? getPageGrading(p) : null;

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

  if (state.mode === "view" && p) {
    if (pgGrade && pgGrade.has_schema && Array.isArray(pgGrade.choice_zones)) {
      ctx.save();
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
    getPageItems(p).forEach(it => {
      if (it.kind !== "choice" || !it.box || it.box.length !== 4) return;
      const [x1, y1, x2, y2] = it.box;
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
    });
  }
  ctx.restore();
}

function traceRoundedRect(c, x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w * 0.5, h * 0.5));
  c.moveTo(x + rr, y);
  c.arcTo(x + w, y, x + w, y + h, rr);
  c.arcTo(x + w, y + h, x, y + h, rr);
  c.arcTo(x, y + h, x, y, rr);
  c.arcTo(x, y, x + w, y, rr);
  c.closePath();
}

/* 悬停/固定聚焦框顺滑衔接动画状态（图像坐标系插值，平移缩放零漂移，静止自动休眠 0% 占用） */
const _fxAnim = {
  alpha: 0,
  targetAlpha: 0,
  box: null,
  targetBox: null,
  rgb: [22, 163, 74],
  targetRgb: [22, 163, 74],
  fillA: 0.13,
  targetFillA: 0.13,
  isPinned: false,
  raf: 0,
  lastTs: 0,
};

function resolveFocusTargetSpec() {
  const p = currentPage();
  if (state.mode !== "view" || !p || state.subboxTarget || !state.img) return null;
  const lockedId = getLockedFillItemId();
  const focusId = lockedId || state.hoverItemId || state.selectedItemId;
  if (!focusId) return null;
  const focusItem = getPageItems(p).find(it =>
    it.id === focusId
    && (it.kind !== "fill" || shouldShowFillInView(it))
    && it.box && it.box.length === 4
  );
  if (!focusItem) return null;

  const pgGrade = getPageGrading(p);
  const isPinned = focusItem.id === lockedId;
  // 参考选择题清透翠绿风格：默认统一清透绿蒙版，判分有错/漏选时对应暖橙/珊瑚红
  let rgb = [22, 163, 74];
  let fillA = isPinned ? 0.18 : 0.13;
  if (focusItem.kind === "choice") {
    const cg = pgGrade && pgGrade.items ? pgGrade.items[focusItem.id] : null;
    if (cg && cg.status === "correct") { rgb = [22, 163, 74]; fillA = 0.18; }
    else if (cg && cg.status === "partial") { rgb = [234, 88, 12]; fillA = 0.18; }
    else if (cg && cg.status === "wrong") { rgb = [220, 38, 38]; fillA = 0.17; }
    else { rgb = [21, 128, 61]; fillA = 0.16; }
  } else if (focusItem.kind === "fill") {
    const fg = pgGrade && pgGrade.items ? pgGrade.items[focusItem.id] : null;
    if (fg && fg.blanks) {
      const sts = Object.values(fg.blanks).map(x => x && x.status);
      if (sts.includes("wrong")) { rgb = [220, 38, 38]; fillA = isPinned ? 0.16 : 0.12; }
      else if (sts.includes("partial") || sts.includes("pending")) { rgb = [234, 88, 12]; fillA = isPinned ? 0.16 : 0.12; }
    }
  } else if (focusItem.kind === "identity") {
    rgb = [22, 163, 74];
    fillA = 0.14;
  }
  return { box: focusItem.box, rgb, fillA, isPinned };
}

function stepFxAnim(ts) {
  _fxAnim.raf = 0;
  const dt = _fxAnim.lastTs ? Math.min(0.05, Math.max(0.004, (ts - _fxAnim.lastTs) / 1000)) : 0.016;
  _fxAnim.lastTs = ts;

  const kBox = 1 - Math.exp(-dt * 24);
  const kAlpha = 1 - Math.exp(-dt * 18);
  const kColor = 1 - Math.exp(-dt * 20);

  _fxAnim.alpha += (_fxAnim.targetAlpha - _fxAnim.alpha) * kAlpha;
  _fxAnim.fillA += (_fxAnim.targetFillA - _fxAnim.fillA) * kColor;
  for (let i = 0; i < 3; i++) {
    _fxAnim.rgb[i] += (_fxAnim.targetRgb[i] - _fxAnim.rgb[i]) * kColor;
  }

  let boxDone = true;
  if (_fxAnim.box && _fxAnim.targetBox) {
    for (let i = 0; i < 4; i++) {
      const diff = _fxAnim.targetBox[i] - _fxAnim.box[i];
      _fxAnim.box[i] += diff * kBox;
      if (Math.abs(diff) > 0.35) boxDone = false;
    }
    if (boxDone) {
      for (let i = 0; i < 4; i++) _fxAnim.box[i] = _fxAnim.targetBox[i];
    }
  }

  const alphaDone = Math.abs(_fxAnim.targetAlpha - _fxAnim.alpha) < 0.008;
  const colorDone = Math.abs(_fxAnim.targetRgb[0] - _fxAnim.rgb[0]) < 0.8
    && Math.abs(_fxAnim.targetRgb[1] - _fxAnim.rgb[1]) < 0.8
    && Math.abs(_fxAnim.targetRgb[2] - _fxAnim.rgb[2]) < 0.8;

  if (alphaDone) _fxAnim.alpha = _fxAnim.targetAlpha;
  if (_fxAnim.alpha <= 0.005 && _fxAnim.targetAlpha === 0) {
    _fxAnim.alpha = 0;
    _fxAnim.box = null;
  }

  renderFxCanvasNow();

  if (!alphaDone || !boxDone || !colorDone) {
    _fxAnim.raf = requestAnimationFrame(stepFxAnim);
  } else {
    _fxAnim.lastTs = 0;
  }
}

function renderFxCanvasNow() {
  if (!fxCanvas || !fxCtx) return;
  const rect = getHostMetrics();
  const dpr = window.devicePixelRatio || 1;
  ensureCanvasBuffer(fxCanvas, fxCtx, rect.width, rect.height, dpr);
  fxCtx.clearRect(0, 0, rect.width, rect.height);
  if (!state.img) return;

  const inv = 1 / Math.max(0.15, state.scale);

  fxCtx.save();
  fxCtx.translate(state.tx, state.ty);
  fxCtx.scale(state.scale, state.scale);

  if (state.mode === "view" && !state.subboxTarget && _fxAnim.alpha > 0.005 && _fxAnim.box) {
    const [fx1, fy1, fx2, fy2] = _fxAnim.box;
    const fw = Math.max(2, fx2 - fx1);
    const fh = Math.max(2, fy2 - fy1);
    const rad = Math.min(8 * inv, fw * 0.25, fh * 0.25);
    const a = _fxAnim.alpha;

    // 清透浅雾白柔化蒙版（改卷用淡绿白纸雾、迭代用清透浅蓝雾），保留试卷明净感且柔和淡化未选中字迹
    const veilRgb = state.workMode === "iteration" ? "234, 241, 252" : "244, 248, 245";
    fxCtx.fillStyle = `rgba(${veilRgb}, ${(0.62 * a).toFixed(3)})`;
    fxCtx.beginPath();
    fxCtx.rect(0, 0, state.img.width, state.img.height);
    traceRoundedRect(fxCtx, fx1, fy1, fw, fh, rad);
    fxCtx.fill("evenodd");

    const r = Math.round(_fxAnim.rgb[0]);
    const g = Math.round(_fxAnim.rgb[1]);
    const b = Math.round(_fxAnim.rgb[2]);
    const isPinned = _fxAnim.isPinned;

    // 选中框内透亮半透明染色蒙版 + 柔和双层圆角亮边（零 shadowBlur 高帧率）
    fxCtx.beginPath();
    traceRoundedRect(fxCtx, fx1, fy1, fw, fh, rad);
    fxCtx.fillStyle = `rgba(${r}, ${g}, ${b}, ${(_fxAnim.fillA * a).toFixed(3)})`;
    fxCtx.fill();

    fxCtx.lineWidth = (isPinned ? 6.5 : 5.5) * inv;
    fxCtx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${(0.22 * a).toFixed(3)})`;
    fxCtx.stroke();

    fxCtx.lineWidth = (isPinned ? 2.8 : 2.3) * inv;
    fxCtx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${(0.95 * a).toFixed(3)})`;
    fxCtx.stroke();
  }

  // 局部真子集小框聚焦重识别：清透雾白压暗父大框外区域 + 亮起父大框引导边 + 绘制真子集小框
  if (state.subboxTarget && state.subboxTarget.parentBox) {
    const [px1, py1, px2, py2] = state.subboxTarget.parentBox;
    fxCtx.save();
    fxCtx.fillStyle = "rgba(226, 234, 246, 0.68)";
    fxCtx.beginPath();
    fxCtx.rect(-5000, -5000, state.img.width + 10000, state.img.height + 10000);
    fxCtx.rect(px1, py1, px2 - px1, py2 - py1);
    fxCtx.fill("evenodd");
    fxCtx.lineWidth = 3.0 * inv;
    fxCtx.strokeStyle = "#2563eb";
    fxCtx.setLineDash([8 * inv, 5 * inv]);
    fxCtx.strokeRect(px1, py1, px2 - px1, py2 - py1);
    fxCtx.setLineDash([]);
    fxCtx.restore();
  }
  if (state.drag && state.drag.type === "subbox_draw") {
    const d = state.drag;
    const x1 = Math.min(d.ix0, d.ix1), y1 = Math.min(d.iy0, d.iy1);
    const rw = Math.abs(d.ix1 - d.ix0), rh = Math.abs(d.iy1 - d.iy0);
    fxCtx.save();
    fxCtx.fillStyle = "rgba(16, 185, 129, 0.16)";
    fxCtx.fillRect(x1, y1, rw, rh);
    fxCtx.strokeStyle = "#059669";
    fxCtx.lineWidth = 2.6 * inv;
    fxCtx.strokeRect(x1, y1, rw, rh);
    fxCtx.restore();
  }

  if (state.drag && state.drag.type === "draw") {
    const d = state.drag;
    const meta = BOX_META[state.activeCls] || { color: "#2563eb", name: String(state.activeCls) };
    const x1 = Math.min(d.ix0, d.ix1), y1 = Math.min(d.iy0, d.iy1);
    const rw = Math.abs(d.ix1 - d.ix0), rh = Math.abs(d.iy1 - d.iy0);
    fxCtx.fillStyle = meta.color + "18";
    fxCtx.fillRect(x1, y1, rw, rh);
    fxCtx.strokeStyle = meta.color;
    fxCtx.lineWidth = 2.2 * inv;
    fxCtx.setLineDash([6 * inv, 4 * inv]);
    fxCtx.strokeRect(x1, y1, rw, rh);
    fxCtx.setLineDash([]);
    if (rw >= 6 * inv && rh >= 6 * inv) {
      fxCtx.font = `600 ${Math.max(10, 11.5 * inv)}px sans-serif`;
      const tag = `+ ${meta.name}`;
      const tw = fxCtx.measureText(tag).width + 8 * inv;
      const th = 16 * inv;
      fxCtx.fillStyle = meta.color;
      fxCtx.fillRect(x1, Math.max(0, y1 - th), tw, th);
      fxCtx.fillStyle = "#fff";
      fxCtx.fillText(tag, x1 + 4 * inv, Math.max(12 * inv, y1 - 3.5 * inv));
    }
  }

  fxCtx.restore();
}

/* 顶层轻量特效画布入口：自动驱动聚焦框形变衔接动效（零底图重绘） */
function drawFxCanvas(immediate = false) {
  const spec = resolveFocusTargetSpec();
  if (spec) {
    const prevTarget = _fxAnim.targetBox;
    const boxChanged = !prevTarget
      || Math.abs(prevTarget[0] - spec.box[0]) > 0.5
      || Math.abs(prevTarget[1] - spec.box[1]) > 0.5
      || Math.abs(prevTarget[2] - spec.box[2]) > 0.5
      || Math.abs(prevTarget[3] - spec.box[3]) > 0.5;
    _fxAnim.targetBox = [...spec.box];
    _fxAnim.targetAlpha = 1;
    _fxAnim.targetRgb = [...spec.rgb];
    _fxAnim.targetFillA = spec.fillA;
    _fxAnim.isPinned = spec.isPinned;
    // 从无聚焦进入首框（或要求立即定位）时直接就位，避免从旧页飞入
    if (immediate || !_fxAnim.box || _fxAnim.alpha <= 0.02) {
      _fxAnim.box = [...spec.box];
      _fxAnim.rgb = [...spec.rgb];
      _fxAnim.fillA = spec.fillA;
      if (immediate) _fxAnim.alpha = 1;
    }
    if (immediate) {
      if (_fxAnim.raf) { cancelAnimationFrame(_fxAnim.raf); _fxAnim.raf = 0; }
      _fxAnim.lastTs = 0;
      renderFxCanvasNow();
      return;
    }
    if (boxChanged || _fxAnim.alpha < 0.995) {
      if (!_fxAnim.raf) {
        _fxAnim.lastTs = 0;
        _fxAnim.raf = requestAnimationFrame(stepFxAnim);
      }
    }
    renderFxCanvasNow();
  } else {
    _fxAnim.targetBox = null;
    _fxAnim.targetAlpha = 0;
    if (immediate || _fxAnim.alpha <= 0.02 || state.mode !== "view" || state.subboxTarget) {
      if (_fxAnim.raf) { cancelAnimationFrame(_fxAnim.raf); _fxAnim.raf = 0; }
      _fxAnim.alpha = 0;
      _fxAnim.box = null;
      _fxAnim.lastTs = 0;
      renderFxCanvasNow();
      return;
    }
    if (!_fxAnim.raf) {
      _fxAnim.lastTs = 0;
      _fxAnim.raf = requestAnimationFrame(stepFxAnim);
    }
    renderFxCanvasNow();
  }
}

function resetFxAnim() {
  if (_fxAnim.raf) { cancelAnimationFrame(_fxAnim.raf); _fxAnim.raf = 0; }
  _fxAnim.alpha = 0;
  _fxAnim.targetAlpha = 0;
  _fxAnim.box = null;
  _fxAnim.targetBox = null;
  _fxAnim.lastTs = 0;
}

function drawCanvasOnly() {
  drawBaseCanvas();
  drawFxCanvas();
}

let _drawRaf = 0;
let _panRaf = 0;
let _fxRaf = 0;
function scheduleDraw() {
  if (_drawRaf) return;
  _drawRaf = requestAnimationFrame(() => {
    _drawRaf = 0;
    draw();
  });
}
function schedulePanFrame() {
  if (_panRaf) return;
  _panRaf = requestAnimationFrame(() => {
    _panRaf = 0;
    drawCanvasOnly();
    if (typeof syncOverlayPanTransform === "function") syncOverlayPanTransform();
  });
}
function scheduleFxOnly() {
  if (_fxRaf) return;
  _fxRaf = requestAnimationFrame(() => {
    _fxRaf = 0;
    drawFxCanvas();
  });
}

function draw() {
  if (_drawRaf) { cancelAnimationFrame(_drawRaf); _drawRaf = 0; }
  if (_panRaf) { cancelAnimationFrame(_panRaf); _panRaf = 0; }
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

