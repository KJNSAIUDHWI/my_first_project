'use strict';
/* ==================== Step 1：架构图化环境渲染 ==================== */
function renderCheck(rep) {
  CHECK = rep;
  const items = rep.items || [];
  const hw = rep.hardware || {};
  const infer = rep.infer_settings || {};
  const tab = $('tabCheck');
  tab.classList.toggle('ok', rep.ok);
  tab.classList.toggle('err', !rep.ok);
  $('tabCheckDesc').textContent = rep.ok ? '全部就绪' : `${rep.error_count} 项待修`;

  const devShort = hw.cuda ? `${hw.gpu_name} (${hw.vram_gb}G)` : `CPU ${hw.cpu_count || ''}线程`;
  $('checkSummary').textContent = rep.ok
    ? `全部就绪 · ${devShort}${rep.has_valid_api_key ? ' · 大模型 API 已配' : ' · 请配置大模型 API'}`
    : `发现 ${rep.error_count} 项缺失，点下方红色节点或按钮一键修复`;
  $('btnToIngest').classList.toggle('hidden', !rep.ok);

  const needRepair = !rep.ok || rep.warning_count > 0;
  $('repairBar').classList.toggle('hidden', !needRepair);
  if (needRepair) {
    const errItems = items.filter(x => x.severity === 'error');
    const warnItems = items.filter(x => x.severity === 'warn');
    if ($('repairBarTitle')) {
      $('repairBarTitle').textContent = errItems.length ? '需处理：' : '兼容提醒：';
      $('repairBarTitle').style.color = errItems.length ? 'var(--err)' : 'var(--warn)';
    }
    if ($('repairBarSummary')) {
      const targetList = errItems.length ? errItems : warnItems;
      const summaryTxt = targetList.slice(0, 2).map(x => `${x.name}（${x.detail}）`).join('；');
      $('repairBarSummary').textContent = summaryTxt + (errItems.length ? '' : '（一般不影响正常改卷）');
    }
  }
  $('btnRepairPip').classList.toggle('hidden', !(rep.missing_packages && rep.missing_packages.length));
  $('btnUnpackWeights').classList.toggle('hidden', !(rep.missing_weights && rep.missing_weights.length && rep.has_encrypted_weights));
  const vlmKeyItem = items.find(x => x.id === 'vlm_api_key' || x.id === 'ark_key');
  $('btnSetVlmKey').classList.toggle('hidden', !vlmKeyItem || vlmKeyItem.ok);
  updateEnvStatusPill(rep);

  // 节点 1：算力底座（Python + 依赖库 + GPU/CPU）
  const pkgItems = items.filter(x => x.category === 'runtime' || x.category === 'packages');
  const pkgErr = pkgItems.filter(x => x.severity === 'error');
  const pkgWarn = pkgItems.filter(x => x.severity === 'warn');
  const compState = pkgErr.length ? 'err' : (pkgWarn.length ? 'warn' : 'ok');
  ENV_STATES.compute = compState;
  $('envNodeCompute').className = `env-node is-${compState}`;
  $('envDotCompute').className = `arch-dot ${compState === 'ok' ? 'done' : compState}`;
  $('envBadgeCompute').className = `badge ${compState}`;
  $('envBadgeCompute').textContent = compState === 'ok' ? (hw.cuda ? 'GPU' : 'CPU') : (compState === 'warn' ? '兼容提醒' : `缺 ${pkgErr.length} 项`);
  $('envComputeMain').textContent = devShort;
  const pyItem = items.find(x => x.id === 'python');
  const pyVer = (pyItem?.detail || '').match(/Python\s+[\d.]+/)?.[0] || 'Python';
  const reqTotal = items.filter(x => x.id?.startsWith('pkg_')).length;
  const reqOk = items.filter(x => x.id?.startsWith('pkg_') && x.ok).length;
  $('envComputeSub').innerHTML = `
    <span class="an-chip ${pkgErr.length ? 'id' : 'choice'}">${esc(pyVer)}</span>
    <span class="an-chip ${reqOk < reqTotal ? 'id' : 'choice'}">依赖 ${reqOk}/${reqTotal}</span>
    ${pkgWarn.length ? `<span class="an-chip fill" style="background:#fffbeb;color:#b45309;border-color:#fcd34d">兼容提醒 ${pkgWarn.length}</span>` : ''}
  `;
  $('envComputeFoot').textContent = pkgErr.length ? '点击展开或上方一键安装' : `Batch ${infer.yolo_batch || '自动'} · 并发 ${infer.api_concurrency || 4}`;

  // 节点 2：本地检测链（orient / m1 / m2 / m3 / choice / paddle_ocr）
  const wSpecs = [
    ['weight_orient', '纠偏'],
    ['weight_m1', 'M1'],
    ['weight_m2', 'M2'],
    ['weight_m3', 'M3'],
    ['paddle_ocr', 'OCR'],
    ['weight_choice', 'CNN'],
  ];
  const wItems = items.filter(x => x.category === 'weights');
  const wMiss = wItems.filter(x => !x.ok);
  const modState = wMiss.length ? 'err' : 'ok';
  ENV_STATES.models = modState;
  $('envNodeModels').className = `env-node is-${modState}`;
  $('envDotModels').className = `arch-dot ${modState === 'ok' ? 'done' : 'err'}`;
  $('envBadgeModels').className = `badge ${modState}`;
  $('envBadgeModels').textContent = modState === 'ok' ? '就绪' : `缺 ${wMiss.length} 项`;
  $('envModelsMain').textContent = modState === 'ok' ? `${wItems.length}/${wItems.length} 权重齐全` : `缺少 ${wMiss.length} 个模型文件`;
  $('envModelPills').innerHTML = wSpecs.map(([id, lbl]) => {
    const it = items.find(x => x.id === id);
    const ok = Boolean(it && it.ok);
    return `<span class="env-mini ${ok ? 'ok' : 'err'}"><i></i>${esc(lbl)}</span>`;
  }).join('');
  $('envModelsFoot').textContent = modState === 'ok' ? '本地 0 费用 · 查看明细' : '点击输入密码解压权重';

  // 节点 3：大模型转录 API
  const vlmOk = Boolean(rep.has_valid_api_key);
  const vlmState = vlmOk ? 'ok' : 'warn';
  ENV_STATES.vlm = vlmState;
  $('envNodeVlm').className = `env-node is-${vlmState}`;
  $('envDotVlm').className = `arch-dot ${vlmOk ? 'done' : 'warn'}`;
  $('envBadgeVlm').className = `badge ${vlmState}`;
  $('envBadgeVlm').textContent = vlmOk ? '已配置' : '待配置';
  const vlmTitle = (vlmKeyItem?.name || '大模型 API').replace(/^大模型视觉 API\s*/, '').replace(/[()（）]/g, '') || '视觉大模型';
  $('envVlmMain').textContent = vlmTitle;
  const vlmModelOnly = (vlmKeyItem?.detail || '').replace(/^已配置\s*/, '').replace(/\s*\([^)]*\)\s*$/, '').trim();
  $('envVlmSub').innerHTML = vlmOk
    ? `<span class="an-chip choice" title="${esc(vlmKeyItem?.detail || '')}">${esc(vlmModelOnly || '密钥就绪')}</span>`
    : `<span class="an-chip id">${rep.placeholder_api_key ? '仍为占位符密钥' : '未填 API Key'}</span>`;
  $('envVlmFoot').textContent = vlmOk ? '更换服务商 / 测试连通' : '点击配置 API Key';

  // 节点 4：审阅协作
  ENV_STATES.review = 'ok';
  $('envReviewFoot').textContent = OVERVIEW?.active_batch ? '进入识别与审阅' : '进入导入试卷';

  // 完整自检明细抽屉（仅存在阻塞缺失项 error 时自动展开，平时默认折叠保持整洁）
  const okCnt = items.filter(x => x.severity === 'ok').length;
  $('checkDetailTag').textContent = `${okCnt}/${items.length} 正常` + (rep.warning_count ? ` · ${rep.warning_count} 提醒` : '') + (rep.error_count ? ` · ${rep.error_count} 缺失` : '');
  $('drawerCheckDetails').open = rep.error_count > 0;

  $('checkGrid').innerHTML = items.map(it => `
    <div class="check-item ${it.severity}">
      <span class="badge ${it.severity}">${it.severity === 'ok' ? '正常' : it.severity === 'warn' ? '提醒' : '缺失'}</span>
      <div style="flex:1;min-width:0">
        <div class="check-name">${esc(it.name)}</div>
        <div class="check-detail">${esc(it.detail)}</div>
        ${it.fix ? `<div class="fix-hint"><b>修复：</b>${esc(it.fix)}</div>` : ''}
      </div>
    </div>
  `).join('');

  if (rep.opencode_path && !$('inputOpencode').value) {
    $('inputOpencode').placeholder = '已检测：' + rep.opencode_path;
  }
  syncPerfPanel(hw, infer);
  requestAnimationFrame(drawEnvArchConnectors);
}

