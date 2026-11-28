/* ==================== 8904 UI 状态镜像 + SSE 实时联动 + 焦点投送 + 全局 AI 帮手抽屉 ==================== */
const REV_URL_PARAMS = new URLSearchParams(window.location.search);
const REV_IS_MIRROR = REV_URL_PARAMS.get("mirror") === "1";
const REV_MIRROR_CLIENT = REV_URL_PARAMS.get("mirror_client") || "";
window.__MIRROR_APPLIED__ = !REV_IS_MIRROR;
window.__RECENT_ERRORS__ = [];
window.addEventListener("error", e => {
  window.__RECENT_ERRORS__.push(`JS错误: ${e.message || "unknown"}`);
  if (window.__RECENT_ERRORS__.length > 10) window.__RECENT_ERRORS__.shift();
});
window.addEventListener("unhandledrejection", e => {
  window.__RECENT_ERRORS__.push(`Promise异常: ${e.reason?.message || e.reason || "unknown"}`);
  if (window.__RECENT_ERRORS__.length > 10) window.__RECENT_ERRORS__.shift();
});

function collectReviewUiState() {
  const p = currentPage();
  return {
    client_id: CLIENT_ID,
    page_id: p ? p.id : "",
    page_name: p ? p.name : "",
    mode: state.mode,
    selected_item_id: state.selectedItemId || state.editingPopItemId || "",
    selected_box_idx: state.selectedBoxIdx,
    zoom: Number((state.scale || 1).toFixed(3)),
    fill_display_mode: state.fillDisplayMode,
    show_overlay: Boolean(state.showOverlay),
    show_conf: Boolean(state.showConf),
    reviewer: REVIEWER || "",
    recent_errors: window.__RECENT_ERRORS__.slice(-6),
  };
}
let _revUiTimer = null;
function scheduleReportReviewUiState() {
  if (REV_IS_MIRROR) return;
  clearTimeout(_revUiTimer);
  _revUiTimer = setTimeout(() => {
    api("/api/ui-state", collectReviewUiState()).catch(() => {});
  }, 240);
}
// 包装 openPage / setMode 使其自动上报焦点
const _origOpenPage = openPage;
openPage = async function(...args) {
  const r = await _origOpenPage.apply(this, args);
  scheduleReportReviewUiState();
  return r;
};
const _origSetMode = setMode;
setMode = function(...args) {
  const r = _origSetMode.apply(this, args);
  scheduleReportReviewUiState();
  return r;
};

async function applyReviewMirrorStateIfNeeded() {
  if (!REV_IS_MIRROR) return;
  try {
    const qs = REV_MIRROR_CLIENT ? `?client_id=${encodeURIComponent(REV_MIRROR_CLIENT)}` : "";
    const res = await api("/api/ui-state" + qs);
    const st = (res && res.active) || {};
    if (st.fill_display_mode) state.fillDisplayMode = st.fill_display_mode;
    if (typeof st.show_overlay === "boolean") state.showOverlay = st.show_overlay;
    if (typeof st.show_conf === "boolean") state.showConf = st.show_conf;
    if (st.page_id) {
      const idx = state.pages.findIndex(p => p.id === st.page_id);
      if (idx >= 0 && idx !== state.pageIdx) await _origOpenPage(idx, true, true);
    }
    if (st.mode && st.mode !== state.mode) _origSetMode(st.mode);
    if (st.selected_item_id) state.selectedItemId = st.selected_item_id;
    draw();
  } catch {}
  window.__MIRROR_APPLIED__ = true;
}

