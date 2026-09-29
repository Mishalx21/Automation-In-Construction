/* ComplyBIM — single-page UI. Vanilla JS, no build step.
 *
 * Two engines behind one nginx origin:
 *   /ifc/api/...   ifcfault    (upload -> survey -> emit script -> verify)
 *   /bnbc/api/...  bnbc-web    (upload -> select checkers -> run -> report)
 *
 * The UI is English throughout. Technical values (rule ids, mm measurements,
 * code refs) stay in their original form. The picked file lives only in the
 * browser; each panel uploads it to its own engine when the user starts that
 * panel's action.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const show = (el, on = true) => (typeof el === "string" ? $(el) : el).classList.toggle("hidden", !on);
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let pickedFile = null;
let previewJobId = null;
let previewJobPromise = null;
// --- shared file picker --------------------------------------------------------
const dropzone = $("dropzone");
const fileInput = $("file-input");

dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("drag"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  if (e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0]);
});
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener("change", () => { if (fileInput.files.length) pickFile(fileInput.files[0]); });

function pickFile(file) {
  if (!file.name.toLowerCase().endsWith(".ifc")) {
    return showError("pick-error", "Only .ifc files are accepted.");
  }
  hideError("pick-error");
  pickedFile = file;
  window.resetIfcPreview?.();
  $("picked-name").textContent = `${file.name} — ${(file.size / 1048576).toFixed(1)} MB`;
  show("picked-name");
  $("inject-upload-btn").disabled = false;
  $("check-upload-btn").disabled = false;

  const loadPreviewButton = $("preview-load-button");
  // Source IFC is never decoded in Chrome. The ifcfault worker converts it to
  // XKT after Upload & analyze, which is safe for full project models.
  if (true) {
    show("preview-card");
    show("viewer-loading", true);
    $("viewer-loading-text").textContent = "Uploading model…";
    show(loadPreviewButton, false);
    $("preview-status").textContent = "Preparing server preview...";
    $("viewer-selection").textContent =
      `This ${(file.size / 1048576).toFixed(0)} MB IFC is ready to upload and analyze. Select Mutation Lab, then Analyze model to generate its server-side XKT preview.`;
    window.pendingIfcPreviewFile = null;
    previewJobId = null;
    previewJobPromise = prepareServerPreview(file);
    return;
  }
  show(loadPreviewButton, false);

  // Give immediate feedback even when xeokit/WebIFC is still downloading or
  // initialising. The viewer module will replace this state as it starts
  // reading the model.
  show("preview-card");
  show("viewer-loading");
  $("preview-status").textContent = "Preparing IFC preview…";
  $("viewer-selection").textContent = "Starting the IFC viewer in your browser…";
  // xeokit is an ES module and may still be initialising when a user picks a
  // file. Keep the file until the viewer exposes its loader, rather than
  // silently skipping the preview.
  if (window.loadIfcPreview) {
    window.pendingIfcPreviewFile = null;
    window.loadIfcPreview(file);
  } else {
    window.pendingIfcPreviewFile = file;
  }
}

$("preview-load-button").addEventListener("click", () => {
  $("preview-status").textContent = "Use the server-prepared preview";
  $("viewer-selection").textContent = "Upload and analyze the model in Mutation Lab to prepare an optimized XKT preview.";
});

function showError(id, msg) { const el = $(id); el.textContent = msg; show(el, true); }
function hideError(id) { show($(id), false); }

/** Progress line = pulsing status text + a bar (its own markup built once,
 * never rebuilt, so its animation never restarts on every tick). Pass
 * `percent` (0-100) when the total is known (e.g. N of M rules checked) for
 * a real filling bar; omit it for a scanning/indeterminate sweep. */
function setProgress(id, text, percent) {
  const el = $(id);
  if (!el.querySelector(".progress-bar")) {
    el.innerHTML = '<span class="ptext"></span><div class="progress-bar" aria-hidden="true"><div class="progress-fill"></div></div>';
  }
  el.querySelector(".ptext").textContent = text;
  const bar = el.querySelector(".progress-bar");
  bar.classList.toggle("indeterminate", percent == null);
  if (percent != null) {
    bar.querySelector(".progress-fill").style.setProperty("--fill", Math.max(.03, Math.min(1, percent / 100)));
  }
  show(el, true);
}
function stopProgress(id) { show($(id), false); }

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `request failed (${res.status})`);
  }
  return res.json();
}

async function uploadTo(prefix, file) {
  const body = new FormData();
  body.append("file", file);
  return api(`${prefix}/api/jobs`, { method: "POST", body });
}

async function prepareServerPreview(file) {
  try {
    const {job_id} = await uploadTo("/ifc", file);
    previewJobId = job_id;
    const job = await pollUntil(
      `/ifc/api/jobs/${job_id}`,
      (j) => j.state === "ready",
      (j) => {
        const text = j.state === "converting_preview"
          ? "Converting server preview..." : "Analyzing model for preview...";
        $("preview-status").textContent = text;
        $("viewer-loading-text").textContent = text;
      },
    );
    if (job.source_preview?.state === "ready") {
      window.setPreviewPropertiesJob?.(job_id);
      await window.loadXktPreviewUrl?.(`/ifc/api/jobs/${job_id}/preview/source`, "Original model preview ready");
    } else {
      show("viewer-loading", false);
      $("preview-status").textContent = "Server preview unavailable";
      $("viewer-selection").textContent = "The IFC uploaded successfully, but its optimized preview could not be created.";
    }
    return job;
  } catch (error) {
    show("viewer-loading", false);
    $("preview-status").textContent = "Preview upload failed";
    $("viewer-selection").textContent = "Could not prepare server preview: " + (error.message || error);
    throw error;
  }
}

function pollUntil(jobPath, isTerminal, onTick, intervalMs = 1500) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      let job;
      try {
        job = await api(jobPath);
      } catch (e) {
        return; // transient network error — keep polling
      }
      if (job.state === "failed") {
        clearInterval(timer);
        return reject(new Error(job.error || "job failed"));
      }
      if (isTerminal(job)) {
        clearInterval(timer);
        return resolve(job);
      }
      onTick(job);
    }, intervalMs);
  });
}

// =============================================================================
// Panel A — ifcinject (inject violations)
// =============================================================================
let ifcJobId = null;
let lastInjectResultsJob = null;
let lastAnalysisRules = [];

$("inject-upload-btn").addEventListener("click", async () => {
  hideError("inject-error");
  show("inject-rules", false);
  show("inject-results", false);
  resetBatchState();
  $("inject-upload-btn").disabled = true;
  setProgress("inject-progress", "Uploading…");
  try {
    if (previewJobPromise) {
      try {
        await previewJobPromise;
      } catch (_) {
        // A preview is optional. Fall back to a normal analysis upload.
        previewJobId = null;
      }
    }
    if (!previewJobId) {
      const { job_id } = await uploadTo("/ifc", pickedFile);
      previewJobId = job_id;
    }
    ifcJobId = previewJobId;
    const job = await pollUntil(
      `/ifc/api/jobs/${ifcJobId}`,
      (j) => j.state === "ready",
      (j) => setProgress("inject-progress", "Analyzing model (running all 10 rule queries)…"));
    stopProgress("inject-progress");
    renderInjectMatrix(job);
  } catch (e) {
    stopProgress("inject-progress");
    showError("inject-error", `Upload failed: ${e.message}`);
  } finally {
    $("inject-upload-btn").disabled = false;
  }
});

