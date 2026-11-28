'use strict';
/* ==================== Step 3：实时流水线架构图渲染 ==================== */
function fmtSec(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return '';
  const s = Math.max(0, Math.round(Number(sec)));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${s % 60}s`;
}

function setArchNodeState(nodeId, dotId, state, extraCls = '') {
  const node = $(nodeId), dot = $(dotId);
  if (node) node.className = `arch-node is-${state || 'idle'}${extraCls ? ' ' + extraCls : ''}`;
  if (dot) dot.className = 'arch-dot' + (state && state !== 'idle' ? ' ' + state : '');
}

function fmtDeviceShort(raw, hw) {
  let s = String(raw || '').trim();
  if ((!s || /^cuda(:\d+)?$/i.test(s)) && hw?.cuda && hw.gpu_name) {
    s = `${hw.gpu_name}${hw.vram_gb ? ` (${hw.vram_gb} GB)` : ''}`;
  }
  if (!s) return '';
  const vramM = s.match(/\(([\d.]+)\s*GB\)/i);
  const vram = vramM ? `${parseFloat(vramM[1])}G` : '';
  let body = s
    .replace(/^GPU:\s*/i, '')
    .replace(/\s*\([^)]*GB\)\s*/ig, '')
    .replace(/\bNVIDIA\s+GeForce\s+/i, '')
    .replace(/\bNVIDIA\s+/i, '')
    .replace(/\bAMD\s+Radeon\s+/i, 'Radeon ')
    .replace(/\s+Laptop\s+GPU$/i, '')
    .replace(/\s+GPU$/i, '')
    .trim();
  if (/^cuda/i.test(body)) body = 'GPU';
  return vram ? `${body} · ${vram}` : body;
}

function fmtModelShort(providerLabel, modelId) {
  const m = String(modelId || '').trim();
  const prov = String(providerLabel || '').trim();
  if (!m) return prov;
  let shortM = m.replace(/-\d{6,8}$/, '');
  if (prov) {
    shortM = shortM.replace(/^(doubao|volc|deepseek|qwen|glm|mimo)[-_]/i, '');
    return `${prov} · ${shortM}`;
  }
  return shortM;
}

let LAST_SPEED_UI_TS = 0;

function applyArchCollapseUI() {
  $('archStageBox').classList.toggle('hidden', ARCH_COLLAPSED);
  $('btnToggleArch').textContent = ARCH_COLLAPSED ? '展开流向图 ▾' : '收起流向图 ▴';
  if (!ARCH_COLLAPSED) requestAnimationFrame(() => PipeFlow.relayout(true));
}

function renderPipelineDashboard(data) {
  const ab = data.active_batch;
  const rawJob = data.pipeline_job || {};
  const jobMatchesBatch = !ab || !rawJob.batch_id || rawJob.batch_id === ab.batch_id;
  const job = jobMatchesBatch ? rawJob : {};
  const rs = data.recognition_summary || (jobMatchesBatch ? job.result_summary : null) || null;
  const running = !!rawJob.running && jobMatchesBatch;
  const anyJobRunning = !!rawJob.running;

  const pageFailCnt = Number(((rs && rs.stats) || {}).page_failures || 0);
  const apiFailCnt = Number(((rs && rs.stats) || {}).api_failures || 0);
  const failedItemsCnt = Number((rs && rs.failed_items) || 0);
  const hasFailed = Boolean(rs && (failedItemsCnt > 0 || apiFailCnt > 0 || pageFailCnt > 0));
  const retryBtn = $('btnRetryFailed');
  retryBtn.classList.toggle('hidden', !ab || !hasFailed || anyJobRunning);
  if (hasFailed) {
    retryBtn.innerHTML = pageFailCnt > 0
      ? `${uiIcon('refresh')}<span>补跑失败 (${pageFailCnt}页)</span>`
      : (failedItemsCnt > 0 ? `${uiIcon('refresh')}<span>补跑失败 (${failedItemsCnt}处)</span>` : `${uiIcon('refresh')}<span>补跑失败</span>`);
    retryBtn.title = pageFailCnt > 0
      ? '仅补跑本地检测失败的页面，已成功页与已转录内容自动复用不重复扣费'
      : '仅重跑失败题块，已成功项不重复扣费';
  }
  $('btnStopRecognize').classList.toggle('hidden', !anyJobRunning);

  const showDash = running || (job.status && job.status !== 'idle') || (rs && rs.stats);
  $('pipelineDashboard').classList.toggle('hidden', !showDash);
  if (!showDash) return;

  const isErrState = !running && (job.status === 'error' || String(rs?.status || '').includes('识别失败'));
  const isWarnState = !running && !isErrState && (job.status === 'warn' || hasFailed);

  $('pipelineDashboard').className = 'pipe-dash' + (
    running ? ' running' : (isErrState ? ' error' : (isWarnState ? ' warn' : ''))
  );

  // 运行中或出错自动展开流向图；已完成则默认自动收起为紧凑摘要条，把首屏 C 位留给「人工复审」
  if (running || isErrState) {
    ARCH_COLLAPSED = false;
  } else {
    ARCH_COLLAPSED = !ARCH_USER_EXPANDED;
  }
  applyArchCollapseUI();

  const prog = job.progress || {};
  const st = (rs && rs.stats) || {};
  const pick = (k, fallback = 0) => {
    if (running && prog[k] !== undefined && prog[k] !== null) return Number(prog[k]) || 0;
    if (st[k] !== undefined && st[k] !== null) return Number(st[k]) || 0;
    if (prog[k] !== undefined && prog[k] !== null) return Number(prog[k]) || 0;
    return Number(fallback) || 0;
  };
  const pctOf = (a, b) => (b > 0 ? Math.max(0, Math.min(100, Math.round((a / b) * 100))) : 0);

  const pagesTotal = Math.max(1, pick('pages_total', ab?.total_images || st.pages || 0));
  const pagesDone = running ? Number(prog.pages_done || 0) : pick('pages', prog.pages_done || 0);
  const pageFail = pick('page_failures', 0);
  const orientDone = Math.max(pagesDone - pageFail, pick('orient_done', pagesDone - pageFail));
  const m1Pages = Math.max(pagesDone - pageFail, pick('m1_pages_done', pagesDone - pageFail));
  const m1Choice = pick('m1_choice_zones', 0);
  const m1Fill = pick('m1_fill_zones', 0);
  const m1Identity = pick('m1_identity_zones', 0);

  const m2ZonesYolo = Math.max(pick('m2_zones_yolo_done', 0), pick('m2_zones_done', 0));
  const m2ZonesDone = pick('m2_zones_done', m2ZonesYolo);
  const m2Ocr = pick('m2_ocr_done', pick('choice_items', 0));
  const choiceItems = pick('choice_items', pick('m2_cnn_done', 0));

  const m3ZonesYolo = Math.max(pick('m3_zones_yolo_done', 0), pick('m3_zones_done', 0));
  const m3ZonesDone = pick('m3_zones_done', m3ZonesYolo);
  const m3Ocr = pick('m3_ocr_done', 0);
  const fillItems = pick('fill_items', 0);
  const idItems = pick('identity_items', m1Identity);
  const tilesQueued = pick('tiles_queued', fillItems + idItems);

  const gridsTotal = running ? Number(prog.grids_total || 0) : pick('grids', prog.grids_total || 0);
  const apiSuccess = running ? Number(prog.api_success || 0) : pick('api_success', prog.api_success || 0);
  const apiFail = running ? Number(prog.api_failures || 0) : pick('api_failures', prog.api_failures || 0);
  const apiInflight = running ? Number(prog.api_inflight || 0) : 0;
  const transCnt = running ? Number(prog.items_with_transcription || 0) : pick('items_with_transcription', prog.items_with_transcription || 0);

  const reviewTask = (data.tasks || []).find(t => t.id === 'review') || {};
  const reviewedPages = Number(reviewTask.done || 0);
  const reviewTotal = Number(reviewTask.total || pagesDone || pagesTotal);

  const detFrac = pagesTotal > 0 ? Math.min(1, pagesDone / pagesTotal) : 0;
  const m1Frac = pagesTotal > 0 ? Math.min(1, m1Pages / pagesTotal) : 0;
  const apiLocalFrac = gridsTotal > 0 ? Math.min(1, apiSuccess / gridsTotal) : (detFrac >= 1 ? 1 : 0);
  const globalApiFrac = (running && job.kind === 'retry') ? apiLocalFrac : (detFrac * apiLocalFrac);

  let pct = 0;
  if (!running && !isErrState && !isWarnState && (job.status === 'done' || rs)) {
    pct = 100;
  } else if (running && job.kind === 'retry') {
    pct = Math.round(apiLocalFrac * 100);
    if (pct < 4) pct = 4;
  } else {
    pct = Math.round((detFrac * 0.55 + globalApiFrac * 0.45) * 100);
    if (running && pct < 4) pct = 4;
  }
  $('pipeMainBar').style.width = pct + '%';
  $('pipeMainPct').textContent = pct + '%';
  $('pipeMainBarWrap').className = 'prog-bar' + (isErrState ? ' error' : (isWarnState ? ' warn' : ''));

  let stageTitle = '识别完成';
  if (running) {
    if (job.kind === 'retry') stageTitle = '补跑失败图块';
    else if (orientDone === 0 && m1Pages === 0 && gridsTotal === 0) {
      stageTitle = prog.stage === '加载本地检测模型' ? '加载本地模型' : '准备模型与环境';
    } else if (pagesDone < pagesTotal) {
      stageTitle = '并行识别与转录中';
    } else if (gridsTotal > 0 && (apiSuccess + apiFail) < gridsTotal) {
      stageTitle = '等待大模型返回';
    } else {
      stageTitle = '正在汇总落盘';
    }
  } else if (isErrState) {
    stageTitle = '识别失败';
  } else if (isWarnState) {
    stageTitle = '部分待补跑';
  }
  $('pipeStageTitle').textContent = stageTitle;
  const badgeEl = $('pipeStageBadge');
  if (running) { badgeEl.className = 'badge ok'; badgeEl.textContent = '运行中'; }
  else if (isErrState) { badgeEl.className = 'badge err'; badgeEl.textContent = '异常'; }
  else if (isWarnState) { badgeEl.className = 'badge warn'; badgeEl.textContent = '待补跑'; }
  else { badgeEl.className = 'badge ok'; badgeEl.textContent = '已完成'; }

  const metaParts = [];
  const rawDev = prog.device_label || (rs?.settings?.device ? String(rs.settings.device).toUpperCase() : '');
  const devChip = fmtDeviceShort(rawDev, data.hardware);
  if (devChip) metaParts.push(`<span class="pipe-meta-chip" title="${esc(rawDev || devChip)}">${esc(devChip)}</span>`);
  const modTxt = prog.model || rs?.model || '';
  const provTxt = prog.provider_label || rs?.provider_label || '';
  const modChip = fmtModelShort(provTxt, modTxt);
  if (modChip) metaParts.push(`<span class="pipe-meta-chip" title="${esc(modTxt)}">${esc(modChip)}</span>`);
  const costInfo = (running && prog.api_cost) ? prog.api_cost : (rs?.api_cost || prog.api_cost || null);
  let costShortText = '';
  if (costInfo && costInfo.found && (costInfo.total_tokens > 0 || !running)) {
    const srcTag = costInfo.source === 'official_crawl'
      ? `官网定价${costInfo.crawled_at ? ' ' + costInfo.crawled_at.slice(0, 10) : ''}（已永久缓存）`
      : (costInfo.source === 'manual' ? '自定义单价' : '参考单价');
    const perPageHint = costInfo.per_page_display ? ` · 均 ${costInfo.per_page_display}/页` : '';
    const reuseHint = costInfo.all_reused ? ' · 本次命中本地缓存 0 扣费' : '';
    const subHint = costInfo.subscription ? '（套餐订阅内）' : '';
    const tip = `输入 ${Number(costInfo.prompt_tokens || 0).toLocaleString()} × ¥${costInfo.input_per_m}/M + 输出 ${Number(costInfo.completion_tokens || 0).toLocaleString()} × ¥${costInfo.output_per_m}/M = ${costInfo.cost_display}${perPageHint}${reuseHint} · ${srcTag}`;
    costShortText = costInfo.subscription ? `套餐内 (${costInfo.cost_display})` : costInfo.cost_display;
    const chipTxt = costInfo.all_reused
      ? `费用 ${costShortText} · 缓存`
      : `费用 ${costShortText}${subHint}`;
    metaParts.push(`<span class="pipe-meta-chip cost" title="${esc(tip)}">${esc(chipTxt)}</span>`);
  }
  if (rs?.returned_model && rs.returned_model !== rs.model) {
    metaParts.push(`<span class="pipe-meta-chip warn" title="${esc(String(rs.returned_model))}">实际: ${esc(fmtModelShort('', rs.returned_model))}</span>`);
  }
  const metaHtml = metaParts.join('');
  if ($('pipeMetaRow').dataset.h !== metaHtml) {
    $('pipeMetaRow').dataset.h = metaHtml;
    $('pipeMetaRow').innerHTML = metaHtml;
  }

  const transTotalCur = Math.max(transCnt, tilesQueued);
  if (running) {
    if (orientDone === 0 && pagesDone === 0 && gridsTotal === 0) {
      $('pipeMainDesc').textContent = prog.stage === '加载本地检测模型'
        ? '正在将本地检测模型加载至计算设备...'
        : '正在初始化识别环境与并行工作池...';
    } else {
      $('pipeMainDesc').textContent = `已落盘 ${pagesDone}/${pagesTotal} 页 · 选择 ${choiceItems} 题 · 手写 ${transCnt}/${transTotalCur} 处`
        + (apiInflight > 0 ? ` · 大模型在途 ${apiInflight} 图` : '')
        + (pageFail > 0 ? ` · 失败 ${pageFail} 页` : '');
    }
  } else {
    $('pipeMainDesc').textContent = `${pagesDone}/${pagesTotal} 页 · 选择 ${choiceItems} 题 · 手写 ${transCnt} 处`
      + (pageFail > 0 ? ` · 失败 ${pageFail} 页` : '')
      + (hasFailed ? ` · 待补跑 ${rs?.failed_items || apiFail}` : '');
  }
  const nowPerf = performance.now();
  if (!running || nowPerf - LAST_SPEED_UI_TS >= 900 || !$('pipeSpeedEta').textContent) {
    LAST_SPEED_UI_TS = nowPerf;
    const spdParts = [];
    if (prog.pages_per_min > 0) spdParts.push(`${Number(prog.pages_per_min).toFixed(1)} 页/分`);
    if (running && prog.eta_seconds !== null && prog.eta_seconds !== undefined) spdParts.push(`预计剩 ${fmtSec(prog.eta_seconds)}`);
    else if (st.wall_seconds) spdParts.push(`耗时 ${fmtSec(st.wall_seconds)}`);
    $('pipeSpeedEta').textContent = spdParts.join(' · ');
  }

  // ① 原图纠偏
  const orientPct = pctOf(orientDone, pagesTotal);
  const orientState = pageFail >= pagesTotal ? 'err' : (running && orientDone < pagesTotal ? 'active' : (orientDone > 0 ? (pageFail > 0 ? 'warn' : 'done') : 'idle'));
  setArchNodeState('nodeOrient', 'dotOrient', orientState);
  $('anOrientVal').textContent = `${orientDone} / ${pagesTotal} 页`;
  $('anOrientPct').textContent = orientPct + '%';
  $('anOrientBar').style.width = orientPct + '%';
  $('anOrientFoot').textContent = pageFail > 0 ? `失败 ${pageFail} 页` : (running && orientDone < pagesTotal ? '读图拉正中' : (orientDone > 0 ? '全部拉正' : '等待中'));

  // ② M1 版面
  const m1Pct = pctOf(m1Pages, pagesTotal);
  const m1State = running && orientDone > 0 && m1Pages < pagesTotal ? 'active' : (m1Pages > 0 ? 'done' : 'idle');
  setArchNodeState('nodeM1', 'dotM1', m1State);
  $('tagM1Dev').textContent = rawDev.toLowerCase().includes('gpu') || rawDev.toLowerCase().includes('cuda') ? 'GPU' : 'YOLO';
  $('anM1Val').textContent = `${m1Pages} / ${pagesTotal} 页`;
  $('anM1Pct').textContent = m1Pct + '%';
  $('anM1Bar').style.width = m1Pct + '%';
  $('anM1Choice').textContent = `选 ${m1Choice}`;
  $('anM1Fill').textContent = `填 ${m1Fill}`;
  $('anM1Id').textContent = `名 ${m1Identity}`;

  // ③ M2 选择题（全局进度锚定到 M1 已扫描页占比，避免前期局部飙高）
  const m2YoloPct = pctOf(m2ZonesYolo, Math.max(1, m1Choice));
  const m2DonePct = pctOf(m2ZonesDone, Math.max(1, m1Choice));
  const m2Scale = (running && m1Pages < pagesTotal) ? m1Frac : 1;
  const m2GlobalYoloPct = m1Choice > 0 ? Math.round(m2YoloPct * m2Scale) : 0;
  const m2GlobalDonePct = m1Choice > 0 ? Math.round(m2DonePct * m2Scale) : 0;
  const m2State = running && m1Choice > 0 && (m2ZonesDone < m1Choice || m1Pages < pagesTotal) ? 'active' : (m2ZonesDone > 0 || choiceItems > 0 ? 'done' : (m1Choice > 0 ? 'active' : 'idle'));
  setArchNodeState('nodeM2', 'dotM2', m2State);
  $('anM2InTag').textContent = `收 ${m1Choice} 区`;
  $('anM2YoloVal').textContent = `${m2ZonesYolo}/${m1Choice}`;
  $('anM2YoloBar').style.width = m2GlobalYoloPct + '%';
  $('anM2OcrVal').textContent = `${m2Ocr} 号`;
  $('anM2OcrBar').style.width = m2GlobalDonePct + '%';
  $('anM2CnnVal').textContent = `${choiceItems} 题`;
  $('anM2CnnBar').style.width = m2GlobalDonePct + '%';
  $('anM2Pct').textContent = m2GlobalDonePct + '%';

  // ④ M3 大题定位（同样锚定全局占比）
  const m3YoloPct = pctOf(m3ZonesYolo, Math.max(1, m1Fill));
  const m3DonePct = pctOf(m3ZonesDone, Math.max(1, m1Fill));
  const m3OcrPct = fillItems > 0 ? pctOf(m3Ocr, fillItems) : m3DonePct;
  const m3Scale = (running && m1Pages < pagesTotal) ? m1Frac : 1;
  const m3GlobalYoloPct = m1Fill > 0 ? Math.round(m3YoloPct * m3Scale) : 0;
  const m3GlobalOcrPct = m1Fill > 0 ? Math.round(m3OcrPct * m3Scale) : 0;
  const m3GlobalDonePct = m1Fill > 0 ? Math.round(m3DonePct * m3Scale) : 0;
  const m3State = running && m1Fill > 0 && (m3ZonesDone < m1Fill || m1Pages < pagesTotal) ? 'active' : (m3ZonesDone > 0 || fillItems > 0 ? 'done' : (m1Fill > 0 ? 'active' : 'idle'));
  setArchNodeState('nodeM3', 'dotM3', m3State);
  $('anM3InTag').textContent = `收 ${m1Fill} 区`;
  $('anM3YoloVal').textContent = `${m3ZonesYolo}/${m1Fill} · ${fillItems}题`;
  $('anM3YoloBar').style.width = m3GlobalYoloPct + '%';
  $('anM3OcrVal').textContent = `${m3Ocr}/${fillItems}`;
  $('anM3OcrBar').style.width = m3GlobalOcrPct + '%';
  $('anM3Foot').textContent = `下游 ${tilesQueued} 块`;
  $('anM3Pct').textContent = m3GlobalDonePct + '%';

  // ⑤ 拼图 & 大模型 API（流式跑批前期按已扫描页占比折算为全批进度）
  const apiPct = gridsTotal > 0 ? pctOf(apiSuccess, gridsTotal) : 0;
  const apiGlobalPct = (running && job.kind !== 'retry' && pagesDone < pagesTotal)
    ? Math.round(apiPct * detFrac)
    : apiPct;
  const apiState = (isErrState || (!running && apiFail > 0 && apiSuccess === 0))
    ? 'err'
    : (apiFail > 0 ? (running ? 'warn' : 'err') : (running && (apiInflight > 0 || gridsTotal > apiSuccess || pagesDone < pagesTotal) ? 'active' : (apiSuccess > 0 ? 'done' : 'idle')));
  setArchNodeState('nodeApi', 'dotApi', apiState);
  $('stApiTag').textContent = running && apiInflight > 0 ? `${apiInflight} 并发中` : `${prog.concurrency || st.concurrency || 4} 并发`;
  $('anApiIn').textContent = `${tilesQueued}块→${gridsTotal}图`;
  $('anApiVal').textContent = `${apiSuccess}/${gridsTotal}` + (apiFail > 0 ? ` (错${apiFail})` : '');
  $('anApiVal').className = 'an-m-val' + (apiFail > 0 ? ' err' : (apiSuccess > 0 && apiSuccess >= gridsTotal ? ' ok' : ''));
  $('anApiBar').style.width = apiGlobalPct + '%';
  $('anApiBarWrap').className = 'an-bar' + (apiFail > 0 ? ' err' : '');
  const apiCostSuffix = (costInfo && costInfo.found && costInfo.total_tokens > 0) ? ` · ${costInfo.cost_display}` : '';
  $('anApiFoot').textContent = (apiInflight > 0 ? `在途 ${apiInflight} · 已转 ${transCnt}` : `已转 ${transCnt} 处`) + apiCostSuffix;
  $('anApiPct').textContent = apiGlobalPct + '%';

  // ⑥ 实时落盘（严格按全批总页数 × 已扫描页转录比例计算，绝不超过已落盘页占比）
  const transDenom = Math.max(transCnt, tilesQueued);
  const transLocalFrac = transDenom > 0 ? Math.min(1, transCnt / transDenom) : 1;
  const outPct = Math.round(detFrac * transLocalFrac * 100);
  const outState = isErrState ? 'err' : (isWarnState ? 'warn' : (running ? 'active' : (pagesDone > 0 ? 'done' : 'idle')));
  setArchNodeState('nodeOut', 'dotOut', outState);
  $('tagOutState').textContent = running ? '写入中' : (isErrState ? '失败' : (isWarnState ? '待补跑' : '已落盘'));
  $('anOutIn').textContent = `${pagesDone} / ${pagesTotal} 页`;
  $('anOutPct').textContent = outPct + '%';
  $('anOutBar').style.width = outPct + '%';
  $('anOutBarWrap').className = 'an-bar' + (isErrState ? ' err' : (isWarnState ? ' amber' : ''));
  $('anOutSub').textContent = `选 ${choiceItems} · 手写 ${transCnt}/${transDenom}`;

  // ⑦ 人工复审（可点击直接开 8904）
  const revPct = reviewTotal > 0 ? pctOf(reviewedPages, reviewTotal) : 0;
  const revReady = Boolean(reviewTask.available || pagesDone > 0);
  const revState = !revReady ? 'idle' : (reviewedPages >= reviewTotal && reviewTotal > 0 ? 'done' : (reviewTask.running ? 'active' : 'done'));
  setArchNodeState('nodeReview', 'dotReview', revState, 'clickable');
  $('tagReviewState').textContent = reviewTask.running ? '审阅中' : '8904';
  $('anReviewVal').textContent = `已审 ${reviewedPages} / ${reviewTotal} 页`;
  $('anReviewPct').textContent = revPct + '%';
  $('anReviewBar').style.width = revPct + '%';
  $('anReviewFoot').textContent = !revReady ? '等待落盘' : (reviewedPages >= reviewTotal && reviewTotal > 0 ? '全部审完' : '点击核对');

  // 更新 7 条管道状态与实数标签 + 触发真实增量粒子脉冲
  requestAnimationFrame(() => {
    PipeFlow.relayout();
    PipeFlow.applyEdgeVisual('orient_m1', {
      state: orientDone > 0 ? (running && m1Pages < pagesTotal ? 'active' : 'done') : 'idle',
      flowing: running && orientDone > m1Pages,
      label: orientDone > 0 ? `${orientDone}页` : '',
    });
    PipeFlow.applyEdgeVisual('m1_m2', {
      state: m1Choice > 0 ? (running && m2ZonesDone < m1Choice ? 'active' : 'done') : 'idle',
      flowing: running && m1Choice > m2ZonesDone,
      label: m1Choice > 0 ? `${m1Choice}区` : '',
    });
    PipeFlow.applyEdgeVisual('m1_m3', {
      state: m1Fill > 0 ? (running && m3ZonesDone < m1Fill ? 'active' : 'done') : 'idle',
      flowing: running && m1Fill > m3ZonesDone,
      label: m1Fill > 0 ? `${m1Fill}区` : '',
    });
    PipeFlow.applyEdgeVisual('m2_out', {
      state: choiceItems > 0 ? (running && m2ZonesDone < m1Choice ? 'active' : 'done') : 'idle',
      flowing: running && m2ZonesDone > 0 && m2ZonesDone < m1Choice,
      label: choiceItems > 0 ? `${choiceItems}题` : '',
    });
    PipeFlow.applyEdgeVisual('m3_api', {
      state: tilesQueued > 0 ? (running && pagesDone < pagesTotal ? 'active' : 'done') : 'idle',
      flowing: running && tilesQueued > 0 && apiInflight > 0,
      label: tilesQueued > 0 ? `${tilesQueued}块` : '',
    });
    PipeFlow.applyEdgeVisual('api_out', {
      state: apiFail > 0 ? 'err' : (running && (apiInflight > 0 || apiSuccess < gridsTotal) && gridsTotal > 0 ? 'active' : (apiSuccess > 0 ? 'done' : 'idle')),
      flowing: running && apiInflight > 0,
      label: apiFail > 0 ? `错${apiFail}` : (gridsTotal > 0 ? `${apiSuccess}/${gridsTotal}` : ''),
    });
    PipeFlow.applyEdgeVisual('out_review', {
      state: revReady ? (reviewTask.running ? 'active' : 'done') : 'idle',
      flowing: running && pagesDone > 0 || Boolean(reviewTask.running && reviewedPages < reviewTotal),
      label: revReady ? `${reviewedPages}/${reviewTotal}页` : '',
    });

    PipeFlow.ingestSnapshot(ab?.batch_id || '', {
      orientDone, m1Pages, m1Choice, m1Fill,
      choiceItems, tilesQueued, apiSuccess, transCnt,
      pagesDone, reviewedPages,
    }, running);
  });

  // 中文诊断卡片
  const diagBox = $('pipelineDiagBox');
  const errRep = job.error_report || null;
  const exitInfo = job.exit_info || null;
  const showDiag = Boolean(errRep || (job.returncode !== null && job.returncode !== undefined && job.returncode !== 0) || job.status === 'error' || (job.status === 'warn' && hasFailed));
  diagBox.classList.toggle('hidden', !showDiag);
  if (showDiag) {
    const isWarnOnly = job.status === 'warn' && !errRep && (job.returncode === 0 || job.returncode == null);
    diagBox.className = 'diag-box' + (isWarnOnly ? ' warn' : '');
    const codeTag = (job.returncode !== null && job.returncode !== undefined)
      ? `退出码 ${job.returncode} · ${exitInfo?.title || ''}`
      : (isWarnOnly ? '待补跑提醒' : '错误诊断');
    const modName = errRep?.module || (isWarnOnly ? '视觉大模型转录' : '自动识别');
    const title = errRep?.title || exitInfo?.title || (isWarnOnly ? '部分图块未完成转录' : '运行异常');
    const reason = errRep?.reason || exitInfo?.reason || (
      pageFail > 0
        ? `有 ${pageFail}/${pagesTotal} 页本地检测失败（其余成功页与已转录结果均已保留）。`
        : `还有 ${rs?.failed_items || apiFail} 处待补跑（已成功项均保留）。`
    );
    const fix = errRep?.fix || exitInfo?.fix || (isWarnOnly ? '点击下方或右上角「补跑失败」，仅补跑失败页/失败题块，不重复扣费。' : '请前往「运行环境」检查修复。');
    diagBox.innerHTML = `
      <div class="diag-head">
        <span class="diag-title h-ico">${uiIcon('alert')}<span>${esc(title)}</span></span>
        <span class="diag-code">${esc(codeTag)}</span>
      </div>
      <div class="diag-row"><b>模块：</b>${esc(modName)}</div>
      <div class="diag-row"><b>原因：</b>${esc(reason)}</div>
      <div class="diag-row"><b>建议：</b>${esc(fix)}</div>
      <div class="row" style="margin-top:8px">
        ${hasFailed && !anyJobRunning ? `<button class="btn warn xs" type="button" id="btnDiagRetryNow">${uiIcon('refresh', 'ui-ico-xs')}<span>立即补跑失败</span></button>` : ''}
        <button class="btn xs" type="button" id="btnDiagGoCheck">检查环境</button>
        <button class="btn xs" type="button" id="btnDiagOpenVlm">配置 API</button>
        ${rs?.has_error_log ? '<button class="btn xs" type="button" id="btnDiagViewLog">错误日志</button>' : ''}
      </div>
    `;
    const rBtn = $('btnDiagRetryNow');
    if (rBtn) rBtn.onclick = () => $('btnRetryFailed').click();
    const gBtn = $('btnDiagGoCheck');
    if (gBtn) gBtn.onclick = e => { setView('check', { animate: true, originPt: clickOriginFromEvent(e, gBtn) }); $('btnRecheck').click(); };
    const vBtn = $('btnDiagOpenVlm');
    if (vBtn) vBtn.onclick = () => openVlmDialog();
    const lBtn = $('btnDiagViewLog');
    if (lBtn) lBtn.onclick = async () => {
      try {
        const res = await api('/api/pipeline/errors');
        $('pipelineRawDetails').open = true;
        $('pipelineLogBox').textContent = res.content || '错误日志为空';
      } catch (e) { alert(e.message); }
    };
  }

  if (job.logs && job.logs.length) {
    $('pipelineLogBox').textContent = job.logs.slice(-60).join('\n');
    $('pipelineLogBox').scrollTop = $('pipelineLogBox').scrollHeight;
  }
}

function renderSubjects(list) {
  const subjects = list && list.length ? list : ['物理','数学','化学','生物','地理','其他'];
  if (!subjects.includes(SELECTED_SUBJECT)) SELECTED_SUBJECT = subjects[0];
  $('subjectPills').innerHTML = subjects.map(s => `
    <button type="button" class="subj ${s === SELECTED_SUBJECT ? 'active' : ''}" data-subj="${esc(s)}">${esc(s)}</button>
  `).join('');
  $('subjectPills').querySelectorAll('.subj').forEach(btn => {
    btn.onclick = () => {
      SELECTED_SUBJECT = btn.dataset.subj;
      localStorage.setItem('wbLastSubject', SELECTED_SUBJECT);
      renderSubjects(subjects);
    };
  });
}

function updateEnvStatusPill(rep) {
  const pill = $('btnEnvPill');
  const txt = $('envPillText');
  if (!pill || !txt) return;
  if (!rep) {
    pill.className = 'env-status-pill ok';
    txt.textContent = '环境就绪';
    return;
  }
  if (!rep.ok) {
    pill.className = 'env-status-pill err';
    txt.textContent = `环境缺 ${rep.error_count || 1} 项`;
  } else if (rep.warning_count > 0) {
    pill.className = 'env-status-pill warn';
    txt.textContent = `环境就绪 (${rep.warning_count}提醒)`;
  } else {
    pill.className = 'env-status-pill ok';
    txt.textContent = '环境就绪';
  }
  pill.classList.toggle('hidden', !OVERVIEW?.is_host || WORK_MODE !== 'grading');
}

function renderOverview(data) {
  OVERVIEW = data;
  const isHost = !!data.is_host;
  const workMode = data.work_mode === 'iteration' ? 'iteration' : 'grading';
  WORK_MODE = workMode;

  // 主机不重复展示「主机」双徽章；协作者展示蓝色协作者标签
  $('rolePill').className = 'role-pill ' + (isHost ? 'host hidden' : 'collab');
  $('rolePill').innerHTML = '<i class="dot"></i><span>协作者</span>';
  $('topSub').textContent = isHost
    ? (workMode === 'grading' ? '改试卷模式 · 暖色轻量批改' : '模型迭代模式 · 冷色训练调优')
    : `已连主机 ${window.location.host}`;

  $('workModePill').classList.toggle('hidden', !isHost);
  applyWorkModeUI(workMode, false);

  $('hostStepper').classList.toggle('hidden', !isHost);
  if ($('btnLanShare')) $('btnLanShare').classList.toggle('hidden', !isHost);
  if ($('btnOpenMemDialog')) $('btnOpenMemDialog').classList.toggle('hidden', !isHost);
  $('hostBatchActions').classList.toggle('hidden', !isHost);
  $('btnOpenAi').classList.toggle('hidden', !isHost);
  $('btnOpenVlm').classList.toggle('hidden', !isHost);
  if ($('lanUrlText')) $('lanUrlText').textContent = data.lan_url;
  if (isHost && data.last_check) updateEnvStatusPill(data.last_check);
  else if (isHost) updateEnvStatusPill(CHECK);

  if (isHost) {
    renderSchemaStrip(data);
    renderMemCropStrip(data);
    if (data.training_db) renderTrainingDbCard(data.training_db);
  }

  if (isHost && data.config?.opencode_path) {
    $('inputOpencode').value = data.config.opencode_path;
  }

  const ab = data.active_batch;
  const rs = data.recognition_summary;
  if (ab) {
    const showSubj = ab.subject && ab.subject !== '通用' && ab.subject !== '试卷';
    $('activeBatchTitle').textContent = showSubj
      ? `${ab.batch_id}（${ab.subject} · ${ab.total_images} 张）`
      : `${ab.batch_id}（${ab.total_images} 张）`;
    const recText = rs?.status || (ab.has_recognition ? '已识别' : '待识别');
    $('activeBatchMeta').textContent = `${ab.created_at} · ${recText}`;
  } else {
    $('activeBatchTitle').textContent = '尚未导入试卷';
    $('activeBatchMeta').textContent = isHost ? '点右侧「+ 新试卷」或切到第 1 步贴入试卷' : '等待主机导入试卷';
  }

  const sel = $('batchSelect');
  sel.innerHTML = (data.batches || []).map(b =>
    `<option value="${esc(b.batch_id)}" ${ab && b.batch_id === ab.batch_id ? 'selected' : ''}>${esc(b.batch_id)} (${b.total_images}张)</option>`
  ).join('') || '<option value="">暂无批次</option>';

  const job = data.pipeline_job || {};
  const recBtn = $('btnStartRecognize');
  recBtn.disabled = !!job.running || !ab;
  recBtn.textContent = job.running ? '识别中...' : (ab?.has_recognition ? '重新识别' : '开始识别');
  // 已有识别结果时将「重新识别」降为次级按钮，把视觉主 CTA 焦点留给下方「进去审 →」
  recBtn.className = (!ab?.has_recognition && !job.running) ? 'btn primary sm' : 'btn sm';

  renderPipelineDashboard(data);
  if (data.agents) syncAgentsUI(data.agents, data.config);
  if (Array.isArray(data.ai_sessions)) syncAiSessions(data.ai_sessions);
  scheduleReportUiState();
  if (isHost) {
    syncPerfPanel(data.hardware, data.infer_settings);
    if (data.benchmark_job) renderBenchmarkJob(data.benchmark_job);
  }

  // 自适应轮询：识别中 1.5s，平时 4.5s
  scheduleOverviewPoll(!!job.running ? 1500 : 4500);

  const hostForLinks = window.location.hostname || '127.0.0.1';
  const renderCard = t => {
    const pct = t.total > 0 ? Math.round((t.done / t.total) * 100) : 0;
    const statusTag = t.running ? '<span class="badge ok">运行中</span>' : '';
    return `
      <div class="task-card ${t.running ? 'running' : ''}" id="task_${esc(t.id)}">
        <div class="between">
          <h3>${esc(t.name)}</h3>
          ${statusTag}
        </div>
        <div class="task-desc">${esc(t.desc)}</div>
        <div class="prog-nums">
          <span>${t.done} / ${t.total} ${esc(t.unit)} (${pct}%)</span>
          <span class="muted">剩 ${t.remaining}</span>
        </div>
        <div class="prog-bar"><i style="width:${pct}%"></i></div>
        <div class="between" style="margin-top:auto;padding-top:4px">
          <span class="muted" style="font-size:11.5px">${esc(t.extra)}</span>
          <div class="row">
            ${isHost && t.running ? `<button class="btn xs ghost" data-stop-task="${esc(t.id)}" type="button">停止</button>` : ''}
            <button class="btn primary sm" data-open-task="${esc(t.id)}" ${t.available ? '' : 'disabled'} type="button">进去审 →</button>
          </div>
        </div>
      </div>
    `;
  };
  const primaryTasks = (data.tasks || []).filter(t => t.tier !== 'secondary');
  const secondaryTasks = (data.tasks || []).filter(t => t.tier === 'secondary');
  $('taskGrid').innerHTML = primaryTasks.map(renderCard).join('');
  // 改试卷模式下彻底隐藏训练专用次级工具（8900 标框 / 8905 涂卡审核）
  $('secondaryWrap').classList.toggle('hidden', !isHost || workMode !== 'iteration' || !secondaryTasks.length);
  $('secondaryTaskGrid').innerHTML = secondaryTasks.map(renderCard).join('');

  document.querySelectorAll('[data-open-task]').forEach(btn => {
    btn.onclick = () => {
      if (btn.dataset.openTask === 'review') PipeFlow.triggerReviewPulse('开始审阅');
      openTask(btn.dataset.openTask, hostForLinks);
    };
  });
  document.querySelectorAll('[data-stop-task]').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api('/api/service/stop', { method: 'POST', body: JSON.stringify({ task_id: btn.dataset.stopTask }) });
        await refreshOverview();
      } catch (e) { alert(e.message); }
    };
  });

  const online = data.online_users || [];
  $('onlineList').innerHTML = online.length
    ? online.map(u => `<span class="online-chip"><i class="dot"></i>${esc(u.name)}</span>`).join('')
    : '<span class="muted">仅你在线</span>';

  const lb = data.leaderboard || [];
  $('leaderboard').innerHTML = lb.length
    ? lb.map((p, idx) => `
        <div class="person-row">
          <span><strong>${idx + 1}. ${esc(p.name)}</strong></span>
          <span style="color:var(--primary);font-weight:700">${p.count} 页</span>
        </div>
      `).join('')
    : '<div class="muted">暂无审阅记录</div>';

  renderSubjects(data.subjects);

  if (!INITIALIZED) {
    INITIALIZED = true;
    if (!isHost) {
      setView('overview');
    } else if (data.last_check && !data.last_check.ok) {
      renderCheck(data.last_check);
      setView('check');
    } else {
      if (data.last_check) renderCheck(data.last_check);
      setView(ab ? 'overview' : 'ingest');
    }
    if (!getUserName() && !IS_MIRROR_MODE) {
      $('nameInput').value = isHost ? '主机' : '';
      $('nameDialog').showModal();
    }
  }
}

async function openTask(taskId, hostForLinks, extra = {}) {
  showBanner('gui', '正在打开审核页...', 5000);
  const pendingWin = window.open('about:blank', '_blank');
  if (pendingWin && pendingWin.document) {
    try {
      pendingWin.document.title = '正在打开...';
      pendingWin.document.body.innerHTML = '<div style="font-family:sans-serif;padding:40px;color:#475569;text-align:center;">正在准备审核页面...</div>';
    } catch (_) {}
  }
  try {
    const res = await api('/api/service/ensure', { method: 'POST', body: JSON.stringify({ task_id: taskId, ...extra }) });
    const who = getUserName();
    const qs = who ? ('?reviewer=' + encodeURIComponent(who)) : '';
    const url = `http://${hostForLinks}:${res.port}/${qs}`;
    pingPresence(taskId);
    if (pendingWin && !pendingWin.closed) pendingWin.location.href = url;
    else window.location.href = url;
    await refreshOverview();
  } catch (err) {
    if (pendingWin && !pendingWin.closed) pendingWin.close();
    alert(err.message);
  }
}

