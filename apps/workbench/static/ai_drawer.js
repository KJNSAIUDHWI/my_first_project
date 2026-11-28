'use strict';
/* ==================== 全局非阻塞 AI 帮手抽屉（多 Agent + 多会话并发 + 配置导入） ==================== */
let AI_SESSIONS = {};
let ACTIVE_CHAT_ID = '';
let AGENTS_CACHE = null;

function syncAgentsUI(agentsInfo, cfg) {
  AGENTS_CACHE = agentsInfo;
  const sel = $('aiProviderSelect');
  if (sel && agentsInfo?.list) {
    const curVal = sel.value || cfg?.ai_helper_provider || 'auto';
    sel.innerHTML = `<option value="auto">自动优选 (${esc(agentsInfo.recommended || '无')})</option>` +
      agentsInfo.list.map(a => `<option value="${esc(a.id)}">${a.available ? '[可用] ' : '[未装] '}${esc(a.label)}</option>`).join('');
    sel.value = curVal;
  }
  if ($('aiModeSelect') && cfg?.ai_default_mode && !$('aiModeSelect').dataset.userPicked) {
    $('aiModeSelect').value = cfg.ai_default_mode;
  }
  if ($('chkAiAttachShot') && typeof cfg?.ai_default_screenshot === 'boolean' && !$('chkAiAttachShot').dataset.userPicked) {
    $('chkAiAttachShot').checked = cfg.ai_default_screenshot;
  }
  updateAiProviderHint();
}

function updateAiProviderHint() {
  const hint = $('aiProviderHint');
  if (!hint) return;
  const info = AGENTS_CACHE;
  const chosen = $('aiProviderSelect')?.value || 'auto';
  const targetId = chosen === 'auto' ? (info?.recommended || '') : chosen;
  const ag = info?.agents?.[targetId];
  if (ag && ag.available) {
    hint.textContent = `当前引擎：${ag.label}（${ag.cli}）· 支持同构操控界面、导入配置/密钥与离屏截图`;
  } else {
    hint.textContent = '未检测到所选 CLI Agent，点右上「配置」可手动填路径或执行可选扩展扫描，也可直接复制提示词。';
  }
  if ($('aiManualPathInput') && chosen !== 'auto') {
    const cfgPaths = OVERVIEW?.config?.agent_paths || {};
    $('aiManualPathInput').value = chosen === 'custom'
      ? (OVERVIEW?.config?.agent_custom_cmd || '')
      : (cfgPaths[chosen] || '');
  }
}

function syncAiSessions(list) {
  if (!Array.isArray(list)) return;
  for (const s of list) {
    if (s && s.chat_id) AI_SESSIONS[s.chat_id] = s;
  }
  if (!ACTIVE_CHAT_ID || !AI_SESSIONS[ACTIVE_CHAT_ID]) {
    const ids = Object.keys(AI_SESSIONS);
    if (ids.length) ACTIVE_CHAT_ID = ids[0];
  }
  renderAiTabs();
  if (ACTIVE_CHAT_ID && AI_SESSIONS[ACTIVE_CHAT_ID]) renderAiPanel(AI_SESSIONS[ACTIVE_CHAT_ID]);
}

function upsertAiSessionSnapshot(snap) {
  if (!snap || !snap.chat_id) return;
  AI_SESSIONS[snap.chat_id] = snap;
  if (!ACTIVE_CHAT_ID) ACTIVE_CHAT_ID = snap.chat_id;
  renderAiTabs();
  if (snap.chat_id === ACTIVE_CHAT_ID) renderAiPanel(snap);
}

