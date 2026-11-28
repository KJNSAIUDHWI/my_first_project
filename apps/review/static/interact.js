/* ==================== 鼠标与画布交互（支持右键删框 + 实时缩放光标） ==================== */
let spaceDown = false;

host.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  if (!state.img) return;
  const rect = getHostMetrics();
  const sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
  const factor = ev.deltaY < 0 ? 1.14 : 1 / 1.14;
  const nextScale = Math.max(0.08, Math.min(8, state.scale * factor));
  const ix = (sx - state.tx) / state.scale, iy = (sy - state.ty) / state.scale;
  state.scale = nextScale;
  state.tx = sx - ix * state.scale;
  state.ty = sy - iy * state.scale;
  $("zoomLabel").textContent = Math.round(state.scale * 100) + "%";
  scheduleDraw();
}, { passive: false });

host.addEventListener("mousedown", (ev) => {
  if (!state.img) return;
  const rect = syncHostMetrics();
  const sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
  const [ix, iy] = screenToImg(sx, sy);

  // 点击画布空白处关闭原地文字编辑器并取消高亮选中框
  if (state.editingPopItemId && ev.button === 0 && !spaceDown) {
    state.editingPopItemId = null;
    state.selectedItemId = null;
    draw();
    return;
  }

  // 中键拖动 / 空格+左键拖动 -> 任意模式立即平移（兼容 CAD 习惯）
  if (ev.button === 1 || (ev.button === 0 && spaceDown)) {
    ev.preventDefault();
    state.drag = { type: "pan", sx, sy, tx0: state.tx, ty0: state.ty };
    host.classList.add("panning");
    return;
  }

  // 右键（全模式兼容）：
  // - 拖动 >= 5px：平移画布（兼容右键拖图习惯）
  // - 调框模式下短按单击（< 5px）落在框上：删除最内层命中的框；落在空白：取消选中
  if (ev.button === 2) {
    ev.preventDefault();
    const hit = state.mode === "edit" ? hitTestAnyBox(ix, iy) : null;
    state.drag = {
      type: "right_pending",
      sx, sy,
      tx0: state.tx, ty0: state.ty,
      hitIdx: hit ? hit.idx : -1,
    };
    return;
  }

  if (ev.button !== 0) return;

  // 局部真子集小框重识别模式：左键在大框内拉出严格真子集小框
  if (state.subboxTarget && !state.subboxTarget.busy) {
    const [px1, py1, px2, py2] = state.subboxTarget.parentBox;
    const cx = Math.max(px1, Math.min(px2, ix));
    const cy = Math.max(py1, Math.min(py2, iy));
    state.drag = { type: "subbox_draw", sx, sy, ix0: cx, iy0: cy, ix1: cx, iy1: cy };
    draw();
    return;
  }

  if (state.mode === "view") {
    const p = currentPage();
    const hit = getPageItems(p).find(it => {
      if (it.kind === "fill" && !shouldShowFillInView(it)) return false;
      const [x1, y1, x2, y2] = it.box || [0, 0, 0, 0];
      return ix >= x1 && ix <= x2 && iy >= y1 && iy <= y2;
    });
    if (hit) {
      // 点击(<5px)才锁定/编辑，拖动(>=5px)则平滑平移画布
      state.drag = { type: "view_pending", sx, sy, tx0: state.tx, ty0: state.ty, hitItem: hit };
      return;
    }
    if (state.selectedItemId || state.pinnedItemId || state.hoverItemId || state.focusedLowConf || state.inlineBlankEdit || state.inlineChipEdit) {
      clearHoverTimer();
      state.selectedItemId = null;
      state.pinnedItemId = null;
      state.hoverItemId = null;
      state.hoverSource = null;
      state.hoverInPopover = false;
      state.focusedLowConf = null;
      state.inlineBlankEdit = null;
      state.inlineChipEdit = null;
      refreshOverviewPanel();
      draw();
    }
    state.drag = { type: "pan", sx, sy, tx0: state.tx, ty0: state.ty };
    host.classList.add("panning");
    return;
  }

  // 调框模式左键（与「标注工具」手感完全一致）：
  // 1) 若已选中某个框，且按在【该已选中框】的四边/四角或框内 -> 缩放 / 移动（短按未拖动时可在重叠框间循环切换）
  // 2) 其余任意位置（即使落在蓝色「非选择题区」等未选中大框内部！）：
  //    - 长按拖动 >= 5px -> 直接新建框（永不误抓大框）
  //    - 短按单击 < 5px -> 选中鼠标下最小框（再点可循环重叠框；点空白取消选中）
  const selHit = hitTestSelectedBox(ix, iy);
  if (selHit && selHit.kind === "resize") {
    pushUndo();
    state.drag = {
      type: "resize",
      dir: selHit.dir,
      idx: selHit.idx,
      orig: [...state.workingBoxes[selHit.idx].box],
      sx, sy,
      moved: false,
    };
    draw();
    return;
  }
  if (selHit && selHit.kind === "move") {
    pushUndo();
    state.drag = {
      type: "move",
      idx: selHit.idx,
      orig: [...state.workingBoxes[selHit.idx].box],
      sx, sy,
      ix0: ix, iy0: iy,
      rawIx: ix, rawIy: iy,
      moved: false,
    };
    return;
  }

  // 任意未选中区域（含大框内部）：待判定左键单击选中 vs 左键长按拖动新建框
  const cx = Math.max(0, Math.min(state.img.width, ix));
  const cy = Math.max(0, Math.min(state.img.height, iy));
  state.drag = {
    type: "draw_pending",
    sx, sy,
    rawIx: ix, rawIy: iy,
    ix0: cx, iy0: cy,
    ix1: cx, iy1: cy,
  };
});

