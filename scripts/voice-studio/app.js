// Voice Studio (docs/18). Plain JavaScript, no framework, no inline code (the
// page's Content-Security-Policy forbids it). Every piece of user data is put
// into the page with textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, String(v));
  }
  for (const c of kids.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const num = (x, d = 1) => (x === null || x === undefined || isNaN(x) ? "—" : Number(x).toFixed(d));
function when(isoText) {
  if (!isoText) return "";
  const d = new Date(String(isoText).replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
  return isNaN(d) ? isoText : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
function setText(id, text, cls) {
  const e = $(id);
  e.textContent = text || "";
  if (cls !== undefined) e.className = cls;
}

const S = {
  session: null, state: null, fish: null, profileId: null, replacing: null,
  gen: null, live: null, blobs: new Map(), cmp: { A: null, B: null }, opening: null,
};

// ------------------------------------------------------------------ API
class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
}

async function openSession() {
  if (S.opening) return S.opening;
  S.opening = (async () => {
    const r = await fetch("/api/voice/session", { method: "POST", headers: { "X-Newton-Control": "1" } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(j.error || ("HTTP " + r.status), r.status, j);
    S.session = j.session;
  })();
  try { await S.opening; } finally { S.opening = null; }
}

async function api(method, path, opts = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!S.session) await openSession();
    const headers = { "X-Newton-Control": "1", "X-Voice-Session": S.session };
    let body;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    } else if (opts.body !== undefined) {
      headers["Content-Type"] = opts.ctype || "application/octet-stream";
      body = opts.body;
    }
    let r;
    try {
      r = await fetch(path, { method, headers, body, cache: "no-store" });
    } catch (e) {
      throw new ApiError("Newton AI Control is not answering. Is it running?", 0);
    }
    if (r.status === 401 && attempt === 0) { S.session = null; continue; }
    if (opts.raw && r.ok) return r;
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(j.error || ("HTTP " + r.status), r.status, j);
    return j;
  }
  throw new ApiError("could not open a Voice Studio session", 401);
}

async function blobUrl(key, path) {
  if (S.blobs.has(key)) return S.blobs.get(key);
  const r = await api("GET", path, { raw: true });
  const url = URL.createObjectURL(await r.blob());
  S.blobs.set(key, url);
  return url;
}

function dropBlob(key) {
  const u = S.blobs.get(key);
  if (u) { URL.revokeObjectURL(u); S.blobs.delete(key); }
}

function banner(id, text, cls) {
  const b = $(id);
  b.textContent = text || "";
  b.className = "banner" + (text ? " " + cls : "");
}

// ------------------------------------------------------- Fish (9.1)
const DOT = { Ready: "on", Busy: "busy", Starting: "busy", Stopping: "busy", Recovering: "busy",
              Stopped: "off", Failed: "err", Unknown: "warn", Unavailable: "err" };

function modelName(f, id) {
  return (f.model_names && f.model_names[id]) || id || "an unidentified model";
}

function renderFish(f) {
  S.fish = f;
  const state = f.state || "Unknown";
  $("fish-state").textContent = state;
  $("fish-dot").className = "dot " + (DOT[state] || "warn");
  if (f.display_name) $("fish-name").textContent = f.display_name + " · " + (f.variant || "");
  const m = f.memory || {};
  const op = f.operation;
  let detail = "";
  if (!f.available) detail = f.error || "Voice Studio is unavailable.";
  else if (state === "Ready") detail = "Ready to generate speech" + (f.pid ? " · pid " + f.pid : "") +
    (f.load && f.load.ready_s ? " · loaded in " + num(f.load.ready_s, 0) + " s" : "");
  else if (state === "Busy") {
    const p = (f.current_job && f.current_job.progress) || {};
    detail = "Generating speech" + (p.chunks ? " · chunk " + (p.chunk || 1) + " of " + p.chunks : "") + "…";
  } else if (state === "Starting") {
    const since = op && op.started_at ? Math.max(0, Date.now() / 1000 - op.started_at) : null;
    detail = "Loading the model into memory (about a minute)…" + (since !== null ? " " + num(since, 0) + " s" : "");
  } else if (state === "Stopping") detail = op && op.cancelling ? "Cancelling the start and unloading…" : "Unloading the model…";
  else if (state === "Recovering") detail = "Fish stopped unexpectedly; restarting it…";
  else if (state === "Failed") detail = "Fish is not working right now. " + ((f.last_operation && f.last_operation.message) || "Try Restart.");
  else if (state === "Stopped") detail = "Not loaded, so it uses no memory. Start it to generate speech.";
  else detail = f.error || "Status unknown.";
  setText("fish-detail", detail);

  const mach = m.machine || {};
  const memParts = [];
  if (m.mlx_active_gib !== undefined && m.mlx_active_gib !== null && ["Ready", "Busy"].includes(state)) {
    memParts.push("model " + num(m.mlx_active_gib) + " GiB" + (m.mlx_peak_gib ? " (peak " + num(m.mlx_peak_gib) + ")" : ""));
  }
  if (mach.free_for_models_gib !== undefined && mach.free_for_models_gib !== null) memParts.push(num(mach.free_for_models_gib, 0) + " GiB free for models");
  if (mach.pressure) memParts.push("pressure " + mach.pressure);
  setText("fish-mem", memParts.join(" · ") || "—", "val" + (mach.pressure && mach.pressure !== "normal" ? " amber" : ""));

  const qwen = f.qwen || [];
  let compat = "No language model is running.";
  let cls = "val";
  const blocked = qwen.filter((q) => q.rule !== "conditional");
  if (blocked.length) {
    compat = blocked.map((q) => modelName(f, q.model) + " is " + q.phase).join("; ") + " — Fish can't run at the same time.";
    cls = "val red";
  } else if (qwen.length) {
    compat = qwen.map((q) => modelName(f, q.model) + " is " + q.phase).join("; ") + " — Fish may run alongside it when memory allows.";
  }
  setText("fish-compat", compat, cls);
  banner("banner-conflict", blocked.length
    ? blocked.map((q) => modelName(f, q.model)).join(", ") + " is active. Fish and Flash-Next are never allowed to run together, so Fish can't start until it stops (Newton AI Control → Stop the AI Model)."
    : "", "amber");

  const adm = f.admission;
  if (state === "Stopped" && adm) {
    setText("fish-admission", adm.ok ? "Start check: " + adm.message : "Start would be refused right now: " + adm.message,
            "note" + (adm.ok ? "" : " amber"));
  } else setText("fish-admission", "");

  const inOp = !!op;
  const cancellable = inOp && ["start", "restart"].includes(op.name) && !op.cancelling;
  $("btn-fish-start").disabled = !f.available || inOp || ["Ready", "Busy", "Starting", "Stopping", "Recovering"].includes(state);
  $("btn-fish-stop").disabled = !f.available || (inOp ? !cancellable : state === "Stopped");
  $("btn-fish-stop").title = cancellable ? "Cancels the start and unloads the model" : "";
  $("btn-fish-restart").disabled = !f.available || inOp || ["Stopped", "Starting", "Stopping"].includes(state);

  const lo = f.last_operation;
  const el = $("fish-lastop");
  el.textContent = "";
  if (lo) {
    el.append("Last action — " + lo.name + " at " + lo.finished_at_iso + (lo.cancelled ? " (cancelled): " : lo.ok ? ": " : " FAILED: ") + (lo.message || ""));
    if (!lo.ok && lo.log) el.append(h("span", { class: "logpath", text: "Engine log: " + lo.log }));
    el.className = "lastop" + (lo.ok ? "" : " err");
  }
  renderGenerateButton();
}

async function pollFish() {
  let next = 3000;
  try {
    const r = await fetch("/api/fish/status", { cache: "no-store" });
    const j = await r.json();
    renderFish(j.fish || {});
    if (j.fish && j.fish.operation) next = 1000;
  } catch (e) {
    renderFish({ available: false, state: "Unknown", error: "Newton AI Control is not answering — retrying…" });
  }
  setTimeout(pollFish, next);
}

async function fishOp(verb) {
  try {
    const r = await fetch("/api/fish/" + verb, { method: "POST", headers: { "X-Newton-Control": "1" } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) setText("fish-lastop", j.error || ("HTTP " + r.status), "lastop err");
  } catch (e) {
    setText("fish-lastop", "Newton AI Control is not answering.", "lastop err");
  }
  setTimeout(async () => {
    try { renderFish((await (await fetch("/api/fish/status", { cache: "no-store" })).json()).fish || {}); } catch (e) { /* the poll retries */ }
  }, 300);
}

// --------------------------------------------------------- state
function profile() {
  const ps = (S.state && S.state.profiles) || [];
  return ps.find((p) => p.id === S.profileId) || null;
}

function currentRefs(p) {
  return ((p && p.references) || []).filter((r) => !r.replaced_by);
}

async function loadState() {
  const j = await api("GET", "/api/voice/state");
  S.state = j;
  const ps = j.profiles || [];
  if (!ps.find((p) => p.id === S.profileId)) {
    let saved = null;
    try { saved = sessionStorage.getItem("voice.profile"); } catch (e) { /* storage unavailable */ }
    S.profileId = (ps.find((p) => p.id === saved) || ps[0] || {}).id || null;
  }
  renderAll();
}

function renderAll() {
  const st = S.state;
  if (!st) return;
  const m = st.model || {};
  setText("license-line", (m.license && m.license.notice ? m.license.notice + " " : "") + ((m.license && m.license.attribution) || "Built with Fish Audio") + ".");
  setText("store-line", st.store ? "Stored in " + st.store.path + (st.store.bytes !== null ? " (" + num(st.store.bytes / 1048576, 1) + " MiB)." : ".") : "");
  setText("gen-max", String((m.limits && m.limits.max_text_chars) || 2000));
  $("gen-text").maxLength = (m.limits && m.limits.max_text_chars) || 2000;
  renderProfiles();
  renderRefs();
  renderGenVoice();
  renderTakes();
}

// ------------------------------------------------------ profiles (9.5)
function renderProfiles() {
  const ps = S.state.profiles || [];
  const sel = $("profile-select");
  sel.textContent = "";
  for (const p of ps) sel.append(h("option", { value: p.id, text: p.name }));
  sel.value = S.profileId || "";
  sel.classList.toggle("hidden", !ps.length);
  $("profile-empty").classList.toggle("hidden", ps.length > 0);
  $("profile-actions").classList.toggle("hidden", !ps.length);
  const p = profile();
  if (p) {
    const c = p.consent || {};
    setText("profile-info", (c.basis === "own_voice" ? "Your own voice" : "Used with documented permission" + (c.note ? " (" + c.note + ")" : "")) +
      " · consent confirmed " + when(c.attested_at) + " · " + currentRefs(p).length + " recording(s)");
  } else setText("profile-info", "");
  if (!ps.length) showProfileForm(true);
}

function showProfileForm(show) {
  $("profile-form").classList.toggle("hidden", !show);
  if (show) { $("pf-error").textContent = ""; $("pf-name").focus(); }
}

async function createProfile(ev) {
  ev.preventDefault();
  const basis = document.querySelector("input[name=pf-basis]:checked").value;
  try {
    const j = await api("POST", "/api/voice/profiles", { json: {
      name: $("pf-name").value, consent: { basis, attested: $("pf-attest").checked, note: $("pf-note").value } } });
    S.profileId = j.profile.id;
    rememberProfile();
    $("profile-form").reset();
    $("pf-note-wrap").classList.add("hidden");
    showProfileForm(false);
    await loadState();
  } catch (e) { $("pf-error").textContent = e.message; }
}

async function renameProfile(ev) {
  ev.preventDefault();
  try {
    await api("PATCH", "/api/voice/profiles/" + S.profileId, { json: { name: $("rn-name").value } });
    $("rename-form").classList.add("hidden");
    await loadState();
  } catch (e) { $("rn-error").textContent = e.message; }
}

async function deleteProfile() {
  const p = profile();
  if (!p) return;
  const n = currentRefs(p).length;
  if (!confirm("Delete the voice profile “" + p.name + "” and its " + n + " recording(s)? Their audio files are removed from this Mac. Takes already generated are kept until you delete them.")) return;
  try {
    await api("DELETE", "/api/voice/profiles/" + p.id);
    S.profileId = null;
    await loadState();
  } catch (e) { alert(e.message); }
}

function rememberProfile() {
  try { sessionStorage.setItem("voice.profile", S.profileId || ""); } catch (e) { /* optional */ }
}

// ----------------------------------------------------- recordings (9.2-9.6)
function refFacts(r) {
  const a = r.analysis || {}, f = (r.original && r.original.format) || {};
  const parts = [num(r.model_input && r.model_input.seconds, 1) + " s of speech"];
  if (a.peak_dbfs !== undefined) parts.push("peak " + num(a.peak_dbfs, 1) + " dBFS");
  if (a.noise_floor_dbfs !== undefined) parts.push("noise " + num(a.noise_floor_dbfs, 0) + " dBFS");
  if (a.snr_db !== undefined) parts.push("speech/noise " + num(a.snr_db, 0) + " dB");
  parts.push((f.codec || "?") + ", " + (f.sample_rate ? num(f.sample_rate / 1000, 1) + " kHz" : "?") + ", " + (f.channels || "?") + " ch");
  parts.push(r.source === "recording" ? "recorded here" : "uploaded" + (r.filename ? " (" + r.filename + ")" : ""));
  parts.push(when(r.created_at));
  return parts.join(" · ");
}

function refNode(r, p, older) {
  const isDefault = p.active_reference_id === r.id;
  const audio = h("audio", { controls: true, class: "hidden" });
  const status = h("span", { class: "note" });
  const ta = h("textarea", { rows: 2, maxlength: S.state.max_transcript_chars || 1000,
    placeholder: "Type exactly what is said in this recording, word for word." });
  ta.value = r.transcript || "";
  const play = (kind) => async () => {
    try {
      audio.src = await blobUrl("ref:" + r.id + ":" + kind, "/api/voice/references/" + r.id + "/audio?kind=" + kind);
      audio.classList.remove("hidden");
      audio.play().catch(() => {});
    } catch (e) { status.textContent = e.message; }
  };
  const saveT = async () => {
    try {
      await api("PATCH", "/api/voice/references/" + r.id, { json: { transcript: ta.value } });
      status.textContent = "Transcript saved.";
      await loadState();
    } catch (e) { status.textContent = e.message; status.className = "note red"; }
  };
  const renameRef = async () => {
    const v = prompt("Label for this recording:", r.label);
    if (v === null) return;
    try { await api("PATCH", "/api/voice/references/" + r.id, { json: { label: v } }); await loadState(); }
    catch (e) { alert(e.message); }
  };
  const del = async () => {
    if (!confirm("Delete the recording “" + r.label + "”? Its original and model-ready audio are removed from this Mac. Takes made from it are kept.")) return;
    try {
      await api("DELETE", "/api/voice/references/" + r.id);
      dropBlob("ref:" + r.id + ":original"); dropBlob("ref:" + r.id + ":model");
      await loadState();
    } catch (e) { alert(e.message); }
  };
  const makeDefault = async () => {
    try { await api("PATCH", "/api/voice/profiles/" + p.id, { json: { active_reference_id: r.id } }); await loadState(); }
    catch (e) { alert(e.message); }
  };
  const replace = () => {
    S.replacing = r.id;
    setText("replacing-label", r.label);
    $("replacing").classList.remove("hidden");
    setText("add-ref-title", "Replace a recording");
    $("ref-label").value = r.label;
    $("add-ref").scrollIntoView({ behavior: "smooth", block: "center" });
  };
  const badges = [];
  if (isDefault && !older) badges.push(h("span", { class: "badge default", text: "default" }));
  if (!r.transcript) badges.push(h("span", { class: "badge red", text: "needs a transcript" }));
  else if (r.transcript_needs_review) badges.push(h("span", { class: "badge warn", text: "check the transcript" }));
  if (older) badges.push(h("span", { class: "badge", text: "replaced" }));
  return h("div", { class: "ref", id: "ref-" + r.id },
    h("div", { class: "ref-head" },
      h("div", { class: "ref-title" }, r.label, ...badges),
      h("div", { class: "take-tools" },
        h("button", { class: "small", onclick: play("original"), text: "Play" }),
        h("button", { class: "small", onclick: play("model"), title: "The 44.1 kHz mono copy the model hears", text: "Play model input" }),
        older ? null : h("button", { class: "small", onclick: makeDefault, disabled: isDefault, text: "Make default" }),
        older ? null : h("button", { class: "small", onclick: replace, text: "Replace…" }),
        h("button", { class: "small", onclick: renameRef, text: "Rename" }),
        h("button", { class: "small danger", onclick: del, text: "Delete" }))),
    h("div", { class: "facts", text: refFacts(r) }),
    (r.warnings || []).length ? h("ul", { class: "warns" }, (r.warnings || []).map((w) => h("li", { text: w }))) : null,
    audio,
    h("div", { class: "transcript" }, ta,
      h("div", { class: "btnrow tight" },
        h("button", { class: "small primary", onclick: saveT, text: "Save transcript" }), status)));
}

function renderRefs() {
  const p = profile();
  const list = $("refs-list");
  list.textContent = "";
  $("card-refs").classList.toggle("hidden", !p);
  if (!p) return;
  const cur = currentRefs(p);
  const old = (p.references || []).filter((r) => r.replaced_by);
  setText("refs-count", cur.length ? cur.length + " recording(s)" : "");
  if (!cur.length) list.append(h("div", { class: "detail", text: "No recordings yet. Record yourself below, or upload a recording you already have." }));
  for (const r of cur) list.append(refNode(r, p, false));
  $("refs-older").classList.toggle("hidden", !old.length);
  setText("refs-older-sum", "Earlier versions (" + old.length + ")");
  const ol = $("refs-older-list");
  ol.textContent = "";
  for (const r of old) ol.append(refNode(r, p, true));
}

function clearReplacing() {
  S.replacing = null;
  $("replacing").classList.add("hidden");
  setText("add-ref-title", "Add a recording");
}

async function addReference(body, ctype, source, filename) {
  const p = profile();
  setText("add-ref-error", "");
  setText("add-ref-ok", "");
  if (!p) { setText("add-ref-error", "Create a voice profile first."); return null; }
  const q = new URLSearchParams({ source, label: $("ref-label").value });
  if (filename) q.set("filename", filename);
  if (S.replacing) q.set("replaces", S.replacing);
  setText("add-ref-ok", "Checking and preparing the audio…");
  try {
    const j = await api("POST", "/api/voice/profiles/" + p.id + "/references?" + q.toString(), { body, ctype });
    const r = j.reference;
    clearReplacing();
    $("ref-label").value = "";
    setText("add-ref-ok", "Saved “" + r.label + "” (" + num(r.model_input.seconds, 1) + " s of speech). " +
      (r.transcript ? "Check that the transcript still matches." : "Now type exactly what is said in it, in its transcript box above."));
    await loadState();
    const node = $("ref-" + r.id);
    if (node) { node.scrollIntoView({ behavior: "smooth", block: "center" }); const t = node.querySelector("textarea"); if (t) t.focus(); }
    return r;
  } catch (e) {
    setText("add-ref-ok", "");
    setText("add-ref-error", e.message);
    return null;
  }
}

// ---- browser recording
const REC = { stream: null, ctx: null, node: null, src: null, chunks: [], rate: 48000, t0: 0,
              timer: null, blob: null, url: null, on: false, peak: 0, maxPeak: 0, ready: false };
const REC_MAX_S = 60;

function micMessage(e) {
  const n = e && e.name;
  return ({
    NotAllowedError: "Microphone access was blocked. Allow it for this page in the browser's site settings (and for the browser in System Settings › Privacy & Security › Microphone), then try again.",
    NotFoundError: "No microphone was found. Connect one and try again.",
    NotReadableError: "The microphone is busy in another app or could not be opened.",
    OverconstrainedError: "That microphone is no longer available; choose another one.",
    SecurityError: "The browser refused microphone access on this page.",
  })[n] || ("The microphone could not be opened: " + ((e && e.message) || n || "unknown error"));
}

function micConstraints() {
  const c = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 };
  const id = $("mic-select").value;
  if (id) c.deviceId = { exact: id };
  return c;
}

async function allowMic() {
  setText("add-ref-error", "");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioWorkletNode) {
    setText("add-ref-error", "This browser can't record here. Open Voice Studio at http://127.0.0.1:8765/voice in a current Safari, Chrome or Firefox.");
    return;
  }
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    s.getTracks().forEach((t) => t.stop());
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    const sel = $("mic-select");
    const keep = sel.value;
    sel.textContent = "";
    devs.forEach((d, i) => sel.append(h("option", { value: d.deviceId, text: d.label || "Microphone " + (i + 1) })));
    if (keep) sel.value = keep;
    sel.classList.toggle("hidden", devs.length < 2);
    REC.ready = true;
    $("btn-rec").disabled = false;
    $("btn-mic").textContent = "Microphone ready";
    $("btn-mic").disabled = true;
    setText("rec-note", "Press Record and read naturally for 8–15 seconds. Speech processing in the browser (echo cancellation, noise suppression, automatic gain) is switched off so the recording is your real voice.");
  } catch (e) { setText("add-ref-error", micMessage(e)); }
}

