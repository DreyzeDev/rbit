// rbit desktop renderer: connects to `rbit serve` HTTP/NDJSON API.
let rbitCfg = window.rbit || {
  port: Number(new URLSearchParams(window.location.search).get("port") || window.location.port) || 8337,
  workspace: "",
  token: "",
};

function baseUrl() {
  return `http://127.0.0.1:${rbitCfg.port || 8337}`;
}
function authHeaders() {
  return rbitCfg.token ? { Authorization: `Bearer ${rbitCfg.token}` } : {};
}

let sessionId = null;
let sessions = [];
let busy = false;

const feed = document.getElementById("feed");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop-btn");
const tabsEl = document.getElementById("tabs");
const sessionsEl = document.getElementById("sessions");
const sessionsList = document.getElementById("sessions-list");
const sessionsCount = document.getElementById("sessions-count");
const modeSelect = document.getElementById("select-mode");
const modelSelect = document.getElementById("select-model");
const sandboxPill = document.getElementById("sandbox-pill");
const sandboxLabel = document.getElementById("sandbox-label");
const usagePill = document.getElementById("usage-pill");
const gitBranchEl = document.getElementById("git-branch");
const crumbNameEl = document.getElementById("crumb-name");
const crumbPathEl = document.getElementById("crumb-path");

// Persist user mode/model preferences across restarts
modeSelect.value = localStorage.getItem("rbit.mode") || "build";
modeSelect.onchange = () => localStorage.setItem("rbit.mode", modeSelect.value);
modelSelect.onchange = () => localStorage.setItem("rbit.model", modelSelect.value);

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function md(text) {
  const blocks = [];
  let out = esc(text).replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, lang, code) => {
    const label = lang || "code";
    const id = blocks.length;
    blocks.push(
      `<div class="code-wrap"><div class="code-head"><span>${esc(label)}</span>` +
      `<button class="copy-btn" data-copy-idx="${id}">Copy</button></div>` +
      `<pre><code>${code}</code></pre></div>`
    );
    return `\u0000${id}\u0000`;
  });
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  return out.replace(/\u0000(\d+)\u0000/g, (_m, index) => blocks[Number(index)]);
}

feed.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const wrap = btn.closest(".code-wrap");
  const code = wrap?.querySelector("pre code")?.textContent || "";
  navigator.clipboard?.writeText(code).then(() => {
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = "Copy"; }, 1200);
  }).catch(() => {});
});