let _hoverMoveRaf = 0;
let _lastMoveEv = null;
function processHoverMove(ev) {
  if (!state.img || state.drag) return;
  const rect = getHostMetrics();
  const sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
  const [ix, iy] = screenToImg(sx, sy);

  if (state.subboxTarget) {
    host.style.cursor = spaceDown ? "grab" : "crosshair";
    return;
  }
  if (state.mode === "edit" && sx >= 0 && sy >= 0 && sx <= rect.width && sy <= rect.height) {
    if (spaceDown) { host.style.cursor = "grab"; return; }
    const selHit = hitTestSelectedBox(ix, iy);
    if (selHit && selHit.kind === "resize") host.style.cursor = edgeToCursor(selHit.dir);
    else if (selHit && selHit.kind === "move") host.style.cursor = "move";
    else host.style.cursor = "crosshair";
    return;
  }
  if (state.mode !== "view") return;

  // 鼠标位于悬停/固定气泡（或其透明桥接区）内时直接忽略底层画布命中，绝不穿透切题！
  const overPop = ev.target && ev.target.closest && ev.target.closest(".fill-hover-pop");
  if (overPop) {
    clearHoverTimer();
    state.hoverInPopover = true;
    return;
  }
  const overRightDock = ev.target && ev.target.closest && ev.target.closest("#sidebarRight");
  if (overRightDock) {
    if (!getLockedFillItemId() && state.hoverItemId && state.hoverSource === "canvas" && !state.hoverInPopover) {
      scheduleHoverClear();
    }
    return;
  }

  if (state.showOverlay && !state.editingPopItemId && sx >= 0 && sy >= 0 && sx <= rect.width && sy <= rect.height) {
    const hitItem = findCanvasHoverItemAt(ix, iy);
    host.style.cursor = hitItem ? "pointer" : "grab";
    if (!getLockedFillItemId()) {
      if (hitItem) {
        setHoverCanvasItem(hitItem.id);
      } else if (state.hoverItemId && state.hoverSource === "canvas" && !state.hoverInPopover) {
        if (isInPopoverBridge(sx, sy)) clearHoverTimer();
        else scheduleHoverClear();
      }
    }
  } else {
    host.style.cursor = "grab";
    if (!getLockedFillItemId() && state.hoverItemId && state.hoverSource === "canvas" && !state.hoverInPopover) {
      scheduleHoverClear();
    }
  }
}