async function startRec() {
  clearPreview();
  setText("add-ref-error", "");
  REC.chunks = []; REC.maxPeak = 0; REC.peak = 0;
  try {
    REC.stream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    REC.ctx = new AudioContext();
    await REC.ctx.audioWorklet.addModule("/voice/recorder-worklet.js");
    await REC.ctx.resume();
    REC.src = REC.ctx.createMediaStreamSource(REC.stream);
    REC.node = new AudioWorkletNode(REC.ctx, "newton-recorder", { numberOfInputs: 1, numberOfOutputs: 1,
      channelCount: 1, channelCountMode: "explicit", channelInterpretation: "speakers" });
  } catch (e) {
    stopTracks();
    setText("add-ref-error", micMessage(e));
    return;
  }
  REC.node.port.onmessage = (e) => {
    if (e.data.done) return finishRec();
    REC.chunks.push(e.data.samples);
    REC.peak = Math.max(REC.peak * 0.85, e.data.peak);
    REC.maxPeak = Math.max(REC.maxPeak, e.data.peak);
  };
  REC.src.connect(REC.node);
  REC.node.connect(REC.ctx.destination);           // silent output keeps the graph running
  REC.rate = REC.ctx.sampleRate;
  REC.t0 = performance.now();
  REC.on = true;
  $("btn-rec").textContent = "Stop";
  REC.timer = setInterval(() => {
    const s = (performance.now() - REC.t0) / 1000;
    setText("rec-time", num(s, 1) + " s");
    const db = REC.peak > 0 ? 20 * Math.log10(REC.peak) : -90;
    const pct = Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    const bar = $("meter-bar");
    bar.style.width = pct + "%";
    bar.className = "meter-bar" + (db > -1 ? " hot" : db < -35 ? " low" : "");
    setText("rec-level", num(db, 0) + " dBFS");
    if (s >= REC_MAX_S) stopRec();
  }, 100);
}

