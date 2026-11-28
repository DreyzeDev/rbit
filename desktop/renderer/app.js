// rbit desktop renderer: connects to `rbit serve` HTTP/NDJSON API.
const isElectron = Boolean(window.rbit);
let rbitCfg = window.rbit || {
  port: Number(window.location.port) || 8337,
  workspace: "",
  token: "",
};

// In Electron the page is a file:// document and the backend is on loopback; when `rbit serve`
// itself serves this page, talk to the origin that served it (works for localhost and LAN names).
function baseUrl() {
  if (!isElectron && window.location.protocol.startsWith("http")) return window.location.origin;
  return `http://127.0.0.1:${rbitCfg.port || 8337}`;
}
function authHeaders() {
  return rbitCfg.token ? { Authorization: `Bearer ${rbitCfg.token}` } : {};
}

let sessionId = null;
let sessions = [];
let busy = false;
let turnAbort = null; // AbortController of the running turn (stops it even before a session id exists)
let backendOk = true;

const $ = (id) => document.getElementById(id);
const feed = $("feed");
const promptEl = $("prompt");
const sendBtn = $("send");
const stopBtn = $("stop-btn");
const tabsEl = $("tabs");
const sessionsEl = $("sessions");
const sessionsList = $("sessions-list");
const sessionsCount = $("sessions-count");
const modeSelect = $("select-mode");
const modelSelect = $("select-model");
const sandboxPill = $("sandbox-pill");
const sandboxLabel = $("sandbox-label");
const usagePill = $("usage-pill");
const gitBranchEl = $("git-branch");
const crumbNameEl = $("crumb-name");
const crumbPathEl = $("crumb-path");
const bannerEl = $("banner");
const approvalEl = $("approval");

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {}
}
function load(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

modelSelect.onchange = () => store("rbit.model", modelSelect.value);
modeSelect.onchange = () => store("rbit.mode", modeSelect.value);

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---- Markdown (small, safe subset: everything is escaped first) ----
function inline(text) {
  let out = text;
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  out = out.replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?![*\w])/g, "$1<i>$2</i>");
  out = out.replace(/(^|[^\w_])_([^_\s][^_\n]*?)_(?![\w_])/g, "$1<i>$2</i>");
  // Only http(s) links; the text is already escaped, so it cannot carry markup.
  out = out.replace(
    /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  return out;
}

function md(text) {
  const codeBlocks = [];
  const source = esc(text).replace(/```([a-zA-Z0-9_+-]*)[^\n]*\n([\s\S]*?)(?:```|$)/g, (_m, lang, code) => {
    const id = codeBlocks.length;
    codeBlocks.push(
      `<div class="code-wrap"><div class="code-head"><span>${lang || "code"}</span>` +
        `<button type="button" class="copy-btn" aria-label="Copy code">Copy</button></div>` +
        `<pre><code>${code}</code></pre></div>`
    );
    return `\u0000${id}\u0000`;
  });

  const html = [];
  let list = null; // "ul" | "ol"
  let para = [];
  const flushPara = () => {
    if (para.length) html.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  const closeList = () => {
    if (list) html.push(`</${list}>`);
    list = null;
  };
  for (const line of source.split("\n")) {
    let m;
    if (/^\u0000\d+\u0000$/.test(line.trim())) {
      flushPara();
      closeList();
      html.push(line.trim());
    } else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara();
      closeList();
      html.push(`<h${m[1].length + 2}>${inline(m[2])}</h${m[1].length + 2}>`);
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      flushPara();
      if (list !== "ul") {
        closeList();
        html.push("<ul>");
        list = "ul";
      }
      html.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (list !== "ol") {
        closeList();
        html.push("<ol>");
        list = "ol";
      }
      html.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^&gt;\s?(.*)$/))) {
      flushPara();
      closeList();
      html.push(`<blockquote>${inline(m[1])}</blockquote>`);
    } else if (!line.trim()) {
      flushPara();
      closeList();
    } else {
      closeList();
      para.push(line);
    }
  }
  flushPara();
  closeList();
  // Placeholders may sit inside a paragraph if the model glued text to a fence.
  return html.join("").replace(/\u0000(\d+)\u0000/g, (_m, i) => codeBlocks[Number(i)] || "");
}

feed.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const code = btn.closest(".code-wrap")?.querySelector("pre code")?.textContent || "";
  navigator.clipboard
    ?.writeText(code)
    .then(() => {
      btn.textContent = "Copied";
      setTimeout(() => {
        btn.textContent = "Copy";
      }, 1200);
    })
    .catch(() => {
      btn.textContent = "Copy failed";
      setTimeout(() => {
        btn.textContent = "Copy";
      }, 1500);
    });
});

