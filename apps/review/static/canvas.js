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

/* 底层纯静态原卷画布：仅绘制原卷图片（翻页/缩放/平移时重绘；悬停/动效/切题零底图重绘！） */
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
  ctx.restore();
}

function traceRoundedRect(c, x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w * 0.5, h * 0.5));
  if (rr <= 0.05) {
    c.rect(x, y, w, h);
    return;
  }
  c.moveTo(x + rr, y);
  c.arcTo(x + w, y, x + w, y + h, rr);
  c.arcTo(x + w, y + h, x, y + h, rr);
  c.arcTo(x, y + h, x, y, rr);
  c.arcTo(x, y, x + w, y, rr);
  c.closePath();
}

/* 聚焦框单框原位形变（直角->圆角）+ 蒙版渐入/淡出 + 跨框滑移衔接动效（静止自动休眠 0% 占用） */
const _fxAnim = {
  alpha: 0,
  targetAlpha: 0,
  entering: false,
  box: null,
  targetBox: null,
  focusId: null,      // 当前正在承载焦点框的题 ID（绘制底框时直接跳过，绝不套双框）
  rgb: [22, 163, 74],
  targetRgb: [22, 163, 74],
  fillA: 0.12,
  targetFillA: 0.12,
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
  const isIter = state.workMode === "iteration";
  // 统一清透主色：改卷模式翠绿（与选择题完全一致），迭代模式科技蓝；仅在判分错/漏时叠加红/橙
  let rgb = isIter ? [37, 99, 235] : [22, 163, 74];
  let fillA = isPinned ? 0.17 : 0.12;
  if (focusItem.kind === "choice") {
    const cg = pgGrade && pgGrade.items ? pgGrade.items[focusItem.id] : null;
    if (cg && cg.status === "correct") { rgb = [22, 163, 74]; fillA = 0.18; }
    else if (cg && cg.status === "partial") { rgb = [234, 88, 12]; fillA = 0.18; }
    else if (cg && cg.status === "wrong") { rgb = [220, 38, 38]; fillA = 0.17; }
    else { rgb = isIter ? [37, 99, 235] : [21, 128, 61]; fillA = 0.15; }
  } else if (focusItem.kind === "fill") {
    const fg = pgGrade && pgGrade.items ? pgGrade.items[focusItem.id] : null;
    if (fg && fg.blanks) {
      const sts = Object.values(fg.blanks).map(x => x && x.status);
      if (sts.includes("wrong")) { rgb = [220, 38, 38]; fillA = isPinned ? 0.16 : 0.12; }
      else if (sts.includes("partial") || sts.includes("pending")) { rgb = [234, 88, 12]; fillA = isPinned ? 0.16 : 0.12; }
    }
  } else if (focusItem.kind === "identity") {
    rgb = isIter ? [37, 99, 235] : [22, 163, 74];
    fillA = 0.13;
  }
  return { id: focusItem.id, box: focusItem.box, rgb, fillA, isPinned };
}