function renderInjectMatrix(job) {
  const a = job.analysis;
  $("inject-file-meta").textContent =
    `${job.filename} — ${a.schema_name || "IFC"} — ${(a.file_size_bytes / 1048576).toFixed(1)} MB`;
  buildRuleTable($("inject-table-archi"), a.rules.filter((r) => r.domain === "architectural"), true, "inject-cb");
  buildRuleTable($("inject-table-struct"), a.rules.filter((r) => r.domain === "structural"), true, "inject-cb");
  setupRuleFilter("inject", "inject-cb", "inject-btn");
  show("inject-rules", true);
  refreshRunButton("inject-btn", ".inject-cb");
  lastAnalysisRules = a.rules || [];
  const applicableCount = lastAnalysisRules.filter((r) => r.applicable !== false).length;
  $("batch-pool-hint").textContent = applicableCount
    ? `Each case injects a random 1–${applicableCount} of the ${applicableCount} rule(s) applicable to this model.`
    : "No rules are applicable to this model, so batch generation is unavailable.";
  resetBatchState();
  $("inject-mode-batch").disabled = applicableCount === 0;
  $("batch-generate-btn").disabled = applicableCount === 0;
  setInjectMode("manual");
  if (job.source_preview?.state === "ready") {
    window.setPreviewPropertiesJob?.(ifcJobId);
    window.loadXktPreviewUrl?.(`/ifc/api/jobs/${ifcJobId}/preview/source`, "Original model preview ready");
  } else {
    $("preview-status").textContent = "Server preview unavailable";
    $("viewer-selection").textContent = "The model is ready for analysis and injection; only its optional 3D conversion was unavailable.";
  }
}

/** "A2" < "A10", not the other way round — plain string sort gets this wrong. */
function compareRuleIds(a, b) {
  const pa = String(a).match(/^([A-Za-z]*)(\d+)$/);
  const pb = String(b).match(/^([A-Za-z]*)(\d+)$/);
  if (pa && pb && pa[1] === pb[1]) return parseInt(pa[2], 10) - parseInt(pb[2], 10);
  return String(a).localeCompare(String(b));
}

// The injection analysis prefixes every description with "Inject a controlled
// A10 violation." — the rule id is already in the bubble beside it, so the
// sentence only pushes the part that matters further down the column.
const RULE_PREFIX = /^inject a controlled\s+[a-z]?\d+[a-z]?\s+violation\.?\s*/i;
const ruleText = (r) =>
  (r.description || "").replace(RULE_PREFIX, "").trim()
  || r.title
  || catalogueById[r.rule_id]?.title
  || r.description
  || r.rule_id;

function buildRuleTable(table, rules, withApplicability, cbClass) {
  // Candidate count and the "not applicable" badge only ever have content
  // for the per-model injection analysis; the check-compliance catalogue
  // has neither, so those columns are dropped there instead of sitting
  // empty and stealing width from the description column.
  table.classList.toggle("with-applicability", withApplicability);
  const sorted = [...rules].sort((a, b) => compareRuleIds(a.rule_id, b.rule_id));
  table.innerHTML = `
    <tr><th></th><th>Rule</th><th>${withApplicability ? "What it injects" : "What it checks"}</th>${withApplicability
      ? `<th title="Elements in this model eligible for this rule's violation">Found</th><th></th>` : ""}</tr>` +
    sorted.map((r) => `
    <tr class="${r.applicable === false ? "na" : ""}">
      <td>${r.applicable === false ? "" : `<input type="checkbox" class="${cbClass}" value="${r.rule_id}" aria-label="${escapeHtml(r.rule_id)}">`}</td>
      <td class="rule-id"><span class="rule-bubble">${escapeHtml(r.rule_id)}</span></td>
      <td>${escapeHtml(ruleText(r))}<span class="clause">${escapeHtml(r.clause || r.reference)}</span>
        ${!withApplicability && r.rule_preview ? `<details class="source-rule"><summary>View source rule</summary><pre>${escapeHtml(r.rule_preview)}</pre></details>` : ""}</td>
      ${withApplicability ? `
      <td class="num">${r.candidate_count != null ? r.candidate_count : ""}</td>
      <td>${r.applicable === false
        ? `<span class="badge na" title="${escapeHtml(r.reason || "")}">not applicable</span>`
        : ""}</td>` : ""}
    </tr>`).join("");
  table.querySelectorAll(`.${cbClass}`).forEach((cb) =>
    cb.addEventListener("change", () => refreshRunButton(cbClass === "inject-cb" ? "inject-btn" : "check-btn", `.${cbClass}`)));
}

// --- rule filter -------------------------------------------------------------------
// Twenty rules in two tables is more than anyone reads at once, so the list is
// searchable and starts with nothing selected. Rows are hidden rather than
// removed, so a checkbox that scrolls out of the filter keeps its state.
function setupRuleFilter(prefix, cbClass, btnId) {
  const search = $(`${prefix}-filter`);
  const count = $(`${prefix}-count`);
  const tables = [$(`${prefix}-table-archi`), $(`${prefix}-table-struct`)].filter(Boolean);
  if (!search || !tables.length) return;

  const rows = () => tables.flatMap((t) => [...t.querySelectorAll("tr")].slice(1));
  const boxes = () => [...document.querySelectorAll(`.${cbClass}`)];

  function updateCount() {
    const all = boxes();
    const picked = all.filter((cb) => cb.checked).length;
    if (count) count.textContent = `${picked}/${all.length} selected`;
    refreshRunButton(btnId, `.${cbClass}`);
  }

  function applyFilter() {
    const q = search.value.trim().toLowerCase();
    rows().forEach((row) => {
      row.classList.toggle("filtered-out", q !== "" && !row.textContent.toLowerCase().includes(q));
    });
  }

  search.oninput = applyFilter;
  // Select all / Clear act on what the filter is currently showing, which is
  // what "all" means once a filter is typed.
  $(`${prefix}-select-all`).onclick = () => {
    rows().forEach((row) => {
      if (row.classList.contains("filtered-out")) return;
      const cb = row.querySelector(`.${cbClass}`);
      if (cb) cb.checked = true;
    });
    updateCount();
  };
  $(`${prefix}-clear`).onclick = () => {
    boxes().forEach((cb) => { cb.checked = false; });
    updateCount();
  };
  boxes().forEach((cb) => cb.addEventListener("change", updateCount));
  search.value = "";
  applyFilter();
  updateCount();
}

function refreshRunButton(btnId, cbClass) {
  $(btnId).disabled = document.querySelectorAll(`${cbClass}:checked`).length === 0;
}

// --- batch test-case generation --------------------------------------------------
function setInjectMode(mode) {
  $("inject-mode-manual").classList.toggle("active", mode === "manual");
  $("inject-mode-batch").classList.toggle("active", mode === "batch");
  show("inject-manual-mode", mode === "manual");
  show("inject-batch-mode", mode === "batch");
}
$("inject-mode-manual").addEventListener("click", () => setInjectMode("manual"));
$("inject-mode-batch").addEventListener("click", () => setInjectMode("batch"));

function resetBatchState() {
  $("batch-case-list").innerHTML = "";
  show("batch-progress", false);
  $("batch-count").disabled = false;
  $("batch-generate-btn").disabled = false;
  $("batch-generate-btn").textContent = "Generate batch";
}

