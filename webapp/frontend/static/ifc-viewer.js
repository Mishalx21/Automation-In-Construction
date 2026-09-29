import {Viewer, WebIFCLoaderPlugin, XKTLoaderPlugin} from "https://cdn.jsdelivr.net/npm/@xeokit/xeokit-sdk@2.6.84/dist/xeokit-sdk.es.min.js";
import * as WebIFC from "https://cdn.jsdelivr.net/npm/web-ifc@0.0.51/web-ifc-api.js";

const canvas = document.getElementById("ifc-viewer");
const previewCard = document.getElementById("preview-card");
const status = document.getElementById("preview-status");
const loading = document.getElementById("viewer-loading");
const loadingText = document.getElementById("viewer-loading-text");
const selection = document.getElementById("viewer-selection");
const propertiesPanel = document.getElementById("viewer-properties");
const originalTab = document.getElementById("preview-original");
const violatingTab = document.getElementById("preview-violating");
const resetViewButton = document.getElementById("preview-reset-view");
const isolateButton = document.getElementById("preview-isolate");
const xrayButton = document.getElementById("preview-xray");
const showAllButton = document.getElementById("preview-show-all");
const showViolationsButton = document.getElementById("preview-show-violations");
const violationCountBadge = document.getElementById("preview-violation-count");

// Keep the mouse wheel inside the model viewport. Without this, the browser
// scrolls the page at the same time as xeokit zooms the camera.
canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  event.stopPropagation();
}, {passive: false});

// IFC geometry is decoded on Chrome's UI thread by WebIFC. This threshold is
// intentionally lower than the upload limit so a large model cannot freeze a tab.
// WebIFC currently parses geometry on the page's main thread. Do not allow
// full-size BIM models to trigger Chrome's unresponsive-page warning.
const MAX_BROWSER_PREVIEW_BYTES = 1 * 1024 * 1024;

const viewer = new Viewer({
  canvasId: "ifc-viewer",
  transparent: false,
  dtxEnabled: true,
});
viewer.camera.eye = [12, 10, 12];
viewer.camera.look = [0, 0, 0];
viewer.camera.up = [0, 1, 0];
viewer.cameraControl.navMode = "orbit";
viewer.cameraControl.panRightClick = true;

function resetCamera() {
  if (!activeModel) return;
  viewer.cameraFlight.jumpTo(viewer.scene);
  selection.textContent = "View reset to fit the full model.";
}

function selectedObjectIds() {
  const ids = viewer.scene.selectedObjectIds;
  if (!ids || ids.length === 0) {
    selection.textContent = "Select a model component first.";
    return null;
  }
  return ids;
}

function isolateSelected() {
  const ids = selectedObjectIds();
  if (!ids) return;
  viewer.scene.setObjectsVisible(viewer.scene.objectIds, false);
  viewer.scene.setObjectsVisible(ids, true);
  selection.textContent = "Showing only the selected component. Choose Show all to restore the model.";
}

function xraySelected() {
  const ids = selectedObjectIds();
  if (!ids) return;
  viewer.scene.setObjectsXRayed(ids, true);
  selection.textContent = "Selected component is shown in X-ray mode. Choose Show all to restore it.";
}

function showAllObjects() {
  viewer.scene.setObjectsVisible(viewer.scene.objectIds, true);
  viewer.scene.setObjectsXRayed(viewer.scene.xrayedObjectIds, false);
  clearViolationHighlights();
  selection.textContent = "All model components are visible.";
}

let highlightedViolationIds = [];
// The full set of flagged GlobalIds from the most recent compliance check or
// injection, kept even after the user clears the highlight (e.g. via "Show
// all" or by selecting something else), so "Show violations" can bring every
// flagged object back into view without re-running the check.
let lastViolationIds = [];

function clearViolationHighlights() {
  for (const id of highlightedViolationIds) {
    const object = viewer.scene.objects[id];
    if (object) object.colorize = [1, 1, 1];
  }
  highlightedViolationIds = [];
}

function updateViolationButton() {
  showViolationsButton.disabled = lastViolationIds.length === 0;
  violationCountBadge.textContent = lastViolationIds.length ? String(lastViolationIds.length) : "";
  violationCountBadge.classList.toggle("hidden", lastViolationIds.length === 0);
}

/** Colours exactly the given elements red and x-rays the rest of the model.
 * Purely a display action — does not touch the tracked "full set" below. */