// ---- Feed helpers ----
function stick() {
  feed.scrollTop = feed.scrollHeight;
}

function addMsg(role, text) {
  feed.querySelector(".home-hero")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (role === "assistant") div.innerHTML = md(text);
  else div.textContent = text;
  feed.appendChild(div);
  stick();
  return div;
}

function addNote(text, cls = "notice") {
  feed.querySelector(".home-hero")?.remove();
  const div = document.createElement("div");
  div.className = cls;
  if (cls.includes("error")) div.setAttribute("role", "alert");
  div.textContent = text;
  feed.appendChild(div);
  stick();
  return div;
}

function toolSummary(args) {
  const a = args || {};
  const key = a.command || a.filePath || a.path || a.pattern || a.url || a.query;
  return String(key !== undefined ? key : JSON.stringify(a)).slice(0, 140);
}

// A tool card: header (name, summary, status) plus an expandable output/diff.
function toolCard(rec) {
  feed.querySelector(".home-hero")?.remove();
  const card = document.createElement("details");
  card.className = "toolcall";
  card.dataset.id = rec.id || "";
  card.innerHTML =
    `<summary><span class="tool-status" aria-hidden="true"></span><b>${esc(rec.name || "tool")}</b>` +
    `<span class="tool-args">${esc(toolSummary(rec.arguments))}</span><span class="tool-meta"></span></summary>` +
    `<pre class="tool-out"></pre>`;
  feed.appendChild(card);
  updateToolCard(card, rec);
  stick();
  return card;
}

function updateToolCard(card, rec) {
  const status = rec.status || "running";
  card.dataset.status = status;
  const label = { running: "running", success: "done", error: "failed", denied: "denied" }[status] || status;
  card.querySelector(".tool-status").textContent = status === "running" ? "…" : status === "success" ? "✓" : "✕";
  const parts = [label];
  if (rec.duration_ms) parts.push(`${(rec.duration_ms / 1000).toFixed(1)}s`);
  card.querySelector(".tool-meta").textContent = parts.join(" · ");
  const body = [rec.summary, rec.diff, rec.output].filter(Boolean).join("\n\n");
  const out = card.querySelector(".tool-out");
  out.textContent = body.slice(0, 20000);
  out.hidden = !body;
  if (status === "error" || status === "denied") card.open = true;
}

function setHome() {
  document.body.classList.add("home");
  feed.innerHTML =
    '<div class="home-hero"><div class="wordmark">rbit</div>' +
    '<p class="hero-sub">Autonomous terminal and desktop coding agent</p></div>';
}

// ---- Tabs and sessions ----
function renderTabs() {
  tabsEl.innerHTML = "";
  const shown = sessions.slice(0, 8);
  // The open session may be older than the first eight: keep it visible.
  const current = sessions.find((s) => s.id === sessionId);
  if (current && !shown.includes(current)) shown[shown.length - 1] = current;
  for (const s of shown) {
    const el = document.createElement("button");
    el.type = "button";
    el.setAttribute("role", "tab");
    el.setAttribute("aria-selected", String(s.id === sessionId));
    el.className = "tab" + (s.id === sessionId ? " active" : "");
    el.title = s.title || "Session";
    el.innerHTML =
      `<span class="badge" aria-hidden="true">${esc((s.title || "S")[0].toUpperCase())}</span>` +
      `<span class="tab-title">${esc((s.title || "Session").slice(0, 24))}</span>`;
    el.onclick = () => loadSession(s.id);
    tabsEl.appendChild(el);
  }
  if (sessionId === null) {
    const el = document.createElement("button");
    el.type = "button";
    el.setAttribute("role", "tab");
    el.setAttribute("aria-selected", "true");
    el.className = "tab active";
    el.innerHTML = `<span class="badge" aria-hidden="true">+</span><span class="tab-title">New session</span>`;
    tabsEl.appendChild(el);
  }
}