function stopRec() {
  if (!REC.on) return;
  REC.on = false;
  clearInterval(REC.timer);
  $("btn-rec").disabled = true;
  REC.node.port.postMessage("stop");
}

function stopTracks() {
  try { if (REC.src) REC.src.disconnect(); if (REC.node) REC.node.disconnect(); } catch (e) { /* closing */ }
  if (REC.stream) REC.stream.getTracks().forEach((t) => t.stop());
  if (REC.ctx) REC.ctx.close().catch(() => {});
  REC.stream = REC.ctx = REC.node = REC.src = null;
}

function finishRec() {
  stopTracks();
  $("btn-rec").textContent = "Record";
  $("btn-rec").disabled = false;
  $("meter-bar").style.width = "0%";
  const n = REC.chunks.reduce((a, c) => a + c.length, 0);
  const secs = n / REC.rate;
  if (secs < 1) { setText("add-ref-error", "That was too short. Press Record and speak for 8–15 seconds."); return; }
  const x = new Float32Array(n);
  let o = 0;
  for (const c of REC.chunks) { x.set(c, o); o += c.length; }
  REC.chunks = [];
  REC.blob = encodeWav24(x, REC.rate);
  REC.url = URL.createObjectURL(REC.blob);
  $("rec-audio").src = REC.url;
  $("rec-preview").classList.remove("hidden");
  const peakDb = REC.maxPeak > 0 ? 20 * Math.log10(REC.maxPeak) : -90;
  const notes = [num(secs, 1) + " s recorded, peak " + num(peakDb, 1) + " dBFS."];
  if (peakDb > -0.5) notes.push("It may have clipped (too loud): consider moving back and recording again.");
  else if (peakDb < -30) notes.push("It is very quiet: move closer or raise the input level.");
  if (secs < 4) notes.push("Short: 8–15 seconds of speech usually clones better.");
  notes.push("Listen back, then save it or record again.");
  setText("rec-note", notes.join(" "));
}