function displayViolationHighlight(globalIds) {
  clearViolationHighlights();
  const wanted = new Set((globalIds || []).filter(Boolean));
  // Reveal internal violations by making the rest of the model translucent.
  viewer.scene.setObjectsXRayed(viewer.scene.objectIds, true);
  for (const [id, object] of Object.entries(viewer.scene.objects)) {
    if (wanted.has(id.split("#").pop())) {
      object.colorize = [1, 0.08, 0.08];
      highlightedViolationIds.push(id);
    }
  }
  viewer.scene.setObjectsXRayed(highlightedViolationIds, false);
  if (highlightedViolationIds.length) {
    selection.textContent = `${highlightedViolationIds.length} violated component(s) shown in red; the surrounding model is transparent.`;
  }
}

// Sets/replaces the full known set of flagged elements (called once, right
// after a compliance check or injection completes) and displays all of them.
window.highlightViolationIds = (globalIds) => {
  lastViolationIds = (globalIds || []).filter(Boolean);
  updateViolationButton();
  displayViolationHighlight(lastViolationIds);
};

// Narrows the display to a single flagged element (e.g. a "Show in model"
// click on one specific violation) without disturbing the tracked full set,
// so "Show violations" still brings every flagged element back afterwards.
// Fitting the camera to the element alone fills the view with one door and
// loses where it is. Frame a box a few times its size — never smaller than a
// few metres — so the finding stays readable against its surroundings.
function roomAround(aabb, factor = 2.1, minSize = 3.5) {
  const center = [0, 1, 2].map((i) => (aabb[i] + aabb[i + 3]) / 2);
  const half = [0, 1, 2].map((i) => Math.max((aabb[i + 3] - aabb[i]) * factor, minSize) / 2);
  return [...center.map((c, i) => c - half[i]), ...center.map((c, i) => c + half[i])];
}

window.showSingleViolation = (globalIds) => {
  const ids = [].concat(globalIds || []).filter(Boolean);
  // Undo any earlier Isolate / X-ray / selection, so what is shown depends only
  // on this click and not on whatever the previous one left behind.
  viewer.scene.setObjectsSelected(viewer.scene.selectedObjectIds, false);
  viewer.scene.setObjectsVisible(viewer.scene.objectIds, true);
  displayViolationHighlight(ids);
  if (!highlightedViolationIds.length) {
    selection.textContent = "That element is not in the model on screen.";
    return;
  }
  // Frame it: a finding elsewhere in the building is otherwise off-screen, and
  // the click looks as if it did nothing.
  viewer.cameraFlight.flyTo({ aabb: roomAround(viewer.scene.getAABB(highlightedViolationIds)), duration: 0.6, fit: true });
  selection.textContent = highlightedViolationIds.length > 1
    ? `${highlightedViolationIds.length} components of this finding shown in red; the rest of the model is transparent.`
    : "This component is shown in red; the rest of the model is transparent.";
};

showViolationsButton.addEventListener("click", () => {
  if (lastViolationIds.length) displayViolationHighlight(lastViolationIds);
});

const ifcAPI = new WebIFC.IfcAPI();
ifcAPI.SetWasmPath("https://cdn.jsdelivr.net/npm/web-ifc@0.0.51/");
await ifcAPI.Init();

const loader = new WebIFCLoaderPlugin(viewer, {
  WebIFC,
  IfcAPI: ifcAPI,
  objectDefaults: {
    IfcSpace: { visible: false },
    IfcOpeningElement: { visible: false },
  },
});

const xktLoader = new XKTLoaderPlugin(viewer, {
  objectDefaults: {
    IfcSpace: { visible: false },
    IfcOpeningElement: { visible: false },
  },
});

viewer.scene.input.on("mouseclicked", (canvasPos) => {
  const hit = viewer.scene.pick({canvasPos});
  if (!hit || !hit.entity) {
    viewer.scene.setObjectsSelected(viewer.scene.selectedObjectIds, false);
    propertiesPanel.classList.add("hidden");
    selection.textContent = "No IFC element selected.";
    return;
  }
  viewer.scene.setObjectsSelected(viewer.scene.selectedObjectIds, false);
  viewer.scene.setObjectsSelected([hit.entity.id], true);
  selection.textContent = "Selected IFC object: " + hit.entity.id;
  loadSelectedProperties(hit.entity.id);
});

// Standard CAD-style deselection: press Escape or click empty model space.
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || viewer.scene.selectedObjectIds.length === 0) return;
  viewer.scene.setObjectsSelected(viewer.scene.selectedObjectIds, false);
  propertiesPanel.classList.add("hidden");
  selection.textContent = "No IFC element selected.";
});

