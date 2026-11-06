/* ViewSpec renderers shared by the web UI and HTML reports. One function per ViewSpec.type.
   store = { streams: { id: { info, t: [], data: [[...]] } }, events: [ {type, t, duration, source, data} ] } */
(function () {
  "use strict";

  const cssVar = (name, fallback) =>
    (getComputedStyle(document.documentElement).getPropertyValue(name) || "").trim() || fallback;

  function palette() {
    return [1, 2, 3, 4, 5, 6, 7, 8].map((i) => cssVar(`--series-${i}`, "#888"));
  }

  function levelColor(level) {
    return cssVar(`--level-${level}`, level === "bad" ? "#d33" : level === "warn" ? "#d90" : "#2a2");
  }

  function levelFor(spec, v) {
    for (const th of spec.thresholds || []) {
      const lo = th.min == null ? -Infinity : th.min;
      const hi = th.max == null ? Infinity : th.max;
      if (v >= lo && v < hi) return th.level;
    }
    return null;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function surface(container, height) {
    let c = container.querySelector("canvas");
    if (!c) {
      c = document.createElement("canvas");
      container.appendChild(c);
    }
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(120, container.clientWidth || 320);
    c.style.width = "100%";
    c.style.height = height + "px";
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(height * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(height * dpr);
    }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, height);
    return { g, w, h: height };
  }

  function legend(container, names, colors) {
    let lg = container.querySelector(".legend");
    if (!lg) {
      lg = el("div", "legend");
      container.appendChild(lg);
    }
    const key = names.join("|");
    if (lg.dataset.key === key) return;
    lg.dataset.key = key;
    lg.replaceChildren(...names.map((n, i) => {
      const item = el("span", "legend-item");
      const sw = el("i", "swatch");
      sw.style.background = colors[i % colors.length];
      item.append(sw, document.createTextNode(n));
      return item;
    }));
  }

  function empty(container, msg) {
    let e = container.querySelector(".empty");
    if (!e) {
      container.replaceChildren();
      e = el("div", "empty");
      container.appendChild(e);
    }
    e.textContent = msg;
  }

  function finiteRange(rows, cols) {
    let lo = Infinity, hi = -Infinity;
    for (const r of rows) for (const c of cols) {
      const v = r[c];
      if (v != null && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }
    if (!isFinite(lo)) return [0, 1];
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    return [lo, hi];
  }

  function fmt(v) {
    if (v == null || !isFinite(v)) return "–";
    if (Number.isInteger(v)) return String(v);
    const a = Math.abs(v);
    return a >= 1000 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
  }

  function axes(g, w, h, pad, ylo, yhi, unit) {
    g.strokeStyle = cssVar("--grid", "#ddd");
    g.fillStyle = cssVar("--muted", "#777");
    g.lineWidth = 1;
    g.font = "11px system-ui, sans-serif";
    for (let i = 0; i <= 2; i++) {
      const y = pad.t + ((h - pad.t - pad.b) * i) / 2;
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
      const v = yhi - ((yhi - ylo) * i) / 2;
      g.fillText(fmt(v) + (i === 0 && unit ? " " + unit : ""), 2, y + 4);
    }
  }

  function selectChannels(spec, info) {
    const all = info.channels.map((c, i) => [c, i]);
    return spec.channels ? all.filter(([c]) => spec.channels.includes(c)) : all;
  }

  const R = {};

  R.timeseries = function (container, spec, store) {
    const s = store.streams[spec.streams[0]];
    if (!s || !s.t.length) return empty(container, "waiting for data…");
    container.querySelector(".empty")?.remove();
    const chans = selectChannels(spec, s.info).slice(0, 8);
    const colors = palette();
    const { g, w, h } = surface(container, 170);
    const pad = { l: 46, r: 8, t: 8, b: 16 };
    const tEnd = store.tEnd ?? s.t[s.t.length - 1];
    const tStart = store.tStart ?? tEnd - spec.window_s;
    let i0 = 0;
    while (i0 < s.t.length && s.t[i0] < tStart) i0++;
    const rows = s.data.slice(i0), ts = s.t.slice(i0);
    let [lo, hi] = finiteRange(rows, chans.map(([, i]) => i));
    if (spec.y && spec.y.min != null) lo = spec.y.min;
    if (spec.y && spec.y.max != null) hi = spec.y.max;
    if (hi <= lo) hi = lo + 1;
    axes(g, w, h, pad, lo, hi, spec.y?.unit);
    const X = (t) => pad.l + ((t - tStart) / (tEnd - tStart || 1)) * (w - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    for (const ev of store.events || []) {
      if (ev.type === "marker" && ev.t >= tStart && ev.t <= tEnd) {
        g.strokeStyle = cssVar("--accent", "#36c");
        g.setLineDash([3, 3]);
        g.beginPath(); g.moveTo(X(ev.t), pad.t); g.lineTo(X(ev.t), h - pad.b); g.stroke();
        g.setLineDash([]);
      }
    }
    g.lineWidth = 1.25;
    chans.forEach(([, ci], k) => {
      g.strokeStyle = colors[k % colors.length];
      g.beginPath();
      let pen = false;
      for (let j = 0; j < rows.length; j++) {
        const v = rows[j][ci];
        if (v == null || !isFinite(v)) { pen = false; continue; }
        const x = X(ts[j]), y = Math.min(h - pad.b, Math.max(pad.t, Y(v)));
        if (pen) g.lineTo(x, y); else { g.moveTo(x, y); pen = true; }
      }
      g.stroke();
    });
    g.fillStyle = cssVar("--muted", "#777");
    g.fillText(`${fmt(tEnd - tStart)} s`, w - pad.r - 40, h - 3);
    legend(container, chans.map(([c]) => c), colors);
  };

  R.spectrum = function (container, spec, store) {
    const s = store.streams[spec.streams[0]];
    if (!s || !s.data.length) return empty(container, "waiting for data…");
    container.querySelector(".empty")?.remove();
    const row = s.data[s.data.length - 1];
    const freqs = (s.info.meta && s.info.meta.freqs) || s.info.channels.map(Number);
    const { g, w, h } = surface(container, 170);
    const pad = { l: 46, r: 8, t: 8, b: 18 };
    const [lo, hi] = finiteRange([row], row.map((_, i) => i));
    axes(g, w, h, pad, lo, hi, spec.y?.unit);
    const fmax = freqs[freqs.length - 1], fmin = freqs[0];
    const X = (f) => pad.l + ((f - fmin) / (fmax - fmin || 1)) * (w - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    const bands = [[4, "θ"], [8, "α"], [13, "β"], [30, "γ"]];
    g.fillStyle = cssVar("--muted", "#777");
    for (const [f, name] of bands) {
      if (f > fmin && f < fmax) {
        g.strokeStyle = cssVar("--grid", "#ddd");
        g.beginPath(); g.moveTo(X(f), pad.t); g.lineTo(X(f), h - pad.b); g.stroke();
        g.fillText(name, X(f) + 3, h - 5);
      }
    }
    g.strokeStyle = palette()[0];
    g.lineWidth = 1.5;
    g.beginPath();
    row.forEach((v, i) => { const x = X(freqs[i]), y = Y(v); i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
    g.fillText(`${fmt(fmax)} Hz`, w - pad.r - 44, h - 5);
  };

  R.bars = function (container, spec, store) {
    const s = store.streams[spec.streams[0]];
    if (!s || !s.data.length) return empty(container, "waiting for data…");
    container.querySelector(".empty")?.remove();
    const row = s.data[s.data.length - 1];
    const chans = selectChannels(spec, s.info);
    let list = container.querySelector(".bars");
    if (!list) { list = el("div", "bars"); container.appendChild(list); }
    const lo = spec.y?.min ?? 0;
    const hi = spec.y?.max ?? Math.max(1e-9, ...chans.map(([, i]) => row[i] ?? 0));
    const unit = spec.y?.unit || "";
    list.replaceChildren(...chans.map(([name, i], k) => {
      const v = row[i];
      const frac = v == null || !isFinite(v) ? 0 : Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
      const lvl = v == null ? null : levelFor(spec, v);
      const line = el("div", "bar-row");
      const fill = el("div", "bar-fill");
      fill.style.width = (frac * 100).toFixed(1) + "%";
      fill.style.background = lvl ? levelColor(lvl) : palette()[k % 8];
      const track = el("div", "bar-track");
      track.appendChild(fill);
      line.append(el("span", "bar-label", name), track, el("span", "bar-value", fmt(v) + (unit ? " " + unit : "")));
      return line;
    }));
  };

  R.metric = function (container, spec, store) {
    const s = store.streams[spec.streams[0]];
    if (!s || !s.data.length) return empty(container, "waiting for data…");
    container.querySelector(".empty")?.remove();
    const row = s.data[s.data.length - 1];
    let box = container.querySelector(".metrics");
    if (!box) { box = el("div", "metrics"); container.appendChild(box); }
    const pct = spec.y?.unit === "%";
    box.replaceChildren(...s.info.channels.map((name, i) => {
      const v = row[i];
      const lvl = v == null ? null : levelFor(spec, v);
      const m = el("div", "metric");
      const val = el("div", "metric-value", pct && v != null ? Math.round(v * 100) + "%" : fmt(v));
      if (lvl) val.style.color = levelColor(lvl);
      m.append(val, el("div", "metric-label", s.info.channels.length > 1 ? name : spec.title));
      return m;
    }));
  };

  R.table = function (container, spec, store) {
    const s = store.streams[spec.streams[0]];
    if (!s || !s.data.length) return empty(container, "waiting for data…");
    container.querySelector(".empty")?.remove();
    const row = s.data[s.data.length - 1];
    const t = el("table", "kv");
    for (const [name, i] of selectChannels(spec, s.info)) {
      const tr = el("tr");
      tr.append(el("th", null, name), el("td", null, fmt(row[i])));
      t.appendChild(tr);
    }
    container.replaceChildren(t);
  };

  R.events = function (container, spec, store) {
    const prefixes = spec.event_types || [];
    const evs = (store.events || []).filter((e) => !prefixes.length || prefixes.some((p) => e.type.startsWith(p)));
    if (!evs.length) return empty(container, "no events yet");
    const ul = el("ul", "events");
    for (const e of evs.slice(-12).reverse()) {
      const li = el("li");
      const time = new Date(e.t * 1000).toLocaleTimeString();
      const extra = e.data && e.data.label ? ` · ${e.data.label}` : e.duration ? ` · ${e.duration.toFixed(2)} s` : "";
      li.append(el("span", "ev-time", time), el("span", "ev-type", e.type), el("span", "ev-extra", extra));
      ul.appendChild(li);
    }
    container.replaceChildren(ul);
  };

  /** Paragraphs carried by events (data.text), newest first: e.g. the session description. */
  R.text = function (container, spec, store) {
    const prefixes = spec.event_types || [];
    const evs = (store.events || []).filter((e) => e.data && e.data.text &&
      (!prefixes.length || prefixes.some((p) => e.type.startsWith(p))));
    if (!evs.length) return empty(container, "nothing to describe yet");
    const limit = spec.limit || (spec.window_s >= 1e8 ? evs.length : 20);
    const list = el("div", "text-view" + (limit === 1 ? " latest" : ""));
    for (const e of evs.slice(-limit).reverse()) {
      const item = el("div", "text-item");
      const t0 = new Date(e.t * 1000).toLocaleTimeString();
      const t1 = e.duration ? "–" + new Date((e.t + e.duration) * 1000).toLocaleTimeString() : "";
      item.append(el("div", "ev-time", t0 + t1), el("p", null, e.data.text));
      list.appendChild(item);
    }
    container.replaceChildren(list);
  };

  function render(container, spec, store) {
    const fn = R[spec.type];
    if (fn) fn(container, spec, store);
    else empty(container, `no renderer for "${spec.type}"`);
  }

  /** Rolling buffers fed by WebSocket messages. */
  class Store {
    constructor(maxSeconds = 300) {
      this.streams = {};
      this.events = [];
      this.maxSeconds = maxSeconds;
    }
    declare(infos) {
      for (const info of infos) if (!this.streams[info.id]) this.streams[info.id] = { info, t: [], data: [] };
    }
    add(msg) {
      if (msg.type === "event") {
        this.events.push(msg.event);
        if (this.events.length > 500) this.events.splice(0, this.events.length - 500);
      } else if (msg.type === "chunk") {
        const s = this.streams[msg.stream];
        if (!s) return;
        for (let i = 0; i < msg.t.length; i++) { s.t.push(msg.t[i]); s.data.push(msg.data[i]); }
        const cutoff = s.t[s.t.length - 1] - this.maxSeconds;
        let k = 0;
        while (k < s.t.length && s.t[k] < cutoff) k++;
        if (k) { s.t.splice(0, k); s.data.splice(0, k); }
      }
    }
  }

  /* ---------- info icon: an "i" button that explains the element next to it ----------
     Click or tap pins the explanation; hovering (mouse) or keyboard focus previews it.
     Escape, clicking elsewhere, scrolling or resizing closes it. The popover is positioned in the
     viewport so it never causes horizontal scrolling on narrow screens. */
  let open = null;
  let seq = 0;

  function closeInfo() {
    if (!open) return;
    open.btn.setAttribute("aria-expanded", "false");
    open.btn.removeAttribute("aria-describedby");
    open.pop.remove();
    open = null;
  }

  function showInfo(btn, text, pinned) {
    if (open && open.btn === btn) { open.pinned = open.pinned || pinned; return; }
    closeInfo();
    const pop = el("div", "info-pop", text);
    pop.id = `info-pop-${++seq}`;
    pop.setAttribute("role", "tooltip");
    document.body.appendChild(pop);
    const r = btn.getBoundingClientRect();
    const w = pop.offsetWidth, ph = pop.offsetHeight;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8));
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8 && r.top - ph - 6 > 8) top = r.top - ph - 6;
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-describedby", pop.id);
    open = { btn, pop, pinned };
  }

  function info(text, label) {
    const btn = el("button", "info-btn", "i");
    btn.type = "button";
    btn.setAttribute("aria-label", label ? `About ${label}` : "More information");
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (open && open.btn === btn && open.pinned) closeInfo();
      else showInfo(btn, text, true);
    });
    btn.addEventListener("focus", () => showInfo(btn, text, false));
    btn.addEventListener("blur", () => { if (open && open.btn === btn) closeInfo(); });
    btn.addEventListener("mouseenter", () => {
      if (window.matchMedia("(hover: hover)").matches) showInfo(btn, text, false);
    });
    btn.addEventListener("mouseleave", () => { if (open && open.btn === btn && !open.pinned) closeInfo(); });
    return btn;
  }

  document.addEventListener("click", (e) => { if (open && !open.pop.contains(e.target)) closeInfo(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeInfo(); });
  window.addEventListener("scroll", closeInfo, true);
  window.addEventListener("resize", closeInfo);
  window.addEventListener("hashchange", closeInfo);

  /** A heading with an optional info icon. */
  function heading(tag, text, help) {
    const hd = el(tag, "with-info", text);
    if (help) hd.appendChild(info(help, text));
    return hd;
  }

  window.MuseRenderers = { render, Store, fmt, info, heading, renderers: R };
})();