function clearPreview() {
  if (REC.url) URL.revokeObjectURL(REC.url);
  REC.url = REC.blob = null;
  $("rec-audio").removeAttribute("src");
  $("rec-preview").classList.add("hidden");
}

function encodeWav24(x, rate) {
  // 24-bit PCM mono WAV: lossless enough for a voice reference, plays everywhere.
  const n = x.length - (x.length & 1);
  const bytes = n * 3;
  const buf = new ArrayBuffer(44 + bytes);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + bytes, true); w(8, "WAVE");
  w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 3, true); v.setUint16(32, 3, true); v.setUint16(34, 24, true);
  w(36, "data"); v.setUint32(40, bytes, true);
  let o = 44;
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, x[i]));
    let q = Math.round(s * 8388607);
    if (q < 0) q += 0x1000000;
    v.setUint8(o, q & 255); v.setUint8(o + 1, (q >> 8) & 255); v.setUint8(o + 2, (q >> 16) & 255);
    o += 3;
  }
  return new Blob([buf], { type: "audio/wav" });
}

async function saveRecording() {
  if (!REC.blob) return;
  $("btn-rec-save").disabled = true;
  const r = await addReference(REC.blob, "audio/wav", "recording", "browser-recording.wav");
  $("btn-rec-save").disabled = false;
  if (r) { clearPreview(); setText("rec-note", "Saved. Record another one any time; several recordings let you compare."); }
}