async function loadSelectedProperties(objectId) {
  if (!propertiesJobId) return;
  const globalId = String(objectId).split("#").pop();
  try {
    const response = await fetch(`/ifc/api/jobs/${propertiesJobId}/elements/${encodeURIComponent(globalId)}`, {credentials: "same-origin"});
    if (!response.ok) throw new Error();
    const data = await response.json();
    const details = Object.entries(data.properties || {}).map(([key, value]) => `${key}: ${value}`);
    propertiesPanel.textContent = `${data.type}${details.length ? " — " + details.join(" · ") : ""}`;
    propertiesPanel.classList.remove("hidden");
  } catch (_) {
    propertiesPanel.classList.add("hidden");
  }
}

window.setPreviewPropertiesJob = (jobId) => { propertiesJobId = jobId; };

resetViewButton.addEventListener("click", resetCamera);
isolateButton.addEventListener("click", isolateSelected);
xrayButton.addEventListener("click", xraySelected);
showAllButton.addEventListener("click", showAllObjects);

let activeModel = null;
let originalIfc = null;
let violatingIfc = null;
let pendingViolatingUrl = null;
let violatingXktUrl = null;
let originalXktUrl = null;
let propertiesJobId = null;
let injectedViolationFocusIds = [];

window.resetIfcPreview = () => {
  if (activeModel) {
    activeModel.destroy();
    activeModel = null;
  }
  originalIfc = null;
  violatingIfc = null;
  pendingViolatingUrl = null;
  violatingXktUrl = null;
  originalXktUrl = null;
  injectedViolationFocusIds = [];
  propertiesJobId = null;
  propertiesPanel.classList.add("hidden");
  violatingTab.disabled = true;
  lastViolationIds = [];
  highlightedViolationIds = [];
  updateViolationButton();
  setPreviewTab("original");
};

function setPreviewTab(kind) {
  originalTab.classList.toggle("active", kind === "original");
  violatingTab.classList.toggle("active", kind === "violating");
}

async function loadXktPreview(url, readyLabel) {
  previewCard.classList.remove("hidden");
  loading.classList.remove("hidden");
  loadingText.textContent = "Loading optimized model geometry…";
  status.textContent = "Loading server-prepared preview...";
  selection.textContent = "Loading optimized model geometry...";
  try {
    await new Promise((resolve) => requestAnimationFrame(resolve));
    if (activeModel) {
      activeModel.destroy();
      activeModel = null;
    }
    activeModel = xktLoader.load({
      id: "preview-model",
      src: url,
      edges: true,
      excludeTypes: ["IfcSpace", "IfcOpeningElement"],
    });
    activeModel.on("loaded", () => {
      viewer.cameraFlight.jumpTo(viewer.scene);
      loading.classList.add("hidden");
      status.textContent = readyLabel;
      selection.textContent = "Select an element to inspect its IFC identifier.";
      if (injectedViolationFocusIds.length && violatingTab.classList.contains("active")) {
        window.highlightViolationIds(injectedViolationFocusIds);
      }
    });
    activeModel.on("error", (message) => {
      loading.classList.add("hidden");
      status.textContent = "Preview unavailable";
      selection.textContent = "Could not load the server-prepared preview: " + (message || "XKT loading failed");
    });
  } catch (error) {
    loading.classList.add("hidden");
    status.textContent = "Preview unavailable";
    selection.textContent = "Could not load the server-prepared preview: " + (error.message || error);
  }
}

window.loadXktPreviewUrl = async (url, readyLabel = "Original model preview ready") => {
  originalXktUrl = url;
  originalIfc = null;
  violatingIfc = null;
  pendingViolatingUrl = null;
  violatingXktUrl = null;
  violatingTab.disabled = true;
  setPreviewTab("original");
  await loadXktPreview(url, readyLabel);
};

