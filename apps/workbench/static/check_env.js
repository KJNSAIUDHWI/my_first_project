'use strict';
/* ==================== Step 1：与识别流同构的体检 DAG + 共享基座双向联动 + 可忽略非阻塞提醒 ==================== */
const ENV_NODE_META = {
  orient: {
    elId: 'envNodeOrient',
    label: '① 原图纠偏',
    deps: ['compute', 'python', 'pkg_torch', 'pkg_ultralytics', 'pkg_cv2', 'pkg_numpy', 'pkg_PIL'],
  },
  m1: {
    elId: 'envNodeM1',
    label: '② M1 版面',
    deps: ['compute', 'python', 'pkg_torch', 'pkg_ultralytics', 'pkg_cv2', 'pkg_numpy'],
  },
  m2: {
    elId: 'envNodeM2',
    label: '③ M2 选择题',
    deps: ['compute', 'python', 'pkg_torch', 'pkg_ultralytics', 'paddle_ocr', 'pkg_paddleocr', 'pkg_onnxruntime', 'pkg_scipy', 'pkg_cv2', 'pkg_numpy'],
  },
  m3: {
    elId: 'envNodeM3',
    label: '④ M3 大题',
    deps: ['compute', 'python', 'pkg_torch', 'pkg_ultralytics', 'paddle_ocr', 'pkg_paddleocr', 'pkg_onnxruntime', 'pkg_scipy', 'pkg_cv2', 'pkg_numpy'],
  },
  vlm: {
    elId: 'envNodeVlm',
    label: '⑤ 大模型 API',
    deps: ['python', 'pkg_requests', 'pkg_PIL'],
  },
  review: {
    elId: 'envNodeReview',
    label: '⑥ 审阅协作',
    deps: ['python', 'pkg_yaml'],
  },
};

const ENV_DEP_LABELS = {
  compute: '计算设备',
  python: 'Python',
  pkg_torch: 'PyTorch',
  pkg_ultralytics: 'YOLO',
  paddle_ocr: 'OCR 题号权重',
  pkg_paddleocr: 'PaddleOCR',
  pkg_onnxruntime: 'ONNX',
  pkg_cv2: 'OpenCV',
  pkg_numpy: 'NumPy',
  pkg_PIL: 'Pillow',
  pkg_scipy: 'SciPy',
  pkg_yaml: 'PyYAML',
  pkg_requests: 'Requests',
};

let ENV_DEP_STATUS_MAP = {};

function clearEnvLinkage() {
  const stage = $('envArchStage');
  if (!stage) return;
  stage.classList.remove('env-hovering');
  stage.querySelectorAll('.is-linked').forEach(el => el.classList.remove('is-linked'));
  const hint = $('envLinkHint');
  if (hint) {
    hint.classList.remove('active');
    hint.textContent = '悬停上方模块或下方依赖，高亮查看调用关系';
  }
}

function highlightEnvByNode(nodeKey) {
  const meta = ENV_NODE_META[nodeKey];
  const stage = $('envArchStage');
  if (!meta || !stage) return;
  stage.classList.add('env-hovering');
  stage.querySelectorAll('.is-linked').forEach(el => el.classList.remove('is-linked'));
  const nodeEl = $(meta.elId);
  if (nodeEl) nodeEl.classList.add('is-linked');
  const depSet = new Set(meta.deps);
  stage.querySelectorAll('.env-dep-chip[data-dep-id]').forEach(chip => {
    if (depSet.has(chip.dataset.depId)) chip.classList.add('is-linked');
  });
  const hint = $('envLinkHint');
  if (hint) {
    const names = meta.deps.map(d => ENV_DEP_LABELS[d] || d);
    hint.classList.add('active');
    hint.textContent = `${meta.label} 共调用 ${names.length} 项基座：${names.join(' · ')}`;
  }
}