function renderSessionsPanel() {
  sessionsList.innerHTML = "";
  sessionsCount.textContent = String(sessions.length);
  for (const s of sessions) {
    const row = document.createElement("div");
    row.className = "sess" + (s.id === sessionId ? " active" : "");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "sess-open";
    open.setAttribute("aria-current", String(s.id === sessionId));
    open.innerHTML =
      `<span class="badge" aria-hidden="true">${esc((s.title || "S")[0].toUpperCase())}</span>` +
      `<span class="name">${esc(s.title || s.id)}</span>`;
    open.onclick = () => loadSession(s.id);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "del";
    del.title = "Delete session";
    del.setAttribute("aria-label", `Delete session ${s.title || s.id}`);
    del.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>';
    del.onclick = () => deleteSession(s);
    row.append(open, del);
    sessionsList.appendChild(row);
  }
}

async function deleteSession(s) {
  if (busy) return;
  if (!window.confirm(`Delete session "${s.title || s.id}"? This cannot be undone.`)) return;
  try {
    const res = await fetch(`${baseUrl()}/v1/sessions/${encodeURIComponent(s.id)}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (sessionId === s.id) {
      sessionId = null;
      setHome();
    }
    await refreshSessions();
  } catch (err) {
    showBanner(`Could not delete the session: ${err.message || err}`);
  }
}

// ---- Backend status / banner ----
function showBanner(text, retry = false) {
  bannerEl.textContent = "";
  const span = document.createElement("span");
  span.textContent = text;
  bannerEl.appendChild(span);
  if (retry) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Retry";
    btn.onclick = () => {
      hideBanner();
      refreshSessions();
      refreshMetadata();
    };
    bannerEl.appendChild(btn);
  }
  bannerEl.hidden = false;
}
function hideBanner() {
  bannerEl.hidden = true;
  bannerEl.textContent = "";
}
function setBackend(ok) {
  if (ok === backendOk && (ok || !bannerEl.hidden)) return;
  backendOk = ok;
  if (ok) {
    hideBanner();
  } else {
    sandboxLabel.textContent = "Backend offline";
    sandboxPill.className = "meta-chip off";
    showBanner("Cannot reach the rbit backend. Is `rbit serve` running?", true);
  }
}

async function api(path, options = {}) {
  const res = await fetch(`${baseUrl()}${path}`, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) },
  });
  if (res.status === 401) {
    setBackend(true);
    showBanner("The backend rejected the access token (401). Restart the app.");
    throw new Error("unauthorized");
  }
  return res;
}

async function refreshSessions() {
  try {
    const res = await api("/v1/sessions");
    if (!res.ok) return;
    setBackend(true);
    const data = await res.json();
    sessions = data.sessions || [];
    renderTabs();
    renderSessionsPanel();
  } catch (err) {
    if (String(err.message) !== "unauthorized") setBackend(false);
  }
}

async function refreshMetadata() {
  const get = (p) => api(p).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const [status, cfg, vcs, models, usage, agents] = await Promise.all([
    get("/v1/status"),
    get("/v1/config"),
    get("/v1/vcs/status"),
    get("/v1/models"),
    get("/v1/usage"),
    get("/v1/agents"),
  ]);
  if (!status && !cfg) return;

  if (cfg) {
    const sbMode = String(cfg.shell_sandbox || "off").toLowerCase();
    sandboxLabel.textContent = `Sandbox: ${sbMode}`;
    sandboxPill.className = "meta-chip " + (sbMode === "strict" ? "ok" : sbMode === "auto" ? "warn" : "off");
    sandboxPill.title =
      sbMode === "off"
        ? "OS sandbox is off: commands run with your user's full access (you are asked before each one)"
        : "OS-level sandbox status";
  } else {
    sandboxLabel.textContent = "Backend: ready";
    sandboxPill.className = "meta-chip ok";
  }

  if (vcs) gitBranchEl.textContent = vcs.branch || (vcs.is_git ? "(no branch)" : "No git");

  if (agents && Array.isArray(agents.agents) && agents.agents.length) {
    const keep = modeSelect.value || load("rbit.mode") || "build";
    modeSelect.innerHTML = "";
    for (const a of agents.agents) {
      const opt = document.createElement("option");
      opt.value = a.name;
      opt.textContent = a.title || a.name;
      opt.title = a.description || "";
      modeSelect.appendChild(opt);
    }
    modeSelect.value = [...modeSelect.options].some((o) => o.value === keep) ? keep : "build";
  }

  if (models && !busy) {
    const list = models.models || [];
    const saved = load("rbit.model") || models.current || "";
    modelSelect.innerHTML = "";
    if (!list.length) {
      modelSelect.innerHTML = '<option value="">Default model</option>';
    } else {
      for (const m of list) {
        const id = typeof m === "string" ? m : m.id;
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = typeof m === "string" ? m : m.name || m.id;
        modelSelect.appendChild(opt);
      }
      modelSelect.value = list.some((m) => (typeof m === "string" ? m : m.id) === saved)
        ? saved
        : typeof list[0] === "string"
          ? list[0]
          : list[0].id;
    }
  }

  if (usage) {
    const tok = Number(usage.total_tokens || usage.tokens || 0);
    const cost = Number(usage.cost_usd || usage.cost || 0);
    usagePill.textContent = `${tok.toLocaleString()} tok · $${cost.toFixed(4)}`;
  }
}

function updateWorkspaceCrumbs() {
  const ws = rbitCfg.workspace || "";
  crumbPathEl.textContent = ws;
  crumbPathEl.title = ws;
  const parts = ws.replace(/\\/g, "/").split("/").filter(Boolean);
  crumbNameEl.textContent = parts.length ? parts[parts.length - 1] : "Workspace";
}

async function loadSession(id) {
  if (busy) return;
  try {
    const res = await api(`/v1/sessions/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    sessionId = data.id;
    document.body.classList.remove("home");
    feed.innerHTML = "";
    for (const m of data.messages || []) {
      if (m.role === "user") {
        addMsg("user", m.content || "");
        continue;
      }
      for (const call of m.tool_calls || []) toolCard(call);
      if (m.content) addMsg("assistant", m.content);
    }
    renderTabs();
    renderSessionsPanel();
  } catch {
    addNote("Could not load session.", "notice error");
  }
}

$("menu-btn").onclick = () => {
  const hidden = sessionsEl.classList.toggle("hidden");
  $("menu-btn").setAttribute("aria-expanded", String(!hidden));
};
$("tab-new").onclick = newSession;
function newSession() {
  if (busy) return;
  sessionId = null;
  setHome();
  renderTabs();
  renderSessionsPanel();
}

$("btn-workspace").onclick = async () => {
  if (busy || !window.rbit?.selectWorkspace) return;
  const nextCfg = await window.rbit.selectWorkspace();
  if (!nextCfg) return;
  rbitCfg = nextCfg;
  sessionId = null;
  setHome();
  updateWorkspaceCrumbs();
  if (!nextCfg.ready) showBanner("The backend did not start in the selected folder.", true);
  await refreshSessions();
  await refreshMetadata();
};

async function abortTurn() {
  if (!busy) return;
  try {
    if (sessionId) {
      await api(`/v1/sessions/${encodeURIComponent(sessionId)}/abort`, { method: "POST" });
    }
    turnAbort?.abort(); // also covers a turn whose session id has not arrived yet
    addNote("Turn aborted.", "notice");
  } catch {}
}
stopBtn.onclick = abortTurn;

// ---- Tool approvals ----
let approvalQueue = [];
let approvalActive = null;
let approvalTimer = null;

function enqueueApproval(ev) {
  approvalQueue.push(ev);
  if (!approvalActive) nextApproval();
}

function nextApproval() {
  const ev = approvalQueue.shift();
  if (!ev) {
    approvalActive = null;
    approvalEl.hidden = true;
    promptEl.focus();
    return;
  }
  approvalActive = ev;
  const command = String(ev.input || "");
  approvalEl.innerHTML =
    `<div class="approval-card" role="alertdialog" aria-modal="true" aria-labelledby="ap-title" aria-describedby="ap-cmd">` +
    `<h2 id="ap-title">Allow ${esc(ev.tool || "tool")}?</h2>` +
    (ev.destructive ? `<p class="ap-warn">This command looks destructive.</p>` : "") +
    `<pre id="ap-cmd"></pre>` +
    `<div class="ap-actions">` +
    `<button type="button" data-a="deny" class="ap-deny">Deny (Esc)</button>` +
    `<button type="button" data-a="always" class="ap-secondary">Always allow this</button>` +
    `<button type="button" data-a="once" class="ap-allow">Allow once</button></div></div>`;
  approvalEl.querySelector("#ap-cmd").textContent = command;
  approvalEl.hidden = false;
  // Buttons stay disabled for a moment so a keystroke meant for the prompt cannot approve a command.
  const buttons = [...approvalEl.querySelectorAll("button")];
  for (const b of buttons) {
    b.disabled = b.dataset.a !== "deny";
    b.onclick = () => answerApproval(b.dataset.a);
  }
  clearTimeout(approvalTimer);
  approvalTimer = setTimeout(() => buttons.forEach((b) => (b.disabled = false)), 700);
  approvalEl.querySelector(".ap-deny").focus();
}

async function answerApproval(action) {
  const ev = approvalActive;
  if (!ev) return;
  clearTimeout(approvalTimer);
  try {
    await api("/v1/approvals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: ev.id, approved: action !== "deny", always: action === "always" }),
    });
  } catch {
    addNote("Could not send the answer to the backend.", "notice error");
  }
  nextApproval();
}

