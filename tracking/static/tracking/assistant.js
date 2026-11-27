/* The on-screen assistant: an animated robot that opens a chat panel on every page.
   Page tips and the health check work without any AI key; free-form questions go to the server,
   which answers with Claude / OpenAI when configured (Settings → AI assistant) or a built-in guide. */
(function () {
  const root = document.getElementById("mgtBot");
  if (!root) return;
  const D = root.dataset;
  const F = window.Fleet || {esc: s => String(s ?? "")};
  const esc = s => F.esc(s);
  const store = {
    get(k, d) { try { const v = sessionStorage.getItem("mgt-bot-" + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { sessionStorage.setItem("mgt-bot-" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };

  const ROBOT = `
  <svg class="bot-svg" viewBox="0 0 120 142" aria-hidden="true">
    <defs>
      <linearGradient id="botShell" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4cc9ff"/><stop offset="1" stop-color="#2563eb"/></linearGradient>
      <linearGradient id="botBody" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#1d4ed8"/></linearGradient>
      <radialGradient id="botGlow"><stop offset="0" stop-color="#ccfbf1"/><stop offset="1" stop-color="#2dd4bf"/></radialGradient>
    </defs>
    <ellipse class="bot-shadow" cx="60" cy="136" rx="24" ry="4"/>
    <g class="bot-float">
      <line x1="60" y1="24" x2="60" y2="11" stroke="#1e3a8a" stroke-width="3" stroke-linecap="round"/>
      <circle class="bot-bulb" cx="60" cy="8" r="6"/>
      <g class="bot-arm-l"><rect x="18" y="90" width="12" height="26" rx="6" fill="#1e40af"/><circle cx="24" cy="117" r="6" fill="#93c5fd"/></g>
      <g class="bot-arm-r"><rect x="90" y="90" width="12" height="26" rx="6" fill="#1e40af"/><circle cx="96" cy="117" r="6" fill="#93c5fd"/></g>
      <rect x="32" y="84" width="56" height="40" rx="16" fill="url(#botBody)"/>
      <rect x="44" y="94" width="32" height="18" rx="8" fill="#0b1a33" opacity=".55"/>
      <circle class="bot-heart" cx="60" cy="103" r="5" fill="url(#botGlow)"/>
      <circle cx="17" cy="53" r="8" fill="#1e40af"/><circle cx="103" cy="53" r="8" fill="#1e40af"/>
      <rect x="20" y="22" width="80" height="62" rx="26" fill="url(#botShell)"/>
      <path d="M32 30 Q60 18 88 30" stroke="#fff" stroke-opacity=".35" stroke-width="4" fill="none" stroke-linecap="round"/>
      <rect x="29" y="35" width="62" height="38" rx="17" fill="#0b1220"/>
      <g class="bot-eyes"><ellipse class="bot-eye" cx="47" cy="52" rx="5.5" ry="7.5"/><ellipse class="bot-eye" cx="73" cy="52" rx="5.5" ry="7.5"/></g>
      <path class="bot-mouth" d="M53 63 Q60 68 67 63" stroke="#5eead4" stroke-width="2.6" fill="none" stroke-linecap="round"/>
      <circle cx="36" cy="64" r="3" fill="#f472b6" opacity=".45"/><circle cx="84" cy="64" r="3" fill="#f472b6" opacity=".45"/>
    </g>
  </svg>`;

  const name = D.name || "Assistant";
  root.className = "bot-root pos-" + (D.position || "bottom-right");
  document.body.classList.add("has-bot", "bot-" + (D.position || "bottom-right"));
  root.innerHTML = `
    <section class="bot-panel" role="dialog" aria-label="${esc(name)} assistant" hidden>
      <header class="bot-head">
        <div class="bot-mini">${ROBOT}</div>
        <div class="flex-grow-1 min-w-0"><div class="fw-semibold">${esc(name)}</div><div class="bot-status small text-truncate">…</div></div>
        <button type="button" class="btn btn-sm bot-hbtn" data-act="check" title="Check for problems"><i class="bi bi-heart-pulse"></i></button>
        <button type="button" class="btn btn-sm bot-hbtn" data-act="clear" title="New conversation"><i class="bi bi-arrow-counterclockwise"></i></button>
        <button type="button" class="btn btn-sm bot-hbtn" data-act="close" title="Close"><i class="bi bi-x-lg"></i></button>
      </header>
      <div class="bot-log" aria-live="polite"></div>
      <div class="bot-chips"></div>
      <form class="bot-form">
        <textarea rows="1" class="form-control form-control-sm" placeholder="Ask ${esc(name)}…" maxlength="2000" aria-label="Your question"></textarea>
        <button class="btn btn-primary btn-sm" aria-label="Send"><i class="bi bi-send"></i></button>
      </form>
    </section>
    <div class="bot-bubble" hidden><span></span><button type="button" class="btn-close btn-close-sm" aria-label="Dismiss"></button></div>
    <button type="button" class="bot-fab" aria-label="Open ${esc(name)}, the assistant" title="${esc(name)} - ask me anything">
      ${ROBOT}<span class="bot-badge" hidden></span>
    </button>
`;

  const $ = s => root.querySelector(s);
  const fab = $(".bot-fab"), panel = $(".bot-panel"), log = $(".bot-log"), chips = $(".bot-chips");
  const input = $("textarea"), form = $(".bot-form"), badge = $(".bot-badge"), bubble = $(".bot-bubble");
  let history = store.get("chat", []);
  let ctx = null, busy = false, seenToasts = [];

  /* ---------- small Markdown renderer (escaped first, so nothing from the server runs as HTML) ---------- */
  function md(text) {
    const inline = s => esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\(((?:\/|https:\/\/)[^\s)]*)\)/g, '<a href="$2">$1</a>');
    const out = [];
    let list = null;
    for (const raw of String(text || "").split("\n")) {
      const line = raw.trimEnd();
      const m = line.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/);
      if (m) {
        const tag = m[1] ? "ol" : "ul";
        if (!list || list.tag !== tag) { if (list) out.push(`</${list.tag}>`); list = {tag}; out.push(`<${tag}>`); }
        out.push(`<li>${inline(m[2])}</li>`);
        continue;
      }
      if (list) { out.push(`</${list.tag}>`); list = null; }
      if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push(`</${list.tag}>`);
    return out.join("");
  }

  function addMsg(role, html, cls = "") {
    const el = document.createElement("div");
    el.className = `bot-msg ${role} ${cls}`;
    el.innerHTML = html;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function mood(state) {
    root.classList.remove("is-thinking", "is-alert", "is-happy");
    if (state) root.classList.add("is-" + state);
  }

  /* ---------- what the page knows that the server can't see ---------- */
  function clientInfo() {
    return {google_error: F._googleError || "", google_failed: !!F._googleFailed,
            js_errors: (window.__mgtErrors || []).slice(0, 3), toasts: seenToasts.slice(-3)};
  }
  function pageText() {
    const parts = [];
    const drawer = document.querySelector(".vp-drawer:not([hidden])");
    if (drawer && drawer.offsetParent !== null) parts.push("Open vehicle panel: " + drawer.innerText);
    document.querySelectorAll("main .alert").forEach(a => parts.push("Alert: " + a.innerText));
    if (seenToasts.length) parts.push("Notices shown: " + seenToasts.join(" | "));
    const main = document.querySelector("main");
    if (main) parts.push(main.innerText);
    return parts.join("\n").replace(/[ \t]+/g, " ").replace(/\n{2,}/g, "\n").slice(0, 1500);
  }

  async function post(url, body) {
    const r = await fetch(url, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": D.csrf, "Accept": "application/json"},
      body: JSON.stringify(body)});
    let data = {};
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok && !data.reply) throw new Error(data.error || (r.status + " " + r.statusText));
    return data;
  }

  /* ---------- health check ---------- */
  const ICON = {error: "bi-x-octagon-fill", warn: "bi-exclamation-triangle-fill", info: "bi-info-circle-fill", ok: "bi-check-circle-fill"};
  function findingsHtml(list) {
    return `<div class="bot-findings">${list.map(f => `
      <div class="bot-finding ${f.level}"><i class="bi ${ICON[f.level] || ICON.info}"></i><div>
        <div class="fw-semibold">${esc(f.title)}</div><div>${md(f.detail)}</div>
        ${f.fix ? `<div class="bot-fix">${md("**What to do:** " + f.fix)}</div>` : ""}
        ${f.link ? `<a class="small" href="${esc(f.link)}">Open the page <i class="bi bi-arrow-right"></i></a>` : ""}
      </div></div>`).join("")}</div>`;
  }
  function problems(list) { return (list || []).filter(f => f.level === "error" || f.level === "warn"); }
  function setBadge(list) {
    const n = problems(list).length;
    badge.hidden = !n;
    badge.textContent = n;
    badge.classList.toggle("err", (list || []).some(f => f.level === "error"));
    mood(n ? "alert" : null);
  }

  async function loadContext(force) {
    const cached = store.get("ctx", null);
    if (!force && cached && cached.page === D.page && Date.now() - cached.at < 120000) { ctx = cached.data; return ctx; }
    ctx = await post(D.context, {page: D.page, client: clientInfo()});
    store.set("ctx", {page: D.page, at: Date.now(), data: ctx});
    return ctx;
  }

  function renderChips() {
    const ask = (ctx && ctx.page && ctx.page.ask) || [];
    chips.innerHTML = [`<button type="button" class="bot-chip check" data-act="check"><i class="bi bi-heart-pulse"></i> Check for problems</button>`,
      `<button type="button" class="bot-chip" data-act="tips"><i class="bi bi-lightbulb"></i> Tips for this page</button>`]
      .concat(ask.map(q => `<button type="button" class="bot-chip" data-q="${esc(q)}">${esc(q)}</button>`)).join("");
  }

  function showTips() {
    const p = ctx.page;
    addMsg("bot", `<p><b>${esc(p.title)}</b>: ${esc(p.about)}</p>` +
      (p.tips.length ? `<ul>${p.tips.map(t => `<li>${md(t).replace(/^<p>|<\/p>$/g, "")}</li>`).join("")}</ul>` : ""));
  }

  async function runCheck() {
    mood("thinking");
    const wait = addMsg("bot", `<span class="bot-dots"><i></i><i></i><i></i></span> Checking the receiver, your vehicles and the last 24 hours…`);
    try {
      await loadContext(true);
      wait.remove();
      const bad = problems(ctx.findings);
      addMsg("bot", `<p>${bad.length ? `I found <b>${bad.length}</b> thing(s) that need attention:` : "Everything I can check looks healthy."}</p>` +
        findingsHtml(ctx.findings));
      setBadge(ctx.findings);
      if (!bad.length) { mood("happy"); setTimeout(() => mood(null), 2500); }
    } catch (e) {
      wait.remove();
      addMsg("bot", `<p>I couldn't run the check: ${esc(e.message)}</p>`, "err");
      mood(null);
    }
  }

  /* ---------- chat ---------- */
  function remember(role, text) {
    history.push({role, text});
    history = history.slice(-30);
    store.set("chat", history);
  }

  async function ask(question) {
    question = (question || "").trim();
    if (!question || busy) return;
    busy = true;
    addMsg("user", md(question));
    const prior = history.slice(-10);
    remember("user", question);
    input.value = "";
    autosize();
    mood("thinking");
    const wait = addMsg("bot", `<span class="bot-dots"><i></i><i></i><i></i></span>`);
    try {
      const d = await post(D.chat, {message: question, history: prior, page: D.page, page_text: pageText(), client: clientInfo()});
      wait.remove();
      addMsg("bot", md(d.reply) + (d.source === "guide" && !d.error ? `<div class="bot-src">From the built-in guide</div>` : ""));
      remember("assistant", d.reply);
    } catch (e) {
      wait.remove();
      addMsg("bot", `<p>Sorry, I couldn't reach the server (${esc(e.message)}). Check your connection and try again.</p>`, "err");
    } finally {
      busy = false;
      mood(ctx ? (problems(ctx.findings).length ? "alert" : null) : null);
    }
  }

  function autosize() { input.style.height = "auto"; input.style.height = Math.min(120, input.scrollHeight) + "px"; }

  async function open() {
    panel.hidden = false;
    root.classList.add("open");
    bubble.hidden = true;
    store.set("open", true);
    if (!log.childElementCount) {
      try {
        await loadContext(false);
      } catch (e) {
        addMsg("bot", `<p>I couldn't load my settings (${esc(e.message)}).</p>`, "err");
        return;
      }
      $(".bot-status").textContent = ctx.ai;
      $(".bot-status").title = ctx.ai;
      if (history.length) {
        for (const h of history) addMsg(h.role === "user" ? "user" : "bot", md(h.text));
      } else {
        addMsg("bot", md(ctx.greeting));
        showTips();
      }
      const bad = problems(ctx.findings);
      if (bad.length && !history.length) addMsg("bot", `<p>Heads-up: I can see <b>${bad.length}</b> problem(s) right now.</p>` + findingsHtml(bad));
      if (!ctx.ai_ready && ctx.can_configure && !history.length)
        addMsg("bot", `<p class="small text-body-secondary">${esc(ctx.ai)}. Connect Claude on <a href="/settings/#assistant">Settings → AI assistant</a> so I can answer any question.</p>`);
      renderChips();
      setBadge(ctx.findings);
    }
    setTimeout(() => input.focus(), 50);
  }
  function close() { panel.hidden = true; root.classList.remove("open"); store.set("open", false); }

  function say(text, ms = 9000) {
    if (!panel.hidden) return;
    bubble.querySelector("span").textContent = text;
    bubble.hidden = false;
    clearTimeout(say.t);
    say.t = setTimeout(() => { bubble.hidden = true; }, ms);
  }

  fab.addEventListener("click", () => (panel.hidden ? open() : close()));
  bubble.querySelector(".btn-close").addEventListener("click", e => { e.stopPropagation(); bubble.hidden = true; });
  bubble.addEventListener("click", open);
  root.addEventListener("click", e => {
    const b = e.target.closest("[data-act],[data-q]");
    if (!b || !root.contains(b)) return;
    if (b.dataset.q) return ask(b.dataset.q);
    const act = b.dataset.act;
    if (act === "close") close();
    else if (act === "check") runCheck();
    else if (act === "tips" && ctx) showTips();
    else if (act === "clear") { history = []; store.set("chat", []); log.innerHTML = ""; open(); }
  });
  form.addEventListener("submit", e => { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); } });
  input.addEventListener("input", autosize);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !panel.hidden) close(); });

  /* Notices raised by the app (e.g. Google rejected the maps key) wake the robot up. */
  if (F.toast) {
    const orig = F.toast.bind(F);
    F.toast = (message, sticky) => {
      seenToasts.push(String(message).slice(0, 300));
      mood("alert");
      badge.hidden = false;
      badge.classList.add("err");
      badge.textContent = "!";
      say("I noticed a problem on this page. Tap me and I'll explain what to do.", 12000);
      return orig(message, sticky);
    };
  }

  /* Proactive health check when the page opens (cached for 2 minutes across pages). */
  if (D.proactive === "1") {
    setTimeout(async () => {
      try {
        await loadContext(false);
        setBadge(ctx.findings);
        const bad = problems(ctx.findings);
        const sig = bad.map(f => f.title).join("|");
        if (bad.length && store.get("told", "") !== sig) {
          store.set("told", sig);
          say(`I found ${bad.length} thing(s) that need attention. Tap me to see them.`);
        }
      } catch (e) { /* the robot just stays quiet */ }
    }, 1500);
  }
  if (store.get("open", false)) open();
})();
