/* Settings page: test buttons for the Google Maps, Anthropic and OpenAI keys. */
(function () {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector("input[name=csrfmiddlewaretoken]").value;
  const show = (el, ok, text) => {
    el.className = "small " + (ok === null ? "text-muted" : ok ? "text-success" : "text-danger");
    el.innerHTML = (ok === null ? '<span class="spinner-border spinner-border-sm me-1"></span>' :
      `<i class="bi ${ok ? "bi-check-circle" : "bi-x-circle"} me-1"></i>`) + Fleet.esc(text);
  };

  // Google Maps: load the Maps API in a throw-away iframe so each test starts clean and a bad
  // key can't break this page. Google reports a rejected key through window.gm_authFailure.
  const gBtn = $("testGoogle");
  if (gBtn) {
    let pending = null;
    window.addEventListener("message", e => {
      if (!pending || e.source !== pending.win || !e.data || e.data.mgt !== "gmaps") return;
      clearTimeout(pending.timer);
      const why = e.data.code ? `Google says ${e.data.code}: ${Fleet.GOOGLE_ERRORS[e.data.code] || "see Google's Maps error list."}` : e.data.text;
      show($("googleResult"), e.data.ok, why);
      if (!e.data.ok) $("googleFrameWrap").hidden = true;
      pending = null;
    });
    gBtn.addEventListener("click", () => {
      const key = $("id_google_maps_api_key").value.trim() || (Fleet.cfg.gkey || "");
      if (!key) { show($("googleResult"), false, "Enter a key first."); return; }
      const wrap = $("googleFrameWrap");
      wrap.hidden = false;
      wrap.innerHTML = "";
      const f = document.createElement("iframe");
      f.style.cssText = "width:100%;height:180px;border:1px solid var(--line);border-radius:8px";
      f.srcdoc = `<!doctype html><html><body style="margin:0"><div id="m" style="height:180px"></div><script>
        const say = (ok, text, code) => parent.postMessage({mgt: "gmaps", ok, text, code}, "*");
        let code = "";
        const err = console.error.bind(console);
        console.error = (...a) => { const m = String(a[0] || "").match(/\\b([A-Z][A-Za-z]+MapError)\\b/); if (m) code = m[1]; err(...a); };
        window.gm_authFailure = () => setTimeout(() => say(false, "Google rejected the key. Check that the Maps JavaScript API is enabled, billing is on, and this site is allowed under the key's website restrictions.", code), 300);
        window.ready = () => {
          const map = new google.maps.Map(document.getElementById("m"), {center: {lat: 24.45, lng: 54.6}, zoom: 10, disableDefaultUI: true});
          google.maps.event.addListenerOnce(map, "tilesloaded", () => setTimeout(() => say(true, "Key works. Google Maps loaded."), 1500));
        };
        const s = document.createElement("script");
        s.src = "https://maps.googleapis.com/maps/api/js?key=" + encodeURIComponent(${JSON.stringify(key)}) + "&callback=ready";
        s.onerror = () => say(false, "Could not load Google Maps from this browser (network or ad blocker).");
        document.head.appendChild(s);
      <\/script></body></html>`;
      wrap.appendChild(f);
      show($("googleResult"), null, "Testing…");
      pending = {win: f.contentWindow, timer: setTimeout(() => { show($("googleResult"), false, "No answer from Google within 20 s."); pending = null; }, 20000)};
    });
  }

  // Anthropic: the server lists the Claude models the key can use (free, sends no conversation data).
  const aBtn = $("testAnthropic");
  if (aBtn) aBtn.addEventListener("click", async () => {
    const body = new URLSearchParams({anthropic_api_key: $("id_anthropic_api_key").value.trim(), anthropic_model: $("id_anthropic_model").value.trim()});
    show($("anthropicResult"), null, "Testing…");
    aBtn.disabled = true;
    try {
      const r = await fetch(aBtn.closest("form").action + "test-anthropic/", {method: "POST", body, credentials: "same-origin",
        headers: {"X-CSRFToken": csrf, "Accept": "application/json"}});
      const d = await r.json();
      show($("anthropicResult"), d.ok, d.detail);
      if (d.models) $("anthropicModels").innerHTML = d.models.map(m => `<option value="${Fleet.esc(m)}">`).join("");
    } catch (e) {
      show($("anthropicResult"), false, "Test failed: " + e.message);
    } finally { aBtn.disabled = false; }
  });

  // OpenAI: the server calls GET /v1/models with the key (free, sends no data) and lists the models.
  const oBtn = $("testOpenai");
  if (oBtn) oBtn.addEventListener("click", async () => {
    const body = new URLSearchParams({openai_api_key: $("id_openai_api_key").value.trim(), openai_model: $("id_openai_model").value.trim()});
    show($("openaiResult"), null, "Testing…");
    oBtn.disabled = true;
    try {
      const r = await fetch(oBtn.closest("form").action + "test-openai/", {method: "POST", body, credentials: "same-origin",
        headers: {"X-CSRFToken": csrf, "Accept": "application/json"}});
      const d = await r.json();
      show($("openaiResult"), d.ok, d.detail);
      if (d.models) $("openaiModels").innerHTML = d.models.map(m => `<option value="${Fleet.esc(m)}">`).join("");
    } catch (e) {
      show($("openaiResult"), false, "Test failed: " + e.message);
    } finally { oBtn.disabled = false; }
  });

  // WhatsApp: show only the chosen provider's fields; the test sends a real message to the saved numbers.
  const prov = $("wa_provider");
  if (prov) {
    const sync = () => document.querySelectorAll(".wa-box").forEach(b => { b.hidden = prov.value ? b.dataset.provider !== prov.value : false; });
    prov.addEventListener("change", sync); sync();
    const wBtn = $("testWhatsapp");
    wBtn.addEventListener("click", async () => {
      show($("whatsappResult"), null, "Sending…");
      wBtn.disabled = true;
      try {
        const r = await fetch($("waForm").action + "test/", {method: "POST", credentials: "same-origin",
          headers: {"X-CSRFToken": csrf, "Accept": "application/json"}});
        const d = await r.json();
        show($("whatsappResult"), d.ok, d.ok ? `Sent. Check WhatsApp. ${d.detail}` : d.detail);
      } catch (e) {
        show($("whatsappResult"), false, "Test failed: " + e.message);
      } finally { wBtn.disabled = false; }
    });
  }
})();