async function loadPreviewData(data, readyLabel) {
  if (data.byteLength > MAX_BROWSER_PREVIEW_BYTES) {
    throw new Error("3D preview is disabled for large IFC files to keep Chrome responsive");
  }
  previewCard.classList.remove("hidden");
  loading.classList.remove("hidden");
  loadingText.textContent = "Reading IFC geometry…";
  status.textContent = "Loading preview…";
  selection.textContent = "Reading IFC geometry in your browser…";

  try {
    // Paint the loading overlay before WebIFC starts synchronous geometry work.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (activeModel) {
      activeModel.destroy();
      activeModel = null;
    }
    activeModel = loader.load({
      id: "preview-model",
      ifc: data,
      edges: true,
      excludeTypes: ["IfcSpace", "IfcOpeningElement"],
    });
    activeModel.on("loaded", () => {
      viewer.cameraFlight.jumpTo(viewer.scene);
      loading.classList.add("hidden");
      status.textContent = readyLabel;
      selection.textContent = "Select an element to inspect its IFC identifier.";
    });
    activeModel.on("error", (message) => {
      // This callback runs asynchronously, so throwing here bypasses the
      // surrounding try/catch and can leave the loading overlay visible.
      loading.classList.add("hidden");
      status.textContent = "Preview unavailable";
      selection.textContent = "Could not create preview: " + (message || "xeokit could not load this IFC file");
    });
  } catch (error) {
    loading.classList.add("hidden");
    status.textContent = "Preview unavailable";
    selection.textContent = "Could not create preview: " + (error.message || error);
  }
}

window.loadIfcPreview = async (file) => {
  try {
    if (file.size > MAX_BROWSER_PREVIEW_BYTES) {
      throw new Error("3D preview is disabled for large IFC files to keep Chrome responsive");
    }
    originalIfc = await file.arrayBuffer();
    violatingIfc = null;
    pendingViolatingUrl = null;
    violatingTab.disabled = true;
    setPreviewTab("original");
    await loadPreviewData(originalIfc, "Your model is ready");
  } catch (error) {
    status.textContent = "Preview unavailable";
    selection.textContent = "Could not read preview: " + (error.message || error);
  }
};

// app.js can receive a file before this ES module has finished initialising
// WebIFC. Pick up that queued file as soon as the viewer is ready.
if (window.pendingIfcPreviewFile) {
  const pendingFile = window.pendingIfcPreviewFile;
  window.pendingIfcPreviewFile = null;
  window.loadIfcPreview(pendingFile);
}

window.loadIfcPreviewUrl = async (url) => {
  try {
    const response = await fetch(url, {credentials: "same-origin"});
    if (!response.ok) throw new Error("generated IFC download failed (" + response.status + ")");
    violatingIfc = await response.arrayBuffer();
    violatingTab.disabled = false;
    setPreviewTab("violating");
    await loadPreviewData(violatingIfc, "Modified copy ready");
  } catch (error) {
    status.textContent = "Preview unavailable";
    selection.textContent = "Could not load violating IFC: " + (error.message || error);
  }
};

// Called after injection completes. Do not download or parse the coloured IFC
// until the user explicitly opens its tab, and never offer a browser preview
// for a large result.
window.prepareViolatingXktPreview = (url) => {
  violatingIfc = null;
  pendingViolatingUrl = null;
  violatingXktUrl = url;
  if (!url) {
    violatingTab.disabled = true;
    status.textContent = "Modified copy unavailable";
    selection.textContent = "The coloured IFC is ready to download, but its optional server conversion was unavailable.";
    return;
  }
  pendingViolatingUrl = url;
  violatingTab.disabled = false;
  status.textContent = "Modified copy ready to view";
  selection.textContent = "Select Modified copy to load the generated model.";
};

// Open the coloured injection model and persist its focus across tab switches.
// The affected objects remain red while the surrounding building is transparent.
window.focusInjectedViolation = (globalIds) => {
  injectedViolationFocusIds = (globalIds || []).filter(Boolean);
  if (!injectedViolationFocusIds.length) return;
  if (violatingTab.disabled || !violatingXktUrl) {
    selection.textContent = "The coloured violating-model preview is unavailable for this injected fault.";
    return;
  }
  setPreviewTab("violating");
  loadXktPreview(violatingXktUrl, "Modified copy — the changed parts are shown in red");
};

originalTab.addEventListener("click", () => {
  if (originalXktUrl) {
    setPreviewTab("original");
    loadXktPreview(originalXktUrl, "Original model preview ready");
  } else if (originalIfc) {
    setPreviewTab("original");
    loadPreviewData(originalIfc, "Your model is ready");
  }
});

violatingTab.addEventListener("click", () => {
  if (violatingIfc) {
    setPreviewTab("violating");
    loadPreviewData(violatingIfc, "Modified copy ready");
  } else if (violatingXktUrl) {
    // Keep the URL so users can switch back and forth between both models.
    setPreviewTab("violating");
    loadXktPreview(violatingXktUrl, "Modified copy ready");
  }
});