function stepFxAnim(ts) {
  _fxAnim.raf = 0;
  const dt = _fxAnim.lastTs ? Math.min(0.05, Math.max(0.004, (ts - _fxAnim.lastTs) / 1000)) : 0.016;
  _fxAnim.lastTs = ts;

  const kBox = 1 - Math.exp(-dt * 23);
  // 进入蒙版采用丝滑阻尼缓入（约 220ms），移出轻柔回退为原直角框
  const kAlpha = 1 - Math.exp(-dt * (_fxAnim.targetAlpha > _fxAnim.alpha ? 14 : 17));
  const kColor = 1 - Math.exp(-dt * 18);

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

  const alphaDone = Math.abs(_fxAnim.targetAlpha - _fxAnim.alpha) < 0.006;
  const colorDone = Math.abs(_fxAnim.targetRgb[0] - _fxAnim.rgb[0]) < 0.8
    && Math.abs(_fxAnim.targetRgb[1] - _fxAnim.rgb[1]) < 0.8
    && Math.abs(_fxAnim.targetRgb[2] - _fxAnim.rgb[2]) < 0.8;

  if (alphaDone) {
    _fxAnim.alpha = _fxAnim.targetAlpha;
    if (_fxAnim.alpha >= 0.99) _fxAnim.entering = false;
  }
  if (_fxAnim.alpha <= 0.005 && _fxAnim.targetAlpha === 0) {
    _fxAnim.alpha = 0;
    _fxAnim.box = null;
    _fxAnim.focusId = null;
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

  const p = currentPage();
  const inv = 1 / Math.max(0.15, state.scale);
  const pgGrade = (state.mode === "view" && p) ? getPageGrading(p) : null;
  const isIter = state.workMode === "iteration";
  const viewBaseColor = isIter ? "#2563eb" : "#15803d";

  const hasFocusAnim = state.mode === "view" && !state.subboxTarget && _fxAnim.alpha > 0.005 && Boolean(_fxAnim.box);
  const skipFocusId = hasFocusAnim ? _fxAnim.focusId : null;
  const skipTargetBox = hasFocusAnim ? (_fxAnim.targetBox || _fxAnim.box) : null;

  fxCtx.save();
  fxCtx.translate(state.tx, state.ty);
  fxCtx.scale(state.scale, state.scale);

  // 1) 绘制常规题框（看答案模式下：填空/姓名框与选择题统一清晰翠绿/科技蓝；当前焦点框在此跳过，交由单框形变层绘制，彻底杜绝套双框！）
  visibleBoxIndices().forEach(idx => {
    const b = state.workingBoxes[idx];
    const meta = BOX_META[b.cls] || { color: viewBaseColor, name: String(b.cls) };
    const [x1, y1, x2, y2] = b.box;

    if (state.mode === "view") {
      if (skipFocusId && (b.item_id === skipFocusId || (skipTargetBox && boxIoU(b.box, skipTargetBox) > 0.88))) {
        return;
      }
      fxCtx.lineWidth = 1.55 * inv;
      fxCtx.strokeStyle = viewBaseColor;
      fxCtx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      return;
    }

    const isSel = idx === state.selectedBoxIdx;
    fxCtx.lineWidth = (isSel ? 2.6 : 1.5) * inv;
    fxCtx.strokeStyle = meta.color;
    fxCtx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    if (isSel) {
      fxCtx.fillStyle = meta.color + "1c";
      fxCtx.fillRect(x1, y1, x2 - x1, y2 - y1);
      const hs = 5 * inv;
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const pts = [[x1,y1],[mx,y1],[x2,y1],[x2,my],[x2,y2],[mx,y2],[x1,y2],[x1,my]];
      fxCtx.fillStyle = "#fff";
      fxCtx.strokeStyle = meta.color;
      fxCtx.lineWidth = 1.6 * inv;
      pts.forEach(([px, py]) => {
        fxCtx.fillRect(px - hs, py - hs, hs * 2, hs * 2);
        fxCtx.strokeRect(px - hs, py - hs, hs * 2, hs * 2);
      });
    }
    const isRecogBusy = Boolean(b._uid && state.recognizingUids[b._uid]);
    if (isRecogBusy) {
      fxCtx.save();
      fxCtx.fillStyle = "rgba(37, 99, 235, 0.14)";
      fxCtx.fillRect(x1, y1, x2 - x1, y2 - y1);
      fxCtx.strokeStyle = "#2563eb";
      fxCtx.lineWidth = 2.6 * inv;
      fxCtx.setLineDash([7 * inv, 4 * inv]);
      fxCtx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      fxCtx.restore();
    }
    fxCtx.font = `600 ${Math.max(10, 11.5 * inv)}px sans-serif`;
    const confVal = typeof b.confidence === "number" ? b.confidence : (typeof b.conf === "number" ? b.conf : null);
    const confSuffix = state.showConf ? (confVal !== null ? ` ${Math.round(confVal * 100)}%` : " 人工") : "";
    const recogSuffix = isRecogBusy ? " · 识别中" : (isVlmRecogCls(b.cls) && b._needsRecog ? " · 待识别" : "");
    const tag = meta.name + confSuffix + recogSuffix;
    const tw = fxCtx.measureText(tag).width + 8 * inv;
    const th = 16 * inv;
    fxCtx.fillStyle = isRecogBusy
      ? "#2563eb"
      : ((isVlmRecogCls(b.cls) && b._needsRecog)
          ? "#d97706"
          : ((state.showConf && !b.trusted && confVal !== null && confVal < 0.7) ? "#d97706" : meta.color));
    fxCtx.fillRect(x1, Math.max(0, y1 - th), tw, th);
    fxCtx.fillStyle = "#fff";
    fxCtx.fillText(tag, x1 + 4 * inv, Math.max(12 * inv, y1 - 3.5 * inv));
  });

  // 2) 看答案模式：绘制选择题大区边界与各选择题框（当前焦点选择题同样跳过原直角框，由单框形变层接管）
  if (state.mode === "view" && p) {
    if (pgGrade && pgGrade.has_schema && Array.isArray(pgGrade.choice_zones)) {
      fxCtx.save();
      pgGrade.choice_zones.forEach(cz => {
        if (!cz.box || cz.box.length !== 4) return;
        const [zx1, zy1, zx2, zy2] = cz.box;
        fxCtx.lineWidth = 1.8 * inv;
        fxCtx.strokeStyle = isIter ? "rgba(37, 99, 235, 0.55)" : "rgba(22, 163, 74, 0.65)";
        fxCtx.setLineDash([6 * inv, 4 * inv]);
        fxCtx.strokeRect(zx1, zy1, zx2 - zx1, zy2 - zy1);
      });
      fxCtx.restore();
    }
    getPageItems(p).forEach(it => {
      if (it.kind !== "choice" || !it.box || it.box.length !== 4) return;
      if (skipFocusId && it.id === skipFocusId) return;
      const [x1, y1, x2, y2] = it.box;
      const cg = pgGrade && pgGrade.items ? pgGrade.items[it.id] : null;
      const low = isLowConfChoice(it);
      fxCtx.lineWidth = (cg ? 1.9 : 1.55) * inv;
      if (cg && cg.status === "correct") {
        fxCtx.fillStyle = "rgba(22, 163, 74, 0.12)";
        fxCtx.fillRect(x1, y1, x2 - x1, y2 - y1);
        fxCtx.strokeStyle = "#16a34a";
      } else if (cg && cg.status === "partial") {
        fxCtx.fillStyle = "rgba(234, 88, 12, 0.14)";
        fxCtx.fillRect(x1, y1, x2 - x1, y2 - y1);
        fxCtx.strokeStyle = "#ea580c";
      } else if (cg && cg.status === "wrong") {
        fxCtx.fillStyle = "rgba(220, 38, 38, 0.14)";
        fxCtx.fillRect(x1, y1, x2 - x1, y2 - y1);
        fxCtx.strokeStyle = "#dc2626";
      } else {
        fxCtx.strokeStyle = low ? "#d97706" : viewBaseColor;
      }
      fxCtx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    });
  }

  // 3) 蒙版渐入动效 + 单一焦点框原位由直角平滑变圆角并跨框滑移衔接（绝不叠双框！）
  if (hasFocusAnim) {
    const a = _fxAnim.alpha;
    const ease = 1 - Math.pow(1 - a, 3); // cubic ease-out：入场柔和沉稳
    const pad = 2.0 * inv * ease;        // 进入蒙版时微微外扩 2px 呼吸感，退出无缝贴回原框
    const fx1 = _fxAnim.box[0] - pad;
    const fy1 = _fxAnim.box[1] - pad;
    const fx2 = _fxAnim.box[2] + pad;
    const fy2 = _fxAnim.box[3] + pad;
    const fw = Math.max(2, fx2 - fx1);
    const fh = Math.max(2, fy2 - fy1);
    // 原有框本身从直角 (r=0) 平滑变形为圆角 (r=9px)，而非在直角框外再套一个圆角框
    const maxRad = Math.min(9 * inv, fw * 0.28, fh * 0.28);
    const rad = maxRad * ease;

    // 外部柔白纸雾蒙版随 ease 平滑渐入（柔和压淡未选中字迹与框线，同时保持试卷明亮洁净）
    const veilRgb = isIter ? "234, 241, 252" : "244, 248, 245";
    fxCtx.fillStyle = `rgba(${veilRgb}, ${(0.60 * ease).toFixed(3)})`;
    fxCtx.beginPath();
    fxCtx.rect(0, 0, state.img.width, state.img.height);
    traceRoundedRect(fxCtx, fx1, fy1, fw, fh, rad);
    fxCtx.fill("evenodd");

    const r = Math.round(_fxAnim.rgb[0]);
    const g = Math.round(_fxAnim.rgb[1]);
    const b = Math.round(_fxAnim.rgb[2]);
    const isPinned = _fxAnim.isPinned;

    // 唯一焦点框：柔和半透明着色蒙版 + 紧贴单框外沿的淡光晕 + 单一清晰圆角边框
    fxCtx.beginPath();
    traceRoundedRect(fxCtx, fx1, fy1, fw, fh, rad);
    fxCtx.fillStyle = `rgba(${r}, ${g}, ${b}, ${(_fxAnim.fillA * ease).toFixed(3)})`;
    fxCtx.fill();

    fxCtx.lineWidth = (1.55 + (isPinned ? 4.8 : 3.8) * ease) * inv;
    fxCtx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${(0.20 * ease).toFixed(3)})`;
    fxCtx.stroke();

    fxCtx.lineWidth = (1.55 + (isPinned ? 1.25 : 0.85) * ease) * inv;
    fxCtx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${(0.88 + 0.12 * ease).toFixed(3)})`;
    fxCtx.stroke();

    // 进入蒙版瞬间的轻柔聚焦波纹（单次扩散渐隐，提升入场手感）
    if (_fxAnim.entering && a < 0.98) {
      const rp = 11 * inv * ease;
      const ra = 0.34 * (1 - ease);
      if (ra > 0.01) {
        fxCtx.beginPath();
        traceRoundedRect(fxCtx, fx1 - rp, fy1 - rp, fw + rp * 2, fh + rp * 2, rad + rp * 0.45);
        fxCtx.lineWidth = 1.8 * inv;
        fxCtx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${ra.toFixed(3)})`;
        fxCtx.stroke();
      }
    }
  }

  // 4) 局部真子集小框聚焦重识别：清透雾白压暗父大框外区域 + 亮起父大框引导边 + 绘制真子集小框
  if (state.subboxTarget && state.subboxTarget.parentBox) {
    const [px1, py1, px2, py2] = state.subboxTarget.parentBox;
    fxCtx.save();
    fxCtx.fillStyle = "rgba(234, 241, 252, 0.68)";
    fxCtx.beginPath();
    fxCtx.rect(-5000, -5000, state.img.width + 10000, state.img.height + 10000);
    fxCtx.rect(px1, py1, px2 - px1, py2 - py1);
    fxCtx.fill("evenodd");
    fxCtx.lineWidth = 3.0 * inv;
    fxCtx.strokeStyle = isIter ? "#2563eb" : "#16a34a";
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
    fxCtx.fillStyle = "rgba(22, 163, 74, 0.16)";
    fxCtx.fillRect(x1, y1, rw, rh);
    fxCtx.strokeStyle = "#15803d";
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

/* 顶层轻量特效画布入口：自动驱动蒙版渐入 + 单框直角变圆角 + 跨框形变衔接（零底图重绘） */
function drawFxCanvas(immediate = false) {
  const spec = resolveFocusTargetSpec();
  if (spec) {
    const prevTarget = _fxAnim.targetBox;
    const boxChanged = !prevTarget
      || Math.abs(prevTarget[0] - spec.box[0]) > 0.5
      || Math.abs(prevTarget[1] - spec.box[1]) > 0.5
      || Math.abs(prevTarget[2] - spec.box[2]) > 0.5
      || Math.abs(prevTarget[3] - spec.box[3]) > 0.5;
    _fxAnim.focusId = spec.id;
    _fxAnim.targetBox = [...spec.box];
    _fxAnim.targetAlpha = 1;
    _fxAnim.targetRgb = [...spec.rgb];
    _fxAnim.targetFillA = spec.fillA;
    _fxAnim.isPinned = spec.isPinned;
    // 从无聚焦进入首框时，精确从该框原直角尺寸起跳，再平滑圆角化 + 渐入蒙版（零跳闪）
    if (immediate || !_fxAnim.box || _fxAnim.alpha <= 0.02) {
      _fxAnim.box = [...spec.box];
      _fxAnim.rgb = [...spec.rgb];
      _fxAnim.fillA = spec.fillA;
      _fxAnim.entering = !immediate;
      if (immediate) _fxAnim.alpha = 1;
    }
    if (immediate) {
      if (_fxAnim.raf) { cancelAnimationFrame(_fxAnim.raf); _fxAnim.raf = 0; }
      _fxAnim.entering = false;
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
    _fxAnim.entering = false;
    if (immediate || _fxAnim.alpha <= 0.02 || state.mode !== "view" || state.subboxTarget) {
      if (_fxAnim.raf) { cancelAnimationFrame(_fxAnim.raf); _fxAnim.raf = 0; }
      _fxAnim.alpha = 0;
      _fxAnim.box = null;
      _fxAnim.focusId = null;
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
  _fxAnim.entering = false;
  _fxAnim.box = null;
  _fxAnim.targetBox = null;
  _fxAnim.focusId = null;
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