// ---- Turn ----
async function send() {
  const text = promptEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  stopBtn.classList.remove("hidden");
  feed.setAttribute("aria-busy", "true");
  promptEl.value = "";
  promptEl.style.height = "auto";
  if (document.body.classList.contains("home")) {
    document.body.classList.remove("home");
    feed.innerHTML = "";
  }
  addMsg("user", text);

  let thinkEl = null;
  let streamEl = null;
  const cards = new Map();
  turnAbort = new AbortController();
  try {
    const payload = { prompt: text, session_id: sessionId, agent: modeSelect.value || "build" };
    if (modelSelect.value) payload.model = modelSelect.value;

    const res = await api("/v1/turns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: turnAbort.signal,
    });
    if (!res.ok) {
      let detail = "";
      try {
        detail = (await res.json()).error || "";
      } catch {}
      addNote(`Backend error: HTTP ${res.status}${detail ? ` — ${detail}` : ""}`, "notice error");
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let ev;
        try {
          ev = JSON.parse(line);
        } catch {
          continue;
        }
        switch (ev.type) {
          case "thinking":
            if (ev.active) thinkEl = thinkEl || addNote("Thinking...", "thinking");
            else if (thinkEl) {
              thinkEl.remove();
              thinkEl = null;
            }
            break;
          case "status":
            break;
          case "tool_call":
            cards.set(ev.id, toolCard({ ...ev, status: "running" }));
            streamEl = null;
            break;
          case "tool_result": {
            const card = cards.get(ev.id) || toolCard(ev);
            updateToolCard(card, ev);
            break;
          }
          case "tool_approval":
            enqueueApproval(ev);
            break;
          case "delta":
            if (!streamEl) streamEl = addMsg("assistant", "");
            streamEl.innerHTML = md(ev.text);
            stick();
            break;
          case "session":
            sessionId = ev.session_id;
            renderTabs();
            break;
          case "message":
            streamEl?.remove();
            streamEl = null;
            addMsg("assistant", ev.content);
            sessionId = ev.session_id;
            break;
          case "aborted":
            break;
          case "error":
            addNote(ev.text || "Unknown error", "notice error");
            break;
        }
      }
    }
  } catch (err) {
    if (err && err.name !== "AbortError" && String(err.message) !== "unauthorized") {
      addNote(`Backend error: ${err}`, "notice error");
    }
  } finally {
    thinkEl?.remove();
    approvalQueue = [];
    approvalActive = null;
    approvalEl.hidden = true;
    turnAbort = null;
    busy = false;
    sendBtn.disabled = false;
    stopBtn.classList.add("hidden");
    feed.removeAttribute("aria-busy");
    refreshSessions();
    refreshMetadata();
  }
}

