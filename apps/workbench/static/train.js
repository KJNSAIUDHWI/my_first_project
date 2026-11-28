'use strict';
/* ==================== 模型微调训练 ==================== */
let TRAIN_DATA = null;
let SELECTED_TRAIN_TASK = 'm1';

function updateTrainParamPreview() {
  const ep = $('trainEpochs').value, bs = $('trainBatch').value, lr = $('trainLr').value;
  $('trainParamPreview').textContent = `${ep} 轮 · Batch ${bs} · LR ${lr}`;
}
function applyTrainTaskDefaults(task) {
  SELECTED_TRAIN_TASK = task;
  $('trainTaskSelect').value = task;
  const mod = (TRAIN_DATA?.modules || []).find(m => m.task === task);
  const hw = TRAIN_DATA?.hardware || {};
  const isCuda = $('trainDeviceSelect').value !== 'cpu' && !!hw.cuda;
  if (mod) {
    $('trainEpochs').value = mod.default_epochs || 50;
    $('trainBatch').value = isCuda ? (mod.default_batch_gpu || 4) : (mod.default_batch_cpu || 2);
    $('trainLr').value = String(mod.default_lr || 0.0001);
    $('trainImgszWrap').classList.toggle('hidden', mod.kind !== 'yolo');
  }
  updateTrainParamPreview();
  if (task === 'choice' && $('drawerOptionalChoiceTrain')) {
    $('drawerOptionalChoiceTrain').open = true;
  }
  document.querySelectorAll('.train-mod').forEach(el => el.classList.toggle('active', el.dataset.task === task));
}

