import {Viewer, WebIFCLoaderPlugin, XKTLoaderPlugin} from "https://cdn.jsdelivr.net/npm/@xeokit/xeokit-sdk@2.6.84/dist/xeokit-sdk.es.min.js";
import * as WebIFC from "https://cdn.jsdelivr.net/npm/web-ifc@0.0.51/web-ifc-api.js";

const canvas = document.getElementById("ifc-viewer");
const previewCard = document.getElementById("preview-card");
const status = document.getElementById("preview-status");
const loading = document.getElementById("viewer-loading");
const selection = document.getElementById("viewer-selection");
const originalTab = document.getElementById("preview-original");
const violatingTab = document.getElementById("preview-violating");
const resetViewButton = document.getElementById("preview-reset-view");
const isolateButton = document.getElementById("preview-isolate");
const xrayButton = document.getElementById("preview-xray");
const showAllButton = document.getElementById("preview-show-all");

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
  selection.textContent = "All model components are visible.";
}

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
    selection.textContent = "No IFC element selected.";
    return;
  }
  viewer.scene.setObjectsSelected(viewer.scene.selectedObjectIds, false);
  viewer.scene.setObjectsSelected([hit.entity.id], true);
  selection.textContent = "Selected IFC object: " + hit.entity.id;
});

resetViewButton.addEventListener("click", resetCamera);
isolateButton.addEventListener("click", isolateSelected);
xrayButton.addEventListener("click", xraySelected);
showAllButton.addEventListener("click", showAllObjects);

let activeModel = null;
let originalIfc = null;
let violatingIfc = null;
let pendingViolatingUrl = null;
let originalXktUrl = null;

window.resetIfcPreview = () => {
  if (activeModel) {
    activeModel.destroy();
    activeModel = null;
  }
  originalIfc = null;
  violatingIfc = null;
  pendingViolatingUrl = null;
  originalXktUrl = null;
  violatingTab.disabled = true;
  setPreviewTab("original");
};

function setPreviewTab(kind) {
  originalTab.classList.toggle("active", kind === "original");
  violatingTab.classList.toggle("active", kind === "violating");
}

async function loadXktPreview(url, readyLabel) {
  previewCard.classList.remove("hidden");
  loading.classList.remove("hidden");
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
    await loadPreviewData(originalIfc, "Original IFC preview ready");
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
    await loadPreviewData(violatingIfc, "Violating IFC preview ready");
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
  if (!url) {
    violatingTab.disabled = true;
    status.textContent = "Violating preview unavailable";
    selection.textContent = "The coloured IFC is ready to download, but its optional server conversion was unavailable.";
    return;
  }
  pendingViolatingUrl = url;
  violatingTab.disabled = false;
  status.textContent = "Violating IFC ready to preview";
  selection.textContent = "Select the Violating IFC tab to load the generated model.";
};

originalTab.addEventListener("click", () => {
  if (originalXktUrl) {
    setPreviewTab("original");
    loadXktPreview(originalXktUrl, "Original model preview ready");
  } else if (originalIfc) {
    setPreviewTab("original");
    loadPreviewData(originalIfc, "Original IFC preview ready");
  }
});

violatingTab.addEventListener("click", () => {
  if (violatingIfc) {
    setPreviewTab("violating");
    loadPreviewData(violatingIfc, "Violating IFC preview ready");
  } else if (pendingViolatingUrl) {
    const url = pendingViolatingUrl;
    pendingViolatingUrl = null;
    setPreviewTab("violating");
    loadXktPreview(url, "Violating model preview ready");
  }
});
