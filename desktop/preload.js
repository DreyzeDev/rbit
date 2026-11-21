const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("rbit", {
  port: Number(new URLSearchParams(window.location.search).get("port") || 8337),
  // URLSearchParams already decodes; decoding twice throws on a path containing "%".
  workspace: new URLSearchParams(window.location.search).get("workspace") || "",
  token: new URLSearchParams(window.location.search).get("token") || "",
  ready: new URLSearchParams(window.location.search).get("ready") === "1",
});
