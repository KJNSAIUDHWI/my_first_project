'use strict';
/* ==================== 双贴入区（试卷 + 答案）与爽感确认动效（回弹 + 礼花 + 绿勾） ==================== */
let STAGED_PAPER = null;   // { kind: 'path', path, name, image_count, sample_names } | { kind: 'files', files: [{ name, data_b64, size, thumbUrl }] }
let STAGED_ANSWER = null;  // { kind: 'file', file, name, size, thumbUrl } | { kind: 'path', path, name, size } | { kind: 'text', text, length }

function fmtFileSize(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || ''));
    fr.onerror = () => reject(new Error('读取文件失败'));
    fr.readAsDataURL(file);
  });
}

function spawnCelebrateBurst(hostEl) {
  if (!hostEl) return;
  hostEl.innerHTML = '';
  const isIter = typeof WORK_MODE === 'string' && WORK_MODE === 'iteration';
  const c1 = isIter ? '#2563eb' : '#b8531b';
  const c2 = isIter ? '#0284c7' : '#d97706';
  const c3 = isIter ? '#0e7490' : '#4a7852';
  const c4 = isIter ? '#4f46e5' : '#e07a24';
  const pieces = [
    { ico: 'sparkles', x1: -10, y1: -16, x2: -44, y2: -54, r1: '-18deg', r2: '-42deg', color: c1 },
    { ico: 'sparkles', x1: 8,   y1: -20, x2: 26,  y2: -58, r1: '15deg',  r2: '45deg',  color: c2 },
    { ico: 'check',    x1: -24, y1: -8,  x2: -74, y2: -34, r1: '-25deg', r2: '-60deg', color: c3 },
    { ico: 'check',    x1: -2,  y1: -24, x2: -8,  y2: -64, r1: '0deg',   r2: '12deg',  color: c3 },
    { ico: 'sparkles', x1: 14,  y1: -8,  x2: 42,  y2: -28, r1: '20deg',  r2: '55deg',  color: c4 },
    { ico: 'zap',      x1: -18, y1: 6,   x2: -58, y2: 14,  r1: '-10deg', r2: '-35deg', color: c2 },
  ];
  pieces.forEach((p, idx) => {
    const sp = document.createElement('span');
    sp.className = 'celebrate-bit';
    sp.innerHTML = uiIcon(p.ico, 'ui-ico');
    sp.style.right = '22px';
    sp.style.top = '18px';
    sp.style.color = p.color;
    sp.style.setProperty('--tx1', p.x1 + 'px');
    sp.style.setProperty('--ty1', p.y1 + 'px');
    sp.style.setProperty('--tx2', p.x2 + 'px');
    sp.style.setProperty('--ty2', p.y2 + 'px');
    sp.style.setProperty('--rot1', p.r1);
    sp.style.setProperty('--rot2', p.r2);
    sp.style.animationDelay = (idx * 24) + 'ms';
    hostEl.appendChild(sp);
  });
  clearTimeout(hostEl._ct);
  hostEl._ct = setTimeout(() => { hostEl.innerHTML = ''; }, 980);
}

function triggerDropConfirmFX(cardEl, celebrateEl, tagEl, tagText) {
  if (!cardEl) return;
  if (tagEl && tagText) tagEl.textContent = tagText;
  cardEl.classList.remove('confirm-pop', 'is-confirmed');
  void cardEl.offsetWidth;
  cardEl.classList.add('is-confirmed', 'confirm-pop');
  spawnCelebrateBurst(celebrateEl);
  clearTimeout(cardEl._pt);
  cardEl._pt = setTimeout(() => cardEl.classList.remove('confirm-pop'), 620);
}

function updateIngestReadyHint() {
  const hint = $('ingestReadyHint');
  const cta = $('btnIngestAndRun');
  const pOk = Boolean(STAGED_PAPER || $('inputSourceDir').value.trim());
  const aOk = Boolean(STAGED_ANSWER);
  if (cta) {
    const wasReady = cta.dataset.ready === '1';
    cta.dataset.ready = pOk ? '1' : '0';
    if (pOk && !wasReady) {
      cta.classList.remove('ready-pop');
      void cta.offsetWidth;
      cta.classList.add('ready-pop');
    }
  }
  if (!hint) return;
  if (pOk && aOk) hint.textContent = '试卷与答案都已就位，点击上方按钮即可一键开改';
  else if (pOk) hint.textContent = '试卷已就位（答案可留空，也可现在贴入）';
  else hint.textContent = '放入试卷后即可一键开改';
}

