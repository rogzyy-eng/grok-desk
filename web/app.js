// Grok Desk PWA — plain JS, hand-drawn canvas charts. Token lives in localStorage only and is
// sent as an Authorization header; it is never put in a URL the server sees.
"use strict";
(() => {
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const KEY = "desk_token";
// First open: https://host/#t=TOKEN  → store, then wipe the fragment from the address bar.
(() => { const m = location.hash.match(/[#&]t=([A-Za-z0-9_\-]{20,})/); if (m) { localStorage.setItem(KEY, m[1]); history.replaceState(null, "", location.pathname); } })();
const token = () => localStorage.getItem(KEY) || "";
let state = null, tab = "home", es = null;

async function api(path, opt = {}) {
  const r = await fetch(path, { ...opt, headers: { "Authorization": "Bearer " + token(), "Content-Type": "application/json", ...(opt.headers || {}) } });
  if (r.status === 401) { localStorage.removeItem(KEY); login("Access code was rejected."); throw new Error("401"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
  return j;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 2600); }
const usd = (v, d = 2) => v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = v => v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "%";
const px = v => v == null ? "—" : "$" + (v >= 1 ? v.toFixed(4) : Number(v).toPrecision(4));
const ago = ts => { const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? Math.floor(s) + "s" : s < 3600 ? Math.floor(s / 60) + "m" : s < 86400 ? Math.floor(s / 3600) + "h" : Math.floor(s / 86400) + "d"; };
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "muted";
const AG = { scanner: "SC", auditor: "AU", analyst: "AN", executor: "EX", risk: "RK", desk: "DK" };
const loading = () => `<div class="skel"></div><div class="skel" style="height:140px"></div><div class="skel"></div>`;
const empty = (ico, title, sub) => `<div class="state"><div class="ico">${ico}</div><b>${esc(title)}</b><div>${esc(sub)}</div></div>`;
const errorBox = e => `<div class="state"><div class="ico">⚠️</div><b>Couldn't load</b><div>${esc(e.message || e)}</div><button class="btn" style="max-width:200px;margin-top:8px" data-retry="1">Try again</button></div>`;

// ---------- canvas helpers ----------
function canvas(el, h) {
  const c = typeof el === "string" ? $(el) : el; if (!c) return null;
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth; c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const g = c.getContext("2d"); g.scale(dpr, dpr); return { g, w, h };
}
function lineChart(el, pts, h = 120) {
  const k = canvas(el, h); if (!k) return; const { g, w } = k;
  if (!pts || pts.length < 2) { g.fillStyle = "#636366"; g.font = "13px -apple-system, Helvetica, Arial, sans-serif"; g.textAlign = "center"; g.fillText("Equity curve appears after the first trade", w / 2, h / 2); return; }
  const ys = pts.map(p => p[1]), mn = Math.min(...ys), mx = Math.max(...ys), pad = (mx - mn) * 0.15 || 1;
  const X = i => 4 + i * (w - 8) / (pts.length - 1), Y = v => h - 6 - (v - (mn - pad)) * (h - 12) / (mx - mn + 2 * pad);
  const up = ys[ys.length - 1] >= ys[0], col = up ? "#30d158" : "#ff453a";
  const grad = g.createLinearGradient(0, 0, 0, h); grad.addColorStop(0, up ? "rgba(48,209,88,.28)" : "rgba(255,69,58,.28)"); grad.addColorStop(1, "rgba(0,0,0,0)");
  g.beginPath(); pts.forEach((p, i) => i ? g.lineTo(X(i), Y(p[1])) : g.moveTo(X(i), Y(p[1])));
  g.lineWidth = 2.2; g.strokeStyle = col; g.lineJoin = "round"; g.stroke();
  g.lineTo(X(pts.length - 1), h); g.lineTo(X(0), h); g.closePath(); g.fillStyle = grad; g.fill();
  g.setLineDash([3, 4]); g.strokeStyle = "rgba(142,142,147,.35)"; g.lineWidth = 1; g.beginPath(); g.moveTo(0, Y(ys[0])); g.lineTo(w, Y(ys[0])); g.stroke(); g.setLineDash([]);
}
function candleChart(el, candles, entry, h = 240) {
  const k = canvas(el, h); if (!k) return; const { g, w } = k;
  if (!candles || !candles.length) { g.fillStyle = "#636366"; g.font = "13px -apple-system, Helvetica, Arial, sans-serif"; g.textAlign = "center"; g.fillText("No price samples yet", w / 2, h / 2); return; }
  const hi = Math.max(...candles.map(c => c[2]), entry || 0), lo = Math.min(...candles.map(c => c[3]), entry || Infinity);
  const pad = (hi - lo) * 0.08 || hi * 0.02, top = hi + pad, bot = lo - pad, R = 58;
  const Y = v => 8 + (top - v) * (h - 24) / (top - bot), cw = (w - R) / Math.max(candles.length, 20);
  g.font = "10px SF Mono, Menlo, monospace"; g.fillStyle = "#636366"; g.textAlign = "left";
  for (let i = 0; i <= 4; i++) { const v = bot + (top - bot) * i / 4, y = Y(v); g.strokeStyle = "rgba(84,84,88,.25)"; g.beginPath(); g.moveTo(0, y); g.lineTo(w - R, y); g.stroke(); g.fillText(v < 0.01 ? v.toExponential(2) : Number(v).toPrecision(3), w - R + 4, y + 3); }
  candles.forEach((c, i) => {
    const [, o, H, L, C] = c, x = i * cw + cw / 2, up = C >= o; g.strokeStyle = g.fillStyle = up ? "#30d158" : "#ff453a";
    g.beginPath(); g.moveTo(x, Y(H)); g.lineTo(x, Y(L)); g.lineWidth = 1; g.stroke();
    const bh = Math.max(1.5, Math.abs(Y(o) - Y(C))); g.fillRect(x - cw * 0.34, Math.min(Y(o), Y(C)), cw * 0.68, bh);
  });
  if (entry) { const y = Y(entry); g.setLineDash([4, 4]); g.strokeStyle = "#0a84ff"; g.beginPath(); g.moveTo(0, y); g.lineTo(w - R, y); g.stroke(); g.setLineDash([]); g.fillStyle = "#0a84ff"; g.fillText("entry", 4, y - 4); }
}
function disc(el, layers, size = 230) {
  const c = typeof el === "string" ? $(el) : el; if (!c) return; c.style.width = size + "px";
  const k = canvas(c, size); const { g } = k, cx = size / 2, cy = size / 2, r = size / 2 - 14, n = 8, gap = 0.035;
  const col = { pass: "#30d158", fail: "#ff453a", nodata: "#ff453a" };
  for (let i = 0; i < n; i++) {
    const L = layers[i] || { status: "wait" }, a0 = -Math.PI / 2 + i * 2 * Math.PI / n + gap, a1 = a0 + 2 * Math.PI / n - 2 * gap;
    g.beginPath(); g.arc(cx, cy, r, a0, a1); g.lineWidth = 18; g.lineCap = "butt"; g.strokeStyle = col[L.status] || "#3a3a3c";
    if (L.status === "nodata") g.setLineDash([4, 5]); g.globalAlpha = L.status === "nodata" ? .7 : 1; g.stroke(); g.setLineDash([]); g.globalAlpha = 1;
    const am = (a0 + a1) / 2; g.fillStyle = "#8e8e93"; g.font = "600 11px -apple-system, Helvetica, Arial, sans-serif"; g.textAlign = "center"; g.fillText("L" + (i + 1), cx + Math.cos(am) * (r - 26), cy + Math.sin(am) * (r - 26) + 4);
  }
  const ok = layers.filter(l => l.status === "pass").length;
  g.fillStyle = ok === 8 ? "#30d158" : "#fff"; g.font = "700 40px -apple-system, Helvetica, Arial, sans-serif"; g.textAlign = "center"; g.fillText(ok + "/8", cx, cy + 8);
  g.fillStyle = "#8e8e93"; g.font = "13px -apple-system, Helvetica, Arial, sans-serif"; g.fillText(ok === 8 ? "all layers pass" : "rejected", cx, cy + 30);
}

// ---------- screens ----------
function setTitle(t) { $("#screen-title").textContent = t; }
function badge() {
  const b = $("#mode-badge"); if (!state) return; b.classList.remove("hidden");
  b.textContent = state.mode + (state.paused ? " · PAUSED" : ""); b.classList.toggle("live", state.mode === "LIVE");
}
async function loadState() { state = await api("/api/state"); badge(); return state; }

async function home() {
  setTitle("Desk"); const v = $("#view"); if (!state) v.innerHTML = loading();
  try { await loadState(); } catch (e) { v.innerHTML = errorBox(e); return; }
  const s = state, pnl = s.equity_usd - s.start_stack, banners = [];
  if (s.layer8_proof && !s.layer8_proof.passed) banners.push(`<div class="banner red">LAYER 8 proof failed — desk is paused until it passes. ${esc(s.layer8_proof.error || "")}</div>`);
  if (s.paused) banners.push(`<div class="banner">Desk paused — Risk still guards open positions.</div>`);
  if (!s.push_ready) banners.push(`<div class="banner">Push is not configured on the server.</div>`);
  v.innerHTML = `${banners.join("")}
  <div class="card"><div class="sub">${s.mode === "PAPER" ? "Paper equity" : "Tracked equity"}</div>
    <div class="big num">${usd(s.equity_usd)}</div>
    <div class="sub"><span class="${cls(pnl)}">${usd(pnl)} (${pct(s.start_stack ? pnl / s.start_stack * 100 : 0)})</span> · cash ${usd(s.stack_usd)}</div>
    <canvas id="eq" style="margin-top:12px"></canvas></div>
  <div class="grid3"><div class="stat"><b>${s.today.seen}</b><span>coins seen 24h</span></div>
    <div class="stat"><b>${s.today.audited}</b><span>audited</span></div><div class="stat"><b>${s.today.signals}</b><span>buy signals</span></div></div>
  <div class="h">Open positions</div>
  ${s.positions.length ? `<div class="list">${s.positions.map(p => `<div class="row" data-coin="${esc(p.mint)}">
      <div class="av executor">${esc((p.symbol || "?").slice(0, 3))}</div>
      <div class="grow"><div class="t">${esc(p.symbol || p.mint.slice(0, 6))}</div><div class="s">${usd(p.ticket_usd)} @ ${px(p.entry_price)} · ${ago(p.opened_at)} · ${esc(p.status === "pending_user" ? "waiting for your FOMO buy" : "Risk watching")}</div></div>
      <div class="r"><div class="${cls(p.upnl_pct)} num">${pct(p.upnl_pct)}</div><div class="s">${usd(p.value_usd)}</div></div></div>`).join("")}</div>`
    : `<div class="card">${empty("🌙", "No open positions", "The desk opens at most " + "2 positions; it waits for coins that pass all 8 audit layers.")}</div>`}
  <div class="h">In the pipeline</div>
  ${s.watching.length ? `<div class="list">${s.watching.map(c => `<div class="row" data-coin="${esc(c.mint)}"><div class="av scanner">${esc((c.symbol || "··").slice(0, 3))}</div><div class="grow"><div class="t">${esc(c.symbol || c.mint.slice(0, 8) + "…")}</div><div class="s">${esc(c.status)}</div></div><span class="chev">›</span></div>`).join("")}</div>`
    : `<div class="card">${empty("📡", "Listening for migrations", "New PumpSwap migrations show up here.")}</div>`}
  <div class="btns"><button class="btn ${s.paused ? "primary" : "danger"}" id="pp">${s.paused ? "Resume desk" : "Pause desk"}</button><button class="btn" id="rep">Night report</button></div>`;
  lineChart("#eq", s.equity_curve);
  $("#pp").onclick = async () => { const r = await post(s.paused ? "/api/resume" : "/api/pause"); toast(r.text); home(); };
  $("#rep").onclick = async () => { const r = await api("/api/report"); toast(r.text); };
}

async function agents() {
  setTitle("Agents"); const v = $("#view"); v.innerHTML = loading();
  let d; try { d = await api("/api/agents"); } catch (e) { v.innerHTML = errorBox(e); return; }
  const role = { scanner: "Watches PumpSwap migrations", auditor: "8 safety layers", analyst: "Alive · organic · chase", executor: "Sizes tickets · never places orders", risk: "Stop · trail · timeout" };
  v.innerHTML = `<div class="list">${d.agents.map(a => { const hb = a.heartbeat, age = hb ? Date.now() / 1000 - hb.ts : 1e9;
    const st = !hb ? ["wait", "starting"] : hb.status === "error" ? ["bad", "error"] : age > 180 ? ["bad", "stale"] : ["ok", "live"];
    return `<div class="row" data-agent="${a.name}"><div class="av ${a.name}">${AG[a.name]}</div>
      <div class="grow"><div class="t" style="text-transform:capitalize">${a.name}</div><div class="s">${esc(a.last ? (a.last.symbol ? a.last.symbol + ": " : "") + a.last.text : role[a.name])}</div></div>
      <div class="r"><span class="pill ${st[0]}">${st[1]}</span><div class="s">${a.decisions_24h} / 24h</div></div></div>`; }).join("")}</div>
  <div class="h">Ask the desk</div><div class="card"><div class="cmd"><input id="cmd" placeholder="status · why SYMBOL · set risk.stop_pct 6" autocapitalize="off" autocorrect="off"><button id="go">Send</button></div><div id="out" class="sub" style="white-space:pre-wrap;margin-top:10px"></div></div>`;
  const send = async () => { const t = $("#cmd").value.trim(); if (!t) return; try { const r = await post("/api/command", { text: t }); $("#out").textContent = r.text; } catch (e) { $("#out").textContent = e.message; } };
  $("#go").onclick = send; $("#cmd").onkeydown = e => { if (e.key === "Enter") send(); };
}

async function agentSheet(name) {
  openSheet(`<div class="state loading"><div class="spinner"></div></div>`, name);
  try {
    const d = await api("/api/decisions?agent=" + name + "&limit=60");
    let html = "";
    if (name === "auditor") {
      const lastAudit = d.items.find(i => i.mint && (i.kind === "pass" || i.kind === "reject"));
      if (lastAudit) { html += `<button class="btn blue" style="margin-bottom:12px" data-audit="${esc(lastAudit.mint)}">Open latest audit disc</button>`; }
    }
    html += d.items.length ? `<div class="list">${d.items.map(feedRow).join("")}</div>` : empty("🕊️", "No decisions yet", "This agent hasn't decided anything yet.");
    sheetBody(html);
  } catch (e) { sheetBody(errorBox(e)); }
}

function feedRow(i) {
  const k = i.kind, pill = { pass: "ok", buy: "ok", bought: "ok", sold: "ok", stopped: "bad", rejected: "bad", kill: "wait", exit: "bad", reject: "bad", skip: "wait", signal: "ok" }[k] || "";
  return `<div class="row" ${i.mint ? `data-coin="${esc(i.mint)}"` : ""}><div class="av ${esc(i.agent)}">${AG[i.agent] || "··"}</div>
    <div class="grow"><div class="t">${esc(i.title || i.symbol || (i.mint ? i.mint.slice(0, 6) + "…" : i.agent))} <span class="pill ${pill}">${esc(k)}</span></div><div class="s">${esc(i.text)}</div></div>
    <div class="r s">${ago(i.ts)}</div></div>`;
}

async function activity() {
  setTitle("Activity"); const v = $("#view"); v.innerHTML = loading();
  let d; try { d = await api("/api/activity?limit=100"); } catch (e) { v.innerHTML = errorBox(e); return; }
  v.innerHTML = d.items.length ? `<div class="list" id="feed">${d.items.map(feedRow).join("")}</div>` : empty("📭", "Nothing yet", "Every decision from every agent lands here.");
}

async function account() {
  setTitle("Account"); const v = $("#view"); v.innerHTML = loading();
  let a, s; try { [a, s] = await Promise.all([api("/api/account"), api("/api/settings")]); } catch (e) { v.innerHTML = errorBox(e); return; }
  const pushState = !("Notification" in window) ? "Add Desk to your Home Screen to enable push" : Notification.permission === "granted" ? "Enabled on this phone" : "Tap to enable";
  const groups = s.settings, editable = ["scanner", "auditor", "analyst", "executor", "risk", "desk"];
  v.innerHTML = `
  <div class="card"><div class="kv"><span>FOMO</span><span>@${esc(a.fomo_handle)}</span></div>
    <div class="kv"><span>Mode</span><span>${esc(a.mode)}</span></div><div class="kv"><span>Stack</span><span class="num">${usd(a.stack_usd)}</span></div>
    <div class="kv"><span>Fees paid</span><span class="num">${usd(a.fees_usd)}</span></div><div class="kv"><span>PAPER nights</span><span>${a.paper_nights}</span></div>
    <div class="kv"><span>Stack for 8% ≥ min ticket</span><span class="num">${usd(a.min_stack_for_pct)}</span></div></div>
  <div class="btns"><a class="btn" href="${esc(a.profile_url)}" target="_blank" rel="noopener noreferrer">My FOMO profile</a><a class="btn" href="${esc(a.signup_url)}" target="_blank" rel="noopener noreferrer">Invite link</a></div>
  <div class="h">Notifications</div><div class="list"><div class="row" id="push"><div class="grow"><div class="t">Push alerts</div><div class="s">${esc(pushState)}</div></div><span class="chev">›</span></div>
    <div class="row" id="pushtest"><div class="grow"><div class="t">Send a test push</div></div><span class="chev">›</span></div></div>
  <div class="h">Mode</div><div class="card">${a.live_missing.length ? `<div class="sub">LIVE is locked: needs ${esc(a.live_missing.join(" and "))}.</div>` : `<div class="sub" style="margin-bottom:10px">LIVE = signals with FOMO links. You buy and sell by hand; the desk never places orders.</div><input id="liveconf" placeholder="Type LIVE to confirm" autocapitalize="characters"><button class="btn danger" id="golive" style="margin-top:10px">Switch to LIVE</button>`}
    ${a.mode === "LIVE" ? `<button class="btn" id="gopaper" style="margin-top:10px">Back to PAPER</button>` : ""}</div>
  <div class="h">Settings</div>${editable.map(g => `<div class="list">${Object.entries(groups[g] || {}).filter(([k]) => !["mode", "paper_nights", "paper_since", "layer8_proof", "last_night_closed", "paused", "paused_by"].includes(k) || g !== "desk").map(([k, val]) =>
      `<div class="row" data-set="${g}.${k}" data-val="${esc(JSON.stringify(val))}"><div class="grow"><div class="t" style="font-weight:500">${g}.${esc(k)}</div></div><div class="r num muted">${esc(typeof val === "object" ? JSON.stringify(val) : val)}</div></div>`).join("")}</div>`).join("")}
  <button class="btn danger" id="logout" style="margin:18px 0">Forget access code on this phone</button>`;
  $("#push").onclick = enablePush; $("#pushtest").onclick = async () => { try { const r = await post("/api/push/test"); toast("Push: " + JSON.stringify(r.result)); } catch (e) { toast(e.message); } };
  const gl = $("#golive"); if (gl) gl.onclick = async () => { try { await post("/api/mode", { mode: "LIVE", confirm: $("#liveconf").value.trim() }); toast("LIVE on"); account(); } catch (e) { toast(e.message); } };
  const gp = $("#gopaper"); if (gp) gp.onclick = async () => { await post("/api/mode", { mode: "PAPER" }); account(); };
  $("#logout").onclick = () => { localStorage.removeItem(KEY); login(); };
  document.querySelectorAll("[data-set]").forEach(r => r.onclick = async () => {
    const key = r.dataset.set, cur = JSON.parse(r.dataset.val); const nv = prompt(key, typeof cur === "object" ? JSON.stringify(cur) : cur); if (nv == null) return;
    try { const res = await post("/api/settings", { key, value: nv }); toast(`${key}: ${res.old} → ${res.new}`); account(); } catch (e) { toast(e.message); }
  });
}

async function enablePush() {
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) { toast("On iPhone: Share → Add to Home Screen, then open Desk from the icon."); return; }
    const perm = await Notification.requestPermission(); if (perm !== "granted") { toast("Notifications were not allowed"); return; }
    const reg = await navigator.serviceWorker.ready, k = await api("/api/push/key"); if (!k.enabled) { toast("Server push keys missing"); return; }
    const raw = atob(k.publicKey.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - k.publicKey.length % 4) % 4));
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, c => c.charCodeAt(0)) });
    await post("/api/push/subscribe", sub.toJSON()); toast("Push enabled ✔"); account();
  } catch (e) { toast("Push failed: " + e.message); }
}