sendBtn.onclick = send;
promptEl.addEventListener("keydown", (e) => {
  // Enter inside an IME composition confirms the candidate, it must not send.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    send();
  }
});
promptEl.addEventListener("input", () => {
  promptEl.style.height = "auto";
  promptEl.style.height = Math.min(200, promptEl.scrollHeight) + "px";
});
window.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (approvalActive) {
    if (e.key === "Escape") {
      e.preventDefault();
      answerApproval("deny");
    } else if (e.key === "Tab") {
      // keep focus inside the dialog
      const focusable = [...approvalEl.querySelectorAll("button:not(:disabled)")];
      if (focusable.length) {
        const i = focusable.indexOf(document.activeElement);
        e.preventDefault();
        focusable[(i + (e.shiftKey ? -1 : 1) + focusable.length) % focusable.length].focus();
      }
    }
    return;
  }
  if (mod && e.key === "n") {
    e.preventDefault();
    newSession();
  } else if (mod && e.key === "b") {
    e.preventDefault();
    $("menu-btn").click();
  } else if (e.key === "Escape" && busy) {
    abortTurn();
  }
});

updateWorkspaceCrumbs();
setHome();
if (isElectron && rbitCfg.ready === false) showBanner("The rbit backend did not start. Is Python with rbit installed?", true);
refreshSessions();
refreshMetadata();
setInterval(() => {
  if (document.hidden) return;
  refreshSessions();
  if (!busy) refreshMetadata();
}, 15000);
