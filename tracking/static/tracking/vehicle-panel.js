/* Graphical vehicle panel: speedometer, dials, compass, tiles, status lights, trends and every parameter
   with its own chart. Used as a drawer on the live map and inline on the device page.

     const panel = VehiclePanel.create(element, {onClose});
     panel.show(deviceId);   // loads and keeps refreshing every Fleet.cfg.poll seconds
     panel.hide();
*/
(function () {
  const F = window.Fleet;
  const esc = F.esc;
  const NS_W = 300;

  /* ---------- geometry ---------- */
  const SWEEP = 120;
  const polar = (cx, cy, r, a) => [cx + r * Math.sin(a * Math.PI / 180), cy - r * Math.cos(a * Math.PI / 180)];
  const arcPath = (cx, cy, r, a0, a1) => {
    const [x0, y0] = polar(cx, cy, r, a0), [x1, y1] = polar(cx, cy, r, a1);
    return `M${x0.toFixed(1)},${y0.toFixed(1)} A${r},${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(1)},${y1.toFixed(1)}`;
  };
  const GREEN = "var(--moving)", AMBER = "var(--idling)", RED = "var(--parked)";
  const DIALS = {
    speed: {min: 0, max: 140, major: 20, minor: 10, zones: [[0, 80, GREEN], [80, 120, AMBER], [120, 140, RED]]},
    temp: {min: -10, max: 80, major: 30, minor: 10, zones: [[-10, 45, GREEN], [45, 60, AMBER], [60, 80, RED]]},
    volt: {min: 8, max: 32, major: 4, minor: 2, zones: [[8, 11.5, RED], [11.5, 12, AMBER], [12, 28.8, GREEN], [28.8, 32, RED]]},
  };
  const dialAngle = (d, v) => -SWEEP + (Math.min(d.max, Math.max(d.min, v)) - d.min) / (d.max - d.min) * SWEEP * 2;
  const zoneOf = (d, v) => (d.zones.find(([a, b]) => v >= a && v <= b) || d.zones[d.zones.length - 1])[2];

  function dialSvg(uid, key, big) {
    const d = DIALS[key], W = big ? 240 : 130, cx = W / 2, cy = big ? 118 : 64, r = big ? 98 : 50, w = big ? 13 : 9, fs = big ? 11 : 8.5;
    let o = `<svg viewBox="0 0 ${W} ${big ? 178 : 112}" class="vp-dial" role="img" aria-label="${key} gauge">`;
    o += `<path d="${arcPath(cx, cy, r, -SWEEP, SWEEP)}" stroke="var(--line)" stroke-width="${w}" fill="none"/>`;
    d.zones.forEach(([a, b, c]) => o += `<path d="${arcPath(cx, cy, r, dialAngle(d, a), dialAngle(d, b))}" stroke="${c}" stroke-width="${w}" fill="none"/>`);
    for (let t = d.min; t <= d.max + 1e-9; t += d.minor) {
      const major = Math.abs((t - d.min) / d.major - Math.round((t - d.min) / d.major)) < 1e-6, a = dialAngle(d, t);
      const [x0, y0] = polar(cx, cy, r - w / 2 - 2, a), [x1, y1] = polar(cx, cy, r - w / 2 - (major ? (big ? 11 : 7) : (big ? 6 : 4)), a);
      o += `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}" stroke="var(--muted)" stroke-width="${major ? 1.6 : 1}"/>`;
      if (major) {
        const [lx, ly] = polar(cx, cy, r - w / 2 - (big ? 23 : 15), a);
        o += `<text x="${lx.toFixed(1)}" y="${(ly + fs / 3).toFixed(1)}" font-size="${fs}" text-anchor="middle" class="vp-tick">${t}</text>`;
      }
    }
    o += `<g class="vp-needle" data-n="${uid}-${key}" style="transform-origin:${cx}px ${cy}px; transform:rotate(${-SWEEP}deg)"><path d="M${cx - (big ? 4 : 3)},${cy} L${cx},${cy - r + w / 2 + 3} L${cx + (big ? 4 : 3)},${cy} Z" fill="var(--ink)"/></g>`;
    o += `<circle cx="${cx}" cy="${cy}" r="${big ? 8 : 5}" fill="var(--ink)"/><circle cx="${cx}" cy="${cy}" r="${big ? 3 : 2}" fill="var(--panel)"/>`;
    o += `<text x="${cx}" y="${cy + 44}" text-anchor="middle" class="vp-val" font-size="${big ? 30 : 17}" data-r="${uid}-${key}">—</text>`;
    if (big) o += `<text x="${cx}" y="${cy + 59}" text-anchor="middle" class="vp-tick" font-size="11">km/h</text>`;
    return o + "</svg>";
  }
  function compassSvg(uid) {
    let o = `<svg viewBox="0 0 130 100" class="vp-dial" role="img" aria-label="Heading compass"><circle cx="65" cy="50" r="40" fill="var(--panel-2)" stroke="var(--line)" stroke-width="2"/>`;
    for (let a = 0; a < 360; a += 30) {
      const [x0, y0] = polar(65, 50, 39, a), [x1, y1] = polar(65, 50, a % 90 ? 35 : 32, a);
      o += `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}" stroke="var(--muted)" stroke-width="${a % 90 ? 1 : 1.6}"/>`;
    }
    [["N", 0], ["E", 90], ["S", 180], ["W", 270]].forEach(([l, a]) => {
      const [x, y] = polar(65, 50, 25, a);
      o += `<text x="${x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="middle" font-size="10" class="${l === "N" ? "vp-north" : "vp-tick"}">${l}</text>`;
    });
    o += `<g class="vp-needle" data-n="${uid}-head" style="transform-origin:65px 50px"><path d="M65,16 L71,50 L65,46 L59,50 Z" fill="var(--parked)"/><path d="M65,84 L71,50 L65,54 L59,50 Z" fill="var(--muted)" opacity=".5"/></g><circle cx="65" cy="50" r="3.5" fill="var(--ink)"/></svg>`;
    return o;
  }
  const CARD = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const cardinal = a => CARD[Math.round(a / 45) % 8];
  const timeOnly = iso => iso ? F.fmt(iso).slice(11, 16) : "";
  const dur = s => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); return h ? `${h} h ${m} min` : `${m} min`; };
  const num = (v, dec) => v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(dec);

  /* ---------- charts ---------- */
  function areaChart(title, pts, key, {unit, dec = 0, min, max, color, line, lineLabel, span}) {
    const P = pts.filter(p => p[key] != null);
    if (P.length < 2) return `<div class="vp-trend"><div class="vp-trend-h"><span>${title}</span><b>—</b></div><div class="text-muted small">Not enough data in the last ${span}</div></div>`;
    const vals = P.map(p => p[key]);
    const lo = Math.min(min ?? Infinity, ...vals), hi = Math.max(max ?? -Infinity, ...vals), pad = (hi - lo) * .08 || 1, y0 = lo - pad, y1 = hi + pad;
    const t = P.map(p => Date.parse(p.ts)), t0 = t[0], t1 = t[t.length - 1] || t0 + 1, W = NS_W, H = 64;
    const X = x => ((x - t0) / Math.max(1, t1 - t0) * W).toFixed(1), Y = v => (H - (v - y0) / (y1 - y0) * H).toFixed(1);
    const pl = P.map((p, i) => `${X(t[i])},${Y(p[key])}`).join(" ");
    const cur = vals[vals.length - 1];
    const ref = line != null && line > y0 && line < y1 ? `<line x1="0" x2="${W}" y1="${Y(line)}" y2="${Y(line)}" stroke="var(--parked)" stroke-dasharray="4 3" stroke-width="1" vector-effect="non-scaling-stroke"/>` : "";
    return `<div class="vp-trend"><div class="vp-trend-h"><span>${title}</span><b style="color:${color}">${num(cur, dec)} <small>${unit}</small></b></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="vp-trend-svg" role="img" aria-label="${title} trend">
        <polygon points="0,${H} ${pl} ${W},${H}" fill="${color}" opacity=".14"/>${ref}
        <polyline points="${pl}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>
      <div class="vp-trend-f"><span>${timeOnly(P[0].ts)}</span><span>min ${num(Math.min(...vals), dec)} · max ${num(Math.max(...vals), dec)}${ref ? ` · <span style="color:var(--parked)">- - ${lineLabel}</span>` : ""}</span><span>${timeOnly(P[P.length - 1].ts)}</span></div></div>`;
  }

  // Interactive history chart for one parameter (hover shows time and value).
  function historyChart(host, data, labels) {
    const pts = data.points.map(([ts, v]) => [Date.parse(ts), v]);
    if (pts.length < 1) { host.innerHTML = `<div class="text-muted small py-3 text-center">No values in this period.</div>`; return; }
    const W = 560, H = 190, L = 50, B = 22, vals = pts.map(p => p[1]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * .08; lo -= pad; hi += pad;
    const t0 = pts[0][0], t1 = Math.max(pts[pts.length - 1][0], t0 + 1);
    const X = t => L + (t - t0) / (t1 - t0) * (W - L - 6), Y = v => 6 + (1 - (v - lo) / (hi - lo)) * (H - B - 6);
    const step = labels || vals.every(v => Number.isInteger(v)) && new Set(vals).size <= 6;   // on/off style values
    let d = "";
    pts.forEach(([t, v], i) => {
      if (!i) d += `M${X(t).toFixed(1)},${Y(v).toFixed(1)}`;
      else d += step ? `H${X(t).toFixed(1)}V${Y(v).toFixed(1)}` : `L${X(t).toFixed(1)},${Y(v).toFixed(1)}`;
    });
    const ticks = [0, .25, .5, .75, 1].map(f => lo + f * (hi - lo));
    const fmtV = v => labels && labels[Math.round(v)] != null ? labels[Math.round(v)] : (Math.abs(v) >= 100 ? v.toFixed(0) : +v.toFixed(3));
    let o = `<svg viewBox="0 0 ${W} ${H}" class="vp-hist-svg" role="img" aria-label="${esc(data.name)} history">`;
    ticks.forEach(v => o += `<line x1="${L}" x2="${W - 6}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--line)"/><text x="${L - 6}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="12" class="vp-tick">${esc(String(fmtV(v)))}</text>`);
    [0, .5, 1].forEach(f => { const t = t0 + f * (t1 - t0); o += `<text x="${X(t).toFixed(1)}" y="${H - 4}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}" font-size="12" class="vp-tick">${F.fmt(new Date(t).toISOString()).slice(0, 16)}</text>`; });
    o += `<path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>`;
    if (pts.length === 1) o += `<circle cx="${X(pts[0][0])}" cy="${Y(pts[0][1])}" r="3" fill="var(--accent)"/>`;
    o += `<line class="vp-cross" x1="0" x2="0" y1="6" y2="${H - B}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/><circle class="vp-dot" r="4" fill="var(--accent)" visibility="hidden"/></svg>
      <div class="vp-hover small text-muted">Hover the chart to read values.</div>`;
    host.innerHTML = o;
    const svg = host.querySelector("svg"), cross = svg.querySelector(".vp-cross"), dot = svg.querySelector(".vp-dot"), info = host.querySelector(".vp-hover");
    svg.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * W;
      const t = t0 + (x - L) / (W - L - 6) * (t1 - t0);
      let best = 0;
      for (let i = 1; i < pts.length; i++) if (Math.abs(pts[i][0] - t) < Math.abs(pts[best][0] - t)) best = i;
      const [bt, bv] = pts[best];
      cross.setAttribute("x1", X(bt)); cross.setAttribute("x2", X(bt)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", X(bt)); dot.setAttribute("cy", Y(bv)); dot.setAttribute("visibility", "visible");
      info.innerHTML = `<b>${esc(String(fmtV(bv)))} ${esc(data.unit || "")}</b> at ${F.fmt(new Date(bt).toISOString())}`;
    });
    svg.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
  }

  /* ---------- parameter groups ---------- */
  const GROUPS = [
    ["Position and GNSS", ["speed", "altitude", "satellites", "angle", 69, 181, 182, 24]],
    ["Ignition, inputs and outputs", [239, 240, 1, 2, 3, 179, 180, 380, 9, 6, 78]],
    ["Power", [66, 67, 68, 113, 252]],
    ["Sensors", [72, 73, 74, 75, 17, 18, 19]],
    ["Distance and fuel", [16, 199, 12, 13, 15]],
    ["Network", [21, 241, 80, 205, 206, 11, 14, 200]],
    ["Events", [246, 247, 249, 250, 251, 253, 255]],
  ];
  const groupOf = key => { for (const [g, ids] of GROUPS) if (ids.includes(isNaN(+key) ? key : +key)) return g; return "Other"; };

  let seq = 0;
  function create(root, opts = {}) {
    const uid = "vp" + (++seq);
    let deviceId = null, timer = null, data = null, built = null, openParam = null, range = 24;
    const q = sel => root.querySelector(sel);
    const needle = (key, deg) => {
      const n = root.querySelector(`[data-n="${uid}-${key}"]`); if (!n) return;
      const prev = +(n.dataset.deg ?? deg); let next = deg;
      if (key === "head") next = prev + ((deg - prev + 540) % 360 - 180);   // turn the short way round
      n.dataset.deg = next; n.style.transform = `rotate(${next}deg)`;
    };
    const readout = (key, text, color) => { const r = root.querySelector(`[data-r="${uid}-${key}"]`); if (r) { r.textContent = text; r.style.fill = color || "var(--ink)"; } };

    function build(d) {
      const dev = d.device;
      root.innerHTML = `
        <header class="vp-head"><div class="min-w-0"><h5 class="mb-0 text-truncate">${esc(dev.name)}</h5>
          <div class="d-flex gap-2 align-items-center flex-wrap mt-1"><span class="vp-pill"></span><span class="small text-muted">${esc(dev.plate || "")}${dev.plate ? " · " : ""}${esc(dev.group)}</span></div></div>
          <div class="d-flex gap-2 align-items-start flex-shrink-0"><a class="btn btn-sm btn-primary vp-dash" href="/vehicles/${dev.id}/" title="Daily distance, driver performance and trips"><i class="bi bi-bar-chart-line"></i> Dashboard</a>
          ${opts.onClose ? `<button class="btn btn-sm btn-outline-secondary vp-close" aria-label="Close vehicle panel"><i class="bi bi-x-lg"></i></button>` : ""}</div></header>
        <div class="vp-body">
          <div class="vp-stale alert alert-warning py-1 px-2 small" hidden></div>
          <div class="vp-empty text-muted small" hidden>This vehicle has not sent any data yet. Configure the FMC130 with this server and port 5027 (TCP); its gauges appear here as soon as the first record arrives.</div>
          <div class="vp-main">
            <div class="vp-speedo">${dialSvg(uid, "speed", true)}<div class="vp-side">
              <div class="vp-stat"><span>Trip today</span><b class="vp-trip">—</b></div>
              <div class="vp-stat"><span>Odometer</span><b class="vp-odo">—</b></div>
              <div class="vp-stat"><span>Moving today</span><b class="vp-moving">—</b></div></div></div>
            <div class="vp-dials">
              <figure class="vp-temp-fig">${dialSvg(uid, "temp")}<figcaption>Temperature <span class="vp-avl">#72</span></figcaption></figure>
              <figure>${dialSvg(uid, "volt")}<figcaption>Vehicle battery <span class="vp-avl">#66</span></figcaption></figure>
              <figure>${compassSvg(uid)}<figcaption class="vp-headcap">Heading</figcaption></figure>
            </div>
            <div class="vp-tiles"></div>
            <div class="vp-leds"></div>
            <h6 class="vp-h">Trends · last 30 minutes</h6><div class="vp-trends"></div>
          </div>
          <h6 class="vp-h d-flex justify-content-between align-items-center">All parameters <span class="small text-muted fw-normal vp-asof"></span></h6>
          <div class="small text-muted mb-1">Click any parameter to chart its history. Stored for ${d.retention_days} days.</div>
          <div class="vp-params"></div>
          <div class="d-flex gap-2 mt-3 flex-wrap">
            <a class="btn btn-sm btn-primary" href="/vehicles/${dev.id}/"><i class="bi bi-bar-chart-line"></i> Vehicle dashboard</a>
            <a class="btn btn-sm btn-outline-primary" href="/history/?device=${dev.id}"><i class="bi bi-signpost-split"></i> Route history</a>
            <a class="btn btn-sm btn-outline-secondary" href="/devices/${dev.id}/"><i class="bi bi-cpu"></i> Device</a>
            ${F.cfg.raw ? `<a class="btn btn-sm btn-outline-secondary" href="/raw/?device=${dev.id}"><i class="bi bi-file-binary"></i> Raw packets</a>` : ""}
          </div>
          <p class="small text-muted mt-2 mb-0">Gauge colours: green normal, amber watch, red act. Temperature needs a Dallas 1-Wire probe on the FMC130.</p>
        </div>`;
      if (opts.onClose) q(".vp-close").onclick = opts.onClose;
      q(".vp-params").addEventListener("click", e => {
        const row = e.target.closest("[data-key]"); if (!row) return;
        const rb = e.target.closest("[data-range]");
        if (rb) { range = +rb.dataset.range; loadParam(openParam); return; }
        if (e.target.closest(".vp-chart")) return;
        openParam = openParam === row.dataset.key ? null : row.dataset.key;
        renderParams(); if (openParam) loadParam(openParam);
      });
      built = dev.id;
    }

    function renderParams() {
      const d = data, l = d.latest, box = q(".vp-params");
      if (!l) { box.innerHTML = ""; return; }
      const rows = [
        {key: "speed", name: "GNSS speed", text: `${l.speed} km/h`},
        {key: "altitude", name: "Altitude", text: `${l.alt} m`},
        {key: "satellites", name: "Satellites", text: String(l.sats)},
        {key: "angle", name: "Heading", text: `${l.angle}° ${cardinal(l.angle)}`},
        ...l.io.map(e => ({key: String(e.id), id: e.id, name: e.name, text: e.text || (e.value ?? "—"), known: e.known})),
      ];
      const byGroup = {};
      rows.forEach(r => (byGroup[groupOf(r.key)] = byGroup[groupOf(r.key)] || []).push(r));
      const order = [...GROUPS.map(g => g[0]), "Other"];
      let html = "";
      for (const g of order) {
        if (!byGroup[g]) continue;
        html += `<div class="vp-group">${g}</div>`;
        for (const r of byGroup[g]) {
          const open = openParam === r.key;
          html += `<div class="vp-param ${open ? "open" : ""}" data-key="${esc(r.key)}" role="button" tabindex="0">
            <span class="vp-pname">${esc(r.name)}${r.id != null ? ` <span class="vp-avl">#${r.id}</span>` : ""}${r.known === false ? ' <span class="badge text-bg-secondary">unknown</span>' : ""}</span>
            <span class="vp-pval">${esc(String(r.text))}</span><i class="bi ${open ? "bi-chevron-up" : "bi-graph-up"} vp-pico"></i>
            ${open ? `<div class="vp-chart"><div class="btn-group btn-group-sm mb-2">${[[1, "1 h"], [6, "6 h"], [24, "24 h"], [168, "7 days"], [720, "30 days"], [1440, "60 days"]].map(([h, lab]) => `<button type="button" class="btn ${h === range ? "btn-primary" : "btn-outline-secondary"}" data-range="${h}">${lab}</button>`).join("")}</div>
              <div class="vp-chart-stats small text-muted"></div><div class="vp-chart-body"><div class="text-muted small py-3 text-center">Loading…</div></div></div>` : ""}
          </div>`;
        }
      }
      box.innerHTML = html;
    }

    async function loadParam(key) {
      if (!key) return;
      renderParams();
      const body = q(".vp-param.open .vp-chart-body"); if (!body) return;
      try {
        const r = await F.json(`/api/vehicle/${deviceId}/param/${encodeURIComponent(key)}/?hours=${range}`);
        if (openParam !== key) return;
        const cat = (data.parameters || []).find(p => p.key === key);
        const fmt = v => v == null ? "—" : (Math.abs(v) >= 100 ? v.toFixed(0) : +v.toFixed(3));
        q(".vp-param.open .vp-chart-stats").innerHTML = r.count ? `${r.count} values · min <b>${fmt(r.min)}</b> · avg <b>${fmt(r.avg)}</b> · max <b>${fmt(r.max)}</b> ${esc(r.unit || "")}` : "";
        historyChart(q(".vp-param.open .vp-chart-body"), r, cat && cat.labels);
      } catch (e) { body.innerHTML = `<div class="text-danger small">Could not load: ${esc(e.message)}</div>`; }
    }

    function render() {
      const d = data, dev = d.device, l = d.latest;
      if (built !== dev.id) build(d);
      q(".vp-pill").outerHTML = `<span class="vp-pill badge badge-state bg-${dev.state}">${F.stateLabel[dev.state]}</span>`;
      const stale = q(".vp-stale");
      if (l && dev.link !== "online") { stale.hidden = false; stale.textContent = `No data for ${F.ago(dev.last_packet).replace(" ago", "")}. Showing the last values the vehicle sent.`; }
      else stale.hidden = true;
      q(".vp-empty").hidden = !!l;
      q(".vp-main").hidden = !l;
      q(".vp-asof").textContent = l ? `as of ${F.fmt(l.ts)}` : "";
      if (!l) { renderParams(); return; }
      const io = id => l.io.find(e => e.id === id);
      const v = id => { const e = io(id); return e ? e.value : null; };
      const speed = dev.link === "online" || dev.link === "stale" ? l.speed : 0;
      needle("speed", dialAngle(DIALS.speed, speed));
      readout("speed", Math.round(speed), speed > 80 ? zoneOf(DIALS.speed, speed) : null);
      const temp = v(72);
      q(".vp-temp-fig").classList.toggle("vp-off", temp == null);
      if (temp != null) { needle("temp", dialAngle(DIALS.temp, temp)); readout("temp", temp.toFixed(1) + "°", zoneOf(DIALS.temp, temp)); }
      else readout("temp", "no sensor");
      const volt = v(66) ?? dev.ext_v;
      if (volt != null) { needle("volt", dialAngle(DIALS.volt, volt)); readout("volt", volt.toFixed(1) + " V", zoneOf(DIALS.volt, volt)); }
      needle("head", l.angle);
      q(".vp-headcap").innerHTML = `Heading <b>${l.angle}° ${cardinal(l.angle)}</b>`;
      q(".vp-trip").textContent = d.today.distance_km.toFixed(1) + " km";
      q(".vp-odo").textContent = d.odometer_km != null ? String(Math.floor(d.odometer_km)).padStart(6, "0") : "—";
      q(".vp-odo").className = "vp-odo" + (d.odometer_km != null ? " vp-odo-digits" : "");
      q(".vp-moving").textContent = dur(d.today.moving_s);

      const gsm = v(21) ?? dev.gsm ?? 0, sats = l.sats, bl = v(113), bv = v(67), op = io(241);
      const hdop = v(182), gnss = io(69);
      q(".vp-tiles").innerHTML = `
        <div class="vp-tile"><span>GSM signal</span><div class="vp-sig">${[1, 2, 3, 4, 5].map(i => `<i class="${i <= gsm ? "on" : ""}" style="height:${4 + i * 4}px"></i>`).join("")}</div><b>${gsm}/5${op ? ` · ${esc(op.text)}` : ""}</b></div>
        <div class="vp-tile"><span>Satellites</span><svg viewBox="0 0 40 40" class="vp-ring"><circle cx="20" cy="20" r="16" stroke="var(--line)"/><circle cx="20" cy="20" r="16" stroke="${sats >= 7 ? GREEN : sats >= 4 ? AMBER : RED}" stroke-dasharray="${(Math.min(sats, 16) / 16 * 100.5).toFixed(1)} 200" transform="rotate(-90 20 20)"/><text x="20" y="24" text-anchor="middle" font-size="12">${sats}</text></svg><b>${l.valid_fix ? "Fix OK" : "No fix"}${hdop != null ? ` · HDOP ${hdop}` : ""}</b></div>
        <div class="vp-tile"><span>Backup battery</span>${bl != null ? `<div class="vp-batt"><i style="width:${Math.min(100, bl)}%; background:${bl > 30 ? GREEN : bl > 15 ? AMBER : RED}"></i></div>` : `<div class="vp-batt"><i style="width:0"></i></div>`}<b>${bl != null ? bl + "%" : "—"}${bv != null ? ` · ${bv.toFixed(2)} V` : ""}</b></div>
        <div class="vp-tile"><span>Altitude</span><div class="vp-big">${l.alt}<small> m</small></div><b>${gnss ? esc(gnss.text) : "GNSS"}</b></div>`;
      const led = (label, on, avl, warn) => `<span class="vp-led ${on ? (warn ? "warn" : "on") : ""}" title="${avl ? "AVL #" + avl : ""}"><i></i>${label}</span>`;
      const has = id => io(id) != null;
      let leds = led("Ignition", v(239) ?? dev.ignition, 239) + led("Moving", (v(240) ?? dev.movement) && speed > 3, 240)
        + led("GNSS fix", l.valid_fix, 69) + led("Over 80", speed > 80, 24, true);
      [[1, "DIN1"], [2, "DIN2"], [3, "DIN3"], [179, "DOUT1"], [180, "DOUT2"]].forEach(([id, lab]) => { if (has(id)) leds += led(lab, v(id), id); });
      if (has(249)) leds += led("Jamming", v(249), 249, true);
      if (has(252)) leds += led("Battery unplugged", v(252), 252, true);
      if (has(246)) leds += led("Towing", v(246), 246, true);
      q(".vp-leds").innerHTML = leds;

      const pts = d.trends.points;
      const span = "30 minutes";
      q(".vp-trends").innerHTML = areaChart("Speed", pts, "speed", {unit: "km/h", min: 0, max: 90, color: "var(--accent)", line: 80, lineLabel: "80 km/h", span})
        + (pts.some(p => p.temp != null) ? areaChart("Temperature", pts, "temp", {unit: "°C", dec: 1, color: "var(--idling)", line: 45, lineLabel: "45 °C alert", span}) : "")
        + areaChart("Vehicle battery", pts, "ext_v", {unit: "V", dec: 2, color: "var(--moving)", line: 11.8, lineLabel: "11.8 V low", span});
      if (!q(".vp-param.open")) renderParams();
      else {  // keep an open chart; only refresh the values
        const vals = Object.fromEntries(l.io.map(e => [String(e.id), e.text]));
        root.querySelectorAll(".vp-param:not(.open)").forEach(r => { if (vals[r.dataset.key] != null) r.querySelector(".vp-pval").textContent = vals[r.dataset.key]; });
      }
    }

    async function refresh() {
      if (deviceId == null) return;
      const id = deviceId;
      try {
        const d = await F.json(`/api/vehicle/${id}/`);
        if (id !== deviceId) return;
        data = d;
        render();
      } catch (e) {
        if (built == null) root.innerHTML = `<div class="p-3 text-danger small">Could not load vehicle: ${esc(e.message)}</div>`;
      }
    }

    return {
      show(id) {
        if (id !== deviceId) { deviceId = id; built = null; openParam = null; root.innerHTML = `<div class="p-4 text-muted small">Loading…</div>`; }
        root.hidden = false;
        clearInterval(timer);
        refresh();
        timer = setInterval(refresh, (F.cfg.poll || 5) * 1000);
      },
      hide() { clearInterval(timer); timer = null; deviceId = null; built = null; root.hidden = true; },
      get deviceId() { return deviceId; },
    };
  }
  window.VehiclePanel = {create};
})();