let LAST_BENCH_REC = null;
function syncPerfPanel(hw, infer) {
  if (hw) {
    $('perfHwSummary').textContent = hw.cuda
      ? `GPU：${hw.gpu_name}（${hw.vram_gb} GB）· CPU ${hw.cpu_count} 线程`
      : `CPU 模式（${hw.cpu_count || '?'} 线程，未检测到 CUDA GPU）`;
  }
  if (infer) {
    if ($('selInferDevice')) $('selInferDevice').value = infer.infer_device || 'auto';
    if ($('selYoloBatch')) $('selYoloBatch').value = String(infer.yolo_batch || 0);
    if ($('selCpuWorkers')) $('selCpuWorkers').value = String(infer.cpu_workers || 0);
    if ($('selApiConc')) $('selApiConc').value = String(infer.api_concurrency || 4);
    const devTxt = infer.infer_device === 'cpu' ? '仅 CPU' : (infer.infer_device === 'cuda:0' ? '强制 GPU' : '自动 GPU');
    $('perfQuickTag').textContent = `${devTxt} · Batch ${infer.yolo_batch || '自动'} · 并发 ${infer.api_concurrency || 4}`;
    if (infer.last_benchmark && !LAST_BENCH_REC) {
      const lb = infer.last_benchmark;
      $('benchResultWrap').className = 'bench-strip';
      $('benchSummaryText').textContent = `上次测速最优：${lb.label || ''}（${lb.sec_per_page} 秒/页 · ${lb.pages_per_min} 页/分）`;
    }
  }
}