function showPaperError(msg) {
  const el = $('paperErrorTip');
  if (!el) return;
  el.textContent = msg || '';
  el.classList.toggle('hidden', !msg);
}

function clearStagedPaper(clearInput = true) {
  STAGED_PAPER = null;
  if (clearInput && $('inputSourceDir')) $('inputSourceDir').value = '';
  $('dropZonePaper')?.classList.remove('is-confirmed', 'confirm-pop');
  $('paperSummaryBox')?.classList.add('hidden');
  $('paperThumbImg')?.classList.add('hidden');
  showPaperError('');
  updateIngestReadyHint();
}

function setPaperConfirmedPath(info) {
  STAGED_PAPER = { kind: 'path', ...info };
  showPaperError('');
  $('paperSummaryBox').classList.remove('hidden');
  $('paperThumbImg').classList.add('hidden');
  $('paperSummaryTitle').textContent = info.kind === 'file'
    ? `单张测试卷：${info.name}`
    : `文件夹：${info.name}（共 ${info.image_count} 张）`;
  $('paperSummaryMeta').textContent = info.kind === 'file'
    ? `${fmtFileSize(info.size_bytes)} · ${info.path}`
    : `含 ${(info.sample_names || []).slice(0, 3).join('、')}${info.image_count > 3 ? ' 等' : ''}`;
  triggerDropConfirmFX(
    $('dropZonePaper'),
    $('paperCelebrate'),
    $('paperConfirmTag'),
    info.kind === 'file' ? '已放入 1 张' : `已放入 ${info.image_count} 张`
  );
  updateIngestReadyHint();
}

async function setPaperConfirmedFiles(fileList, sourceLabel = '拖入图片') {
  const imgs = Array.from(fileList || []).filter(f => f && (f.type.startsWith('image/') || /\.(jpg|jpeg|png|bmp|webp)$/i.test(f.name || '')));
  if (!imgs.length) {
    showPaperError('请放入图片格式文件（jpg / png / bmp / webp）');
    return;
  }
  showPaperError('');
  const encoded = [];
  for (let i = 0; i < imgs.length; i++) {
    const f = imgs[i];
    const dataUrl = await readFileAsDataUrl(f);
    const fname = f.name && f.name !== 'image.png' ? f.name : `剪贴板试卷_${i + 1}.png`;
    encoded.push({ name: fname, data_b64: dataUrl, size: f.size || 0, thumbUrl: dataUrl });
  }
  STAGED_PAPER = { kind: 'files', files: encoded };
  if ($('inputSourceDir')) $('inputSourceDir').value = '';
  $('paperSummaryBox').classList.remove('hidden');
  const thumb = $('paperThumbImg');
  if (thumb && encoded[0]?.thumbUrl) {
    thumb.src = encoded[0].thumbUrl;
    thumb.classList.remove('hidden');
  }
  const totalBytes = encoded.reduce((a, b) => a + (b.size || 0), 0);
  $('paperSummaryTitle').textContent = encoded.length === 1
    ? `${sourceLabel}：${encoded[0].name}`
    : `已选 ${encoded.length} 张试卷照片`;
  $('paperSummaryMeta').textContent = encoded.length === 1
    ? `${fmtFileSize(totalBytes)} · 单张快速测试已就绪`
    : `共 ${fmtFileSize(totalBytes)} · ${encoded.slice(0, 3).map(x => x.name).join('、')}${encoded.length > 3 ? ' 等' : ''}`;
  triggerDropConfirmFX(
    $('dropZonePaper'),
    $('paperCelebrate'),
    $('paperConfirmTag'),
    encoded.length === 1 ? '已贴入 1 张' : `已放入 ${encoded.length} 张`
  );
  updateIngestReadyHint();
}

let _paperInspectTimer = null;
async function inspectPaperPathInput(immediate = false) {
  const raw = ($('inputSourceDir')?.value || '').trim();
  if (!raw) {
    if (STAGED_PAPER?.kind === 'path') clearStagedPaper(false);
    return;
  }
  const run = async () => {
    try {
      const info = await api('/api/ingest/inspect-path', { method: 'POST', body: JSON.stringify({ path: raw }) });
      if (info.ok) {
        if ($('inputSourceDir').value.trim() !== info.path) $('inputSourceDir').value = info.path;
        setPaperConfirmedPath(info);
      }
    } catch (err) {
      STAGED_PAPER = null;
      $('dropZonePaper')?.classList.remove('is-confirmed');
      $('paperSummaryBox')?.classList.add('hidden');
      showPaperError(err.message);
      updateIngestReadyHint();
    }
  };
  clearTimeout(_paperInspectTimer);
  if (immediate) await run();
  else _paperInspectTimer = setTimeout(run, 360);
}

