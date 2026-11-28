'use strict';
// 初始化
/* ==================== 双模式冷暖分色切换（Codex 胶囊 + 径向波纹动效）+ 答案弹窗 + 静默内存预算 ==================== */
let WORK_MODE = 'grading';
let LAST_PREFLIGHT_BATCH = '';
let CURRENT_SCHEMA_OBJ = null;

function playModeWave(mode, originPt) {
  const wave = $('modeWaveOverlay');
  if (!wave) return;
  const pill = $('workModePill');
  let wx = window.innerWidth * 0.5, wy = 36;
  if (originPt && Number.isFinite(originPt.x) && Number.isFinite(originPt.y)) {
    wx = originPt.x; wy = originPt.y;
  } else if (pill) {
    const r = pill.getBoundingClientRect();
    wx = r.left + r.width / 2;
    wy = r.top + r.height / 2;
  }
  wave.style.setProperty('--wx', Math.round(wx) + 'px');
  wave.style.setProperty('--wy', Math.round(wy) + 'px');
  wave.className = 'mode-wave ' + (mode === 'iteration' ? 'cool' : 'warm');
  void wave.offsetWidth;
  wave.classList.add('play');
  clearTimeout(wave._wt);
  wave._wt = setTimeout(() => wave.classList.remove('play'), 620);
}