async function uploadFile() {
  const f = $("file-input").files[0];
  if (!f) return;
  const max = (S.state && S.state.max_upload_bytes) || 33554432;
  if (f.size > max) { setText("add-ref-error", "That file is " + num(f.size / 1048576, 1) + " MiB; the limit is " + num(max / 1048576, 0) + " MiB."); return; }
  $("btn-upload").disabled = true;
  const r = await addReference(f, f.type || "application/octet-stream", "upload", f.name);
  if (r) $("file-input").value = "";
  $("btn-upload").disabled = !$("file-input").files.length;
}

function showTab(which) {
  $("tab-record").classList.toggle("active", which === "record");
  $("tab-upload").classList.toggle("active", which === "upload");
  $("pane-record").classList.toggle("hidden", which !== "record");
  $("pane-upload").classList.toggle("hidden", which !== "upload");
}

// ------------------------------------------------------ generation (9.7, 9.8)
const PRESETS = [
  ["Conversational", "Hi, it's me. I'm running about ten minutes late, but I'm on my way. Go ahead and start without me, and I'll catch up when I get there."],
  ["A question", "Did you get a chance to look at the proposal? I'd like to hear what you think before Friday, especially about the timeline."],
  ["Longer explanation", "Here's how it works. First, the system checks how much memory is free. If there is enough room, it loads the model and confirms that it is the right one. If there isn't, it refuses to start and tells you why, instead of slowing everything else down. That way nothing gets pushed into swap, and you always know what is running."],
  ["Names and numbers", "Your order number is 48213, and it should arrive on Tuesday, October 21st, between 9 and 11 a.m. If you have any questions, ask for Siobhan or Mateus at extension 304."],
  ["Natural pauses", "Well... I wasn't expecting that. Give me a second. Okay, here's what we'll do: we'll wait until tomorrow, and then, if nothing has changed, we'll call them."],
  ["Neutral narration", "The river runs north for about forty miles before it turns toward the coast. In spring the water rises quickly, and the old bridge near the mill is often closed for several days."],
  ["Expressive (cues)", "[sighs] I really thought we'd make it this time. [laughs] Well, at least nobody can say we didn't try."],
];
const SETTING_KEYS = ["temperature", "top_p", "top_k", "max_frames_per_chunk", "chunk_bytes"];

function setupSettings() {
  const sch = (S.state && S.state.settings) || {};
  for (const k of SETTING_KEYS) {
    const s = sch[k];
    if (!s) continue;
    for (const id of ["s-" + k, "n-" + k]) {
      const e = $(id);
      if (!e) continue;
      e.min = s.min; e.max = s.max;
      if (e.value === "" || e.dataset.init !== "1") { e.value = s.default; e.dataset.init = "1"; }
    }
    const rng = $("s-" + k), box = $("n-" + k);
    if (rng && !rng.dataset.bound) {
      rng.dataset.bound = "1";
      rng.addEventListener("input", () => { box.value = rng.value; });
      box.addEventListener("input", () => { rng.value = box.value; });
    }
  }
  $("n-seed").max = (S.state && S.state.max_seed) || 2147483647;
}

function resetDefaults() {
  const sch = (S.state && S.state.settings) || {};
  for (const k of SETTING_KEYS) {
    if (!sch[k]) continue;
    for (const id of ["s-" + k, "n-" + k]) if ($(id)) $(id).value = sch[k].default;
  }
  $("n-single_chunk").checked = false;
  $("n-seed").value = "";
}

function renderGenVoice() {
  const p = profile();
  setText("gen-voice", p ? "Voice: " + p.name : "");
  const sel = $("gen-ref");
  const keep = sel.value;
  sel.textContent = "";
  const refs = currentRefs(p);
  for (const r of refs) {
    sel.append(h("option", { value: r.id, disabled: !r.transcript,
      text: r.label + " (" + num(r.model_input.seconds, 1) + " s)" + (r.transcript ? "" : " — needs a transcript") }));
  }
  if (!refs.length) sel.append(h("option", { value: "", text: p ? "Add a recording first" : "Create a voice profile first" }));
  const def = p && refs.find((r) => r.id === p.active_reference_id && r.transcript);
  sel.value = refs.find((r) => r.id === keep && r.transcript) ? keep : def ? def.id : (refs.find((r) => r.transcript) || {}).id || "";
  const ml = $("gen-multi-list");
  const checked = new Set([...ml.querySelectorAll("input:checked")].map((i) => i.value));
  ml.textContent = "";
  for (const r of refs.filter((x) => x.transcript)) {
    ml.append(h("label", { class: "opt small-opt" },
      h("input", { type: "checkbox", value: r.id, checked: checked.has(r.id) || (!checked.size && r.id === sel.value) }), r.label));
  }
  setupSettings();
  renderGenerateButton();
}

function renderGenerateButton() {
  const f = S.fish || {};
  const ready = ["Ready", "Busy"].includes(f.state);
  const running = S.gen && !S.gen.finished_at;
  $("btn-generate").disabled = !ready || !!running || !profile();
  $("btn-generate").title = ready ? "" : "Start Fish first (top of the page).";
  $("btn-gen-cancel").disabled = !running;
  $("n-progressive").disabled = $("n-count").value !== "1";
}

function selectedRefIds() {
  if ($("gen-multi").checked) {
    return [...$("gen-multi-list").querySelectorAll("input:checked")].map((i) => i.value);
  }
  return $("gen-ref").value ? [$("gen-ref").value] : [];
}

function readSettings() {
  const out = {};
  for (const k of SETTING_KEYS) out[k] = Number($("n-" + k).value);
  out.single_chunk = $("n-single_chunk").checked;
  const seed = $("n-seed").value.trim();
  out.seed = seed === "" ? null : Number(seed);
  return out;
}

async function generate(body) {
  setText("gen-error", "");
  const p = profile();
  if (!body) {
    if (!p) { setText("gen-error", "Create a voice profile first."); return; }
    const ids = selectedRefIds();
    if (!ids.length) { setText("gen-error", "Choose a recording that has a transcript."); return; }
    if (ids.length > 3) { setText("gen-error", "Choose at most three recordings."); return; }
    body = { profile_id: p.id, reference_ids: ids, text: $("gen-text").value, settings: readSettings(),
             count: Number($("n-count").value), progressive: $("n-progressive").checked && $("n-count").value === "1" };
  }
  if (!body.text || !body.text.trim()) { setText("gen-error", "Type something for the voice to say."); return; }
  const t0 = performance.now();
  if (S.live) { S.live.stop(); S.live = null; }
  let live = null;
  if (body.progressive) live = new LivePlayer(t0);                 // created inside the click: audio may start
  try {
    const j = await api("POST", "/api/voice/generate", { json: body });
    S.gen = j.generation;
    S.gen.t0 = t0;
    if (live) { live.gid = S.gen.id; S.live = live; live.run(); }
    renderGenerateButton();
    pollGeneration(S.gen.id);
  } catch (e) {
    if (live) live.stop();
    setText("gen-error", e.message);
    if (e.data && e.data.code === "fish_not_ready") $("card-model").scrollIntoView({ behavior: "smooth" });
  }
}