function clearStagedAnswer(clearTextarea = true) {
  STAGED_ANSWER = null;
  if (clearTextarea && $('inputAnswerPaste')) $('inputAnswerPaste').value = '';
  $('dropZoneAnswer')?.classList.remove('is-confirmed', 'confirm-pop');
  $('answerSummaryBox')?.classList.add('hidden');
  $('answerThumbImg')?.classList.add('hidden');
  updateIngestReadyHint();
}

async function setAnswerConfirmedFile(file, sourceLabel = '答案文件') {
  if (!file) return;
  const isImg = file.type.startsWith('image/') || /\.(png|jpg|jpeg|bmp|webp)$/i.test(file.name || '');
  const fname = file.name && file.name !== 'image.png' ? file.name : '剪贴板答案截图.png';
  let thumbUrl = '';
  if (isImg) thumbUrl = await readFileAsDataUrl(file);
  STAGED_ANSWER = { kind: 'file', file, name: fname, size: file.size || 0, thumbUrl };
  $('answerSummaryBox').classList.remove('hidden');
  const thumb = $('answerThumbImg');
  if (thumb && thumbUrl) { thumb.src = thumbUrl; thumb.classList.remove('hidden'); }
  else if (thumb) thumb.classList.add('hidden');
  $('answerSummaryTitle').textContent = `${sourceLabel}：${fname}`;
  $('answerSummaryMeta').textContent = `${fmtFileSize(file.size)} · 入库后自动交 AI 整理为判分标准`;
  triggerDropConfirmFX($('dropZoneAnswer'), $('answerCelebrate'), $('answerConfirmTag'), '答案已放入');
  updateIngestReadyHint();
}

function setAnswerConfirmedPath(info) {
  STAGED_ANSWER = { kind: 'path', path: info.path, name: info.name, size: info.size_bytes || 0 };
  $('answerSummaryBox').classList.remove('hidden');
  $('answerThumbImg')?.classList.add('hidden');
  $('answerSummaryTitle').textContent = `本地答案文件：${info.name}`;
  $('answerSummaryMeta').textContent = `${fmtFileSize(info.size_bytes)} · ${info.path}`;
  triggerDropConfirmFX($('dropZoneAnswer'), $('answerCelebrate'), $('answerConfirmTag'), '路径已确认');
  updateIngestReadyHint();
}

function setAnswerConfirmedText(text) {
  const clean = String(text || '').trim();
  if (!clean) {
    if (STAGED_ANSWER?.kind === 'text' || STAGED_ANSWER?.kind === 'path') clearStagedAnswer(false);
    return;
  }
  const lines = clean.split(/\r?\n/).filter(Boolean).length;
  STAGED_ANSWER = { kind: 'text', text: clean, length: clean.length };
  $('answerSummaryBox').classList.remove('hidden');
  $('answerThumbImg')?.classList.add('hidden');
  $('answerSummaryTitle').textContent = `已贴入答案文本（${clean.length} 字 · ${lines} 行）`;
  $('answerSummaryMeta').textContent = clean.slice(0, 56).replace(/\s+/g, ' ') + (clean.length > 56 ? '...' : '');
  triggerDropConfirmFX($('dropZoneAnswer'), $('answerCelebrate'), $('answerConfirmTag'), '答案已贴入');
  updateIngestReadyHint();
}

