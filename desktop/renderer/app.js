// rbit desktop renderer: talks to the local `rbit serve` NDJSON backend.
const PORT = window.rbit.port;
const BASE = `http://127.0.0.1:${PORT}`;

let sessionId = null;
let busy = false;

const feed = document.getElementById("feed");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("send");
const sessList = document.getElementById("session-list");
const statusEl = document.getElementById("backend-status");
document.getElementById("ws").textContent = window.rbit.workspace;

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function md(text) {
  // Minimal markdown: fenced code, inline code, bold. Never trusts raw HTML.
  let out = esc(text);
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _lang, code) => `<pre>${code}</pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  return out;
}

function addMsg(role, text) {
  document.getElementById("empty")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<span class="role">${esc(role)}</span>${md(text)}`;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

function addNote(text, cls = "notice") {
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = text;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
  return div;
}

async function refreshSessions() {
  try {
    const res = await fetch(`${BASE}/v1/sessions`);
    const data = await res.json();
    sessList.innerHTML = "";
    let active = data.sessions?.find((s) => s.id === sessionId);
    for (const s of data.sessions || []) {
      const el = document.createElement("div");
      el.className = "sess" + (s.id === sessionId ? " active" : "");
      el.textContent = `${s.title} (${s.messages})`;
      el.onclick = () => {
        sessionId = s.id;
        feed.innerHTML = `<div class="empty">Сессия «${s.title}» — история подгружается агентом автоматически.</div>`;
        refreshSessions();
      };
      sessList.appendChild(el);
    }
    document.getElementById("hdr-title").textContent = active ? active.title : "rbit — new session";
    document.getElementById("hdr-stats").textContent = active
      ? `${active.messages} messages`
      : `${data.sessions?.length || 0} stored sessions`;
    statusEl.textContent = `backend ok · ${data.sessions?.length || 0} sessions`;
    statusEl.className = "ok";
  } catch {
    statusEl.textContent = "backend недоступен";
    statusEl.className = "bad";
  }
}

async function send() {
  const text = promptEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendBtn.disabled = true;
  promptEl.value = "";
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
          streamEl.innerHTML = `<span class="role">assistant</span>${md(ev.text)}`;
          feed.scrollTop = feed.scrollHeight;
        } else if (ev.type === "message") {
          streamEl?.remove();
          streamEl = null;
          addMsg("assistant", ev.content);
          sessionId = ev.session_id;
        } else if (ev.type === "error") {
          addNote(`✗ ${ev.text}`, "toolcall");
        }
      }
    }
  } catch (err) {
    addNote(`✗ Backend error: ${err}`, "toolcall");
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
document.getElementById("new-session").onclick = () => {
  sessionId = null;
  feed.innerHTML = '<div class="empty" id="empty">Новая сессия.</div>';
  refreshSessions();
};
refreshSessions();
setInterval(refreshSessions, 15000);
