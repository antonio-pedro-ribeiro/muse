/* Muse Monitor web UI: plain JS, hash routing, talks only to /api/v1. */
(function () {
  "use strict";
  const { render, Store, fmt, info, heading } = window.MuseRenderers;

  // Plain-language explanations for the UI's own elements. Plugin views bring their own (ViewSpec.description).
  const HELP = {
    source: "Where the data comes from. openmuse connects to the Muse headband directly over Bluetooth; synthetic generates test signals so you can try the app without hardware.",
    label: "Optional name for this session, shown in the session list and reports (e.g. \"morning meditation\").",
    record: "Save the unmodified sensor data to disk so you can review, re-analyze or export it later. If off, nothing is stored.",
    sourceOptions: "Settings for the chosen source. The defaults work in most cases. For openmuse, leave the address empty to use the strongest headband in range.",
    analyzers: "Processing steps that run on the live signal. Each one adds charts. You can re-run any of them later on a recording with \"Analyze offline\".",
    start: "Connects to the source and starts streaming. Only one live session can run at a time.",
    status: "running: data is streaming. stopped: the session ended normally. failed: it ended because of an error. incomplete: the app quit while recording; run `muse sessions repair` to finish the files.",
    elapsed: "Time since the session started.",
    battery: "Headband battery level as reported by the device.",
    health: "One chip per plugin in this session. Green: working. Yellow: degraded, e.g. Bluetooth packets lost. Red: stopped after an error; the rest of the session keeps running. \"dropped\" counts chunks an analyzer skipped because it fell behind; the recording itself never drops data.",
    marker: "Adds a labelled timestamp to the recording, e.g. \"eyes closed\" or \"breathing exercise\". Markers show as dashed lines in charts and are kept in exports.",
    calibrate: "A 2-minute personal baseline: keep your eyes open and relaxed for 60 s, then closed for 60 s, following the prompts. Band power is then also shown relative to this baseline.",
    stop: "Ends the session and finishes writing the recording.",
    sessions: "All recorded sessions, newest first. Click a row to open it.",
    started: "When the session started, in your local time.",
    duration: "How long the session lasted.",
    device: "The headband (or test source) the data came from.",
    gaps: "How many times samples were lost, e.g. Bluetooth dropouts. Gaps are recorded and reported, never filled in.",
    analyze: "Replays the recording through the analyzers, saves their results next to the raw data and creates an HTML report. The raw recording is never changed; running it again replaces the previous results.",
    exportCsv: "Download every stream and the event list as CSV files (zipped), for spreadsheets or your own scripts.",
    exportEdf: "Download an EDF+ file for EEG tools such as EDFbrowser, EEGLAB or MNE. Markers and artifacts become annotations; gaps are filled with zeros and annotated.",
    recorded: "The recorded signal for the whole session, reduced for display (peaks are kept). Choose a stream: eeg (brain signal), acc (head movement), gyro (rotation), optics (blood-flow sensors).",
    streams: "Per sensor stream: samples received, the measured sample rate, the rate the device should deliver, and lost data. A rate well below nominal or many gaps points to a weak Bluetooth link.",
    samples: "Number of measurements received for this stream.",
    rate: "Measured samples per second over the session.",
    nominal: "Samples per second the device is expected to deliver (0 means irregular, e.g. battery).",
    missing: "Total number of samples lost inside gaps.",
    reports: "HTML summaries created by offline analyses. Each opens in a new tab and is a single file you can keep or share.",
    report: "Summary computed by this plugin at the end of the session or analysis.",
    plugins: "Plugins provide devices (sources), processing (analyzers) and outputs (sinks). ok: ready. unavailable: a dependency is missing (see the hint). incompatible: built for a newer version of the app.",
    options: "Configuration options with their default values. Change them in a pipeline TOML file or, for sources, in the start form.",
  };
  const sub = (text, help, style) => { const e = heading("h2", text, help); if (style) e.setAttribute("style", style); return e; };
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
    for (const c of children.flat()) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(c));
    return e;
  }
  function notice(msg, error = false) { return h("div", { class: "notice" + (error ? " error" : "") }, msg); }
  function when(iso) { return iso ? new Date(iso).toLocaleString() : "–"; }
  function duration(a, b) {
    if (!a) return "–";
    const s = Math.max(0, ((b ? new Date(b) : new Date()) - new Date(a)) / 1000);
    return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  }
  function statusChip(status) { return h("span", { class: "chip " + (status === "running" || status === "stopped" ? "ok" : status) }, status); }
  function showTokenForm() {
    const input = h("input", { type: "password", placeholder: "API token", size: 40 });
    app.replaceChildren(h("div", { class: "card" }, h("h2", {}, "This server needs a token"),
      h("p", { class: "muted" }, "Find it in the terminal where `muse serve` started, or in the token file."),
      h("div", { class: "row" }, input, h("button", { class: "primary", onclick: () => { setToken(input.value.trim()); route(); } }, "Save"))));
  }

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

  // ---------- pages ----------
  async function pageLive() {
    const health = await api("/health");
    const id = health.live_session || health.last_session;
    const sess = id ? await api(`/sessions/${id}`) : null;
    if (!sess || !sess.active) return startForm(sess);
    return liveView(sess);
  }

  async function startForm(last) {
    const plugins = await api("/plugins");
    const sources = plugins.filter((p) => p.kind === "source" && p.name !== "replay");
    const analyzers = plugins.filter((p) => p.kind === "analyzer");
    const defaults = ["quality", "filter", "baseline", "bandpower", "artifacts"];
    const srcSelect = h("select", {}, sources.map((s) => h("option", { value: s.name, disabled: s.status !== "ok" },
      `${s.name}${s.status !== "ok" ? " (" + s.status + ")" : ""}`)));
    const optsBox = h("div");
    const showOpts = () => {
      const s = sources.find((x) => x.name === srcSelect.value);
      optsBox.replaceChildren(schemaForm(s ? s.config_schema : {}, s ? s.config_defaults : {}));
    };
    srcSelect.addEventListener("change", showOpts);
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
      start.disabled = true; msg.replaceChildren();
      const pipeline = {
        session: { label: label.value || null },
        source: { plugin: srcSelect.value, ...optsBox.firstChild.values() },
        analyzer: checks.map((c) => c.querySelector("input")).filter((i) => i.checked).map((i) => ({ plugin: i.value })),
        sink: record.checked ? [{ plugin: "recorder" }] : [],
      };
      try { await api("/sessions", { method: "POST", json: pipeline }); route(); }
      catch (e) { msg.replaceChildren(notice(e.message, true)); start.disabled = false; }
    } }, "Start session");
    app.replaceChildren(...[
      h("h1", {}, "Start a session"),
      last ? notice(`Last session ${last.label || last.id} ended (${last.status}${last.error ? ": " + last.error : ""}).`, last.status === "failed") : null,
      msg,
      h("div", { class: "card" },
        h("div", { class: "form-grid" },
          field("Source", srcSelect, HELP.source),
          field("Label", label, HELP.label),
          field("Record to disk", record, HELP.record)),
        sub("Source options", HELP.sourceOptions, "margin-top:14px"), optsBox,
        sub("Analyzers", HELP.analyzers, "margin-top:14px"), h("div", { class: "row" }, checks),
        h("div", { class: "row", style: "margin-top:14px" }, start, info(HELP.start, "Start session")))].filter(Boolean));
  }

  function liveView(sess) {
    const store = new Store(300);
    const cards = new Map();
    const grid = h("div", { class: "grid" });
    const chips = h("div", { class: "row" });
    const elapsed = h("span", { class: "muted" });
    const battery = h("span", { class: "muted" });
    const markerLabel = h("input", { type: "text", placeholder: "marker label", size: 16 });
    const calibrateBtn = h("button", { onclick: async () => {
      calibrateBtn.disabled = true;
      await api(`/sessions/${sess.id}/calibrate`, { method: "POST", json: {} });
      calibrateBtn.textContent = "Calibrating: eyes open, then closed when told";
    } }, "Calibrate (2 min)");
    app.replaceChildren(
      h("div", { class: "row" },
        h("h1", {}, sess.label || "Live session"),
        h("span", { class: "with-info" }, statusChip("running"), info(HELP.status, "status")),
        h("span", { class: "with-info" }, elapsed, info(HELP.elapsed, "elapsed time")),
        h("span", { class: "with-info" }, battery, info(HELP.battery, "battery")),
        h("span", { class: "spacer" }),
        markerLabel,
        h("button", { onclick: async () => { await api(`/sessions/${sess.id}/markers`, { method: "POST", json: { label: markerLabel.value || "marker" } }); markerLabel.value = ""; } }, "Add marker"),
        info(HELP.marker, "Add marker"),
        calibrateBtn, info(HELP.calibrate, "Calibrate"),
        h("button", { class: "danger", onclick: async () => { await api(`/sessions/${sess.id}/stop`, { method: "POST" }); route(); } }, "Stop"),
        info(HELP.stop, "Stop")),
      h("div", { class: "muted" }, `${sess.id} · ${sess.device ? sess.device.name || sess.device.model : ""}`),
      h("div", { class: "row" }, h("span", { class: "with-info muted" }, "Plugins", info(HELP.health, "plugin health")), chips),
      grid);

    function ensureCards(views) {
      for (const v of views) {
        if (cards.has(v.id)) continue;
        const body = h("div");
        const card = h("div", { class: "card" + (v.type === "timeseries" && v.window_s <= 10 ? " wide" : "") }, sub(v.title, v.description), body);
        cards.set(v.id, { v, body });
        grid.append(card);
      }
    }
    let dirty = false, lastDraw = 0, frame = 0;
    function draw(ts) {
      frame = requestAnimationFrame(draw);
      if (!dirty || ts - lastDraw < 100) return;
      dirty = false; lastDraw = ts;
      for (const { v, body } of cards.values()) render(body, v, store);
      const b = store.streams.battery;
      if (b && b.data.length) battery.textContent = `battery ${Math.round(b.data[b.data.length - 1][0])}%`;
      elapsed.textContent = duration(sess.started_at);
    }
    frame = requestAnimationFrame(draw);

    const q = new URLSearchParams({ max_hz: "10" });
    if (token()) q.set("token", token());
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${API}/sessions/${sess.id}/live?${q}`);
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "hello") { store.declare(msg.streams); ensureCards(msg.views); }
      else if (msg.type === "health") {
        chips.replaceChildren(...Object.entries(msg.plugins).map(([name, hl]) =>
          h("span", { class: "chip " + hl.status, title: hl.message || "" }, name + (hl.dropped ? ` (dropped ${hl.dropped})` : ""))));
      } else if (msg.type === "end") { setTimeout(route, 300); }
      else store.add(msg);
      dirty = true;
    };
    ws.onclose = () => { if (location.hash.startsWith("#/live")) setTimeout(route, 1500); };
    cleanup.push(() => { cancelAnimationFrame(frame); ws.onclose = null; ws.close(); });
  }

  async function pageSessions() {
    const list = await api("/sessions");
    const rows = list.map((s) => h("tr", { class: "link", onclick: () => (location.hash = `#/session/${s.id}`) },
      h("td", {}, when(s.started_at)), h("td", {}, s.label || h("span", { class: "muted" }, "—")),
      h("td", {}, duration(s.started_at, s.ended_at)),
      h("td", {}, s.device ? s.device.model || s.device.name : "–"),
      h("td", {}, String(Object.values(s.stats || {}).reduce((a, x) => a + (x.gaps || 0), 0))),
      h("td", {}, statusChip(s.status))));
    app.replaceChildren(heading("h1", "Sessions", HELP.sessions),
      list.length ? h("div", { class: "card table-wrap" }, h("table", {},
        h("tr", {}, th("Started", HELP.started), th("Label", HELP.label), th("Duration", HELP.duration),
          th("Device", HELP.device), th("Gaps", HELP.gaps), th("Status", HELP.status)), rows))
        : notice("No recordings yet. Start one on the Live page."));
  }

  async function pageSession(id) {
    const [sess, streams, events, reports] = await Promise.all([
      api(`/sessions/${id}`), api(`/sessions/${id}/streams`), api(`/sessions/${id}/events`), api(`/sessions/${id}/reports`)]);
    const msg = h("div");
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
    } }, "Analyze offline");
    const exportBtn = (fmt) => [h("button", { onclick: async () => {
      try {
        const res = await api(`/sessions/${id}/export?format=${fmt}`, { method: "POST", raw: true });
        const blob = await res.blob();
        const a = h("a", { href: URL.createObjectURL(blob), download: `${id}.${fmt === "csv" ? "zip" : fmt}` });
        a.click(); URL.revokeObjectURL(a.href);
      } catch (e) { msg.replaceChildren(notice(e.message, true)); }
    } }, `Export ${fmt.toUpperCase()}`), info(fmt === "csv" ? HELP.exportCsv : HELP.exportEdf, `Export ${fmt.toUpperCase()}`)];

    const statsRows = Object.entries(sess.stats || {}).filter(([sid]) => !sid.includes("/")).map(([sid, s]) => h("tr", {},
      h("td", {}, sid), h("td", {}, String(s.samples)), h("td", {}, fmt(s.effective_rate)), h("td", {}, fmt(s.nominal_rate)),
      h("td", {}, String(s.gaps)), h("td", {}, String(s.missing_samples))));
    const reportCards = (sess.reports || []).map((r) => h("div", { class: "card" }, sub(r.title, HELP.report),
      h("table", { class: "kv" }, Object.entries(r.metrics).map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, typeof v === "number" ? fmt(v) : String(v ?? "–"))))),
      r.notes.map((n) => h("p", { class: "muted" }, n))));

    // trace viewer
    const raw = streams.filter((s) => !s.id.includes("/") && s.rate > 0)
      .sort((a, b) => (a.kind === "eeg" ? -1 : 0) - (b.kind === "eeg" ? -1 : 0));
    const select = h("select", {}, raw.map((s) => h("option", { value: s.id }, `${s.id} (${s.channels.length} ch, ${s.rate} Hz)`)));
    const traceBody = h("div");
    async function loadTrace() {
      if (!select.value) return traceBody.replaceChildren(notice("No raw streams."));
      const d = await api(`/sessions/${id}/streams/${select.value}?max_points=3000`);
      const store = new Store(1e12);
      store.streams[select.value] = { info: d.info, t: d.t, data: d.data };
      store.events = events;
      if (d.t.length) { store.tStart = d.t[0]; store.tEnd = d.t[d.t.length - 1]; }
      render(traceBody, { type: "timeseries", streams: [select.value], window_s: 1e9, y: { unit: d.info.units[0] } }, store);
    }
    select.addEventListener("change", loadTrace);

    // derived views (available after offline analysis)
    const available = new Set(streams.map((s) => s.id));
    const viewCards = [];
    for (const v of sess.views || []) {
      if (v.type === "events") {
        const body = h("div");
        render(body, v, { streams: {}, events });
        viewCards.push(h("div", { class: "card" }, sub(v.title, v.description), body));
      } else if (v.streams.every((s) => available.has(s)) && v.type !== "spectrum") {
        const body = h("div");
        viewCards.push(h("div", { class: "card" }, sub(v.title, v.description), body));
        (async () => {
          const store = new Store(1e12);
          for (const s of v.streams) {
            const d = await api(`/sessions/${id}/streams/${s}?max_points=2000`);
            store.streams[s] = { info: d.info, t: d.t, data: d.data };
            if (d.t.length) { store.tStart = d.t[0]; store.tEnd = d.t[d.t.length - 1]; }
          }
          store.events = events;
          render(body, { ...v, window_s: 1e9 }, store);
        })().catch((e) => body.replaceChildren(notice(e.message, true)));
      }
    }

    app.replaceChildren(
      h("div", { class: "row" }, h("h1", {}, sess.label || "Session"),
        h("span", { class: "with-info" }, statusChip(sess.status), info(HELP.status, "status")), h("span", { class: "spacer" }),
        sess.mode === "live" ? [analyze, info(HELP.analyze, "Analyze offline"), exportBtn("csv"), exportBtn("edf")] : null),
      h("div", { class: "muted" }, `${sess.id} · ${when(sess.started_at)} · ${duration(sess.started_at, sess.ended_at)}${sess.error ? " · " + sess.error : ""}`),
      msg,
      h("div", { class: "grid" },
        h("div", { class: "card wide" }, h("div", { class: "row" }, sub("Recorded signal", HELP.recorded), h("span", { class: "spacer" }), select), traceBody),
        h("div", { class: "card table-wrap" }, sub("Streams", HELP.streams), h("table", {},
          h("tr", {}, th("Stream"), th("Samples", HELP.samples), th("Rate", HELP.rate), th("Nominal", HELP.nominal),
            th("Gaps", HELP.gaps), th("Missing", HELP.missing)), statsRows)),
        h("div", { class: "card" }, sub("Reports", HELP.reports), reports.length
          ? h("ul", {}, reports.map((r) => h("li", {}, h("a", { href: `${API}/sessions/${id}/reports/${r}${token() ? "?token=" + encodeURIComponent(token()) : ""}`, target: "_blank" }, r))))
          : h("p", { class: "muted" }, "Run an offline analysis to create a report.")),
        reportCards, viewCards));
    loadTrace().catch((e) => traceBody.replaceChildren(notice(e.message, true)));
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
    document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", hash.startsWith(a.getAttribute("href"))));
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