function looksLikeLocalPath(s) {
  const t = String(s || '').trim().replace(/^["']|["']$/g, '');
  if (!t || t.includes('\n') || t.length > 320) return false;
  return /^[a-zA-Z]:[\\/]/.test(t) || /^\\\\/.test(t);
}

let _ansInputTimer = null;
async function processAnswerTextOrPath(raw, immediate = false) {
  const clean = String(raw || '').trim();
  if (!clean) {
    if (STAGED_ANSWER?.kind === 'text' || STAGED_ANSWER?.kind === 'path') clearStagedAnswer(false);
    return;
  }
  const run = async () => {
    if (looksLikeLocalPath(clean)) {
      try {
        const info = await api('/api/schema/inspect-path', { method: 'POST', body: JSON.stringify({ path: clean }) });
        if (info.ok) {
          setAnswerConfirmedPath(info);
          return;
        }
      } catch (_) {}
    }
    setAnswerConfirmedText(clean);
  };
  clearTimeout(_ansInputTimer);
  if (immediate) await run();
  else _ansInputTimer = setTimeout(run, 420);
}

// 左侧试卷卡片绑定
$('btnPickFolder').onclick = async () => {
  $('btnPickFolder').disabled = true;
  try {
    const res = await api('/api/pick-folder', { method: 'POST', body: '{}' });
    if (res.path) {
      $('inputSourceDir').value = res.path;
      await inspectPaperPathInput(true);
    } else if (res.timeout || res.cancelled) {
      showBanner('gui', res.message || '已取消选择文件夹，可直接粘贴路径', 4500);
    }
  } catch (err) {
    showBanner('gui', '打开目录选择框失败，请直接粘贴文件夹路径（' + err.message + '）', 5000);
  } finally {
    $('btnPickFolder').disabled = false;
  }
};
if ($('btnPickPaperFiles')) $('btnPickPaperFiles').onclick = () => $('paperFilesInput')?.click();
if ($('paperFilesInput')) {
  $('paperFilesInput').onchange = e => {
    const files = e.target.files;
    if (files && files.length) setPaperConfirmedFiles(files, '已选单张测试卷');
    e.target.value = '';
  };
}
if ($('btnClearPaper')) $('btnClearPaper').onclick = () => clearStagedPaper(true);
if ($('inputSourceDir')) {
  $('inputSourceDir').addEventListener('paste', () => setTimeout(() => inspectPaperPathInput(true), 20));
  $('inputSourceDir').addEventListener('change', () => inspectPaperPathInput(true));
  $('inputSourceDir').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); inspectPaperPathInput(true); } });
}
const _dzPaper = $('dropZonePaper');
if (_dzPaper) {
  _dzPaper.addEventListener('dragover', e => { e.preventDefault(); _dzPaper.classList.add('dragover'); });
  _dzPaper.addEventListener('dragleave', e => { if (!_dzPaper.contains(e.relatedTarget)) _dzPaper.classList.remove('dragover'); });
  _dzPaper.addEventListener('drop', e => {
    e.preventDefault();
    _dzPaper.classList.remove('dragover');
    if (e.dataTransfer?.files?.length) setPaperConfirmedFiles(e.dataTransfer.files, '拖入试卷');
  });
}

// 右侧答案卡片绑定
if ($('btnPickAnswerFile')) $('btnPickAnswerFile').onclick = () => $('answerStageFileInput')?.click();
if ($('answerStageFileInput')) {
  $('answerStageFileInput').onchange = e => {
    const f = e.target.files?.[0];
    if (f) setAnswerConfirmedFile(f, '答案文件');
    e.target.value = '';
  };
}
if ($('btnClearAnswerStage')) $('btnClearAnswerStage').onclick = () => clearStagedAnswer(true);
if ($('inputAnswerPaste')) {
  $('inputAnswerPaste').addEventListener('paste', e => {
    const items = Array.from(e.clipboardData?.items || []);
    const imgItem = items.find(it => it.type && it.type.startsWith('image/'));
    if (imgItem) {
      e.preventDefault();
      const f = imgItem.getAsFile();
      if (f) setAnswerConfirmedFile(f, '剪贴板答案截图');
      return;
    }
    setTimeout(() => processAnswerTextOrPath($('inputAnswerPaste').value, true), 20);
  });
  $('inputAnswerPaste').addEventListener('input', () => processAnswerTextOrPath($('inputAnswerPaste').value, false));
}
const _dzAns = $('dropZoneAnswer');
if (_dzAns) {
  _dzAns.addEventListener('dragover', e => { e.preventDefault(); _dzAns.classList.add('dragover'); });
  _dzAns.addEventListener('dragleave', e => { if (!_dzAns.contains(e.relatedTarget)) _dzAns.classList.remove('dragover'); });
  _dzAns.addEventListener('drop', e => {
    e.preventDefault();
    _dzAns.classList.remove('dragover');
    const f = e.dataTransfer?.files?.[0];
    if (f) setAnswerConfirmedFile(f, '拖入答案');
  });
}

// 全局智能 Ctrl+V（在「贴入试卷与答案」页面直接贴截图或路径，不用先找按钮）
document.addEventListener('paste', e => {
  if (CURRENT_VIEW !== 'ingest') return;
  const active = document.activeElement;
  if (active && (active.id === 'inputAnswerPaste' || active.id === 'aiNoteInput' || active.id === 'inputBatchName')) return;
  const items = Array.from(e.clipboardData?.items || []);
  const imgItems = items.filter(it => it.type && it.type.startsWith('image/'));
  const inAnswerZone = active && $('dropZoneAnswer')?.contains(active);
  if (imgItems.length) {
    e.preventDefault();
    const files = imgItems.map(it => it.getAsFile()).filter(Boolean);
    if (inAnswerZone && files[0]) setAnswerConfirmedFile(files[0], '剪贴板答案截图');
    else if (files.length) setPaperConfirmedFiles(files, '剪贴板测试卷');
    return;
  }
  if (active && active.id === 'inputSourceDir') return;
  const txt = (e.clipboardData?.getData('text') || '').trim();
  if (txt && looksLikeLocalPath(txt) && !inAnswerZone) {
    e.preventDefault();
    $('inputSourceDir').value = txt;
    inspectPaperPathInput(true);
  }
});