function renderAiTabs() {
  const bar = $('aiTabsBar');
  if (!bar) return;
  const sessions = Object.values(AI_SESSIONS);
  const tabsHtml = sessions.map((s, idx) => {
    const active = s.chat_id === ACTIVE_CHAT_ID ? ' active' : '';
    const dot = s.running ? '<i class="dot-run"></i>' : '';
    const lbl = esc(s.title || `对话 ${idx + 1}`);
    return `<button class="ai-tab${active}" type="button" data-chat-id="${esc(s.chat_id)}">${dot}${lbl}</button>`;
  }).join('');
  bar.innerHTML = tabsHtml + `<button class="btn xs" id="btnNewAiChat" type="button" title="新建独立并发对话（与已有对话并发执行）">＋ 新并发对话</button>`;
  bar.querySelectorAll('[data-chat-id]').forEach(b => {
    b.onclick = () => {
      ACTIVE_CHAT_ID = b.dataset.chatId;
      renderAiTabs();
      renderAiPanel(AI_SESSIONS[ACTIVE_CHAT_ID]);
    };
  });
  const btnNew = $('btnNewAiChat');
  if (btnNew) btnNew.onclick = createNewConcurrentChat;
}

async function createNewConcurrentChat() {
  try {
    const res = await api('/api/ai/sessions/new', {
      method: 'POST',
      body: JSON.stringify({
        surface: 'workbench',
        provider: $('aiProviderSelect')?.value || 'auto',
        mode: $('aiModeSelect')?.value || 'yolo',
      }),
    });
    if (res.session) {
      AI_SESSIONS[res.session.chat_id] = res.session;
      ACTIVE_CHAT_ID = res.session.chat_id;
      renderAiTabs();
      renderAiPanel(res.session);
    }
  } catch (err) { showBanner('ai', '创建对话失败：' + err.message); }
}

function renderAiPanel(aiJob) {
  if (!aiJob) return;
  const running = Boolean(aiJob.running);
  const st = aiJob.status || 'idle';
  const badge = $('aiStatusBadge');
  if (badge) {
    badge.className = 'badge ' + (running ? 'ok' : (st === 'done' ? 'ok' : (st === 'error' ? 'err' : 'warn')));
    badge.textContent = running ? (aiJob.queued ? `执行中(排队${aiJob.queued})` : '执行中...') : (st === 'done' ? '已完成' : (st === 'error' ? '异常' : (st === 'stopped' ? '已停止' : '就绪')));
  }
  if ($('btnStopAi')) $('btnStopAi').classList.toggle('hidden', !running);
  const meta = [];
  if (aiJob.provider_label || aiJob.provider) meta.push(`引擎：${aiJob.provider_label || aiJob.provider}`);
  if (aiJob.mode) meta.push(`权限：${aiJob.mode}`);
  if (aiJob.session_id) meta.push(`会话：${aiJob.session_id.slice(0, 14)}`);
  const u = aiJob.usage || {};
  if (u.inputTokens || u.outputTokens) meta.push(`Token：${u.inputTokens || 0}/${u.outputTokens || 0}`);
  if ($('aiMetaBar')) $('aiMetaBar').textContent = meta.join(' · ');

  const body = $('aiChatBody');
  if (!body) return;
  const msgs = Array.isArray(aiJob.messages) ? aiJob.messages : [];
  if (!msgs.length && !running && !aiJob.response && !aiJob.error) {
    body.innerHTML = `<div id="aiResponseBox" class="ai-msg assistant">直接用中文下达指令即可（默认完全权限，支持查错、操控界面、导入配置/密钥、修代码并同步 GitHub）。</div>`;
    return;
  }
  const html = msgs.map(m => {
    const att = (m.attach && m.attach.length) ? ` · ${uiIcon('camera', 'ui-ico-xs')} ${esc(m.attach.join(', '))}` : '';
    return `<div class="ai-msg ${m.role === 'user' ? 'user' : 'assistant'}"><div class="ai-msg-meta">${m.role === 'user' ? '你' : 'AI 帮手'} · ${esc(m.ts || '')}${att}</div>${esc(m.text || '')}</div>`;
  });
  if (running) {
    const liveTxt = aiJob.response || 'AI 正在读取实时状态、分析并执行同构操作，你可以继续自由操作主界面...';
    html.push(`<div id="aiResponseBox" class="ai-msg assistant"><div class="ai-msg-meta">AI 帮手 · 执行中</div>${esc(liveTxt)}</div>`);
  }
  body.innerHTML = html.join('');
  body.scrollTop = body.scrollHeight;
}