/** Fisher–Yates shuffle; does not mutate the input array. */
function shuffle(array) {
  const a = array.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** A random, non-empty subset (1..pool.length items) of the given pool. */
function randomRuleSubset(pool) {
  const n = 1 + Math.floor(Math.random() * pool.length);
  return shuffle(pool).slice(0, n);
}

function clampInt(value, min, max, fallback) {
  const n = parseInt(value, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function batchCaseRow(idx) {
  return `
  <div class="batch-case" id="batch-case-${idx}">
    <div class="batch-case-head">
      <span class="dot wait" id="batch-case-dot-${idx}" aria-hidden="true"></span>
      <span class="v-num">${idx + 1}</span>
      <span class="badge wait" id="batch-case-badge-${idx}">queued</span>
      <span class="batch-case-msg" id="batch-case-msg-${idx}"></span>
    </div>
    <div class="batch-case-body hidden" id="batch-case-body-${idx}"></div>
  </div>`;
}

function setBatchCaseStatus(idx, status, msg) {
  const tone = {queued: "wait", running: "warn", done: "ok", failed: "bad"}[status] || "wait";
  $(`batch-case-dot-${idx}`).className = `dot ${tone}`;
  const badge = $(`batch-case-badge-${idx}`);
  badge.className = `badge ${tone}`;
  badge.textContent = status;
  $(`batch-case-msg-${idx}`).textContent = msg || "";
}

function renderBatchCaseResult(idx, jobId, rules, job) {
  setBatchCaseStatus(idx, "done");
  const body = $(`batch-case-body-${idx}`);
  body.classList.remove("hidden");
  const injectedIds = (job.mutations || [])
    .filter((m) => m.attribute !== "(entity deleted)")
    .map((m) => m.target_global_id)
    .filter(Boolean);
  body.innerHTML = `
    <div class="mut-meta">${rules.map((r) => `<span class="rule-bubble bad">${escapeHtml(r)}</span>`).join("")}</div>
    <p class="dl-row">
      <a class="btn small" href="/ifc/api/jobs/${jobId}/download">Violating .ifc</a>
      <a class="btn small" href="/ifc/api/jobs/${jobId}/download/colored">Coloured .ifc</a>
      <a class="btn small" href="/ifc/api/jobs/${jobId}/download/script">Script</a>
      <a class="btn small" href="/ifc/api/jobs/${jobId}/download/report" target="_blank">Report (JSON)</a>
      ${job.coloured_preview?.state === "ready" && injectedIds.length
        ? `<button class="btn small preview-batch-case" type="button">Preview in model</button>` : ""}
    </p>`;
  body.querySelector(".preview-batch-case")?.addEventListener("click", () => {
    window.prepareViolatingXktPreview?.(`/ifc/api/jobs/${jobId}/preview/violating`);
    window.focusInjectedViolation?.(injectedIds);
    $("preview-card").scrollIntoView({behavior: "smooth", block: "center"});
  });
}

function updateBatchSummary(done, total) {
  $("batch-progress-summary").textContent = `${done}/${total} test case(s) complete`;
}

$("batch-generate-btn").addEventListener("click", async () => {
  const applicable = lastAnalysisRules.filter((r) => r.applicable !== false).map((r) => r.rule_id);
  if (!applicable.length) return;
  const count = clampInt($("batch-count").value, 1, 20, 5);
  hideError("inject-error");
  $("batch-generate-btn").disabled = true;
  $("batch-count").disabled = true;
  $("batch-case-list").innerHTML = Array.from({length: count}, (_, i) => batchCaseRow(i)).join("");
  show("batch-progress", true);
  updateBatchSummary(0, count);

  for (let i = 0; i < count; i++) {
    try {
      setBatchCaseStatus(i, "running", "Cloning model…");
      const {job_id} = await api(`/ifc/api/jobs/${ifcJobId}/clone`, {method: "POST"});
      const rules = randomRuleSubset(applicable);
      setBatchCaseStatus(i, "running", `Injecting ${rules.join(", ")}…`);
      await api(`/ifc/api/jobs/${job_id}/inject`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rules: rules.map((rule) => ({rule}))}),
      });
      const job = await pollUntil(
        `/ifc/api/jobs/${job_id}`,
        (j) => j.state === "done",
        (j) => setBatchCaseStatus(i, "running", `${j.state}…`));
      renderBatchCaseResult(i, job_id, rules, job);
      window.recordHistory?.("ifcfault", job);
    } catch (e) {
      setBatchCaseStatus(i, "failed", e.message || "generation failed");
    }
    updateBatchSummary(i + 1, count);
  }
  $("batch-generate-btn").disabled = false;
  $("batch-count").disabled = false;
});

$("inject-btn").addEventListener("click", async () => {
  hideError("inject-error");
  const rules = [...document.querySelectorAll(".inject-cb:checked")].map((cb) => ({ rule: cb.value }));
  const injectButton = $("inject-btn");
  injectButton.disabled = true;
  injectButton.classList.add("is-loading");
  injectButton.setAttribute("aria-busy", "true");
  injectButton.innerHTML = '<span class="button-spinner" aria-hidden="true"></span>Injecting selected…';
  setProgress("inject-progress", "Generating validated injection script…");
  try {
    await api(`/ifc/api/jobs/${ifcJobId}/inject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules }),
    });
    const job = await pollUntil(
      `/ifc/api/jobs/${ifcJobId}`,
      (j) => j.state === "done",
      (j) => setProgress("inject-progress", {
        queued_inject: "Queued for injection…",
        emitting: "Generating and validating the injection script…",
        writing_outputs: "Producing plain and coloured IFC outputs…",
        verifying: "Finalizing independently verified artifacts…",
      }[j.state] || `${j.state}…`));
    stopProgress("inject-progress");
    lastInjectResultsJob = job;
    renderInjectResults(job);
    window.recordHistory?.("ifcfault", job);
  } catch (e) {
    stopProgress("inject-progress");
    showError("inject-error", e.message);
  } finally {
    injectButton.classList.remove("is-loading");
    injectButton.removeAttribute("aria-busy");
    injectButton.textContent = "Inject selected";
    refreshRunButton("inject-btn", ".inject-cb");
  }
});

// --- injection results rendering ------------------------------------------------
// Mutation dicts (from ifcfault's output record):
//   { rule_id, element_type, target_global_id, attribute, before, after,
//     clause, description, extra }
// Verification dict (verify/report.py):
//   { case_id, kind, checks: [{name, passed, evidence, message}], all_passed }

function fmtVal(v) {
  if (v == null) return "—";
  if (typeof v === "number") {
    // IFC attribute values often arrive as raw floats with binary rounding
    // noise (e.g. 915.0000000000005) — round to a sane display precision
    // instead of showing that noise as if it were meaningful.
    const rounded = Math.round(v * 1000) / 1000;
    return String(rounded);
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function mutationCard(m, idx) {
  const deleted = m.attribute === "(entity deleted)";
  const targetGuid = m.target_global_id || "";
  const change = deleted
    ? `<span class="chg chg-del">entity deleted</span>`
    : `<span class="chg"><s>${escapeHtml(fmtVal(m.before))}</s><span class="chg-arrow" aria-hidden="true">→</span><b>${escapeHtml(fmtVal(m.after))}</b></span>`;
  return `
  <div class="mut-card">
    <div class="mut-head">
      <span class="v-num">${idx + 1}</span>
      <span class="rule-bubble bad">${escapeHtml(m.rule_id || "")}</span>
      <span class="mut-type">${escapeHtml(m.element_type || "")}</span>
      <span class="mut-attr">${escapeHtml(m.attribute || "")}</span>
      ${change}
    </div>
    ${m.description ? `<p class="mut-desc">${escapeHtml(m.description)}</p>` : ""}
    <div class="mut-meta">
      ${m.target_global_id ? `<span class="chip mono" title="Target GlobalId">${escapeHtml(m.target_global_id)}</span>` : ""}
      ${m.clause ? `<span class="chip">Clause ${escapeHtml(m.clause)}</span>` : ""}
      ${targetGuid && !deleted ? `<button class="show-in-model show-in-injected-model" type="button" data-guid="${escapeHtml(targetGuid)}">Show in model</button>` : ""}
    </div>
  </div>`;
}

function verificationPanel(ver) {
  const checks = ver?.checks || [];
  if (!checks.length) return "";
  const rows = checks.map((c) => `
    <li>
      <span class="badge ${c.passed ? "ok" : "bad"}">${c.passed ? "✓ pass" : "✕ fail"}</span>
      <span class="vc-name">${escapeHtml(c.name)}</span>
      ${c.message ? `<span class="vc-msg">${escapeHtml(c.message)}</span>` : ""}
    </li>`).join("");
  const passed = checks.filter((c) => c.passed).length;
  return `
  <details class="ver-checks">
    <summary>How the result was double-checked — ${passed} of ${checks.length} passed</summary>
    <ul>${rows}</ul>
  </details>`;
}

function renderInjectResults(job) {
  show("inject-rules", false);
  const muts = job.mutations || [];
  const ver = job.verification || {};
  const checks = ver.checks || [];
  const passedChecks = checks.filter((c) => c.passed).length;
  const allPassed = ver.all_passed === true;

  const banner = allPassed
    ? `<div class="banner ok"><span class="b-icon" aria-hidden="true">✓</span>
        <div><strong>Done — ${muts.length} rule${muts.length !== 1 ? "s" : ""} broken, and each one confirmed.</strong>
        Take the broken model below, pick it in step 1, and run Design Validation: the checks should now catch it.</div></div>`
    : `<div class="banner bad"><span class="b-icon" aria-hidden="true">✕</span>
        <div><strong>The change could not be confirmed</strong> (${passedChecks} of ${checks.length} checks passed).
        Do not rely on this file — open the report to see which check failed.</div></div>`;

  $("inject-mutations").innerHTML = `
    ${banner}
    <div class="tiles">
      ${statTile("Rules broken", muts.length, "bad")}
      ${statTile("Checks run on the result", checks.length, "na")}
      ${statTile("Checks passed", checks.length ? `${passedChecks}/${checks.length}` : "—", allPassed ? "ok" : "bad")}
    </div>
    ${muts.map(mutationCard).join("")}
    ${verificationPanel(ver)}`;

  $("inject-download").href = `/ifc/api/jobs/${ifcJobId}/download`;
  $("inject-colored-download").href = `/ifc/api/jobs/${ifcJobId}/download/colored`;
  $("inject-script").href = `/ifc/api/jobs/${ifcJobId}/download/script`;
  $("inject-report").href = `/ifc/api/jobs/${ifcJobId}/download/report`;
  // Never parse a generated IFC automatically. Coloured output can be hundreds
  // of MB, and WebIFC geometry decoding on Chrome's main thread can freeze a
  // tab. The viewer enables a manual violating-model tab only when it is safe.
  window.prepareViolatingXktPreview?.(
    job.coloured_preview?.state === "ready" ? `/ifc/api/jobs/${ifcJobId}/preview/violating` : null,
  );
  const injectedIds = muts
    .filter((mutation) => mutation.attribute !== "(entity deleted)")
    .map((mutation) => mutation.target_global_id)
    .filter(Boolean);
  window.focusInjectedViolation?.(injectedIds);
  $("inject-mutations").querySelectorAll(".show-in-injected-model").forEach((button) => {
    const focus = () => {
      window.focusInjectedViolation?.([button.dataset.guid]);
      $("preview-card").scrollIntoView({behavior: "smooth", block: "center"});
    };
    button.addEventListener("click", focus);
  });
  show("inject-results", true);
}

// =============================================================================
// Panel B — bnbc-web (check compliance)
// =============================================================================
let bnbcJobId = null;
let lastCheckJob = null;
let catalogueById = {};   // rule_id -> catalogue entry, for result-card titles

async function renderCheckerCatalogue() {
  try {
    const checkers = await api("/bnbc/api/checkers");
    catalogueById = Object.fromEntries(checkers.map((c) => [c.rule_id, c]));
    buildRuleTable($("check-table-archi"), checkers.filter((c) => c.domain === "architectural"), false, "check-cb");
    buildRuleTable($("check-table-struct"), checkers.filter((c) => c.domain === "structural"), false, "check-cb");
    setupRuleFilter("check", "check-cb", "check-btn");
    refreshRunButton("check-btn", ".check-cb");
  } catch (e) {
    showError("check-error", `Could not load the checker catalogue: ${e.message}`);
  }
}

$("check-upload-btn").addEventListener("click", async () => {
  hideError("check-error");
  show("check-rules", false);
  show("check-results", false);
  $("check-upload-btn").disabled = true;
  setProgress("check-progress", "Uploading…");
  try {
    const { job_id } = await uploadTo("/bnbc", pickedFile);
    bnbcJobId = job_id;
    const job = await api(`/bnbc/api/jobs/${bnbcJobId}`);
    stopProgress("check-progress");
    $("check-file-meta").textContent = `${job.filename} — ready. Pick the checkers to run (all preselected).`;
    show("check-rules", true);
  } catch (e) {
    stopProgress("check-progress");
    showError("check-error", `Upload failed: ${e.message}`);
  } finally {
    $("check-upload-btn").disabled = false;
  }
});

$("check-btn").addEventListener("click", async () => {
  hideError("check-error");
  const rules = [...document.querySelectorAll(".check-cb:checked")].map((cb) => cb.value);
  $("check-btn").disabled = true;
  checkFilter = null;
  setProgress("check-progress", "Queued…");
  try {
    await api(`/bnbc/api/jobs/${bnbcJobId}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules }),
    });
    const job = await pollUntil(
      `/bnbc/api/jobs/${bnbcJobId}`,
      (j) => j.state === "done",
      (j) => {
        const done = (j.results || []).length;
        setProgress("check-progress",
          `Checking… ${done}/${rules.length} rule(s) done`, (done / rules.length) * 100);
        lastCheckJob = j;
        renderCheckResults(j); // stream partial results
      });
    stopProgress("check-progress");
    lastCheckJob = job;
    renderCheckResults(job, true);
    window.recordHistory?.("bnbc", job);
  } catch (e) {
    stopProgress("check-progress");
    showError("check-error", e.message);
  } finally {
    $("check-btn").disabled = false;
  }
});

