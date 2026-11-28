'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
if (typeof hydrateIcons === 'function') hydrateIcons();

const CLIENT_ID = (() => {
  let id = sessionStorage.getItem('wbClientId');
  if (!id) { id = 'u_' + Math.random().toString(36).slice(2, 10); sessionStorage.setItem('wbClientId', id); }
  return id;
})();

let OVERVIEW = null;
let CHECK = null;
let CURRENT_VIEW = 'overview';
let SELECTED_SUBJECT = localStorage.getItem('wbLastSubject') || '物理';
let INITIALIZED = false;
let ARCH_COLLAPSED = true;
let ARCH_USER_EXPANDED = false;
let OVERVIEW_INFLIGHT = false;
let OVERVIEW_TIMER = null;
let OVERVIEW_TIMER_MS = 0;

function getUserName() { return (localStorage.getItem('reviewUserName') || '').trim(); }
function setUserName(name) {
  localStorage.setItem('reviewUserName', name.trim());
  const lbl = $('btnUserLabel');
  if (lbl) lbl.textContent = name.trim() || '填写称呼';
  pingPresence('总览');
}

function showBanner(source, text, durationMs = 4200) {
  $('liveSource').textContent = source === 'ai' ? 'AI 联动' : (source === 'cli' ? 'CLI 联动' : '实时');
  $('liveText').textContent = text;
  $('liveBanner').classList.add('show');
  clearTimeout(showBanner._t);
  if (durationMs > 0) showBanner._t = setTimeout(() => $('liveBanner').classList.remove('show'), durationMs);
}

function highlightElement(id, durationMs = 2600) {
  const el = $(id);
  if (!el) return;
  el.classList.add('spotlight');
  clearTimeout(el._st);
  el._st = setTimeout(() => el.classList.remove('spotlight'), durationMs);
}

let ACTIVE_VT = null;