let _revFocusTimer = null;
function castReviewFocus(rect, label = "AI 聚焦") {
  const ring = $("revFocusRing"), tag = $("revFocusTag");
  if (!ring || !rect || rect.width < 2 || rect.height < 2) return;
  ring.style.left = Math.max(2, rect.left - 5) + "px";
  ring.style.top = Math.max(2, rect.top - 5) + "px";
  ring.style.width = (rect.width + 10) + "px";
  ring.style.height = (rect.height + 10) + "px";
  if (tag) tag.textContent = "🤖 " + label;
  ring.classList.remove("hidden");
  clearTimeout(_revFocusTimer);
  _revFocusTimer = setTimeout(() => ring.classList.add("hidden"), 4500);
}
function focusReviewTarget(cmd) {
  if (!cmd) return;
  const doFocusNow = () => {
    if (cmd.target) {
      const el = document.querySelector(cmd.target);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
        setTimeout(() => {
          castReviewFocus(el.getBoundingClientRect(), cmd.label || "AI 聚焦元素");
          if (cmd.action === "click") setTimeout(() => el.click(), 320);
        }, 160);
        return;
      }
    }
    if (cmd.item_id) {
      const it = (state.workingItems || []).find(x => x.id === cmd.item_id);
      if (it && Array.isArray(it.box) && it.box.length === 4) {
        state.selectedItemId = it.id;
        draw();
        const hr = host.getBoundingClientRect();
        const [x1, y1, x2, y2] = it.box;
        const left = hr.left + x1 * state.scale + state.tx;
        const top = hr.top + y1 * state.scale + state.ty;
        const width = Math.max(20, (x2 - x1) * state.scale);
        const height = Math.max(20, (y2 - y1) * state.scale);
        castReviewFocus({ left, top, width, height }, cmd.label || `AI 聚焦 ${it.number ? "第" + it.number + "题" : it.id}`);
      }
    }
  };
  const cur = currentPage();
  if (cmd.page_id && (!cur || cur.id !== cmd.page_id)) {
    const idx = state.pages.findIndex(p => p.id === cmd.page_id);
    if (idx >= 0 && (cmd.force_nav || cmd.action === "navigate" || cmd.action === "focus")) {
      openPage(idx, true, true).then(() => setTimeout(doFocusNow, 180));
      return;
    }
  }
  doFocusNow();
}

function connectReviewSSE() {
  if (!window.EventSource) return;
  const es = new EventSource("/api/events");
  es.onmessage = async (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.type === "data_updated") {
        const cur = currentPage();
        if (ev.page_id && cur && cur.id === ev.page_id) {
          await hotReloadResults(false);
          toast("🤖 " + (ev.message || "当前页已由 AI 实时更新"), 2600);
        } else {
          await pollLive();
          if (ev.source === "ai" && ev.message) {
            toast("🤖 " + ev.message + "（未打断当前页）", 3200);
          }
        }
      } else if (ev.type === "work_mode" && ev.work_mode) {
        state.workMode = ev.work_mode;
        state.showUnmatchedFills = state.workMode === "iteration";
        updateTopbar();
        draw();
        if (ev.message) toast(ev.message, 2200);
      } else if (ev.type === "ui_command") {
        focusReviewTarget(ev);
        if (ev.label) toast("🤖 " + ev.label, 2800);
      }
    } catch {}
  };
  es.onerror = () => { try { es.close(); } catch {} setTimeout(connectReviewSSE, 3500); };
}

/* 8904 全局 AI 帮手抽屉（通过 /api/hub/* 同源代理共享 8888 多会话管理器） */
let REV_AI_SESSIONS = {};
let REV_ACTIVE_CHAT = "";
let REV_AGENTS_CACHE = null;

