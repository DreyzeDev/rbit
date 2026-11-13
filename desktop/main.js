// rbit desktop: spawns `rbit serve` (the local HTTP/NDJSON backend) and shows
// the chat UI. Requires Python with the rbit package installed.
const { app, BrowserWindow, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const PORT = Number(process.env.RBIT_DESKTOP_PORT || 8337);
const WORKSPACE = process.env.RBIT_DESKTOP_WORKSPACE || app.getPath("home");
let backend = null;
let backendReady = false;

function health() {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port: PORT, path: "/health", timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitBackend(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await health()) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

function startBackend() {
  const py = process.platform === "win32" ? "python" : "python3";
  backend = spawn(py, ["-m", "rbit.cli", "serve", "--port", String(PORT), "-d", WORKSPACE], {
    cwd: WORKSPACE,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stderr.on("data", (d) => console.error("[rbit]", String(d)));
}

function killBackend() {
  if (backend) {
    try {
      backend.kill();
    } catch {}
    backend = null;
  }
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#141414",
    title: "rbit",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"), {
    query: { port: String(PORT), workspace: WORKSPACE, ready: backendReady ? "1" : "0" },
  });
}

app.whenReady().then(async () => {
  startBackend();
  backendReady = await waitBackend();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  killBackend();
  app.quit();
});
app.on("before-quit", killBackend);
