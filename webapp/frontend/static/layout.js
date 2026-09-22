/* IFC Compliance Workbench — workspace layout behavior.
 *
 * Purely presentational: resizable/collapsible split between the model
 * preview and the workflow panel, the recent-activity drawer, and the
 * collapsed file-strip state. Does not depend on app.js/auth.js internals —
 * only reads/writes classes and its own new elements, plus the existing
 * #file-input / #dropzone change/drop events.
 */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const split = $("workspace-split");
  const resizer = $("pane-resizer");
  const previewPane = $("preview-pane");
  const panelPane = $("panel-pane");
  const collapseLeft = $("collapse-left");
  const collapseRight = $("collapse-right");
  const expandLeft = $("expand-left");
  const expandRight = $("expand-right");

  const STORAGE_KEY = "workbench:pane-split";
  const MIN_PANE = 320;
  const SPLIT_BREAKPOINT = 960;

  function isSplitLayout() {
    return window.innerWidth > SPLIT_BREAKPOINT;
  }

  function loadState() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (_) {
      return {};
    }
  }
  function saveState(state) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) {}
  }

  let state = Object.assign({ leftWidth: null, collapsed: null }, loadState());

  function applyLayout() {
    if (!isSplitLayout()) {
      split.style.gridTemplateColumns = "";
      split.classList.remove("collapsed-left", "collapsed-right");
      return;
    }
    if (state.collapsed === "left") {
      split.classList.add("collapsed-left");
      split.classList.remove("collapsed-right");
      split.style.gridTemplateColumns = "0 0 1fr";
      show(expandLeft, true);
      show(expandRight, false);
      return;
    }
    if (state.collapsed === "right") {
      split.classList.add("collapsed-right");
      split.classList.remove("collapsed-left");
      split.style.gridTemplateColumns = "1fr 0 0";
      show(expandLeft, false);
      show(expandRight, true);
      return;
    }
    split.classList.remove("collapsed-left", "collapsed-right");
    show(expandLeft, false);
    show(expandRight, false);
    const total = split.clientWidth || window.innerWidth;
    const maxLeft = Math.max(MIN_PANE, total - MIN_PANE - 6);
    const left = clamp(state.leftWidth ?? Math.round(total / 2), MIN_PANE, maxLeft);
    split.style.gridTemplateColumns = `${left}px 6px 1fr`;
  }

  function show(el, on) {
    if (el) el.classList.toggle("hidden", !on);
  }
  function clamp(v, min, max) {
    return Math.max(min, Math.min(max, v));
  }

  // --- drag to resize -------------------------------------------------------
  let dragging = false;
  resizer.addEventListener("pointerdown", (event) => {
    if (!isSplitLayout() || state.collapsed) return;
    dragging = true;
    resizer.setPointerCapture(event.pointerId);
    document.body.classList.add("pane-dragging");
  });
  resizer.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = split.getBoundingClientRect();
    const total = rect.width;
    const maxLeft = Math.max(MIN_PANE, total - MIN_PANE - 6);
    const left = clamp(Math.round(event.clientX - rect.left), MIN_PANE, maxLeft);
    state.leftWidth = left;
    split.style.gridTemplateColumns = `${left}px 6px 1fr`;
  });
  function endDrag() {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("pane-dragging");
    saveState(state);
    window.dispatchEvent(new Event("resize")); // let xeokit's canvas observer pick up the new size
  }
  resizer.addEventListener("pointerup", endDrag);
  resizer.addEventListener("pointercancel", endDrag);
  resizer.addEventListener("dblclick", () => {
    state.leftWidth = null;
    state.collapsed = null;
    saveState(state);
    applyLayout();
    window.dispatchEvent(new Event("resize"));
  });

  // --- collapse / expand ------------------------------------------------------
  collapseLeft.addEventListener("click", () => {
    state.collapsed = "left";
    saveState(state);
    applyLayout();
    window.dispatchEvent(new Event("resize"));
  });
  collapseRight.addEventListener("click", () => {
    state.collapsed = "right";
    saveState(state);
    applyLayout();
    window.dispatchEvent(new Event("resize"));
  });
  expandLeft.addEventListener("click", () => {
    state.collapsed = null;
    saveState(state);
    applyLayout();
    window.dispatchEvent(new Event("resize"));
  });
  expandRight.addEventListener("click", () => {
    state.collapsed = null;
    saveState(state);
    applyLayout();
    window.dispatchEvent(new Event("resize"));
  });

  window.addEventListener("resize", () => {
    if (!dragging) applyLayout();
  });
  applyLayout();

  // --- recent activity drawer --------------------------------------------------
  const historyToggle = $("history-toggle");
  const historyDrawer = $("history-drawer");
  const historyBackdrop = $("history-backdrop");
  const historyClose = $("history-close");

  function setDrawerOpen(open) {
    historyDrawer.classList.toggle("open", open);
    show(historyBackdrop, open);
    historyToggle.setAttribute("aria-expanded", String(open));
  }
  historyToggle.addEventListener("click", () => setDrawerOpen(!historyDrawer.classList.contains("open")));
  historyClose.addEventListener("click", () => setDrawerOpen(false));
  historyBackdrop.addEventListener("click", () => setDrawerOpen(false));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && historyDrawer.classList.contains("open")) setDrawerOpen(false);
  });

  // --- reveal topbar controls once signed in ------------------------------------
  window.addEventListener("auth:ready", () => {
    show($("workflow-switch"), true);
    show(historyToggle, true);
  });

  // --- file strip: collapse the dropzone once a file is picked -------------------
  const dropzone = $("dropzone");
  const fileInput = $("file-input");
  const changeFileBtn = $("change-file-btn");
  const previewPlaceholder = $("preview-placeholder");

  function onFilePicked() {
    dropzone.classList.add("collapsed");
    show(changeFileBtn, true);
    show(previewPlaceholder, false);
  }
  fileInput.addEventListener("change", () => { if (fileInput.files.length) onFilePicked(); });
  dropzone.addEventListener("drop", () => onFilePicked());
  changeFileBtn.addEventListener("click", () => fileInput.click());
})();