function highlightEnvByDep(depId) {
  const stage = $('envArchStage');
  if (!stage || !depId) return;
  stage.classList.add('env-hovering');
  stage.querySelectorAll('.is-linked').forEach(el => el.classList.remove('is-linked'));
  const chip = stage.querySelector(`.env-dep-chip[data-dep-id="${depId}"]`);
  if (chip) chip.classList.add('is-linked');
  const users = [];
  for (const [, meta] of Object.entries(ENV_NODE_META)) {
    if (meta.deps.includes(depId)) {
      users.push(meta.label);
      const nodeEl = $(meta.elId);
      if (nodeEl) nodeEl.classList.add('is-linked');
    }
  }
  const hint = $('envLinkHint');
  if (hint) {
    const st = ENV_DEP_STATUS_MAP[depId];
    const extra = st && st.note ? `（${st.note}）` : '';
    hint.classList.add('active');
    hint.textContent = `${ENV_DEP_LABELS[depId] || depId} 被 ${users.join('、')} 共用${extra}`;
  }
}

function bindEnvLinkageEvents() {
  const stage = $('envArchStage');
  if (!stage || stage._envLinkBound) return;
  stage._envLinkBound = true;
  stage.querySelectorAll('.env-node[data-env-node]').forEach(el => {
    el.addEventListener('mouseenter', () => highlightEnvByNode(el.dataset.envNode));
    el.addEventListener('mouseleave', clearEnvLinkage);
  });
}

function makeWeightMini(label, item) {
  const ok = Boolean(item && item.ok);
  const szMatch = String(item?.detail || '').match(/\(([\d.]+\s*MB)\)/i);
  const txt = ok ? (szMatch ? `${label} ${szMatch[1]}` : `${label} 就绪`) : `缺 ${label}`;
  return `<span class="env-mini ${ok ? 'ok' : 'err'}" title="${esc(item?.detail || '')}"><i></i>${esc(txt)}</span>`;
}

function makeDepChip(depId, label, subText, status, title) {
  const cls = status === 'err' ? 'is-err' : (status === 'warn' ? 'is-warn' : (status === 'dismissed' ? 'is-dismissed' : ''));
  return `<button type="button" class="env-dep-chip ${cls}" data-dep-id="${esc(depId)}" title="${esc(title || label)}"><i></i><span>${esc(label)}</span>${subText ? `<small>${esc(subText)}</small>` : ''}</button>`;
}

async function dismissEnvWarnings(warnItems) {
  if (!warnItems || !warnItems.length) return;
  try {
    const payload = warnItems.map(x => ({ id: x.id, detail: x.detail || '' }));
    const res = await api('/api/config', { method: 'POST', body: JSON.stringify({ dismiss_env_warnings: payload }) });
    if (res.check) renderCheck(res.check);
    else renderCheck(await api('/api/check'));
    showBanner('gui', `已忽略 ${payload.length} 条非阻塞提醒（可在下方「完整自检明细」随时恢复）`, 4200);
  } catch (err) {
    alert('忽略提醒失败：' + err.message);
  }
}

async function restoreEnvWarnings(idsOrAll = true) {
  try {
    const res = await api('/api/config', { method: 'POST', body: JSON.stringify({ restore_env_warnings: idsOrAll }) });
    if (res.check) renderCheck(res.check);
    else renderCheck(await api('/api/check'));
    showBanner('gui', '已恢复兼容提醒显示', 3200);
  } catch (err) {
    alert('恢复提醒失败：' + err.message);
  }
}