async function coinSheet(mint) {
  openSheet(`<div class="state loading"><div class="spinner"></div></div>`, "Coin");
  try {
    const [d, ch] = await Promise.all([api("/api/coin/" + encodeURIComponent(mint)), api("/api/chart?tf=60&mint=" + encodeURIComponent(mint))]);
    const c = d.coin, sc = d.scan || {}, p = d.position, ve = d.verdict;
    $("#sheet .ttl").textContent = c.symbol || mint.slice(0, 6);
    sheetBody(`<div class="card"><div class="sub">${esc(c.name || "")} · ${esc(c.status)}</div><div class="big num">${px(sc.price_usd)}</div>
      <div class="sub">h1 <span class="${cls(sc.chg_h1)}">${pct(sc.chg_h1)}</span> · m5 <span class="${cls(sc.chg_m5)}">${pct(sc.chg_m5)}</span> · liq ${usd(sc.liq_usd, 0)}</div>
      <div class="seg" style="margin-top:12px"><button data-tf="15">15s</button><button data-tf="60" class="on">1m</button><button data-tf="300">5m</button></div><canvas id="cc"></canvas></div>
      ${p ? `<div class="card"><div class="kv"><span>Position</span><span>${esc(p.status)}</span></div><div class="kv"><span>Ticket</span><span class="num">${usd(p.ticket_usd)}</span></div><div class="kv"><span>Entry</span><span class="num">${px(p.entry_price)}</span></div><div class="kv"><span>P&L</span><span class="num ${cls(p.pnl_pct ?? p.upnl_pct)}">${pct(p.pnl_pct ?? p.upnl_pct)}</span></div>${p.exit_reason ? `<div class="kv"><span>Exit</span><span>${esc(p.exit_reason)}</span></div>` : ""}</div>` : ""}
      ${ve ? `<div class="card"><div class="kv"><span>Analyst</span><span class="pill ${ve.decision === "buy" ? "ok" : "bad"}">${esc(ve.decision)} · ${ve.score}</span></div><div class="kv"><span>Alive</span><span class="num">${ve.alive}</span></div><div class="kv"><span>Organic</span><span class="num">${ve.organic}</span></div><div class="kv"><span>Chase</span><span class="num">${ve.chase}</span></div><div class="sub" style="margin-top:8px">${esc(ve.reasons.join(" · "))}</div></div>` : ""}
      <button class="btn blue" data-audit="${esc(mint)}" style="margin-bottom:10px">Auditor disc</button>
      <a class="btn primary" href="${esc(d.link)}" target="_blank" rel="noopener noreferrer">${esc(d.button || "Buy on FOMO")}</a>
      <div class="sub" style="text-align:center;margin-top:10px">The desk never trades for you — FOMO opens so you can decide.</div>`);
    candleChart("#cc", ch.candles, ch.entry && ch.entry.entry_price);
    document.querySelectorAll("[data-tf]").forEach(b => b.onclick = async () => { document.querySelectorAll("[data-tf]").forEach(x => x.classList.remove("on")); b.classList.add("on"); const r = await api(`/api/chart?tf=${b.dataset.tf}&mint=${encodeURIComponent(mint)}`); candleChart("#cc", r.candles, r.entry && r.entry.entry_price); });
  } catch (e) { sheetBody(errorBox(e)); }
}

