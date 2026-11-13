const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("rbit", {
  port: Number(new URLSearchParams(window.location.search).get("port") || 8337),
  workspace: decodeURIComponent(new URLSearchParams(window.location.search).get("workspace") || ""),
  ready: new URLSearchParams(window.location.search).get("ready") === "1",
});