async function attachStagedAnswerToBatch(batchId, staged) {
  if (!staged || !batchId) return;
  try {
    // 若是标准答案 JSON 文件直接导入生效
    if (staged.kind === 'file' && /\.json$/i.test(staged.name || '')) {
      const txt = await staged.file.text();
      const parsed = JSON.parse(txt);
      await api('/api/schema', { method: 'POST', body: JSON.stringify({ batch_id: batchId, schema: parsed }) });
      showBanner('gui', `已直接应用结构化答案 JSON：${staged.name}`, 4500);
      return;
    }
    let res;
    if (staged.kind === 'file') {
      const qs = `?batch_id=${encodeURIComponent(batchId)}&filename=${encodeURIComponent(staged.name)}`;
      const r = await fetch('/api/schema/upload-source' + qs, {
        method: 'POST',
        headers: { 'Content-Type': staged.file.type || 'application/octet-stream' },
        body: staged.file,
      });
      res = await r.json().catch(() => ({}));
      if (!r.ok || !res.ok) throw new Error(res.error || '上传答案失败');
    } else if (staged.kind === 'path') {
      res = await api('/api/schema/upload-source', {
        method: 'POST',
        body: JSON.stringify({ batch_id: batchId, source_path: staged.path }),
      });
    } else if (staged.kind === 'text') {
      res = await api('/api/schema/upload-source', {
        method: 'POST',
        body: JSON.stringify({ batch_id: batchId, filename: 'pasted_answer.txt', text_content: staged.text }),
      });
    }
    if (res && res.ok) {
      const hasAgent = Boolean(AGENTS_CACHE?.available?.length);
      if (hasAgent && res.ai_prompt) {
        openAiHelperDialog(true);
        $('aiNoteInput').value = res.ai_prompt;
        $('btnStartAi').click();
        showBanner('ai', '已把贴入的答案交给 AI 帮手整理为判分标准！', 5500);
      } else {
        showBanner('gui', '答案已随批次保存；本机无 CLI Agent，可在「判分规则」中一键走 VLM 直连解析。', 6000);
      }
    }
  } catch (err) {
    showBanner('gui', '附加答案失败（不影响试卷已入库）：' + err.message, 6000);
  }
}

async function doIngest(autoStart = false) {
  const source_dir = ($('inputSourceDir')?.value || '').trim();
  const hasFiles = STAGED_PAPER?.kind === 'files' && STAGED_PAPER.files?.length > 0;
  if (!source_dir && !hasFiles) {
    showPaperError('请先放入试卷照片文件夹，或拖入 / 粘贴一张测试试卷图');
    $('dropZonePaper')?.focus();
    return;
  }
  const useIterMeta = WORK_MODE === 'iteration';
  const subject = useIterMeta ? SELECTED_SUBJECT : '通用';
  const batch_name = useIterMeta ? ($('inputBatchName')?.value.trim() || '') : '';
  const stagedAnsCopy = STAGED_ANSWER ? { ...STAGED_ANSWER } : null;

  $('btnDoIngest').disabled = true;
  $('btnIngestAndRun').disabled = true;
  $('ingestMsg').textContent = hasFiles ? `正在导入 ${STAGED_PAPER.files.length} 张试卷...` : '正在整理去重...';
  try {
    let res;
    if (hasFiles) {
      res = await api('/api/ingest/upload', {
        method: 'POST',
        body: JSON.stringify({
          files: STAGED_PAPER.files.map(f => ({ name: f.name, data_b64: f.data_b64 })),
          subject,
          batch_name,
        }),
      });
    } else {
      res = await api('/api/ingest', {
        method: 'POST',
        body: JSON.stringify({ source_dir, subject, batch_name }),
      });
    }
    $('ingestMsg').textContent = res.message;
    clearStagedPaper(true);
    clearStagedAnswer(true);
    const newBatchId = res.batch?.batch_id || '';
    if (stagedAnsCopy && newBatchId) {
      await attachStagedAnswerToBatch(newBatchId, stagedAnsCopy);
    }
    await refreshOverview();
    if (autoStart) {
      await triggerRecognize();
    } else {
      setView('overview', { animate: true, originPt: clickOriginFromEvent(null, $('btnDoIngest')) });
    }
  } catch (err) {
    $('ingestMsg').textContent = '失败：' + err.message;
    alert(err.message);
  } finally {
    $('btnDoIngest').disabled = false;
    $('btnIngestAndRun').disabled = false;
  }
}
$('btnIngestAndRun').onclick = () => doIngest(true);
$('btnDoIngest').onclick = () => doIngest(false);

