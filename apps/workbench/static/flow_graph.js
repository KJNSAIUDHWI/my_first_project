'use strict';
/* ==================== 通用 SVG 动态连线与真实数据脉冲引擎 ==================== */
const SVG_NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

function anchorPoint(stageRect, el, side, ratio = 0.5) {
  const r = el.getBoundingClientRect();
  const x = r.left - stageRect.left;
  const y = r.top - stageRect.top;
  if (side === 'right') return { x: x + r.width, y: y + r.height * ratio };
  if (side === 'left') return { x, y: y + r.height * ratio };
  if (side === 'bottom') return { x: x + r.width * ratio, y: y + r.height };
  if (side === 'top') return { x: x + r.width * ratio, y };
  return { x: x + r.width / 2, y: y + r.height / 2 };
}

function bezierPath(a, b, vertical = false) {
  if (vertical) {
    const dy = (b.y - a.y) * 0.5;
    return `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} C ${a.x.toFixed(1)} ${(a.y + dy).toFixed(1)}, ${b.x.toFixed(1)} ${(b.y - dy).toFixed(1)}, ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
  }
  const dx = Math.max(18, Math.abs(b.x - a.x) * 0.48);
  return `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} C ${(a.x + dx).toFixed(1)} ${a.y.toFixed(1)}, ${(b.x - dx).toFixed(1)} ${b.y.toFixed(1)}, ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
}

