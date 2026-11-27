/* Shared helpers for the Meltice GPS Tracker UI */
(function () {
  const cfg = window.FLEET_CFG || {tz: "Asia/Dubai", poll: 5};
  const dtf = new Intl.DateTimeFormat("en-GB", {timeZone: cfg.tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});

  const F = window.Fleet = {
    cfg,
    fmt(iso) { if (!iso) return "—"; return dtf.format(new Date(iso)).replace(",", ""); },
    ago(iso) {
      if (!iso) return "never";
      let s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
      if (s < 60) return s + " s ago";
      if (s < 3600) return Math.round(s / 60) + " min ago";
      if (s < 86400) return Math.round(s / 3600) + " h ago";
      return Math.round(s / 86400) + " days ago";
    },
    stateLabel: {moving: "Moving", idling: "Idling", parked: "Parked", nosignal: "No signal"},
    stateColor: {moving: "#16a34a", idling: "#f59e0b", parked: "#dc2626", nosignal: "#6b7280"},
    esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c])); },
    async json(url) {
      const r = await fetch(url, {credentials: "same-origin", headers: {"Accept": "application/json"}});
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    },
    /* Map marker. A moving vehicle gets a navigation arrow turned to its compass heading
       (0 = north, clockwise), so the arrow's tip points where the vehicle is going. Pass the
       server's "course" (worked out from the last fixes) first and the tracker's angle as fallback. */
    vehicleIcon(state, angle) {
      const c = F.stateColor[state] || "#6b7280";
      const deg = Math.round(((+angle || 0) % 360 + 360) % 360);
      let inner;
      if (state === "moving") {
        inner = `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M12 2 L20 21 L12 16.5 L4 21 Z" fill="#fff" transform="rotate(${deg} 12 12)"/></svg>`;
      } else if (state === "nosignal") inner = '<i class="bi bi-wifi-off"></i>';
      else if (state === "idling") inner = '<i class="bi bi-pause-fill"></i>';
      else inner = '<b style="font-size:13px">P</b>';
      const title = state === "moving" ? ` title="Heading ${deg}°"` : "";
      return L.divIcon({className: "", iconSize: [30, 30], iconAnchor: [15, 15], popupAnchor: [0, -14],
        html: `<div class="veh-icon" style="width:30px;height:30px;background:${c}"${title}>${inner}</div>`});
    },
    heading(fix) { return fix ? (fix.course ?? fix.angle) : 0; },
    /* Base maps. With a Google Maps key (Settings → Integrations) Google Road / Satellite / Hybrid / Terrain
       come first; the free maps below need no key and are always there. The choice is remembered.
       "dark: true" maps are already dark (or photos), so the dark theme doesn't re-tint them. */
    FREE_MAPS: [
      ["Road map (OpenStreetMap)", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"}],
      ["OSM Humanitarian", "https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png",
        {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors, tiles HOT / OSM France"}],
      ["CARTO Light", "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        {maxZoom: 20, subdomains: "abcd", attribution: "&copy; OpenStreetMap contributors &copy; CARTO"}],
      ["CARTO Voyager", "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        {maxZoom: 20, subdomains: "abcd", attribution: "&copy; OpenStreetMap contributors &copy; CARTO"}],
      ["CARTO Dark", "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        {maxZoom: 20, subdomains: "abcd", attribution: "&copy; OpenStreetMap contributors &copy; CARTO", dark: true}],
      ["Esri Satellite", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        {maxZoom: 19, attribution: "Imagery &copy; Esri", dark: true}],
      ["Satellite + roads (Esri)", [
        ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {maxZoom: 19, attribution: "Imagery &copy; Esri"}],
        ["https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}", {maxZoom: 19}],
        ["https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", {maxZoom: 19}],
      ], {dark: true}],
      ["Esri Streets", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        {maxZoom: 19, attribution: "&copy; Esri"}],
      ["Esri Topographic", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        {maxZoom: 19, attribution: "&copy; Esri"}],
      ["OpenTopoMap", "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        {maxZoom: 17, attribution: "&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)"}],
    ],
    GOOGLE_MAPS: [["Google Road", "roadmap"], ["Google Satellite", "satellite", true], ["Google Hybrid", "hybrid", true],
      ["Google Terrain", "terrain"]],
    baseLayers(map, opts = {}) {
      const layers = {}, dark = new Set();
      const google = !!(cfg.gkey && L.gridLayer && L.gridLayer.googleMutant);
      if (google) {
        F.loadGoogle();
        for (const [n, type, isDark] of F.GOOGLE_MAPS) {
          layers[n] = L.gridLayer.googleMutant({type, maxZoom: 21});
          if (isDark) dark.add(n);
        }
      } else if (cfg.gkey) {
        console.warn("[maps] a Google key is set but the Google layer plugin did not load; showing free maps only");
      }
      for (const [n, url, o] of F.FREE_MAPS) {
        layers[n] = Array.isArray(url) ? L.layerGroup(url.map(([u, lo]) => L.tileLayer(u, lo))) : L.tileLayer(url, o);
        if (o.dark) dark.add(n);
      }
      let saved = null;
      try { saved = localStorage.getItem("mgt-map"); } catch (e) { /* private mode */ }
      // Default is a road map: Google Road when the key works, otherwise OpenStreetMap.
      const name = layers[saved] ? saved : (google ? "Google Road" : F.ROAD);
      const mark = n => map.getContainer().classList.toggle("map-imagery", dark.has(n));
      layers[name].addTo(map);
      mark(name);
      map.on("baselayerchange", e => {
        mark(e.name);
        try { localStorage.setItem("mgt-map", e.name); } catch (err) { /* private mode */ }
      });
      const control = opts.control === false ? null : L.control.layers(layers, {}, {position: "topright"}).addTo(map);
      const entry = {map, layers, control, mark};
      F._maps.push(entry);
      if (opts.quick !== false) F._quickControls(entry);
      if (F._googleFailed) F._dropGoogle();
      return layers;
    },
    // Geofence zones as a switchable overlay ("Geofences" in the layers menu), drawn under the vehicles.
    async geofenceOverlay(map) {
      const m = F._maps.find(x => x.map === map);
      try {
        const d = await F.json("/geofences/api/");
        const g = L.featureGroup();
        for (const f of d.fences.filter(x => x.active)) {
          const style = {color: f.color, weight: 2, fillOpacity: .1, interactive: true};
          (f.shape === "circle" ? L.circle([f.center_lat, f.center_lon], {radius: f.radius_m, ...style}) : L.polygon(f.points, style))
            .bindTooltip(F.esc(f.name), {sticky: true}).addTo(g);
        }
        g.addTo(map);
        if (m && m.control) m.control.addOverlay(g, "Geofences");
        return g;
      } catch (e) { return null; }
    },
    ROAD: "Road map (OpenStreetMap)",
    SAT: "Satellite + roads (Esri)",
    // Switch a map to another base layer by name (fires baselayerchange so the choice is remembered).
    setBase(m, name) {
      if (!m.layers[name]) return;
      for (const [n, l] of Object.entries(m.layers)) if (n !== name && m.map.hasLayer(l)) m.map.removeLayer(l);
      if (!m.map.hasLayer(m.layers[name])) m.layers[name].addTo(m.map);
      m.map.fire("baselayerchange", {name, layer: m.layers[name]});
    },
    // Road / Satellite one-click switch and a Locate me button, under the zoom buttons.
    _quickControls(m) {
      const Quick = L.Control.extend({
        options: {position: "topleft"},
        onAdd() {
          const bar = L.DomUtil.create("div", "leaflet-bar map-quick");
          const btn = (icon, title, fn) => {
            const a = L.DomUtil.create("a", "", bar);
            a.href = "#"; a.title = title; a.setAttribute("role", "button"); a.setAttribute("aria-label", title);
            a.innerHTML = `<i class="bi ${icon}"></i>`;
            L.DomEvent.on(a, "click", e => { L.DomEvent.stop(e); fn(a); });
            return a;
          };
          btn("bi-signpost-split", "Road map", () => F.setBase(m, m.layers["Google Road"] ? "Google Road" : F.ROAD));
          btn("bi-globe-americas", "Satellite with roads", () => F.setBase(m, m.layers["Google Hybrid"] ? "Google Hybrid" : F.SAT));
          btn("bi-crosshair", "Locate me", a => F.locateMe(m.map, a));
          L.DomEvent.disableClickPropagation(bar);
          return bar;
        },
      });
      new Quick().addTo(m.map);
    },
    // Show where this browser is (phone GPS or computer Wi-Fi location) and zoom there.
    locateMe(map, btn) {
      const fail = msg => { if (btn) btn.classList.remove("busy"); F.toast(msg); };
      if (!window.isSecureContext) return fail("Locate me needs the site opened over https://.");
      if (!navigator.geolocation) return fail("This browser can't share its location.");
      if (btn) btn.classList.add("busy");
      navigator.geolocation.getCurrentPosition(pos => {
        if (btn) btn.classList.remove("busy");
        const ll = [pos.coords.latitude, pos.coords.longitude], acc = pos.coords.accuracy || 0;
        if (map._meLayer) map.removeLayer(map._meLayer);
        map._meLayer = L.layerGroup([
          L.circle(ll, {radius: acc, color: "#2563eb", weight: 1, fillOpacity: .12, interactive: false}),
          L.circleMarker(ll, {radius: 8, color: "#fff", weight: 3, fillColor: "#2563eb", fillOpacity: 1})
            .bindTooltip(`You are here (± ${Math.round(acc)} m)`),
        ]).addTo(map);
        map.setView(ll, Math.max(map.getZoom(), acc > 1000 ? 13 : 16));
      }, err => {
        const why = {1: "Location permission was refused. Allow location for this site in the browser's address bar (site settings), then try again.",
          2: "The browser couldn't work out your location. Turn on location services (and GPS on a phone) and try again.",
          3: "Finding your location took too long. Try again outdoors or with Wi-Fi on."}[err.code];
        fail(why || "Couldn't get your location: " + err.message);
      }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 10000});
    },
    _maps: [],
    _googleFailed: false,
    // What Google's console error codes mean, so the notice says what to fix.
    GOOGLE_ERRORS: {
      RefererNotAllowedMapError: "this website is not in the key's allowed websites. In Google Cloud → Credentials → your key → Website restrictions add https://gps.melticetech.com/*",
      ApiNotActivatedMapError: "the Maps JavaScript API is not enabled for this key's project. Enable it in Google Cloud → APIs & Services → Library.",
      ApiTargetBlockedMapError: "the key's API restrictions don't include the Maps JavaScript API. Add it under the key's API restrictions.",
      BillingNotEnabledMapError: "billing is not enabled on the Google Cloud project.",
      InvalidKeyMapError: "the key is not valid. Copy it again from Google Cloud → Credentials.",
      MissingKeyMapError: "no key was sent.",
      ExpiredKeyMapError: "the key has expired or was deleted.",
      DeletedApiProjectMapError: "the key's Google Cloud project was deleted.",
    },
    // Google reports the reason only on the console, so remember the last *MapError it logs.
    watchGoogleErrors(target = window) {
      const c = target.console, orig = c.error.bind(c);
      c.error = (...a) => {
        const m = String(a[0] || "").match(/\b([A-Z][A-Za-z]+MapError)\b/);
        if (m) F._googleError = m[1];
        return orig(...a);
      };
    },
    googleReason() {
      const code = F._googleError;
      return code ? `${code}: ${F.GOOGLE_ERRORS[code] || "see Google's Maps error list."}` : "";
    },
    loadGoogle() {
      if (F._googleLoading) return;
      F._googleLoading = true;
      F.watchGoogleErrors();
      window.gm_authFailure = () => setTimeout(() => F._dropGoogle(
        "Google rejected the Maps key, so the Google maps are hidden. " + (F.googleReason() || "Use Test on Settings → Integrations to see why."), true), 300);
      window.__mgtGoogleReady = () => {};
      const s = document.createElement("script");
      s.src = "https://maps.googleapis.com/maps/api/js?key=" + encodeURIComponent(cfg.gkey) + "&callback=__mgtGoogleReady&loading=async";
      s.async = true;
      s.onerror = () => F._dropGoogle("Google Maps could not be loaded from this browser (network, firewall or ad blocker), so the Google maps are hidden.", true);
      document.head.appendChild(s);
      setTimeout(() => { if (!(window.google && google.maps && google.maps.Map)) F._dropGoogle("Google Maps did not load within 20 s, so the Google maps are hidden. Reload to try again.", true); }, 20000);
    },
    // Switch every map from Google to OpenStreetMap (bad key, blocked network) and hide the Google choices.
    _dropGoogle(message, sticky) {
      const first = !F._googleFailed;
      F._googleFailed = true;
      for (const m of F._maps) {
        let wasGoogle = false;
        for (const [n, layer] of Object.entries(m.layers)) {
          if (!n.startsWith("Google")) continue;
          if (m.map.hasLayer(layer)) { m.map.removeLayer(layer); wasGoogle = true; }
          if (m.control) m.control.removeLayer(layer);
          delete m.layers[n];
        }
        if (wasGoogle) { m.layers[F.ROAD].addTo(m.map); m.mark(F.ROAD); }
      }
      if (first && message) { console.warn("[maps] " + message); F.toast(message, sticky); }
    },
    toast(message, sticky) {
      const t = document.createElement("div");
      t.className = "alert alert-warning shadow py-2 px-3 small map-toast d-flex gap-2 align-items-start";
      const text = document.createElement("span");
      text.textContent = message;
      const x = document.createElement("button");
      x.type = "button";
      x.className = "btn-close btn-sm ms-auto";
      x.setAttribute("aria-label", "Close");
      x.onclick = () => t.remove();
      t.append(text, x);
      document.body.appendChild(t);
      if (!sticky) setTimeout(() => t.remove(), 9000);
    },
  };

  // light / dark / match-system theme (applied early by an inline script in base.html)
  const media = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  const ICON = {light: "bi-sun", dark: "bi-moon-stars", auto: "bi-circle-half"};
  function applyTheme(pref) {
    const dark = pref === "dark" || (pref === "auto" && media && media.matches);
    document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
    document.documentElement.dataset.themePref = pref;
    document.querySelectorAll("[data-theme-icon]").forEach(i => { i.className = "bi " + ICON[pref]; });
    document.querySelectorAll("[data-theme-set]").forEach(b => b.classList.toggle("active", b.dataset.themeSet === pref));
    document.dispatchEvent(new CustomEvent("themechange", {detail: {dark}}));
  }
  F.theme = () => document.documentElement.getAttribute("data-bs-theme");
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-theme-set]");
    if (!b) return;
    try { localStorage.setItem("mgt-theme", b.dataset.themeSet); } catch (err) { /* private mode */ }
    applyTheme(b.dataset.themeSet);
  });
  if (media && media.addEventListener) media.addEventListener("change", () => {
    if ((document.documentElement.dataset.themePref || "auto") === "auto") applyTheme("auto");
  });
  applyTheme(document.documentElement.dataset.themePref || "auto");

  // clock + receiver pill
  const clock = document.getElementById("uaeClock");
  const tick = () => { if (clock) clock.textContent = F.fmt(new Date().toISOString()) + " UAE"; };
  tick(); setInterval(tick, 1000);
  const pill = document.getElementById("rxPill");
  async function rx() {
    if (!pill || !cfg.rx) return;
    try {
      const s = await F.json(cfg.rx);
      pill.className = "rx-pill " + (s.up ? "up" : "down");
      pill.title = s.up ? `Receiver listening on ${s.host}:${s.port} — ${s.active} active connection(s)` : s.detail;
      pill.innerHTML = `<i class="bi bi-circle-fill"></i> Receiver ${s.up ? "online" : "offline"}`;
    } catch (e) { pill.className = "rx-pill down"; }
  }
  rx(); setInterval(rx, 10000);
})();