async function auditSheet(mint) {
  openSheet(`<div class="state loading"><div class="spinner"></div></div>`, "Auditor");
  try {
    const a = await api("/api/audit/" + encodeURIComponent(mint));
    sheetBody(`<div class="disc-wrap"><canvas id="disc"></canvas></div><div style="text-align:center;margin-bottom:12px"><b>${esc(a.headline)}</b><div class="sub">${esc(a.subline)} · ${ago(a.ts)} ago</div></div>
      <div class="list">${a.layers.map(l => `<div class="layer"><div class="n ${l.status}">${l.n}</div><div class="grow"><div class="t" style="font-weight:600">${esc(l.name)}</div><div class="s ${l.status === "pass" ? "" : "down"}">${esc(l.note)}</div></div><div class="r s num">${l.value == null ? "" : esc(l.value)}${l.limit != null && typeof l.limit === "number" ? " / " + esc(l.limit) : ""}</div></div>`).join("")}</div>
      <a class="btn" href="${esc(a.link)}" target="_blank" rel="noopener noreferrer">Open on FOMO</a>`);
    disc("#disc", a.layers);
  } catch (e) { sheetBody(errorBox(e)); }
}

function openSheet(body, title) { const s = $("#sheet"); s.innerHTML = `<div class="bar"><button class="back">‹ Back</button><b class="ttl" style="text-transform:capitalize">${esc(title)}</b><span style="width:50px"></span></div><div class="body">${body}</div>`; s.classList.remove("hidden"); s.querySelector(".back").onclick = () => s.classList.add("hidden"); }
function sheetBody(html) { $("#sheet .body").innerHTML = html; }