async function triggerRecognize() {
  $('btnStartRecognize').disabled = true;
  try {
    await api('/api/pipeline/run', { method: 'POST', body: '{}' });
    ARCH_COLLAPSED = false;
    setView('overview');
    await refreshOverview();
  } catch (err) {
    setView('overview');
    await refreshOverview();
    alert('无法启动识别：\n' + err.message);
  }
}
$('btnStartRecognize').onclick = triggerRecognize;
$('btnStopRecognize').onclick = async () => {
  if (!confirm('确认停止当前自动识别？（已完成页会保留，下次可断点续跑）')) return;
  $('btnStopRecognize').disabled = true;
  try {
    const res = await api('/api/pipeline/stop', { method: 'POST', body: '{}' });
    showBanner('gui', res.message || '已停止识别');
    await refreshOverview();
  } catch (err) { alert(err.message); }
  finally { $('btnStopRecognize').disabled = false; }
};
$('btnRetryFailed').onclick = async () => {
  const pf = Number(((OVERVIEW?.recognition_summary?.stats) || {}).page_failures || 0);
  const tip = pf > 0
    ? `将仅补跑失败的 ${pf} 页（已成功的页面与已转录题块自动复用，不重复扣费），是否继续？`
    : '仅补跑失败或未转录的题块？（已成功项不重复扣费）';
  if (!confirm(tip)) return;
  $('btnRetryFailed').disabled = true;
  try {
    await api('/api/pipeline/retry-failed', { method: 'POST', body: '{}' });
    ARCH_COLLAPSED = false;
    await refreshOverview();
  } catch (err) { alert('补跑失败：' + err.message); }
  finally { $('btnRetryFailed').disabled = false; }
};

$('batchSelect').onchange = async () => {
  if (!$('batchSelect').value) return;
  try {
    await api('/api/batch/select', { method: 'POST', body: JSON.stringify({ batch_id: $('batchSelect').value }) });
    await refreshOverview();
  } catch (err) { alert(err.message); }
};

$('btnCopyLan').onclick = async () => {
  const txt = $('lanUrlText').textContent;
  try { await navigator.clipboard.writeText(txt); showBanner('gui', '已复制：' + txt); }
  catch (_) { prompt('请复制局域网网址：', txt); }
};

// 新手引导
function focusAnatomy(zone) {
  document.querySelectorAll('.guide-item').forEach(el => el.classList.toggle('active', el.dataset.zone === zone));
  const map = { review: 'zoneReview', choice: 'zoneChoice', box: 'zoneBox' };
  Object.entries(map).forEach(([k, id]) => {
    const g = $(id);
    if (!g) return;
    g.classList.toggle('lit', k === zone);
    g.classList.toggle('dim', k !== zone);
  });
}
document.querySelectorAll('.guide-item').forEach(el => {
  el.addEventListener('mouseenter', () => focusAnatomy(el.dataset.zone));
  el.addEventListener('click', () => focusAnatomy(el.dataset.zone));
});
$('btnOpenGuide').onclick = () => { focusAnatomy('review'); $('guideDialog').showModal(); };
$('btnCloseGuide').onclick = () => { $('guideDialog').close(); localStorage.setItem('wbGuideSeen', '1'); };