// --- verdict badges & labels -----------------------------------------------------
const VERDICT_BADGE = {
  pass: "ok",
  fail: "bad",
  violation: "bad",
  unknown: "warn",
  not_applicable: "na",
  error: "bad",
};
const VERDICT_LABEL = {
  pass: "pass",
  fail: "fail",
  violation: "violation",
  unknown: "unknown",
  not_applicable: "not applicable",
  error: "error",
};

// =============================================================================
// Plain-language explainers — one per checker condition id.
//
// The checkers emit terse machine strings ("L/d=22.6", "support_found=False").
// Engineers need the human version: what the number means on this element,
// why the code cares, and what to look at next. Each explainer gets the
// violation's kv pairs (parsed from the first location's `measured`) plus the
// raw string, and returns { plain, why, check } — any field may be omitted.
// =============================================================================
const EXPLAINERS = {
  // --- architectural, international analogues (A1–A5) ---
  egress_door_min_clear_width: {
    plain: (kv) => `This door gives only <b>${kv.width || "?"}</b> of clear opening — the code minimum is <b>${kv.required || "815 mm"}</b>. A narrower door is too tight for a wheelchair to pass and becomes the bottleneck when the floor has to empty in a hurry.`,
    why: "Egress doors are sized for evacuation flow and wheelchair access; an under-width door fails both.",
    check: "Widen the door leaf / clear opening to at least the required width.",
  },
  stair_riser_max_height: {
    plain: (kv) => `Each step of this stair rises <b>${kv.riser || "?"}</b> — the maximum allowed is <b>${kv.required || "178 mm"}</b>. Steeper stairs are exhausting to climb and a fall hazard, especially going down.`,
    why: "High risers force people to over-stride; falls on steep stairs are a leading cause of stair injuries.",
    check: "Add a step to reduce riser height (and re-check tread depth so the going stays comfortable).",
  },
  stair_tread_min_depth: {
    plain: (kv) => `The tread — where your foot actually lands — is only <b>${kv.tread || "?"}</b> deep, below the <b>${kv.required || "279 mm"}</b> minimum. A heel can overhang the step edge and catch on the way down.`,
    why: "Shallow treads are the classic cause of mis-steps and falls on stairs.",
    check: "Deepen the treads, or reconfigure riser/tread together (2×riser + tread ≈ 630–650 mm).",
  },
  wall_fire_rating_min: {
    plain: (kv) => `This wall is fire-rated for only <b>${kv.FireRating || "?"}</b> but this location requires at least <b>${kv.required || "120 min"}</b> — it will let fire through before the rated period is up.`,
    why: "Fire-rated walls compartment a building so a fire in one part doesn't spread to the rest before occupants escape.",
    check: "Upgrade the wall build-up (board layers, type) to achieve the required rating.",
  },
  wall_fire_rating_removed: {
    plain: () => `This wall <i>used to carry</i> a fire rating, but the rating is now blank — the fire compartment has silently lost its protection.`,
    why: "A removed rating is not the same as a wall that never had one; the compartmentation design assumed it.",
    check: "Restore the fire rating property, or re-verify the compartmentation design without this wall.",
  },
  door_in_rated_wall_needs_rating: {
    plain: (kv, raw) => {
      const host = (raw.match(/host wall=(\d+)/) || [])[1];
      return `This door sits in a <b>${host || "?"} min</b> fire-rated wall but the door itself has <b>no rating</b> — an unprotected opening defeats the whole wall.`;
    },
    why: "Any opening in a fire-rated wall (door, damper, penetration) must be protected to the same level, or fire walks straight through it.",
    check: "Install a rated door/doorset matching the wall's rating, with rated hardware and seals.",
  },
  door_clear_floor_space_obstructed: {
    plain: (kv) => `The clear floor space a wheelchair user needs to reach and pull open this door is blocked — <b>${kv.obstruction || "an element"}</b> intrudes into the maneuvering clearance.`,
    why: "Without clear space on the pull side, a wheelchair user cannot turn, reach the handle and swing the door open.",
    check: "Relocate the obstructing element, or re-swing/re-position the door to restore the required clearance.",
  },
  corridor_dead_end_length: {
    plain: (kv) => `This corridor runs <b>${kv.length || "?"}</b> past the last exit door and stops — a dead end. Anyone who enters it must double back the whole way to escape.`,
    why: "In smoke, people tend to keep moving forward; long dead ends trap them.",
    check: "Add an exit along the dead-end section, or re-plan the corridor layout to shorten/eliminate it.",
  },
  exit_separation_min: {
    plain: (kv) => `The two exits are only <b>${kv.separation || "?"}</b> apart — the code requires at least <b>${kv.required || "?"}</b>. One incident (fire, blockage) at that spot could cut off both.`,
    why: "Remote exits are separated so a single event can't block all escape routes at once.",
    check: "Re-locate one exit so the two are far enough apart (measured along the path of travel).",
  },
  // --- architectural, BNBC clauses (A6–A10) ---
  room_min_ceiling_height: {
    plain: (kv) => `This room is only <b>${kv.height || "?"}</b> from floor to ceiling — the code asks for at least <b>${kv.required || "2750 mm"}</b> in a habitable room. A low ceiling makes a room feel oppressive and traps warm, stale air over the people in it.`,
    why: "The code sets 2.75 m so that a habitable room holds enough air volume and can be ventilated and lit properly.",
    check: "Raise the floor-to-floor height, or reduce the floor and ceiling build-ups so the clear height comes back.",
  },
  corridor_min_ceiling_height: {
    plain: (kv) => `This escape corridor has only <b>${kv.height || "?"}</b> of headroom — the minimum for a corridor used as a means of egress is <b>${kv.required || "2400 mm"}</b>.`,
    why: "Smoke banks down from the ceiling, so a low corridor fills with it sooner and leaves less clear air to escape through.",
    check: "Raise the ceiling, or re-route the services and bulkheads that are eating the headroom.",
  },
  room_min_floor_area: {
    plain: (kv) => `This room has only <b>${kv.area || "?"}</b> of floor area; the minimum for a ${kv.room_type === "other" ? "non-habitable" : "habitable"} room is <b>${kv.required || "?"}</b>.`,
    why: "The code sets a floor area per room so a dwelling cannot be subdivided into cells too small to live or breathe in.",
    check: "Combine it with the adjoining space, or re-plan the floor so the room reaches the minimum area.",
  },
  room_min_least_width: {
    plain: (kv) => `The narrow side of this room measures <b>${kv.width || "?"}</b>, below the <b>${kv.required || "?"}</b> minimum. A room can meet its area target and still be an unusable corridor-shaped strip — this one does.`,
    why: "The code fixes a least width as well as an area, precisely so the area cannot be met by a long thin room.",
    check: "Re-proportion the room, moving the partition so the short dimension reaches the minimum.",
  },
  space_min_opening_area_ratio: {
    plain: (kv) => `The windows serving this room add up to <b>${kv.opening_area || "?"}</b> against a floor area of <b>${kv.floor_area || "?"}</b> — an opening ratio of <b>${kv.opening_ratio || "?"}</b>, where the code requires <b>${kv.required || "?"}</b>.`,
    why: "The code ties daylight and natural ventilation to floor area; under-glazed rooms stay dark and stuffy in a hot, humid climate.",
    check: "Enlarge or add windows in the exterior wall. Doors do not count towards this ratio, even glazed ones.",
  },
  guard_min_height: {
    plain: (kv) => `This guard stands <b>${kv.height || "?"}</b> above the floor — the minimum is <b>${kv.required || "1000 mm"}</b>. A guard below waist height stops being a barrier and becomes something to trip over the top of.`,
    why: "The code requires a 1 m parapet or guardrail at every accessible flat roof and open edge, because an adult's centre of gravity sits above a low rail.",
    check: "Raise the guard to at least 1 m, or replace it with a parapet of the required height.",
  },
  stair_handrail_min_height: {
    plain: (kv) => `This handrail sits <b>${kv.height || "?"}</b> above the stair nosing, below the <b>${kv.required || "900 mm"}</b> minimum — too low to catch hold of naturally on the way down.`,
    why: "The code sets 0.9 m from the nose of the stair so the rail meets the hand where it falls.",
    check: "Raise the handrail, measuring from the tread nosing rather than from the landing.",
  },
  stairway_min_width: {
    plain: (kv) => `This stairway is <b>${kv.width || "?"}</b> wide against a minimum of <b>${kv.required || "1120 mm"}</b>${kv.measured_on && kv.measured_on.indexOf("enclosure") !== -1 ? " (measured across the whole stair enclosure, so the flight itself is narrower still)" : ""}. Two people cannot pass on it, and it cannot carry the flow of a floor emptying at once.`,
    why: "The code sizes egress stairs by occupancy; 1120 mm is the lowest value in the table, and a hospital or a large school needs 2235 mm.",
    check: "Widen the flight, or add a second stair so each carries less of the occupant load.",
  },
  // --- structural, international analogues (S1–S5) ---
  beam_span_to_depth_max: {
    plain: (kv) => `This beam spans <b>${kv.span || "?"}</b> but its depth is only <b>${kv.depth || "?"}</b> — a span-to-depth ratio of <b>${kv["L/d"] || "?"}</b> against the limit of <b>20</b>. It is too shallow for its span.`,
    why: "Shallow beams deflect (sag) too much — partition walls and finishes crack, floors feel bouncy — even when strength is fine.",
    check: "Deepen the beam, add an intermediate support/column to halve the span, or check deflection explicitly.",
  },
  column_min_dimension: {
    plain: (kv) => `The thinnest side of this column is only <b>${kv.least_dimension || "?"}</b> — below the <b>${kv.required || "200 mm"}</b> minimum.`,
    why: "Very thin columns are hard to cast properly (concrete can't consolidate around rebar) and buckle more easily.",
    check: "Resize the column section so no side is below the minimum dimension.",
  },
  column_slenderness_max: {
    plain: (kv) => `This column is <b>${kv.height || "?"}</b> tall with a thinnest side of <b>${kv.least_dimension || "?"}</b> — slenderness ratio <b>${kv.ratio || "?"}</b> against the limit of <b>15</b>. It is disproportionately thin for its height.`,
    why: "Slender columns don't crush — they buckle sideways under load, with little warning.",
    check: "Widen the section, or brace the column mid-height to cut its effective length.",
  },
  slab_thickness_min: {
    plain: (kv) => `This slab is only <b>${kv.thickness || "?"}</b> thick but spans <b>${kv.span || "?"}</b> — a span that length needs at least <b>${kv.required || "?"}</b>.`,
    why: "Thin long-span slabs deflect and vibrate (noticeable footfall bounce); serviceability fails long before strength does.",
    check: "Thicken the slab, shorten the span with a beam, or verify by explicit deflection calculation.",
  },
  column_continuous_support: {
    plain: (kv, raw) => {
      const reason = (raw.match(/\((.*)\)/) || [])[1] || "no column beneath it";
      return `This column ends in mid-air — <b>${escapeHtml(reason)}</b>. Its load has no direct path to the ground and must detour through the beam below: a <b>floating column</b>.`;
    },
    why: "Floating columns dump concentrated load onto transfer beams — a recognized collapse mechanism in earthquakes.",
    check: "Add a column/footing directly below, or design the transfer beam explicitly for the concentrated load.",
  },
  storey_lateral_wall_deficit: {
    plain: (kv) => `This storey has <b>${kv.load_bearing_walls || "0"} load-bearing walls</b> while a typical storey in this building has <b>${kv.building_median || "?"}</b> — a sudden drop in stiffness from one floor to the next.`,
    why: "A soft storey (often the open ground floor) takes almost all the sway in an earthquake and can fail first — the classic pancake collapse.",
    check: "Add walls/bracing to the weak storey, or design dedicated lateral resisting frames for it.",
  },
  // --- structural, BNBC clauses (S6–S10) ---
  masonry_bearing_wall_min_thickness: {
    plain: (kv) => `This load-bearing masonry wall is only <b>${kv.thickness || "?"}</b> thick — the code requires a nominal <b>${kv.required || "250 mm"}</b> for a wall carrying vertical load.`,
    why: "A thin masonry bearing wall is slender out of plane, so it buckles or is pushed over long before its bricks are crushed.",
    check: "Thicken the wall, or take the load off it with a frame and re-classify it as non-load-bearing.",
  },
  concrete_bearing_wall_min_thickness: {
    plain: (kv) => `This concrete bearing wall is <b>${kv.thickness || "?"}</b> thick, under the <b>${kv.required || "?"}</b> its supported height demands.`,
    why: "The code ties a bearing wall's thickness to 1/25 of the height it supports, with 100 mm as the floor — the taller the wall, the thicker it has to be to stay stable.",
    check: "Thicken the wall, brace it at mid-height to shorten the supported height, or design it explicitly rather than by the empirical method.",
  },
  smf_column_min_dimension: {
    plain: (kv) => `The short side of this column measures <b>${kv.short_dimension || "?"}</b> (section ${kv.section || "?"}), below the <b>${kv.required || "300 mm"}</b> a special moment frame column needs.`,
    why: "An earthquake frame column has to fit confinement hoops and a beam-column joint inside it, and a thin section cannot be cast properly around that congestion.",
    check: "Enlarge the section to at least 300 mm on its short side, or exclude the column from the seismic frame and design it to carry gravity only.",
  },
  smf_column_dimension_ratio: {
    plain: (kv) => `This column is <b>${kv.section || "?"}</b> — a short-to-long ratio of <b>${kv.ratio || "?"}</b> against a minimum of <b>${kv.required || "0.4"}</b>. It is a blade, strong one way and weak the other.`,
    why: "An earthquake arrives from any direction, so a column that is far stiffer about one axis than the other is loaded on its weak side half the time.",
    check: "Square the section up towards 0.4 or better, or model it as a wall and design it as one.",
  },
  storey_plan_dimension_jump: {
    plain: (kv) => `On the ${kv.axis || "?"} axis this storey spans <b>${kv.dimension || "?"}</b> while the storey next to it (${kv.adjacent || "?"}) spans a different amount — a ratio of <b>${kv.ratio || "?"}</b> against a limit of <b>${kv.limit || "1.30"}</b>. The building steps in or out sharply here.`,
    why: "A setback concentrates earthquake forces at the level where the plan changes, and the structure above and below no longer shares them evenly.",
    check: "Soften the setback across more than one floor, or design the transition level explicitly for the concentrated forces.",
  },
  plan_reentrant_corner: {
    plain: (kv) => `This storey's plan has an inside corner at its <b>${kv.corner || "?"}</b>, with wings projecting <b>${kv.projection_x || "?"}</b> and <b>${kv.projection_y || "?"}</b> of the plan — both past the <b>${kv.limit || "15%"}</b> limit. The floor is an L rather than a rectangle.`,
    why: "In an earthquake the two wings of an L move differently and tear at the inside corner, which is where the cracking starts.",
    check: "Separate the wings with a seismic joint, or add collectors and chords at the corner to carry the forces across it.",
  },
  footing_min_thickness: {
    plain: (kv) => `This footing is <b>${kv.thickness || "?"}</b> thick, below the <b>${kv.required || "?"}</b> minimum for a ${kv.support === "pile" ? "pile-supported" : "soil-supported"} footing.`,
    why: "A footing needs depth to develop its reinforcement and to resist punching shear where the column pushes through it — a thin pad fails suddenly, in shear, without warning.",
    check: "Deepen the footing, or spread the load over a larger pad so the punching shear drops.",
  },
};

