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
    // Nothing to split while the preview pane is hidden, and an inline
    // grid-template would override the single-column rule.
    if (split.classList.contains("solo")) {
      split.style.gridTemplateColumns = "";
      return;
    }
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

  // --- account menu --------------------------------------------------------------
  const userMenuBtn = $("user-menu-btn");
  const userMenuPanel = $("user-menu-panel");
  function setUserMenuOpen(open, focusItem) {
    show(userMenuPanel, open);
    userMenuBtn.setAttribute("aria-expanded", String(open));
    if (open && focusItem) $("logout-btn").focus();
  }
  userMenuBtn.addEventListener("click", (event) => {
    const opening = userMenuPanel.classList.contains("hidden");
    // Keyboard activation (detail 0) moves focus into the menu; a mouse click does not.
    setUserMenuOpen(opening, event.detail === 0);
  });
  document.addEventListener("click", (event) => {
    if (!userMenuPanel.classList.contains("hidden") && !$("user-bar").contains(event.target)) {
      setUserMenuOpen(false);
    }
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !userMenuPanel.classList.contains("hidden")) {
      setUserMenuOpen(false);
      userMenuBtn.focus();
    }
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
    document.body.classList.add("is-signed-in");
    show(document.querySelector(".landing-signin-hint"), false);
    show($("workflow-switch"), true);
    show(historyToggle, true);
    // Priority: a task already in the URL (direct visit/refresh/bookmark),
    // then a landing-option intent picked before sign-in, then the default.
    const intent = sessionStorage.getItem("workbench:intent");
    const chosen = SLUG_TASKS[currentPath().slice(1)] || intent || "check-card";
    window.selectWorkflow?.(chosen);
    // "/" is the front page for everyone, so a signed-in reload there stays
    // put; signing in from /login, or any task URL, opens the workspace.
    if (currentPath() === "/" && !intent) showLanding(false);
    else pushTaskUrl(chosen, true);
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
    // The front page lives in #auth-main, which sign-in hides; bring it back
    // (and put the workspace away) so it is reachable signed in too.
    show($("auth-main"), true);
    show($("workspace"), false);
    show(landingView, true);
    show(loginView, false);
    if (push) history.pushState({view: "landing"}, "", "/");
    window.scrollTo(0, 0);
  }
  function showWorkspace(task, push) {
    show($("auth-main"), false);
    show($("workspace"), true);
    window.selectWorkflow?.(task);
    pushTaskUrl(task, !push);
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

  // --- the brand mark always leads to the front page ------------------------------
  // Signed in or not. From there the calls to action and the workflow switch
  // lead back into the workspace.
  const brand = document.querySelector(".brand-mini");
  if (brand) {
    brand.tabIndex = 0;
    brand.setAttribute("role", "link");
    brand.setAttribute("aria-label", "ComplyBIM home");
    const goHome = () => showLanding(true);
    brand.addEventListener("click", goHome);
    brand.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); goHome(); }
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
  // --- hide the preview pane until there is a preview ----------------------------
  // app.js and ifc-viewer.js own #preview-card's visibility; watching it keeps
  // this presentational and avoids reaching into their state.
  const previewCard = $("preview-card");
  if (split && previewCard) {
    const syncSolo = () => {
      split.classList.toggle("solo", previewCard.classList.contains("hidden"));
      applyLayout();
    };
    new MutationObserver(syncSolo).observe(previewCard, {attributes: true, attributeFilter: ["class"]});
    syncSolo();
  }

  fileInput.addEventListener("change", () => { if (fileInput.files.length) onFilePicked(); });
  dropzone.addEventListener("drop", () => onFilePicked());
  changeFileBtn.addEventListener("click", () => fileInput.click());
})();