function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function hubApi(path, body = null) {
  const opts = body === null
    ? { method: "GET", headers: { "X-Review-Token": TOKEN } }
    : { method: "POST", headers: { "Content-Type": "application/json", "X-Review-Token": TOKEN }, body: JSON.stringify(body) };
  const r = await fetch("/api/hub" + path, { cache: "no-store", ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败 (${r.status})`);
  return data;
}
function renderRevAgentsUI(info) {
  REV_AGENTS_CACHE = info;
  const sel = $("revAiProvider");
  if (sel && info && Array.isArray(info.list)) {
    const cur = sel.value || info.preferred || "auto";
    sel.innerHTML = `<option value="auto">自动优选 (${escHtml(info.recommended || "无")})</option>` +
      info.list.map(a => `<option value="${escHtml(a.id)}">${a.available ? "✓ " : "✗ "}${escHtml(a.label)}</option>`).join("");
    sel.value = cur;
  }
  const targetId = (sel && sel.value !== "auto") ? sel.value : (info?.recommended || "");
  const ag = info?.agents?.[targetId];
  if ($("revAiHint")) {
    $("revAiHint").textContent = ag && ag.available
      ? `引擎：${ag.label} · 改动实时推给本页（发送瞬间冻结当前页，发完可继续翻页）`
      : "未检测到本机 CLI Agent，可点「⚙ 配置」填路径或执行可选扩展扫描。";
  }
}
function renderRevAiTabs() {
  const bar = $("revAiTabs");
  if (!bar) return;
  const list = Object.values(REV_AI_SESSIONS);
  bar.innerHTML = list.map((s, i) => {
    const act = s.chat_id === REV_ACTIVE_CHAT ? " active" : "";
    const dot = s.running ? '<i class="dot-run"></i>' : "";
    return `<button class="rev-ai-tab${act}" type="button" data-chat-id="${escHtml(s.chat_id)}">${dot}${escHtml(s.title || ("对话 " + (i + 1)))}</button>`;
  }).join("") + `<button id="btnRevNewChat" type="button" style="padding:3px 8px;font-size:11.5px">＋ 新并发对话</button>`;
  bar.querySelectorAll("[data-chat-id]").forEach(b => {
    b.onclick = () => {
      REV_ACTIVE_CHAT = b.dataset.chatId;
      renderRevAiTabs();
      renderRevAiSession(REV_AI_SESSIONS[REV_ACTIVE_CHAT]);
    };
  });
  const btnN = $("btnRevNewChat");
  if (btnN) btnN.onclick = async () => {
    try {
      const r = await hubApi("/ai/sessions/new", { surface: "review", provider: $("revAiProvider").value, mode: $("revAiMode").value });
      if (r.session) {
        REV_AI_SESSIONS[r.session.chat_id] = r.session;
        REV_ACTIVE_CHAT = r.session.chat_id;
        renderRevAiTabs();
        renderRevAiSession(r.session);
      }
    } catch (e) { toast(e.message, 3000); }
  };
}
function renderRevAiSession(s) {
  if (!s) return;
  const running = Boolean(s.running);
  const st = s.status || "idle";
  const b = $("revAiBadge");
  if (b) {
    b.className = "badge " + (running || st === "done" ? "badge-done" : (st === "error" ? "badge-lock" : "badge-todo"));
    b.textContent = running ? (s.queued ? `执行中(排队${s.queued})` : "执行中...") : (st === "done" ? "已完成" : (st === "error" ? "异常" : "就绪"));
  }
  if ($("btnRevAiStop")) $("btnRevAiStop").style.display = running ? "" : "none";
  const meta = [];
  if (s.provider_label || s.provider) meta.push(`引擎：${s.provider_label || s.provider}`);
  if (s.mode) meta.push(`权限：${s.mode}`);
  const u = s.usage || {};
  if (u.inputTokens || u.outputTokens) meta.push(`Token：${u.inputTokens || 0}/${u.outputTokens || 0}`);
  if ($("revAiMeta")) $("revAiMeta").textContent = meta.join(" · ");
  const body = $("revAiBody");
  if (!body) return;
  const msgs = Array.isArray(s.messages) ? s.messages : [];
  if (!msgs.length && !running) {
    body.innerHTML = `<div class="rev-ai-msg assistant">已就绪：可让 AI 直接读图改字、删框重识、查报错或导入密钥配置。</div>`;
    return;
  }
  const html = msgs.map(m => {
    const att = (m.attach && m.attach.length) ? ` · 📷 ${escHtml(m.attach.join(", "))}` : "";
    return `<div class="rev-ai-msg ${m.role === "user" ? "user" : "assistant"}"><div class="rev-ai-msg-meta">${m.role === "user" ? "你" : "AI 帮手"} · ${escHtml(m.ts || "")}${att}</div>${escHtml(m.text || "")}</div>`;
  });
  if (running) {
    html.push(`<div class="rev-ai-msg assistant"><div class="rev-ai-msg-meta">AI 帮手 · 执行中</div>${escHtml(s.response || "⏳ 正在分析当前冻结页面并执行同构操作，你可以继续自由审阅...")}</div>`);
  }
  body.innerHTML = html.join("");
  body.scrollTop = body.scrollHeight;
}
function connectReviewHubSSE() {
  if (!window.EventSource) return;
  const es = new EventSource("/api/hub/events");
  es.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.step === "ai" && ev.ai_snapshot && ev.ai_snapshot.chat_id) {
        REV_AI_SESSIONS[ev.ai_snapshot.chat_id] = ev.ai_snapshot;
        if (!REV_ACTIVE_CHAT) REV_ACTIVE_CHAT = ev.ai_snapshot.chat_id;
        renderRevAiTabs();
        if (ev.ai_snapshot.chat_id === REV_ACTIVE_CHAT) renderRevAiSession(ev.ai_snapshot);
      }
    } catch {}
  };
  es.onerror = () => { try { es.close(); } catch {} setTimeout(connectReviewHubSSE, 5000); };
}

const _revDrawerDrag = window.initDraggableDrawer
  ? window.initDraggableDrawer({
      drawer: $("reviewAiDrawer"),
      handle: $("revAiDrawerHead"),
      resizeHandle: $("revAiResizeHandle"),
      storageKey: "revAiDrawerRect",
      defaultWidth: 430,
      minWidth: 320,
      minHeight: 340,
    })
  : null;

if ($("btnReviewAi")) {
  $("btnReviewAi").onclick = () => {
    const d = $("reviewAiDrawer");
    d.classList.toggle("hidden");
    if (!d.classList.contains("hidden")) {
      if (_revDrawerDrag) _revDrawerDrag.sync();
      hubApi("/agents").then(renderRevAgentsUI).catch(e => { $("revAiHint").textContent = e.message; });
      hubApi("/ai/sessions").then(r => {
        for (const s of (r.sessions || [])) REV_AI_SESSIONS[s.chat_id] = s;
        if (!REV_ACTIVE_CHAT && r.sessions?.length) REV_ACTIVE_CHAT = r.sessions[0].chat_id;
        renderRevAiTabs();
        if (REV_ACTIVE_CHAT && REV_AI_SESSIONS[REV_ACTIVE_CHAT]) renderRevAiSession(REV_AI_SESSIONS[REV_ACTIVE_CHAT]);
      }).catch(() => {});
      setTimeout(() => $("revAiInput")?.focus(), 80);
    }
  };
}
if ($("btnRevAiClose")) $("btnRevAiClose").onclick = () => $("reviewAiDrawer").classList.add("hidden");
if ($("btnRevAiCfg")) $("btnRevAiCfg").onclick = () => $("revAiCfg").classList.toggle("hidden");
if ($("revAiProvider")) $("revAiProvider").onchange = () => renderRevAgentsUI(REV_AGENTS_CACHE);
if ($("btnRevSavePath")) {
  $("btnRevSavePath").onclick = async () => {
    const prov = $("revAiProvider").value;
    const val = $("revAiPathInput").value.trim();
    if (prov === "auto") { $("revAiScanMsg").textContent = "先选具体 Agent 再填路径"; return; }
    try {
      const r = await hubApi("/config", prov === "custom" ? { agent_custom_cmd: val } : { agent_paths: { [prov]: val } });
      if (r.agents) renderRevAgentsUI(r.agents);
      $("revAiScanMsg").textContent = "已保存并重探";
    } catch (e) { $("revAiScanMsg").textContent = e.message; }
  };
}
if ($("btnRevTestAgent")) {
  $("btnRevTestAgent").onclick = async () => {
    const p = $("revAiProvider").value === "auto" ? (REV_AGENTS_CACHE?.recommended || "zcode") : $("revAiProvider").value;
    $("revAiScanMsg").textContent = `测试 ${p}...`;
    try {
      const r = await hubApi("/agents/test", { provider: p });
      $("revAiScanMsg").textContent = `✓ ${p} 正常 (${r.version || "OK"})`;
    } catch (e) { $("revAiScanMsg").textContent = `✗ ${e.message}`; }
  };
}
if ($("btnRevDeepScan")) {
  $("btnRevDeepScan").onclick = async () => {
    $("revAiScanMsg").textContent = "扩展扫描中...";
    try {
      const r = await hubApi("/agents?deep=1");
      renderRevAgentsUI(r);
      $("revAiScanMsg").textContent = `完成 (${r.elapsed_ms}ms，发现 ${r.available.length} 个)`;
    } catch (e) { $("revAiScanMsg").textContent = e.message; }
  };
}
if ($("btnRevImportCfg")) {
  $("btnRevImportCfg").onclick = () => $("revCfgFile").click();
  $("revCfgFile").onchange = async () => {
    const f = $("revCfgFile").files?.[0];
    if (!f) return;
    try {
      const txt = await f.text();
      let payload;
      if (txt.trim().startsWith("{")) payload = JSON.parse(txt);
      else {
        const env = {};
        for (const ln of txt.split(/\r?\n/)) {
          const s = ln.trim();
          if (!s || s.startsWith("#") || !s.includes("=")) continue;
          const idx = s.indexOf("=");
          env[s.slice(0, idx).trim()] = s.slice(idx + 1).trim().replace(/^['"]|['"]$/g, "");
        }
        payload = { env };
      }
      const r = await hubApi("/config/import", payload);
      $("revAiScanMsg").textContent = "✓ " + (r.message || "已导入");
      toast(r.message || "配置/密钥导入完成", 3200);
    } catch (e) { $("revAiScanMsg").textContent = "✗ " + e.message; }
    finally { $("revCfgFile").value = ""; }
  };
}
if ($("revAiInput")) {
  $("revAiInput").addEventListener("keydown", e => {
    if (e.key === "Enter" && !$("btnRevAiSend").disabled) $("btnRevAiSend").click();
  });
}
if ($("btnRevAiSend")) {
  $("btnRevAiSend").onclick = async () => {
    const note = $("revAiInput").value.trim();
    $("btnRevAiSend").disabled = true;
    try {
      const r = await hubApi("/ai/run", {
        chat_id: REV_ACTIVE_CHAT,
        surface: "review",
        provider: $("revAiProvider").value,
        mode: $("revAiMode").value,
        note,
        attach_screenshot: Boolean($("chkRevAiShot").checked),
        ui_state: collectReviewUiState(),
        review_port: Number(window.location.port || 8904),
      });
      $("revAiInput").value = "";
      if (r.headless && r.session) {
        REV_ACTIVE_CHAT = r.session.chat_id;
        REV_AI_SESSIONS[r.session.chat_id] = r.session;
        renderRevAiTabs();
        renderRevAiSession(r.session);
        toast(r.message || "AI 帮手已启动", 2600);
      } else if (r.prompt) {
        $("revAiBody").innerHTML = `<div class="rev-ai-msg assistant">${escHtml((r.message || "") + "\n\n" + r.prompt)}</div>`;
        toast(r.message || "已复制诊断提示词", 3000);
      }
    } catch (e) { toast("启动失败：" + e.message, 3500); }
    finally { $("btnRevAiSend").disabled = false; }
  };
}
if ($("btnRevAiStop")) {
  $("btnRevAiStop").onclick = async () => {
    try {
      const r = await hubApi("/ai/stop", { chat_id: REV_ACTIVE_CHAT });
      toast(r.message || "已停止", 2000);
    } catch (e) { toast(e.message, 2500); }
  };
}

// 绑定双模式药丸、未配题号清理、成绩导出与局部重识别浮条
document.querySelectorAll("#revWorkPill .work-pill-btn").forEach(btn => {
  btn.onclick = () => { if (btn.dataset.work && btn.dataset.work !== state.workMode) switchWorkMode(btn.dataset.work); };
});
if ($("btnExportCsv")) {
  $("btnExportCsv").onclick = () => window.open("/api/grading.csv", "_blank");
}
if ($("btnCancelSubbox")) $("btnCancelSubbox").onclick = cancelSubboxReOcr;
if ($("btnClearMultiSel")) {
  $("btnClearMultiSel").onclick = () => { state.multiSelect = null; updateMultiBlankBarUI(); refreshOverviewPanel(); draw(); };
}
if ($("btnStartMultiSubbox")) {
  $("btnStartMultiSubbox").onclick = () => {
    const p = currentPage();
    if (!p || !state.multiSelect || state.multiSelect.slots.length < 2) return;
    const it = getPageItems(p).find(x => x.id === state.multiSelect.itemId);
    if (it) enterSubboxReOcr(it, state.multiSelect.slots);
  };
}
if ($("btnUnmatchedFills")) {
  $("btnUnmatchedFills").onclick = async () => {
    const p = currentPage();
    if (!p) return;
    if (state.workMode === "grading") {
      state.showUnmatchedFills = !state.showUnmatchedFills;
      updateTopbar();
      refreshOverviewPanel();
      draw();
      return;
    }
    const pageUm = getPageItems(p).filter(isUnmatchedFillItem).length;
    const batchUm = state.pages.reduce((s, pg) => s + (pg.items || []).filter(isUnmatchedFillItem).length, 0);
    const scopeBatch = batchUm > pageUm && confirm(`全批次共有 ${batchUm} 个未匹配大题号的 M3 填空框（当前页 ${pageUm} 个）。\n点「确定」一键清理【整批】未配题号框；点「取消」仅清理【当前页】？`);
    const scope = scopeBatch ? "batch" : "page";
    if (!scopeBatch && pageUm === 0) { toast("当前页没有未匹配大题号的填空框", 1800); return; }
    if (!scopeBatch && !confirm(`确认清理当前页的 ${pageUm} 个未匹配大题号填空框？（清理前会自动备份 results.before_purge.json）`)) return;
    try {
      const res = await api("/api/review/purge-unmatched-fills", { scope, page_id: p.id, reviewer: REVIEWER || "审阅员" });
      await hotReloadResults(false);
      toast(`✓ 已清理 ${res.removed_count || 0} 个未配题号填空框`, 2800);
    } catch (err) {
      toast("清理失败：" + err.message, 3200);
    }
  };
}

resizeCanvas();
loadAll().then(async () => {
  await applyReviewMirrorStateIfNeeded();
  if (!REV_IS_MIRROR) {
    connectReviewSSE();
    connectReviewHubSSE();
    scheduleReportReviewUiState();
  }
}).catch(err => toast("加载失败：" + err.message, 5000));
