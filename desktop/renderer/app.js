// rbit desktop renderer: talks to the local `rbit serve` NDJSON backend.
// Works inside Electron (window.rbit from preload) and in a plain browser
// when the renderer is served by `rbit serve` itself (same origin).
const rbitCfg = window.rbit || {
  port: Number(new URLSearchParams(window.location.search).get("port") || window.location.port) || 8337,
};
const PORT = rbitCfg.port;
const BASE = `http://127.0.0.1:${PORT}`;

let sessionId = null;
let sessions = [];
let busy = false;

const feed = document.getElementById("feed");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("send");
const tabsEl = document.getElementById("tabs");
const sessionsEl = document.getElementById("sessions");
const sessionsList = document.getElementById("sessions-list");

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function md(text) {
  let out = esc(text);
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _lang, code) => `<pre>${code}</pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  return out;
}

function addMsg(role, text) {
  feed.querySelector(".wordmark")?.remove();
  feed.querySelector(".tagline")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (role === "assistant") div.innerHTML = md(text);
  else div.textContent = text;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function addNote(text, cls = "notice") {
  feed.querySelector(".wordmark")?.remove();
  feed.querySelector(".tagline")?.remove();
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = text;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function setHome() {
  document.body.classList.add("home");
  feed.innerHTML = '<div class="wordmark">rbit</div>';
}

function renderTabs() {
  tabsEl.innerHTML = "";
  for (const s of sessions) {
    const el = document.createElement("button");
    el.className = "tab" + (s.id === sessionId ? " active" : "");
    el.innerHTML = `<span class="badge">${esc((s.title || "S")[0].toUpperCase())}</span>` +
      `<span>${esc(s.title.slice(0, 26))}</span>`;
    el.onclick = () => loadSession(s.id);
    tabsEl.appendChild(el);
  }
  if (sessionId === null) {
    const el = document.createElement("button");
    el.className = "tab active";
    el.innerHTML = `<span class="badge">＋</span><span>Новая сессия</span>`;
    tabsEl.appendChild(el);
  }
}

function renderSessionsPanel() {
  sessionsList.innerHTML = "";
  for (const s of sessions) {
    const el = document.createElement("div");
    el.className = "sess" + (s.id === sessionId ? " active" : "");
    el.innerHTML = `<span class="badge">${esc((s.title || "S")[0].toUpperCase())}</span>` +
      `<span class="name">${esc(s.title)}</span>`;
    el.onclick = () => loadSession(s.id);
    sessionsList.appendChild(el);
  }
}

async function refreshSessions() {
  try {
    const res = await fetch(`${BASE}/v1/sessions`);
    const data = await res.json();
    sessions = data.sessions || [];
    renderTabs();
    renderSessionsPanel();
  } catch {}
}

async function loadSession(id) {
  try {
    const res = await fetch(`${BASE}/v1/sessions/${id}`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    sessionId = data.id;
    document.body.classList.remove("home");
    feed.innerHTML = "";
    for (const m of data.messages || []) addMsg(m.role, m.content);
    renderTabs();
    renderSessionsPanel();
    document.getElementById("hdr-title")?.remove();
  } catch {
    addNote("Не удалось загрузить сессию.", "notice error");
  }
}

document.getElementById("menu-btn").onclick = () => sessionsEl.classList.toggle("hidden");
document.getElementById("tab-new").onclick = () => {
  sessionId = null;
  setHome();
  renderTabs();
  renderSessionsPanel();
};

async function send() {
  const text = promptEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  promptEl.value = "";
  if (document.body.classList.contains("home")) {
    document.body.classList.remove("home");
    feed.innerHTML = "";
  }
  addMsg("user", text);

  let thinkEl = null;
  let streamEl = null;
  try {
    const res = await fetch(`${BASE}/v1/turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text, session_id: sessionId }),
    });
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
            thinkEl = thinkEl || addNote("⠋ Thinking…", "thinking");
            thinkEl.textContent = "⠋ Thinking…";
          } else if (thinkEl) {
            thinkEl.remove();
            thinkEl = null;
          }
        } else if (ev.type === "tool_call") {
          addNote(`▸ ${ev.name} ${JSON.stringify(ev.arguments).slice(0, 120)}`, "toolcall");
          streamEl = null;
        } else if (ev.type === "delta") {
          if (!streamEl) streamEl = addMsg("assistant", "");
          streamEl.innerHTML = md(ev.text);
          feed.scrollTop = feed.scrollHeight;
        } else if (ev.type === "message") {
          streamEl?.remove();
          streamEl = null;
          addMsg("assistant", ev.content);
          sessionId = ev.session_id;
        } else if (ev.type === "error") {
          addNote(`✗ ${ev.text}`, "notice error");
        }
      }
    }
  } catch (err) {
    addNote(`✗ Backend error: ${err}`, "notice error");
  } finally {
    busy = false;
    sendBtn.disabled = false;
    refreshSessions();
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

setHome();
refreshSessions();
setInterval(refreshSessions, 15000);
