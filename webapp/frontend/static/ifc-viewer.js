import {Viewer, WebIFCLoaderPlugin} from "https://cdn.jsdelivr.net/npm/@xeokit/xeokit-sdk@2.6.84/dist/xeokit-sdk.es.min.js";
import * as WebIFC from "https://cdn.jsdelivr.net/npm/web-ifc@0.0.51/web-ifc-api.js";

const canvas = document.getElementById("ifc-viewer");
const previewCard = document.getElementById("preview-card");
const status = document.getElementById("preview-status");
const loading = document.getElementById("viewer-loading");
const selection = document.getElementById("viewer-selection");
const originalTab = document.getElementById("preview-original");
const violatingTab = document.getElementById("preview-violating");

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

let activeModel = null;
let originalIfc = null;
let violatingIfc = null;

function setPreviewTab(kind) {
  originalTab.classList.toggle("active", kind === "original");
  violatingTab.classList.toggle("active", kind === "violating");
}

async function loadPreviewData(data, readyLabel) {
  previewCard.classList.remove("hidden");
  loading.classList.remove("hidden");
  status.textContent = "Loading preview…";
  selection.textContent = "Reading IFC geometry in your browser…";

  try {
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
    originalIfc = await file.arrayBuffer();
    violatingIfc = null;
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

originalTab.addEventListener("click", () => {
  if (originalIfc) {
    setPreviewTab("original");
    loadPreviewData(originalIfc, "Original IFC preview ready");
  }
});

violatingTab.addEventListener("click", () => {
  if (violatingIfc) {
    setPreviewTab("violating");
    loadPreviewData(violatingIfc, "Violating IFC preview ready");
  }
});