/** Explainer HTML block for one violation, or "" when the condition is unknown. */
function explainBlock(v) {
  const exp = EXPLAINERS[v.condition];
  if (!exp) return "";
  const firstLoc = (v.locations || [])[0] || {};
  const kv = measuredKv(firstLoc.measured);
  let plain = "";
  try { plain = exp.plain(kv, String(firstLoc.measured || "")) || ""; } catch (e) { plain = ""; }
  if (!plain) return "";
  return `
    <div class="v-explain">
      <p class="v-plain">${plain}</p>
    </div>`;
}

// --- violation rendering ---------------------------------------------------------
// A checker's result dict (verified shapes across all twenty checkers):
//   { verdict, violations: [{condition, description, rule_ref, threshold,
//      locations: [{element: "Name (GUID)", storey, measured: "k=v, k=v"}]}],
//     unknown_reasons: [{condition, missing, affected_elements}],
//     checked_summary: {condition: {elements_checked, elements_skipped}} }

/** "span=6550 mm, depth=290 mm, L/d=22.6" -> [["span","6550 mm"], ...]
 *  Key names may contain spaces ("host wall=120 min"). Values without any
 *  key= pattern are returned as [["", whole-string]]. */
function parseMeasured(measured) {
  const s = String(measured ?? "").trim();
  if (!s) return [];
  return s.split(/,\s*(?=[A-Za-z][\w/ ]*=)/).map((part) => {
    const eq = part.indexOf("=");
    return eq === -1 ? ["", part.trim()] : [part.slice(0, eq).trim(), part.slice(eq + 1).trim()];
  });
}

