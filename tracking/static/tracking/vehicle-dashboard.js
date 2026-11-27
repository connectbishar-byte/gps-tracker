/* Vehicle dashboard: charts drawn as inline SVG from the numbers embedded in the page (#vdData). */
(function () {
  "use strict";
  const F = window.Fleet, esc = F.esc;
  const D = JSON.parse(document.getElementById("vdData").textContent);
  const root = document.getElementById("vd");
  const tip = document.getElementById("vdTip");
  const NS = "http://www.w3.org/2000/svg";
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const RATING = {good: "Good", fair: "Fair", aggressive: "Aggressive"};
  const css = n => getComputedStyle(root).getPropertyValue(n).trim();

  const day = iso => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
  const dLabel = iso => { const t = day(iso); return `${t.getDate()} ${MON[t.getMonth()]}`; };
  const dLong = iso => { const t = day(iso); return `${WD[t.getDay()]} ${t.getDate()} ${MON[t.getMonth()]} ${t.getFullYear()}`; };
  const dur = s => { if (s == null) return "—"; const m = Math.round(s / 60), h = Math.floor(m / 60); return h ? `${h} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`; };
  const n1 = v => (v == null ? "—" : (+v).toLocaleString(undefined, {maximumFractionDigits: 1}));

  /* ---------- tooltip ---------- */
  function showTip(html, ev) {
    tip.innerHTML = html; tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = ev.clientX + 14, y = ev.clientY - r.height - 10;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
    if (y < 8) y = ev.clientY + 16;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  /* ---------- svg helpers ---------- */
  function el(name, attrs, parent) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function text(parent, x, y, s, attrs = {}) { const t = el("text", Object.assign({x, y}, attrs), parent); t.textContent = s; return t; }
  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  // A bar with a 4px rounded top, square at the baseline.
  function barPath(x, y, w, h, r = 4) {
    if (h <= 0) return "";
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  function frame(host, padL = 38) {
    host.innerHTML = "";
    const W = host.clientWidth || 600, H = host.clientHeight || 240;
    const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img"}, host);
    return {svg, W, H, L: padL, R: W - 8, T: 14, B: H - 24};
  }
  // Tick count that gives round steps for the maxima niceMax produces (1, 2, 2.5, 5, 10 × 10^n).
  function ticksFor(max) { const f = max / Math.pow(10, Math.floor(Math.log10(max))); return f === 2 ? 4 : 5; }
  function yAxis(f, max, ticks, fmt) {
    if (ticks == null) ticks = ticksFor(max);
    for (let i = 0; i <= ticks; i++) {
      const v = max * i / ticks, y = f.B - (f.B - f.T) * i / ticks;
      el("line", {x1: f.L, x2: f.R, y1: y, y2: y, class: i ? "grid" : "base"}, f.svg);
      text(f.svg, f.L - 6, y + 4, fmt(v), {"text-anchor": "end"});
    }
  }
  function xLabels(f, rows, x0, step, fmt) {
    const every = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor((f.R - f.L) / 54))));
    rows.forEach((r, i) => { if (i % every === 0) text(f.svg, x0 + step * i + step / 2, f.H - 6, fmt(r), {"text-anchor": "middle"}); });
  }
  function empty(host, msg) { host.innerHTML = `<div class="vd-empty">${esc(msg)}</div>`; }
  // Invisible full-height hit column for each x step, so small bars are easy to hover.
  function hitCol(f, x, w, onTip) {
    const g = el("g", {}, f.svg);
    el("rect", {x, y: f.T, width: w, height: f.B - f.T, class: "col-hover"}, g);
    const hit = el("rect", {x, y: f.T, width: w, height: f.B - f.T + 20, class: "hit"}, g);
    hit.addEventListener("mousemove", ev => { g.classList.add("on"); showTip(onTip(), ev); });
    hit.addEventListener("mouseleave", () => { g.classList.remove("on"); hideTip(); });
    return g;
  }

  /* ---------- daily distance ---------- */
  function kmChart() {
    const host = document.getElementById("chKm"), rows = D.daily;
    if (!rows.some(r => r.km > 0)) return empty(host, "No driving in this period.");
    const f = frame(host, 42), max = niceMax(Math.max(...rows.map(r => r.km)) * 1.08);
    yAxis(f, max, null, v => n1(v));
    const step = (f.R - f.L) / rows.length, bw = Math.max(3, Math.min(42, step - 2 - step * 0.25));
    const sy = v => f.B - (f.B - f.T) * v / max, color = css("--dv-1");
    rows.forEach((r, i) => {
      const x = f.L + step * i;
      hitCol(f, x, step, () => `<div class="fw-semibold">${dLong(r.date)}</div><div><span class="k">Distance</span> <b>${n1(r.km)} km</b></div>
        <div><span class="k">Trips</span> <b>${r.trips}</b> · <span class="k">driving</span> <b>${dur(r.driving_s)}</b></div>
        <div><span class="k">Max speed</span> <b>${r.max_kmh} km/h</b></div>`);
      if (r.km > 0) el("path", {d: barPath(x + (step - bw) / 2, sy(r.km), bw, f.B - sy(r.km)), fill: color, class: "mark", "pointer-events": "none"}, f.svg);
    });
    // direct label on the longest day only
    const best = rows.reduce((a, b) => (b.km > a.km ? b : a));
    const bi = rows.indexOf(best);
    text(f.svg, f.L + step * bi + step / 2, sy(best.km) - 5, `${n1(best.km)} km`, {"text-anchor": "middle", class: "lbl"});
    const avg = D.totals.avg_km_day;
    if (avg > 0) {
      el("line", {x1: f.L, x2: f.R, y1: sy(avg), y2: sy(avg), class: "avg", "pointer-events": "none"}, f.svg);
      text(f.svg, f.R, sy(avg) - 5, `avg ${n1(avg)} km`, {"text-anchor": "end", class: "lbl"});
    }
    xLabels(f, rows, f.L, step, r => dLabel(r.date));
  }

  /* ---------- daily score ---------- */
  function scoreChart() {
    const host = document.getElementById("chScore"), rows = D.daily;
    if (!rows.some(r => r.score != null)) return empty(host, "No scored driving in this period.");
    const f = frame(host, 32), sy = v => f.B - (f.B - f.T) * v / 100;
    yAxis(f, 100, 4, v => Math.round(v));
    const bands = [[D.score.good_min, css("--st-good"), "Good"], [D.score.fair_min, css("--st-fair"), "Fair"]];
    for (const [v, c, lab] of bands) {
      el("line", {x1: f.L, x2: f.R, y1: sy(v), y2: sy(v), class: "band", stroke: c}, f.svg);
      text(f.svg, f.L + 4, sy(v) - 4, `${lab} ≥ ${v}`, {"text-anchor": "start"});
    }
    const step = (f.R - f.L) / rows.length, cx = i => f.L + step * i + step / 2;
    const pts = rows.map((r, i) => [r, i]).filter(([r]) => r.score != null);
    if (pts.length > 1) el("polyline", {points: pts.map(([r, i]) => `${cx(i)},${sy(r.score)}`).join(" "), fill: "none",
      stroke: css("--dv-1"), "stroke-width": 2, "stroke-linejoin": "round", "pointer-events": "none"}, f.svg);
    const ring = css("--card") || "#fff", colors = {good: css("--st-good"), fair: css("--st-fair"), aggressive: css("--st-bad")};
    rows.forEach((r, i) => {
      if (r.score == null) return;
      hitCol(f, f.L + step * i, step, () => `<div class="fw-semibold">${dLong(r.date)}</div>
        <div><span class="k">Score</span> <b>${r.score}</b> · ${RATING[r.rating] || ""}</div>
        <div><span class="k">Harsh events</span> <b>${r.harsh}</b> · <span class="k">over speed</span> <b>${dur(r.overspeed_s)}</b></div>
        <div><span class="k">${n1(r.km)} km</span> in ${r.trips} trip${r.trips === 1 ? "" : "s"}</div>`);
      el("circle", {cx: cx(i), cy: sy(r.score), r: 5, fill: colors[r.rating] || css("--dv-1"), stroke: ring, "stroke-width": 2, "pointer-events": "none"}, f.svg);
    });
    const last = pts[pts.length - 1];
    text(f.svg, cx(last[1]), sy(last[0].score) - 10, String(last[0].score), {"text-anchor": "middle", class: "lbl"});
    xLabels(f, rows, f.L, step, r => dLabel(r.date));
  }

  /* ---------- driving vs idling (stacked, hours) ---------- */
  function timeChart() {
    const host = document.getElementById("chTime"), rows = D.daily;
    const c1 = css("--dv-1"), c2 = css("--dv-2"), hasIdle = rows.some(r => r.idle_s != null);
    document.getElementById("lgTime").innerHTML = `<span><i style="background:${c1}"></i>Driving</span>` +
      (hasIdle ? `<span><i style="background:${c2}"></i>Idling</span>` : `<span>idling needs ignition data</span>`);
    if (!rows.some(r => r.driving_s > 0 || r.idle_s > 0)) return empty(host, "No driving in this period.");
    const hrs = s => (s || 0) / 3600;
    const f = frame(host, 36), max = niceMax(Math.max(...rows.map(r => hrs(r.driving_s) + hrs(r.idle_s))) * 1.08);
    yAxis(f, max, null, v => `${n1(v)} h`);
    const step = (f.R - f.L) / rows.length, bw = Math.max(3, Math.min(42, step - 2 - step * 0.25));
    const sy = v => f.B - (f.B - f.T) * v / max;
    rows.forEach((r, i) => {
      const x = f.L + step * i + (step - bw) / 2, a = hrs(r.driving_s), b = hrs(r.idle_s);
      hitCol(f, f.L + step * i, step, () => `<div class="fw-semibold">${dLong(r.date)}</div>
        <div><i class="d-inline-block rounded-1 me-1" style="width:.6rem;height:.6rem;background:${c1}"></i><span class="k">Driving</span> <b>${dur(r.driving_s)}</b></div>
        ${hasIdle ? `<div><i class="d-inline-block rounded-1 me-1" style="width:.6rem;height:.6rem;background:${c2}"></i><span class="k">Idling</span> <b>${dur(r.idle_s)}</b></div>` : ""}
        <div><span class="k">Engine on</span> <b>${dur(r.engine_s)}</b></div>`);
      if (b > 0 && a > 0) {
        el("rect", {x, y: sy(a), width: bw, height: f.B - sy(a), fill: c1, "pointer-events": "none"}, f.svg);
        el("path", {d: barPath(x, sy(a + b), bw, Math.max(0, sy(a) - sy(a + b) - 2)), fill: c2, "pointer-events": "none"}, f.svg);
      } else if (a > 0 || b > 0) {
        el("path", {d: barPath(x, sy(a || b), bw, f.B - sy(a || b)), fill: a > 0 ? c1 : c2, "pointer-events": "none"}, f.svg);
      }
    });
    xLabels(f, rows, f.L, step, r => dLabel(r.date));
  }

  /* ---------- speed profile ---------- */
  function speedChart() {
    const host = document.getElementById("chSpeed"), rows = D.speed_profile;
    if (!rows.some(r => r.seconds > 0)) return empty(host, "No driving in this period.");
    const f = frame(host, 34), max = niceMax(Math.max(...rows.map(r => r.pct)) * 1.15);
    yAxis(f, max, null, v => `${n1(v)}%`);
    const step = (f.R - f.L) / rows.length, bw = Math.min(46, step * 0.7), sy = v => f.B - (f.B - f.T) * v / max;
    const limit = D.score.speed_limit_kmh;
    rows.forEach((r, i) => {
      const x = f.L + step * i;
      const lo = parseInt(r.label, 10), over = lo >= limit;
      hitCol(f, x, step, () => `<div class="fw-semibold">${esc(r.label)} km/h</div><div><b>${r.pct}%</b> of driving · ${dur(r.seconds)}</div>
        ${over ? `<div class="k">at or above the ${limit} km/h limit</div>` : ""}`);
      if (r.pct > 0) {
        el("path", {d: barPath(x + (step - bw) / 2, sy(r.pct), bw, f.B - sy(r.pct)), fill: css("--dv-1"), "pointer-events": "none",
          opacity: over ? 1 : 0.85}, f.svg);
        text(f.svg, x + step / 2, sy(r.pct) - 5, `${Math.round(r.pct)}%`, {"text-anchor": "middle", class: "lbl"});
      }
    });
    rows.forEach((r, i) => text(f.svg, f.L + step * i + step / 2, f.H - 6, r.label, {"text-anchor": "middle"}));
  }

  /* ---------- when it drives: heatmap (≤ 31 days) or hours of the day ---------- */
  function heatChart() {
    const host = document.getElementById("chHeat");
    if (!D.hours.some(v => v > 0)) { host.style.height = "200px"; return empty(host, "No driving in this period."); }
    if (!D.heatmap) {
      document.getElementById("hmNote").textContent = "driving minutes in each hour of the day";
      host.style.height = "230px";
      const f = frame(host, 40), max = niceMax(Math.max(...D.hours) * 1.08);
      yAxis(f, max, null, v => `${n1(v)}m`);
      const step = (f.R - f.L) / 24, bw = Math.max(3, step - 4), sy = v => f.B - (f.B - f.T) * v / max;
      D.hours.forEach((v, h) => {
        const x = f.L + step * h;
        hitCol(f, x, step, () => `<div class="fw-semibold">${String(h).padStart(2, "0")}:00–${String(h + 1).padStart(2, "0")}:00</div><div><b>${dur(v * 60)}</b> driving</div>`);
        if (v > 0) el("path", {d: barPath(x + (step - bw) / 2, sy(v), bw, f.B - sy(v)), fill: css("--dv-1"), "pointer-events": "none"}, f.svg);
      });
      D.hours.forEach((_, h) => { if (h % 3 === 0) text(f.svg, f.L + step * h + step / 2, f.H - 6, String(h).padStart(2, "0"), {"text-anchor": "middle"}); });
      return;
    }
    const days = Object.keys(D.heatmap).sort();
    const max = Math.max(1, ...days.flatMap(d => D.heatmap[d]));
    const c = css("--dv-1");
    let html = `<div class="vd-hm" style="grid-template-columns: 5.2rem repeat(24, 1fr)"><div></div>`;
    for (let h = 0; h < 24; h++) html += `<div class="h">${h % 3 === 0 ? String(h).padStart(2, "0") : ""}</div>`;
    for (const d of days) {
      html += `<div class="r">${dLabel(d)} ${WD[day(d).getDay()]}</div>`;
      D.heatmap[d].forEach((v, h) => {
        const a = v > 0 ? 0.18 + 0.82 * v / max : 0;
        html += `<div class="c" data-d="${d}" data-h="${h}" data-v="${v}" style="${a ? `background:${c};opacity:${a.toFixed(2)}` : ""}"></div>`;
      });
    }
    html += `</div><div class="d-flex align-items-center gap-2 mt-2 small text-body-secondary">less
      ${[0.18, 0.4, 0.6, 0.8, 1].map(a => `<span style="display:inline-block;width:14px;height:12px;border-radius:3px;background:${c};opacity:${a}"></span>`).join("")}
      more · darkest = ${Math.round(max)} min in one hour</div>`;
    host.innerHTML = html;
  }
  const heatHost = document.getElementById("chHeat");
  heatHost.addEventListener("mousemove", ev => {
    const cell = ev.target.closest(".vd-hm .c"); if (!cell) return;
    const h = +cell.dataset.h;
    showTip(`<div class="fw-semibold">${dLong(cell.dataset.d)}</div><div>${String(h).padStart(2, "0")}:00–${String(h + 1).padStart(2, "0")}:00 · <b>${dur(+cell.dataset.v * 60)}</b> driving</div>`, ev);
  });
  heatHost.addEventListener("mouseout", ev => { if (ev.target.closest(".vd-hm .c")) hideTip(); });

  /* ---------- score ring ---------- */
  function ring() {
    const host = document.getElementById("vdRing"), s = D.score.score;
    const color = {good: css("--st-good"), fair: css("--st-fair"), aggressive: css("--st-bad")}[D.score.rating] || css("--muted");
    const R = 62, C = 2 * Math.PI * R, frac = s == null ? 0 : s / 100;
    host.innerHTML = `<svg viewBox="0 0 150 150"><circle cx="75" cy="75" r="${R}" fill="none" stroke="var(--line)" stroke-width="12"/>
      <circle cx="75" cy="75" r="${R}" fill="none" stroke="${color}" stroke-width="12" stroke-linecap="round"
        stroke-dasharray="${(C * frac).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 75 75)"/>
      <text x="75" y="82" text-anchor="middle" style="font-size:38px;font-weight:700;fill:var(--ink)">${s == null ? "—" : s}</text>
      <text x="75" y="102" text-anchor="middle" style="font-size:11px;fill:var(--muted)">out of 100</text></svg>`;
  }

  /* ---------- trips table ---------- */
  function trips() {
    if (!D.trips.length) return;
    const body = document.getElementById("vdTrips");
    body.innerHTML = D.trips.map(t => {
      const from = encodeURIComponent(t.start), to = encodeURIComponent(t.end);
      return `<tr><td>${esc(F.fmt(t.start).slice(0, 16))}${t.drivers && t.drivers.length ? `<div class="small text-body-secondary">iButton ${esc(t.drivers.join(", "))}</div>` : ""}</td>
        <td>${dur(t.duration_s)}</td><td class="text-end">${n1(t.km)}</td><td class="text-end">${t.max_kmh}</td><td class="text-end">${t.harsh}</td>
        <td>${t.score != null ? `<span class="ds-score ${t.rating}">${t.score}</span>` : "—"}</td>
        <td><a class="btn btn-sm btn-outline-secondary py-0" href="/history/?device=${D.vehicle.id}&from=${from}&to=${to}" title="Replay this trip on the map"><i class="bi bi-play-fill"></i></a></td></tr>`;
    }).join("");
  }

  /* ---------- live "now" strip ---------- */
  async function now() {
    let v;
    try { v = await F.json(`/api/vehicle/${D.vehicle.id}/?minutes=5`); } catch (e) { return; }
    const st = v.device, set = (k, s) => { const n = root.querySelector(`[data-now="${k}"]`); if (n) n.innerHTML = s; };
    const io = id => { const r = v.latest && v.latest.io.find(x => x.id === id); return r ? r.value : null; };
    const colors = {moving: "var(--moving)", idling: "var(--idling)", parked: "var(--parked)", nosignal: "var(--nosignal)"};
    document.getElementById("vdState").innerHTML = `<span class="dot" style="background:${colors[st.state]}"></span>${esc(F.stateLabel[st.state] || st.state)}`;
    const ign = st.ignition ?? io(239);
    set("speed", st.fix ? `${st.fix.speed} km/h` : "—");
    set("ign", ign == null ? "—" : ign ? "On" : "Off");
    set("today", v.today ? `${n1(v.today.distance_km)} km` : "—");
    set("odo", v.odometer_km != null ? `${Math.round(v.odometer_km).toLocaleString()} km` : "—");
    const ev = st.ext_v ?? io(66);
    set("batt", ev != null ? `${(+ev).toFixed(2)} V` : "—");
    set("seen", st.last_packet ? esc(F.ago(st.last_packet)) : "never");
    set("loc", st.fix ? `<a href="https://www.google.com/maps?q=${st.fix.lat},${st.fix.lon}" target="_blank" rel="noopener">${st.fix.lat.toFixed(4)}, ${st.fix.lon.toFixed(4)}</a>` : "no fix");
  }

  // "Longest day" subtitle
  const bd = root.querySelector("[data-date]");
  if (bd && bd.dataset.date) bd.textContent = dLong(bd.dataset.date);

  function drawAll() { kmChart(); scoreChart(); timeChart(); speedChart(); heatChart(); ring(); }
  drawAll(); trips(); now();
  setInterval(now, Math.max(15, F.cfg.poll || 30) * 1000);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawAll, 150); });
  // redraw when the colour theme changes so the marks pick up the new tokens
  new MutationObserver(() => drawAll()).observe(document.documentElement, {attributes: true, attributeFilter: ["data-bs-theme"]});
})();
