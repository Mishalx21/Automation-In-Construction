/* IFC Compliance Workbench — single-page UI. Vanilla JS, no build step.
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
    show("viewer-loading", false);
    show(loadPreviewButton, false);
    $("preview-status").textContent = "Preparing server preview...";
    $("viewer-selection").textContent =
      `This ${(file.size / 1048576).toFixed(0)} MB IFC is ready to upload and analyze. Select Create test cases, then Upload & analyze to generate its server-side XKT preview.`;
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
  $("viewer-selection").textContent = "Upload and analyze the model in Create test cases to prepare an optimized XKT preview.";
});

function showError(id, msg) { const el = $(id); el.textContent = msg; show(el, true); }
function hideError(id) { show($(id), false); }

/** Progress line = persistent spinner + text span (spinner animation never restarts). */
function setProgress(id, text) {
  const el = $(id);
  if (!el.querySelector(".spinner")) {
    el.innerHTML = '<span class="spinner" aria-hidden="true"></span><span class="ptext"></span>';
  }
  el.querySelector(".ptext").textContent = text;
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
        $("preview-status").textContent = j.state === "converting_preview"
          ? "Converting server preview..." : "Analyzing model for preview...";
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

$("inject-upload-btn").addEventListener("click", async () => {
  hideError("inject-error");
  show("inject-rules", false);
  show("inject-results", false);
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
  show("inject-rules", true);
  refreshRunButton("inject-btn", ".inject-cb");
  if (job.source_preview?.state === "ready") {
    window.setPreviewPropertiesJob?.(ifcJobId);
    window.loadXktPreviewUrl?.(`/ifc/api/jobs/${ifcJobId}/preview/source`, "Original model preview ready");
  } else {
    $("preview-status").textContent = "Server preview unavailable";
    $("viewer-selection").textContent = "The model is ready for analysis and injection; only its optional 3D conversion was unavailable.";
  }
}

function buildRuleTable(table, rules, withApplicability, cbClass) {
  table.innerHTML = `
    <tr><th></th><th>Rule</th><th>${withApplicability ? "What it injects" : "What it checks"}</th><th></th><th></th></tr>` +
    rules.map((r) => `
    <tr class="${r.applicable === false ? "na" : ""}">
      <td>${r.applicable === false ? "" : `<input type="checkbox" class="${cbClass}" value="${r.rule_id}" checked aria-label="${escapeHtml(r.rule_id)}">`}</td>
      <td class="rule-id">${r.rule_id}</td>
      <td>${escapeHtml(r.description || r.title)}<span class="clause">${escapeHtml(r.clause || r.reference)}</span>
        ${!withApplicability && r.rule_preview ? `<details class="source-rule"><summary>View source rule</summary><pre>${escapeHtml(r.rule_preview)}</pre></details>` : ""}</td>
      <td class="num">${r.candidate_count != null ? r.candidate_count : ""}</td>
      <td>${r.applicable === false
        ? `<span class="badge na" title="${escapeHtml(r.reason || "")}">not applicable</span>`
        : ""}</td>
    </tr>`).join("");
  table.querySelectorAll(`.${cbClass}`).forEach((cb) =>
    cb.addEventListener("change", () => refreshRunButton(cbClass === "inject-cb" ? "inject-btn" : "check-btn", `.${cbClass}`)));
}

function refreshRunButton(btnId, cbClass) {
  $(btnId).disabled = document.querySelectorAll(`${cbClass}:checked`).length === 0;
}

$("inject-btn").addEventListener("click", async () => {
  hideError("inject-error");
  const rules = [...document.querySelectorAll(".inject-cb:checked")].map((cb) => ({ rule: cb.value }));
  $("inject-btn").disabled = true;
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
    $("inject-btn").disabled = false;
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
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function mutationCard(m, idx) {
  const deleted = m.attribute === "(entity deleted)";
  const change = deleted
    ? `<span class="chg chg-del">entity deleted</span>`
    : `<span class="chg"><s>${escapeHtml(fmtVal(m.before))}</s><span class="chg-arrow" aria-hidden="true">→</span><b>${escapeHtml(fmtVal(m.after))}</b></span>`;
  return `
  <div class="mut-card">
    <div class="mut-head">
      <span class="v-num">${idx + 1}</span>
      <span class="badge bad">${escapeHtml(m.rule_id || "")}</span>
      <span class="mut-type">${escapeHtml(m.element_type || "")}</span>
      <span class="mut-attr">${escapeHtml(m.attribute || "")}</span>
      ${change}
    </div>
    ${m.description ? `<p class="mut-desc">${escapeHtml(m.description)}</p>` : ""}
    <div class="mut-meta">
      ${m.target_global_id ? `<span class="chip mono" title="Target GlobalId">${escapeHtml(m.target_global_id)}</span>` : ""}
      ${m.clause ? `<span class="chip">Clause ${escapeHtml(m.clause)}</span>` : ""}
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
    <summary>Independent verification — ${passed}/${checks.length} checks passed</summary>
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
        <div><strong>${muts.length} violation${muts.length !== 1 ? "s" : ""} injected and independently verified.</strong>
        Download the model below, pick it in step 1, and run step 3 — the checkers should now flag these violations.</div></div>`
    : `<div class="banner bad"><span class="b-icon" aria-hidden="true">✕</span>
        <div><strong>Verification did not pass</strong> (${passedChecks}/${checks.length} checks passed) —
        treat the downloaded file with caution and inspect the report.</div></div>`;

  $("inject-mutations").innerHTML = `
    ${banner}
    <div class="tiles">
      ${statTile("Violations injected", muts.length, "bad")}
      ${statTile("Verification checks", checks.length, "na")}
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
        setProgress("check-progress",
          `Checking… ${(j.results || []).length}/${rules.length} rule(s) done`);
        lastCheckJob = j;
        renderCheckResults(j); // stream partial results
      });
    stopProgress("check-progress");
    lastCheckJob = job;
    renderCheckResults(job);
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
  // --- architectural (A1–A5) ---
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
  // --- structural (S1–S5) ---
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
};

/** Explainer HTML block for one violation, or "" when the condition is unknown. */
function explainBlock(v) {
  const exp = EXPLAINERS[v.condition];
  if (!exp) return "";
  const firstLoc = (v.locations || [])[0] || {};
  const kv = measuredKv(firstLoc.measured);
  let plain = "";
  try { plain = exp.plain(kv, String(firstLoc.measured || "")) || ""; } catch (e) { plain = ""; }
  if (!plain && !exp.why) return "";
  return `
    <div class="v-explain">
      ${plain ? `<p class="v-plain"><span class="v-label">What this means:</span> ${plain}</p>` : ""}
      ${exp.why ? `<p class="v-note"><span class="v-label">Why it matters:</span> ${escapeHtml(exp.why)}</p>` : ""}
      ${exp.check ? `<p class="v-note"><span class="v-label">What to check:</span> ${escapeHtml(exp.check)}</p>` : ""}
    </div>`;
}

// --- violation rendering ---------------------------------------------------------
// A checker's result dict (verified shapes across all ten checkers):
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
 *  -> { name: "M_Concrete-Rectangular Beam:FB3:744165", guid: "0WSz…" } */
function splitElement(element) {
  const m = String(element ?? "").match(/^(.*?)\s*\(([^()]{22})\)\s*$/);
  return m ? { name: m[1], guid: m[2] } : { name: element ?? "", guid: null };
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
      </tr>`;
  }).join("");

  return `
  <div class="violation">
    <div class="v-head">
      <span class="v-num">${idx + 1}</span>
      <div class="v-title">${escapeHtml(v.description || v.condition || "")}
        ${v.condition ? `<span class="clause">${escapeHtml(v.condition)}</span>` : ""}</div>
    </div>
    ${explainBlock(v)}
    ${v.threshold ? `<div class="v-meta"><span class="chip limit">Limit: ${escapeHtml(v.threshold)}</span></div>` : ""}
    ${locations ? `
      <table class="v-locations">
        <tr><th>Element</th><th>Storey</th><th>Measured</th></tr>
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

function statTile(label, value, cls) {
  return `
    <div class="tile">
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

  let banner;
  if (results.length && failed) {
    banner = `<div class="banner bad"><span class="b-icon" aria-hidden="true">✕</span>
      <div><strong>${failed} rule${failed > 1 ? "s" : ""} failed</strong> —
      ${totalViolations} violation${totalViolations !== 1 ? "s" : ""} found in this model.</div></div>`;
  } else if (results.length && unknown) {
    banner = `<div class="banner warn"><span class="b-icon" aria-hidden="true">?</span>
      <div><strong>No violations found</strong>, but ${unknown} rule${unknown > 1 ? "s" : ""} could not be
      determined — see the details below before relying on this result.</div></div>`;
  } else if (results.length) {
    banner = `<div class="banner ok"><span class="b-icon" aria-hidden="true">✓</span>
      <div><strong>All ${results.length} checked rule${results.length > 1 ? "s" : ""} passed.</strong>
      No violations were found in this model.</div></div>`;
  } else {
    banner = "";
  }

  $("check-summary").innerHTML = `
    ${banner}
    <div class="tiles">
      ${statTile("Passed", counts.pass, "ok")}
      ${statTile("Failed", failed, "bad")}
      ${statTile("Unknown", unknown, "warn")}
      ${statTile("Not applicable", counts.not_applicable, "na")}
      ${statTile("Violations found", totalViolations, "bad")}
    </div>`;
}

function renderCheckResults(job) {
  show("check-rules", false);
  renderCheckSummary(job);

  const cards = (job.results || []).map((r) => {
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
        <span class="rr-title">${escapeHtml(r.rule_id)} — ${escapeHtml(catalogueById[r.rule_id]?.title || "")}</span>
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
  }).join("");

  $("check-results-list").innerHTML = cards;
  $("check-results-list").querySelectorAll(".show-more").forEach((btn) => {
    btn.addEventListener("click", () => {
      const rest = btn.nextElementSibling;
      const expanding = rest.classList.contains("hidden");
      rest.classList.toggle("hidden", !expanding);
      btn.textContent = expanding
        ? "Show less"
        : `Show ${rest.querySelectorAll(".violation").length} more`;
    });
  });
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
document.querySelectorAll(".workflow-choice").forEach((button) =>
  button.addEventListener("click", () => selectWorkflow(button.dataset.workflow)));
selectWorkflow("check-card");
window.addEventListener("auth:ready", renderCheckerCatalogue);
window.authReady.then((user) => { if (user) renderCheckerCatalogue(); });