function renderTrainOverview(data) {
  const firstLoad = !TRAIN_DATA;
  TRAIN_DATA = data;
  const hw = data.hardware || {};
  $('trainHwInfo').textContent = hw.cuda ? 'CUDA 混合精度就绪' : 'CPU 模式（建议调小轮数）';
  if (firstLoad) {
    $('trainDeviceSelect').innerHTML = hw.cuda
      ? `<option value="0" selected>GPU: ${esc(hw.gpu_name)}</option><option value="cpu">CPU</option>`
      : `<option value="cpu" selected>CPU</option>`;
  }

  const renderModCard = m => {
    const ds = m.dataset || {};
    return `
      <div class="train-mod ${m.task === SELECTED_TRAIN_TASK ? 'active' : ''}" data-task="${esc(m.task)}">
        <div class="between">
          <strong style="font-size:13.5px">${esc(m.name)}</strong>
          <span class="badge ${ds.ready ? 'ok' : 'warn'}">${ds.ready ? '可训练' : '待补充'}</span>
        </div>
        <div style="font-size:12.5px;font-weight:600">已审 ${ds.reviewed || 0} / ${ds.total_images || 0}</div>
        <div class="muted" style="font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(m.current_weight || '未就绪')}</div>
        ${m.has_rollback ? `<button class="btn xs" type="button" data-rollback-task="${esc(m.task)}">↩ 回退旧版</button>` : ''}
      </div>
    `;
  };
  const allMods = data.modules || [];
  const coreMods = allMods.filter(m => m.task !== 'choice');
  const choiceMod = allMods.find(m => m.task === 'choice');

  $('trainModuleGrid').innerHTML = coreMods.map(renderModCard).join('');
  if (choiceMod && $('trainOptionalChoiceHost')) {
    const cds = choiceMod.dataset || {};
    $('choiceTrainTag').textContent = `已审 ${cds.reviewed || 0} / ${cds.total_images || 0} · 可选`;
    $('trainOptionalChoiceHost').innerHTML = renderModCard(choiceMod);
  }

  const bindTrainCards = rootEl => {
    if (!rootEl) return;
    rootEl.querySelectorAll('.train-mod').forEach(card => {
      card.onclick = e => { if (!e.target.closest('button')) applyTrainTaskDefaults(card.dataset.task); };
    });
    rootEl.querySelectorAll('[data-rollback-task]').forEach(btn => {
      btn.onclick = async () => {
        const task = btn.dataset.rollbackTask;
        if (!confirm(`确认将 ${task.toUpperCase()} 回退到上一版权重？`)) return;
        btn.disabled = true;
        try {
          const res = await api('/api/train/rollback', { method: 'POST', body: JSON.stringify({ task }) });
          showBanner('gui', res.message || '已回退');
          await refreshTrainOverview();
        } catch (err) { alert(err.message); }
        finally { btn.disabled = false; }
      };
    });
  };
  bindTrainCards($('trainModuleGrid'));
  bindTrainCards($('trainOptionalChoiceHost'));
  if (firstLoad) applyTrainTaskDefaults(SELECTED_TRAIN_TASK);

  const job = data.train_job || {};
  const running = !!job.running;
  if (running && job.task === 'choice' && $('drawerOptionalChoiceTrain')) {
    $('drawerOptionalChoiceTrain').open = true;
  }
  $('btnStartTrain').disabled = running;
  $('btnStartTrain').innerHTML = running ? `${uiIcon('loader')}<span>训练中 (${esc(job.task?.toUpperCase() || '')})...</span>` : `${uiIcon('play', 'ui-ico-xs')}<span>开始微调</span>`;
  $('btnStopTrain').classList.toggle('hidden', !running);
  $('tabTrainDesc').textContent = running ? `训练 ${job.task?.toUpperCase() || ''} 中` : '微调与换权重';

  if (running || (job.logs && job.logs.length)) {
    $('trainProgressWrap').classList.remove('hidden');
    const ep = job.completed_epochs || 0, totalEp = job.epochs || 1;
    const pct = Math.min(100, Math.round((ep / Math.max(1, totalEp)) * 100));
    $('trainProgBar').style.width = (running ? Math.max(3, pct) : pct) + '%';
    $('trainProgLabel').textContent = `${job.run || ''} · ${ep}/${totalEp} 轮`;
    const mt = job.metrics || {}, parts = [];
    if (mt.mAP50 !== undefined) parts.push(`mAP50 ${(mt.mAP50 * 100).toFixed(1)}%`);
    if (mt.exact !== undefined) parts.push(`精确 ${(mt.exact * 100).toFixed(2)}%`);
    if (mt.box_loss !== undefined) parts.push(`loss ${mt.box_loss}`);
    $('trainProgMetrics').textContent = parts.join(' · ');
    $('trainLogBox').classList.remove('hidden');
    $('trainLogBox').textContent = (job.logs || []).slice(-60).join('\n');
    $('trainLogBox').scrollTop = $('trainLogBox').scrollHeight;
  }

  const runs = data.runs || [];
  $('trainRunsBody').innerHTML = !runs.length
    ? '<tr><td colspan="6" class="muted">暂无训练记录</td></tr>'
    : runs.map(r => {
      const cnt = r.counts || {};
      const cntStr = (cnt.train || cnt.val) ? `${cnt.train || 0}/${cnt.val || 0}` : '-';
      const mt = r.metrics || {};
      let mStr = mt.completed_epochs ? `${mt.completed_epochs}轮` : '-';
      if (mt.mAP50 !== undefined) mStr += ` · ${(mt.mAP50 * 100).toFixed(1)}%`;
      if (mt.exact !== undefined) mStr += ` · ${(mt.exact * 100).toFixed(1)}%`;
      const stBadge = r.is_deployed ? '<span class="badge ok">使用中</span>'
        : r.status === 'running' ? '<span class="badge warn">训练中</span>'
        : r.status === 'success' ? '<span class="badge ok">就绪</span>'
        : `<span class="badge err" title="${esc(r.error || '')}">失败</span>`;
      const act = r.is_deployed ? '<span class="muted">已启用</span>'
        : r.can_deploy ? `<button class="btn primary xs" type="button" data-deploy-task="${esc(r.task)}" data-deploy-run="${esc(r.run)}">启用</button>` : '-';
      return `<tr>
        <td><strong>${esc(r.task_name)}</strong></td>
        <td><code>${esc(r.run)}</code></td>
        <td>${esc(cntStr)}</td>
        <td>${esc(mStr)}</td>
        <td>${stBadge}</td>
        <td class="row">${act}<button class="btn xs" type="button" data-monitor-run="${esc(r.run)}">监控</button></td>
      </tr>`;
    }).join('');

  $('trainRunsBody').querySelectorAll('[data-deploy-run]').forEach(btn => {
    btn.onclick = async () => {
      const task = btn.dataset.deployTask, run = btn.dataset.deployRun;
      if (!confirm(`确认将 ${task.toUpperCase()} 切换为 ${run}？（自动备份旧权重）`)) return;
      btn.disabled = true;
      try {
        const res = await api('/api/train/deploy', { method: 'POST', body: JSON.stringify({ task, run }) });
        showBanner('gui', res.message || '权重已切换');
        await refreshTrainOverview();
      } catch (err) { alert(err.message); }
      finally { btn.disabled = false; }
    };
  });
  $('trainRunsBody').querySelectorAll('[data-monitor-run]').forEach(btn => {
    btn.onclick = () => openMonitor(btn.dataset.monitorRun);
  });
}