window.addEventListener("mousemove", (ev) => {
  if (!state.img) return;
  if (!state.drag) {
    _lastMoveEv = ev;
    if (!_hoverMoveRaf) {
      _hoverMoveRaf = requestAnimationFrame(() => {
        _hoverMoveRaf = 0;
        if (_lastMoveEv) processHoverMove(_lastMoveEv);
      });
    }
    return;
  }

  const rect = getHostMetrics();
  const sx = ev.clientX - rect.left, sy = ev.clientY - rect.top;
  const [ix, iy] = screenToImg(sx, sy);

  const d = state.drag;
  if (d.type === "right_pending" || d.type === "view_pending") {
    if (Math.hypot(sx - d.sx, sy - d.sy) >= 5) {
      d.type = "pan";
      host.classList.add("panning");
      if (!getLockedFillItemId() && state.hoverItemId) clearHoverCanvasImmediate();
      state.tx = d.tx0 + (sx - d.sx);
      state.ty = d.ty0 + (sy - d.sy);
      schedulePanFrame();
    }
  } else if (d.type === "draw_pending") {
    if (Math.hypot(sx - d.sx, sy - d.sy) >= 5) {
      d.type = "draw";
      state.selectedBoxIdx = -1;
      d.ix1 = Math.max(0, Math.min(state.img.width, ix));
      d.iy1 = Math.max(0, Math.min(state.img.height, iy));
      renderRightPanel();
      draw();
    }
  } else if (d.type === "subbox_draw" && state.subboxTarget) {
    const [px1, py1, px2, py2] = state.subboxTarget.parentBox;
    d.ix1 = Math.max(px1, Math.min(px2, ix));
    d.iy1 = Math.max(py1, Math.min(py2, iy));
    scheduleFxOnly();
  } else if (d.type === "pan") {
    state.tx = d.tx0 + (sx - d.sx);
    state.ty = d.ty0 + (sy - d.sy);
    schedulePanFrame();
  } else if (d.type === "draw") {
    d.ix1 = Math.max(0, Math.min(state.img.width, ix));
    d.iy1 = Math.max(0, Math.min(state.img.height, iy));
    scheduleFxOnly();
  } else if (d.type === "move") {
    if (!d.moved && Math.hypot(sx - d.sx, sy - d.sy) < 4) return;
    d.moved = true;
    const dx = ix - d.ix0, dy = iy - d.iy0;
    const w = d.orig[2] - d.orig[0], h = d.orig[3] - d.orig[1];
    const x1 = Math.max(0, Math.min(state.img.width - w, d.orig[0] + dx));
    const y1 = Math.max(0, Math.min(state.img.height - h, d.orig[1] + dy));
    const b = state.workingBoxes[d.idx];
    b.box = [x1, y1, x1 + w, y1 + h];
    const bound = findBoundItemForBox(b);
    if (bound) {
      bound.box = [...b.box];
      state.itemsModified = true;
    }
    state.boxesModified = true;
    updateDirtyBadge();
    scheduleDraw();
  } else if (d.type === "resize") {
    if (!d.moved && Math.hypot(sx - d.sx, sy - d.sy) < 3) return;
    let [x1, y1, x2, y2] = d.orig;
    const cx = Math.max(0, Math.min(state.img.width, ix));
    const cy = Math.max(0, Math.min(state.img.height, iy));
    if (d.dir.includes("w")) x1 = Math.min(x2 - 8, cx);
    if (d.dir.includes("e")) x2 = Math.max(x1 + 8, cx);
    if (d.dir.includes("n")) y1 = Math.min(y2 - 8, cy);
    if (d.dir.includes("s")) y2 = Math.max(y1 + 8, cy);
    d.moved = true;
    const b = state.workingBoxes[d.idx];
    b.box = [x1, y1, x2, y2];
    const bound = findBoundItemForBox(b);
    if (bound) {
      bound.box = [...b.box];
      state.itemsModified = true;
    }
    state.boxesModified = true;
    updateDirtyBadge();
    scheduleDraw();
  }
});

