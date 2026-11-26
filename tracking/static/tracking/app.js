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
    /* Base maps. With a Google Maps key (Settings → Integrations) Google Road / Satellite / Hybrid come
       first; OpenStreetMap and Esri imagery are always there as a fallback. The choice is remembered. */
    baseLayers(map, opts = {}) {
      const layers = {};
      const google = !!(cfg.gkey && L.gridLayer && L.gridLayer.googleMutant);
      if (google) {
        F.loadGoogle();
        layers["Google Road"] = L.gridLayer.googleMutant({type: "roadmap", maxZoom: 21});
        layers["Google Satellite"] = L.gridLayer.googleMutant({type: "satellite", maxZoom: 21});
        layers["Google Hybrid"] = L.gridLayer.googleMutant({type: "hybrid", maxZoom: 21});
      }
      layers["OpenStreetMap"] = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"});
      layers["Esri Satellite"] = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        {maxZoom: 19, attribution: "Imagery &copy; Esri"});
      const imagery = name => /Satellite|Hybrid/.test(name);
      let saved = null;
      try { saved = localStorage.getItem("mgt-map"); } catch (e) { /* private mode */ }
      const name = layers[saved] ? saved : Object.keys(layers)[0];
      const mark = n => map.getContainer().classList.toggle("map-imagery", imagery(n));
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
    loadGoogle() {
      if (F._googleLoading) return;
      F._googleLoading = true;
      window.gm_authFailure = () => F._dropGoogle("Google rejected the Maps key. Showing OpenStreetMap. An admin can fix the key under Settings → Integrations.");
      window.__mgtGoogleReady = () => {};
      const s = document.createElement("script");
      s.src = "https://maps.googleapis.com/maps/api/js?key=" + encodeURIComponent(cfg.gkey) + "&callback=__mgtGoogleReady";
      s.async = true;
      s.onerror = () => F._dropGoogle("Google Maps could not be loaded. Showing OpenStreetMap.");
      document.head.appendChild(s);
      setTimeout(() => { if (!(window.google && google.maps && google.maps.Map)) F._dropGoogle("Google Maps did not load in time. Showing OpenStreetMap."); }, 15000);
    },
    // Switch every map from Google to OpenStreetMap (bad key, blocked network) and hide the Google choices.
    _dropGoogle(message) {
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
      if (first && message) F.toast(message);
    },
    toast(message) {
      const t = document.createElement("div");
      t.className = "alert alert-warning shadow py-2 px-3 small map-toast";
      t.textContent = message;
      document.body.appendChild(t);
      setTimeout(() => t.remove(), 9000);
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
