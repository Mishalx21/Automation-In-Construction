/* ComplyBIM — workspace layout behavior.
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

  // --- task URLs: the signed-in workspace also gets a real path per task ---------
  // /design-validation for Design Validation, /mutation-lab for Mutation Lab
  // — nginx's SPA catch-all serves index.html for any path, so this is
  // plain history.pushState routing, no server change needed.
  const TASK_SLUGS = {"check-card": "design-validation", "inject-card": "mutation-lab"};
  const SLUG_TASKS = Object.fromEntries(Object.entries(TASK_SLUGS).map(([id, slug]) => [slug, id]));
  const currentPath = () => location.pathname.replace(/\/+$/, "") || "/";
  let signedIn = false;
  function pushTaskUrl(workflowId, replace) {
    const slug = TASK_SLUGS[workflowId];
    if (!slug || currentPath() === "/" + slug) return;
    history[replace ? "replaceState" : "pushState"]({workflow: workflowId}, "", "/" + slug);
  }
  // Signed-in users can visit the homepage too; these swap between it and
  // the workspace without touching the session.
  function setWorkspaceVisible(on) {
    show($("workspace"), on);
    show($("auth-main"), !on);
    // The 3D viewer sizes itself on resize; it measured 0x0 while hidden.
    if (on) window.dispatchEvent(new Event("resize"));
  }
  function openTask(workflowId, push) {
    setWorkspaceVisible(true);
    window.selectWorkflow?.(workflowId);
    if (push) pushTaskUrl(workflowId, false);
    window.scrollTo(0, 0);
  }
  document.querySelectorAll(".workflow-choice").forEach((button) => {
    button.addEventListener("click", () => {
      if (signedIn && $("workspace").classList.contains("hidden")) setWorkspaceVisible(true);
      pushTaskUrl(button.dataset.workflow, false);
    });
  });

  // --- reveal topbar controls once signed in ------------------------------------
  window.addEventListener("auth:ready", () => {
    signedIn = true;
    show(document.querySelector(".landing-signin-hint"), false);
    show($("workflow-switch"), true);
    show(historyToggle, true);
    // Priority: a task already in the URL (direct visit/refresh/bookmark),
    // then a landing-option intent picked before sign-in, then the default.
    const intent = sessionStorage.getItem("workbench:intent");
    const chosen = SLUG_TASKS[currentPath().slice(1)] || intent || "check-card";
    window.selectWorkflow?.(chosen);
    pushTaskUrl(chosen, true);
    if (intent) sessionStorage.removeItem("workbench:intent");
  });

  // --- landing vs. login: two distinct screens, not one scrolled page ------------
  // No build step / no router library, so this is the smallest thing that
  // still behaves like real navigation: swap which section is visible and
  // push a real /login URL (nginx's SPA catch-all already serves index.html
  // for any path, so a direct visit or refresh on /login works too).
  const landingView = $("landing-view");
  const loginView = $("login-view");
  function showLogin(push) {
    if (!landingView || !loginView) return;
    show(landingView, false);
    show(loginView, true);
    if (push) history.pushState({view: "login"}, "", "/login");
    window.scrollTo(0, 0);
    $("auth-email")?.focus();
  }
  function showLanding(push) {
    if (!landingView || !loginView) return;
    show(landingView, true);
    show(loginView, false);
    if (push) history.pushState({view: "landing"}, "", "/");
    window.scrollTo(0, 0);
  }
  if (landingView && loginView) {
    if (currentPath() === "/login") showLogin(false);
    else if (!SLUG_TASKS[currentPath().slice(1)]) showLanding(false);
    function goHome(push) {
      if (signedIn) setWorkspaceVisible(false);
      showLanding(push);
    }
    window.addEventListener("popstate", () => {
      const path = currentPath();
      if (signedIn) {
        const task = SLUG_TASKS[path.slice(1)];
        if (task) openTask(task, false);
        else goHome(false);
        return;
      }
      if (path === "/login") showLogin(false);
      else showLanding(false);
    });
    document.querySelectorAll(".landing-option").forEach((button) => {
      button.addEventListener("click", () => {
        if (signedIn) {
          openTask(button.dataset.intent, true);
          return;
        }
        sessionStorage.setItem("workbench:intent", button.dataset.intent);
        showLogin(true);
      });
    });
    $("landing-signin")?.addEventListener("click", () => showLogin(true));
    $("login-back")?.addEventListener("click", () => showLanding(true));
    $("brand-home")?.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      goHome(currentPath() !== "/");
    });
  }

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
