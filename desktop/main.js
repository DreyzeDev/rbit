// rbit desktop: spawns `rbit serve` (the local HTTP/NDJSON backend) and shows
// the chat UI. Requires Python with the rbit package installed.
const { app, BrowserWindow, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const net = require("net");
const path = require("path");
const crypto = require("crypto");

const PREFERRED_PORT = Number(process.env.RBIT_DESKTOP_PORT || 8337);
let PORT = PREFERRED_PORT;
// One random bearer token per launch: the backend refuses every request without
// it, so a web page in the user's browser cannot drive the local agent.
const TOKEN = crypto.randomBytes(24).toString("base64url");
const WORKSPACE = process.env.RBIT_DESKTOP_WORKSPACE || app.getPath("home");
let backend = null;
let backendReady = false;

// Only the process that holds the lock may run; a second launch just focuses the first.
if (!app.requestSingleInstanceLock()) {
  app.quit();
}

function portIsFree(port) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once("error", () => resolve(false));
    probe.once("listening", () => probe.close(() => resolve(true)));
    probe.listen(port, "127.0.0.1");
  });
}

// Use the preferred port when free; otherwise let the OS pick an unused one so we
// never talk to (or hand our token to) some other process squatting on it.
async function choosePort(preferred) {
  if (await portIsFree(preferred)) return preferred;
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

// Confirms the backend is *our* server: it must accept our per-launch token.
function health() {
  return new Promise((resolve) => {
    const req = http.get(
      {
        host: "127.0.0.1",
        port: PORT,
        path: "/v1/status",
        headers: { Authorization: `Bearer ${TOKEN}` },
        timeout: 1500,
      },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      }
    );
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
    env: { ...process.env, PYTHONUNBUFFERED: "1", RBIT_SERVER_TOKEN: TOKEN },
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stderr.on("data", (d) => console.error("[rbit]", String(d)));
  backend.on("error", (err) => {
    console.error("[rbit] failed to start backend:", err.message);
    backendReady = false;
  });
  backend.on("exit", (code, signal) => {
    console.error(`[rbit] backend exited (code=${code}, signal=${signal})`);
    backend = null;
    backendReady = false;
  });
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
      sandbox: true,
    },
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    // Only web links: file:, smb: and custom protocols can launch local programs.
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"), {
    query: { port: String(PORT), workspace: WORKSPACE, ready: backendReady ? "1" : "0", token: TOKEN },
  });
}

app.on("second-instance", () => {
  const [win] = BrowserWindow.getAllWindows();
  if (win) {
    if (win.isMinimized()) win.restore();
    win.focus();
  }
});

app.whenReady().then(async () => {
  PORT = await choosePort(PREFERRED_PORT);
  startBackend();
  backendReady = await waitBackend();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  killBackend();
  // macOS convention: keep the app alive until the user quits explicitly.
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", killBackend);
