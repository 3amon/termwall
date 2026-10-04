/* termwall phone app: the wall + a real terminal (xterm.js over a pty websocket). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------------------ pairing
(function readToken() {
  const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (m) {
    localStorage.setItem("tw-token", m[1]);
    history.replaceState(null, "", location.pathname);
  }
})();
const TOKEN = localStorage.getItem("tw-token") || "";
document.cookie = `tw=${TOKEN}; path=/; SameSite=Strict${location.protocol === "https:" ? "; Secure" : ""}`;

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "X-Termwall-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { showPair(); throw new Error("not paired"); }
  return r.json();
}
const wsURL = (path) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}`;

function toast(msg, ms = 2200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove("on"), ms);
}

// ------------------------------------------------------------------ ANSI -> HTML
const PAL = ["#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#73daca", "#a9b1d6",
             "#414868", "#ff7a93", "#b9f27c", "#ff9e64", "#7da6ff", "#c0a6ff", "#7dcfff", "#c0caf5"];
function c256(n) {
  if (n < 16) return PAL[n];
  if (n < 232) { n -= 16; const s = [0, 95, 135, 175, 215, 255]; return `rgb(${s[Math.floor(n / 36)]},${s[Math.floor(n / 6) % 6]},${s[n % 6]})`; }
  const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`;
}
function ansiToHtml(text) {
  let fg = null, bg = null, bold = false, dim = false, rev = false, out = "";
  const re = /\x1b\[([0-9;:]*)m|\x1b\[[0-9;:?]*[A-Za-ln-z@`]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]/g;
  let last = 0, m;
  const span = (s) => {
    if (!s) return;
    let f = fg, b = bg;
    if (rev) [f, b] = [b || "#1a1b26", f || "#e0e0e0"];
    const st = [];
    if (f) st.push(`color:${f}`);
    if (b) st.push(`background:${b}`);
    if (bold) st.push("font-weight:700");
    if (dim) st.push("opacity:.6");
    out += st.length ? `<span style="${st.join(";")}">${esc(s)}</span>` : esc(s);
  };
  while ((m = re.exec(text))) {
    span(text.slice(last, m.index));
    last = re.lastIndex;
    if (m[1] === undefined) continue;
    const p = m[1] === "" ? [0] : m[1].split(/[;:]/).map((x) => +x || 0);
    for (let i = 0; i < p.length; i++) {
      const n = p[i];
      if (n === 0) { fg = bg = null; bold = dim = rev = false; }
      else if (n === 1) bold = true; else if (n === 2) dim = true; else if (n === 7) rev = true;
      else if (n === 22) bold = dim = false; else if (n === 27) rev = false;
      else if (n >= 30 && n <= 37) fg = PAL[n - 30]; else if (n >= 90 && n <= 97) fg = PAL[n - 82];
      else if (n >= 40 && n <= 47) bg = PAL[n - 40]; else if (n >= 100 && n <= 107) bg = PAL[n - 92];
      else if (n === 39) fg = null; else if (n === 49) bg = null;
      else if ((n === 38 || n === 48) && p[i + 1] === 5) { const c = c256(p[i + 2] || 0); n === 38 ? (fg = c) : (bg = c); i += 2; }
      else if ((n === 38 || n === 48) && p[i + 1] === 2) { const c = `rgb(${p[i + 2] | 0},${p[i + 3] | 0},${p[i + 4] | 0})`; n === 38 ? (fg = c) : (bg = c); i += 4; }
    }
  }
  span(text.slice(last));
  return out;
}

function ago(ts) {
  if (!ts) return "";
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 45) return "now";
  if (d < 3600) return `${Math.max(1, Math.floor(d / 60))}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

// ------------------------------------------------------------------ state
let STATE = { hosts: [], live: [], recent: [], usage: [], dirs: {} };
let TAB = "live", HOSTF = null, QUERY = "", SEARCH = null;
const GLYPH = { claude: "✻", codex: "◆", shell: "❯" };

function connectState() {
  let ws, backoff = 1000, alive = false;
  const open = () => {
    ws = new WebSocket(wsURL("/ws/state"));
    ws.onopen = () => { backoff = 1000; alive = true; };
    ws.onmessage = (e) => { STATE = JSON.parse(e.data); render(); };
    ws.onclose = () => {
      alive = false;
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 15000);
      api("/api/state").then((s) => { STATE = s; render(); }).catch(() => {});
    };
  };
  open();
  // iOS kills sockets in the background: reconnect when we come back
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && (!ws || ws.readyState > 1)) open();
  });
}

function matches(s) {
  if (!QUERY) return true;
  if (s.snippet) return true;
  const hay = [s.title, s.name, s.cwd, s.host, s.agent, s.model, s.git, s.last_user].join(" ").toLowerCase();
  return QUERY.toLowerCase().split(/\s+/).every((t) => hay.includes(t));
}

function items() {
  let list = TAB === "live" ? STATE.live : STATE.recent;
  if (SEARCH && SEARCH.q === QUERY) {
    const extra = (TAB === "live" ? SEARCH.live : SEARCH.recent).filter((x) => !list.some((y) => y.key === x.key));
    const snip = Object.fromEntries((TAB === "live" ? SEARCH.live : SEARCH.recent).map((x) => [x.key, x.snippet]));
    list = [...extra, ...list].map((x) => (snip[x.key] ? { ...x, snippet: snip[x.key] } : x));
  }
  return list.filter((s) => (!HOSTF || s.host === HOSTF) && matches(s));
}

function hostIdx(h) { return Math.max(0, STATE.hosts.findIndex((x) => x.name === h)); }

function cardHTML(s) {
  const body = s.kind === "tmux" && s.screen
    ? `<pre class="c-screen">${ansiToHtml(s.screen.split("\n").slice(-12).join("\n"))}</pre>`
    : `<div class="c-chat">${s.snippet ? `<div class="snip">⌕ ${esc(s.snippet)}</div>` : ""}${s.last_user ? `<div class="u">❯ ${esc(s.last_user)}</div>` : ""}<div class="a">${esc(s.last_assistant ? "⏺ " + s.last_assistant.slice(-500) : "")}</div></div>`;
  const ctx = s.ctx == null ? "" : `<div class="c-ctx"><i class="${s.ctx > 0.85 ? "hi" : s.ctx > 0.6 ? "mid" : ""}" style="width:${(s.ctx * 100).toFixed(0)}%"></i></div>`;
  const where = [s.kind === "tmux" ? s.name : "", s.git || s.cwd].filter(Boolean).join(" · ");
  const ctxTxt = s.ctx == null ? "" : `ctx ${(s.ctx * 100).toFixed(0)}% · `;
  const actions = s.status === "approval"
    ? `<div class="c-actions"><button class="go" data-act="approve">Approve ⏎</button><button data-act="reply">Reply…</button><button data-act="peek">Peek</button></div>` : "";
  return `<article class="card st-${s.status} ${s.kind === "history" ? "ended" : ""} ${s.kind === "bare" ? "bare" : ""}" data-key="${esc(s.key)}">
    <div class="c-head"><span class="glyph ${s.agent}">${GLYPH[s.agent] || "?"}</span>
      <span class="c-title">${s.pinned ? "★ " : ""}${esc(s.title)}</span>
      ${s.model ? `<span class="chip model ${s.agent}">${esc(s.model)}</span>` : ""}
      <span class="chip host h${hostIdx(s.host)}">${esc(s.host)}</span></div>
    <div class="c-body">${body}</div>${ctx}
    <div class="c-foot"><span class="dot"></span><span class="s">${esc(s.status_text)}${s.kind === "bare" ? " · outside tmux" : ""}</span>
      <span class="where">${esc(where)}</span><span class="ago">${ctxTxt}${ago(s.updated)}</span></div>${actions}
  </article>`;
}

function render() {
  if ($("#wall").classList.contains("hidden")) return;
  $("#hosts").innerHTML = STATE.hosts.map((h) =>
    `<button class="hchip ${h.state} ${HOSTF === h.name ? "on" : ""}" data-host="${esc(h.name)}">${esc(h.name)}</button>`).join("");
  $("#n-live").textContent = STATE.live.length || "";
  $("#usage").textContent = (STATE.usage || []).join("  ·  ");
  const list = items();
  const scroll = $("#cards").scrollTop;
  $("#cards").innerHTML = list.length ? list.map(cardHTML).join("")
    : `<div class="empty">${QUERY ? `Nothing matches “${esc(QUERY)}”${QUERY.length >= 3 && !SEARCH ? " (searching transcripts…)" : ""}` : TAB === "live" ? "No live sessions. Tap + to start one." : "No recent sessions."}</div>`;
  $("#cards").scrollTop = scroll;
}

const byKey = (k) => [...STATE.live, ...STATE.recent, ...(SEARCH ? [...SEARCH.live, ...SEARCH.recent] : [])].find((s) => s.key === k);

// ------------------------------------------------------------------ wall interactions
$("#hosts").addEventListener("click", (e) => {
  const b = e.target.closest("[data-host]");
  if (!b) return;
  HOSTF = HOSTF === b.dataset.host ? null : b.dataset.host;
  render();
});
document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.addEventListener("click", () => {
  TAB = b.dataset.tab;
  document.querySelectorAll(".tabs [data-tab]").forEach((x) => x.classList.toggle("on", x === b));
  $("#cards").scrollTop = 0;
  render();
}));
let searchTimer;
$("#q").addEventListener("input", (e) => {
  QUERY = e.target.value.trim();
  SEARCH = null;
  render();
  clearTimeout(searchTimer);
  if (QUERY.length >= 3) searchTimer = setTimeout(async () => {
    const q = QUERY;
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (q === QUERY) { SEARCH = { q, ...r }; render(); }
  }, 450);
});

let pressTimer = null, pressed = false;
$("#cards").addEventListener("pointerdown", (e) => {
  const card = e.target.closest(".card");
  if (!card || e.target.closest("button")) return;
  pressed = false;
  pressTimer = setTimeout(() => { pressed = true; navigator.vibrate?.(10); actionSheet(byKey(card.dataset.key)); }, 480);
});
["pointerup", "pointercancel", "pointermove"].forEach((ev) => $("#cards").addEventListener(ev, (e) => {
  if (ev === "pointermove" && Math.abs(e.movementY) < 4) return;
  clearTimeout(pressTimer);
}));
$("#cards").addEventListener("click", (e) => {
  if (pressed) { pressed = false; return; }
  const card = e.target.closest(".card");
  if (!card) return;
  const s = byKey(card.dataset.key);
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "approve") return reply(s, "");
  if (act === "reply") return replySheet(s);
  if (act === "peek") return peekSheet(s);
  openSession(s);
});
$("#cards").addEventListener("contextmenu", (e) => e.preventDefault());

async function reply(s, text) {
  const r = await api("/api/reply", { key: s.key, text });
  toast(r.ok ? (text ? `sent to ${s.name}` : `⏎ sent to ${s.name}`) : r.error || "failed");
}

async function openSession(s) {
  if (!s) return;
  if (s.kind === "bare") return toast(`Running outside tmux on ${s.host}${s.tty ? ` (${s.tty})` : ""}. Quit it there, then resume it from Recent.`, 4000);
  if (s.status === "offline") return toast(`${s.host} is offline`);
  if (s.kind === "history") {
    toast(`resuming on ${s.host}…`);
    const r = await api("/api/resume", { key: s.key });
    if (!r.ok) return toast(r.error || "resume failed", 4000);
    return openTerm({ ...s, key: r.key, kind: "tmux", name: r.name });
  }
  openTerm(s);
}

// ------------------------------------------------------------------ sheets
function sheet(html) {
  $("#sheet").innerHTML = `<div class="grab"></div>${html}`;
  $("#sheet").classList.remove("hidden");
  $("#scrim").classList.remove("hidden");
  return $("#sheet");
}
function closeSheet() { $("#sheet").classList.add("hidden"); $("#scrim").classList.add("hidden"); }
$("#scrim").addEventListener("click", closeSheet);

function actionSheet(s) {
  if (!s) return;
  const tm = s.kind === "tmux";
  const el = sheet(`<h3>${esc(s.title)}</h3>
    ${tm || s.kind === "history" ? `<button class="act" data-a="open">${s.kind === "history" ? "Resume in tmux" : "Open terminal"}</button>` : ""}
    <button class="act" data-a="peek">Peek ${tm ? "(scrollback)" : "(conversation)"}</button>
    ${tm ? `<button class="act" data-a="reply">Quick reply…</button>
    <button class="act" data-a="enter">Press Enter (approve)</button>
    <button class="act" data-a="escape">Press Esc (decline / interrupt)</button>
    <button class="act" data-a="rename">Rename…</button>
    <button class="act" data-a="pin">${s.pinned ? "Unpin" : "Pin to top ★"}</button>
    <button class="act" data-a="hide">Hide from the wall</button>
    <button class="act danger" data-a="kill">Kill session</button>` : ""}`);
  el.onclick = async (e) => {
    const a = e.target.closest("[data-a]")?.dataset.a;
    if (!a) return;
    closeSheet();
    if (a === "open") openSession(s);
    if (a === "peek") peekSheet(s);
    if (a === "reply") replySheet(s);
    if (a === "enter") reply(s, "");
    if (a === "escape") { await api("/api/keys", { key: s.key, keys: ["Escape"] }); toast("esc sent"); }
    if (a === "rename") {
      const n = prompt("Rename session", s.name);
      if (n && n !== s.name) { const r = await api("/api/rename", { key: s.key, name: n }); toast(r.ok ? "renamed" : r.error); }
    }
    if (a === "pin" || a === "hide") {
      const r = await api("/api/pref", { key: s.key, which: a === "pin" ? "pinned" : "hidden" });
      toast(r.ok ? (a === "pin" ? (r.on ? "pinned" : "unpinned") : "hidden (desktop: ctrl+shift+H shows hidden)") : r.error);
    }
    if (a === "kill" && confirm(`Kill ${s.host}:${s.name}? (the conversation stays resumable)`)) {
      const r = await api("/api/kill", { key: s.key });
      toast(r.ok ? "killed" : r.error);
    }
  };
}

function replySheet(s) {
  const el = sheet(`<h3>Reply to ${esc(s.title)}</h3>
    <textarea id="rtext" placeholder="message… (empty = just press Enter)"></textarea>
    <button class="primary" id="rsend">Send</button>`);
  const ta = $("#rtext", el);
  setTimeout(() => ta.focus(), 50);
  $("#rsend", el).onclick = () => { closeSheet(); reply(s, ta.value.replace(/\n+$/, "")); };
}

async function peekSheet(s) {
  const el = sheet(`<h3>${esc(s.title)}</h3><pre class="peek">loading…</pre>`);
  const r = await api(`/api/peek?key=${encodeURIComponent(s.key)}`);
  const pre = $(".peek", el);
  if (r.kind === "screen") pre.innerHTML = ansiToHtml(r.text.replace(/\s+$/, ""));
  else if (r.kind === "chat") {
    pre.classList.add("chat");
    pre.innerHTML = r.messages.map((m) => `<div class="${m.role === "user" ? "u" : "a"}">${m.role === "user" ? "❯ " : "⏺ "}${esc(m.text)}</div>`).join("");
  } else pre.textContent = "nothing to show";
  pre.scrollTop = pre.scrollHeight;
}

$("#fab").addEventListener("click", () => {
  const hosts = STATE.hosts.map((h) => h.name);
  let agent = "claude";
  const el = sheet(`<h3>New session</h3>
    <label>Host</label><select id="nhost">${hosts.map((h) => `<option ${h === HOSTF ? "selected" : ""}>${esc(h)}</option>`).join("")}</select>
    <label>Agent</label><div class="seg" id="nagent"><button data-v="claude" class="on">claude</button><button data-v="codex">codex</button><button data-v="shell">shell</button></div>
    <label>Directory</label><input id="ndir" list="ndirs" autocapitalize="off" autocorrect="off" spellcheck="false"><datalist id="ndirs"></datalist>
    <button class="primary" id="ngo">Start</button>`);
  // each host's own recent directories (newest first); switching host resets the path
  const fillDirs = () => {
    const dirs = STATE.dirs[$("#nhost", el).value] || ["~"];
    $("#ndirs", el).innerHTML = dirs.map((d) => `<option value="${esc(d)}">`).join("");
    $("#ndir", el).value = dirs[0];
  };
  fillDirs();
  $("#nhost", el).onchange = fillDirs;
  $("#nagent", el).onclick = (e) => {
    const b = e.target.closest("[data-v]");
    if (!b) return;
    agent = b.dataset.v;
    el.querySelectorAll("#nagent button").forEach((x) => x.classList.toggle("on", x === b));
  };
  $("#ngo", el).onclick = async () => {
    const host = $("#nhost", el).value;
    closeSheet();
    toast(`starting on ${host}…`);
    const r = await api("/api/new", { host, agent, cwd: $("#ndir", el).value });
    if (!r.ok) return toast(r.error || "failed", 4000);
    openTerm({ key: r.key, kind: "tmux", host, name: r.name, title: r.name, agent });
  };
});

async function pushSubscribed() {
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
    const reg = await navigator.serviceWorker.getRegistration();
    return !!(reg && (await reg.pushManager.getSubscription()));
  } catch (_) { return false; }
}

$("#menu-btn").addEventListener("click", async () => {
  // label from the real subscription, not iOS permission (granted != subscribed to *this* server)
  const subbed = await pushSubscribed();
  const el = sheet(`<h3>termwall</h3>
    <button class="act" data-a="push">${subbed ? "Notifications on ✓ (re-register + test)" : "Enable notifications"}</button>
    <button class="act" data-a="reload">Reload app</button>
    <button class="act danger" data-a="unpair">Unpair this device</button>
    <p class="muted" style="margin:8px 4px">Tip: long-press a card for peek / reply / approve / kill.</p>`);
  el.onclick = async (e) => {
    const a = e.target.closest("[data-a]")?.dataset.a;
    if (!a) return;
    closeSheet();
    if (a === "push") enablePush();
    if (a === "reload") location.reload();
    if (a === "unpair" && confirm("Forget the pairing token on this device?")) { localStorage.removeItem("tw-token"); location.reload(); }
  };
});

// ------------------------------------------------------------------ terminal
let TERM = null, TWS = null, FIT = null, CUR = null;

function openTerm(s) {
  CUR = s;
  $("#wall").classList.add("hidden");
  $("#term").classList.remove("hidden");
  $("#t-title").textContent = s.title || s.name;
  $("#t-sub").textContent = `${s.host} · ${s.name || ""}${s.model ? " · " + s.model : ""}`;
  history.pushState({ term: s.key }, "");
  if (!TERM) {
    TERM = new Terminal({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, "DejaVu Sans Mono", monospace',
      fontSize: window.innerWidth < 500 ? 11 : 13, cursorBlink: true, scrollback: 5000, allowProposedApi: true,
      theme: { background: "#1a1b26", foreground: "#e0e0e0", cursor: "#7aa2f7", selectionBackground: "#7aa2f766",
        black: PAL[0], red: PAL[1], green: PAL[2], yellow: PAL[3], blue: PAL[4], magenta: PAL[5], cyan: PAL[6], white: PAL[7],
        brightBlack: PAL[8], brightRed: PAL[9], brightGreen: PAL[10], brightYellow: PAL[11], brightBlue: PAL[12],
        brightMagenta: PAL[13], brightCyan: PAL[14], brightWhite: PAL[15] },
    });
    FIT = new FitAddon.FitAddon();
    TERM.loadAddon(FIT);
    TERM.open($("#xterm"));
    TERM.onData((d) => TWS?.readyState === 1 && TWS.send(JSON.stringify({ t: "i", d })));
    TERM.onResize(({ cols, rows }) => TWS?.readyState === 1 && TWS.send(JSON.stringify({ t: "r", c: cols, r: rows })));
  }
  TERM.reset();
  fitTerm();
  const { cols, rows } = TERM;
  TWS?.close();
  const ws = new WebSocket(wsURL(`/ws/term?key=${encodeURIComponent(s.key)}&cols=${cols}&rows=${rows}`));
  ws.binaryType = "arraybuffer";
  ws.onmessage = (e) => TERM.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
  ws.onclose = () => { if (TWS === ws && CUR === s) TERM.write("\r\n\x1b[2m[disconnected — tap ‹ and reopen]\x1b[0m\r\n"); };
  TWS = ws;
  setTimeout(() => TERM.focus(), 100);
}

function fitTerm() {
  // keep the terminal above the iOS keyboard
  const vv = window.visualViewport;
  if (vv) $("#term").style.height = `${vv.height}px`;
  try { FIT?.fit(); } catch (_) {}
}
window.visualViewport?.addEventListener("resize", () => { if (!$("#term").classList.contains("hidden")) { fitTerm(); window.scrollTo(0, 0); } });
window.addEventListener("orientationchange", () => setTimeout(fitTerm, 300));

function closeTerm() {
  TWS?.close();
  TWS = null;
  CUR = null;
  $("#term").classList.add("hidden");
  $("#wall").classList.remove("hidden");
  render();
}
$("#back").addEventListener("click", () => history.back());
window.addEventListener("popstate", () => { if (!$("#term").classList.contains("hidden")) closeTerm(); });
$("#t-more").addEventListener("click", () => CUR && actionSheet(byKey(CUR.key) || CUR));

const KEYS = { esc: "\x1b", tab: "\t", btab: "\x1b[Z", ctrlc: "\x03", up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D", enter: "\r" };
$("#keybar").addEventListener("click", (e) => {
  const k = e.target.closest("[data-k]")?.dataset.k;
  if (!k) return;
  if (k === "kbd") { TERM?.focus(); return; }
  TWS?.readyState === 1 && TWS.send(JSON.stringify({ t: "i", d: KEYS[k] }));
});
// keep the soft keyboard from closing when tapping the key bar
$("#keybar").addEventListener("pointerdown", (e) => e.preventDefault());

// ------------------------------------------------------------------ push notifications
async function enablePush() {
  const step = (m) => { enablePush.step = m; };
  try {
    step("feature check");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      api("/api/clientlog", { where: "enablePush", msg: "no PushManager", ua: navigator.userAgent,
        standalone: matchMedia("(display-mode: standalone)").matches }).catch(() => {});
      return toast(location.protocol !== "https:" ? "Push needs HTTPS and Add to Home Screen" : "Open termwall from the home screen icon, then enable notifications", 4500);
    }
    api("/api/clientlog", { where: "enablePush", msg: "start", standalone: matchMedia("(display-mode: standalone)").matches }).catch(() => {});
    step("permission");
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return toast(`notifications not allowed (${perm})`);
    step("server key");
    const { key } = await api("/api/push/key");
    step("service worker");
    const reg = await Promise.race([
      navigator.serviceWorker.register("sw.js").then(() => navigator.serviceWorker.ready),
      new Promise((_, rej) => setTimeout(() => rej(new Error("service worker didn't start (10s)")), 10000)),
    ]);
    step("subscribe");
    let sub = await reg.pushManager.getSubscription();
    if (sub) await sub.unsubscribe();   // a stale one from another server/key fails silently
    sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u(key) });
    step("register");
    const r = await api("/api/push/subscribe", sub.toJSON());
    toast(r.ok ? "notifications on — sent a test" : r.error || "failed", 3500);
  } catch (e) {
    const msg = `${enablePush.step}: ${e && (e.name + " " + e.message)}`;
    toast("couldn't enable notifications — " + msg, 6000);
    api("/api/clientlog", { where: "enablePush", msg, ua: navigator.userAgent, standalone: matchMedia("(display-mode: standalone)").matches }).catch(() => {});
  }
}
function b64u(s) {
  const p = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + p).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

// ------------------------------------------------------------------ boot
function showPair() {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $("#pair").classList.remove("hidden");
  setTimeout(() => $("#pair-code").focus(), 200);
}
async function pair() {
  const code = $("#pair-code").value.replace(/\D/g, "");
  if (code.length !== 6) return ($("#pair-err").textContent = "6 digits");
  $("#pair-err").textContent = "pairing…";
  const r = await fetch("/api/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) })
    .then((x) => x.json()).catch(() => ({ error: "can't reach the server (is Tailscale on?)" }));
  if (!r.ok) return ($("#pair-err").textContent = r.error || "failed");
  localStorage.setItem("tw-token", r.token);
  location.replace(location.pathname);
}
$("#pair-go").addEventListener("click", pair);
$("#pair-code").addEventListener("keydown", (e) => e.key === "Enter" && pair());
$("#pair-code").addEventListener("input", (e) => { if (e.target.value.replace(/\D/g, "").length === 6) pair(); });
window.addEventListener("unhandledrejection", (e) => {
  if (TOKEN) api("/api/clientlog", { where: "unhandled", msg: String(e.reason && (e.reason.stack || e.reason)) }).catch(() => {});
});
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  navigator.serviceWorker.register("sw.js");
  navigator.serviceWorker.addEventListener("message", (e) => { if (e.data?.open) openFromPush(e.data.open); });
}
function openFromPush(key) {
  const s = byKey(key);
  if (s) openSession(s); else setTimeout(() => byKey(key) && openSession(byKey(key)), 1500);
}
if (!TOKEN) showPair();
else {
  connectState();
  const m = location.search.match(/[?&]open=([^&]+)/);
  if (m) setTimeout(() => openFromPush(decodeURIComponent(m[1])), 800);
  setInterval(() => { if (!$("#wall").classList.contains("hidden")) render(); }, 30000);  // refresh "3m" labels
}