/** parsed pairs -> {key: value} object for template lookups.
 *  Values are HTML-escaped here because explainer templates embed them
 *  directly (they may originate from IFC element names). */
function measuredKv(measured) {
  const kv = {};
  for (const [k, v] of parseMeasured(measured)) {
    if (k) kv[k] = escapeHtml(v);
  }
  return kv;
}

/** "M_Concrete-Rectangular Beam:FB3:744165 (0WSzKynGn1vOzPGJa2X0RZ)"
 *  -> { name: "M_Concrete-Rectangular Beam:FB3:744165", guid: "0WSz…" }
 *
 * Some conditions (e.g. door_clear_floor_space_obstructed) describe the
 * violating element AND a second, unrelated object in one string, e.g.
 * "Door (DOOR_GUID) <- obstructed by Railing (RAILING_GUID)". The violating
 * element's GlobalId always comes first, so this takes the *first*
 * GUID-shaped "(...)" found rather than the last — matching against the
 * end of the string would grab the unrelated object's id instead. */
function splitElement(element) {
  const s = String(element ?? "");
  const m = s.match(/\s*\(([^()]{22})\)/);
  if (!m) return { name: s, guid: null };
  return { name: (s.slice(0, m.index) + s.slice(m.index + m[0].length)).trim(), guid: m[1] };
}

function kvChips(measured) {
  return parseMeasured(measured).map(([k, v]) =>
    `<span class="kv">${k ? `<b>${escapeHtml(k)}</b>=` : ""}${escapeHtml(v)}</span>`).join(" ");
}

function violationCard(v, idx) {
  const locations = (v.locations || []).map((loc) => {
    const { name, guid } = splitElement(loc.element);
    return `
      <tr>
        <td class="v-el">${escapeHtml(name)}
            ${guid ? `<span class="v-guid" title="GlobalId">${escapeHtml(guid)}</span>` : ""}</td>
        <td class="v-storey">${escapeHtml(loc.storey || "")}</td>
        <td class="v-measured">${kvChips(loc.measured)}</td>
        <td class="v-action">${guid ? `<button class="show-in-model" type="button" data-guid="${escapeHtml(guid)}">Show in model</button>` : ""}</td>
      </tr>`;
  }).join("");

  return `
  <div class="violation">
    <div class="v-head">
      <span class="v-num">${idx + 1}</span>
      <div class="v-title">${escapeHtml(v.description || v.condition || "")}</div>
    </div>
    ${explainBlock(v)}
    ${v.threshold ? `<div class="v-meta"><span class="chip limit">Limit: ${escapeHtml(v.threshold)}</span></div>` : ""}
    ${locations ? `
      <table class="v-locations">
        <tr><th>Element</th><th>Storey</th><th>Measured</th><th>Preview</th></tr>
        ${locations}
      </table>` : ""}
    ${v.rule_ref ? `<div class="v-ref">${escapeHtml(v.rule_ref)}</div>` : ""}
  </div>`;
}

