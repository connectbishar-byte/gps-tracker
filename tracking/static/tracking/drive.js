/* Mobile car view: record a drive from the phone's GPS (or a tracked vehicle), show live stats,
   save the stop as a place. The trip maths (DriveTrip) has no DOM use so it can be tested on its own. */
(function () {
  "use strict";

  // ---------------------------------------------------------------- trip maths
  const R = 6371008.8;
  const rad = d => d * Math.PI / 180;
  function haversine(a, b) {
    const dLat = rad(b[0] - a[0]), dLon = rad(b[1] - a[1]);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function bearing(a, b) {
    const y = Math.sin(rad(b[1] - a[1])) * Math.cos(rad(b[0]));
    const x = Math.cos(rad(a[0])) * Math.sin(rad(b[0])) - Math.sin(rad(a[0])) * Math.cos(rad(b[0])) * Math.cos(rad(b[1] - a[1]));
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }
  const angleDiff = (a, b) => ((b - a + 540) % 360) - 180;

  // m/s² thresholds per sensitivity: [harsh accel, harsh brake, harsh turn (lateral)]
  const SENS = {low: [3.5, 4.5, 4.5], normal: [3.0, 3.5, 4.0], high: [2.5, 3.0, 3.3]};
  const MOVING_KMH = 3;

  function newTrip(now) {
    return {started: now, ended: null, startPos: null, startAddr: "", points: [], last: null, dist: 0, movingS: 0,
      maxKmh: 0, harshAccel: 0, harshBrake: 0, harshTurn: 0, overspeedS: 0, events: [], lastEvt: {}};
  }

  /* Feed one fix: {t (ms), lat, lon, acc (m|null), kmh (null = unknown), heading (deg|null), alt}.
     Returns the fix as used (speed and heading filled in) or null when it was dropped. */
  function addFix(trip, fx, opts) {
    const o = Object.assign({limit: 120, sens: "normal", maxAcc: 60}, opts || {});
    if (fx.acc != null && fx.acc > o.maxAcc) return null;          // too inaccurate to use
    const p = [fx.lat, fx.lon];
    const prev = trip.last;
    const out = Object.assign({}, fx);
    if (!prev) {
      if (out.kmh == null) out.kmh = 0;
      trip.startPos = trip.startPos || p;
      trip.ref = {t: fx.t, lat: fx.lat, lon: fx.lon};
      trip.points.push(p);
      trip.last = out;
      return out;
    }
    const dt = (fx.t - prev.t) / 1000;
    if (dt <= 0) return null;
    const d = haversine([prev.lat, prev.lon], p);
    // no speed from the GPS: derive it over at least one second, from a reference point
    if (out.kmh == null) {
      const ref = trip.ref || prev, dtr = (fx.t - ref.t) / 1000;
      if (dtr >= 1) { out.kmh = haversine([ref.lat, ref.lon], p) / dtr * 3.6; trip.ref = {t: fx.t, lat: fx.lat, lon: fx.lon}; }
      else out.kmh = prev.kmh || 0;
    }
    if (d / dt * 3.6 > 300 && d > 200) return null;                 // a GPS jump, not driving
    // Ignore standing-still jitter: the point moved less than its own accuracy while (nearly) stopped.
    const jitter = Math.max(3, (fx.acc || 5) * 0.6);
    const moved = d >= jitter || (out.kmh > MOVING_KMH && d > 1);
    if (out.heading == null || out.kmh < MOVING_KMH) {
      out.heading = moved && d > 4 ? bearing([prev.lat, prev.lon], p) : prev.heading;
    }
    const step = Math.min(dt, 30);                                   // a long gap (tunnel, no signal) is not all moving time
    if (out.kmh > MOVING_KMH) trip.movingS += step;
    if (out.kmh > o.limit) trip.overspeedS += step;
    if (out.kmh <= 300) trip.maxKmh = Math.max(trip.maxKmh, out.kmh);

    // harsh driving: needs closely spaced fixes (phone GPS ~1/s); slow tracker uploads are skipped
    if (dt >= 0.5 && dt <= 5 && prev.kmh != null) {
      const [ta, tb, tt] = SENS[o.sens] || SENS.normal;
      const a = (out.kmh - prev.kmh) / 3.6 / dt;
      const v = out.kmh / 3.6;
      if (a > ta && out.kmh > 10) evt(trip, "harshAccel", fx.t);
      if (a < -tb && prev.kmh > 10) evt(trip, "harshBrake", fx.t);
      if (v > 5.5 && prev.heading != null && out.heading != null) {
        const lat = Math.abs(rad(angleDiff(prev.heading, out.heading)) / dt) * v;
        if (lat > tt) evt(trip, "harshTurn", fx.t);
      }
    }
    if (moved) {
      trip.dist += d;
      trip.points.push(p);
      trip.last = out;
    } else {
      // stay on the old point, but keep the new time / speed so the next distance is measured from a real fix
      trip.last = Object.assign({}, out, {lat: prev.lat, lon: prev.lon});
    }
    return out;
  }
  function evt(trip, kind, t) {
    if (trip.lastEvt[kind] && t - trip.lastEvt[kind] < 4000) return;   // one event per manoeuvre
    trip.lastEvt[kind] = t;
    trip[kind] += 1;
    trip.events.push([kind, t]);
  }

  function score(trip) {
    const km = trip.dist / 1000;
    const penalty = 6 * trip.harshAccel + 8 * trip.harshBrake + 5 * trip.harshTurn + 2 * (trip.overspeedS / 60);
    const s = Math.round(Math.max(0, Math.min(100, 100 - penalty / Math.max(1, km / 25))));
    return {score: s, rating: s >= 80 ? "good" : s >= 60 ? "fair" : "aggressive"};
  }

  function summary(trip, now) {
    const end = trip.ended || now;
    const elapsed = Math.max(0, (end - trip.started) / 1000);
    const km = trip.dist / 1000;
    return Object.assign({km, elapsed, movingS: trip.movingS, maxKmh: trip.maxKmh,
      avgKmh: trip.movingS > 10 ? km / (trip.movingS / 3600) : 0,
      avgAllKmh: elapsed > 10 ? km / (elapsed / 3600) : 0}, score(trip));
  }

  window.DriveTrip = {haversine, bearing, newTrip, addFix, score, summary};
  if (typeof document === "undefined" || !document.getElementById("dmap")) return;

  // ---------------------------------------------------------------- page
  const $ = id => document.getElementById(id);
  const C = window.DRIVE_CFG;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode / full */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
  };
  const esc = s => Fleet.esc(s);
  const fmtDur = s => {
    s = Math.round(s || 0);
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`;
  };
  const compass = h => h == null ? "—" : ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(h / 45) % 8];
  const RATING = {good: "Good", fair: "Fair", aggressive: "Aggressive"};

  // which details show at the bottom (configurable, saved on the phone)
  const TILES = [
    ["distance", "Distance", true], ["duration", "Drive time", true], ["avg", "Average speed", true],
    ["max", "Max speed", true], ["driver", "Driver", true], ["heading", "Heading", true],
    ["location", "Location", false], ["moving", "Moving time", false], ["avgall", "Average incl. stops", false],
    ["harsh", "Harsh events", true], ["overspeed", "Over the limit", false], ["altitude", "Altitude", false],
    ["accuracy", "GPS accuracy", false], ["from", "Started at", false], ["clock", "Time", false],
    ["coords", "Coordinates", false],
  ];
  const DEF = {tiles: TILES.filter(t => t[2]).map(t => t[0]), limit: 120, sens: "normal", wake: true, beep: false, ask: true};
  let cfg = Object.assign({}, DEF, store.get("mgt-drive-cfg", {}));
  const saveCfg = () => store.set("mgt-drive-cfg", cfg);

  // ---------------------------------------------------------------- map
  const map = L.map("dmap", {zoomControl: false, attributionControl: true}).setView([24.45, 54.6], 12);
  Fleet.baseLayers(map);
  // the travelled track: white casing under a blue line so it shows on every base map
  const routeCasing = L.polyline([], {color: "#ffffff", weight: 10, opacity: .9, className: "dv-route-casing", interactive: false}).addTo(map);
  const routeLine = L.polyline([], {color: "#0ea5e9", weight: 6, opacity: 1, interactive: false}).addTo(map);
  const gapLines = L.layerGroup().addTo(map);
  const route = {setLatLngs(pts) { routeCasing.setLatLngs(pts); routeLine.setLatLngs(pts); }};
  function drawGaps() {
    gapLines.clearLayers();
    for (const g of (trip && trip.gaps) || []) if (!g.road) L.polyline([g.from, g.to], {color: "#0ea5e9", weight: 4, dashArray: "6 8", opacity: .9}).addTo(gapLines);
  }
  const accCircle = L.circle([0, 0], {radius: 0, className: "dv-acc", color: "#0ea5e9", weight: 1, fillOpacity: .12, interactive: false});
  const startMark = L.circleMarker([0, 0], {radius: 7, color: "#fff", weight: 2, fillColor: "#16a34a", fillOpacity: 1});
  const meIcon = L.divIcon({className: "dv-me", iconSize: [44, 44], iconAnchor: [22, 22],
    html: '<svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="17" fill="#0ea5e9" stroke="#fff" stroke-width="3"/>' +
          '<path d="M22 9 L31 31 L22 26 L13 31 Z" fill="#fff"/></svg>'});
  let me = null, follow = true, lastHeading = null;
  // Centre a point in the part of the map NOT covered by the bottom panel (or the side panel in landscape),
  // so your position and the track are always in view.
  function centerOn(ll, zoom, animate) {
    const z = zoom == null ? map.getZoom() : zoom, size = map.getSize();
    const r = document.getElementById("dsheet").getBoundingClientRect();
    let cx = size.x / 2, cy = size.y / 2;
    if (r.top > 80) cy = (64 + r.top) / 2;           // portrait: between the top bar and the panel
    else if (r.left > 80) cx = r.left / 2;            // landscape: left of the side panel
    const target = map.unproject(map.project(L.latLng(ll), z).add([size.x / 2 - cx, size.y / 2 - cy]), z);
    if (animate) map.panTo(target, {animate: true, duration: .5}); else map.setView(target, z, {animate: false});
  }
  map.on("dragstart", () => { follow = false; $("dlocate").classList.remove("on"); });
  $("dlocate").classList.add("on");

  // ---------------------------------------------------------------- state
  let source = store.get("mgt-drive-source", "phone");
  let trip = null, live = null, watchId = null, pollT = null, lastVehicleTs = null, lastFixAt = 0;
  let place = {name: "", at: null, t: 0, busy: false};
  const saved = store.get("mgt-drive-trip", null);
  if (saved && saved.started && Date.now() - saved.started < 12 * 3600e3) {
    trip = saved;
    if (trip.source) source = trip.source;
  }
  if (![...$("dsource").options].some(o => o.value === String(source))) source = "phone";
  $("dsource").value = String(source);

  // ---------------------------------------------------------------- position sources
  function stopSources() {
    if (watchId != null && navigator.geolocation) navigator.geolocation.clearWatch(watchId);
    watchId = null;
    clearInterval(pollT); pollT = null;
  }
  function startSource() {
    stopSources();
    if (source === "phone") {
      if (!navigator.geolocation) return alertMsg("This browser has no GPS access.", true);
      if (!window.isSecureContext) return alertMsg("Location needs the https:// address of this site.", true);
      watchId = navigator.geolocation.watchPosition(onPhone, onGeoError,
        {enableHighAccuracy: true, maximumAge: 0, timeout: 20000});
    } else {
      pollVehicle(); pollT = setInterval(pollVehicle, Math.max(2, Fleet.cfg.poll) * 1000);
    }
  }
  function onPhone(pos) {
    const c = pos.coords;
    onFix({t: pos.timestamp || Date.now(), lat: c.latitude, lon: c.longitude, acc: c.accuracy,
      kmh: c.speed == null || isNaN(c.speed) ? null : Math.max(0, c.speed * 3.6),
      heading: c.heading == null || isNaN(c.heading) ? null : c.heading, alt: c.altitude});
  }
  function onGeoError(err) {
    const msg = {1: "Location is blocked. Allow location for this site in the browser settings, then reload.",
      2: "No GPS position yet. Check that location is on.", 3: "Still waiting for GPS…"}[err.code] || err.message;
    alertMsg(msg, err.code === 1);
    setGps(null);
  }
  async function pollVehicle() {
    if (trip && trip.last && lastVehicleTs) return pollVehicleHistory();
    try {
      const data = await Fleet.json(Fleet.cfg.api);
      const d = data.devices.find(x => String(x.id) === String(source));
      if (!d || !d.fix) { alertMsg("That vehicle has no valid GPS fix yet."); setGps(null); return; }
      if (d.fix.ts === lastVehicleTs) return;
      lastVehicleTs = d.fix.ts;
      onFix({t: new Date(d.fix.ts).getTime(), lat: d.fix.lat, lon: d.fix.lon, acc: null, kmh: d.fix.speed || 0,
        heading: d.fix.angle, alt: null, sats: d.fix.sats, vehicle: d.name});
    } catch (e) { alertMsg("Can't reach the server - check mobile data."); }
  }
  // While recording a vehicle, take every record the tracker sent since the last one, so nothing is lost
  // while this page was in the background or the phone was locked.
  let historyBusy = false;
  async function pollVehicleHistory() {
    if (historyBusy) return;
    historyBusy = true;
    try {
      const url = `${C.history}?device=${encodeURIComponent(source)}&from=${encodeURIComponent(lastVehicleTs)}`;
      const data = await Fleet.json(url);
      const since = new Date(lastVehicleTs).getTime();
      const pts = data.points.filter(p => p.valid && new Date(p.ts).getTime() > since);
      for (const p of pts) {
        lastVehicleTs = p.ts;
        onFix({t: new Date(p.ts).getTime(), lat: p.lat, lon: p.lon, acc: null, kmh: p.speed || 0, heading: p.angle,
          alt: p.alt, sats: p.sats, vehicle: data.device}, pts.length > 1 && p !== pts[pts.length - 1]);
      }
    } catch (e) { alertMsg("Can't reach the server - check mobile data."); }
    historyBusy = false;
  }

  // ---------------------------------------------------------------- fixes
  // ---------------------------------------------------------------- gaps (page in the background)
  // A web page gets no GPS while the phone is locked or another app is in front. When fixes come back after a
  // gap, the missing part is filled along the roads (OSRM routing), or with a straight dashed line if that fails.
  const GAP_S = 20, GAP_M = 150;
  let filling = false;
  async function fillGap(prev, fx) {
    const from = [prev.lat, prev.lon], to = [fx.lat, fx.lon];
    const straight = haversine(from, to);
    const gapS = (fx.t - prev.t) / 1000;
    let pts = null, dist = straight, dur = null;
    try {
      const ctl = new AbortController(); setTimeout(() => ctl.abort(), 6000);
      const u = `https://router.project-osrm.org/route/v1/driving/${from[1]},${from[0]};${to[1]},${to[0]}?overview=full&geometries=geojson`;
      const j = await (await fetch(u, {signal: ctl.signal, referrerPolicy: "no-referrer"})).json();
      const r = j.routes && j.routes[0];
      // trust the road route only when it is a plausible drive for the time that passed
      if (r && r.distance < straight * 3 + 500 && r.distance / Math.max(gapS, 1) * 3.6 < 250) {
        pts = r.geometry.coordinates.map(c => [+c[1].toFixed(6), +c[0].toFixed(6)]);
        dist = r.distance; dur = r.duration;
      }
    } catch (e) { /* offline or routing down: straight line */ }
    if (!trip || trip.last !== prev) return;           // stopped or moved on meanwhile
    trip.gaps = trip.gaps || [];
    trip.gaps.push({from, to, km: +(dist / 1000).toFixed(3), s: Math.round(gapS), road: !!pts});
    if (pts) trip.points.push(...pts.slice(1, -1));
    trip.points.push(to);
    trip.dist += dist;
    trip.movingS += Math.min(gapS, dur != null ? dur : gapS);
    trip.last = Object.assign({}, fx, {kmh: fx.kmh == null ? 0 : fx.kmh,
      heading: fx.heading != null ? fx.heading : bearing(from, to)});
    trip.ref = {t: fx.t, lat: fx.lat, lon: fx.lon};
    persistTrip.t = 0; persistTrip();
    drawGaps();
    toast(`Filled ${(dist / 1000).toFixed(1)} km recorded while the app was in the background` + (pts ? " (along the roads)" : ""), 6000);
  }

  function onFix(fx, quiet) {
    if (trip && trip.last && !trip.ended && source === "phone") {
      const gapS = (fx.t - trip.last.t) / 1000;
      if (filling) return;
      if (gapS > GAP_S && haversine([trip.last.lat, trip.last.lon], [fx.lat, fx.lon]) > GAP_M &&
          (fx.acc == null || fx.acc <= 60)) {
        filling = true;
        fillGap(trip.last, fx).finally(() => { filling = false; onFix(fx, quiet); });
        return;
      }
    }
    hideAlert();
    lastFixAt = Date.now();
    setGps(fx.acc, fx.sats);
    let used = fx;
    if (trip) {
      used = addFix(trip, fx, {limit: cfg.limit, sens: cfg.sens, maxAcc: source === "phone" ? 60 : 1e9});
      if (!used) { used = Object.assign({}, fx, {kmh: fx.kmh || 0}); }
      else persistTrip();
    } else {
      // not recording: still show speed / heading
      if (used.kmh == null && live) {
        const dt = (fx.t - live.t) / 1000;
        used = Object.assign({}, fx, {kmh: dt > 0 ? haversine([live.lat, live.lon], [fx.lat, fx.lon]) / dt * 3.6 : 0});
      }
      if (used.heading == null && live && haversine([live.lat, live.lon], [fx.lat, fx.lon]) > 5) used.heading = bearing([live.lat, live.lon], [fx.lat, fx.lon]);
    }
    if (used.heading == null && live) used.heading = live.heading;
    live = used;
    if (live.heading != null && (live.kmh || 0) > MOVING_KMH) lastHeading = live.heading;
    if (quiet) return;            // catching up on a batch of tracker records: draw once at the end
    drawMe();
    lookupPlace(fx.lat, fx.lon);
    render();
  }

  function drawMe() {
    const ll = [live.lat, live.lon];
    if (!me) {
      me = L.marker(ll, {icon: meIcon, zIndexOffset: 1000, keyboard: false}).addTo(map);
      centerOn(ll, 16);
    } else me.setLatLng(ll);
    const svg = me.getElement() && me.getElement().querySelector("svg");
    if (svg) svg.style.transform = `rotate(${lastHeading || 0}deg)`;
    if (live.acc) { accCircle.setLatLng(ll).setRadius(live.acc); if (!map.hasLayer(accCircle)) accCircle.addTo(map); }
    else if (map.hasLayer(accCircle)) map.removeLayer(accCircle);
    if (trip) {
      route.setLatLngs(trip.points);
      if (trip.startPos) { startMark.setLatLng(trip.startPos); if (!map.hasLayer(startMark)) startMark.addTo(map); }
    }
    if (follow) centerOn(ll, null, true);
  }

  function setGps(acc, sats) {
    const g = $("dgps");
    let cls = "bad", txt = "No GPS";
    if (acc != null) { cls = acc <= 15 ? "good" : acc <= 40 ? "fair" : "bad"; txt = `±${Math.round(acc)} m`; }
    else if (source !== "phone" && live) { cls = "good"; txt = sats ? `${sats} sats` : "Tracker"; }
    g.className = "dv-gps " + cls;
    g.querySelector("span").textContent = txt;
  }

  // ---------------------------------------------------------------- place name
  async function lookupPlace(lat, lon, force) {
    const now = Date.now();
    if (place.busy || now < (place.retryAt || 0)) return;
    if (!force && place.at) {
      const d = haversine(place.at, [lat, lon]);
      if (d < 120 && now - place.t < 120000) return;
      if (d < 30) return;
      if (now - place.t < 15000) return;
    }
    place.busy = true;
    try {
      const r = await fetch(`${C.geocode}?lat=${lat.toFixed(5)}&lon=${lon.toFixed(5)}`, {credentials: "same-origin"});
      if (r.ok) {
        const j = await r.json();
        place = {name: j.name || "", full: j.full || "", at: [lat, lon], t: now, busy: false, retryAt: 0};
        if (trip && !trip.startAddr && trip.startPos && haversine(trip.startPos, [lat, lon]) < 300) { trip.startAddr = place.name; persistTrip(); }
        render();
        return;
      }
    } catch (e) { /* offline: keep the last name */ }
    place.busy = false;
    place.retryAt = now + 10000;   // busy or offline: try again in 10 s
  }

  // ---------------------------------------------------------------- render
  function tileValue(k, s) {
    const L2 = live;
    switch (k) {
      case "distance": return [s ? s.km < 10 ? s.km.toFixed(2) : s.km.toFixed(1) : "0.00", "km"];
      case "duration": return [fmtDur(s && s.elapsed)];
      case "moving": return [fmtDur(s && s.movingS)];
      case "avg": return [s ? Math.round(s.avgKmh) : 0, "km/h"];
      case "avgall": return [s ? Math.round(s.avgAllKmh) : 0, "km/h"];
      case "max": return [s ? Math.round(s.maxKmh) : 0, "km/h"];
      case "driver": return s ? [`${RATING[s.rating]} <small>${s.score}</small>`, "", s.rating] : ["—"];
      case "harsh": return trip ? [`${trip.harshAccel}<small>accel</small> ${trip.harshBrake}<small>brake</small> ${trip.harshTurn}<small>turn</small>`, "", "", "wide"] : ["—", "", "", "wide"];
      case "overspeed": return [fmtDur(trip && trip.overspeedS), `>${cfg.limit}`];
      case "heading": return lastHeading == null ? ["—"] : [`${compass(lastHeading)} <small>${Math.round(lastHeading)}°</small>`];
      case "altitude": return L2 && L2.alt != null ? [Math.round(L2.alt), "m"] : ["—"];
      case "accuracy": return L2 && L2.acc != null ? [`±${Math.round(L2.acc)}`, "m"] : ["—"];
      case "location": return [esc(place.name || "—"), "", "", true];
      case "from": return [esc(trip && trip.startAddr || "—"), "", "", true];
      case "clock": return [new Date().toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", timeZone: Fleet.cfg.tz})];
      case "coords": return L2 ? [`${L2.lat.toFixed(5)}, ${L2.lon.toFixed(5)}`, "", "", true] : ["—"];
    }
    return ["—"];
  }
  function render() {
    const s = trip ? summary(trip, Date.now()) : null;
    const kmh = live && (Date.now() - lastFixAt < 15000 || source !== "phone") ? Math.round(live.kmh || 0) : 0;
    $("dspeed").textContent = kmh;
    const over = kmh > cfg.limit;
    $("dspeedbox").classList.toggle("over", over);
    if (over && trip && cfg.beep) beep();
    $("dloc").querySelector("span").textContent = place.name || (live ? `${live.lat.toFixed(5)}, ${live.lon.toFixed(5)}` : "Waiting for location…");
    $("dsub").textContent = trip
      ? `Recording · ${s.km.toFixed(1)} km · ${fmtDur(s.elapsed)}${live && live.vehicle ? " · " + live.vehicle : ""}`
      : (live ? "Press Start to record a drive" : "Waiting for GPS…");
    const b = $("dstart");
    b.classList.toggle("stop", !!trip);
    b.innerHTML = trip ? '<i class="bi bi-stop-fill"></i><span>Stop</span>' : '<i class="bi bi-play-fill"></i><span>Start</span>';
    const names = Object.fromEntries(TILES.map(t => [t[0], t[1]]));
    $("dtiles").innerHTML = cfg.tiles.filter(k => names[k]).map(k => {
      const [v, unit, cls, wide] = tileValue(k, s);
      return `<div class="dv-tile ${cls || ""} ${wide ? "wide" : ""}"><div class="k">${names[k]}</div>` +
        `<div class="v ${wide === true ? "txt" : ""}">${v}${unit ? `<small>${unit}</small>` : ""}</div></div>`;
    }).join("");
  }
  setInterval(render, 1000);

  let lastBeep = 0, actx = null;
  function beep() {
    if (Date.now() - lastBeep < 5000) return;
    lastBeep = Date.now();
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      const o = actx.createOscillator(), g = actx.createGain();
      o.frequency.value = 880; g.gain.value = .15; o.connect(g); g.connect(actx.destination);
      o.start(); o.stop(actx.currentTime + .25);
    } catch (e) { /* no audio */ }
  }

  // ---------------------------------------------------------------- start / stop / save
  function persistTrip() {
    if (!trip) return;
    const now = Date.now();
    if (now - (persistTrip.t || 0) < 3000) return;
    persistTrip.t = now;
    store.set("mgt-drive-trip", trip);
  }
  function start() {
    trip = newTrip(Date.now());
    trip.source = source;
    if (live) addFix(trip, Object.assign({}, live, {t: Date.now()}), {limit: cfg.limit, sens: cfg.sens});
    trip.startAddr = place.name && live && place.at && haversine(place.at, [live.lat, live.lon]) < 300 ? place.name : "";
    persistTrip.t = 0; persistTrip();
    route.setLatLngs(trip.points);
    drawGaps();
    if (source !== "phone" && live) lastVehicleTs = new Date(live.t).toISOString();
    follow = true; $("dlocate").classList.add("on");
    wake(true);
    $("dsource").disabled = true;
    toast("Drive started");
    render();
  }
  function stop() {
    if (!trip) return;
    trip.ended = Date.now();
    store.set("mgt-drive-trip", trip);
    const s = summary(trip, trip.ended);
    if (!cfg.ask) return save("", "");
    $("dstopsum").textContent = `${s.km.toFixed(1)} km in ${fmtDur(s.elapsed)} · max ${Math.round(s.maxKmh)} km/h · avg ${Math.round(s.avgKmh)} km/h · ${RATING[s.rating]} driving (${s.score}/100)`;
    const to = place.name ? place.name.split(",")[0] : "";
    const from = trip.startAddr ? trip.startAddr.split(",")[0] : "";
    $("dstopname").value = from && to && from !== to ? `${from} → ${to}` : (to || from);
    const gaps = (trip.gaps || []).length;
    if (gaps) $("dstopsum").textContent += ` · ${gaps} background gap${gaps > 1 ? "s" : ""} filled`;
    $("dstopname").placeholder = "Name (default: place name and time)";
    $("dstopnotes").value = "";
    $("dstopbox").hidden = false;
  }
  function endTrip() {
    trip = null;
    route.setLatLngs([]); gapLines.clearLayers();
    if (map.hasLayer(startMark)) map.removeLayer(startMark);
    store.del("mgt-drive-trip");
    $("dsource").disabled = false;
    wake(false);
    render();
  }
  function placeBody(kind, name, notes) {
    const pos = live || (trip && trip.last);
    const body = {kind, name: name || "", notes: notes || "", source: source === "phone" ? "phone" : "vehicle",
      device: source === "phone" ? null : +source, latitude: pos.lat, longitude: pos.lon,
      accuracy_m: pos.acc == null ? null : Math.round(pos.acc), address: (place.name || "").slice(0, 300)};
    body.name = body.name.slice(0, 120); body.notes = body.notes.slice(0, 2000);
    if (kind === "trip" && trip) {
      const s = summary(trip, trip.ended || Date.now());
      Object.assign(body, {started_utc: new Date(trip.started).toISOString(), ended_utc: new Date(trip.ended || Date.now()).toISOString(),
        start_latitude: trip.startPos ? trip.startPos[0] : null, start_longitude: trip.startPos ? trip.startPos[1] : null,
        start_address: (trip.startAddr || "").slice(0, 300), distance_km: +s.km.toFixed(3), duration_s: Math.round(s.elapsed),
        moving_s: Math.round(s.movingS), max_speed_kmh: +s.maxKmh.toFixed(1), avg_speed_kmh: +s.avgKmh.toFixed(1),
        driver_score: s.score, driver_rating: s.rating, harsh_accel: trip.harshAccel, harsh_brake: trip.harshBrake,
        harsh_turn: trip.harshTurn, overspeed_s: Math.round(trip.overspeedS), track: trip.points});
    }
    return body;
  }
  async function post(body) {
    const r = await fetch(C.places, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": C.csrf, "Accept": "application/json"}, body: JSON.stringify(body)});
    if (r.status === 400 || r.status === 403) {
      const t = await r.text();
      const err = new Error(r.status === 403 ? "Your sign-in expired. Open the site again and sign in." : t.slice(0, 200));
      err.fatal = r.status === 400 ? "bad" : "auth"; throw err;
    }
    if (!r.ok) throw new Error(r.status + " " + r.statusText);
    return r.json();
  }
  async function send(body, okMsg) {
    try {
      const saved = await post(body);
      toast(okMsg + (saved.name ? `: ${saved.name}` : ""));
      todayCache.t = 0;
      return true;
    } catch (e) {
      if (e.fatal === "bad") { toast("Not saved: " + e.message, 8000); return false; }
      if (e.fatal) { toast("Kept on this phone. " + e.message, 8000); queue(body, false); return false; }
      queue(body, true);
      toast("No connection - saved on this phone, it will upload automatically.", 6000);
      return false;
    }
  }
  function queue(body, retry) {
    const q = store.get("mgt-drive-outbox", []);
    q.push(body); store.set("mgt-drive-outbox", q.slice(-50));
    if (retry) setTimeout(flush, 30000);
  }
  async function flush() {
    const q = store.get("mgt-drive-outbox", []);
    if (!q.length) return;
    const left = [];
    for (const b of q) { try { await post(b); } catch (e) { if (e.fatal !== "bad") left.push(b); } }
    store.set("mgt-drive-outbox", left);
    if (left.length < q.length) toast(`Uploaded ${q.length - left.length} saved place(s)`);
    if (left.length) setTimeout(flush, 60000);
  }
  window.addEventListener("online", flush);
  flush();

  async function save(name, notes) {
    if (!live && !(trip && trip.last)) { toast("No position yet - nothing to save."); endTrip(); return; }
    const body = placeBody("trip", name, notes);
    $("dstopbox").hidden = true;
    endTrip();
    await send(body, "Trip saved");
  }
  async function pin() {
    if (!live) return toast("No position yet.");
    await send(placeBody("pin", "", ""), "Spot saved");
  }

  $("dstart").onclick = () => trip ? stop() : start();
  $("dpin").onclick = pin;
  $("dstopsave").onclick = () => save($("dstopname").value.trim(), $("dstopnotes").value.trim());
  $("dstopdiscard").onclick = () => { $("dstopbox").hidden = true; endTrip(); toast("Drive not saved"); };
  $("dstopresume").onclick = () => { trip.ended = null; $("dstopbox").hidden = true; persistTrip.t = 0; persistTrip(); };
  $("dsource").onchange = e => {
    source = e.target.value === "phone" ? "phone" : e.target.value;
    store.set("mgt-drive-source", source);
    live = null; lastVehicleTs = null; place = {name: "", at: null, t: 0, busy: false};
    if (me) { map.removeLayer(me); me = null; }
    startSource(); render();
  };
  $("dlocate").onclick = () => {
    follow = true; $("dlocate").classList.add("on");
    if (live) centerOn([live.lat, live.lon], Math.max(map.getZoom(), 16));
    else { startSource(); toast("Finding your location…"); }
  };
  $("dhandle").onclick = () => $("dsheet").classList.toggle("min");

  // ---------------------------------------------------------------- full screen + keep screen on
  const root = document.documentElement;
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement;
  $("dfull").onclick = async () => {
    try {
      if (fsEl()) await (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      else if (root.requestFullscreen) await root.requestFullscreen({navigationUI: "hide"});
      else if (root.webkitRequestFullscreen) root.webkitRequestFullscreen();
      else toast("On iPhone: tap Share → Add to Home Screen, then open Car view from the home screen for full screen.", 8000);
    } catch (e) { toast("Full screen isn't available here."); }
    wake(true);
  };
  const onFs = () => {
    $("dfull").innerHTML = `<i class="bi ${fsEl() ? "bi-fullscreen-exit" : "bi-fullscreen"}"></i>`;
    setTimeout(() => map.invalidateSize(), 200);
  };
  document.addEventListener("fullscreenchange", onFs);
  document.addEventListener("webkitfullscreenchange", onFs);

  let lock = null;
  async function wake(on) {
    if (!cfg.wake || !("wakeLock" in navigator)) return;
    try {
      if (on && !lock) { lock = await navigator.wakeLock.request("screen"); lock.addEventListener("release", () => { lock = null; }); }
      else if (!on && lock && !fsEl()) { await lock.release(); lock = null; }
    } catch (e) { /* battery saver or not allowed */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      if (trip) { wake(true); persistTrip(); if (source !== "phone") pollVehicle(); }
      flush();
    }
    else if (trip) { persistTrip.t = 0; persistTrip(); }
  });

  // ---------------------------------------------------------------- settings
  function openCfg() {
    $("dcfglist").innerHTML = TILES.map(([k, label]) =>
      `<label><input type="checkbox" class="form-check-input m-0" value="${k}" ${cfg.tiles.includes(k) ? "checked" : ""}> ${label}</label>`).join("");
    $("dlimit").value = cfg.limit; $("dsens").value = cfg.sens;
    $("dwake").checked = cfg.wake; $("dbeep").checked = cfg.beep; $("dautopin").checked = cfg.ask;
    $("dcfgbox").hidden = false;
  }
  function readCfg() {
    // keep the user's order for tiles that stay on, add new ones at the end
    const on = [...$("dcfglist").querySelectorAll("input:checked")].map(i => i.value);
    cfg.tiles = cfg.tiles.filter(k => on.includes(k)).concat(on.filter(k => !cfg.tiles.includes(k)));
    const lim = parseInt($("dlimit").value, 10);
    cfg.limit = lim >= 20 && lim <= 250 ? lim : DEF.limit;
    cfg.sens = SENS[$("dsens").value] ? $("dsens").value : "normal";
    cfg.wake = $("dwake").checked; cfg.beep = $("dbeep").checked; cfg.ask = $("dautopin").checked;
    saveCfg(); render();
  }
  $("dcfg").onclick = openCfg;
  $("dcfgbox").addEventListener("change", readCfg);
  $("dcfgbox").addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target === $("dcfgbox")) { readCfg(); $("dcfgbox").hidden = true; }
  });
  $("dcfgreset").onclick = () => { cfg = Object.assign({}, DEF); saveCfg(); openCfg(); render(); };

  // ---------------------------------------------------------------- messages
  let alertT = null;
  function alertMsg(text, sticky) {
    const a = $("dalert");
    a.className = "dv-alert warn"; a.textContent = text; a.hidden = false;
    clearTimeout(alertT);
    if (!sticky) alertT = setTimeout(() => { a.hidden = true; }, 8000);
  }
  function hideAlert() { if (!$("dalert").hidden && $("dalert").classList.contains("warn")) $("dalert").hidden = true; }
  let toastT = null;
  function toast(text, ms) {
    const t = $("dtoast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => { t.hidden = true; }, ms || 3000);
  }

  // ---------------------------------------------------------------- bottom bar pop-ups (recording continues)
  const POPS = {
    home: {title: "Home", render: homePane},
    live: {title: "Live map", frame: C.live},
    driver: {title: "Driver", render: driverPane},
    trips: {title: "Trips", frame: C.placesEmbed},
  };
  let popOpen = null, popTimer = null;
  function openPop(name, frameUrl, title) {
    const def = POPS[name] || {};
    popOpen = name;
    document.querySelectorAll(".dv-nav button").forEach(b => b.classList.toggle("on", b.dataset.pop === name));
    $("dpoptitle").textContent = title || def.title || "";
    $("dpoprec").hidden = !trip;
    const body = $("dpopbody"), url = frameUrl || def.frame;
    clearInterval(popTimer);
    if (url) {
      body.innerHTML = "";
      const f = document.createElement("iframe");
      f.src = url; f.title = title || def.title || "";
      body.appendChild(f);
      $("dpopopen").href = url.replace(/([?&])embed=1&?/, "$1").replace(/[?&]$/, ""); $("dpopopen").hidden = false;
    } else {
      $("dpopopen").hidden = true;
      def.render(body);
      popTimer = setInterval(() => { if (popOpen === name) def.render(body, true); }, 2000);
    }
    $("dv").classList.add("popmode");
    $("dpop").hidden = false;
    placePop();
  }
  function placePop() {
    const r = $("dsheet").getBoundingClientRect(), pop = $("dpop");
    if (r.top > 60) { pop.style.bottom = Math.round(innerHeight - r.top) + "px"; pop.style.right = "0"; }
    else { pop.style.right = Math.round(innerWidth - r.left) + "px"; pop.style.bottom = "0"; }   // landscape: bar on the right
  }
  addEventListener("resize", () => { if (popOpen) placePop(); });
  function closePop() {
    $("dpop").hidden = true; popOpen = null; clearInterval(popTimer);
    $("dv").classList.remove("popmode");
    setTimeout(() => map.invalidateSize(), 50);
    $("dpopbody").innerHTML = "";
    document.querySelectorAll(".dv-nav button").forEach(b => b.classList.remove("on"));
  }
  document.querySelector(".dv-nav").addEventListener("click", e => {
    const b = e.target.closest("button[data-pop]"); if (!b) return;
    if (popOpen === b.dataset.pop) closePop(); else openPop(b.dataset.pop);
  });
  $("dpopclose").onclick = closePop;
  $("dpop").addEventListener("click", e => {
    if (e.target === $("dpop")) closePop();
    const l = e.target.closest("[data-frame]");
    if (l) openPop(l.dataset.name || "frame", l.dataset.frame, l.dataset.title);
  });
  $("dback").addEventListener("click", e => { if (trip) { e.preventDefault(); openPop("home"); } });

  const kpi = (k, v, u) => `<div class="dv-kpi"><div class="k">${k}</div><div class="v">${v}${u ? `<small>${u}</small>` : ""}</div></div>`;
  const RCOL = {good: "#16a34a", fair: "#d97706", aggressive: "#dc2626"};
  let todayCache = {t: 0, rows: []};
  async function todayTrips() {
    if (Date.now() - todayCache.t < 30000) return todayCache.rows;
    try {
      const j = await Fleet.json(`${C.places}?kind=trip&limit=100`);
      const day = new Date().toLocaleDateString("en-CA", {timeZone: Fleet.cfg.tz});
      todayCache = {t: Date.now(), rows: (j.results || j).filter(p =>
        new Date(p.created_utc).toLocaleDateString("en-CA", {timeZone: Fleet.cfg.tz}) === day)};
    } catch (e) { todayCache.t = Date.now(); }
    return todayCache.rows;
  }
  async function homePane(body, refresh) {
    const s = trip ? summary(trip, Date.now()) : null;
    const rows = await todayTrips();
    const km = rows.reduce((a, p) => a + (p.distance_km || 0), 0) + (s ? s.km : 0);
    const scores = rows.filter(p => p.driver_score != null).map(p => p.driver_score);
    if (popOpen !== "home") return;
    const gaps = trip && trip.gaps ? trip.gaps.length : 0;
    body.innerHTML = `<div class="dv-pane">
      <div class="small text-body-secondary mb-2">${trip ? "Recording - the drive keeps going while this is open." : "No drive running. Press Start on the car view to record one."}</div>
      ${s ? `<h3 class="h6">This drive</h3><div class="dv-kpis">${kpi("Distance", s.km.toFixed(2), "km")}${kpi("Time", fmtDur(s.elapsed))}
        ${kpi("Average", Math.round(s.avgKmh), "km/h")}${kpi("Max", Math.round(s.maxKmh), "km/h")}
        ${kpi("Driver", `<span style="color:${RCOL[s.rating]}">${RATING[s.rating]}</span>`, s.score)}${kpi("From", `<span style="font-size:14px">${esc(trip.startAddr || "—")}</span>`)}</div>
        ${gaps ? `<div class="dv-gapnote mb-2"><i class="bi bi-info-circle"></i> ${gaps > 1 ? gaps + " parts" : "1 part"} of the drive, while the app was in the background, ${gaps > 1 ? "were" : "was"} filled in.</div>` : ""}` : ""}
      <h3 class="h6">Today</h3><div class="dv-kpis">${kpi("Trips saved", rows.length)}${kpi("Km today", km.toFixed(1), "km")}
        ${kpi("Avg driver score", scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : "—")}</div>
      <h3 class="h6">Open</h3><div class="dv-links">
        <button data-frame="${C.dashboard}" data-title="Dashboard"><i class="bi bi-grid-1x2"></i> Dashboard</button>
        <button data-frame="${C.live}" data-title="Live map" data-name="live"><i class="bi bi-broadcast-pin"></i> Live map</button>
        <button data-frame="${C.drivers}" data-title="Driver scores"><i class="bi bi-person-badge"></i> Driver scores</button>
        <button data-frame="${C.placesEmbed}" data-title="Trips" data-name="trips"><i class="bi bi-signpost-split"></i> Saved trips</button>
        <a href="${C.dashboard.split("?")[0]}" ${trip ? 'onclick="return confirm(\'Leave the car view? The drive is kept and continues when you come back, but nothing is recorded while you are away.\')"' : ""}><i class="bi bi-box-arrow-left"></i> Leave car view</a>
      </div></div>`;
  }
  function gauge(score, rating) {
    const c = 2 * Math.PI * 48, f = Math.max(0, Math.min(100, score)) / 100;
    return `<svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="48" fill="none" stroke="var(--line)" stroke-width="12"/>
      <circle cx="60" cy="60" r="48" fill="none" stroke="${RCOL[rating]}" stroke-width="12" stroke-linecap="round"
        stroke-dasharray="${(c * f).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 60 60)"/>
      <text x="60" y="66" text-anchor="middle" font-size="30" font-weight="700" fill="currentColor">${score}</text></svg>`;
  }
  async function driverPane(body) {
    const rows = await todayTrips();
    if (popOpen !== "driver") return;
    let html = '<div class="dv-pane">';
    if (trip) {
      const s = summary(trip, Date.now()), km = Math.max(1, s.km / 25);
      const parts = [["Harsh acceleration", trip.harshAccel, 6 * trip.harshAccel], ["Harsh braking", trip.harshBrake, 8 * trip.harshBrake],
        ["Harsh turns", trip.harshTurn, 5 * trip.harshTurn], ["Over " + cfg.limit + " km/h", fmtDur(trip.overspeedS), 2 * trip.overspeedS / 60]];
      html += `<div class="dv-gauge">${gauge(s.score, s.rating)}<div><div class="h4 m-0" style="color:${RCOL[s.rating]}">${RATING[s.rating]} driving</div>
        <div class="small text-body-secondary">This drive: ${s.km.toFixed(1)} km, ${fmtDur(s.elapsed)}, max ${Math.round(s.maxKmh)} km/h.<br>
        80+ is Good, 60-79 Fair, under 60 Aggressive.</div></div></div>
        <h3 class="h6">What lowers the score</h3>` +
        parts.map(([k, n, pen]) => { const pts = pen / km; return `<div class="dv-bar"><div class="t"><span>${k}: <b>${n}</b></span><span>-${Math.round(pts)} pts</span></div>
          <div class="b"><span style="width:${Math.min(100, pts * 2.5)}%;background:${pts > 10 ? "#dc2626" : pts > 0 ? "#d97706" : "#16a34a"}"></span></div></div>`; }).join("");
    } else html += '<div class="small text-body-secondary mb-2">No drive running. The score of the current drive shows here while you drive.</div>';
    html += `<h3 class="h6 mt-3">Today's trips</h3>` + (rows.length ? rows.map(p => `<div class="dv-trip">
        <div class="s" style="background:${RCOL[p.driver_rating] || "#64748b"}">${p.driver_score ?? "—"}</div>
        <div class="flex-grow-1 min-w-0"><div class="fw-semibold text-truncate">${esc(p.name)}</div>
        <div class="text-body-secondary">${(p.distance_km || 0).toFixed(1)} km · max ${Math.round(p.max_speed_kmh || 0)} · avg ${Math.round(p.avg_speed_kmh || 0)} km/h · harsh ${p.harsh_accel}/${p.harsh_brake}/${p.harsh_turn}</div></div></div>`).join("")
      : '<div class="small text-body-secondary">No trips saved today yet.</div>');
    html += `<div class="dv-links mt-3"><button data-frame="${C.drivers}" data-title="Driver scores"><i class="bi bi-person-badge"></i> Full driver score sheet</button></div></div>`;
    body.innerHTML = html;
  }

  // ---------------------------------------------------------------- go
  if (trip) {
    $("dsource").disabled = true;
    route.setLatLngs(trip.points);
    drawGaps();
    if (trip.last && source !== "phone") lastVehicleTs = new Date(trip.last.t).toISOString();
    if (trip.startPos) startMark.setLatLng(trip.startPos).addTo(map);
    if (trip.ended) stop(); else { toast("Drive resumed"); wake(true); }
  }
  startSource();
  render();
})();