function clickOriginFromEvent(ev, fallbackEl = null) {
  if (ev && Number.isFinite(ev.clientX) && Number.isFinite(ev.clientY) && (ev.clientX !== 0 || ev.clientY !== 0)) {
    return { x: ev.clientX, y: ev.clientY };
  }
  const el = fallbackEl || ev?.currentTarget || null;
  if (el && typeof el.getBoundingClientRect === 'function') {
    const r = el.getBoundingClientRect();
    if (r.width > 0 || r.height > 0) return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  return { x: window.innerWidth * 0.5, y: 56 };
}

function playFallbackWave(tone, originPt) {
  const wave = $('modeWaveOverlay');
  if (!wave) return;
  const wx = Number.isFinite(originPt?.x) ? originPt.x : window.innerWidth * 0.5;
  const wy = Number.isFinite(originPt?.y) ? originPt.y : 56;
  wave.style.setProperty('--wx', Math.round(wx) + 'px');
  wave.style.setProperty('--wy', Math.round(wy) + 'px');
  wave.className = 'mode-wave ' + (tone === 'cool' ? 'cool' : 'warm');
  void wave.offsetWidth;
  wave.classList.add('play');
  clearTimeout(wave._wt);
  wave._wt = setTimeout(() => wave.classList.remove('play'), 540);
}

/**
 * 高帧率零重排圆形水波揭屏引擎（供模式切换与步骤切换共用）：
 * - 切换瞬间锁定 CSS 过渡（html.vt-active），确保新快照仅光栅化 1 次、全程 GPU 合成层动画
 * - 连续快速点击自动安全中断上一次动画（skipTransition），杜绝排队掉帧
 * - 降级路径使用纯 transform:scale + opacity 合成层波纹
 */
function runCircularTransition({ originPt = null, durationMs = 380, waveTone = 'warm', mutateDOM, fallbackAfter = null }) {
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion || IS_MIRROR_MODE || typeof mutateDOM !== 'function') {
    if (typeof mutateDOM === 'function') mutateDOM();
    return;
  }
  const wx = Number.isFinite(originPt?.x) ? originPt.x : window.innerWidth * 0.5;
  const wy = Number.isFinite(originPt?.y) ? originPt.y : 56;

  if (typeof document.startViewTransition === 'function') {
    if (ACTIVE_VT) {
      try { ACTIVE_VT.skipTransition(); } catch (_) {}
      ACTIVE_VT = null;
    }
    const maxR = Math.hypot(Math.max(wx, window.innerWidth - wx), Math.max(wy, window.innerHeight - wy));
    document.documentElement.classList.add('vt-active');
    let vt;
    try {
      vt = document.startViewTransition(() => {
        mutateDOM();
      });
    } catch (_) {
      document.documentElement.classList.remove('vt-active');
      mutateDOM();
      return;
    }
    ACTIVE_VT = vt;
    vt.ready.then(() => {
      if (ACTIVE_VT !== vt) return;
      document.documentElement.animate(
        { clipPath: [`circle(0px at ${wx.toFixed(0)}px ${wy.toFixed(0)}px)`, `circle(${maxR.toFixed(0)}px at ${wx.toFixed(0)}px ${wy.toFixed(0)}px)`] },
        { duration: durationMs, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', pseudoElement: '::view-transition-new(root)' }
      );
    }).catch(() => {});
    vt.finished.finally(() => {
      if (ACTIVE_VT === vt) ACTIVE_VT = null;
      document.documentElement.classList.remove('vt-active');
    });
    return;
  }

  mutateDOM();
  if (typeof fallbackAfter === 'function') fallbackAfter();
  playFallbackWave(waveTone, { x: wx, y: wy });
}

function applyViewDOM(view) {
  CURRENT_VIEW = view;
  document.body.dataset.view = view;
  $('viewCheck').classList.toggle('hidden', view !== 'check');
  $('viewIngest').classList.toggle('hidden', view !== 'ingest');
  $('viewOverview').classList.toggle('hidden', view !== 'overview');
  $('viewTrain').classList.toggle('hidden', view !== 'train');
  if ($('btnBackFromCheck')) {
    $('btnBackFromCheck').classList.toggle('hidden', !(view === 'check' && WORK_MODE === 'grading'));
  }
  document.querySelectorAll('.step-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  // 同步预排 SVG 连线，确保进入新快照前箭头已完全就位，水波展开期间零重排零闪现
  if (view === 'check' && typeof drawEnvArchConnectors === 'function') drawEnvArchConnectors();
  if (view === 'overview' && typeof PipeFlow !== 'undefined') PipeFlow.relayout();
}

function setView(view, opts = null) {
  if (!OVERVIEW?.is_host && view !== 'overview') view = 'overview';
  const animate = Boolean(opts && opts.animate && view !== CURRENT_VIEW);
  if (animate) {
    const tone = (typeof WORK_MODE === 'string' && WORK_MODE === 'iteration') ? 'cool' : 'warm';
    runCircularTransition({
      originPt: opts.originPt || null,
      durationMs: opts.durationMs || 380,
      waveTone: tone,
      mutateDOM: () => applyViewDOM(view),
    });
  } else {
    applyViewDOM(view);
    requestAnimationFrame(() => {
      if (view === 'check' && typeof drawEnvArchConnectors === 'function') drawEnvArchConnectors();
      if (view === 'overview' && typeof PipeFlow !== 'undefined') PipeFlow.relayout();
    });
  }
  scheduleReportUiState();
}

/* ==================== 前台 UI 状态同步（离屏一致渲染）与 AI 焦点投送 ==================== */
const URL_PARAMS = new URLSearchParams(window.location.search);
const IS_MIRROR_MODE = URL_PARAMS.get('mirror') === '1';
const MIRROR_CLIENT_ID = URL_PARAMS.get('mirror_client') || '';
window.__RECENT_ERRORS__ = [];
window.__MIRROR_APPLIED__ = !IS_MIRROR_MODE;
window.addEventListener('error', e => {
  window.__RECENT_ERRORS__.push(`JS错误: ${e.message || 'unknown'}`);
  if (window.__RECENT_ERRORS__.length > 10) window.__RECENT_ERRORS__.shift();
});
window.addEventListener('unhandledrejection', e => {
  window.__RECENT_ERRORS__.push(`Promise异常: ${e.reason?.message || e.reason || 'unknown'}`);
  if (window.__RECENT_ERRORS__.length > 10) window.__RECENT_ERRORS__.shift();
});

let _uiStateTimer = null;
function collectWbUiState() {
  let openDlg = '';
  if ($('aiDrawer') && !$('aiDrawer').classList.contains('hidden')) openDlg = 'ai';
  else if ($('vlmDialog')?.open) openDlg = 'vlm';
  else if ($('lanDialog')?.open) openDlg = 'lan';
  else if ($('schemaRuleDialog')?.open) openDlg = 'schema';
  else if ($('memPreflightDialog')?.open) openDlg = 'mem';
  else if ($('guideDialog')?.open) openDlg = 'guide';
  return {
    client_id: CLIENT_ID,
    view: CURRENT_VIEW,
    work_mode: typeof WORK_MODE === 'string' ? WORK_MODE : 'grading',
    dialog: openDlg,
    batch_id: OVERVIEW?.active_batch?.batch_id || '',
    scroll_y: Math.round(window.scrollY || 0),
    arch_collapsed: ARCH_COLLAPSED,
    recent_errors: window.__RECENT_ERRORS__.slice(-6),
  };
}
function scheduleReportUiState() {
  if (IS_MIRROR_MODE || !OVERVIEW?.is_host) return;
  clearTimeout(_uiStateTimer);
  _uiStateTimer = setTimeout(() => {
    api('/api/ui-state', { method: 'POST', body: JSON.stringify(collectWbUiState()) }).catch(() => {});
  }, 220);
}
async function applyMirrorUiStateIfNeeded() {
  if (!IS_MIRROR_MODE) return;
  try {
    const qs = MIRROR_CLIENT_ID ? `?client_id=${encodeURIComponent(MIRROR_CLIENT_ID)}` : '';
    const res = await api('/api/ui-state' + qs);
    const st = res?.active || {};
    const qMode = URL_PARAMS.get('work_mode') || '';
    const qView = URL_PARAMS.get('view') || '';
    const qDlg = URL_PARAMS.get('dialog') || '';
    const qExpandArch = URL_PARAMS.get('expand_arch') === '1';
    const qHoverDep = URL_PARAMS.get('hover_dep') || '';
    const qHoverNode = URL_PARAMS.get('hover_node') || '';

    const targetMode = (qMode === 'grading' || qMode === 'iteration')
      ? qMode
      : ((st.work_mode === 'grading' || st.work_mode === 'iteration') ? st.work_mode : '');
    if (targetMode && typeof applyWorkModeUI === 'function') {
      applyWorkModeUI(targetMode, false);
    }

    const targetView = ['check', 'ingest', 'overview', 'train'].includes(qView)
      ? qView
      : (['check', 'ingest', 'overview', 'train'].includes(st.view) ? st.view : '');
    if (targetView) {
      setView(targetView);
      if (targetView === 'check') {
        if (!CHECK) renderCheck(await api('/api/check'));
        if (qHoverDep && typeof highlightEnvByDep === 'function') highlightEnvByDep(qHoverDep);
        else if (qHoverNode && typeof highlightEnvByNode === 'function') highlightEnvByNode(qHoverNode);
      }
      if (targetView === 'train') await refreshTrainOverview();
    }

    if (qExpandArch) {
      ARCH_COLLAPSED = false;
      ARCH_USER_EXPANDED = true;
      applyArchCollapseUI();
    } else if (typeof st.arch_collapsed === 'boolean') {
      ARCH_COLLAPSED = st.arch_collapsed;
      applyArchCollapseUI();
    }

    const targetDlg = qDlg || st.dialog || '';
    if (targetDlg === 'ai' && typeof openAiHelperDialog === 'function') {
      openAiHelperDialog(true);
    } else if (targetDlg === 'vlm' && typeof openVlmDialog === 'function') {
      await openVlmDialog();
    } else if (targetDlg === 'lan') {
      $('lanDialog')?.showModal();
    } else if (targetDlg === 'schema') {
      $('schemaRuleDialog')?.showModal();
    } else if (targetDlg === 'mem') {
      $('memPreflightDialog')?.showModal();
    } else if (targetDlg === 'guide') {
      if (typeof focusAnatomy === 'function') focusAnatomy('review');
      $('guideDialog')?.showModal();
    }

    if (!qView && st.scroll_y) window.scrollTo(0, Number(st.scroll_y) || 0);
  } catch (_) {}
  window.__MIRROR_APPLIED__ = true;
}

let _focusRingTimer = null;
function castFocusOnElement(selector, label = '') {
  if (!selector) return false;
  const el = document.querySelector(selector);
  if (!el) return false;
  el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
  setTimeout(() => {
    const r = el.getBoundingClientRect();
    const ring = $('aiFocusRing'), tag = $('aiFocusTag');
    if (!ring || r.width < 2 || r.height < 2) return;
    ring.style.left = Math.max(2, r.left - 5) + 'px';
    ring.style.top = Math.max(2, r.top - 5) + 'px';
    ring.style.width = (r.width + 10) + 'px';
    ring.style.height = (r.height + 10) + 'px';
    if (tag) tag.innerHTML = `${uiIcon('sparkles', 'ui-ico-xs')}<span>${esc(label || 'AI 聚焦')}</span>`;
    ring.classList.remove('hidden');
    clearTimeout(_focusRingTimer);
    _focusRingTimer = setTimeout(() => ring.classList.add('hidden'), 4200);
  }, 180);
  return true;
}
function handleUiCommand(ev) {
  if (!ev || IS_MIRROR_MODE) return;
  if (ev.view && ['check', 'ingest', 'overview', 'train'].includes(ev.view)) {
    setView(ev.view);
    if (ev.view === 'check' && !CHECK) api('/api/check').then(renderCheck).catch(() => {});
    if (ev.view === 'train') refreshTrainOverview();
  }
  if (ev.action === 'focus' && ev.target) {
    castFocusOnElement(ev.target, ev.label || 'AI 聚焦');
  } else if (ev.action === 'click' && ev.target) {
    castFocusOnElement(ev.target, ev.label || 'AI 操作');
    const el = document.querySelector(ev.target);
    if (el) setTimeout(() => el.click(), 320);
  }
  if (ev.message) showBanner('ai', ev.message, 4200);
}

async function api(url, opts = {}) {
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    cache: 'no-store',
    ...opts,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败 (${r.status})`);
  return data;
}

