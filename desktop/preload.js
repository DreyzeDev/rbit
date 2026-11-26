const { contextBridge, ipcRenderer } = require("electron");

// The per-launch bearer token must not travel in the page URL (it would show up in
// history, logs and window titles): the main process hands the config over IPC and
// only to a page loaded from this app's own renderer folder.
const cfg = ipcRenderer.sendSync("rbit:config") || {};

contextBridge.exposeInMainWorld("rbit", {
  port: Number(cfg.port) || 8337,
  workspace: typeof cfg.workspace === "string" ? cfg.workspace : "",
  token: typeof cfg.token === "string" ? cfg.token : "",
  ready: cfg.ready === true,
  selectWorkspace: () => ipcRenderer.invoke("rbit:selectWorkspace"),
});