function applyWorkModeUI(mode, animate = false, originPt = null) {
  const m = mode === 'iteration' ? 'iteration' : 'grading';
  const isGrading = m === 'grading';
  WORK_MODE = m;
  document.body.dataset.workMode = m;
  const pill = $('workModePill');
  if (pill) {
    pill.dataset.mode = m;
    pill.querySelectorAll('.mode-cap-btn').forEach(btn => {
      const on = btn.dataset.workMode === m;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }
  const stepper = $('hostStepper');
  if (stepper) stepper.classList.toggle('mode-grading', isGrading);
  $('tabCheck')?.classList.toggle('hidden', isGrading);
  $('tabTrain')?.classList.toggle('hidden', isGrading);
  if ($('stepNumIngest')) $('stepNumIngest').textContent = isGrading ? '1' : '2';
  if ($('stepNumOverview')) $('stepNumOverview').textContent = isGrading ? '2' : '3';
  if ($('tabIngestTitle')) $('tabIngestTitle').textContent = isGrading ? '贴入试卷与答案' : '导入试卷';
  if ($('tabIngestDesc')) $('tabIngestDesc').textContent = isGrading ? '拖放 / 贴路径 / Ctrl+V' : '选图入库与标注';
  if ($('tabOverviewTitle')) $('tabOverviewTitle').textContent = isGrading ? '改卷与导分' : '识别与审阅';
  if ($('tabOverviewDesc')) $('tabOverviewDesc').textContent = isGrading ? '进度 / 复审 / 成绩单' : '实时流向与协作';
  if ($('iterIngestMeta')) $('iterIngestMeta').classList.toggle('hidden', isGrading);
  if ($('btnIngestAndRun')) $('btnIngestAndRun').textContent = isGrading ? '▶ 开始改试卷' : '▶ 导入并开始识别';
  if ($('btnEnvPill')) $('btnEnvPill').classList.toggle('hidden', !OVERVIEW?.is_host || !isGrading);

  const secWrap = $('secondaryWrap');
  if (secWrap && OVERVIEW) {
    const hasSec = (OVERVIEW.tasks || []).some(t => t.tier === 'secondary');
    secWrap.classList.toggle('hidden', !OVERVIEW.is_host || isGrading || !hasSec);
  }
  if (isGrading && (CURRENT_VIEW === 'train' || CURRENT_VIEW === 'check')) {
    setView(OVERVIEW?.active_batch ? 'overview' : 'ingest');
  }
  if ($('btnBackFromCheck')) {
    $('btnBackFromCheck').classList.toggle('hidden', !(CURRENT_VIEW === 'check' && isGrading));
  }
  if ($('topSub') && OVERVIEW?.is_host) {
    $('topSub').textContent = isGrading ? '改试卷模式 · 暖色轻量批改' : '模型迭代模式 · 冷色训练调优';
  }
  if ($('chkMemoryCrops') && animate) {
    $('chkMemoryCrops').checked = isGrading;
  }
  if (animate) {
    playModeWave(m, originPt);
    document.body.classList.remove('mode-switching');
    void document.body.offsetWidth;
    document.body.classList.add('mode-switching');
    setTimeout(() => document.body.classList.remove('mode-switching'), 340);
  }
}

document.querySelectorAll('#workModePill .mode-cap-btn').forEach(btn => {
  btn.onclick = async e => {
    const nextMode = btn.dataset.workMode;
    if (!nextMode || nextMode === WORK_MODE) return;
    const originPt = { x: e.clientX, y: e.clientY };
    applyWorkModeUI(nextMode, true, originPt);
    try {
      const res = await api('/api/config', { method: 'POST', body: JSON.stringify({ work_mode: nextMode, memory_crops: nextMode === 'grading' }) });
      if (res.training_db) renderTrainingDbCard(res.training_db);
      showBanner('gui', nextMode === 'grading' ? '已切换到「改试卷模式」（暖色 · 极简两步改卷）' : '已切换到「模型迭代模式」（冷色 · 展开环境自检与训练库）');
      await refreshOverview();
    } catch (err) {
      showBanner('gui', '切换模式失败：' + err.message);
    }
  };
});

function renderSchemaStrip(data) {
  const ab = data.active_batch;
  const strip = $('schemaStrip');
  if (!strip) return;
  strip.classList.toggle('hidden', !ab);
  if (!ab) return;
  const s = data.schema_summary || {};
  const badge = $('schemaBadge');
  const meta = $('schemaMetaText');
  const csvBtn = $('btnExportGradesCsv');
  if (s.configured) {
    badge.className = 'badge ok';
    badge.textContent = `✓ 已配答案 · 满分 ${s.total_possible_score || 0}`;
    meta.textContent = `选择 ${s.choice_count || 0} 题 · 填空 ${s.fill_blank_count || 0} 空（多选漏选 ${Math.round((s.multi_choice_partial_ratio ?? 0.5) * 100)}%）`;
  } else {
    badge.className = 'badge warn';
    badge.textContent = '未配答案';
    meta.textContent = '仍可直接识别；把答案 PDF / 图片拖到这里即可自动判分';
  }
  if (csvBtn) csvBtn.classList.toggle('hidden', !ab.has_recognition);
  const ratio = Number(s.multi_choice_partial_ratio ?? 0.5);
  if ($('inputCustomRatio') && document.activeElement !== $('inputCustomRatio')) {
    $('inputCustomRatio').value = String(ratio);
  }
  document.querySelectorAll('#multiRatioPills .ratio-pill').forEach(p => {
    p.classList.toggle('active', Math.abs(Number(p.dataset.ratio) - ratio) < 0.02);
  });
}

async function handleAnswerSourceFile(file) {
  if (!file || !OVERVIEW?.active_batch) {
    alert('请先选择或导入一个试卷批次');
    return;
  }
  const bid = OVERVIEW.active_batch.batch_id;
  // 若直接拖入的是结构化 JSON，免解析直接生效并弹绿勾
  if (/\.json$/i.test(file.name || '')) {
    try {
      const parsed = JSON.parse(await file.text());
      await api('/api/schema', { method: 'POST', body: JSON.stringify({ batch_id: bid, schema: parsed }) });
      const st = $('schemaStrip');
      if (st) { st.classList.remove('confirm-pop'); void st.offsetWidth; st.classList.add('confirm-pop'); setTimeout(() => st.classList.remove('confirm-pop'), 650); }
      showBanner('gui', `🎉 已直接导入标准答案 JSON：${file.name}`, 4500);
      await refreshOverview();
      return;
    } catch (err) {
      alert('JSON 格式错误：' + err.message);
      return;
    }
  }
  showBanner('gui', `正在上传并提取答案文件：${file.name}...`, 10000);
  try {
    const qs = `?batch_id=${encodeURIComponent(bid)}&filename=${encodeURIComponent(file.name)}`;
    const r = await fetch('/api/schema/upload-source' + qs, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    });
    const res = await r.json().catch(() => ({}));
    if (!r.ok || !res.ok) throw new Error(res.error || '上传答案失败');
    const st = $('schemaStrip');
    if (st) { st.classList.remove('confirm-pop'); void st.offsetWidth; st.classList.add('confirm-pop'); setTimeout(() => st.classList.remove('confirm-pop'), 650); }
    showBanner('gui', `🎉 已放入 ${res.manifest?.page_count || 1} 页答案，正在交由 AI 帮手整理判分标准...`, 6000);

    // 默认直接唤醒可拖动 AI 帮手解析输出结构化 JSON；无本地 Agent 时打开判分规则弹窗走 VLM 兜底
    const hasAgent = Boolean(AGENTS_CACHE?.available?.length);
    if (hasAgent && res.ai_prompt) {
      openAiHelperDialog(true);
      $('aiNoteInput').value = res.ai_prompt;
      $('btnStartAi').click();
    } else {
      $('schemaRuleDialog')?.showModal();
      if (confirm('已提取答案文件，但本机暂未检测到可用 CLI Agent（如 ZCode / OpenCode）。\n是否立即使用「VLM 视觉大模型直连解析」生成结构化标准答案？')) {
        $('btnParseSchemaVlm').click();
      }
    }
  } catch (err) {
    alert('答案上传失败：' + err.message);
  }
}

$('btnUploadAnswerPdf').onclick = () => $('answerPdfInput').click();
$('answerPdfInput').onchange = e => {
  const f = e.target.files?.[0];
  e.target.value = '';
  if (f) handleAnswerSourceFile(f);
};
const _scStrip = $('schemaStrip');
if (_scStrip) {
  _scStrip.addEventListener('dragover', e => { e.preventDefault(); _scStrip.classList.add('dragover'); });
  _scStrip.addEventListener('dragleave', () => _scStrip.classList.remove('dragover'));
  _scStrip.addEventListener('drop', e => {
    e.preventDefault();
    _scStrip.classList.remove('dragover');
    const f = e.dataTransfer?.files?.[0];
    if (f) handleAnswerSourceFile(f);
  });
}
$('btnToggleSchemaAdv').onclick = () => $('schemaRuleDialog')?.showModal();
document.querySelectorAll('#multiRatioPills .ratio-pill').forEach(btn => {
  btn.onclick = () => {
    $('inputCustomRatio').value = btn.dataset.ratio;
    $('btnSaveMultiRatio').click();
  };
});
$('btnSaveMultiRatio').onclick = async () => {
  const ratio = Number($('inputCustomRatio').value);
  if (!Number.isFinite(ratio) || ratio < 0 || ratio > 1) { alert('漏选比例应在 0 到 1 之间'); return; }
  try {
    await api('/api/schema', { method: 'POST', body: JSON.stringify({ multi_choice_partial_ratio: ratio }) });
    showBanner('gui', `多选题漏选给分已更新为 ${Math.round(ratio * 100)}%`);
    await refreshOverview();
  } catch (err) { alert(err.message); }
};
$('btnAskAiEditSchema').onclick = () => {
  $('schemaRuleDialog')?.close();
  openAiHelperDialog(true);
  $('aiNoteInput').value = '请查看当前批次的 exam_schema 试卷答案与多选题评分规则，帮我修改为：';
  $('aiNoteInput').focus();
};
$('btnParseSchemaVlm').onclick = async () => {
  $('btnParseSchemaVlm').disabled = true;
  showBanner('gui', '正在调用视觉大模型直连解析答案，请稍候...', 20000);
  try {
    const res = await api('/api/schema/parse-vlm', { method: 'POST', body: '{}' });
    const s = res.summary || {};
    showBanner('gui', `解析完成：选择 ${s.choice_count || 0} 题 · 填空 ${s.fill_blank_count || 0} 空 · 满分 ${s.total_possible_score || 0}`);
    $('schemaRuleDialog')?.close();
    await refreshOverview();
  } catch (err) { alert(err.message); }
  finally { $('btnParseSchemaVlm').disabled = false; }
};
$('btnViewSchemaJson').onclick = async () => {
  const wrap = $('schemaJsonWrap');
  const willShow = wrap.classList.contains('hidden');
  wrap.classList.toggle('hidden', !willShow);
  if (willShow) {
    try {
      const res = await api('/api/schema');
      CURRENT_SCHEMA_OBJ = res.schema || { choices: {}, fills: {}, scoring: { multi_choice_partial_ratio: 0.5 } };
      $('schemaJsonEdit').value = JSON.stringify(CURRENT_SCHEMA_OBJ, null, 2);
    } catch (err) { alert(err.message); }
  }
};
$('btnSaveSchemaJson').onclick = async () => {
  try {
    const parsed = JSON.parse($('schemaJsonEdit').value);
    await api('/api/schema', { method: 'POST', body: JSON.stringify({ schema: parsed }) });
    showBanner('gui', '已保存结构化标准答案 JSON');
    $('schemaJsonWrap').classList.add('hidden');
    $('schemaRuleDialog')?.close();
    await refreshOverview();
  } catch (err) { alert('JSON 格式错误或保存失败：' + err.message); }
};
$('btnClearSchema').onclick = async () => {
  if (!confirm('确认清除当前批次标准答案并恢复通用识别模式？')) return;
  try {
    await api('/api/schema/clear', { method: 'POST', body: '{}' });
    showBanner('gui', '已清除标准答案');
    $('schemaRuleDialog')?.close();
    await refreshOverview();
  } catch (err) { alert(err.message); }
};
$('btnExportGradesCsv').onclick = () => {
  const bid = OVERVIEW?.active_batch?.batch_id || '';
  window.open('/api/grades.csv' + (bid ? `?batch_id=${encodeURIComponent(bid)}` : ''), '_blank');
};

function renderMemCropStrip(data) {
  const ab = data.active_batch;
  const memBtn = $('btnOpenMemDialog');
  if (!ab) {
    if (memBtn) memBtn.classList.add('hidden');
    return;
  }
  const memOn = Boolean(data.infer_settings?.memory_crops ?? (WORK_MODE === 'grading'));
  if ($('chkMemoryCrops')) $('chkMemoryCrops').checked = memOn;
  if (memBtn) {
    memBtn.classList.remove('hidden');
    memBtn.textContent = memOn ? '⚡ 内存暂存' : '💾 硬盘缓存';
  }
  const plan = data.last_memory_plan || data.pipeline_job?.memory_preflight || null;
  if (plan && (!plan.batch_id || plan.batch_id === ab.batch_id)) {
    const badge = plan.use_memory_crops ? '✅ 推荐内存暂存' : '⚠ 自动降级硬盘缓存';
    if ($('memPlanText')) {
      $('memPlanText').textContent = `${badge} · 可用 ${(plan.available_ram_mb / 1024).toFixed(1)} GB · 预计稳态峰值 ~${plan.est_peak_mb} MB · 在途窗口 ${plan.max_inflight_grids} 拼图 · 省写 ~${plan.est_saved_files} 小文件 (${plan.benchmark_ms}ms 实测)`;
    }
  } else if (!memOn && $('memPlanText')) {
    $('memPlanText').textContent = '已关闭内存暂存（保留 crops/*.png 与 grids/*.png 到硬盘，适合训练/排障）';
  }
}

async function runMemoryPreflight(silent = false) {
  const btn = $('btnRunMemPreflight');
  if (btn) btn.disabled = true;
  if (!silent && $('memPlanText')) $('memPlanText').textContent = '正在抽样实测本批次试卷内存占用与释放预算...';
  try {
    const plan = await api('/api/memory/preflight', { method: 'POST', body: '{}' });
    if (OVERVIEW) OVERVIEW.last_memory_plan = plan;
    renderMemCropStrip(OVERVIEW || { active_batch: { batch_id: plan.batch_id }, infer_settings: { memory_crops: $('chkMemoryCrops')?.checked }, last_memory_plan: plan });
    if (!silent) showBanner('gui', plan.reason || '内存预检完成', 5000);
  } catch (err) {
    if (!silent && $('memPlanText')) $('memPlanText').textContent = '预检失败：' + err.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}
$('btnRunMemPreflight').onclick = () => runMemoryPreflight(false);
$('chkMemoryCrops').onchange = async () => {
  const on = $('chkMemoryCrops').checked;
  try {
    await api('/api/config', { method: 'POST', body: JSON.stringify({ memory_crops: on }) });
    if (on) await runMemoryPreflight(false);
    else if ($('memPlanText')) $('memPlanText').textContent = '已切换为传统硬盘缓存（写入 crops/*.png 到磁盘）';
    if ($('btnOpenMemDialog')) $('btnOpenMemDialog').textContent = on ? '⚡ 内存暂存' : '💾 硬盘缓存';
  } catch (err) { alert(err.message); }
};

function renderTrainingDbCard(db) {
  if (!db || !$('trainDbPathText')) return;
  $('trainDbPathText').textContent = `${db.training_db_dir}${db.is_default ? ' （默认项目内 data/）' : ' （自定义路径）'}`;
  if ($('chkTrainDbEnabled')) $('chkTrainDbEnabled').checked = !!db.enabled;
  const c = db.counts || {};
  $('trainDbCounts').innerHTML = `
    <span class="db-stat-pill">M1 版面<b>${c.m1 || 0}</b> 张</span>
    <span class="db-stat-pill">M2 选择<b>${c.m2 || 0}</b> 张</span>
    <span class="db-stat-pill">M3 大题<b>${c.m3 || 0}</b> 张</span>
    <span class="db-stat-pill">涂卡 CNN<b>${c.choice || 0}</b> 条</span>
    <span class="db-stat-pill">8904 回流<b>${db.allow_sync_data ? '已开启' : '已关闭'}</b></span>
  `;
}
$('btnOpenTrainDbDir').onclick = async () => {
  try { await api('/api/training-db/open', { method: 'POST', body: '{}' }); }
  catch (err) { alert(err.message); }
};
$('btnPickTrainDbDir').onclick = async () => {
  try {
    const picked = await api('/api/pick-folder', { method: 'POST', body: '{}' });
    if (picked.path) {
      const res = await api('/api/training-db', { method: 'POST', body: JSON.stringify({ training_db_dir: picked.path }) });
      renderTrainingDbCard(res);
      showBanner('gui', '已更新本地训练数据库路径：' + picked.path);
    }
  } catch (err) { alert(err.message); }
};
$('btnInitTrainDbDir').onclick = async () => {
  try {
    const res = await api('/api/training-db', { method: 'POST', body: JSON.stringify({ ensure_dirs: true }) });
    renderTrainingDbCard(res);
    showBanner('gui', '已初始化训练库子目录（m1 / m2 / m3 / choice）');
  } catch (err) { alert(err.message); }
};
$('chkTrainDbEnabled').onchange = async () => {
  try {
    const res = await api('/api/training-db', { method: 'POST', body: JSON.stringify({ training_db_enabled: $('chkTrainDbEnabled').checked }) });
    renderTrainingDbCard(res);
  } catch (err) { alert(err.message); }
};

refreshOverview().then(async () => {
  await applyMirrorUiStateIfNeeded();
  if (!IS_MIRROR_MODE) {
    pingPresence('总览');
    connectSSE();
    scheduleOverviewPoll(4500);
    setInterval(() => { if (!document.hidden && (CURRENT_VIEW === 'train' || TRAIN_DATA?.train_job?.running)) refreshTrainOverview(); }, 3500);
    setInterval(() => pingPresence('总览'), 25000);
  }
});
