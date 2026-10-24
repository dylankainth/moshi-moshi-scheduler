"use strict";
// Foresight UI. No build, no CDN, no inline handlers (strict CSP): all clicks go through data-act delegation.

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtMin = (m) => {
  if (m == null || isNaN(m)) return "–";
  m = Math.round(m);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};
const fmtDur = (s) => (s < 60 ? `${Math.round(s)} s` : fmtMin(s / 60));
const fmtClock = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
};
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};

const S = {
  cfg: null, view: "now", token: null, demo: false, sid: null,
  draft: { desc: "", guess: "" }, est: null, estSeq: 0, explained: null,
  active: null, poll: null, dismissedAsk: 0, result: null, actualEdit: null,
};

// ---------- API ----------
async function api(method, path, body) {
  const h = { "Content-Type": "application/json" };
  if (S.token) h.Authorization = `Bearer ${S.token}`;
  if (S.demo) h["X-Demo-Session"] = S.sid;
  const r = await fetch(path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg; t.setAttribute("role", "status");
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message); } };

// ---------- shell ----------
async function boot() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) { store.set("fs_token", decodeURIComponent(m[1])); history.replaceState(null, "", location.pathname); }
  S.token = store.get("fs_token");
  S.sid = store.get("fs_sid") || (crypto.getRandomValues(new Uint32Array(2)).join("").slice(0, 16));
  store.set("fs_sid", S.sid);
  S.demo = store.get("fs_demo") === "1";
  S.cfg = await api("GET", "/api/config");
  if (S.cfg.mode === "cloud" && S.cfg.user !== "me") S.demo = true;   // no owner token: demo session
  if (S.cfg.mode === "cloud" && S.cfg.user === "me") S.demo = store.get("fs_demo") === "1";
  renderBadges();
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  document.addEventListener("click", onClick);
  document.addEventListener("input", onInput);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.id === "desc" && !e.shiftKey) { e.preventDefault(); act.start(); }
  });
  show(location.hash.slice(1) || "now");
}

function renderBadges() {
  const b = $("#mode-badge");
  if (S.cfg.mode === "local") { b.className = "badge local"; b.textContent = "● Local · zero egress"; b.title = "API on 127.0.0.1 only. Nothing leaves this machine."; }
  else { b.className = "badge cloud"; b.textContent = "☁ Cloud · Vultr"; b.title = "Hosted on Vultr. Your PC sends only attributed segments."; }
  const d = $("#demo-toggle");
  const canToggle = S.cfg.mode === "local" || S.cfg.user === "me";
  d.hidden = false;
  d.className = "badge demo-badge" + (S.demo ? " on" : "");
  d.textContent = S.demo ? "Demo user · scripted replay ✕" : "Try the scripted demo";
  d.disabled = !canToggle && S.demo;
  if (!canToggle) d.textContent = "Demo user · read-only";
  $("#foot-mode").textContent = S.cfg.mode === "local"
    ? "Local mode: everything stays on this machine."
    : "Cloud mode: raw activity stays on your PC; the server sees only attributed segments.";
}
$("#demo-toggle").addEventListener("click", () => {
  if (S.cfg.mode === "cloud" && S.cfg.user !== "me") return;
  S.demo = !S.demo; store.set("fs_demo", S.demo ? "1" : "0");
  S.est = null; S.result = null; S.active = null; S.explained = null;
  renderBadges(); show(S.view);
});

function show(view) {
  if (!["now", "calibration", "privacy", "system"].includes(view)) view = "now";
  S.view = view;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  clearInterval(S.poll); S.poll = null;
  ({ now: loadNow, calibration: loadCalibration, privacy: loadPrivacy, system: loadSystem })[view]();
}

// ---------- Task view: estimate -> live -> result ----------
async function loadNow() {
  const a = await api("GET", "/api/tasks/active");
  S.active = a;
  if (a.task && a.task.status === "review") { S.result = a.card; return renderResult(); }
  if (a.task) { renderLive(); S.poll = setInterval(guard(pollLive), 2000); return; }
  if (S.result && S.result.status === "done") return renderResult();
  renderEstimate();
}