/* Step 1：与识别流同构的 6 节点体检 DAG 连线（配色随当前冷暖模式自适应，拒绝突兀亮绿） */
let ENV_STATES = { orient: 'ok', m1: 'ok', m2: 'ok', m3: 'ok', vlm: 'ok', review: 'ok' };
function drawEnvArchConnectors() {
  const stage = $('envDagStage') || $('envArchStage');
  const svg = $('envArchSvg');
  if (!stage || !svg || stage.offsetWidth === 0) return;
  const sr = stage.getBoundingClientRect();
  svg.innerHTML = '';
  const cs = getComputedStyle(document.body);
  const okColor = (cs.getPropertyValue('--ok-edge') || '#60a5fa').trim();
  const defs = svgEl('defs');
  [
    ['envArrOk', okColor],
    ['envArrWarn', '#d97706'],
    ['envArrErr', '#dc2626'],
    ['envArrIdle', '#94a3b8'],
  ].forEach(([id, color]) => {
    const m = svgEl('marker', { id, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto' });
    m.appendChild(svgEl('path', { d: 'M 0 1 L 9 5 L 0 9 z', fill: color }));
    defs.appendChild(m);
  });
  svg.appendChild(defs);

  const edges = [
    { from: 'envNodeOrient', fr: 0.5,  to: 'envNodeM1',     tr: 0.5,  sA: ENV_STATES.orient, sB: ENV_STATES.m1 },
    { from: 'envNodeM1',     fr: 0.34, to: 'envNodeM2',     tr: 0.5,  sA: ENV_STATES.m1,     sB: ENV_STATES.m2 },
    { from: 'envNodeM1',     fr: 0.68, to: 'envNodeM3',     tr: 0.5,  sA: ENV_STATES.m1,     sB: ENV_STATES.m3 },
    { from: 'envNodeM3',     fr: 0.5,  to: 'envNodeVlm',    tr: 0.5,  sA: ENV_STATES.m3,     sB: ENV_STATES.vlm },
    { from: 'envNodeM2',     fr: 0.5,  to: 'envNodeReview', tr: 0.36, sA: ENV_STATES.m2,     sB: ENV_STATES.review },
    { from: 'envNodeVlm',    fr: 0.5,  to: 'envNodeReview', tr: 0.66, sA: ENV_STATES.vlm,    sB: ENV_STATES.review },
  ];
  edges.forEach(e => {
    const aEl = $(e.from), bEl = $(e.to);
    if (!aEl || !bEl) return;
    const a = anchorPoint(sr, aEl, 'right', e.fr);
    const b = anchorPoint(sr, bEl, 'left', e.tr);
    if (b.x - a.x < 8) return;
    const d = bezierPath(a, { x: b.x - 4, y: b.y }, false);
    const bad = e.sA === 'err' || e.sB === 'err';
    const warn = !bad && (e.sA === 'warn' || e.sB === 'warn');
    const cls = bad ? 'err' : (warn ? 'warn' : 'done');
    const arrId = bad ? 'envArrErr' : (warn ? 'envArrWarn' : 'envArrOk');
    svg.appendChild(svgEl('path', { d, class: `pipe-base ${cls}`, 'marker-end': `url(#${arrId})` }));
    if (!bad && !warn) {
      svg.appendChild(svgEl('path', { d, class: 'pipe-stream flowing', style: 'opacity:.22' }));
    }
  });
}

/* Step 3：真实数据驱动流动动画引擎 */
const PipeFlow = (() => {
  const EDGES = [
    { id: 'orient_m1',  from: 'nodeOrient', fromSide: 'right',  fromRatio: 0.5,  to: 'nodeM1',     toSide: 'left',  toRatio: 0.5,  color: 'green',  vertical: false },
    { id: 'm1_m2',      from: 'nodeM1',     fromSide: 'right',  fromRatio: 0.34, to: 'nodeM2',     toSide: 'left',  toRatio: 0.5,  color: 'green',  vertical: false },
    { id: 'm1_m3',      from: 'nodeM1',     fromSide: 'right',  fromRatio: 0.68, to: 'nodeM3',     toSide: 'left',  toRatio: 0.5,  color: 'blue',   vertical: false },
    { id: 'm2_out',     from: 'nodeM2',     fromSide: 'right',  fromRatio: 0.5,  to: 'nodeOut',    toSide: 'left',  toRatio: 0.36, color: 'green',  vertical: false },
    { id: 'm3_api',     from: 'nodeM3',     fromSide: 'right',  fromRatio: 0.5,  to: 'nodeApi',    toSide: 'left',  toRatio: 0.5,  color: 'blue',   vertical: false },
    { id: 'api_out',    from: 'nodeApi',    fromSide: 'right',  fromRatio: 0.42, to: 'nodeOut',    toSide: 'left',  toRatio: 0.72, color: 'violet', vertical: false },
    { id: 'out_review', from: 'nodeOut',    fromSide: 'bottom', fromRatio: 0.5,  to: 'nodeReview', toSide: 'top',   toRatio: 0.5,  color: 'green',  vertical: true },
  ];

  function getFlowPalette() {
    const cs = getComputedStyle(document.body);
    const primary = (cs.getPropertyValue('--primary') || '#b8531b').trim();
    const primarySoft = (cs.getPropertyValue('--primary-soft') || '#fdf1e6').trim();
    const primaryBorder = (cs.getPropertyValue('--primary-border') || '#ecc8a6').trim();
    const primaryRing = (cs.getPropertyValue('--primary-ring') || 'rgba(184,83,27,0.28)').trim();
    const okEdge = (cs.getPropertyValue('--ok-edge') || '#7fa585').trim();
    const isIter = document.body.dataset.workMode === 'iteration';
    return {
      arrMain: isIter ? primary : okEdge,
      green:  { fill: primary, glow: primaryRing, textBg: primarySoft, textBorder: primaryBorder, textColor: primary, pulseCls: 'recv-pulse' },
      blue:   { fill: '#2563eb', glow: 'rgba(37,99,235,0.32)',  textBg: '#eff6ff', textBorder: '#60a5fa', textColor: '#1e40af', pulseCls: 'recv-pulse-blue' },
      violet: { fill: '#7c3aed', glow: 'rgba(124,58,237,0.32)', textBg: '#f5f3ff', textBorder: '#a78bfa', textColor: '#5b21b6', pulseCls: 'recv-pulse' },
    };
  }

  let edgeEls = {};
  let edgeMeta = {};
  let particles = [];
  let rafId = null;
  let lastBatchId = '';
  let prevSnap = null;
  let lastLayoutKey = '';

  function ensureDefs(svg) {
    const pal = getFlowPalette();
    const defs = svgEl('defs');
    [
      ['arrIdle', '#cbd5e1'],
      ['arrGreen', pal.arrMain],
      ['arrBlue', '#2563eb'],
      ['arrViolet', '#7c3aed'],
      ['arrWarn', '#d97706'],
      ['arrErr', '#dc2626'],
    ].forEach(([id, color]) => {
      const m = svgEl('marker', { id, viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto' });
      m.appendChild(svgEl('path', { d: 'M 0 1 L 9 5 L 0 9 z', fill: color }));
      defs.appendChild(m);
    });
    svg.appendChild(defs);
  }

  function relayout(force = false) {
    const stage = $('pipeArchStage');
    const svg = $('pipeArchSvg');
    const pillsHost = $('pipeEdgePills');
    if (!stage || !svg || !pillsHost || stage.offsetWidth === 0 || ARCH_COLLAPSED) return;
    const sr = stage.getBoundingClientRect();
    const layoutKey = `${Math.round(sr.width)}x${Math.round(sr.height)}`;
    if (!force && lastLayoutKey === layoutKey && Object.keys(edgeEls).length === EDGES.length) {
      return;
    }
    lastLayoutKey = layoutKey;
    svg.innerHTML = '';
    pillsHost.innerHTML = '';
    ensureDefs(svg);
    edgeEls = {};

    EDGES.forEach(e => {
      const fEl = $(e.from), tEl = $(e.to);
      if (!fEl || !tEl) return;
      const a = anchorPoint(sr, fEl, e.fromSide, e.fromRatio);
      const bRaw = anchorPoint(sr, tEl, e.toSide, e.toRatio);
      const b = e.vertical ? { x: bRaw.x, y: bRaw.y - 4 } : { x: bRaw.x - 4, y: bRaw.y };
      const d = bezierPath(a, b, e.vertical);

      const base = svgEl('path', { d, class: 'pipe-base' });
      const burst = svgEl('path', { d, class: `pipe-burst ${e.color === 'blue' ? 'blue' : (e.color === 'violet' ? 'violet' : '')}` });
      const stream = svgEl('path', { d, class: `pipe-stream ${e.color === 'blue' ? 'blue' : (e.color === 'violet' ? 'violet' : '')}` });
      svg.appendChild(base);
      svg.appendChild(burst);
      svg.appendChild(stream);

      const len = base.getTotalLength();
      const midPt = base.getPointAtLength(len * 0.5);
      const pill = document.createElement('div');
      pill.className = 'edge-pill';
      pill.style.left = midPt.x.toFixed(1) + 'px';
      pill.style.top = (midPt.y + (e.vertical ? 0 : -11)).toFixed(1) + 'px';
      if (e.vertical) pill.style.left = (midPt.x + 30).toFixed(1) + 'px';
      pillsHost.appendChild(pill);

      edgeEls[e.id] = { cfg: e, base, burst, stream, pill, len };
      const meta = edgeMeta[e.id];
      if (meta) applyEdgeVisual(e.id, meta);
    });
  }

  function applyEdgeVisual(edgeId, meta) {
    edgeMeta[edgeId] = meta;
    const ref = edgeEls[edgeId];
    if (!ref) return;
    const { cfg, base, stream, pill } = ref;
    const colorCls = cfg.color === 'blue' ? 'blue' : '';
    const st = meta.state || 'idle';
    base.setAttribute('class', `pipe-base ${st} ${colorCls}`);
    const marker = st === 'err' ? 'arrErr'
      : st === 'warn' ? 'arrWarn'
      : (st === 'active' || st === 'done')
        ? (cfg.color === 'blue' ? 'arrBlue' : (cfg.color === 'violet' ? 'arrViolet' : 'arrGreen'))
        : 'arrIdle';
    base.setAttribute('marker-end', `url(#${marker})`);
    stream.setAttribute('class', `pipe-stream ${cfg.color === 'blue' ? 'blue' : (cfg.color === 'violet' ? 'violet' : '')}${meta.flowing ? ' flowing' : ''}`);
    if (meta.label) {
      pill.textContent = meta.label;
      pill.style.display = '';
      pill.className = 'edge-pill' + (st === 'err' ? ' err' : (st === 'active' || st === 'done' ? (cfg.color === 'blue' ? ' blue' : ' active') : ''));
    } else {
      pill.style.display = 'none';
    }
  }

  function pulseNode(nodeId, pulseCls) {
    const el = $(nodeId);
    if (!el) return;
    el.classList.remove('recv-pulse', 'recv-pulse-blue');
    void el.offsetWidth;
    el.classList.add(pulseCls || 'recv-pulse');
  }

  function emitPacket(edgeId, deltaLabel = '', count = 1) {
    const ref = edgeEls[edgeId];
    const pSvg = $('pipeParticleSvg');
    if (!ref || !pSvg || ARCH_COLLAPSED || document.hidden) return;
    const palMap = getFlowPalette();
    const pal = palMap[ref.cfg.color] || palMap.green;

    // 管道瞬时高亮
    ref.burst.classList.add('flash');
    clearTimeout(ref._bt);
    ref._bt = setTimeout(() => ref.burst.classList.remove('flash'), 420);

    const n = Math.max(1, Math.min(3, count));
    for (let k = 0; k < n; k++) {
      const g = svgEl('g');
      const halo = svgEl('circle', { r: 8.5, fill: pal.glow });
      const core = svgEl('circle', { r: 4.2, fill: pal.fill, stroke: '#ffffff', 'stroke-width': 1.5 });
      g.appendChild(halo);
      g.appendChild(core);
      if (k === 0 && deltaLabel) {
        const bw = Math.max(26, deltaLabel.length * 7.2 + 10);
        const tagG = svgEl('g', { class: 'pkt-tag' });
        const bg = svgEl('rect', { x: (-bw / 2).toFixed(1), y: -23, width: bw.toFixed(1), height: 15, rx: 7.5, fill: pal.textBg, stroke: pal.textBorder, 'stroke-width': 1 });
        const txt = svgEl('text', { x: 0, y: -12.3, 'text-anchor': 'middle', 'font-size': 9.2, 'font-weight': 800, fill: pal.textColor });
        txt.textContent = deltaLabel;
        tagG.appendChild(bg);
        tagG.appendChild(txt);
        g.appendChild(tagG);
      }
      pSvg.appendChild(g);
      particles.push({
        edgeId,
        g,
        tagG: g.querySelector('.pkt-tag'),
        targetNode: ref.cfg.to,
        pulseCls: pal.pulseCls,
        start: performance.now() + k * 170,
        dur: 1180 + k * 65,
      });
    }
    if (!rafId) rafId = requestAnimationFrame(tick);
  }

  function tick(now) {
    const keep = [];
    for (const p of particles) {
      const ref = edgeEls[p.edgeId];
      if (!ref || !p.g.parentNode) { p.g.remove(); continue; }
      const rawT = (now - p.start) / p.dur;
      if (rawT < 0) {
        p.g.setAttribute('opacity', '0');
        keep.push(p);
        continue;
      }
      if (rawT >= 1) {
        p.g.remove();
        pulseNode(p.targetNode, p.pulseCls);
        continue;
      }
      // 平滑缓出插值；+Δ 标签在管道中段稳定停留清晰可读，靠近目标卡片前柔和淡出避免遮字
      const t = 1 - Math.pow(1 - rawT, 2.1);
      const pt = ref.base.getPointAtLength(t * ref.len);
      const op = rawT < 0.12 ? rawT / 0.12 : (rawT > 0.86 ? (1 - rawT) / 0.14 : 1);
      p.g.setAttribute('transform', `translate(${pt.x.toFixed(1)},${pt.y.toFixed(1)})`);
      p.g.setAttribute('opacity', op.toFixed(2));
      if (p.tagG) {
        const tagOp = rawT < 0.14 ? rawT / 0.14 : (rawT > 0.64 ? Math.max(0, (0.82 - rawT) / 0.18) : 1);
        p.tagG.setAttribute('opacity', tagOp.toFixed(2));
      }
      keep.push(p);
    }
    particles = keep;
    rafId = particles.length ? requestAnimationFrame(tick) : null;
  }

  /** 根据真实计数增量（prevSnap -> snap）发射对应连线的真实数据包动效 */
  function ingestSnapshot(batchId, snap, running) {
    if (batchId !== lastBatchId) {
      lastBatchId = batchId;
      prevSnap = null;
    }
    if (!prevSnap) {
      prevSnap = { ...snap, _running: running };
      // 若首次看到正在运行中的任务，对当前活跃传输边发一束引导脉冲，一眼看出数据正往哪流
      if (running) {
        if (snap.orientDone > 0) emitPacket('orient_m1', `${snap.orientDone}页`, 1);
        if (snap.m1Choice > 0) emitPacket('m1_m2', `${snap.m1Choice}区`, 1);
        if (snap.m1Fill > 0) emitPacket('m1_m3', `${snap.m1Fill}区`, 1);
        if (snap.choiceItems > 0) emitPacket('m2_out', `${snap.choiceItems}题`, 1);
        if (snap.tilesQueued > 0) emitPacket('m3_api', `${snap.tilesQueued}块`, 1);
        if (snap.apiSuccess > 0) emitPacket('api_out', `${snap.apiSuccess}图`, 1);
        if (snap.pagesDone > 0) emitPacket('out_review', `${snap.pagesDone}页`, 1);
      }
      return;
    }

    // 任务刚从空闲切入运行：清零基线，让第一张完成的图就触发脉冲
    if (running && !prevSnap._running) {
      prevSnap = {
        orientDone: 0, m1Pages: 0, m1Choice: 0, m1Fill: 0,
        choiceItems: 0, tilesQueued: 0, apiSuccess: 0, transCnt: 0,
        pagesDone: 0, reviewedPages: snap.reviewedPages, _running: true,
      };
    }

    const dOrient = snap.orientDone - prevSnap.orientDone;
    const dM1Choice = snap.m1Choice - prevSnap.m1Choice;
    const dM1Fill = snap.m1Fill - prevSnap.m1Fill;
    const dM1Pages = snap.m1Pages - prevSnap.m1Pages;
    const dChoice = snap.choiceItems - prevSnap.choiceItems;
    const dTiles = snap.tilesQueued - prevSnap.tilesQueued;
    const dApiOk = snap.apiSuccess - prevSnap.apiSuccess;
    const dTrans = snap.transCnt - prevSnap.transCnt;
    const dPages = snap.pagesDone - prevSnap.pagesDone;
    const dRev = snap.reviewedPages - prevSnap.reviewedPages;

    if (dOrient > 0) emitPacket('orient_m1', `+${dOrient}页`, Math.min(3, dOrient));
    if (dM1Choice > 0) emitPacket('m1_m2', `+${dM1Choice}区`, Math.min(3, dM1Choice));
    else if (dM1Pages > 0 && snap.m1Choice > 0) emitPacket('m1_m2', `+${dM1Pages}页`, 1);
    if (dM1Fill > 0) emitPacket('m1_m3', `+${dM1Fill}区`, Math.min(3, dM1Fill));
    if (dChoice > 0) emitPacket('m2_out', `+${dChoice}题`, Math.min(3, Math.ceil(dChoice / 6)));
    if (dTiles > 0) emitPacket('m3_api', `+${dTiles}块`, Math.min(3, Math.ceil(dTiles / 2)));
    if (dTrans > 0) emitPacket('api_out', `+${dTrans}处`, Math.min(3, Math.ceil(dTrans / 3)));
    else if (dApiOk > 0) emitPacket('api_out', `+${dApiOk}图`, Math.min(3, dApiOk));
    if (dRev > 0) emitPacket('out_review', `已审+${dRev}`, Math.min(3, dRev));
    else if (dPages > 0) emitPacket('out_review', `+${dPages}页`, Math.min(2, dPages));

    prevSnap = { ...snap, _running: running };
  }

  function triggerReviewPulse(label = '开始审阅') {
    emitPacket('out_review', label, 2);
  }

  return { relayout, applyEdgeVisual, ingestSnapshot, triggerReviewPulse };
})();

window.addEventListener('resize', () => {
  if (CURRENT_VIEW === 'check') drawEnvArchConnectors();
  if (CURRENT_VIEW === 'overview') PipeFlow.relayout(true);
});