async function refreshTrainOverview() {
  if (!OVERVIEW?.is_host) return;
  try { renderTrainOverview(await api('/api/train/overview')); }
  catch (err) { $('trainHwInfo').textContent = '读取失败：' + err.message; }
}
async function openMonitor(runName = '') {
  const pendingWin = window.open('about:blank', '_blank');
  try {
    const res = await api('/api/train/monitor', { method: 'POST', body: JSON.stringify({ run: runName }) });
    const url = `http://${window.location.hostname || '127.0.0.1'}:${res.port}/`;
    if (pendingWin && !pendingWin.closed) pendingWin.location.href = url;
    else window.open(url, '_blank');
  } catch (err) {
    if (pendingWin && !pendingWin.closed) pendingWin.close();
    alert(err.message);
  }
}

$('trainTaskSelect').onchange = () => applyTrainTaskDefaults($('trainTaskSelect').value);
$('trainDeviceSelect').onchange = () => applyTrainTaskDefaults($('trainTaskSelect').value);
['trainEpochs', 'trainBatch', 'trainLr'].forEach(id => $(id).addEventListener('input', updateTrainParamPreview));
$('btnRefreshTrain').onclick = refreshTrainOverview;
$('btnOpenMonitor').onclick = () => openMonitor(TRAIN_DATA?.train_job?.run || '');

$('btnSyncBatchTrain').onclick = async () => {
  $('btnSyncBatchTrain').disabled = true;
  $('trainSyncMsg').textContent = '同步中...';
  try {
    const res = await api('/api/train/sync-batch', { method: 'POST', body: '{}' });
    $('trainSyncMsg').textContent = res.message;
    await refreshTrainOverview();
  } catch (err) { $('trainSyncMsg').textContent = '失败：' + err.message; alert(err.message); }
  finally { $('btnSyncBatchTrain').disabled = false; }
};
$('btnStartTrain').onclick = async () => {
  const task = $('trainTaskSelect').value;
  const payload = {
    task, device: $('trainDeviceSelect').value,
    epochs: Number($('trainEpochs').value) || 50,
    imgsz: Number($('trainImgsz').value) || 1024,
    batch: Number($('trainBatch').value) || 4,
    lr: Number($('trainLr').value) || 0.0001,
    purpose: $('trainPurpose').value.trim() || '工作台微调',
  };
  if (!confirm(`开始微调 ${task.toUpperCase()}（${payload.epochs} 轮）？`)) return;
  $('btnStartTrain').disabled = true;
  try {
    const res = await api('/api/train/start', { method: 'POST', body: JSON.stringify(payload) });
    showBanner('gui', `已启动训练：${res.run}`);
    await refreshTrainOverview();
  } catch (err) { alert(err.message); $('btnStartTrain').disabled = false; }
};
$('btnStopTrain').onclick = async () => {
  if (!confirm('确认停止训练？')) return;
  $('btnStopTrain').disabled = true;
  try {
    const res = await api('/api/train/stop', { method: 'POST', body: '{}' });
    showBanner('gui', res.message || '已停止');
    await refreshTrainOverview();
  } catch (err) { alert(err.message); }
  finally { $('btnStopTrain').disabled = false; }
};

// 容器尺寸监听，保证两套架构图 SVG 连线永不漂移
if (typeof ResizeObserver !== 'undefined') {
  const ro = new ResizeObserver(() => {
    if (CURRENT_VIEW === 'check') drawEnvArchConnectors();
    if (CURRENT_VIEW === 'overview') PipeFlow.relayout(true);
  });
  ro.observe($('envArchStage'));
  ro.observe($('pipeArchStage'));
}