function unknownReasonsPanel(report) {
  const reasons = report?.unknown_reasons || [];
  if (!reasons.length) return "";
  return `
  <details class="v-unknown">
    <summary>Why it could not be determined (${reasons.length})</summary>
    <ul>
      ${reasons.map((r) => `
        <li><strong>${escapeHtml(r.condition || "")}</strong> —
          missing data: ${escapeHtml(r.missing || "")}
          ${r.affected_elements != null ? `<span class="chip">${r.affected_elements} affected element(s)</span>` : ""}</li>`).join("")}
    </ul>
  </details>`;
}

function checkedSummaryLine(report) {
  const summary = report?.checked_summary || {};
  const parts = Object.entries(summary).map(([condition, s]) => {
    if (!s || typeof s !== "object") return null;
    const checked = `${s.elements_checked ?? 0} element(s) checked`;
    const skipped = s.elements_skipped ? ` · ${s.elements_skipped} skipped` : "";
    return `<span title="${escapeHtml(condition)}">${checked}${skipped}</span>`;
  }).filter(Boolean);
  return parts.length ? `<div class="rr-footer">${parts.join(" · ")}</div>` : "";
}

// --- results summary: overall banner + stat tile row --------------------------------
// Stat tiles per the tile contract: status color lives on the dot + label
// (never color alone); the big value always wears the ink token.

function statTile(label, value, cls, title, key) {
  return `
    <div class="tile"${key ? ` data-key="${key}" role="button" tabindex="0" aria-pressed="false"` : ""}${title ? ` title="${escapeHtml(title)}"` : ""}>
      <span class="tile-label"><span class="dot ${cls}" aria-hidden="true"></span>${label}</span>
      <span class="tile-value">${value}</span>
    </div>`;
}

function renderCheckSummary(job) {
  const results = job.results || [];
  const counts = { pass: 0, fail: 0, unknown: 0, not_applicable: 0, error: 0 };
  let totalViolations = 0;
  for (const r of results) {
    // "violation" is an alias verdict some checkers emit for "fail" — same bucket.
    if (r.verdict === "violation") counts.fail++;
    else if (counts[r.verdict] != null) counts[r.verdict]++;
    totalViolations += (r.report?.violations || []).length;
  }
  const failed = counts.fail + counts.error;
  const unknown = counts.unknown;

  let banner = null;
  if (results.length && failed) {
    banner = {cls: "bad", icon: "✕", html: `<strong>${failed} rule${failed > 1 ? "s" : ""} failed</strong> —
      ${totalViolations} violation${totalViolations !== 1 ? "s" : ""} found in this model.`};
  } else if (results.length && unknown) {
    banner = {cls: "warn", icon: "?", html: `<strong>No violations found</strong>, but ${unknown} rule${unknown > 1 ? "s" : ""} could not be
      determined — see the details below before relying on this result.`};
  } else if (results.length) {
    banner = {cls: "ok", icon: "✓", html: `<strong>All ${results.length} checked rule${results.length > 1 ? "s" : ""} passed.</strong>
      No violations were found in this model.`};
  }

  const tiles = [
    ["passed", "Passed", counts.pass, "ok"],
    ["failed", "Failed", failed, "bad"],
    ["unknown", "Unknown", unknown, "warn"],
    ["na", "Not applicable", counts.not_applicable, "na"],
    ["violations", "Violations found", totalViolations, "bad",
      "Total violation findings across all rules — the same element can appear in more than one, so this can exceed the number of distinct flagged elements shown by “Show violations” in the model preview."],
  ];
  const root = $("check-summary");
  if (!root.querySelector(".tiles")) {
    root.innerHTML = `<div class="summary-banner"></div>
      <div class="tiles">${tiles.map(([key, label, value, cls, title]) => statTile(label, value, cls, title, key)).join("")}</div>`;
  } else {
    for (const [key, , value] of tiles) {
      const tile = root.querySelector(`.tile[data-key="${key}"]`);
      const out = tile?.querySelector(".tile-value");
      if (!out || out.textContent === String(value)) continue;
      out.textContent = value;
      flash(tile, "tile-flash");
    }
  }
  updateBanner(root.querySelector(".summary-banner"), banner);
}

// Replay a one-shot animation on an element that already exists. Removing the
// class on a timer rather than on animationend: the value inside the tile runs
// its own shorter animation, and its animationend would bubble up and cut the
// tile's flash short.
const flashTimers = new WeakMap();
function flash(el, cls) {
  clearTimeout(flashTimers.get(el));
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  flashTimers.set(el, setTimeout(() => el.classList.remove(cls), 900));
}

// Keep the banner node and change its words, so its entrance animation plays
// once per verdict rather than once per poll.
function updateBanner(slot, banner) {
  if (!slot) return;
  if (!banner) { slot.innerHTML = ""; return; }
  const el = slot.querySelector(".banner");
  if (!el || !el.classList.contains(banner.cls)) {
    slot.innerHTML = `<div class="banner ${banner.cls}"><span class="b-icon" aria-hidden="true">${banner.icon}</span><div class="b-text"></div></div>`;
  }
  const box = slot.querySelector(".banner");
  if (box.dataset.html !== banner.html) {
    box.querySelector(".b-text").innerHTML = banner.html;
    box.dataset.html = banner.html;
  }
}

function checkResultCard(r) {
    const badgeCls = VERDICT_BADGE[r.verdict] || "wait";
    const verdictLabel = VERDICT_LABEL[r.verdict] || r.verdict;
    const report = r.report || {};
    const violations = report.violations || [];
    const shown = violations.slice(0, 3);
    const rest = violations.slice(3);

    return `
    <div class="rule-result ${r.verdict}">
      <div class="rr-head">
        <span class="badge ${badgeCls}">${escapeHtml(verdictLabel)}</span>
        <span class="rule-bubble ${badgeCls}">${escapeHtml(r.rule_id)}</span>
        <span class="rr-title">${escapeHtml(catalogueById[r.rule_id]?.title || "")}</span>
        ${violations.length ? `<span class="rr-count">${violations.length} violation${violations.length !== 1 ? "s" : ""}</span>` : ""}
        ${r.duration_s != null ? `<span class="rr-time">${r.duration_s}s</span>` : ""}
      </div>
      ${catalogueById[r.rule_id]?.rule_preview ? `<details class="source-rule result-source-rule"><summary>View source rule and checking scope</summary><pre>${escapeHtml(catalogueById[r.rule_id].rule_preview)}</pre></details>` : ""}
      ${r.error ? `<p class="error">${escapeHtml(r.error)}</p>` : ""}
      ${r.summary ? `<p class="rr-summary">${escapeHtml(r.summary)}</p>` : ""}
      ${shown.map((v, i) => violationCard(v, i)).join("")}
      ${rest.length ? `
        <button class="btn small show-more" type="button">
          Show ${rest.length} more
        </button>
        <div class="v-rest hidden">${rest.map((v, i) => violationCard(v, i + 3)).join("")}</div>` : ""}
      ${unknownReasonsPanel(report)}
      ${checkedSummaryLine(report)}
    </div>`;
}

// The rendered HTML each card was built from, so an unchanged result is left
// alone instead of being rebuilt (and re-animated) on every poll.
const cardSource = new WeakMap();
let lastHighlightKey = null;