window.addEventListener("mouseup", () => {
  if (!state.drag) return;
  const d = state.drag;
  state.drag = null;
  host.classList.remove("panning");
  if (d.type === "pan") {
    draw();
    return;
  }
  if (d.type === "subbox_draw" && state.subboxTarget) {
    const x1 = Math.min(d.ix0, d.ix1), y1 = Math.min(d.iy0, d.iy1);
    const x2 = Math.max(d.ix0, d.ix1), y2 = Math.max(d.iy0, d.iy1);
    if ((x2 - x1) >= 8 && (y2 - y1) >= 8) {
      submitSubboxReOcr([x1, y1, x2, y2]);
    } else {
      toast("局部小框太小，请在大框内拖动框选完整手写答案", 2000);
      draw();
    }
    return;
  }
  if (d.type === "view_pending") {
    if (d.hitItem) {
      if (d.hitItem.kind === "fill") {
        // 无论右侧总览栏是否开启，点击大题框均立即固定气泡并联动高亮右侧对应题卡
        pinFillItem(d.hitItem.id);
      } else {
        openInlineEditor(d.hitItem);
      }
    }
  } else if (d.type === "right_pending") {
    // 右键短按（未拖动 >= 5px）在调框模式：命中框 -> 删框；点空白 -> 取消选中
    if (state.mode === "edit") {
      if (d.hitIdx >= 0) {
        deleteBoxAt(d.hitIdx);
      } else if (state.selectedBoxIdx >= 0) {
        state.selectedBoxIdx = -1;
        renderRightPanel();
        draw();
      }
    }
  } else if (d.type === "draw_pending") {
    // 左键短按（未拖动 >= 5px）：选中命中的最小框（重叠框可再次单击循环切换；点空白取消选中）
    const hit = hitTestAnyBox(d.rawIx, d.rawIy);
    if (hit) {
      let targetIdx = hit.idx;
      if (hit.allInside && hit.allInside.length > 1 && hit.allInside.includes(state.selectedBoxIdx)) {
        const pos = hit.allInside.indexOf(state.selectedBoxIdx);
        targetIdx = hit.allInside[(pos + 1) % hit.allInside.length];
      }
      state.selectedBoxIdx = targetIdx;
    } else {
      state.selectedBoxIdx = -1;
    }
    renderRightPanel();
    draw();
  } else if (d.type === "move" && !d.moved) {
    // 已选中框内短按未拖动：撤销空快照；若此处重叠多个框，则循环切换到下一框（小框优先）
    state.undoStack.pop();
    const hit = hitTestAnyBox(d.rawIx, d.rawIy);
    if (hit && hit.allInside && hit.allInside.length > 1) {
      const pos = hit.allInside.indexOf(state.selectedBoxIdx);
      state.selectedBoxIdx = hit.allInside[(pos + 1) % hit.allInside.length];
    }
    renderRightPanel();
    draw();
  } else if (d.type === "resize" && !d.moved) {
    state.undoStack.pop();
    draw();
  } else if ((d.type === "move" || d.type === "resize") && d.moved) {
    const b = state.workingBoxes[d.idx];
    if (b && isVlmRecogCls(b.cls) && d.orig) {
      const ov = boxIoU(d.orig, b.box);
      if (ov < 0.90) b._needsRecog = true;
    }
    state.workload.moved += 1;
    updateWorkloadBar();
    renderRightPanel();
    draw();
  } else if (d.type === "draw") {
    const x1 = Math.min(d.ix0, d.ix1), y1 = Math.min(d.iy0, d.iy1);
    const x2 = Math.max(d.ix0, d.ix1), y2 = Math.max(d.iy0, d.iy1);
    if ((x2 - x1) >= 8 && (y2 - y1) >= 8) {
      pushUndo();
      const meta = BOX_META[state.activeCls];
      state.showLayers[meta.stage] = true;
      const canVlm = isVlmRecogCls(state.activeCls);
      state.workingBoxes.push({
        cls: state.activeCls,
        box: [x1, y1, x2, y2],
        stage: meta.stage,
        manual: true,
        _uid: nextBoxUid(),
        _needsRecog: canVlm,
        _isNew: canVlm,
      });
      state.selectedBoxIdx = state.workingBoxes.length - 1;
      state.boxesModified = true;
      state.workload.added += 1;
      updateDirtyBadge();
      updateWorkloadBar();
      renderRightPanel();
      if (canVlm) {
        toast(`已画新框：点「API识别」或按 ${primaryKeyLabel("recognizeBox")} 即可直接调用大模型识别`, 2600);
      }
    }
    draw();
  }
});