let TOUR_STEPS = [], TOUR_IDX = 0;
function buildTourSteps() {
  if (OVERVIEW?.is_host) {
    return [
      { view: 'overview', el: 'tabIngest', title: '① 导入试卷', desc: '选照片文件夹和学科，一键入库并自动识别。' },
      { view: 'overview', el: 'batchCard', title: '② 看实时数据流向', desc: '架构图实时展示每一步进度，数据包沿连线真实流动。' },
      { view: 'overview', el: 'task_review', title: '③ 进去人工复审', desc: '滑过填空框看答案，点框改字，按 E 调框，按 D 翻页。' },
    ];
  }
  return [
    { view: 'overview', el: 'btnUser', title: '① 填写称呼', desc: '用来统计每个人的审阅进度。' },
    { view: 'overview', el: 'task_review', title: '② 点击进去审', desc: '自动领取无人占用的下一张试卷。' },
  ];
}
function showTourStep(idx) {
  TOUR_IDX = idx;
  const st = TOUR_STEPS[idx];
  if (!st) { endTour(); return; }
  if (st.view) setView(st.view);
  const target = $(st.el);
  if (!target) { showTourStep(idx + 1); return; }
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => {
    const r = target.getBoundingClientRect(), pad = 6;
    const box = $('tourBox');
    box.style.left = (r.left - pad) + 'px';
    box.style.top = (r.top - pad) + 'px';
    box.style.width = (r.width + pad * 2) + 'px';
    box.style.height = (r.height + pad * 2) + 'px';
    const pop = $('tourPop');
    const popTop = r.bottom + 14 + 150 < window.innerHeight ? (r.bottom + 14) : Math.max(14, r.top - 165);
    pop.style.top = popTop + 'px';
    pop.style.left = Math.max(14, Math.min(window.innerWidth - 336, r.left)) + 'px';
    $('tourTitle').textContent = st.title;
    $('tourDesc').textContent = st.desc;
    $('tourStep').textContent = `${idx + 1} / ${TOUR_STEPS.length}`;
    $('tourNext').textContent = idx === TOUR_STEPS.length - 1 ? '完成' : '下一步 →';
  }, 140);
}
function endTour() { $('tourMask').classList.add('hidden'); localStorage.setItem('wbGuideSeen', '1'); }
$('btnStartTour').onclick = () => { $('guideDialog').close(); TOUR_STEPS = buildTourSteps(); $('tourMask').classList.remove('hidden'); showTourStep(0); };
$('tourNext').onclick = () => showTourStep(TOUR_IDX + 1);
$('tourSkip').onclick = endTour;