function wireResultCard(card) {
  card.querySelectorAll(".show-in-model").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const guid = button.dataset.guid;
      if (!guid) return;
      window.showSingleViolation?.(guid);
      $("preview-card").scrollIntoView({behavior: "smooth", block: "center"});
    });
  });
  card.querySelectorAll(".show-more").forEach((btn) => {
    btn.addEventListener("click", () => {
      const rest = btn.nextElementSibling;
      const expanding = rest.classList.contains("hidden");
      rest.classList.toggle("hidden", !expanding);
      btn.textContent = expanding
        ? "Show less"
        : `Show ${rest.querySelectorAll(".violation").length} more`;
    });
  });
}

// --- summary tiles as filters ---------------------------------------------------
// Clicking a tile narrows the list below to that group; clicking it again, or
// another tile, changes or clears the filter. The list keeps every card; the
// filter only hides, so streaming results slot in under the current filter.
let checkFilter = null;
const CHECK_FILTERS = {
  passed: (c) => c.dataset.verdict === "pass",
  failed: (c) => ["fail", "violation", "error"].includes(c.dataset.verdict),
  unknown: (c) => c.dataset.verdict === "unknown",
  na: (c) => c.dataset.verdict === "not_applicable",
  violations: (c) => Number(c.dataset.violations) > 0,
};

function applyCheckFilter() {
  const list = $("check-results-list");
  const test = CHECK_FILTERS[checkFilter];
  let visible = 0;
  list.querySelectorAll(":scope > .rule-result").forEach((card) => {
    const on = !test || test(card);
    card.classList.toggle("is-filtered", !on);
    if (on) visible++;
  });
  list.querySelector(":scope > .filter-empty")?.remove();
  if (test && !visible) list.insertAdjacentHTML("beforeend", '<p class="filter-empty">No rules in this group yet.</p>');
  const tiles = $("check-summary").querySelector(".tiles");
  tiles?.classList.toggle("has-filter", Boolean(test));
  tiles?.querySelectorAll(".tile[data-key]").forEach((tile) => {
    const active = tile.dataset.key === checkFilter;
    tile.classList.toggle("is-active", active);
    tile.setAttribute("aria-pressed", String(active));
  });
}

function toggleCheckFilter(key) {
  checkFilter = checkFilter === key ? null : key;
  applyCheckFilter();
}
$("check-summary").addEventListener("click", (event) => {
  const tile = event.target.closest(".tile[data-key]");
  if (tile) toggleCheckFilter(tile.dataset.key);
});
$("check-summary").addEventListener("keydown", (event) => {
  const tile = event.target.closest(".tile[data-key]");
  if (!tile || (event.key !== "Enter" && event.key !== " ")) return;
  event.preventDefault();
  toggleCheckFilter(tile.dataset.key);
});

function renderCheckResults(job, final = false) {
  show("check-rules", false);
  renderCheckSummary(job);

  const list = $("check-results-list");
  const results = job.results || [];
  const present = new Set();
  results.forEach((r, index) => {
    present.add(r.rule_id);
    const html = checkResultCard(r).trim();
    const existing = [...list.children].find((n) => n.dataset.rule === r.rule_id);
    if (existing && cardSource.get(existing) === html) return;
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const card = tpl.content.firstElementChild;
    card.dataset.rule = r.rule_id;
    card.dataset.verdict = r.verdict;
    card.dataset.violations = String((r.report?.violations || []).length);
    cardSource.set(card, html);
    wireResultCard(card);
    if (existing) existing.replaceWith(card);
    else list.insertBefore(card, list.children[index] || null);
  });
  [...list.children].forEach((n) => { if (!present.has(n.dataset.rule)) n.remove(); });
  applyCheckFilter();

  const violationIds = results.flatMap((result) =>
    (result.report?.violations || []).flatMap((violation) =>
      (violation.locations || []).map((location) => splitElement(location.element).guid).filter(Boolean)));
  // De-duplicated: the same element can be flagged by more than one rule
  // (e.g. a door failing both a fire-rating and a clearance check), and the
  // "Show violations" count should match how many distinct objects actually
  // turn red in the viewer, not how many findings mention them.
  const ids = [...new Set(violationIds)];
  // Re-highlighting resets the viewer, so only do it when the set changes —
  // and always once at the end, in case the user cleared it mid-run.
  const key = ids.slice().sort().join(",");
  if (final || key !== lastHighlightKey) {
    lastHighlightKey = key;
    window.highlightViolationIds?.(ids);
  }
  $("check-report").href = `/bnbc/api/jobs/${bnbcJobId}/download/report`;
  show("check-results", true);
}

// =============================================================================
// Panel C — offline BNBC checker generation
// =============================================================================
// This intentionally does not use the uploaded IFC model. The generator uses
// its controlled corpus and fixture engine, then returns code for review.
$("generate-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError("generate-error");
  show("generate-results", false);
  const submit = $("generate-submit");
  submit.disabled = true;
  const clauses = $("generate-clauses").value.split(",").map((x) => x.trim()).filter(Boolean);
  const request = {
    rule_id: $("generate-rule-id").value.trim(),
    title: $("generate-title").value.trim(),
    statement: $("generate-statement").value.trim(),
    scope_note: $("generate-scope").value.trim(),
    source_clauses: clauses,
  };
  setProgress("generate-progress", "Submitting rule to the generation queue…");
  try {
    const started = await api("/generator/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    const job = await pollUntil(
      `/generator/api/jobs/${started.job_id}`,
      (j) => j.state === "done" || j.state === "rejected",
      (j) => setProgress("generate-progress", j.state === "queued"
        ? "Waiting for the single generation worker…"
        : "Interpreting the rule, building fixtures, drafting code and running the acceptance gate…"),
      2500,
    );
    stopProgress("generate-progress");
    renderGeneratedChecker(job);
    window.recordHistory?.("bnbc_generator", job);
  } catch (error) {
    stopProgress("generate-progress");
    showError("generate-error", `Generation failed: ${error.message}`);
  } finally {
    submit.disabled = false;
  }
});

function renderGeneratedChecker(job) {
  const d = job.downloads || {};
  const usage = job.token_usage?.total ? `${Number(job.token_usage.total).toLocaleString()} tokens` : "Token usage unavailable";
  if (job.accepted) {
    $("generate-results").innerHTML = `
      <div class="banner ok"><span class="b-icon" aria-hidden="true">✓</span>
        <div><strong>Checker accepted by the fixture gate.</strong> Review it before promoting it to the live compliance catalogue.</div></div>
      <div class="tiles">${statTile("Rule", escapeHtml(job.rule_id), "ok")}${statTile("Generation", "accepted", "ok")}${statTile("Usage", escapeHtml(usage), "na")}</div>
      <p class="dl-row"><a class="btn primary" href="/generator${d.checker}">Download Python checker</a>
      <a class="btn" href="/generator${d.acceptance}" target="_blank">Acceptance evidence (JSON)</a></p>`;
  } else {
    $("generate-results").innerHTML = `
      <div class="banner bad"><span class="b-icon" aria-hidden="true">×</span>
        <div><strong>Checker was not accepted.</strong> ${escapeHtml(job.error || "Review the gate evidence and refine the rule statement.")}</div></div>
      ${d.rejection ? `<p class="dl-row"><a class="btn" href="/generator${d.rejection}" target="_blank">Download rejection evidence (JSON)</a></p>` : ""}`;
  }
  show("generate-results", true);
}

// --- boot -------------------------------------------------------------------------
function selectWorkflow(id) {
  document.querySelectorAll(".workflow-card").forEach((card) =>
    card.classList.toggle("active", card.id === id));
  document.querySelectorAll(".workflow-choice").forEach((button) =>
    button.classList.toggle("active", button.dataset.workflow === id));
}
// Exposed so the pre-sign-in landing options can route straight into the
// matching workflow tab once the user is signed in (see layout.js).
window.selectWorkflow = selectWorkflow;
document.querySelectorAll(".workflow-choice").forEach((button) =>
  button.addEventListener("click", () => selectWorkflow(button.dataset.workflow)));
selectWorkflow("check-card");
window.addEventListener("auth:ready", renderCheckerCatalogue);
window.authReady.then((user) => { if (user) renderCheckerCatalogue(); });
