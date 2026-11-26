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
    vehicleIcon(state, angle) {
      const c = F.stateColor[state] || "#6b7280";
      const rot = state === "moving" ? `style="transform: rotate(${angle || 0}deg)"` : "";
      const glyph = state === "moving" ? "bi-arrow-up" : (state === "nosignal" ? "bi-wifi-off" : "bi-truck");
      return L.divIcon({className: "", iconSize: [30, 30], iconAnchor: [15, 15], popupAnchor: [0, -14],
        html: `<div class="veh-icon" style="width:30px;height:30px;background:${c}"><i class="bi ${glyph}" ${rot}></i></div>`});
    },
    /* Base maps. With a Google Maps key (Settings → Integrations) Google Road / Satellite / Hybrid / Terrain
       come first; the free maps below need no key and are always there. The choice is remembered.
       "dark: true" maps are already dark (or photos), so the dark theme doesn't re-tint them. */
    FREE_MAPS: [
      ["OpenStreetMap", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
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
        layers[n] = L.tileLayer(url, o);
        if (o.dark) dark.add(n);
      }
      let saved = null;
      try { saved = localStorage.getItem("mgt-map"); } catch (e) { /* private mode */ }
      const name = layers[saved] ? saved : Object.keys(layers)[0];
      const mark = n => map.getContainer().classList.toggle("map-imagery", dark.has(n));
      layers[name].addTo(map);
      mark(name);
      map.on("baselayerchange", e => {
        mark(e.name);
        try { localStorage.setItem("mgt-map", e.name); } catch (err) { /* private mode */ }
      });
      const control = opts.control === false ? null : L.control.layers(layers, {}, {position: "topright"}).addTo(map);
      F._maps.push({map, layers, control, mark});
      if (F._googleFailed) F._dropGoogle();
      return layers;
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
        if (wasGoogle) { m.layers["OpenStreetMap"].addTo(m.map); m.mark("OpenStreetMap"); }
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