/* ==================== 可自由拖动 + 可缩放 AI 帮手浮窗（复用共享 draggable_drawer.js） ==================== */
const WB_AI_DRAWER_CTRL = typeof window.initDraggableDrawer === 'function'
  ? window.initDraggableDrawer({
      drawer: $('aiDrawer'),
      handle: $('aiDrawerHead'),
      resizeHandle: $('aiResizeHandle'),
      storageKey: 'wbAiDrawerRect',
      minWidth: 340,
      minHeight: 360,
      defaultWidth: 430,
    })
  : { sync() {}, reset() {} };

function openAiHelperDialog(forceOpen = false) {
  const drawer = $('aiDrawer');
  if (!drawer) return;
  if (forceOpen) drawer.classList.remove('hidden');
  else drawer.classList.toggle('hidden');
  if (!drawer.classList.contains('hidden')) {
    WB_AI_DRAWER_CTRL.sync();
    if (!AGENTS_CACHE) api('/api/agents').then(r => syncAgentsUI(r, OVERVIEW?.config)).catch(() => {});
    api('/api/ai/sessions').then(r => syncAiSessions(r.sessions || [])).catch(() => {});
    updateAiProviderHint();
    setTimeout(() => $('aiNoteInput')?.focus(), 80);
  }
}

if ($('btnOpenAi')) $('btnOpenAi').onclick = () => openAiHelperDialog(false);
$('btnAskAgent').onclick = () => openAiHelperDialog(true);
$('btnCloseAiDlg').onclick = () => $('aiDrawer')?.classList.add('hidden');
if ($('btnToggleAiCfg')) $('btnToggleAiCfg').onclick = () => $('aiCfgBox')?.classList.toggle('hidden');
if ($('aiProviderSelect')) {
  $('aiProviderSelect').onchange = () => {
    updateAiProviderHint();
    api('/api/config', { method: 'POST', body: JSON.stringify({ ai_helper_provider: $('aiProviderSelect').value }) }).catch(() => {});
  };
}
if ($('aiModeSelect')) {
  $('aiModeSelect').onchange = () => {
    $('aiModeSelect').dataset.userPicked = '1';
    api('/api/config', { method: 'POST', body: JSON.stringify({ ai_default_mode: $('aiModeSelect').value }) }).catch(() => {});
  };
}
if ($('chkAiAttachShot')) {
  $('chkAiAttachShot').onchange = () => {
    $('chkAiAttachShot').dataset.userPicked = '1';
    api('/api/config', { method: 'POST', body: JSON.stringify({ ai_default_screenshot: $('chkAiAttachShot').checked }) }).catch(() => {});
  };
}
if ($('btnSaveAgentPath')) {
  $('btnSaveAgentPath').onclick = async () => {
    const prov = $('aiProviderSelect')?.value || 'auto';
    const val = $('aiManualPathInput')?.value.trim() || '';
    if (prov === 'auto') { $('aiAgentScanMsg').textContent = '请先在左侧选择具体 Agent 再填路径'; return; }
    const body = prov === 'custom' ? { agent_custom_cmd: val } : { agent_paths: { [prov]: val } };
    try {
      const res = await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
      if (res.agents) syncAgentsUI(res.agents, res.config);
      $('aiAgentScanMsg').textContent = '已保存路径并重新探测';
    } catch (err) { $('aiAgentScanMsg').textContent = '保存失败：' + err.message; }
  };
}
if ($('btnTestAgent')) {
  $('btnTestAgent').onclick = async () => {
    const prov = $('aiProviderSelect')?.value || 'auto';
    const target = prov === 'auto' ? (AGENTS_CACHE?.recommended || 'zcode') : prov;
    $('aiAgentScanMsg').textContent = `测试 ${target}...`;
    try {
      const res = await api('/api/agents/test', { method: 'POST', body: JSON.stringify({ provider: target }) });
      $('aiAgentScanMsg').textContent = `正常：${target}（${res.version || 'OK'}）`;
    } catch (err) { $('aiAgentScanMsg').textContent = `异常：${err.message}`; }
  };
}
if ($('btnDeepScanAgents')) {
  $('btnDeepScanAgents').onclick = async () => {
    $('aiAgentScanMsg').textContent = '扩展扫描中...';
    try {
      const res = await api('/api/agents?deep=1');
      syncAgentsUI(res, OVERVIEW?.config);
      $('aiAgentScanMsg').textContent = `扩展扫描完成（${res.elapsed_ms}ms，发现 ${res.available.length} 个）`;
    } catch (err) { $('aiAgentScanMsg').textContent = '扫描失败：' + err.message; }
  };
}
if ($('btnImportCfgFile')) {
  $('btnImportCfgFile').onclick = () => $('aiCfgFileInput')?.click();
  $('aiCfgFileInput').onchange = async () => {
    const f = $('aiCfgFileInput').files?.[0];
    if (!f) return;
    try {
      const txt = await f.text();
      let payload;
      if (txt.trim().startsWith('{')) {
        payload = JSON.parse(txt);
      } else {
        const env = {};
        for (const ln of txt.split(/\r?\n/)) {
          const s = ln.trim();
          if (!s || s.startsWith('#') || !s.includes('=')) continue;
          const idx = s.indexOf('=');
          env[s.slice(0, idx).trim()] = s.slice(idx + 1).trim().replace(/^['"]|['"]$/g, '');
        }
        payload = { env };
      }
      const res = await api('/api/config/import', { method: 'POST', body: JSON.stringify(payload) });
      $('aiAgentScanMsg').textContent = '已完成：' + (res.message || '导入完成');
      showBanner('ai', res.message || '配置/密钥已导入', 4500);
      await refreshOverview();
    } catch (err) {
      $('aiAgentScanMsg').textContent = '导入失败：' + err.message;
    } finally { $('aiCfgFileInput').value = ''; }
  };
}
if ($('aiNoteInput')) {
  $('aiNoteInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !$('btnStartAi').disabled) $('btnStartAi').click();
  });
}
$('btnStartAi').onclick = async () => {
  const note = $('aiNoteInput').value.trim();
  $('btnStartAi').disabled = true;
  try {
    const res = await api('/api/ai/run', {
      method: 'POST',
      body: JSON.stringify({
        chat_id: ACTIVE_CHAT_ID,
        surface: 'workbench',
        provider: $('aiProviderSelect')?.value || 'auto',
        mode: $('aiModeSelect')?.value || 'yolo',
        note,
        attach_screenshot: Boolean($('chkAiAttachShot')?.checked),
        ui_state: collectWbUiState(),
      }),
    });
    $('aiNoteInput').value = '';
    if (res.headless && res.session) {
      ACTIVE_CHAT_ID = res.session.chat_id;
      upsertAiSessionSnapshot(res.session);
      showBanner('ai', res.message || 'AI 帮手已启动', 4000);
    } else if (res.prompt) {
      $('aiChatBody').innerHTML = `<div id="aiResponseBox" class="ai-msg assistant">${esc((res.message || '') + '\n\n' + res.prompt)}</div>`;
      showBanner('ai', res.message || '已复制诊断提示词', 4500);
    }
  } catch (err) {
    showBanner('ai', '发送失败：' + err.message, 4500);
  } finally {
    $('btnStartAi').disabled = false;
  }
};
$('btnStopAi').onclick = async () => {
  try {
    const res = await api('/api/ai/stop', { method: 'POST', body: JSON.stringify({ chat_id: ACTIVE_CHAT_ID }) });
    showBanner('ai', res.message || '已停止 AI 助手');
  } catch (err) { alert(err.message); }
};
$('btnSetVlmKey').onclick = () => openVlmDialog();
$('btnSaveOpencode').onclick = async () => {
  try {
    await api('/api/config', { method: 'POST', body: JSON.stringify({ opencode_path: $('inputOpencode').value.trim() }) });
    showBanner('gui', '已保存 opencode 路径');
  } catch (err) { alert(err.message); }
};