const GEN_TEXT = { preparing: "Preparing the recording…", submitting: "Sending to the model…",
                   queued: "Waiting for the model…", running: "Generating" };

async function pollGeneration(gid) {
  while (S.gen && S.gen.id === gid) {
    let g;
    try { g = (await api("GET", "/api/voice/generations/" + gid)).generation; }
    catch (e) { setText("gen-error", e.message); break; }
    g.t0 = S.gen.t0;
    S.gen = g;
    renderGenStatus(g);
    if (g.finished_at) break;
    await sleep(400);
  }
  renderGenerateButton();
  try { await loadState(); } catch (e) { setText("gen-error", e.message); }
  const g = S.gen;
  if (g && g.take_ids && g.take_ids.length) {
    const first = $("take-" + g.take_ids[0]);
    if (first) first.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function renderGenStatus(g) {
  const box = $("gen-status");
  box.textContent = "";
  const p = g.progress || {};
  let line;
  if (g.finished_at) {
    line = { done: "Done: " + g.take_ids.length + " take(s) in " + num(g.elapsed_s, 1) + " s.",
             partial: "Finished with problems: " + g.take_ids.length + " of " + g.count + " take(s) made.",
             cancelled: "Cancelled.", failed: "Generation failed." }[g.state] || g.state;
  } else {
    line = (GEN_TEXT[g.state] || g.state) + (g.count > 1 ? " take " + g.index + " of " + g.count : "") +
      (g.state === "running" && p.chunks ? " · chunk " + (p.chunk || 1) + " of " + p.chunks : "") +
      (g.state === "running" && p.audio_seconds ? " · " + num(p.audio_seconds, 1) + " s of audio" : "") +
      " · " + num(g.elapsed_s, 1) + " s";
  }
  box.append(h("div", { text: line }));
  if (S.live && S.live.firstMs !== null && S.live.gid === g.id) {
    box.append(h("div", { class: "note", text: "First audio played " + num(S.live.firstMs / 1000, 2) + " s after you pressed Generate." }));
  }
  if (!g.finished_at && p.chunks) {
    const frac = Math.min(1, ((p.chunk || 1) - 1 + (p.frames ? 0.5 : 0)) / p.chunks);
    const fill = h("div");
    fill.style.width = Math.round(frac * 100) + "%";          // CSSOM: allowed by the CSP
    box.append(h("div", { class: "bar" }, fill));
  }
  const errs = (g.errors || []).concat(g.error ? [g.error] : []);
  setText("gen-error", errs.join("\n"));
}

async function cancelGeneration() {
  if (!S.gen) return;
  try { await api("POST", "/api/voice/generations/" + S.gen.id + "/cancel"); } catch (e) { setText("gen-error", e.message); }
  if (S.live) S.live.stop();
}

// Progressive playback: PCM arrives as the model decodes it and is scheduled
// back to back with the Web Audio API.
class LivePlayer {
  constructor(t0) {
    this.t0 = t0; this.gid = null; this.from = 0; this.next = 0; this.firstMs = null;
    this.stopped = false; this.sources = [];
    try { this.ctx = new AudioContext(); } catch (e) { this.ctx = null; }
  }
  async run() {
    if (!this.ctx) return;
    while (!this.stopped) {
      let r;
      try { r = await api("GET", "/api/voice/generations/" + this.gid + "/pcm?from=" + this.from, { raw: true }); }
      catch (e) { break; }
      const state = r.headers.get("X-Job-State") || "";
      const rate = parseInt(r.headers.get("X-Sample-Rate") || "44100", 10) || 44100;
      const buf = await r.arrayBuffer();
      const n = buf.byteLength >> 1;
      if (n > 0) { this.schedule(buf, n, rate); this.from += n; }
      if (["done", "failed", "cancelled"].includes(state) && n === 0) break;
      await sleep(n > 0 ? 120 : 200);
    }
  }
  schedule(buf, n, rate) {
    if (this.stopped) return;
    const dv = new DataView(buf);
    const ab = this.ctx.createBuffer(1, n, rate);
    const ch = ab.getChannelData(0);
    for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
    const src = this.ctx.createBufferSource();
    src.buffer = ab;
    src.connect(this.ctx.destination);
    const at = Math.max(this.ctx.currentTime + 0.05, this.next);
    src.start(at);
    this.next = at + ab.duration;
    this.sources.push(src);
    if (this.firstMs === null) this.firstMs = performance.now() - this.t0 + 50;
  }
  stop() {
    this.stopped = true;
    for (const s of this.sources) { try { s.stop(); } catch (e) { /* already ended */ } }
    if (this.ctx) this.ctx.close().catch(() => {});
  }
}

function insertTag(tag) {
  const t = $("gen-text");
  const a = t.selectionStart, b = t.selectionEnd;
  const before = t.value.slice(0, a), after = t.value.slice(b);
  const pad = before && !/\s$/.test(before) ? " " : "";
  t.value = before + pad + tag + " " + after;
  t.focus();
  t.selectionStart = t.selectionEnd = (before + pad + tag + " ").length;
  $("gen-count").textContent = t.value.length;
}

// ------------------------------------------------------------ takes (9.9)
function takeLine(t) {
  const r = t.result || {};
  const parts = [num(r.audio_seconds, 1) + " s audio"];
  if (r.synthesis_s) parts.push("made in " + num(r.synthesis_s, 1) + " s (" + num(r.realtime_factor, 2) + "× real time)");
  if (r.first_audio_s) parts.push("first audio " + num(r.first_audio_s, 2) + " s");
  if (t.seed !== null && t.seed !== undefined) parts.push("seed " + t.seed);
  return parts.join(" · ");
}

function details(t) {
  const r = t.result || {}, m = t.model || {}, s = t.settings || {}, tm = t.timing || {};
  const rows = [
    ["Text", t.text],
    ["Voice", (t.profile && t.profile.name) || ""],
    ["Recording(s)", (t.references || []).map((x) => "“" + x.label + "” " + num(x.seconds, 1) + " s · transcript: " + x.transcript + " · audio sha256 " + String(x.model_input_sha256).slice(0, 12) + "…").join("\n")],
    ["Settings", "temperature " + s.temperature + " · top-p " + s.top_p + " · top-k " + s.top_k + " · max frames/chunk " + s.max_frames_per_chunk + " · chunk " + s.chunk_bytes + " bytes" + (s.single_chunk ? " · single chunk" : "")],
    ["Seed", (t.seed !== undefined ? t.seed : "—") + (t.seed_requested === null ? " (chosen at random)" : " (requested)")],
    ["Model", (m.display_name || "") + " · " + (m.variant || "") + " · " + (m.hf_repo || "") + " @ " + String(m.hf_revision || "").slice(0, 12)],
    ["Runtime", (m.adapter || "") + " · mlx-audio " + (m.mlx_audio || "?") + " @ " + String(m.runtime_commit || "").slice(0, 12) + " · MLX " + (m.mlx || "?") + " · engine start #" + (m.generation || "?")],
    ["Timing", "queued " + num(tm.queue_s, 2) + " s · engine " + num(tm.engine_s, 2) + " s · first frame " + num(r.first_frame_s, 2) + " s · first audio " + num(r.first_audio_s, 2) + " s" + (r.progressive ? " (progressive)" : "") + " · peak model memory " + num(r.peak_gib, 1) + " GiB"],
    ["Chunks", (r.chunks || []).map((c) => "#" + (c.index + 1) + ": " + c.frames + " frames, " + num(c.audio_seconds, 1) + " s, " + (c.eos ? "ended by itself" : "cut at the frame limit") + ", " + num(c.frames_per_s, 1) + " frames/s").join("\n")],
    ["Made", when(t.created_at) + (t.regenerated_from ? " · same seed as an earlier take" : "")],
    ["Audio", (t.audio && t.audio.format) || ""],
  ];
  return h("dl", { class: "kv" }, rows.map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
}

function takeNode(t) {
  const audio = h("audio", { controls: true, class: "hidden" });
  const status = h("span", { class: "note" });
  const det = h("div", { class: "hidden" }, details(t));
  const key = "take:" + t.id;
  const load = async () => {
    audio.src = await blobUrl(key, "/api/voice/takes/" + t.id + "/audio");
    audio.classList.remove("hidden");
  };
  const play = async () => { try { await load(); audio.play().catch(() => {}); } catch (e) { status.textContent = e.message; } };
  const download = async () => {
    try {
      const url = await blobUrl(key, "/api/voice/takes/" + t.id + "/audio");
      const a = h("a", { href: url, download: "voice-take-" + t.number + ".wav" });
      document.body.append(a); a.click(); a.remove();
    } catch (e) { status.textContent = e.message; }
  };
  const star = async () => {
    try { await api("PATCH", "/api/voice/takes/" + t.id, { json: { starred: !t.starred } }); await loadState(); }
    catch (e) { status.textContent = e.message; }
  };
  const del = async () => {
    if (!confirm("Delete take #" + t.number + "? Its audio is removed from this Mac.")) return;
    try {
      await api("DELETE", "/api/voice/takes/" + t.id);
      dropBlob(key);
      for (const slot of ["A", "B"]) if (S.cmp[slot] && S.cmp[slot].id === t.id) S.cmp[slot] = null;
      renderCompare();
      await loadState();
    } catch (e) { status.textContent = e.message; }
  };
  const again = () => {
    const refs = (t.references || []).map((r) => r.id);
    generate({ profile_id: t.profile.id, reference_ids: refs, text: t.text,
               settings: Object.assign({}, t.settings, { seed: t.seed }), count: 1,
               progressive: false, regenerated_from: t.id });
    $("card-gen").scrollIntoView({ behavior: "smooth" });
  };
  const reuse = () => {
    $("gen-text").value = t.text;
    $("gen-count").textContent = t.text.length;
    for (const k of SETTING_KEYS) if (t.settings && t.settings[k] !== undefined) { $("n-" + k).value = t.settings[k]; if ($("s-" + k)) $("s-" + k).value = t.settings[k]; }
    $("n-single_chunk").checked = !!(t.settings && t.settings.single_chunk);
    $("card-gen").scrollIntoView({ behavior: "smooth" });
  };
  const note = h("input", { type: "text", class: "take-note", maxlength: 500, placeholder: "Your notes on this take (e.g. sounds most like me)" });
  note.value = t.note || "";
  note.addEventListener("change", async () => {
    try { await api("PATCH", "/api/voice/takes/" + t.id, { json: { note: note.value } }); status.textContent = "Note saved."; }
    catch (e) { status.textContent = e.message; }
  });
  const flags = (t.result && t.result.flags) || [];
  const refLabels = (t.references || []).map((r) => r.label).join(" + ");
  return h("div", { class: "take", id: "take-" + t.id },
    h("div", { class: "take-head" },
      h("div", { class: "take-title" },
        h("button", { class: "small star" + (t.starred ? " on" : ""), onclick: star, title: "Star", text: t.starred ? "★" : "☆" }),
        "#" + t.number,
        h("span", { class: "sub", text: when(t.created_at) + " · " + ((t.profile && t.profile.name) || "") + " · " + refLabels }),
        flags.map((f) => h("span", { class: "badge warn", title: f.message, text: f.kind.replace("_", " ") }))),
      h("span", { class: "sub", text: takeLine(t) })),
    h("div", { class: "take-text", text: t.text.length > 280 ? t.text.slice(0, 280) + "…" : t.text }),
    flags.length ? h("ul", { class: "warns" }, flags.map((f) => h("li", { text: f.message }))) : null,
    audio,
    h("div", { class: "take-tools" },
      h("button", { class: "small primary", onclick: play, text: "Play" }),
      h("button", { class: "small", onclick: download, text: "Download" }),
      h("button", { class: "small" + (S.cmp.A && S.cmp.A.id === t.id ? " on" : ""), onclick: () => setCompare("A", t), text: "A" }),
      h("button", { class: "small" + (S.cmp.B && S.cmp.B.id === t.id ? " on" : ""), onclick: () => setCompare("B", t), text: "B" }),
      h("button", { class: "small", onclick: again, title: "Generate again with exactly the same text, recording, settings and seed", text: "Same seed again" }),
      h("button", { class: "small", onclick: reuse, title: "Copy this take's text and settings into the editor", text: "Edit as new" }),
      h("button", { class: "small", onclick: () => det.classList.toggle("hidden"), text: "Details" }),
      h("button", { class: "small danger", onclick: del, text: "Delete" }), status),
    note, det);
}

function renderTakes() {
  const list = $("takes-list");
  list.textContent = "";
  const mode = $("take-filter").value;
  const takes = ((S.state && S.state.takes) || []).filter((t) =>
    mode === "all" ? true : mode === "starred" ? t.starred : (t.profile && t.profile.id) === S.profileId);
  if (!takes.length) list.append(h("div", { class: "detail", text: mode === "starred" ? "No starred takes." : "No takes yet." }));
  for (const t of takes) list.append(takeNode(t));
}

// ---- compare
function setCompare(slot, t) {
  S.cmp[slot] = S.cmp[slot] && S.cmp[slot].id === t.id ? null : t;
  renderCompare();
  renderTakes();
}

function renderCompare() {
  const any = S.cmp.A || S.cmp.B;
  $("compare").classList.toggle("hidden", !any);
  setText("cmp-a", S.cmp.A ? "#" + S.cmp.A.number : "—");
  setText("cmp-b", S.cmp.B ? "#" + S.cmp.B.number : "—");
  $("cmp-play-a").disabled = !S.cmp.A;
  $("cmp-play-b").disabled = !S.cmp.B;
  $("cmp-switch").disabled = !(S.cmp.A && S.cmp.B);
}

async function cmpAudio(slot) {
  const t = S.cmp[slot];
  const a = $(slot === "A" ? "cmp-audio-a" : "cmp-audio-b");
  if (!t) return null;
  const url = await blobUrl("take:" + t.id, "/api/voice/takes/" + t.id + "/audio");
  if (a.dataset.take !== t.id) { a.src = url; a.dataset.take = t.id; }
  return a;
}

async function cmpPlay(slot, at) {
  const other = $(slot === "A" ? "cmp-audio-b" : "cmp-audio-a");
  other.pause();
  const a = await cmpAudio(slot);
  if (!a) return;
  if (at !== undefined) {
    const go = () => { a.currentTime = Math.min(at, Math.max(0, (a.duration || at) - 0.05)); a.play().catch(() => {}); };
    if (a.readyState >= 1) go(); else a.addEventListener("loadedmetadata", go, { once: true });
  } else { a.currentTime = 0; a.play().catch(() => {}); }
}

async function cmpSwitch() {
  const a = $("cmp-audio-a"), b = $("cmp-audio-b");
  if (!a.paused) return cmpPlay("B", a.currentTime);
  if (!b.paused) return cmpPlay("A", b.currentTime);
  return cmpPlay("A");
}

// ------------------------------------------------------------------ init
function bind() {
  $("btn-fish-start").addEventListener("click", () => fishOp("start"));
  $("btn-fish-stop").addEventListener("click", () => fishOp("stop"));
  $("btn-fish-restart").addEventListener("click", () => fishOp("restart"));
  $("btn-profile-new").addEventListener("click", () => showProfileForm($("profile-form").classList.contains("hidden")));
  $("pf-cancel").addEventListener("click", () => showProfileForm(false));
  $("profile-form").addEventListener("submit", createProfile);
  for (const r of document.querySelectorAll("input[name=pf-basis]")) {
    r.addEventListener("change", () => $("pf-note-wrap").classList.toggle("hidden", r.value !== "documented_permission" || !r.checked));
  }
  $("profile-select").addEventListener("change", (e) => { S.profileId = e.target.value; rememberProfile(); clearReplacing(); renderAll(); });
  $("btn-profile-rename").addEventListener("click", () => {
    const p = profile(); if (!p) return;
    $("rn-name").value = p.name; $("rn-error").textContent = "";
    $("rename-form").classList.remove("hidden"); $("rn-name").focus();
  });
  $("rn-cancel").addEventListener("click", () => $("rename-form").classList.add("hidden"));
  $("rename-form").addEventListener("submit", renameProfile);
  $("btn-profile-delete").addEventListener("click", deleteProfile);
  $("tab-record").addEventListener("click", () => showTab("record"));
  $("tab-upload").addEventListener("click", () => showTab("upload"));
  $("btn-replace-cancel").addEventListener("click", clearReplacing);
  $("btn-mic").addEventListener("click", allowMic);
  $("btn-rec").addEventListener("click", () => (REC.on ? stopRec() : startRec()));
  $("btn-rec-save").addEventListener("click", saveRecording);
  $("btn-rec-redo").addEventListener("click", () => { clearPreview(); startRec(); });
  $("btn-rec-discard").addEventListener("click", () => { clearPreview(); setText("rec-note", "Discarded. Nothing was saved."); });
  $("file-input").addEventListener("change", () => { $("btn-upload").disabled = !$("file-input").files.length; });
  $("btn-upload").addEventListener("click", uploadFile);
  const pre = $("preset");
  PRESETS.forEach(([name], i) => pre.append(h("option", { value: String(i), text: name })));
  pre.addEventListener("change", () => {
    if (pre.value === "") return;
    $("gen-text").value = PRESETS[Number(pre.value)][1];
    $("gen-count").textContent = $("gen-text").value.length;
    pre.value = "";
  });
  for (const b of document.querySelectorAll("button.tag")) b.addEventListener("click", () => insertTag(b.dataset.tag));
  $("gen-text").addEventListener("input", () => { $("gen-count").textContent = $("gen-text").value.length; });
  $("gen-multi").addEventListener("change", () => {
    const on = $("gen-multi").checked;
    $("gen-multi-list").classList.toggle("hidden", !on);
    $("gen-multi-note").classList.toggle("hidden", !on);
    $("gen-ref").disabled = on;
  });
  $("n-count").addEventListener("change", renderGenerateButton);
  $("btn-seed-random").addEventListener("click", () => { $("n-seed").value = ""; });
  $("btn-defaults").addEventListener("click", resetDefaults);
  $("btn-generate").addEventListener("click", () => generate());
  $("btn-gen-cancel").addEventListener("click", cancelGeneration);
  $("take-filter").addEventListener("change", renderTakes);
  $("cmp-play-a").addEventListener("click", () => cmpPlay("A"));
  $("cmp-play-b").addEventListener("click", () => cmpPlay("B"));
  $("cmp-switch").addEventListener("click", cmpSwitch);
  $("cmp-stop").addEventListener("click", () => { $("cmp-audio-a").pause(); $("cmp-audio-b").pause(); });
  $("cmp-clear").addEventListener("click", () => { S.cmp = { A: null, B: null }; $("cmp-audio-a").pause(); $("cmp-audio-b").pause(); renderCompare(); renderTakes(); });
}

async function init() {
  bind();
  pollFish();
  try {
    await openSession();
    await loadState();
    const g = S.state.generation;
    if (g && !g.finished_at) { S.gen = g; pollGeneration(g.id); }
  } catch (e) {
    banner("banner-error", "Voice Studio could not open: " + e.message, "red");
    for (const id of ["card-profiles", "card-refs", "card-gen", "card-takes"]) $(id).classList.add("hidden");
  }
}

init();