function renderEstimate() {
  const demoHint = S.demo ? `<p class="small muted">Demo user: you'll watch a scripted ${S.cfg.demo.script_min}-minute work session replay at ${S.cfg.demo_speed}× speed. Try “${esc(S.cfg.demo.desc)}” with an optimistic guess.</p>` : "";
  $("#view-now").innerHTML = `
    <div class="card">
      <h2>What are you about to do?</h2>${demoHint}
      <div class="row">
        <div class="grow"><label for="desc">Task</label>
          <input id="desc" class="desc-input" type="text" maxlength="200" autocomplete="off" placeholder="fix the login bug" value="${esc(S.draft.desc)}"></div>
        <div><label for="guess">Your guess (min, optional)</label>
          <input id="guess" class="guess-input" type="number" min="1" max="1440" inputmode="numeric" value="${esc(S.draft.guess)}"></div>
      </div>
      <div id="est"></div>
    </div>`;
  if (S.est) renderEst(); else $("#est").innerHTML = "";
  $("#desc").focus();
}

let estTimer = null;
function onInput(e) {
  if (e.target.id === "desc" || e.target.id === "guess") {
    S.draft[e.target.id] = e.target.value;
    clearTimeout(estTimer);
    estTimer = setTimeout(guard(runEstimate), 250);
  }
  if (e.target.id === "actual") S.actualEdit = e.target.value;
}

async function runEstimate() {
  const desc = S.draft.desc.trim();
  if (!desc) { S.est = null; $("#est").innerHTML = ""; return; }
  const seq = ++S.estSeq;
  const t0 = performance.now();
  const body = { desc, guess: S.draft.guess || null, type: S.draft.type || null };
  const e = await api("POST", "/api/estimate", body);
  if (seq !== S.estSeq) return;
  e.ms = Math.round(performance.now() - t0);
  S.est = e; renderEst();
  if (S.cfg.inference !== "none") explainLater(body, seq);
}

async function explainLater(body, seq) {
  try {
    const x = await api("POST", "/api/explain", body);
    if (seq !== S.estSeq || !S.est) return;
    S.est.explanation = x.explanation; S.est.explanation_source = x.explanation_source; renderEst();
  } catch { /* template already shown */ }
}

