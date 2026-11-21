// Loads desktop/main.js with a stubbed `electron` and checks who may read the
// per-launch config (incl. bearer token) over the "rbit:config" IPC channel.
// Usage: node desktop_ipc_probe.js <repo root>; prints one JSON line.
const Module = require("module");
const path = require("path");
const { pathToFileURL } = require("url");

const handlers = {};
const electronStub = {
  app: {
    getPath: () => "/home/test",
    requestSingleInstanceLock: () => true,
    quit() {},
    on() {},
    whenReady: () => new Promise(() => {}),
  },
  BrowserWindow: class {
    static getAllWindows() {
      return [];
    }
  },
  shell: {},
  ipcMain: { on: (channel, fn) => { handlers[channel] = fn; } },
};
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === "electron") return electronStub;
  return originalLoad.call(this, request, ...rest);
};

const root = process.argv[2];
require(path.join(root, "desktop", "main.js"));

function ask(url) {
  const event = { senderFrame: url === undefined ? null : { url }, returnValue: "unset" };
  handlers["rbit:config"](event);
  return event.returnValue;
}

const renderer = path.join(root, "desktop", "renderer", "index.html");
const ours = ask(pathToFileURL(renderer).href);
console.log(
  JSON.stringify({
    registered: typeof handlers["rbit:config"] === "function",
    oursHasToken: !!(ours && typeof ours.token === "string" && ours.token.length >= 24),
    oursHasPort: !!(ours && Number.isInteger(ours.port)),
    oursWithQueryAllowed: ask(pathToFileURL(renderer).href + "?x=1") !== null,
    foreignHttps: ask("https://evil.example/"),
    siblingDir: ask(pathToFileURL(path.join(root, "desktop", "renderer-evil", "index.html")).href),
    noFrame: ask(undefined),
  })
);