// 大模型 API 配置弹窗
let VLM_PRESETS_MAP = {};
async function loadVlmPricePreview() {
  const model = $('vlmModel').value.trim();
  const provider = $('vlmProvider').value;
  const el = $('vlmPriceText');
  if (!model) { el.textContent = '填入模型名称后自动匹配官网单价'; return; }
  try {
    const res = await api(`/api/pricing?model=${encodeURIComponent(model)}&provider=${encodeURIComponent(provider)}`);
    const p = res.price;
    if (p) {
      const src = p.source === 'official_crawl' ? '官网永久缓存' : (p.source === 'manual' ? '自定义' : '内置参考');
      const cacheBit = (p.cache_hit_per_m !== null && p.cache_hit_per_m !== undefined) ? ` · 缓存 ¥${p.cache_hit_per_m}` : '';
      el.textContent = `单价：输入 ¥${p.input_per_m} / 输出 ¥${p.output_per_m}${cacheBit}（每百万 · ${src}）`;
      $('vlmPriceIn').value = p.input_per_m;
      $('vlmPriceOut').value = p.output_per_m;
    } else {
      el.textContent = `未收录 ${model}，可点「更新官网价格」或「手动填价」`;
      $('vlmPriceIn').value = '';
      $('vlmPriceOut').value = '';
    }
  } catch (_) {
    el.textContent = '定价读取失败';
  }
}
async function openVlmDialog() {
  $('vlmStatus').textContent = '';
  $('vlmApiKey').value = '';
  $('vlmCustomPriceBox').classList.add('hidden');
  try {
    const cfg = await api('/api/vlm/config');
    VLM_PRESETS_MAP = {};
    const sel = $('vlmProvider');
    sel.innerHTML = (cfg.presets || []).map(p => {
      VLM_PRESETS_MAP[p.id] = p;
      return `<option value="${esc(p.id)}">${esc(p.label)}</option>`;
    }).join('');
    sel.value = cfg.provider || 'doubao';
    $('vlmBaseUrl').value = cfg.base_url || '';
    $('vlmModel').value = cfg.model || '';
    $('vlmNote').textContent = (VLM_PRESETS_MAP[sel.value] && VLM_PRESETS_MAP[sel.value].note) || '';
    $('vlmKeyHint').textContent = cfg.has_key ? `（已存 ${cfg.api_key_masked}）` : '（未配置）';
    $('vlmDialog').showModal();
    loadVlmPricePreview();
  } catch (err) { alert('读取 API 配置失败：' + err.message); }
}
$('btnOpenVlm').onclick = openVlmDialog;
$('btnCloseVlm').onclick = () => $('vlmDialog').close();
$('vlmProvider').onchange = () => {
  const p = VLM_PRESETS_MAP[$('vlmProvider').value] || {};
  $('vlmNote').textContent = p.note || '';
  if (p.id !== 'custom') {
    $('vlmBaseUrl').value = p.base_url || '';
    $('vlmModel').value = p.model || '';
  } else if (!$('vlmBaseUrl').value || Object.values(VLM_PRESETS_MAP).some(x => x.id !== 'custom' && x.base_url === $('vlmBaseUrl').value)) {
    $('vlmBaseUrl').value = '';
  }
  loadVlmPricePreview();
};
$('vlmModel').addEventListener('change', loadVlmPricePreview);
$('btnToggleCustomPrice').onclick = () => $('vlmCustomPriceBox').classList.toggle('hidden');
$('btnRefreshPrice').onclick = async () => {
  const btn = $('btnRefreshPrice');
  btn.disabled = true;
  $('vlmPriceText').textContent = '正在从各官网抓取最新定价...';
  try {
    const res = await api('/api/pricing/refresh', {
      method: 'POST',
      body: JSON.stringify({ model: $('vlmModel').value.trim(), provider: $('vlmProvider').value }),
    });
    $('vlmStatus').style.color = 'var(--primary)';
    $('vlmStatus').textContent = '已完成：' + (res.message || '官网定价已永久更新');
    await loadVlmPricePreview();
    await refreshOverview();
  } catch (err) {
    $('vlmStatus').style.color = 'var(--err)';
    $('vlmStatus').textContent = '抓取失败：' + err.message;
  } finally { btn.disabled = false; }
};
$('btnSaveCustomPrice').onclick = async () => {
  const model = $('vlmModel').value.trim();
  const pin = Number($('vlmPriceIn').value);
  const pout = Number($('vlmPriceOut').value);
  if (!model || isNaN(pin) || isNaN(pout) || pin < 0 || pout < 0) {
    $('vlmStatus').style.color = 'var(--err)';
    $('vlmStatus').textContent = '请填写有效的模型名称与非负单价';
    return;
  }
  try {
    const res = await api('/api/pricing/override', {
      method: 'POST',
      body: JSON.stringify({ model, input_per_m: pin, output_per_m: pout }),
    });
    $('vlmStatus').style.color = 'var(--primary)';
    $('vlmStatus').textContent = '已保存：' + (res.message || '已永久保存单价');
    $('vlmCustomPriceBox').classList.add('hidden');
    await loadVlmPricePreview();
    await refreshOverview();
  } catch (err) {
    $('vlmStatus').style.color = 'var(--err)';
    $('vlmStatus').textContent = '保存失败：' + err.message;
  }
};
$('btnTestVlm').onclick = async () => {
  $('btnTestVlm').disabled = true;
  $('vlmStatus').style.color = 'var(--muted)';
  $('vlmStatus').textContent = '正在测试视觉连通性...';
  try {
    const res = await api('/api/vlm/test', {
      method: 'POST',
      body: JSON.stringify({
        provider: $('vlmProvider').value,
        base_url: $('vlmBaseUrl').value.trim(),
        model: $('vlmModel').value.trim(),
        api_key: $('vlmApiKey').value.trim(),
      }),
    });
    $('vlmStatus').style.color = 'var(--primary)';
    $('vlmStatus').textContent = `连通成功${res.vision_verified ? '（支持看图）' : ''} · ${res.latency_ms}ms · ${res.returned_model || ''}${res.warning ? ' · ' + res.warning : ''}`;
  } catch (err) {
    $('vlmStatus').style.color = 'var(--err)';
    $('vlmStatus').textContent = '连通失败：' + err.message;
  } finally { $('btnTestVlm').disabled = false; }
};
$('btnSaveVlm').onclick = async () => {
  $('btnSaveVlm').disabled = true;
  $('vlmStatus').textContent = '';
  try {
    const res = await api('/api/vlm/config', {
      method: 'POST',
      body: JSON.stringify({
        provider: $('vlmProvider').value,
        base_url: $('vlmBaseUrl').value.trim(),
        model: $('vlmModel').value.trim(),
        api_key: $('vlmApiKey').value.trim(),
      }),
    });
    $('vlmStatus').style.color = 'var(--primary)';
    $('vlmStatus').textContent = res.message || '已保存';
    if (res.config) $('vlmKeyHint').textContent = res.config.has_key ? `（已存 ${res.config.api_key_masked}）` : '（未配置）';
    $('vlmApiKey').value = '';
    if (CHECK) renderCheck(await api('/api/check'));
  } catch (err) {
    $('vlmStatus').style.color = 'var(--err)';
    $('vlmStatus').textContent = '保存失败：' + err.message;
  } finally { $('btnSaveVlm').disabled = false; }
};