async function refreshOverview() {
  if (OVERVIEW_INFLIGHT) return;
  OVERVIEW_INFLIGHT = true;
  try {
    const data = await api('/api/overview');
    renderOverview(data);
  } catch (err) {
    $('topSub').textContent = '连接失败：' + err.message;
  } finally {
    OVERVIEW_INFLIGHT = false;
  }
}

function scheduleOverviewPoll(ms) {
  if (OVERVIEW_TIMER && OVERVIEW_TIMER_MS === ms) return;
  if (OVERVIEW_TIMER) clearInterval(OVERVIEW_TIMER);
  OVERVIEW_TIMER_MS = ms;
  OVERVIEW_TIMER = setInterval(() => { if (!document.hidden) refreshOverview(); }, ms);
}

async function pingPresence(task = '总览') {
  try {
    await fetch('/api/presence', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: CLIENT_ID, name: getUserName() || '成员', task }),
    });
  } catch (_) {}
}

function connectSSE() {
  const es = new EventSource('/api/events');
  es.onmessage = e => {
    try {
      const ev = JSON.parse(e.data);
      const isLiveProgressTick = ev.step === 'recognize' && ev.status === 'running' && !!ev.progress;
      if (ev.message && !isLiveProgressTick) showBanner(ev.source || 'gui', ev.message, 4000);
      if (ev.step === 'check') highlightElement('tabCheck');
      else if (ev.step === 'ingest') highlightElement('tabIngest');
      else if (ev.step === 'recognize') {
        // 携带实时节点进度时直接秒级驱动架构图与真实数据脉冲，无需额外等 HTTP 轮询
        if (ev.progress && OVERVIEW) {
          OVERVIEW.pipeline_job = {
            ...(OVERVIEW.pipeline_job || {}),
            running: ev.status === 'running',
            status: ev.status || 'running',
            batch_id: ev.batch_id || OVERVIEW.active_batch?.batch_id || '',
            progress: ev.progress,
          };
          renderPipelineDashboard(OVERVIEW);
        } else {
          refreshOverview();
        }
      } else if (ev.step === 'train') {
        highlightElement('tabTrain');
        if (CURRENT_VIEW === 'train' || TRAIN_DATA) refreshTrainOverview();
      } else if (ev.step?.startsWith('service_')) {
        highlightElement('task_' + ev.step.replace('service_', ''));
        refreshOverview();
      } else if (ev.step === 'ai' && ev.ai_snapshot) {
        if (OVERVIEW) OVERVIEW.ai_job = ev.ai_snapshot;
        upsertAiSessionSnapshot(ev.ai_snapshot);
      } else if (ev.step === 'ui_command') {
        handleUiCommand(ev);
      } else if (ev.step === 'overview') {
        refreshOverview();
      }
    } catch (_) {}
  };
}