function renderCheck(rep) {
  CHECK = rep;
  const items = rep.items || [];
  const itemById = Object.fromEntries(items.map(x => [x.id, x]));
  const hw = rep.hardware || {};
  const infer = rep.infer_settings || {};
  const tab = $('tabCheck');
  tab.classList.toggle('ok', rep.ok);
  tab.classList.toggle('err', !rep.ok);
  $('tabCheckDesc').textContent = rep.ok ? '全部就绪' : `${rep.error_count} 项待修`;

  const devShort = hw.cuda ? `${hw.gpu_name} (${hw.vram_gb}G)` : `CPU ${hw.cpu_count || ''}线程`;
  const devForFmt = hw.cuda ? `${hw.gpu_name}${hw.vram_gb ? ` (${hw.vram_gb} GB)` : ''}` : devShort;
  const devCompact = typeof fmtDeviceShort === 'function' ? (fmtDeviceShort(devForFmt, hw) || devShort) : devShort;
  $('checkSummary').textContent = rep.ok
    ? `全部就绪 · ${devShort}${rep.has_valid_api_key ? ' · 大模型 API 已配' : ' · 请配置大模型 API'}`
    : `发现 ${rep.error_count} 项缺失，点下方红色节点或按钮一键修复`;
  $('btnToIngest').classList.toggle('hidden', !rep.ok);

  const errItems = items.filter(x => x.severity === 'error');
  const activeWarnItems = items.filter(x => x.severity === 'warn' && !x.dismissed);
  const dismissibleWarns = activeWarnItems.filter(x => x.dismissible);
  const dismissedItems = items.filter(x => x.dismissed);

  const needRepair = !rep.ok || activeWarnItems.length > 0;
  $('repairBar').classList.toggle('hidden', !needRepair);
  if (needRepair) {
    if ($('repairBarTitle')) {
      $('repairBarTitle').textContent = errItems.length ? '需处理：' : '兼容提醒：';
      $('repairBarTitle').style.color = errItems.length ? 'var(--err)' : 'var(--warn)';
    }
    if ($('repairBarSummary')) {
      const targetList = errItems.length ? errItems : activeWarnItems;
      const summaryTxt = targetList.slice(0, 2).map(x => `${x.name}（${x.detail}）`).join('；');
      $('repairBarSummary').textContent = summaryTxt + (errItems.length ? '' : '（一般不影响正常改卷，可一键忽略）');
    }
  }
  $('btnRepairPip').classList.toggle('hidden', !(rep.missing_packages && rep.missing_packages.length));
  $('btnUnpackWeights').classList.toggle('hidden', !(rep.missing_weights && rep.missing_weights.length && rep.has_encrypted_weights));
  const vlmKeyItem = itemById.vlm_api_key || itemById.ark_key;
  $('btnSetVlmKey').classList.toggle('hidden', !vlmKeyItem || vlmKeyItem.ok);
  const dismissBtn = $('btnDismissWarns');
  if (dismissBtn) {
    const canDismiss = errItems.length === 0 && dismissibleWarns.length > 0;
    dismissBtn.classList.toggle('hidden', !canDismiss);
    dismissBtn.onclick = () => dismissEnvWarnings(dismissibleWarns);
  }
  updateEnvStatusPill(rep);

  // ---------- 下方公共运行基座状态汇总（共享依赖只出现一次） ----------
  ENV_DEP_STATUS_MAP = {};
  const setDep = (id, status, note) => { ENV_DEP_STATUS_MAP[id] = { status, note }; };

  const pyItem = itemById.python;
  const pyVer = (pyItem?.detail || '').match(/Python\s+([\d.]+)/)?.[1] || '';
  const pySt = pyItem && !pyItem.ok ? 'err' : 'ok';
  setDep('compute', 'ok', devShort);
  setDep('python', pySt, pyItem?.detail || '');

  const buildPkgChip = (pkgId, label, compatId = null) => {
    const it = itemById[pkgId];
    const compat = compatId ? itemById[compatId] : null;
    let st = (it && !it.ok) ? 'err' : 'ok';
    let note = it?.detail || '';
    if (st === 'ok' && compat) {
      if (compat.dismissed) {
        st = 'dismissed';
        note = '已忽略：' + (compat.detail || '');
      } else if (compat.severity === 'warn') {
        st = 'warn';
        note = compat.detail || '';
      }
    }
    const verM = String(it?.detail || '').match(/\(v([\d.]+)\)/i);
    const shortVer = st === 'err' ? '缺失' : (st === 'warn' ? '提醒' : (st === 'dismissed' ? '已忽略' : (verM ? verM[1].split('.').slice(0, 2).join('.') : '')));
    setDep(pkgId, st, note);
    return makeDepChip(pkgId, label, shortVer, st, note || label);
  };

  const paddleItem = itemById.paddle_ocr;
  const paddleSt = paddleItem && !paddleItem.ok ? 'err' : 'ok';
  setDep('paddle_ocr', paddleSt, paddleItem?.detail || '');

  $('envBasePerfTag').textContent = `Batch ${infer.yolo_batch || '自动'} · 并发 ${infer.api_concurrency || 4}`;
  $('envBaseComputeRow').innerHTML = [
    makeDepChip('compute', hw.cuda ? devCompact : `CPU ${hw.cpu_count || ''}核`, hw.cuda ? 'GPU' : 'CPU', 'ok', `${devShort} · 点击展开高级调参`),
    makeDepChip('python', 'Python', pyVer || (pySt === 'ok' ? '就绪' : '异常'), pySt, pyItem?.detail || ''),
  ].join('');

  $('envBaseEngineRow').innerHTML = [
    buildPkgChip('pkg_torch', 'PyTorch'),
    buildPkgChip('pkg_ultralytics', 'YOLO'),
    makeDepChip('paddle_ocr', 'OCR 权重', paddleSt === 'ok' ? 'ONNX' : '缺失', paddleSt, paddleItem?.detail || 'PP-OCRv6 印刷题号权重'),
    buildPkgChip('pkg_paddleocr', 'PaddleOCR'),
    buildPkgChip('pkg_onnxruntime', 'ONNX'),
  ].join('');

  $('envBaseLibRow').innerHTML = [
    buildPkgChip('pkg_cv2', 'OpenCV', 'compat_opencv'),
    buildPkgChip('pkg_numpy', 'NumPy', 'compat_numpy'),
    buildPkgChip('pkg_PIL', 'Pillow'),
    buildPkgChip('pkg_scipy', 'SciPy'),
    buildPkgChip('pkg_yaml', 'PyYAML'),
    buildPkgChip('pkg_requests', 'Requests'),
  ].join('');

  const nodeBaseImpact = (nodeKey) => {
    const deps = ENV_NODE_META[nodeKey]?.deps || [];
    const hasErr = deps.some(d => ENV_DEP_STATUS_MAP[d]?.status === 'err');
    if (hasErr) return '<span class="env-base-impact err" title="下方公共基座存在缺失依赖">基座缺包</span>';
    return '';
  };

  // ---------- 上方 6 节点体检 DAG（与识别流同构，只展示各环节专属权重/配置） ----------
  // ① 原图纠偏
  const wOrient = itemById.weight_orient;
  const orientSt = wOrient && !wOrient.ok ? 'err' : 'ok';
  ENV_STATES.orient = orientSt;
  $('envNodeOrient').className = `env-node is-${orientSt}`;
  $('envDotOrient').className = `arch-dot ${orientSt === 'ok' ? 'done' : orientSt}`;
  $('envBadgeOrient').className = `badge ${orientSt}`;
  $('envBadgeOrient').textContent = orientSt === 'ok' ? '就绪' : '缺权重';
  $('envOrientMain').textContent = orientSt === 'ok' ? 'orient_page.pt' : '缺少纠偏模型';
  $('envOrientSub').innerHTML = makeWeightMini('纠偏', wOrient) + nodeBaseImpact('orient');
  $('envOrientFoot').textContent = orientSt === 'ok' ? '读图拉正 · 0 费用' : '点击解压权重';

  // ② M1 版面分区
  const wM1 = itemById.weight_m1;
  const m1St = wM1 && !wM1.ok ? 'err' : 'ok';
  ENV_STATES.m1 = m1St;
  $('envNodeM1').className = `env-node is-${m1St}`;
  $('envDotM1').className = `arch-dot ${m1St === 'ok' ? 'done' : m1St}`;
  $('envBadgeM1').className = `badge ${m1St}`;
  $('envBadgeM1').textContent = m1St === 'ok' ? '就绪' : '缺权重';
  $('envM1Main').textContent = m1St === 'ok' ? 'm1.pt' : '缺少 M1 模型';
  $('envM1Sub').innerHTML = makeWeightMini('M1', wM1) + nodeBaseImpact('m1');
  $('envM1Foot').textContent = m1St === 'ok' ? '选区 / 非选 / 姓名' : '点击解压权重';

  // ③ M2 选择题（专属 m2.pt + choice.pt）
  const wM2 = itemById.weight_m2;
  const wChoice = itemById.weight_choice;
  const m2St = (wM2 && !wM2.ok) || (wChoice && !wChoice.ok) ? 'err' : 'ok';
  ENV_STATES.m2 = m2St;
  $('envNodeM2').className = `env-node is-${m2St}`;
  $('envDotM2').className = `arch-dot ${m2St === 'ok' ? 'done' : m2St}`;
  $('envBadgeM2').className = `badge ${m2St}`;
  $('envBadgeM2').textContent = m2St === 'ok' ? '就绪' : '缺权重';
  $('envM2Main').textContent = m2St === 'ok' ? '本地直出 · 零 API 费' : '选择题权重缺失';
  $('envM2Sub').innerHTML = makeWeightMini('M2 定位', wM2) + makeWeightMini('CNN 涂卡', wChoice) + nodeBaseImpact('m2');
  $('envM2Foot').textContent = m2St === 'ok' ? '复用基座 OCR 题号' : '点击解压权重';

  // ④ M3 大题定位
  const wM3 = itemById.weight_m3;
  const m3St = wM3 && !wM3.ok ? 'err' : 'ok';
  ENV_STATES.m3 = m3St;
  $('envNodeM3').className = `env-node is-${m3St}`;
  $('envDotM3').className = `arch-dot ${m3St === 'ok' ? 'done' : m3St}`;
  $('envBadgeM3').className = `badge ${m3St}`;
  $('envBadgeM3').textContent = m3St === 'ok' ? '就绪' : '缺权重';
  $('envM3Main').textContent = m3St === 'ok' ? 'm3.pt' : '缺少 M3 模型';
  $('envM3Sub').innerHTML = makeWeightMini('M3 定位', wM3) + nodeBaseImpact('m3');
  $('envM3Foot').textContent = m3St === 'ok' ? '切块送大模型' : '点击解压权重';

  // ⑤ 大模型转录 API
  const vlmOk = Boolean(rep.has_valid_api_key);
  const vlmSt = vlmOk ? 'ok' : 'warn';
  ENV_STATES.vlm = vlmSt;
  $('envNodeVlm').className = `env-node is-${vlmSt}`;
  $('envDotVlm').className = `arch-dot ${vlmOk ? 'done' : 'warn'}`;
  $('envBadgeVlm').className = `badge ${vlmSt}`;
  $('envBadgeVlm').textContent = vlmOk ? '已配置' : '待配置';
  const vlmTitle = (vlmKeyItem?.name || '大模型 API').replace(/^大模型视觉 API\s*/, '').replace(/[()（）]/g, '') || '视觉大模型';
  $('envVlmMain').textContent = vlmTitle;
  const vlmModelOnly = (vlmKeyItem?.detail || '').replace(/^已配置\s*/, '').replace(/\s*\([^)]*\)\s*$/, '').trim();
  $('envVlmSub').innerHTML = vlmOk
    ? `<span class="env-mini ok" title="${esc(vlmKeyItem?.detail || '')}"><i></i>${esc(vlmModelOnly || '密钥就绪')}</span>`
    : `<span class="env-mini warn"><i></i>${rep.placeholder_api_key ? '占位符密钥' : '未填 API Key'}</span>`;
  $('envVlmFoot').textContent = vlmOk ? '更换服务商 / 测连通' : '点击配置 API Key';

  // ⑥ 审阅协作
  ENV_STATES.review = 'ok';
  $('envReviewFoot').textContent = OVERVIEW?.active_batch ? '进入识别与审阅' : '进入导入试卷';

  // 绑定下方基座芯片的悬停联动 & 点击直达
  bindEnvLinkageEvents();
  const stage = $('envArchStage');
  if (stage) {
    stage.querySelectorAll('.env-dep-chip[data-dep-id]').forEach(chip => {
      const depId = chip.dataset.depId;
      chip.addEventListener('mouseenter', () => highlightEnvByDep(depId));
      chip.addEventListener('mouseleave', clearEnvLinkage);
      chip.addEventListener('click', () => {
        if (depId === 'compute') {
          const d = $('drawerPerf');
          d.open = !d.open;
          if (d.open) d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else if (depId === 'paddle_ocr' && rep.missing_weights?.length && rep.has_encrypted_weights) {
          $('btnUnpackWeights').click();
        } else {
          const d = $('drawerCheckDetails');
          d.open = true;
          d.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      });
    });
  }

  // ---------- 完整自检明细抽屉（含已忽略提醒的恢复入口） ----------
  const okCnt = items.filter(x => x.severity === 'ok').length;
  const tagParts = [`${okCnt}/${items.length} 正常`];
  if (activeWarnItems.length) tagParts.push(`${activeWarnItems.length} 提醒`);
  if (dismissedItems.length) tagParts.push(`已忽略 ${dismissedItems.length}`);
  if (rep.error_count) tagParts.push(`${rep.error_count} 缺失`);
  $('checkDetailTag').textContent = tagParts.join(' · ');
  $('drawerCheckDetails').open = rep.error_count > 0;

  const disBar = $('checkDismissBar');
  if (disBar) {
    disBar.classList.toggle('hidden', dismissedItems.length === 0);
    if (dismissedItems.length > 0) {
      $('checkDismissText').textContent = `已忽略 ${dismissedItems.length} 条非阻塞兼容提醒（若对应包版本发生变化会自动重新提醒）`;
      $('btnRestoreAllWarns').onclick = () => restoreEnvWarnings(true);
    }
  }

  $('checkGrid').innerHTML = items.map(it => {
    const isDis = Boolean(it.dismissed);
    const sevCls = isDis ? 'dismissed' : it.severity;
    const badgeCls = isDis ? 'muted' : it.severity;
    const badgeTxt = isDis ? '已忽略' : (it.severity === 'ok' ? '正常' : it.severity === 'warn' ? '提醒' : '缺失');
    const actionBtn = isDis
      ? `<button type="button" class="btn xs" data-restore-warn="${esc(it.id)}" style="margin-top:6px">恢复提醒</button>`
      : (it.severity === 'warn' && it.dismissible
          ? `<button type="button" class="btn xs" data-dismiss-warn="${esc(it.id)}" style="margin-top:6px">忽略此提醒</button>`
          : '');
    return `
      <div class="check-item ${sevCls}">
        <span class="badge ${badgeCls}">${badgeTxt}</span>
        <div style="flex:1;min-width:0">
          <div class="check-name">${esc(it.name)}</div>
          <div class="check-detail">${esc(it.detail)}</div>
          ${it.fix ? `<div class="fix-hint"><b>修复：</b>${esc(it.fix)}</div>` : ''}
          ${actionBtn}
        </div>
      </div>
    `;
  }).join('');

  $('checkGrid').querySelectorAll('[data-dismiss-warn]').forEach(btn => {
    btn.onclick = () => {
      const it = itemById[btn.dataset.dismissWarn];
      if (it) dismissEnvWarnings([it]);
    };
  });
  $('checkGrid').querySelectorAll('[data-restore-warn]').forEach(btn => {
    btn.onclick = () => restoreEnvWarnings([btn.dataset.restoreWarn]);
  });

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
    if ($('envBasePerfTag')) $('envBasePerfTag').textContent = `Batch ${infer.yolo_batch || '自动'} · 并发 ${infer.api_concurrency || 4}`;
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
  $('btnRunBenchmark').innerHTML = running ? `${uiIcon('loader')}<span>测速中...</span>` : `${uiIcon('gauge')}<span>一键测速</span>`;
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
              <td>${esc(r.label)}${r.label === bestLabel ? ' <span class="badge ok">最优</span>' : ''}</td>
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