function rangeBar(est, guess, actual) {
  const vals = [est.lo80, est.hi80, guess, actual].filter((v) => v > 0);
  const lo = Math.log(Math.min(...vals) * 0.8), hi = Math.log(Math.max(...vals) * 1.15);
  const x = (v) => ((Math.log(v) - lo) / (hi - lo)) * 100;
  let h = `<div class="range" aria-label="estimate range">
    <div class="axis"></div>
    <div class="b80" data-style="left:${x(est.lo80)}%;width:${x(est.hi80) - x(est.lo80)}%"></div>
    <div class="b50" data-style="left:${x(est.lo50)}%;width:${x(est.hi50) - x(est.lo50)}%"></div>
    <div class="tick" data-style="left:${x(est.median)}%"></div>
    <div class="mk" data-style="left:${x(est.median)}%"><b>${fmtMin(est.median)}</b></div>
    <div class="mk below" data-style="left:${x(est.lo80)}%">${fmtMin(est.lo80)}</div>
    <div class="mk below" data-style="left:${x(est.hi80)}%">${fmtMin(est.hi80)}</div>`;
  if (guess) h += `<div class="pin guess" data-style="left:${x(guess)}%" title="your guess"></div>`;
  if (actual) h += `<div class="pin actual" data-style="left:${x(actual)}%" title="actual"></div>`;
  return h + `</div><div class="legend"><span><i data-style="background:var(--band50)"></i>50% range</span>
    <span><i data-style="background:var(--band80)"></i>80% range</span>
    ${guess ? `<span><i data-style="background:var(--pending)"></i>your guess ${fmtMin(guess)}</span>` : ""}
    ${actual ? `<span><i data-style="background:var(--on)"></i>actual ${fmtMin(actual)}</span>` : ""}</div>`;
}
function renderEst() {
  const e = S.est, est = e.estimate;
  const types = [...new Set([...(e.types || []), e.type])].sort();
  $("#est").innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="big">${fmtMin(est.median)}</div><div class="l">likely (median)</div></div>
      <div class="kpi"><div class="v">${fmtMin(est.lo50)}–${fmtMin(est.hi50)}</div><div class="l">50% range</div></div>
      <div class="kpi"><div class="v">${fmtMin(est.lo80)}–${fmtMin(est.hi80)}</div><div class="l">80% range</div></div>
    </div>
    ${rangeBar(est, e.guess)}
    <div class="expl"><span id="expl-text">${esc(e.explanation)}</span>
      <div class="small muted">${e.explanation_source === "template" ? "Explanation: deterministic template" : `Explanation: ${esc(e.explanation_source)} model`} · estimated in ${e.ms} ms</div></div>
    <div class="row">
      <div><label for="type">Task type</label>
        <select id="type">${types.map((t) => `<option ${t === e.type ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
      <div class="small muted grow">${e.is_new_type ? "New type: nothing similar in your history yet." : `${est.n_type} past tasks of this type`}</div>
      <button class="btn primary" data-act="start">Start task</button>
    </div>
    ${e.neighbours.length ? `<h3>Past tasks behind this estimate</h3><ul class="nbs">${e.neighbours.map((n) => `
      <li><span>${esc(n.desc)} <span class="chip">${esc(n.type)}</span></span>
      <span class="muted">${n.guess ? `guessed ${fmtMin(n.guess)} · ` : ""}took <b>${fmtMin(n.actual)}</b></span></li>`).join("")}</ul>` : ""}`;
  applyStyles($("#est"));
  $("#type").addEventListener("change", (ev) => { S.draft.type = ev.target.value; guard(runEstimate)(); });
}

// Strict CSP (style-src 'self') ignores style="" attributes; dynamic positions go through the CSSOM instead.
function applyStyles(root) {
  root.querySelectorAll("[data-style]").forEach((el) => {
    const css = el.dataset.style; el.removeAttribute("data-style");
    css.split(";").forEach((d) => { const i = d.indexOf(":"); if (i > 0) el.style.setProperty(d.slice(0, i).trim(), d.slice(i + 1).trim()); });
  });
}

const act = {
  start: guard(async () => {
    const desc = (S.draft.desc || "").trim();
    if (!desc) return $("#desc")?.focus();
    await api("POST", "/api/tasks", { desc, guess: S.draft.guess || null, type: S.est?.type || null });
    S.draft = { desc: "", guess: "" }; S.est = null; S.result = null; S.dismissedAsk = 0;
    loadNow();
  }),
  done: guard(async () => {
    S.result = await api("POST", `/api/tasks/${S.active.task.id}/done`);
    clearInterval(S.poll); S.poll = null; S.actualEdit = null;
    renderResult();
  }),
  discard: guard(async (el) => {
    const id = el.dataset.id || S.active?.task?.id;
    await api("POST", `/api/tasks/${id}/discard`);
    clearInterval(S.poll); S.poll = null; S.result = null; S.active = null;
    renderEstimate();
  }),
  confirm: guard(async () => {
    const v = parseFloat($("#actual").value);
    if (!(v > 0)) return toast("Enter the actual minutes");
    S.result = await api("POST", `/api/tasks/${S.result.id}/confirm`, { actual: v });
    renderResult();
  }),
  next: () => { S.result = null; renderEstimate(); },
  stillOn: () => { S.dismissedAsk = Date.now(); renderLive(); },
  toggle: guard(async (el) => {
    await api("POST", `/api/tasks/${S.active.task.id}/override`, { start: +el.dataset.start, counted: el.dataset.to === "1" });
    toast("Marked. The agent applies it on its next pass.");
  }),
  rename: guard(async (el) => {
    const inp = $(`#rn-${el.dataset.i}`);
    await api("POST", "/api/types/rename", { old: el.dataset.old, new: inp.value });
    toast(`Renamed. Renaming onto an existing type merges them.`); loadCalibration();
  }),
  pause: guard(async (el) => { await api("POST", "/api/privacy/pause", { paused: el.dataset.to === "1" }); loadPrivacy(); }),
  addCat: () => { const c = readIgnore(); c[`custom${Object.keys(c).length + 1}`] = { exe: [], title: [] }; renderIgnore(c); },
  delCat: (el) => { const c = readIgnore(); delete c[el.dataset.cat]; renderIgnore(c); },
  saveIgnore: guard(async () => { await api("PUT", "/api/privacy/ignore", { ignore: readIgnore() }); toast("Ignore list saved. Capture picks it up within seconds."); }),
  resetIgnore: () => renderIgnore(structuredClone(S.privacy.defaults)),
  deleteAll: guard(async () => {
    if ($("#del-confirm").value.trim() !== "DELETE") return toast("Type DELETE to confirm");
    await api("POST", "/api/privacy/delete-all"); toast("Everything deleted and the database vacuumed."); loadPrivacy();
  }),
  saveThresholds: guard(async () => {
    const values = {};
    for (const [k, , unit] of TH) { const v = parseFloat($(`#th-${k}`).value); if (!isNaN(v)) values[k] = unit === "min" ? v * 60 : v; }
    await api("PUT", "/api/thresholds", { values }); toast("Saved. The live task recomputes within 5 s."); loadSystem();
  }),
  resetThresholds: guard(async () => { await api("PUT", "/api/thresholds", { values: {} }); toast("Thresholds reset."); loadSystem(); }),
  reseed: guard(async () => { await api("POST", "/api/privacy/reseed"); toast("Demo history loaded."); loadPrivacy(); }),
};
function onClick(e) {
  const el = e.target.closest("[data-act]");
  if (el && act[el.dataset.act]) { e.preventDefault(); act[el.dataset.act](el); }
}

let polling = false;
async function pollLive() {
  if (S.view !== "now" || polling) return;
  polling = true;
  let a;
  try { a = await api("GET", "/api/tasks/active"); } finally { polling = false; }
  S.active = a;
  if (!a.task) { clearInterval(S.poll); S.poll = null; return renderEstimate(); }
  renderLive();
}

function renderLive() {
  const { task, live, agent_connected, replay } = S.active;
  const est = task.est, segs = live?.segments || [], tot = live?.totals || { counted_s: 0, off_s: 0, pending_s: 0 };
  const total = segs.reduce((s, x) => s + x.dur, 0) || 1;
  const cls = (s) => s.counted === true ? "t-on" : s.counted === null ? "t-pending" : ["idle", "nowindow", "gap"].includes(s.kind) ? "t-hard" : "t-off";
  const elapsed = live ? live.now - task.started : 0;
  const ask = live?.ask_done && Date.now() - S.dismissedAsk > 10 * 60 * 1000;
  const status = replay ? `<span class="status"><span class="dot ok"></span>scripted replay at ${S.cfg.demo_speed}×</span>`
    : `<span class="status"><span class="dot ${agent_connected ? "ok" : "bad"}"></span>${agent_connected ? "agent connected" : "waiting for the capture agent…"}</span>`;
  const showLabels = segs.some((s) => s.label);
  $("#view-now").innerHTML = `
    ${ask ? `<div class="banner" role="alert"><div class="grow"><b>Done with “${esc(task.desc)}”?</b>
      You've been on unrelated things for 15+ minutes. That time isn't counted either way.</div>
      <button class="btn primary" data-act="done">Yes, I'm done</button><button class="btn" data-act="stillOn">Still on it</button></div>` : ""}
    <div class="card">
      <div class="row"><div class="grow"><h2>${esc(task.desc)}</h2>
        <div class="small muted">Foresight said ${fmtMin(est.median)} (80%: ${fmtMin(est.lo80)}–${fmtMin(est.hi80)})${task.guess ? ` · you guessed ${fmtMin(task.guess)}` : ""}</div></div>
        ${status}</div>
      <div class="kpis">
        <div class="kpi"><div class="big">${fmtClock(tot.counted_s)}</div><div class="l">counting toward task</div></div>
        <div class="kpi"><div class="v">${fmtClock(tot.off_s)}</div><div class="l">not counted</div></div>
        <div class="kpi"><div class="v">${fmtClock(tot.pending_s)}</div><div class="l">pending</div></div>
        <div class="kpi"><div class="v">${fmtClock(elapsed)}</div><div class="l">elapsed</div></div>
      </div>
      <div class="timeline" aria-hidden="true">${segs.map((s) => `<span class="${cls(s)}" data-style="flex:${s.dur / total}" title="${esc(fmtDur(s.dur))} · ${esc(s.reason)}"></span>`).join("")}</div>
      <div class="legend"><span><i class="t-on"></i>counted</span><span><i class="t-off"></i>not counted</span>
        <span><i class="t-pending"></i>pending (provisional)</span><span><i class="t-hard"></i>idle / locked / no data</span></div>
      <div class="row" data-style="margin-top:16px"><button class="btn primary" data-act="done">Done</button>
        <button class="btn" data-act="discard">Discard</button>
        <span class="small muted grow">${live?.current ? `Now: ${esc(live.current)}` : ""}</span></div>
    </div>
    <div class="card"><h2>What's counted, and why</h2>
      <p class="small muted">Decided cheapest first: rules → embedding similarity → LLM (only for long, still-ambiguous stretches). Recomputed every few seconds, so earlier stretches can change once later activity settles them.${showLabels ? "" : " Window titles stay on your PC, so only decisions are shown here."}</p>
      <div class="table-wrap"><table class="segs"><thead><tr><th>Time</th><th></th>${showLabels ? "<th>Activity</th>" : "<th>Kind</th>"}<th>Decision</th><th class="hide-sm">Why</th><th></th></tr></thead><tbody>
      ${segs.slice().reverse().map((s) => `<tr>
        <td>${fmtDur(s.dur)}</td><td><span class="dot ${cls(s)}"></span></td>
        <td class="lbl">${esc(showLabels ? s.label : s.kind)}</td>
        <td><span class="chip ${esc(s.source)}">${esc(s.source)}</span></td>
        <td class="hide-sm muted">${esc(s.reason)}</td>
        <td>${["gap", "idle", "nowindow"].includes(s.kind) ? "" : `<button class="btn small" data-act="toggle" data-start="${s.start}" data-to="${s.counted ? 0 : 1}">${s.counted ? "Don't count" : "Count"}</button>`}</td>
      </tr>`).join("")}</tbody></table></div>
      ${segs.length ? "" : `<p class="muted">No activity yet. ${replay ? "The replay is starting…" : "Switch to your work; the agent samples every 3 s."}</p>`}
    </div>`;
  applyStyles($("#view-now"));
}

function renderResult() {
  const c = S.result, est = c.estimate;
  const saved = c.status === "done";
  const actual = saved ? c.actual : parseFloat(S.actualEdit ?? c.measured) || c.measured;
  const inRange = est.lo80 <= actual && actual <= est.hi80;
  const vs = c.guess ? actual / c.guess : null;
  $("#view-now").innerHTML = `
    <div class="card">
      <h2>${saved ? "Saved" : "How did it go?"} · ${esc(c.desc)}</h2>
      <div class="kpis">
        <div class="kpi"><div class="v">${fmtMin(est.lo80)}–${fmtMin(est.hi80)}</div><div class="l">Foresight (80%) · median ${fmtMin(est.median)}</div></div>
        <div class="kpi"><div class="v">${c.guess ? fmtMin(c.guess) : "–"}</div><div class="l">your guess</div></div>
        <div class="kpi"><div class="big">${fmtMin(actual)}</div><div class="l">actual${saved ? "" : " (measured)"}</div></div>
      </div>
      ${rangeBar(est, c.guess, actual)}
      <div class="verdict ${inRange ? "good" : "bad"}">${inRange ? "Inside Foresight's 80% range." : "Outside Foresight's 80% range. It learns from this too."}
        ${vs ? ` You took <b>${vs.toFixed(1)}×</b> your guess.` : ""}</div>
      ${saved ? `<p>${c.saved_to_model ? `Added to your model.${c.bias ? ` You now take about <b>${c.bias.toFixed(1)}×</b> what you say.` : ""}` : "Demo user: shown, but not added to the model."}</p>
        <button class="btn primary" data-act="next">Next task</button>`
      : `<div class="row"><div><label for="actual">Actual minutes. Fix it if the agent got it wrong.</label>
          <input id="actual" class="guess-input" type="number" min="1" max="1440" step="1" value="${Math.round(actual)}"></div>
          <button class="btn primary" data-act="confirm">Confirm & save</button>
          <button class="btn" data-act="discard" data-id="${c.id}">Discard (don't learn from this)</button></div>
        <p class="small muted">Only confirmed times train the model, so a bad measurement never sneaks in.</p>`}
    </div>`;
  applyStyles($("#view-now"));
  if (!saved) $("#actual").addEventListener("change", () => renderResult());
}

// ---------- Calibration ----------
function lineChart(points, { yMin, yMax, ref, refLabel, fmtY = (v) => v.toFixed(1), cls = "ln", xLabel = "" }) {
  const W = 420, H = 180, L = 36, R = 10, T = 10, B = 26;
  if (points.length < 2) return `<p class="muted small">Not enough data yet.</p>`;
  const xs = points.map((p) => p[0]), x0 = Math.min(...xs), x1 = Math.max(...xs);
  const X = (v) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R), Y = (v) => T + (1 - (v - yMin) / (yMax - yMin)) * (H - T - B);
  const ticks = [yMin, (yMin + yMax) / 2, yMax];
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(Math.min(yMax, Math.max(yMin, p[1]))).toFixed(1)}`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">
    ${ticks.map((t) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${L - 6}" y="${Y(t) + 4}" text-anchor="end">${fmtY(t)}</text>`).join("")}
    ${ref != null ? `<line class="ref" x1="${L}" x2="${W - R}" y1="${Y(ref)}" y2="${Y(ref)}"/><text x="${W - R}" y="${Y(ref) - 4}" text-anchor="end">${esc(refLabel || "")}</text>` : ""}
    <path class="${cls}" d="${d}"/>
    <text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle">${esc(xLabel)}</text></svg>`;
}

async function loadCalibration() {
  const c = await api("GET", "/api/calibration");
  const owner = !S.demo && S.cfg.user === "me";
  const bs = c.bias_series.map((p, i) => [i, p.bias]);
  const bmax = Math.max(2, ...bs.map((p) => p[1])) * 1.1;
  $("#view-calibration").innerHTML = `
    <div class="card"><p class="headline">${esc(c.bias_text)}</p>
      <p class="muted">${c.n} confirmed tasks. Foresight's 80% ranges held ${c.hit80 != null ? Math.round(c.hit80 * 100) + "%" : "–"} of the time (target 80%), 50% ranges ${c.hit50 != null ? Math.round(c.hit50 * 100) + "%" : "–"} (target 50%).</p></div>
    <div class="grid2">
      <div class="card"><h2>Your bias over time</h2><p class="small muted">Actual ÷ guess at your typical guess. 1.0 = spot on.</p>
        ${lineChart(bs, { yMin: 0.5, yMax: bmax, ref: 1, refLabel: "spot on", xLabel: "tasks with a guess →" })}</div>
      <div class="card"><h2>Range hit rate</h2><p class="small muted">Share of the last 20 actuals inside the 80% range.</p>
        ${lineChart(c.hit_series.map((p) => [p.i, p.rate]), { yMin: 0, yMax: 1, ref: 0.8, refLabel: "target 80%", fmtY: (v) => Math.round(v * 100) + "%", cls: "ln2", xLabel: "tasks →" })}</div>
      <div class="card"><h2>Ranges narrowing</h2><p class="small muted">How wide the 80% range was (top ÷ bottom) on the nth task of a type.</p>
        ${lineChart(c.narrowing.map((p) => [p.n, p.width]), { yMin: 1, yMax: Math.max(4, ...c.narrowing.map((p) => p.width)) * 1.05, fmtY: (v) => v.toFixed(1) + "×", xLabel: "nth task of that type →" })}</div>
      <div class="card"><h2>Task types</h2>
        <p class="small muted">${owner ? "Rename a type, or rename it onto an existing one to merge them." : "Read-only for the demo user."}</p>
        <table class="segs"><thead><tr><th>Type</th><th>Tasks</th><th>Median</th>${owner ? "<th></th>" : ""}</tr></thead><tbody>
        ${c.types.map((t, i) => `<tr><td>${owner ? `<input type="text" id="rn-${i}" value="${esc(t.type)}" aria-label="name for ${esc(t.type)}">` : esc(t.type)}</td>
          <td>${t.count}</td><td>${fmtMin(t.median)}</td>
          ${owner ? `<td><button class="btn small" data-act="rename" data-i="${i}" data-old="${esc(t.type)}">Save</button></td>` : ""}</tr>`).join("")}
        </tbody></table></div>
    </div>`;
}

// ---------- Privacy ----------
async function loadPrivacy() {
  const el = $("#view-privacy");
  if (S.cfg.mode !== "local" || S.demo) {
    el.innerHTML = `<div class="card"><h2>Privacy</h2>
      <p>${S.cfg.mode === "cloud" ? "In cloud mode the capture agent runs on your own PC. Raw window history, the ignore list and pause stay there. This server only ever receives attributed segments (start, end, counted, source, reason) and redacted snippets for stretches that stay ambiguous." : "Switch off the demo user to manage your own data."}</p>
      <p class="muted">Screenshots, keystrokes and clipboard are never collected in any mode. Ignored apps (banking, password managers, messaging, email, private browsing) are stored only as an anonymous “private” sample with a category.</p></div>`;
    return;
  }
  S.privacy = await api("GET", "/api/privacy");
  const p = S.privacy;
  el.innerHTML = `
    <div class="card"><h2>Capture</h2>
      <div class="row"><div class="grow">${p.paused ? "<b>Paused.</b> Nothing is being recorded. Paused time is a gap: no data, never task time." : "<b>Recording</b> the active app, window title and idle state every 3 s, on this machine only."}</div>
      <button class="btn ${p.paused ? "primary" : ""}" data-act="pause" data-to="${p.paused ? 0 : 1}">${p.paused ? "Resume" : "Pause"}</button></div></div>
    <div class="card"><h2>Ignore list</h2>
      <p class="small muted">Matching windows are stored as “private · category” only. The app name, title and URL are never written to disk or sent to any model. Their time still follows the detour rules, so a quick Slack check can count without Slack being read. Titles match whole words.</p>
      <div id="cats"></div>
      <div class="row"><button class="btn" data-act="addCat">Add category</button><button class="btn" data-act="resetIgnore">Restore defaults</button>
        <span class="grow"></span><button class="btn primary" data-act="saveIgnore">Save ignore list</button></div></div>
    <div class="card"><h2>Delete everything</h2>
      <p class="small muted">Drops every table (samples, tasks, segments, settings) and vacuums the database file. This can't be undone.</p>
      <div class="row"><div><label for="del-confirm">Type DELETE to confirm</label><input id="del-confirm" type="text" autocomplete="off"></div>
        <button class="btn danger" data-act="deleteAll">Delete all data</button>
        <span class="grow"></span><button class="btn" data-act="reseed">Load demo history</button></div></div>`;
  renderIgnore(p.ignore);
}
function renderIgnore(cats) {
  $("#cats").innerHTML = Object.entries(cats).map(([k, v]) => `
    <div class="cat" data-cat="${esc(k)}"><div class="row"><div><label>Category</label><input type="text" class="c-name" value="${esc(k)}"></div>
      <div><label>Apps (exe names, one per line)</label><textarea class="c-exe">${esc((v.exe || []).join("\n"))}</textarea></div>
      <div><label>Title keywords (one per line)</label><textarea class="c-title">${esc((v.title || []).join("\n"))}</textarea></div>
      <button class="btn small" data-act="delCat" data-cat="${esc(k)}">Remove</button></div></div>`).join("");
}
function readIgnore() {
  const out = {};
  document.querySelectorAll("#cats .cat").forEach((c) => {
    const lines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);
    const name = $(".c-name", c).value.trim();
    if (name) out[name] = { exe: lines($(".c-exe", c).value), title: lines($(".c-title", c).value) };
  });
  return out;
}

// ---------- System ----------
const TH = [ // [field, label, unit, hint]
  ["idle_off", "Idle cutoff", "min", "No input for this long never counts."],
  ["detour_max", "Detour limit", "min", "Off-task runs up to this long between real work still count."],
  ["unrelated_ask", "Ask \"done?\" after", "min", "This much off-task time at the end asks if you're done."],
  ["judge_min", "LLM minimum", "s", "Only ask the LLM about ambiguous stretches at least this long."],
  ["max_gap", "Gap after", "s", "A longer wait between samples is a gap (asleep / agent off)."],
  ["sim_on", "Similarity", "0–1", "Embedding similarity that counts as on-task."],
];
function thresholdsCard(t) {
  const show = (k, unit, v) => +(unit === "min" ? v / 60 : v).toFixed(3);
  return `<div class="card"><h2>Thresholds</h2>
    <p class="small muted">For testing. Changes apply to the current task within 5 s, retroactively. Saved in the local database.</p>
    <div class="row">${TH.map(([k, label, unit, hint]) => `<div><label for="th-${k}" title="${esc(hint)}">${esc(label)} (${unit})</label>
      <input id="th-${k}" type="number" min="0" step="any" class="guess-input" value="${show(k, unit, t.values[k])}" title="${esc(hint)} Default ${show(k, unit, t.defaults[k])}."></div>`).join("")}</div>
    <div class="row"><span class="grow"></span><button class="btn" data-act="resetThresholds">Reset</button><button class="btn primary" data-act="saveThresholds">Save thresholds</button></div></div>`;
}
async function loadSystem() {
  const [inf, health, th] = await Promise.all([api("GET", "/api/inference"), api("GET", "/api/health"),
    S.cfg.mode === "local" && !S.demo ? api("GET", "/api/thresholds") : null]);
  const order = ["rule", "embedding", "llm", "user", "default"];
  const share = inf.share || {};
  const names = { rule: "rules", embedding: "embedding", llm: "LLM", user: "you", default: "fallback" };
  $("#view-system").innerHTML = `
    <div class="card"><h2>Who settled each stretch</h2>
      <p class="small muted">Cheapest first: most stretches are settled by rules and local embeddings. The LLM only sees the few that stay ambiguous.</p>
      <div class="share">${order.filter((k) => share[k]).map((k) => `<span class="s-${k}" data-style="flex:${share[k]}" title="${names[k]} ${Math.round(share[k] * 100)}%">${share[k] > 0.08 ? `${names[k]} ${Math.round(share[k] * 100)}%` : ""}</span>`).join("")}</div>
      <div class="legend">${order.map((k) => `<span><i class="s-${k}"></i>${names[k]}: ${inf.resolved_by?.[k] || 0}</span>`).join("")}</div>
      ${Object.keys(share).length ? "" : `<p class="small muted">Nothing settled yet. Run a task (or the scripted demo) and these fill in.</p>`}</div>
    <div class="card"><h2>Inference</h2>
      <div class="kpis">
        <div class="kpi"><div class="v">${esc(inf.provider)}</div><div class="l">provider</div></div>
        <div class="kpi"><div class="v">${esc(inf.model || "–")}</div><div class="l">model</div></div>
        <div class="kpi"><div class="v">${inf.calls}</div><div class="l">calls (${inf.judge || 0} judge · ${inf.explain || 0} explain)</div></div>
        <div class="kpi"><div class="v">${inf.tokens}</div><div class="l">tokens</div></div>
        <div class="kpi"><div class="v">${inf.avg_latency_ms != null ? inf.avg_latency_ms + " ms" : "–"}</div><div class="l">avg latency</div></div>
        <div class="kpi"><div class="v">${inf.errors}</div><div class="l">errors (fell back)</div></div>
      </div>
      ${inf.provider === "none" ? `<p class="small muted">No LLM in use. Ambiguous stretches fall back to rules, explanations use the template. ${S.cfg.mode === "local" ? "Run with <code>--inference ollama</code> for a local model on 127.0.0.1." : ""}</p>` : ""}</div>
    ${th ? thresholdsCard(th) : ""}
    <div class="card"><h2>/api/health</h2><pre>${esc(JSON.stringify(health, null, 2))}</pre></div>`;
  applyStyles($("#view-system"));
}

boot().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<p class="err card">Couldn't start: ${esc(e.message)}</p>`); });