function addMsg(role, text) {
  feed.querySelector(".home-hero")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (role === "assistant") div.innerHTML = md(text);
  else div.textContent = text;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function addNote(text, cls = "notice") {
  feed.querySelector(".home-hero")?.remove();
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = text;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function setHome() {
  document.body.classList.add("home");
  feed.innerHTML =
    '<div class="home-hero"><div class="wordmark">rbit</div>' +
    '<p class="hero-sub">Autonomous terminal and desktop coding agent</p></div>';
}

function renderTabs() {
  tabsEl.innerHTML = "";
  for (const s of sessions.slice(0, 8)) {
    const el = document.createElement("button");
    el.className = "tab" + (s.id === sessionId ? " active" : "");
    el.innerHTML = `<span class="badge">${esc((s.title || "S")[0].toUpperCase())}</span>` +
      `<span>${esc((s.title || "Session").slice(0, 24))}</span>`;
    el.onclick = () => loadSession(s.id);
    tabsEl.appendChild(el);
  }
  if (sessionId === null) {
    const el = document.createElement("button");
    el.className = "tab active";
    el.innerHTML = `<span class="badge">New</span><span>New session</span>`;
    tabsEl.appendChild(el);
  }
}

function renderSessionsPanel() {
  sessionsList.innerHTML = "";
  sessionsCount.textContent = String(sessions.length);
  for (const s of sessions) {
    const el = document.createElement("div");
    el.className = "sess" + (s.id === sessionId ? " active" : "");
    el.innerHTML =
      `<span class="badge">${esc((s.title || "S")[0].toUpperCase())}</span>` +
      `<span class="name">${esc(s.title || s.id)}</span>` +
      `<button class="del" title="Delete session" aria-label="Delete session">` +
      `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg></button>`;
    el.onclick = (e) => {
      if (e.target.closest(".del")) {
        deleteSession(s.id);
        return;
      }
      loadSession(s.id);
    };
    sessionsList.appendChild(el);
  }
}

async function deleteSession(id) {
  if (busy) return;
  try {
    await fetch(`${baseUrl()}/v1/sessions/${id}`, { method: "DELETE", headers: authHeaders() });
    if (sessionId === id) {
      sessionId = null;
      setHome();
    }
    await refreshSessions();
  } catch {}
}

async function refreshSessions() {
  try {
    const res = await fetch(`${baseUrl()}/v1/sessions`, { headers: authHeaders() });
    if (!res.ok) return;
    const data = await res.json();
    sessions = data.sessions || [];
    renderTabs();
    renderSessionsPanel();
  } catch {}
}

async function refreshMetadata() {
  const hdrs = authHeaders();
  const base = baseUrl();
  try {
    const [stRes, cfgRes, vcsRes, modRes, usgRes] = await Promise.allSettled([
      fetch(`${base}/v1/status`, { headers: hdrs }),
      fetch(`${base}/v1/config`, { headers: hdrs }),
      fetch(`${base}/v1/vcs/status`, { headers: hdrs }),
      fetch(`${base}/v1/models`, { headers: hdrs }),
      fetch(`${base}/v1/usage`, { headers: hdrs }),
    ]);

    if (cfgRes.status === "fulfilled" && cfgRes.value.ok) {
      const cfg = await cfgRes.value.json();
      const sbMode = String(cfg.shell_sandbox || "off").toLowerCase();
      sandboxLabel.textContent = `Sandbox: ${sbMode}`;
      sandboxPill.className = "meta-chip " + (sbMode === "strict" ? "ok" : sbMode === "auto" ? "warn" : "off");
    } else if (stRes.status === "fulfilled" && stRes.value.ok) {
      sandboxLabel.textContent = "Backend: ready";
      sandboxPill.className = "meta-chip ok";
    }

    if (vcsRes.status === "fulfilled" && vcsRes.value.ok) {
      const vcs = await vcsRes.value.json();
      gitBranchEl.textContent = vcs.branch || (vcs.is_git ? "HEAD" : "No git");
    }

    if (modRes.status === "fulfilled" && modRes.value.ok) {
      const mdata = await modRes.value.json();
      const list = mdata.models || [];
      const saved = localStorage.getItem("rbit.model") || mdata.active || "";
      modelSelect.innerHTML = "";
      if (!list.length) {
        modelSelect.innerHTML = `<option value="">Default model</option>`;
      } else {
        for (const m of list) {
          const id = typeof m === "string" ? m : m.id;
          const label = typeof m === "string" ? m : (m.name || m.id);
          const opt = document.createElement("option");
          opt.value = id;
          opt.textContent = label;
          if (id === saved) opt.selected = true;
          modelSelect.appendChild(opt);
        }
      }
    }

    if (usgRes.status === "fulfilled" && usgRes.value.ok) {
      const u = await usgRes.value.json();
      const tok = Number(u.total_tokens || u.tokens || 0);
      const cost = Number(u.cost_usd || u.cost || 0);
      usagePill.textContent = `${tok.toLocaleString()} tok · $${cost.toFixed(4)}`;
    }
  } catch {}
}

function updateWorkspaceCrumbs() {
  const ws = rbitCfg.workspace || "";
  crumbPathEl.textContent = ws;
  const parts = ws.replace(/\\/g, "/").split("/").filter(Boolean);
  crumbNameEl.textContent = parts.length ? parts[parts.length - 1] : "Workspace";
}

async function loadSession(id) {
  if (busy) return;
  try {
    const res = await fetch(`${baseUrl()}/v1/sessions/${id}`, { headers: authHeaders() });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    sessionId = data.id;
    document.body.classList.remove("home");
    feed.innerHTML = "";
    for (const m of data.messages || []) addMsg(m.role, m.content);
    renderTabs();
    renderSessionsPanel();
  } catch {
    addNote("Could not load session.", "notice error");
  }
}

document.getElementById("menu-btn").onclick = () => sessionsEl.classList.toggle("hidden");
document.getElementById("tab-new").onclick = () => {
  if (busy) return;
  sessionId = null;
  setHome();
  renderTabs();
  renderSessionsPanel();
};

document.getElementById("btn-workspace").onclick = async () => {
  if (busy || !window.rbit?.selectWorkspace) return;
  const nextCfg = await window.rbit.selectWorkspace();
  if (!nextCfg) return;
  rbitCfg = nextCfg;
  sessionId = null;
  setHome();
  updateWorkspaceCrumbs();
  await refreshSessions();
  await refreshMetadata();
};

async function abortTurn() {
  if (!busy || !sessionId) return;
  try {
    await fetch(`${baseUrl()}/v1/sessions/${sessionId}/abort`, {
      method: "POST",
      headers: authHeaders(),
    });
    addNote("Turn aborted.", "notice");
  } catch {}
}
stopBtn.onclick = abortTurn;

async function send() {
  const text = promptEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  stopBtn.classList.remove("hidden");
  promptEl.value = "";
  promptEl.style.height = "auto";
  if (document.body.classList.contains("home")) {
    document.body.classList.remove("home");
    feed.innerHTML = "";
  }
  addMsg("user", text);

  let thinkEl = null;
  let streamEl = null;
  try {
    const payload = {
      prompt: text,
      session_id: sessionId,
      agent: modeSelect.value || "build",
    };
    if (modelSelect.value) payload.model = modelSelect.value;

    const res = await fetch(`${baseUrl()}/v1/turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      addNote(`Backend error: HTTP ${res.status}`, "notice error");
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
        if (ev.type === "thinking") {
          if (ev.active) {
            thinkEl = thinkEl || addNote("Thinking...", "thinking");
          } else if (thinkEl) {
            thinkEl.remove();
            thinkEl = null;
          }
        } else if (ev.type === "tool_call") {
          const note = document.createElement("div");
          note.className = "toolcall";
          note.innerHTML = `<b>${esc(ev.name)}</b><span>${esc(JSON.stringify(ev.arguments || {}).slice(0, 110))}</span>`;
          feed.appendChild(note);
          feed.scrollTop = feed.scrollHeight;
          streamEl = null;
        } else if (ev.type === "delta") {
          if (!streamEl) streamEl = addMsg("assistant", "");
          streamEl.innerHTML = md(ev.text);
          feed.scrollTop = feed.scrollHeight;
        } else if (ev.type === "session") {
          sessionId = ev.session_id;
          renderTabs();
        } else if (ev.type === "message") {
          streamEl?.remove();
          streamEl = null;
          addMsg("assistant", ev.content);
          sessionId = ev.session_id;
        } else if (ev.type === "error") {
          addNote(ev.text || "Unknown error", "notice error");
        }
      }
    }
  } catch (err) {
    addNote(`Backend error: ${err}`, "notice error");
  } finally {
    busy = false;
    sendBtn.disabled = false;
    stopBtn.classList.add("hidden");
    refreshSessions();
    refreshMetadata();
  }
}

sendBtn.onclick = send;
promptEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
promptEl.addEventListener("input", () => {
  promptEl.style.height = "auto";
  promptEl.style.height = Math.min(200, promptEl.scrollHeight) + "px";
});
window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === "n" || e.key === "k")) {
    e.preventDefault();
    document.getElementById("tab-new").click();
  } else if ((e.ctrlKey || e.metaKey) && e.key === "b") {
    e.preventDefault();
    document.getElementById("menu-btn").click();
  } else if (e.key === "Escape" && busy) {
    abortTurn();
  }
});

updateWorkspaceCrumbs();
setHome();
refreshSessions();
refreshMetadata();
setInterval(() => {
  refreshSessions();
  refreshMetadata();
}, 15000);
