/* ==================== 共享组件：可拖拽 & 可缩放悬浮抽屉 ==================== */
(function () {
  if (window.initDraggableDrawer) return;

  window.initDraggableDrawer = function initDraggableDrawer(opts) {
    const drawer = opts && opts.drawer;
    const handle = opts && opts.handle;
    const resizeHandle = opts && opts.resizeHandle;
    if (!drawer || !handle) return { sync() {}, reset() {} };

    const storageKey = (opts && opts.storageKey) || "";
    const minW = Math.max(260, Number(opts.minWidth || 320));
    const minH = Math.max(240, Number(opts.minHeight || 320));
    const defaultW = Math.max(minW, Number(opts.defaultWidth || 430));
    const margin = 8;

    let savedRect = null;
    if (storageKey) {
      try {
        const raw = localStorage.getItem(storageKey);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && Number.isFinite(parsed.left) && Number.isFinite(parsed.top)) {
            savedRect = parsed;
          }
        }
      } catch {}
    }

    function clampRect(r) {
      const vw = window.innerWidth || 1280;
      const vh = window.innerHeight || 720;
      const w = Math.max(minW, Math.min(vw - margin * 2, Number(r.width) || defaultW));
      const h = Math.max(minH, Math.min(vh - margin * 2, Number(r.height) || Math.min(680, vh - 72)));
      const left = Math.max(margin, Math.min(vw - w - margin, Number(r.left)));
      const top = Math.max(margin, Math.min(vh - h - margin, Number(r.top)));
      return { left: Math.round(left), top: Math.round(top), width: Math.round(w), height: Math.round(h) };
    }

    function applyRect(r, persist) {
      const c = clampRect(r);
      drawer.style.left = c.left + "px";
      drawer.style.top = c.top + "px";
      drawer.style.right = "auto";
      drawer.style.bottom = "auto";
      drawer.style.width = c.width + "px";
      drawer.style.height = c.height + "px";
      savedRect = c;
      if (persist && storageKey) {
        try { localStorage.setItem(storageKey, JSON.stringify(c)); } catch {}
      }
    }

    function reset() {
      savedRect = null;
      drawer.style.left = "";
      drawer.style.top = "";
      drawer.style.right = "";
      drawer.style.bottom = "";
      drawer.style.width = "";
      drawer.style.height = "";
      if (storageKey) {
        try { localStorage.removeItem(storageKey); } catch {}
      }
    }

    function sync() {
      if (savedRect) {
        applyRect(savedRect, false);
      }
    }

    let mode = null; // "drag" | "resize"
    let startX = 0, startY = 0, startRect = null;

    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest("button, input, select, textarea, a, label")) return;
      const b = drawer.getBoundingClientRect();
      mode = "drag";
      startX = e.clientX;
      startY = e.clientY;
      startRect = { left: b.left, top: b.top, width: b.width, height: b.height };
      drawer.classList.add("dragging");
      try { handle.setPointerCapture(e.pointerId); } catch {}
      e.preventDefault();
    });

    handle.addEventListener("dblclick", (e) => {
      if (e.target && e.target.closest && e.target.closest("button, input, select, textarea, a")) return;
      reset();
    });

    if (resizeHandle) {
      resizeHandle.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        const b = drawer.getBoundingClientRect();
        mode = "resize";
        startX = e.clientX;
        startY = e.clientY;
        startRect = { left: b.left, top: b.top, width: b.width, height: b.height };
        drawer.classList.add("dragging");
        try { resizeHandle.setPointerCapture(e.pointerId); } catch {}
        e.preventDefault();
        e.stopPropagation();
      });
    }

    function onMove(e) {
      if (!mode || !startRect) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (mode === "drag") {
        applyRect({
          left: startRect.left + dx,
          top: startRect.top + dy,
          width: startRect.width,
          height: startRect.height,
        }, false);
      } else if (mode === "resize") {
        const vw = window.innerWidth || 1280;
        const vh = window.innerHeight || 720;
        const nextW = Math.max(minW, Math.min(vw - startRect.left - margin, startRect.width + dx));
        const nextH = Math.max(minH, Math.min(vh - startRect.top - margin, startRect.height + dy));
        applyRect({
          left: startRect.left,
          top: startRect.top,
          width: nextW,
          height: nextH,
        }, false);
      }
    }

    function onUp() {
      if (!mode) return;
      mode = null;
      startRect = null;
      drawer.classList.remove("dragging");
      if (savedRect && storageKey) {
        try { localStorage.setItem(storageKey, JSON.stringify(savedRect)); } catch {}
      }
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    window.addEventListener("resize", () => {
      if (savedRect && !drawer.classList.contains("hidden")) {
        applyRect(savedRect, false);
      }
    });

    return { sync, reset };
  };
})();