function renderBenchmarkJob(bjob) {
  if (!bjob) return;
  const running = !!bjob.running;
  $('btnRunBenchmark').disabled = running;
  $('btnRunBenchmark').textContent = running ? '测速中...' : '🚀 一键测速';
  if (running) {
    $('benchResultWrap').className = 'bench-strip';
    $('btnApplyBench').classList.add('hidden');
    $('benchSummaryText').textContent = (bjob.logs || []).slice(-1)[0] || '正在测速（零 API 费用）...';
    return;
  }
  if (bjob.status === 'error' && bjob.error) {
    $('benchResultWrap').className = 'bench-strip err';
    $('btnApplyBench').classList.add('hidden');
    $('benchSummaryText').textContent = `测速失败：${bjob.error}`;
    return;
  }
  if (bjob.status === 'done' && bjob.result) {
    const res = bjob.result;
    LAST_BENCH_REC = res.recommended || null;
    $('benchResultWrap').className = 'bench-strip';
    $('benchSummaryText').textContent = res.summary || '测速完成';
    $('btnApplyBench').classList.toggle('hidden', !LAST_BENCH_REC);
    const rows = res.rows || [];
    const bestLabel = LAST_BENCH_REC?.label || '';
    $('benchTableWrap').innerHTML = `
      <table class="bench-table">
        <thead><tr><th>组合</th><th class="ctr">设备</th><th class="ctr">Batch</th><th class="ctr">线程</th><th class="num">秒/页</th><th class="num">页/分</th><th class="num">显存</th><th class="ctr">状态</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr class="${r.label === bestLabel ? 'best' : ''}">
              <td>${esc(r.label)}${r.label === bestLabel ? ' ★' : ''}</td>
              <td class="ctr">${esc(r.device)}</td>
              <td class="ctr">${r.yolo_batch}</td>
              <td class="ctr">${r.cpu_workers}</td>
              <td class="num">${r.ok ? r.sec_per_page + 's' : '-'}</td>
              <td class="num">${r.ok ? r.pages_per_min : '-'}</td>
              <td class="num">${r.peak_vram_mb ? r.peak_vram_mb + ' MB' : '-'}</td>
              <td class="ctr">${r.ok ? '<span class="badge ok">稳定</span>' : `<span class="badge err">${esc(r.error || '失败')}</span>`}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }
}

