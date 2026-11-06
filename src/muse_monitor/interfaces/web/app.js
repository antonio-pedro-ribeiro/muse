/* Muse Monitor web UI: plain JS, hash routing, talks only to /api/v1.
   Pages answer, in order: is the signal good? what is it doing now? how did it change? what happened?
   Technical detail lives in collapsed "Details" sections. Plugins place their views with ViewSpec.section. */
(function () {
  "use strict";
  const { render, Store, fmt, info, heading, headMap } = window.MuseRenderers;

  // Plain-language explanations for the UI's own elements. Plugin views bring their own (ViewSpec.description).
  const HELP = {
    source: "Where the data comes from: your headband over Bluetooth, your headband through the Mind Monitor phone app, or made-up test signals to try the app.",
    label: "Optional name for this session, shown in the session list and reports (e.g. \"morning meditation\").",
    record: "Save the unmodified sensor data to disk so you can review, replay, re-analyze or export it later. If off, nothing is stored.",
    advanced: "Settings most people never need: options for the chosen source (for the headband, leave the address empty to use the strongest one in range) and which analyzers run.",
    analyzers: "Processing steps that run on the live signal. Each one adds charts. You can re-run any of them later on a recording with \"Analyze\".",
    start: "Connects to the source and starts streaming. Only one live session can run at a time.",
    status: "running: data is streaming. stopped: the session ended normally. failed: it ended because of an error. incomplete: the app quit while recording; run `muse sessions repair` to finish the files.",
    elapsed: "Time since the session started.",
    battery: "Headband battery level as reported by the device.",
    via: "How the data reaches this computer.",
    health: "One chip per plugin in this session. Green: working. Yellow: degraded, e.g. Bluetooth packets lost. Red: stopped after an error; the rest of the session keeps running. \"dropped\" counts chunks an analyzer skipped because it fell behind; the recording itself never drops data.",
    marker: "Adds a labelled timestamp to the recording, e.g. \"eyes closed\" or \"breathing exercise\". Markers show as dashed lines in charts, label parts of the session description, and are kept in exports.",
    calibrate: "A 2-minute personal baseline: keep your eyes open and relaxed for 60 s, then closed for 60 s. A banner tells you when to switch. Band power is then also shown relative to this baseline.",
    stop: "Ends the session and finishes writing the recording.",
    quality: "Check this first. Signal strength shows each electrode where it touches your head: solid green is good contact, half-filled yellow noisy, an empty red ring poor contact, dashed grey no signal. Usable channels is the share with good contact right now. If contact is poor, the other numbers are not meaningful.",
    now: "What the EEG is doing at the moment: how its power is spread over the classic frequency bands (often called brain waves) with their last 2 minutes, and a plain-language description of the last 30 s. Blinks, jaw clenching and movement show up here too, so check Electrode signal and Events. These are measurements, not mental states.",
    trends: "How the measurements change over the last minutes. Cards appear once they have data (for example, the baseline comparison after you calibrate).",
    signal: "The signal itself: the filtered EEG traces and the frequency spectrum.",
    events: "What happened: markers, calibration, blinks, jaw clenching, movement, and all earlier descriptions.",
    sessions: "All recorded sessions, newest first. Click a row to open it.",
    started: "When the session started, in your local time.",
    duration: "How long the session lasted.",
    device: "The headband (or test source) the data came from.",
    usable: "Share of electrode readings with good contact over the session (from the signal-quality check).",
    received: "Share of the expected EEG samples that arrived. Bluetooth or Wi-Fi dropouts lower it; about 98–99% is normal.",
    summaryCol: "The first line of the session description. Run Analyze on older sessions to get one.",
    contact: "Each electrode's share of the session with good contact, on your head seen from above (nose at the top). Green: 80% or more. Yellow: 50–80%. Red ring: under 50%, so that electrode's data is mostly unreliable.",
    replay: "Plays this recording back on the Live page as if it were happening now, at the chosen speed, with the analyzers it was recorded with plus the session description. Markers and calibration from the recording are replayed too. Nothing is saved; the recording is not changed. Only one live session can run at a time.",
    replaying: "This is a recorded session being played back, not live data. Times shown are the original recording times. Adding markers and calibrating are off during a replay.",
    analyze: "Runs the analyzers over the whole recording as fast as possible, saves their results next to the raw data, refreshes the summary and charts on this page, and creates an HTML report. The raw recording is never changed.",
    export: "Download the data: CSV (every stream plus events, zipped) for spreadsheets or your own scripts, or EDF+ for EEG tools such as EDFbrowser, EEGLAB or MNE (markers and artifacts become annotations).",
    summary: "Key numbers and a plain-language description of the session, for the whole recording and for each marked or calibration segment. It describes measurements, not states of mind.",
    timeline: "The recording on one time axis. Pick a segment to zoom every chart to it. Dashed lines are markers.",
    charts: "Charts from the analyzers over the whole session. Run Analyze to create or refresh them.",
    completeness: "Per sensor stream: samples received, share of expected samples that arrived, measured and expected rate, and gaps. Many gaps or a low share point to a weak Bluetooth or Wi-Fi link.",
    pluginReports: "Each analyzer's end-of-session numbers.",
    reports: "HTML summaries created by analyses. Each opens in a new tab and is a single file you can keep or share.",
    versions: "Plugin versions used, so results can be reproduced.",
    plugins: "Plugins provide devices (sources), processing (analyzers) and outputs (sinks). ok: ready. unavailable: a dependency is missing (see the hint). incompatible: built for a newer version of the app.",
    options: "Configuration options with their default values. Change them in a pipeline TOML file or, for sources, under Advanced in the start form.",
  };
  const SOURCES = {
    openmuse: ["Headband (Bluetooth)", "Connects directly to your Muse S Athena. Close the Muse phone app and don't pair the headband in Windows."],
    mindmonitor: ["Via Mind Monitor", "Receives your headband's data from the Mind Monitor phone app over Wi-Fi (OSC, UDP port 5000)."],
    synthetic: ["Test signals", "Made-up EEG with rhythms, blinks and noise, to try the app without a headband."],
  };
  const VIA = { openmuse: "via Bluetooth", mindmonitor: "via Mind Monitor", synthetic: "test signals" };
  const SECTIONS = { quality: "Electrode signal", now: "Brain waves", trends: "Trends", signal: "Signal", events: "Events" };
  const ORDER = ["quality", "now", "trends", "signal", "events"];
  const sectionOf = (v) => v.section || ({ text: "now", events: "events", spectrum: "signal", bars: "now", bands: "now",
    metric: "now", table: "now" }[v.type] || "trends");

  const sub = (text, help, tag = "h2") => heading(tag, text, help);
  function field(text, input, help) {
    const id = input.id || (input.id = `f-${Math.random().toString(36).slice(2, 9)}`);
    return h("div", { class: "field" }, h("span", { class: "field-head" }, h("label", { for: id }, text), help ? info(help, text) : null), input);
  }
  const th = (text, help) => h("th", {}, text, help ? info(help, text) : null);
  const app = document.getElementById("app");
  const API = "/api/v1";
  let cleanup = [];

  // ---------- helpers ----------
  function token() {
    try { return localStorage.getItem("muse-token") || ""; } catch { return ""; }
  }
  function setToken(t) {
    try { localStorage.setItem("muse-token", t); } catch { /* storage unavailable */ }
  }
  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (token()) headers.Authorization = "Bearer " + token();
    if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
    const res = await fetch(API + path, { ...opts, headers });
    if (res.status === 401) { showTokenForm(); throw new Error("authentication required"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { const p = await res.json(); detail = p.detail || p.title || detail; } catch { /* not JSON */ }
      const err = new Error(detail); err.status = res.status; throw err;
    }
    return opts.raw ? res : res.status === 204 ? null : res.json();
  }
  function h(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "class") e.className = v;
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) if (c != null && c !== false) e.append(c instanceof Node ? c : document.createTextNode(c));
    return e;
  }
  function notice(msg, kind = "") { return h("div", { class: "notice" + (kind === true ? " error" : kind ? " " + kind : "") }, msg); }
  function when(iso) { return iso ? new Date(iso).toLocaleString() : "–"; }
  function duration(a, b) {
    if (!a) return "–";
    const s = Math.max(0, ((b ? new Date(b) : new Date()) - new Date(a)) / 1000);
    return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  }
  /** Length of the recording: from the EEG samples (expected = received + missing) when known, else wall clock. */
  function sessionLength(sess) {
    const e = (sess.stats || {}).eeg;
    if (e && e.nominal_rate && e.samples) {
      const s = (e.samples + (e.missing_samples || 0)) / e.nominal_rate;
      return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
    }
    return duration(sess.started_at, sess.ended_at);
  }
  const clock = (s) => { s = Math.max(0, Math.round(s)); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
  function statusChip(status) { return h("span", { class: "chip " + (status === "running" || status === "stopped" ? "ok" : status) }, status); }
  function showTokenForm() {
    const input = h("input", { type: "password", placeholder: "API token", size: 40 });
    app.replaceChildren(h("div", { class: "card" }, h("h2", {}, "This server needs a token"),
      h("p", { class: "muted" }, "Find it in the terminal where `muse serve` started, or in the token file."),
      h("div", { class: "row" }, input, h("button", { class: "primary", onclick: () => { setToken(input.value.trim()); route(); } }, "Save"))));
  }
  const recorded = (sess) => ((sess.pipeline || {}).sink || []).some((s) => s.plugin === "recorder");
  const report = (reports, suffix) => (reports || []).find((r) => r.title.endsWith(suffix));

  // ---------- schema-driven config form ----------
  function schemaForm(schema, values = {}) {
    const props = (schema && schema.properties) || {};
    const fields = [];
    const grid = h("div", { class: "form-grid" });
    for (const [name, p] of Object.entries(props)) {
      const type = p.type || (p.anyOf && p.anyOf.find((x) => x.type && x.type !== "null")?.type);
      const value = name in values ? values[name] : p.default;
      let input, read;
      if (type === "boolean") {
        input = h("input", { type: "checkbox" }); input.checked = !!value; read = () => input.checked;
      } else if (type === "number" || type === "integer") {
        input = h("input", { type: "number", step: type === "integer" ? "1" : "any", value: value ?? "" });
        read = () => (input.value === "" ? null : Number(input.value));
      } else if (type === "string" || type == null && p.enum) {
        input = h("input", { type: "text", value: value ?? "" }); read = () => input.value;
      } else {
        input = h("input", { type: "text", value: value === undefined ? "" : JSON.stringify(value) });
        read = () => { const v = input.value.trim(); return v === "" ? undefined : JSON.parse(v); };
      }
      fields.push([name, read, value]);
      grid.append(field(name, input, p.description));
    }
    grid.values = () => {
      const out = {};
      for (const [name, read, initial] of fields) {
        const v = read();
        if (v !== undefined && JSON.stringify(v) !== JSON.stringify(initial)) out[name] = v;
      }
      return out;
    };
    return grid;
  }

  // ---------- live ----------
  async function pageLive() {
    const health = await api("/health");
    const id = health.live_session || health.last_session;
    const sess = id ? await api(`/sessions/${id}`).catch(() => null) : null;
    if (!sess || !sess.active) return startForm(sess);
    return liveView(sess);
  }

  async function startForm(last) {
    const plugins = await api("/plugins");
    const sources = plugins.filter((p) => p.kind === "source" && p.name !== "replay")
      .sort((a, b) => (a.name in SOURCES ? Object.keys(SOURCES).indexOf(a.name) : 9) - (b.name in SOURCES ? Object.keys(SOURCES).indexOf(b.name) : 9));
    // pipeline order matters: an analyzer reads only the outputs of those before it (describe reads them all)
    const rank = (n) => { const i = ["quality", "filter", "baseline", "bandpower", "artifacts"].indexOf(n); return n === "describe" ? 99 : i < 0 ? 50 : i; };
    const analyzers = plugins.filter((p) => p.kind === "analyzer").sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
    const defaults = ["quality", "filter", "baseline", "bandpower", "artifacts", "describe"];
    let chosen = (sources.find((s) => s.status === "ok") || sources[0] || {}).name;
    const optsBox = h("div");
    const showOpts = () => {
      const s = sources.find((x) => x.name === chosen);
      optsBox.replaceChildren(schemaForm(s ? s.config_schema : {}, s ? s.config_defaults : {}));
    };
    const cards = sources.map((s) => {
      const [title, text] = SOURCES[s.name] || [s.name, s.summary];
      const radio = h("input", { type: "radio", name: "source", value: s.name, disabled: s.status !== "ok" });
      radio.checked = s.name === chosen;
      radio.addEventListener("change", () => { chosen = s.name; showOpts(); });
      return h("label", { class: "source-card" + (s.status !== "ok" ? " disabled" : "") }, radio,
        h("span", {}, h("strong", {}, title), h("span", { class: "muted" }, s.status !== "ok" ? `${s.status}: ${s.hint}` : text)));
    });
    showOpts();
    const checks = analyzers.map((a) => {
      const c = h("input", { type: "checkbox", value: a.name, disabled: a.status !== "ok" });
      c.checked = defaults.includes(a.name) && a.status === "ok";
      return h("span", { class: "chip plain" }, h("label", {}, c, a.name), info(a.summary, a.name));
    });
    const record = h("input", { type: "checkbox" }); record.checked = true;
    const label = h("input", { type: "text", placeholder: "e.g. morning meditation" });
    const msg = h("div");
    const start = h("button", { class: "primary", onclick: async () => {
      start.disabled = true; msg.replaceChildren(notice(`Connecting to ${(SOURCES[chosen] || [chosen])[0].toLowerCase()}…`));
      const pipeline = {
        session: { label: label.value || null },
        source: { plugin: chosen, ...optsBox.firstChild.values() },
        analyzer: checks.map((c) => c.querySelector("input")).filter((i) => i.checked).map((i) => ({ plugin: i.value })),
        sink: record.checked ? [{ plugin: "recorder" }] : [],
      };
      try { await api("/sessions", { method: "POST", json: pipeline }); route(); }
      catch (e) { msg.replaceChildren(notice(e.message, true)); start.disabled = false; }
    } }, "Start session");
    const lastCard = last ? h("div", { class: "card last" },
      h("span", {}, h("span", { class: "muted" }, "Last session: "), h("strong", {}, last.label || last.id), " · ",
        statusChip(last.status), last.ended_at ? h("span", { class: "muted" }, ` · ended ${when(last.ended_at)}`) : null),
      last.error ? h("div", { class: "muted" }, last.error) : null,
      recorded(last) ? h("a", { href: `#/session/${last.id}` }, "Open") : null) : null;
    app.replaceChildren(...[
      h("h1", {}, "Start a session"), lastCard, msg,
      h("div", { class: "card" },
        sub("Source", HELP.source), h("div", { class: "source-cards" }, cards),
        h("div", { class: "form-grid", style: "margin-top:14px" },
          field("Label", label, HELP.label), field("Record to disk", record, HELP.record)),
        h("details", { class: "advanced" }, h("summary", {}, "Advanced ", info(HELP.advanced, "Advanced")),
          sub("Source options", null, "h3"), optsBox,
          heading("h3", "Analyzers", HELP.analyzers), h("div", { class: "row" }, checks)),
        h("div", { class: "row", style: "margin-top:14px" }, start, info(HELP.start, "Start session")))].filter(Boolean));
  }

  function liveView(sess) {
    const source = (sess.pipeline || {}).source || {};
    const isReplay = source.plugin === "replay";
    const store = new Store(300);
    const elapsed = h("span", { class: "muted" });
    const battery = h("span", { class: "muted" });
    const via = h("span", { class: "chip " + (isReplay ? "warn" : "plain") },
      isReplay ? `replay ${source.speed || 1}×` : VIA[source.plugin] || source.plugin || "");
    const banner = h("div", { class: "banner", hidden: true });
    const chips = h("div", { class: "row" });
    const markerLabel = h("input", { type: "text", placeholder: "marker label", size: 16, "aria-label": "Marker label" });
    const addMarker = async () => {
      await api(`/sessions/${sess.id}/markers`, { method: "POST", json: { label: markerLabel.value || "marker" } });
      markerLabel.value = "";
    };
    markerLabel.addEventListener("keydown", (e) => { if (e.key === "Enter") addMarker(); });
    const calibrateBtn = h("button", { onclick: async () => {
      calibrateBtn.disabled = true;
      await api(`/sessions/${sess.id}/calibrate`, { method: "POST", json: {} });
    } }, "Calibrate (2 min)");

    // sections: quality and now are single combined cards; the others are grids that appear once a card has data
    const qualityCard = h("div", { class: "card" }, sub(SECTIONS.quality, HELP.quality));
    const qualityWarn = notice("Fewer than half of the electrode readings have good contact. Adjust the headband so the sensors touch the skin, and moisten them.", "warn");
    qualityWarn.hidden = true;
    qualityCard.append(qualityWarn);
    const nowCard = h("div", { class: "card" }, sub(SECTIONS.now, HELP.now));
    const waiting = { quality: h("div", { class: "empty" }, "waiting for data…"), now: h("div", { class: "empty" }, "waiting for data…") };
    qualityCard.append(waiting.quality); nowCard.append(waiting.now);
    const grids = {}, sectionEls = {};
    for (const s of ["trends", "signal", "events"]) {
      grids[s] = h("div", { class: "grid" });
      sectionEls[s] = h("section", { class: "live-section", hidden: true }, sub(SECTIONS[s], HELP[s]), grids[s]);
    }
    const entries = [];
    const hasData = (v) => {
      if (v.type === "events" || v.type === "text") {
        const pre = v.event_types || [];
        return store.events.some((e) => (!pre.length || pre.some((p) => e.type.startsWith(p))) && (v.type !== "text" || (e.data && e.data.text)));
      }
      return v.streams.some((s) => store.streams[s] && store.streams[s].t.length);
    };
    function addView(v) {
      const sec = sectionOf(v);
      const body = h("div");
      if (sec === "quality" || sec === "now") {
        const spec = v.type === "text" ? { ...v, limit: 1 } : v;
        const block = h("div", { class: "block", hidden: true }, sub(v.title, v.description, "h3"), body);
        const card = sec === "quality" ? qualityCard : nowCard;
        if (v.type === "text") card.insertBefore(block, card.children[1]); else card.append(block);
        entries.push({ v: spec, body, el: block, sec });
        if (v.type === "text") addView({ ...v, id: v.id + "/all", title: "Earlier descriptions", section: "events" });
      } else {
        const wide = v.type === "timeseries" && (v.window_s <= 10 || v.id.endsWith("trend"));
        const card = h("div", { class: "card" + (wide ? " wide" : ""), hidden: true }, sub(v.title, v.description), body);
        grids[sec].append(card);
        entries.push({ v, body, el: card, sec });
      }
    }

    app.replaceChildren(
      h("div", { class: "row" },
        h("h1", {}, sess.label || "Live session"),
        h("span", { class: "with-info" }, statusChip("running"), info(HELP.status, "status")),
        h("span", { class: "with-info" }, elapsed, info(HELP.elapsed, "elapsed time")),
        h("span", { class: "with-info" }, battery, info(HELP.battery, "battery")),
        h("span", { class: "with-info" }, via, info(isReplay ? HELP.replaying : HELP.via, "source")),
        h("span", { class: "spacer" }),
        h("button", { class: "danger", onclick: async () => { await api(`/sessions/${sess.id}/stop`, { method: "POST" }); route(); } }, "Stop"),
        info(HELP.stop, "Stop")),
      isReplay ? null : h("div", { class: "row controls" }, markerLabel,
        h("button", { onclick: addMarker }, "Add marker"), info(HELP.marker, "Add marker"),
        calibrateBtn, info(HELP.calibrate, "Calibrate")),
      banner,
      h("div", { class: "live-top" }, qualityCard, nowCard),
      sectionEls.trends, sectionEls.signal, sectionEls.events,
      h("details", { class: "details" }, h("summary", {}, "Details"),
        h("div", { class: "row" }, h("span", { class: "with-info muted" }, "Plugins", info(HELP.health, "plugin health")), chips),
        h("div", { class: "muted" }, `Session ${sess.id} · ${sess.device ? [sess.device.name, sess.device.model].filter(Boolean).join(" · ") : ""}`)));

    // calibration banner, driven by the session's calibration events
    let calib = null;
    function onEvent(e) {
      const phase = { "calibration.eyes_open": "Keep your eyes OPEN and relax", "calibration.eyes_closed": "CLOSE your eyes and relax" }[e.type];
      if (phase) calib = { text: phase, end: e.t + (e.duration || 0) };
      else if (e.type === "calibration.end" || e.type === "baseline.ready") calib = { text: "Calibration done: band power is now also compared with your baseline.", until: Date.now() + 6000, done: true };
      else if (e.type === "baseline.failed") calib = { text: `Calibration failed: ${(e.data && e.data.reason) || "not enough clean data"}.`, until: Date.now() + 10000, done: true, bad: true };
    }
    function drawBanner() {
      if (!calib || (calib.until && Date.now() > calib.until)) {
        banner.hidden = true; calib = calib && calib.until ? null : calib;
        if (!calib) calibrateBtn.disabled = false;
        return;
      }
      banner.hidden = false;
      banner.className = "banner" + (calib.bad ? " bad" : calib.done ? " done" : "");
      const left = calib.end && !isReplay ? Math.max(0, calib.end - Date.now() / 1000) : null;
      banner.textContent = (isReplay && !calib.done ? "In the recording: " : "Calibration: ") + calib.text + (left != null ? ` · ${clock(left)} left` : "");
      calibrateBtn.disabled = !calib.done;
    }

    let dirty = false, lastDraw = 0, frame = 0;
    function draw(ts) {
      frame = requestAnimationFrame(draw);
      drawBanner();
      if (!dirty || ts - lastDraw < 100) return;
      dirty = false; lastDraw = ts;
      const shown = { quality: 0, now: 0, trends: 0, signal: 0, events: 0 };
      for (const en of entries) {
        const ok = hasData(en.v);
        en.el.hidden = !ok;
        if (ok) { render(en.body, en.v, store); shown[en.sec]++; }
      }
      waiting.quality.hidden = shown.quality > 0;
      waiting.now.hidden = shown.now > 0;
      for (const s of ["trends", "signal", "events"]) sectionEls[s].hidden = !shown[s];
      const usable = entries.find((en) => en.sec === "quality" && en.v.type === "metric");
      const u = usable && store.streams[usable.v.streams[0]];
      qualityWarn.hidden = !(u && u.data.length && u.data[u.data.length - 1][0] < 0.5);
      const b = store.streams.battery;
      if (b && b.data.length) battery.textContent = `battery ${Math.round(b.data[b.data.length - 1][0])}%`;
      elapsed.textContent = duration(sess.started_at);
      if (isReplay) {
        const any = Object.values(store.streams).find((s) => s.t.length);
        if (any) via.textContent = `replay ${source.speed || 1}× · recorded ${new Date(any.t[0] * 1000).toLocaleString()}`;
      }
    }
    frame = requestAnimationFrame(draw);

    const q = new URLSearchParams({ max_hz: "10" });
    if (token()) q.set("token", token());
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${API}/sessions/${sess.id}/live?${q}`);
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "hello") {
        store.declare(msg.streams);
        msg.views.slice().sort((a, b) => ORDER.indexOf(sectionOf(a)) - ORDER.indexOf(sectionOf(b))).forEach(addView);
      } else if (msg.type === "health") {
        chips.replaceChildren(...Object.entries(msg.plugins).map(([name, hl]) =>
          h("span", { class: "chip " + hl.status, title: hl.message || "" }, name + (hl.dropped ? ` (dropped ${hl.dropped})` : ""))));
      } else if (msg.type === "end") { setTimeout(route, 300); }
      else { if (msg.type === "event") onEvent(msg.event); store.add(msg); }
      dirty = true;
    };
    ws.onclose = () => { if (location.hash.startsWith("#/live")) setTimeout(route, 1500); };
    cleanup.push(() => { cancelAnimationFrame(frame); ws.onclose = null; ws.close(); });
  }

  // ---------- sessions ----------
  function keyNumbers(sess, reports) {
    const eeg = (sess.stats || {}).eeg;
    const quality = report(reports, ": Signal quality");
    const desc = report(reports, ": Session description");
    let usable = desc && desc.metrics.usable_pct;
    if (usable == null && quality) {
      const v = Object.entries(quality.metrics).filter(([k]) => k.startsWith("usable_") && k.endsWith("_pct") && !/AUX/.test(k)).map(([, x]) => x);
      usable = v.length ? v.reduce((a, x) => a + x, 0) / v.length : null;
    }
    const received = eeg && eeg.samples ? 100 * eeg.samples / (eeg.samples + (eeg.missing_samples || 0)) : null;
    const art = report(reports, ": Artifacts");
    const bp = report(reports, ": Band power");
    const first = desc && desc.notes.length ? desc.notes[0].replace(/^Whole session \([^)]*\):\s*/, "") : "";
    return { usable, received, blinks: art && art.metrics.blink_per_min, alpha: bp && bp.metrics.mean_rel_alpha, first };
  }
  const pct = (x, d = 0) => (x == null ? "–" : `${Number(x).toFixed(d)}%`);

  async function pageSessions() {
    const list = await api("/sessions");
    const search = h("input", { type: "search", placeholder: "Search label or description", size: 28, "aria-label": "Search sessions" });
    const tbody = h("tbody");
    const rows = list.map((s) => {
      const k = keyNumbers(s, s.reports);
      const tr = h("tr", { class: "link", onclick: () => (location.hash = `#/session/${s.id}`) },
        h("td", {}, when(s.started_at)), h("td", {}, s.label || h("span", { class: "muted" }, "—")),
        h("td", {}, sessionLength(s)),
        h("td", {}, s.device ? s.device.model || s.device.name : "–"),
        h("td", {}, pct(k.usable)), h("td", {}, pct(k.received, 1)),
        h("td", { class: "summary-cell" }, k.first || h("span", { class: "muted" }, "—")),
        h("td", {}, statusChip(s.status)));
      tr.dataset.text = `${s.label || ""} ${k.first}`.toLowerCase();
      return tr;
    });
    tbody.append(...rows);
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      for (const tr of rows) tr.hidden = !!q && !tr.dataset.text.includes(q);
    });
    app.replaceChildren(h("div", { class: "row" }, heading("h1", "Sessions", HELP.sessions), h("span", { class: "spacer" }), list.length ? search : null),
      list.length ? h("div", { class: "card table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, th("Started", HELP.started), th("Label", HELP.label), th("Duration", HELP.duration),
          th("Device", HELP.device), th("Usable", HELP.usable), th("Data received", HELP.received),
          th("Summary", HELP.summaryCol), th("Status", HELP.status))), tbody))
        : notice("No recordings yet. Start one on the Live page."));
  }

  async function pageSession(id) {
    const [sess, streams, events, reports, analyses] = await Promise.all([
      api(`/sessions/${id}`), api(`/sessions/${id}/streams`), api(`/sessions/${id}/events`), api(`/sessions/${id}/reports`),
      api("/sessions?mode=offline").catch(() => [])]);
    const analysis = analyses.find((a) => a.parent === id);                 // newest analysis of this recording
    const allReports = analysis && analysis.reports.length ? analysis.reports : sess.reports || [];
    const views = (analysis && analysis.views.length ? analysis.views : sess.views) || [];
    const msg = h("div");

    // actions
    const analyze = h("button", { class: "primary", onclick: async () => {
      analyze.disabled = true;
      try {
        const job = await api(`/sessions/${id}/analyses`, { method: "POST", json: {} });
        const bar = h("progress", { max: 1, value: 0 });
        msg.replaceChildren(h("div", { class: "notice row" }, "Analyzing…", bar));
        for (;;) {
          await new Promise((r) => setTimeout(r, 500));
          const j = await api(`/jobs/${job.id}`);
          bar.value = j.progress;
          if (j.status === "done") return route();
          if (j.status === "failed") throw new Error(j.error);
        }
      } catch (e) { msg.replaceChildren(notice(e.message, true)); analyze.disabled = false; }
    } }, "Analyze");
    const speed = h("select", { "aria-label": "Replay speed" }, [1, 2, 5, 10].map((x) => h("option", { value: String(x) }, `${x}×`)));
    const replayBtn = h("button", { onclick: async () => {
      replayBtn.disabled = true;
      try {
        await api(`/sessions/${id}/replay?speed=${speed.value}`, { method: "POST", json: {} });
        location.hash = "#/live";
      } catch (e) { msg.replaceChildren(notice(e.message, true)); replayBtn.disabled = false; }
    } }, "Replay");
    const exportMenu = h("details", { class: "menu" }, h("summary", { class: "btn" }, "Export ▾"),
      h("div", { class: "menu-items" }, ["csv", "edf"].map((f) => h("button", { onclick: async () => {
        exportMenu.open = false;
        try {
          const res = await api(`/sessions/${id}/export?format=${f}`, { method: "POST", raw: true });
          const a = h("a", { href: URL.createObjectURL(await res.blob()), download: `${id}.${f === "csv" ? "zip" : f}` });
          a.click(); URL.revokeObjectURL(a.href);
        } catch (e) { msg.replaceChildren(notice(e.message, true)); }
      } }, f === "csv" ? "CSV (zip)" : "EDF+"))));

    // summary
    const k = keyNumbers(sess, allReports);
    const desc = report(allReports, ": Session description");
    const nums = [["Duration", sessionLength(sess), HELP.duration], ["Usable", pct(k.usable), HELP.usable],
      ["Data received", pct(k.received, 1), HELP.received],
      ["Blinks", k.blinks == null ? "–" : `${fmt(k.blinks)}/min`, "Blinks per minute, detected from the forehead electrodes."],
      ["Alpha share", k.alpha == null ? "–" : pct(100 * k.alpha), "Average share of power in the alpha band (8–13 Hz), over windows with good contact."]];
    const descNotes = desc ? desc.notes : [];
    const segNotes = descNotes.slice(1).filter((n) => n.startsWith("“"));
    const otherNotes = descNotes.slice(1).filter((n) => !n.startsWith("“"));
    const qrep = report(allReports, ": Signal quality");
    const contact = qrep ? Object.entries(qrep.metrics).filter(([k2]) => /^usable_.+_pct$/.test(k2) && !/AUX/i.test(k2))
      .map(([k2, v]) => ({ name: k2.slice(7, -4), status: v >= 80 ? "good" : v >= 50 ? "fair" : "poor", caption: `${Math.round(v)}%` })) : [];
    const mapBox = h("div");
    if (contact.length) {
      const worst = contact.slice().sort((a, b) => parseFloat(a.caption) - parseFloat(b.caption))[0];
      headMap(mapBox, contact, { legend: ["good", "fair", "poor"], summary: worst.status === "good"
        ? "Every electrode had good contact for at least 80% of the session."
        : `Least reliable: ${worst.name} (${worst.caption} good contact).` });
    }
    const summary = h("div", { class: "card wide" }, sub("Summary", HELP.summary),
      h("div", { class: "summary-top" + (contact.length ? "" : " single") },
        h("div", {},
          h("div", { class: "keynums" }, nums.map(([l, v, help]) => h("div", {}, h("div", { class: "metric-value" }, v),
            h("div", { class: "metric-label with-info" }, l, info(help, l))))),
          desc ? [h("p", { class: "lead" }, descNotes[0]),
            segNotes.length ? h("ul", { class: "segments" }, segNotes.map((n) => h("li", {}, n))) : null,
            otherNotes.map((n) => h("p", { class: "muted small" }, n))]
            : h("p", { class: "muted" }, "No description for this session yet: press Analyze (or Replay) to create one.")),
        contact.length ? h("div", { class: "summary-map" }, heading("h3", "Signal strength over the session", HELP.contact), mapBox) : null));

    // timeline: one time axis for all charts; segments zoom them
    const raw = streams.filter((s) => !s.id.includes("/") && s.rate > 0)
      .sort((a, b) => (a.kind === "eeg" ? -1 : 0) - (b.kind === "eeg" ? -1 : 0));
    const derived = new Set(streams.map((x) => x.id));
    const trendView = views.find((v) => v.type === "timeseries" && v.streams.length && v.streams[0].endsWith("/mean_rel") && derived.has(v.streams[0]))
      || views.find((v) => v.type === "timeseries" && v.section === "trends" && v.streams.every((s) => derived.has(s)));
    const select = h("select", { "aria-label": "Stream" }, raw.map((s) => h("option", { value: s.id }, `${s.id} (${s.channels.length} ch, ${s.rate} Hz)`)));
    const trendBody = h("div"), rawBody = h("div"), segChips = h("div", { class: "row seg-chips" });
    let range = null, full = null;
    async function loadInto(body, stream, spec) {
      const q = new URLSearchParams({ max_points: "3000" });
      if (range) { q.set("from", String(range[0])); q.set("to", String(range[1])); }
      const d = await api(`/sessions/${id}/streams/${stream}?${q}`);
      const st = new Store(1e12);
      st.streams[stream] = { info: d.info, t: d.t, data: d.data };
      st.events = events;
      if (range) { st.tStart = range[0]; st.tEnd = range[1]; } else if (d.t.length) { st.tStart = d.t[0]; st.tEnd = d.t[d.t.length - 1]; }
      if (!full && !range && d.t.length) full = [d.t[0], d.t[d.t.length - 1]];
      render(body, { type: "timeseries", window_s: 1e9, ...spec, streams: [stream] }, st);
    }
    async function loadTimeline() {
      try {
        if (select.value) await loadInto(rawBody, select.value, { y: { unit: (raw.find((s) => s.id === select.value) || { units: [""] }).units[0] } });
        else rawBody.replaceChildren(notice("No raw streams."));
        if (trendView) await loadInto(trendBody, trendView.streams[0], trendView);
      } catch (e) { rawBody.replaceChildren(notice(e.message, true)); }
    }
    select.addEventListener("change", loadTimeline);
    function segments() {
      const marks = events.filter((e) => e.type === "marker").sort((a, b) => a.t - b.t);
      const out = events.filter((e) => e.type.startsWith("calibration.eyes_")).map((e) => [e.t, e.t + (e.duration || 0), "calibration, " + e.type.slice(16).replace("_", " ")]);
      marks.forEach((m, i) => {
        const label = ((m.data && m.data.label) || "").trim();
        const end = i + 1 < marks.length ? marks[i + 1].t : full ? full[1] : m.t;
        if (label && label !== "-" && end - m.t >= 5) out.push([m.t, end, label]);
      });
      return out.sort((a, b) => a[0] - b[0]);
    }
    function drawChips() {
      const t0 = full ? full[0] : 0;
      const chip = (label, r) => h("button", { class: "chip-btn" + (JSON.stringify(r) === JSON.stringify(range) ? " active" : ""),
        onclick: () => { range = r; drawChips(); loadTimeline(); } }, label);
      segChips.replaceChildren(chip("Whole session", null),
        ...segments().map(([a, b, label]) => chip(`${label} ${clock(a - t0)}–${clock(b - t0)}`, [a, b])));
    }
    const timeline = h("div", { class: "card wide" }, h("div", { class: "row" }, sub("Timeline", HELP.timeline)), segChips,
      trendView ? [h("h3", {}, trendView.title), trendBody]
        : h("p", { class: "muted small" }, "Run Analyze to add band power over time to the timeline."),
      h("div", { class: "row" }, h("h3", {}, "Recorded signal"), h("span", { class: "spacer" }), select), rawBody);

    // other charts from the analysis (whole session); last-value views (bars, metrics) don't summarise a session
    const available = new Set(streams.map((s) => s.id));
    const chartCards = views.filter((v) => v !== trendView && !["text", "bars", "bands", "metric", "table", "spectrum"].includes(v.type))
      .sort((a, b) => ORDER.indexOf(sectionOf(a)) - ORDER.indexOf(sectionOf(b)))
      .filter((v) => v.type === "events" || v.streams.every((s) => available.has(s)))
      .map((v) => {
        const body = h("div");
        if (v.type === "events") render(body, { ...v, window_s: 1e9 }, { streams: {}, events });
        else (async () => {
          const st = new Store(1e12);
          for (const s of v.streams) {
            const d = await api(`/sessions/${id}/streams/${s}?max_points=2000`);
            st.streams[s] = { info: d.info, t: d.t, data: d.data };
            if (d.t.length) { st.tStart = d.t[0]; st.tEnd = d.t[d.t.length - 1]; }
          }
          st.events = events;
          render(body, { ...v, window_s: 1e9 }, st);
        })().catch((e) => body.replaceChildren(notice(e.message, true)));
        return h("div", { class: "card" }, sub(v.title, v.description), body);
      });

    // details
    const completeness = Object.entries(sess.stats || {}).filter(([sid]) => !sid.includes("/")).map(([sid, s]) => h("tr", {},
      h("td", {}, sid), h("td", {}, String(s.samples)),
      h("td", {}, s.nominal_rate ? pct(100 * s.samples / (s.samples + (s.missing_samples || 0)), 1) : "–"),
      h("td", {}, fmt(s.effective_rate)), h("td", {}, fmt(s.nominal_rate)), h("td", {}, String(s.gaps))));
    const metricTables = allReports.filter((r) => !r.title.endsWith(": Session description")).map((r) => h("div", { class: "card" },
      sub(r.title, HELP.pluginReports, "h3"),
      h("table", { class: "kv" }, Object.entries(r.metrics).map(([k2, v]) => h("tr", {}, h("th", {}, k2), h("td", {}, typeof v === "number" ? fmt(v) : String(v ?? "–"))))),
      r.notes.map((n) => h("p", { class: "muted small" }, n))));
    const details = h("details", { class: "details" }, h("summary", {}, "Details"),
      h("div", { class: "grid" },
        h("div", { class: "card table-wrap" }, sub("Data completeness", HELP.completeness, "h3"), h("table", {},
          h("tr", {}, th("Stream"), th("Samples"), th("Received"), th("Rate"), th("Expected"), th("Gaps")), completeness)),
        h("div", { class: "card" }, sub("HTML reports", HELP.reports, "h3"), reports.length
          ? h("ul", {}, reports.map((r) => h("li", {}, h("a", { href: `${API}/sessions/${id}/reports/${r}${token() ? "?token=" + encodeURIComponent(token()) : ""}`, target: "_blank" }, r))))
          : h("p", { class: "muted" }, "Run Analyze to create a report."),
          sub("Plugin versions", HELP.versions, "h3"),
          h("p", { class: "muted small" }, Object.values((analysis || sess).plugins || {}).join(" · ") || "–")),
        metricTables));

    const dev = sess.device ? sess.device.model || sess.device.name : "";
    const src = (((sess.pipeline || {}).source) || {}).plugin;
    app.replaceChildren(
      h("div", { class: "row" }, h("h1", {}, sess.label || "Session"),
        h("span", { class: "with-info" }, statusChip(sess.status), info(HELP.status, "status"))),
      h("div", { class: "muted" }, [when(sess.started_at), sessionLength(sess), dev, VIA[src],
        sess.error].filter(Boolean).join(" · ")),
      sess.mode === "live" ? h("div", { class: "row controls" }, speed, replayBtn, info(HELP.replay, "Replay"),
        analyze, info(HELP.analyze, "Analyze"), exportMenu, info(HELP.export, "Export")) : null,
      msg,
      h("div", { class: "grid" }, summary, timeline),
      chartCards.length ? h("section", { class: "live-section" }, sub("Charts", HELP.charts), h("div", { class: "grid" }, chartCards)) : null,
      details);
    await loadTimeline();
    drawChips();
  }

  async function pagePlugins() {
    const plugins = await api("/plugins");
    const kinds = { source: "connects to a device or file and produces data", analyzer: "processes data and adds charts", sink: "stores or exports data" };
    app.replaceChildren(heading("h1", "Plugins", HELP.plugins), h("div", { class: "grid" }, plugins.map((p) => {
      const opts = Object.entries((p.config_schema && p.config_schema.properties) || {});
      return h("div", { class: "card" },
        h("div", { class: "row" }, sub(p.name, `A ${p.kind}: ${kinds[p.kind] || ""}.`),
          h("span", { class: "muted" }, `${p.kind} · v${p.version}`), h("span", { class: "spacer" }),
          h("span", { class: "chip " + (p.status === "ok" ? "ok" : "bad") }, p.status)),
        h("p", { class: "muted" }, p.summary), p.hint ? notice(p.hint, true) : null,
        opts.length ? h("table", {}, h("tr", {}, th("Option", HELP.options), th("Default")),
          opts.map(([k, o]) => h("tr", {}, h("td", {}, h("span", { class: "with-info" }, k, o.description ? info(o.description, `${p.name} option ${k}`) : null)),
            h("td", {}, k in (p.config_defaults || {}) ? JSON.stringify(p.config_defaults[k]) : "required")))) : null);
    })));
  }

  // ---------- router ----------
  async function route() {
    cleanup.forEach((f) => f()); cleanup = [];
    const hash = location.hash || "#/live";
    document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active",
      hash.startsWith(a.getAttribute("href")) || (a.getAttribute("href") === "#/sessions" && hash.startsWith("#/session/"))));
    try {
      if (hash.startsWith("#/session/")) await pageSession(decodeURIComponent(hash.slice(10)));
      else if (hash.startsWith("#/sessions")) await pageSessions();
      else if (hash.startsWith("#/plugins")) await pagePlugins();
      else await pageLive();
    } catch (e) {
      if (e.message !== "authentication required") app.replaceChildren(notice(e.message, true));
    }
  }
  window.addEventListener("hashchange", route);
  route();
})();