host.addEventListener("mouseleave", (ev) => {
  const rel = ev && ev.relatedTarget;
  if (rel && rel.closest && rel.closest(".fill-hover-pop")) return;
  if (!getLockedFillItemId() && state.hoverItemId && state.hoverSource === "canvas" && !state.hoverInPopover) {
    scheduleHoverClear();
  }
});
host.addEventListener("dblclick", () => { if (!state.subboxTarget) { fitCanvas(); draw(); } });
host.addEventListener("contextmenu", (ev) => ev.preventDefault());

/* ==================== 整页提交 ==================== */
async function submitCurrentPage(goNext = true) {
  if (state.saving || state.recognizingBusy) return;
  const p = currentPage();
  if (!p) return;
  const pendingItems = getPageItems(p).filter(it => !it.transcription && (it.status === "等待视觉转录" || it.status === "等待豆包"));
  if (pendingItems.length > 0) {
    if (!confirm(`本页还有 ${pendingItems.length} 道手写题正在后台识别中（尚未出字）。\n确定现在就要把本页标为「已确认」吗？（建议点取消，等几秒答案自动出现后再确认）`)) {
      return;
    }
  }
  state.saving = true;
  $("btnSubmitNext").disabled = true;
  try {
    const res = await api("/api/submit-page", {
      page_id: p.id,
      reviewer: REVIEWER,
      client_id: CLIENT_ID,
      page_status: "已确认",
      boxes: state.workingBoxes,
      boxes_modified: state.boxesModified,
      items: state.workingItems,
      items_modified: state.itemsModified,
      sync_log: true,
      item_edits: state.itemEdits,
    });
    if (res.feedback) state.feedback = res.feedback;
    if (res.boxes) p.boxes = res.boxes;
    if (res.items) p.items = res.items;
    if (res.results_mtime) state.resultsMtime = res.results_mtime;
    if (res.locks) state.locks = res.locks;
    state.workingBoxes = JSON.parse(JSON.stringify(getPageBoxes(p)));
    state.workingItems = JSON.parse(JSON.stringify(getRawPageItems(p)));
    bindBoxesAndItems(state.workingBoxes, state.workingItems);
    state.boxesModified = false;
    state.itemsModified = false;
    state.itemEdits = {};
    state.focusedLowConf = null;
    updateDirtyBadge();
    renderLeftList();
    updateTopbar();

    const syncMsg = res.sync && res.sync.synced ? `（已同步标框记录）` : "";
    if (goNext) {
      // 多人协作优先通过 /api/claim 自动领取下一张「无人锁定且未审」的试卷，避免撞车
      let nextIdx = -1;
      try {
        const claimRes = await api("/api/claim", {
          client_id: CLIENT_ID,
          user: REVIEWER || "成员",
          exclude: [p.id],
        });
        if (claimRes.locks) state.locks = claimRes.locks;
        const claimedPid = claimRes.page_id || claimRes.key;
        if (claimedPid) {
          nextIdx = state.pages.findIndex(x => x.id === claimedPid);
        }
      } catch {}
      if (nextIdx >= 0) {
        toast(`已保存 ${p.name}${syncMsg}`, 1700);
        await openPage(nextIdx, true, true);
      } else if (state.pageIdx < state.pages.length - 1) {
        toast(`已保存 ${p.name}${syncMsg} · 其余待审页已被他人锁定或已全部审完`, 2400);
        renderRightPanel();
        draw();
      } else {
        toast(`已保存 ${p.name}${syncMsg} · 本批试卷已全部审完`, 2400);
        renderRightPanel();
        draw();
      }
    } else {
      toast(`已保存 ${p.name}${syncMsg}`, 2000);
      renderRightPanel();
      draw();
    }
  } catch (err) {
    toast("保存失败：" + err.message, 3500);
  } finally {
    state.saving = false;
    $("btnSubmitNext").disabled = false;
  }
}

function setMode(m) {
  state.mode = m;
  state.editingPopItemId = null;
  if (m === "edit") {
    state.pinnedItemId = null;
    state.hoverItemId = null;
    state.hoverSource = null;
    state.hoverInPopover = false;
  }
  updateTopbar();
  renderRightPanel();
  syncRightSidebarVisibility(true);
}

