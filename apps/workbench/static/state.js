'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

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

function setView(view) {
  if (!OVERVIEW?.is_host && view !== 'overview') view = 'overview';
  CURRENT_VIEW = view;
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
  requestAnimationFrame(() => {
    if (view === 'check') drawEnvArchConnectors();
    if (view === 'overview') PipeFlow.relayout();
  });
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
  return {
    client_id: CLIENT_ID,
    view: CURRENT_VIEW,
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
    if (st.view && ['check', 'ingest', 'overview', 'train'].includes(st.view)) {
      setView(st.view);
      if (st.view === 'check' && !CHECK) renderCheck(await api('/api/check'));
      if (st.view === 'train') await refreshTrainOverview();
    }
    if (typeof st.arch_collapsed === 'boolean') {
      ARCH_COLLAPSED = st.arch_collapsed;
      applyArchCollapseUI();
    }
    if (st.scroll_y) window.scrollTo(0, Number(st.scroll_y) || 0);
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
    if (tag) tag.textContent = '🤖 ' + (label || 'AI 聚焦');
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

