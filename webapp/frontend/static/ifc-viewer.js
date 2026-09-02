import {Viewer, WebIFCLoaderPlugin} from "https://cdn.jsdelivr.net/npm/@xeokit/xeokit-sdk@2.6.84/dist/xeokit-sdk.es.min.js";
import * as WebIFC from "https://cdn.jsdelivr.net/npm/web-ifc@0.0.51/web-ifc-api.js";

const canvas = document.getElementById("ifc-viewer");
const previewCard = document.getElementById("preview-card");
const status = document.getElementById("preview-status");
const loading = document.getElementById("viewer-loading");
const selection = document.getElementById("viewer-selection");

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
window.loadIfcPreview = async (file) => {
  previewCard.classList.remove("hidden");
  loading.classList.remove("hidden");
  status.textContent = "Loading preview…";
  selection.textContent = "Reading IFC geometry in your browser…";

  try {
    if (activeModel) {
      activeModel.destroy();
      activeModel = null;
    }
    const data = await file.arrayBuffer();
    activeModel = loader.load({
      id: "preview-model",
      ifc: data,
      edges: true,
      excludeTypes: ["IfcSpace", "IfcOpeningElement"],
    });
    activeModel.on("loaded", () => {
      viewer.cameraFlight.jumpTo(viewer.scene);
      loading.classList.add("hidden");
      status.textContent = "Interactive preview ready";
      selection.textContent = "Select an element to inspect its IFC identifier.";
    });
    activeModel.on("error", (message) => {
      throw new Error(message || "xeokit could not load this IFC file");
    });
  } catch (error) {
    loading.classList.add("hidden");
    status.textContent = "Preview unavailable";
    selection.textContent = "Could not create preview: " + (error.message || error);
  }
};