/* ==================== 事件绑定 ==================== */
if ($('btnUserLabel')) $('btnUserLabel').textContent = getUserName() || '主机';
$('btnUser').onclick = () => { $('nameInput').value = getUserName(); $('nameDialog').showModal(); };
$('btnSaveName').onclick = () => { const v = $('nameInput').value.trim(); if (!v) return; setUserName(v); $('nameDialog').close(); };
$('nameInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('btnSaveName').click(); });

if ($('btnEnvPill')) {
  $('btnEnvPill').onclick = async e => {
    setView('check', { animate: true, originPt: clickOriginFromEvent(e, $('btnEnvPill')) });
    if (!CHECK) {
      try { renderCheck(await api('/api/check')); } catch (err) { alert(err.message); }
    }
  };
}
if ($('btnBackFromCheck')) {
  $('btnBackFromCheck').onclick = e => setView(OVERVIEW?.active_batch ? 'overview' : 'ingest', { animate: true, originPt: clickOriginFromEvent(e, $('btnBackFromCheck')) });
}
if ($('btnLanShare')) $('btnLanShare').onclick = () => $('lanDialog')?.showModal();
if ($('btnCloseLanDlg')) $('btnCloseLanDlg').onclick = () => $('lanDialog')?.close();
if ($('btnOpenMemDialog')) {
  $('btnOpenMemDialog').onclick = () => {
    $('memPreflightDialog')?.showModal();
    if (OVERVIEW?.active_batch && !OVERVIEW.last_memory_plan) runMemoryPreflight(false);
  };
}
if ($('btnCloseMemDlg')) $('btnCloseMemDlg').onclick = () => $('memPreflightDialog')?.close();
if ($('btnCloseSchemaRuleDlg')) $('btnCloseSchemaRuleDlg').onclick = () => $('schemaRuleDialog')?.close();

document.querySelectorAll('.step-tab').forEach(btn => {
  btn.onclick = async e => {
    const targetView = btn.dataset.view;
    setView(targetView, { animate: true, originPt: clickOriginFromEvent(e, btn) });
    if (targetView === 'check' && !CHECK) {
      try { renderCheck(await api('/api/check')); } catch (err) { alert(err.message); }
    } else if (targetView === 'train') {
      await refreshTrainOverview();
    }
  };
});

// Step 1 同构体检 DAG 节点交互直达
const _openWeightOrDetails = () => {
  if (CHECK && CHECK.missing_weights?.length && CHECK.has_encrypted_weights) {
    $('btnUnpackWeights').click();
  } else {
    const d = $('drawerCheckDetails');
    d.open = !d.open;
    if (d.open) d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
};
['envNodeOrient', 'envNodeM1', 'envNodeM2', 'envNodeM3'].forEach(id => {
  const el = $(id);
  if (el) el.onclick = _openWeightOrDetails;
});
$('envNodeVlm').onclick = () => openVlmDialog();
$('envNodeReview').onclick = e => setView(OVERVIEW?.active_batch ? 'overview' : 'ingest', { animate: true, originPt: clickOriginFromEvent(e, $('envNodeReview')) });

// Step 3 架构图「⑦ 人工复审」节点点击直达 8904 + 真实数据脉冲
$('nodeReview').onclick = () => {
  const revTask = (OVERVIEW?.tasks || []).find(t => t.id === 'review');
  if (!revTask?.available) { showBanner('gui', '请先完成自动识别落盘后再进入审阅', 3000); return; }
  PipeFlow.triggerReviewPulse('开始审阅');
  openTask('review', window.location.hostname || '127.0.0.1');
};

$('btnToggleArch').onclick = () => {
  ARCH_COLLAPSED = !ARCH_COLLAPSED;
  ARCH_USER_EXPANDED = !ARCH_COLLAPSED;
  applyArchCollapseUI();
};

document.querySelectorAll('[data-open-annotate]').forEach(btn => {
  btn.onclick = () => openTask('annotate', window.location.hostname || '127.0.0.1', { m: btn.dataset.openAnnotate });
});
$('btnOpenChoiceFromTrain').onclick = () => openTask('choice', window.location.hostname || '127.0.0.1');

$('btnRecheck').onclick = async () => {
  $('btnRecheck').disabled = true;
  showBanner('gui', '正在深度检查环境与模型...', 8000);
  try { renderCheck(await api('/api/check?deep=1')); }
  catch (e) { alert(e.message); }
  finally { $('btnRecheck').disabled = false; }
};

$('btnSavePerf').onclick = async () => {
  $('btnSavePerf').disabled = true;
  try {
    await api('/api/config', {
      method: 'POST',
      body: JSON.stringify({
        infer_device: $('selInferDevice').value,
        yolo_batch: Number($('selYoloBatch').value) || 0,
        cpu_workers: Number($('selCpuWorkers').value) || 0,
        api_concurrency: Number($('selApiConc').value) || 4,
      }),
    });
    showBanner('gui', '已保存加速配置', 3000);
    if (CHECK) renderCheck(await api('/api/check'));
  } catch (err) { alert('保存失败：' + err.message); }
  finally { $('btnSavePerf').disabled = false; }
};

let BENCH_TIMER = null;
async function pollBenchmarkStatus() {
  try {
    const res = await api('/api/benchmark/status');
    renderBenchmarkJob(res.benchmark_job);
    if (!res.benchmark_job?.running && BENCH_TIMER) {
      clearInterval(BENCH_TIMER);
      BENCH_TIMER = null;
    }
  } catch (_) {}
}
$('btnRunBenchmark').onclick = async () => {
  $('btnRunBenchmark').disabled = true;
  try {
    await api('/api/benchmark/start', { method: 'POST', body: JSON.stringify({ samples: 4 }) });
    await pollBenchmarkStatus();
    if (BENCH_TIMER) clearInterval(BENCH_TIMER);
    BENCH_TIMER = setInterval(pollBenchmarkStatus, 2000);
  } catch (err) {
    alert(err.message);
    $('btnRunBenchmark').disabled = false;
  }
};
$('btnApplyBench').onclick = async () => {
  if (!LAST_BENCH_REC) return;
  try {
    await api('/api/config', {
      method: 'POST',
      body: JSON.stringify({
        infer_device: LAST_BENCH_REC.infer_device || 'auto',
        yolo_batch: Number(LAST_BENCH_REC.yolo_batch) || 0,
        cpu_workers: Number(LAST_BENCH_REC.cpu_workers) || 0,
        api_concurrency: Number(LAST_BENCH_REC.api_concurrency) || 4,
        last_benchmark: LAST_BENCH_REC,
      }),
    });
    showBanner('gui', `已应用最优配置：${LAST_BENCH_REC.label}`, 4000);
    if (CHECK) renderCheck(await api('/api/check'));
  } catch (err) { alert('应用失败：' + err.message); }
};

$('btnToIngest').onclick = e => setView(OVERVIEW?.active_batch ? 'overview' : 'ingest', { animate: true, originPt: clickOriginFromEvent(e, $('btnToIngest')) });
$('btnSkipIngest').onclick = e => setView('overview', { animate: true, originPt: clickOriginFromEvent(e, $('btnSkipIngest')) });
$('btnGoIngestNew').onclick = e => setView('ingest', { animate: true, originPt: clickOriginFromEvent(e, $('btnGoIngestNew')) });
$('btnRefreshOverview').onclick = refreshOverview;

$('btnRepairPip').onclick = async () => {
  $('btnRepairPip').disabled = true;
  showBanner('gui', '正在测速选源安装缺失依赖...', 15000);
  try {
    const res = await api('/api/repair/pip', { method: 'POST', body: '{}' });
    alert(res.ok ? `安装完成（${res.mirror?.name || ''}）` : '自动安装失败，建议点「求助 AI」');
    renderCheck(await api('/api/check'));
  } catch (err) { alert(err.message); }
  finally { $('btnRepairPip').disabled = false; }
};
$('btnUnpackWeights').onclick = () => { $('pwdErr').textContent = ''; $('pwdInput').value = ''; $('pwdDialog').showModal(); };
$('btnCancelPwd').onclick = () => $('pwdDialog').close();
$('btnConfirmPwd').onclick = async () => {
  $('btnConfirmPwd').disabled = true;
  $('pwdErr').textContent = '';
  try {
    const res = await api('/api/repair/weights', { method: 'POST', body: JSON.stringify({ password: $('pwdInput').value }) });
    $('pwdDialog').close();
    showBanner('gui', res.message || '解压完成', 4000);
    renderCheck(await api('/api/check'));
  } catch (err) { $('pwdErr').textContent = err.message; }
  finally { $('btnConfirmPwd').disabled = false; }
};