function login(msg) {
  stopStream(); $("#tabs").classList.add("hidden"); $("#mode-badge").classList.add("hidden"); setTitle("Desk");
  $("#view").innerHTML = `<div class="login"><h1>Grok Desk</h1><div class="sub">${esc(msg || "Paste your access code. On the server run: grok-desk-url")}</div>
    <input id="tok" placeholder="Access code" autocapitalize="off" autocorrect="off" spellcheck="false"><button class="btn primary" id="tokgo">Unlock</button></div>`;
  $("#tokgo").onclick = () => { const t = $("#tok").value.trim().replace(/^.*#t=/, ""); if (t.length < 20) { toast("That code looks too short"); return; } localStorage.setItem(KEY, t); $("#tabs").classList.remove("hidden"); go("home"); startStream(); };
}

// ---------- live stream (fetch-based SSE so the token stays in a header) ----------
let streamCtl = null;
function stopStream() { if (streamCtl) streamCtl.abort(); streamCtl = null; $("#conn").classList.remove("on"); }
async function startStream() {
  stopStream(); const ctl = new AbortController(); streamCtl = ctl;
  try {
    const r = await fetch("/api/stream", { headers: { Authorization: "Bearer " + token() }, signal: ctl.signal });
    if (!r.ok) throw new Error(r.status); $("#conn").classList.add("on");
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) { const { value, done } = await rd.read(); if (done) break; buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2); const data = chunk.split("\n").filter(l => l.startsWith("data: ")).map(l => l.slice(6)).join("\n"); if (data) onEvent(JSON.parse(data)); } }
  } catch (e) { if (ctl.signal.aborted) return; }
  $("#conn").classList.remove("on"); if (streamCtl === ctl) setTimeout(startStream, 4000);
}
let refreshT = null;
function onEvent(ev) {
  if (ev.topic === "activity" && tab === "activity") { const f = $("#feed"); if (f) f.insertAdjacentHTML("afterbegin", feedRow({ ...ev, ts: ev.ts })); }
  if (ev.topic === "notify") toast(ev.title + " — " + ev.body);
  clearTimeout(refreshT); refreshT = setTimeout(() => { if (tab === "home") home(); else loadState().catch(() => {}); }, 800);
}

// ---------- navigation ----------
const screens = { home, agents, activity, account };
function go(t) { tab = t; document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t)); window.scrollTo(0, 0); screens[t](); }
document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => go(b.dataset.tab));
document.addEventListener("click", e => {
  if (e.target.closest("[data-retry]")) { $("#sheet").classList.add("hidden"); go(tab); return; }
  const a = e.target.closest("[data-audit]"); if (a) { auditSheet(a.dataset.audit); return; }
  const c = e.target.closest("[data-coin]"); if (c) { coinSheet(c.dataset.coin); return; }
  const g = e.target.closest("[data-agent]"); if (g) agentSheet(g.dataset.agent);
});
window.__desk = { go, coin: coinSheet, audit: auditSheet, agent: agentSheet };
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
if (!token()) login(); else { go("home"); startStream(); }
setInterval(() => { if (token() && tab === "home" && $("#sheet").classList.contains("hidden")) home(); }, 30000);
})();
