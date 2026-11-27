/* Geofences page (/geofences/): draw circle or polygon zones, choose alerts, see every crossing. */
(function () {
  const D = JSON.parse(document.getElementById("gf-data").textContent);
  const U = window.GF_URLS, esc = Fleet.esc, $ = id => document.getElementById(id);
  const canEdit = D.can_edit;
  let fences = D.fences, events = D.events;

  const map = L.map("map", {doubleClickZoom: true}).setView([24.45, 54.6], 10);
  Fleet.baseLayers(map);
  const fenceLayer = L.featureGroup().addTo(map);
  const layers = {};
  let draft = null, draftLayer = L.layerGroup().addTo(map), drawMode = null, selected = null, evMarker = null;

  // vehicles at their last position
  for (const v of D.vehicles) {
    if (v.lat == null) continue;
    L.circleMarker([v.lat, v.lon], {radius: 7, color: "#fff", weight: 2, fillColor: "#0f766e", fillOpacity: 1})
      .addTo(map).bindTooltip(`${esc(v.name)} · ${Fleet.ago(v.fix)}`);
  }

  const fmtR = m => m >= 1000 ? (m / 1000).toFixed(m >= 10000 ? 0 : 1) + " km" : Math.round(m) + " m";
  const shapeText = f => f.shape === "circle" ? `Circle ${fmtR(f.radius_m)}` : `Polygon, ${f.points.length} corners`;
  const vehText = f => !f.device_ids.length ? "All vehicles" :
    f.device_ids.map(id => (D.vehicles.find(v => v.id === id) || {name: "#" + id}).name).join(", ");

  function shapeLayer(f, opts = {}) {
    const style = {color: f.color, weight: 2, fillOpacity: f.active ? .15 : .05, dashArray: f.active ? null : "6 6", ...opts};
    return f.shape === "circle" ? L.circle([f.center_lat, f.center_lon], {radius: f.radius_m, ...style})
      : L.polygon(f.points, style);
  }

  function drawFences() {
    fenceLayer.clearLayers();
    for (const f of fences) {
      if (selected && draft && f.id === selected) continue;  // being edited: the draft shows it
      layers[f.id] = shapeLayer(f).addTo(fenceLayer).bindTooltip(esc(f.name), {sticky: true}).on("click", () => { if (!drawMode) select(f.id); });  // while drawing, the click is a corner
    }
  }

  function renderList() {
    $("gfCount").textContent = fences.length;
    $("gfList").innerHTML = fences.length ? fences.map(f => `
      <div class="pitem ${f.id === selected ? "sel" : ""}" data-id="${f.id}">
        <div class="d-flex justify-content-between gap-2">
          <span class="nm"><span class="gf-swatch" style="background:${esc(f.color)}"></span>${esc(f.name)}</span>
          ${f.active ? "" : '<span class="badge text-bg-secondary">Paused</span>'}
        </div>
        <div class="meta">${shapeText(f)} · ${esc(vehText(f))}</div>
        <div class="meta">${f.alert_enter ? '<span class="gf-tag in"><i class="bi bi-box-arrow-in-right"></i> Enter alert</span>' : ""}
          ${f.alert_exit ? '<span class="gf-tag out"><i class="bi bi-box-arrow-right"></i> Exit alert</span>' : ""}
          ${!f.alert_enter && !f.alert_exit ? "Log only" : ""}
          ${(f.inside || []).length ? `· <b>Inside now:</b> ${esc(f.inside.join(", "))}` : ""}</div>
      </div>`).join("") : `<div class="p-3 text-body-secondary small">No zones yet.${canEdit ? " Press <b>New zone</b>, then draw a circle or a polygon on the map." : " An admin can add them."}</div>`;
  }

  const statusBadge = e => ({sent: ["success", "WhatsApp sent"], failed: ["danger", "WhatsApp failed"], pending: ["info", "Sending…"],
    skipped: ["secondary", "Not sent"], off: ["light", "Logged"]}[e.alert_status] || ["light", e.alert_status]);
  function renderEvents() {
    $("gfEvents").innerHTML = events.length ? events.map(e => {
      const [c, t] = statusBadge(e);
      return `<div class="pitem" data-ev="${e.id}">
        <div class="d-flex justify-content-between gap-2">
          <span class="nm"><i class="bi ${e.kind === "enter" ? "bi-box-arrow-in-right text-success" : "bi-box-arrow-right text-danger"}"></i>
            ${esc(e.device)} ${e.kind === "enter" ? "entered" : "left"} ${esc(e.geofence)}</span>
          <span class="small text-body-secondary text-nowrap">${Fleet.fmt(e.timestamp_utc).slice(0, 16)}</span></div>
        <div class="meta">${e.speed} km/h · <span class="badge text-bg-${c}" ${e.alert_detail ? `title="${esc(e.alert_detail)}"` : ""}>${t}</span>
          ${e.alert_detail && e.alert_status !== "sent" ? `<span class="d-block">${esc(e.alert_detail)}</span>` : ""}</div>
      </div>`;
    }).join("") : '<div class="p-3 text-body-secondary small">No crossings yet. They appear here as soon as a vehicle enters or leaves a zone.</div>';
  }

  // ------------------------------------------------------------------ card
  function select(id) {
    stopDrawing();
    selected = id;
    const f = fences.find(x => x.id === id);
    draft = null; draftLayer.clearLayers();
    if (f && layers[f.id]) map.fitBounds(layers[f.id].getBounds(), {padding: [60, 60], maxZoom: 17});
    if (canEdit) openEditor(f || null); else openView(f);
    renderList(); drawFences();
  }

  function openView(f) {
    const card = $("gfCard");
    card.innerHTML = `<div class="d-flex justify-content-between"><b>${esc(f.name)}</b><button class="btn-close" id="gfClose"></button></div>
      <div class="small text-body-secondary mb-2">${shapeText(f)} · ${esc(vehText(f))}</div>
      <div class="small">${f.alert_enter ? "WhatsApp on enter. " : ""}${f.alert_exit ? "WhatsApp on exit. " : ""}${esc(f.notes || "")}</div>`;
    card.hidden = false;
    $("gfClose").onclick = closeCard;
  }

  function closeCard() {
    stopDrawing();
    $("gfCard").hidden = true; selected = null; draft = null; draftLayer.clearLayers();
    renderList(); drawFences();
  }

  function openEditor(f) {
    const isNew = !f;
    f = f || {name: "", shape: null, color: "#2563eb", active: true, alert_enter: true, alert_exit: false, recipients: "", notes: "", device_ids: []};
    draft = f.shape ? {shape: f.shape, center: f.center_lat != null ? [f.center_lat, f.center_lon] : null, radius: f.radius_m, points: f.points.map(p => p.slice())} : null;
    const card = $("gfCard");
    card.innerHTML = `
      <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
        <input class="form-control form-control-sm fw-semibold" id="gfName" value="${esc(f.name)}" maxlength="100" placeholder="Zone name, e.g. Office or Home">
        <input type="color" class="form-control form-control-color form-control-sm" id="gfColor" value="${esc(f.color)}" title="Colour">
        <button class="btn-close" id="gfClose" aria-label="Close"></button></div>
      <div class="small mb-1">Shape</div>
      <div class="btn-group btn-group-sm w-100 mb-1" role="group">
        <button type="button" class="btn btn-outline-primary" id="gfCircle"><i class="bi bi-circle"></i> Circle</button>
        <button type="button" class="btn btn-outline-primary" id="gfPoly"><i class="bi bi-hexagon"></i> Polygon</button>
      </div>
      <div class="input-group input-group-sm mb-2" id="gfRadiusRow" hidden><span class="input-group-text">Radius</span>
        <input type="number" class="form-control" id="gfRadius" min="10" max="500000" step="10"><span class="input-group-text">m</span></div>
      <div class="small text-body-secondary mb-2" id="gfShapeInfo"></div>
      <div class="small mb-1">Send a WhatsApp alert when a vehicle</div>
      <div class="d-flex gap-3 mb-2">
        <label class="form-check-label small"><input type="checkbox" class="form-check-input me-1" id="gfEnter" ${f.alert_enter ? "checked" : ""}>enters</label>
        <label class="form-check-label small"><input type="checkbox" class="form-check-input me-1" id="gfExit" ${f.alert_exit ? "checked" : ""}>leaves</label>
        <label class="form-check-label small ms-auto"><input type="checkbox" class="form-check-input me-1" id="gfActive" ${f.active ? "checked" : ""}>Active</label>
      </div>
      <div class="small mb-1">Vehicles <span class="text-body-secondary">(none ticked = all)</span></div>
      <div class="gf-vehicles mb-2">${D.vehicles.map(v => `<label class="form-check-label small d-block"><input type="checkbox" class="form-check-input me-1 gfVeh" value="${v.id}" ${f.device_ids.includes(v.id) ? "checked" : ""}>${esc(v.name)}</label>`).join("") || '<span class="small text-body-secondary">No vehicles registered.</span>'}</div>
      <details class="mb-2" ${f.recipients ? "open" : ""}><summary class="small">Other WhatsApp numbers for this zone</summary>
        <textarea class="form-control form-control-sm mt-1" id="gfRecips" rows="2" placeholder="Blank = the default numbers on Settings. e.g. +971501234567">${esc(f.recipients)}</textarea></details>
      <textarea class="form-control form-control-sm mb-2" id="gfNotes" rows="2" maxlength="2000" placeholder="Notes">${esc(f.notes)}</textarea>
      <div class="d-flex gap-2">
        <button class="btn btn-success btn-sm" id="gfSave"><i class="bi bi-check2"></i> ${isNew ? "Create zone" : "Save changes"}</button>
        ${isNew ? "" : '<button class="btn btn-outline-danger btn-sm ms-auto" id="gfDelete"><i class="bi bi-trash"></i> Delete</button>'}
      </div>
      <div class="small mt-2" id="gfMsg"></div>`;
    card.hidden = false;
    $("gfClose").onclick = closeCard;
    $("gfCircle").onclick = () => startDrawing("circle");
    $("gfPoly").onclick = () => startDrawing("polygon");
    $("gfRadius").oninput = e => { const r = +e.target.value; if (draft && draft.shape === "circle" && r >= 10) { draft.radius = r; renderDraft(false); } };
    $("gfSave").onclick = () => save(isNew ? null : f.id);
    if (!isNew) $("gfDelete").onclick = () => remove(f);
    renderDraft(false);
    if (isNew) startDrawing("circle");
  }

  function msg(text, ok) { const m = $("gfMsg"); m.className = "small mt-2 " + (ok ? "text-success" : "text-danger"); m.textContent = text; }

  // ------------------------------------------------------------------ drawing
  const handleIcon = cls => L.divIcon({className: "gf-handle " + (cls || ""), iconSize: [14, 14], iconAnchor: [7, 7]});
  function hint(html) { const h = $("gfHint"); h.hidden = !html; h.innerHTML = html || ""; }

  function startDrawing(mode) {
    stopDrawing();
    drawMode = mode;
    draft = {shape: mode, center: null, radius: null, points: []};
    renderDraft(false);
    map.doubleClickZoom.disable();
    map.getContainer().classList.add("gf-drawing");
    hint(mode === "circle" ? "<b>Circle:</b> click the centre, then click the edge. <a href='#' id='gfCancel'>Cancel</a>"
      : "<b>Polygon:</b> click each corner. Click the first corner or <a href='#' id='gfFinish'>Finish</a> to close it. <a href='#' id='gfCancel'>Cancel</a>");
    const c = $("gfCancel"); if (c) c.onclick = e => { e.preventDefault(); stopDrawing(); draft = null; renderDraft(false); };
    const fin = $("gfFinish"); if (fin) fin.onclick = e => { e.preventDefault(); finishPolygon(); };
    ["gfCircle", "gfPoly"].forEach(id => {
      const on = (id === "gfCircle") === (mode === "circle"), b = $(id);
      if (b) { b.classList.toggle("btn-primary", on); b.classList.toggle("btn-outline-primary", !on); }
    });
  }

  function stopDrawing() {
    drawMode = null; hint("");
    map.doubleClickZoom.enable();
    map.getContainer().classList.remove("gf-drawing");
  }

  function finishPolygon() {
    if (!draft || draft.points.length < 3) { hint("<b>Polygon:</b> a zone needs at least 3 corners. Keep clicking."); return; }
    stopDrawing(); renderDraft(true);
  }

  map.on("click", e => {
    if (!drawMode) return;
    const ll = [e.latlng.lat, e.latlng.lng];
    if (drawMode === "circle") {
      if (!draft.center) { draft.center = ll; draft.radius = 1; renderDraft(false); return; }
      draft.radius = Math.max(10, map.distance(draft.center, ll));
      stopDrawing(); renderDraft(true);
    } else {
      draft.points.push(ll); renderDraft(false);
    }
  });
  map.on("dblclick", () => { if (drawMode === "polygon") finishPolygon(); });
  map.on("mousemove", e => {
    if (!drawMode || !draft) return;
    if (drawMode === "circle" && draft.center) { draft.radius = Math.max(10, map.distance(draft.center, e.latlng)); renderDraft(false, true); }
    if (drawMode === "polygon" && draft.points.length) renderDraft(false, true, [e.latlng.lat, e.latlng.lng]);
  });

  let shapeL = null;
  function renderDraft(editable, light, cursor) {
    if (light && shapeL) {  // cheap update while the mouse moves
      if (draft.shape === "circle") shapeL.setRadius(draft.radius);
      else shapeL.setLatLngs(cursor ? [...draft.points, cursor] : draft.points);
      updateInfo();
      return;
    }
    draftLayer.clearLayers(); shapeL = null;
    if (!draft) { updateInfo(); return; }
    const color = ($("gfColor") || {}).value || "#2563eb";
    const style = {color, weight: 3, fillOpacity: .18, interactive: false};
    if (draft.shape === "circle" && draft.center) {
      shapeL = L.circle(draft.center, {radius: draft.radius || 1, ...style}).addTo(draftLayer);
      if (!drawMode) {
        const c = L.marker(draft.center, {draggable: true, icon: handleIcon("center"), title: "Drag to move"}).addTo(draftLayer);
        const edgeLL = () => [draft.center[0], draft.center[1] + draft.radius / (111320 * Math.cos(draft.center[0] * Math.PI / 180))];
        const edge = L.marker(edgeLL(), {draggable: true, icon: handleIcon(), title: "Drag to resize"}).addTo(draftLayer);
        c.on("drag", e => { draft.center = [e.latlng.lat, e.latlng.lng]; shapeL.setLatLng(e.latlng); edge.setLatLng(edgeLL()); updateInfo(); });
        edge.on("drag", e => { draft.radius = Math.max(10, map.distance(draft.center, e.latlng)); shapeL.setRadius(draft.radius); updateInfo(); });
        edge.on("dragend", () => edge.setLatLng(edgeLL()));
      }
    } else if (draft.shape === "polygon" && draft.points.length) {
      const closed = !drawMode;
      shapeL = (closed ? L.polygon(draft.points, style) : L.polyline(cursor ? [...draft.points, cursor] : draft.points, {...style, dashArray: "6 6"})).addTo(draftLayer);
      draft.points.forEach((p, i) => {
        const m = L.marker(p, {draggable: closed, icon: handleIcon(i === 0 && !closed ? "first" : ""), title: closed ? "Drag to move this corner" : (i === 0 ? "Click to close the shape" : "")}).addTo(draftLayer);
        if (closed) m.on("drag", e => { draft.points[i] = [e.latlng.lat, e.latlng.lng]; shapeL.setLatLngs(draft.points); updateInfo(); });
        else if (i === 0) m.on("click", e => { L.DomEvent.stop(e); finishPolygon(); });
      });
    }
    updateInfo();
  }

  function updateInfo() {
    const info = $("gfShapeInfo"); if (!info) return;
    const row = $("gfRadiusRow");
    row.hidden = !(draft && draft.shape === "circle" && draft.center);
    if (!draft || (!draft.center && !draft.points.length)) { info.textContent = "Pick Circle or Polygon, then draw on the map."; return; }
    if (draft.shape === "circle") {
      if (document.activeElement !== $("gfRadius")) $("gfRadius").value = Math.round(draft.radius || 0);
      info.textContent = drawMode ? "Click the edge to set the radius." : "Drag the centre to move it, or the white square to resize.";
    } else info.textContent = `${draft.points.length} corner(s). ${drawMode ? "" : "Drag a corner to adjust it."}`;
  }

  // ------------------------------------------------------------------ save / delete
  async function post(url, body) {
    const r = await fetch(url, {method: "POST", credentials: "same-origin", body: body ? JSON.stringify(body) : null,
      headers: {"Content-Type": "application/json", "X-CSRFToken": U.csrf, "Accept": "application/json"}});
    let d = {};
    try { d = await r.json(); } catch (e) { d = {errors: [r.status + " " + r.statusText]}; }
    return [r.ok, d];
  }

  async function save(id) {
    if (drawMode === "polygon" && draft && draft.points.length >= 3) finishPolygon();
    if (!draft || (draft.shape === "circle" && !draft.center) || (draft.shape === "polygon" && draft.points.length < 3) || drawMode) {
      msg("Finish drawing the zone on the map first."); return;
    }
    const body = {
      name: $("gfName").value.trim(), color: $("gfColor").value, shape: draft.shape,
      center_lat: draft.center && draft.center[0], center_lon: draft.center && draft.center[1], radius_m: draft.radius,
      points: draft.shape === "polygon" ? draft.points : [],
      alert_enter: $("gfEnter").checked, alert_exit: $("gfExit").checked, active: $("gfActive").checked,
      device_ids: [...document.querySelectorAll(".gfVeh:checked")].map(x => +x.value),
      recipients: $("gfRecips").value, notes: $("gfNotes").value,
    };
    const [ok, d] = await post(id ? `${U.api}${id}/` : U.api, body);
    if (!ok) { msg((d.errors || [d.detail || "Not saved."]).join(" ")); return; }
    const i = fences.findIndex(x => x.id === d.fence.id);
    d.fence.inside = i >= 0 && !id ? fences[i].inside : (i >= 0 ? (fences[i].inside || []) : []);
    if (i >= 0) fences[i] = d.fence; else fences.push(d.fence);
    fences.sort((a, b) => a.name.localeCompare(b.name));
    selected = d.fence.id;
    draft = null; draftLayer.clearLayers();
    renderList(); drawFences(); openEditor(d.fence);
    msg(id ? "Saved." : "Zone created. Alerts start with the next position the tracker sends.", true);
  }

  async function remove(f) {
    if (!confirm(`Delete the zone “${f.name}”? Its crossings stay in the log.`)) return;
    const [ok, d] = await post(`${U.api}${f.id}/delete/`);
    if (!ok) { msg(d.detail || "Not deleted."); return; }
    fences = fences.filter(x => x.id !== f.id);
    closeCard();
  }

  // ------------------------------------------------------------------ events
  $("gfEvents").addEventListener("click", e => {
    const it = e.target.closest("[data-ev]"); if (!it) return;
    const ev = events.find(x => x.id === +it.dataset.ev); if (!ev) return;
    if (evMarker) map.removeLayer(evMarker);
    evMarker = L.circleMarker([ev.latitude, ev.longitude], {radius: 9, color: "#fff", weight: 3, fillColor: ev.kind === "enter" ? "#16a34a" : "#dc2626", fillOpacity: 1})
      .addTo(map).bindTooltip(`${esc(ev.device)} ${ev.kind === "enter" ? "entered" : "left"} ${esc(ev.geofence)}<br>${Fleet.fmt(ev.timestamp_utc)}`).openTooltip();
    map.setView([ev.latitude, ev.longitude], Math.max(map.getZoom(), 15));
  });
  async function poll() {
    try {
      const after = events.length ? events[0].id : 0;
      const pending = events.some(e => e.alert_status === "pending");
      const d = await Fleet.json(`${U.events}${pending ? "" : "?after=" + after}`);
      if (pending) events = d.events;
      else if (d.events.length) {
        events = [...d.events, ...events].slice(0, 100);
        const b = $("gfNewEv"); b.hidden = false; b.textContent = d.events.length + (+b.textContent || 0);
      }
      if (pending || d.events.length) renderEvents();
    } catch (e) { /* offline for a moment */ }
  }
  document.querySelector('[data-bs-target="#tabEvents"]').addEventListener("shown.bs.tab", () => { $("gfNewEv").hidden = true; $("gfNewEv").textContent = ""; });
  setInterval(poll, 15000);

  // ------------------------------------------------------------------ start
  $("gfList").addEventListener("click", e => { const it = e.target.closest(".pitem"); if (it) select(+it.dataset.id); });
  if ($("gfNew")) $("gfNew").onclick = () => { selected = null; renderList(); drawFences(); openEditor(null); };
  renderList(); renderEvents(); drawFences();
  if (fences.length) map.fitBounds(fenceLayer.getBounds(), {padding: [40, 40], maxZoom: 15});
  else {
    const pts = D.vehicles.filter(v => v.lat != null).map(v => [v.lat, v.lon]);
    if (pts.length) map.fitBounds(L.latLngBounds(pts), {padding: [60, 60], maxZoom: 14});
  }
})();
