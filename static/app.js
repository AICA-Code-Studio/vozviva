// Vista de audiencia: elegir sala e idioma, leer subtítulos en vivo.
(() => {
  "use strict";
  const $app = document.getElementById("app");
  const $sr = document.getElementById("sr-live");
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento */ } },
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let sessions = [];
  let current = null;       // { sid, ch }
  let source = null;        // EventSource
  let lines = new Map();    // seg -> elemento
  let pickerTimer = null;
  let wakeLock = null;
  let size = Number(store.get("vv-size", 30));

  // ---- tema y tamaño --------------------------------------------------------
  const theme = store.get("vv-theme", "");
  if (theme) document.documentElement.dataset.theme = theme;
  const applySize = () => document.documentElement.style.setProperty("--caption-size", size + "px");
  applySize();

  function toggleTheme() {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const now = document.documentElement.dataset.theme || (dark ? "dark" : "light");
    const next = now === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("vv-theme", next);
  }

  // ---- lectura en voz alta -------------------------------------------------
  // Usa la síntesis de voz del sistema (sin costo ni red). Lee solo las frases
  // finales que llegan en vivo; si la lectura se atrasa, salta a lo último para
  // no quedar desfasada del escenario.
  const RATES = [1, 1.25, 1.5, 1.75];
  const tts = {
    supported: "speechSynthesis" in window && "SpeechSynthesisUtterance" in window,
    on: false,
    pending: 0,
    rate: Number(store.get("vv-rate", "1")) || 1,
    voiceFor(lang) {
      const voices = speechSynthesis.getVoices();
      const l = (lang || "").toLowerCase();
      const prefs = l === "es" ? ["es-ar", "es-419", "es-us", "es-mx", "es"] : [l];
      for (const pre of prefs) {
        const v = voices.find((x) => x.lang.toLowerCase().replace("_", "-").startsWith(pre));
        if (v) return v;
      }
      return null;
    },
    say(text, lang) {
      if (!this.supported || !this.on || !text) return;
      if (this.pending >= 2) { speechSynthesis.cancel(); this.pending = 0; }
      const u = new SpeechSynthesisUtterance(text);
      u.lang = lang || "es";
      const v = this.voiceFor(u.lang);
      if (v) u.voice = v;
      u.rate = this.rate;
      this.pending++;
      u.onend = u.onerror = () => { this.pending = Math.max(0, this.pending - 1); };
      speechSynthesis.speak(u);
    },
    set(on) {
      this.on = on;
      if (!this.supported) return;
      speechSynthesis.cancel();
      this.pending = 0;
      // El aviso sale del toque del usuario: así iOS y Chrome habilitan el audio.
      const u = new SpeechSynthesisUtterance(on ? "Lectura en voz alta activada" : "Lectura en voz alta desactivada");
      u.lang = "es";
      const v = this.voiceFor("es");
      if (v) u.voice = v;
      speechSynthesis.speak(u);
    },
  };
  if (tts.supported) {
    speechSynthesis.getVoices(); // Chrome carga las voces de forma diferida
    speechSynthesis.addEventListener?.("voiceschanged", () => speechSynthesis.getVoices());
  }

  // ---- datos ----------------------------------------------------------------
  async function loadSessions() {
    const r = await fetch("/api/sessions", { cache: "no-store" });
    if (!r.ok) throw new Error("No se pudo cargar la lista de salas");
    sessions = await r.json();
  }

  function langsLabel(s) {
    const src = s.channels.find((c) => c.id === "orig");
    const others = s.channels.filter((c) => c.id !== "orig").map((c) => c.lang.toUpperCase());
    return `${(src?.lang || "").toUpperCase()} → ${others.join(", ")}`;
  }

  // ---- selector de salas ------------------------------------------------------
  function renderPicker(error) {
    closeStream();
    releaseWake();
    document.title = "Subtítulos en vivo";
    const items = sessions.map((s) => `
      <li><button class="room" data-sid="${esc(s.id)}">
        <span class="lamp ${s.live ? "on" : ""}" title="${s.live ? "Con audio" : "Sin audio"}"></span>
        <span class="room-name">${esc(s.name)}</span>
        <span class="room-langs">${esc(langsLabel(s))}</span>
        ${s.description ? `<span class="room-desc">${esc(s.description)}</span>` : ""}
      </button></li>`).join("");
    $app.innerHTML = `
      <header class="bar">
        <a class="brand" href="/">Vozviva<span class="dot">.</span></a>
        <span class="spacer"></span>
        <button class="icon-btn" id="theme" aria-label="Cambiar tema claro u oscuro">◐</button>
      </header>
      <main class="picker">
        <h1>Elegí una sala</h1>
        <p class="hint">Vas a ver lo que se dice en el escenario, en el idioma original o traducido.</p>
        ${error ? `<p class="msg err">${esc(error)}</p>` : ""}
        ${sessions.length ? `<ul class="rooms">${items}</ul>` : `<p class="empty">No hay salas configuradas todavía.</p>`}
      </main>`;
    $app.querySelector("#theme").onclick = toggleTheme;
    $app.querySelectorAll(".room").forEach((b) => (b.onclick = () => open(b.dataset.sid)));
    clearInterval(pickerTimer);
    pickerTimer = setInterval(async () => {
      if (current) return;
      try { await loadSessions(); renderPicker(); } catch { /* reintenta en el próximo ciclo */ }
    }, 15000);
  }

  // ---- vista de subtítulos ------------------------------------------------------
  function defaultChannel(s) {
    const saved = store.get("vv-lang", "");
    const byLang = s.channels.find((c) => c.lang === saved || c.id === saved);
    if (byLang) return byLang.id;
    const es = s.channels.find((c) => c.lang === "es");
    return (es || s.channels[0]).id;
  }

  function open(sid, ch) {
    const s = sessions.find((x) => x.id === sid);
    if (!s) return renderPicker("Esa sala no existe. Elegí otra de la lista.");
    ch = s.channels.some((c) => c.id === ch) ? ch : defaultChannel(s);
    current = { sid, ch };
    const url = new URL(location.href);
    url.searchParams.set("s", sid);
    url.searchParams.set("l", ch);
    history.replaceState(null, "", url);
    document.title = `${s.name} · subtítulos`;
    renderViewer(s);
    connect();
    requestWake();
    clearInterval(viewerTimer);
    viewerTimer = setInterval(async () => {
      if (!current) return clearInterval(viewerTimer);
      try {
        await loadSessions();
        const fresh = sessions.find((x) => x.id === current.sid);
        if (fresh) renderNow(fresh);
      } catch { /* reintenta */ }
    }, 60000);
  }

  function renderViewer(s) {
    const { sid, ch } = current;
    // Con más de 3 idiomas, las pestañas usan códigos (ES, PT, EN) para entrar en el celular;
    // el nombre completo queda para lectores de pantalla y como título.
    const compact = s.channels.length > 3;
    const tabLabel = (c) => !compact ? (c.short || c.label)
      : c.id === "orig" ? "Original" : c.id === "facil" ? "Fácil" : c.lang.toUpperCase();
    const tabs = s.channels.map((c) =>
      `<button data-ch="${esc(c.id)}" aria-pressed="${c.id === ch}" aria-label="${esc(c.label)}" title="${esc(c.label)}">${esc(tabLabel(c))}</button>`).join("");
    $app.innerHTML = `
      <div class="viewer">
        <header class="viewer-head">
          <div class="viewer-title">
            <button class="icon-btn" id="back" aria-label="Volver a la lista de salas">←</button>
            <span class="lamp" id="lamp"></span>
            <h1>${esc(s.name)}</h1>
            <span class="conn" id="conn">conectando…</span>
            <button class="icon-btn" id="theme" aria-label="Cambiar tema claro u oscuro">◐</button>
          </div>
          <div class="seg-ctl" role="group" aria-label="Idioma de los subtítulos">${tabs}</div>
          <div class="now" id="now"></div>
        </header>
        <main class="captions" id="captions" tabindex="0" aria-label="Subtítulos">
          <div class="fill"></div>
          <p class="waiting" id="waiting">Cuando alguien hable en el escenario, el texto aparece acá.</p>
        </main>
        <footer class="viewer-foot">
          ${tts.supported ? `<button class="icon-btn" id="listen" aria-pressed="${tts.on}">Escuchar</button>
          <button class="icon-btn" id="rate" aria-label="Velocidad de lectura" ${tts.on ? "" : "hidden"}>${tts.rate}×</button>` : ""}
          <button class="icon-btn" id="smaller" aria-label="Achicar letra">A−</button>
          <button class="icon-btn" id="bigger" aria-label="Agrandar letra">A+</button>
          <span class="spacer"></span>
          <details class="dl">
            <summary class="icon-btn" role="button" aria-label="Descargar transcripción" title="Descargar transcripción">⤓</summary>
            <div class="menu">
              <a href="/api/sessions/${encodeURIComponent(sid)}/transcript?channel=${encodeURIComponent(ch)}&format=txt">Texto (.txt)</a>
              <a href="/api/sessions/${encodeURIComponent(sid)}/transcript?channel=${encodeURIComponent(ch)}&format=srt">Subtítulos (.srt)</a>
              <a href="/api/sessions/${encodeURIComponent(sid)}/transcript?channel=${encodeURIComponent(ch)}&format=vtt">Subtítulos (.vtt)</a>
            </div>
          </details>
        </footer>
        <button class="to-live" id="toLive">Ir a lo último</button>
        <dialog class="recap" id="recapDlg" aria-labelledby="recapTitle">
          <div class="recap-head">
            <h2 id="recapTitle">Lo último de la charla</h2>
            <button class="icon-btn" id="recapClose" aria-label="Cerrar resumen">✕</button>
          </div>
          <div class="seg-ctl" role="group" aria-label="Tramo a resumir">
            ${[5, 10, 15, 30].map((m) => `<button data-min="${m}" aria-pressed="${m === recapMin}">${m} min</button>`).join("")}
          </div>
          <div id="recapBody" class="recap-body" aria-live="polite"></div>
          <p class="recap-note" id="recapNote"></p>
        </dialog>
        <dialog class="recap" id="explainDlg" aria-labelledby="explainTitle">
          <div class="recap-head">
            <h2 id="explainTitle">Qué quiere decir</h2>
            <button class="icon-btn" id="explainClose" aria-label="Cerrar explicación">✕</button>
          </div>
          <blockquote class="explain-quote" id="explainQuote"></blockquote>
          <div id="explainBody" class="recap-body" aria-live="polite"></div>
          <p class="recap-note" id="explainNote"></p>
        </dialog>
        <div class="hint" id="hint" role="status" hidden>Tocá cualquier frase y te la explicamos.</div>
      </div>`;
    renderNow(s);
    const exp = $app.querySelector("#explainDlg");
    $app.querySelector("#explainClose").onclick = () => exp.close();
    exp.addEventListener("click", (e) => { if (e.target === exp) exp.close(); });
    const capBox = $app.querySelector("#captions");
    const pick = (e) => {
      const line = e.target.closest?.(".line");
      if (!line || line.dataset.final !== "1" || window.getSelection()?.toString()) return;
      exp.showModal();
      loadExplain(s, Number(line.dataset.seg), line.textContent);
    };
    capBox.addEventListener("click", pick);
    capBox.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e); } });
    const dlg = $app.querySelector("#recapDlg");
    $app.querySelector("#recapClose").onclick = () => dlg.close();
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    dlg.querySelectorAll("[data-min]").forEach((b) => (b.onclick = () => {
      recapMin = Number(b.dataset.min);
      dlg.querySelectorAll("[data-min]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      loadRecap(s);
    }));
    $app.querySelector("#back").onclick = () => {
      current = null;
      history.replaceState(null, "", location.pathname);
      loadSessions().then(() => renderPicker(), () => renderPicker());
    };
    $app.querySelectorAll(".seg-ctl button").forEach((b) => (b.onclick = () => {
      store.set("vv-lang", s.channels.find((c) => c.id === b.dataset.ch)?.lang || b.dataset.ch);
      open(sid, b.dataset.ch);
    }));
    $app.querySelector("#smaller").onclick = () => { size = Math.max(18, size - 4); store.set("vv-size", size); applySize(); };
    $app.querySelector("#bigger").onclick = () => { size = Math.min(64, size + 4); store.set("vv-size", size); applySize(); };
    $app.querySelector("#theme").onclick = toggleTheme;
    const listen = $app.querySelector("#listen");
    const rate = $app.querySelector("#rate");
    if (listen) {
      listen.onclick = () => {
        tts.set(!tts.on);
        listen.setAttribute("aria-pressed", String(tts.on));
        rate.hidden = !tts.on;
      };
      rate.onclick = () => {
        tts.rate = RATES[(RATES.indexOf(tts.rate) + 1) % RATES.length] || 1;
        store.set("vv-rate", tts.rate);
        rate.textContent = `${tts.rate}×`;
      };
    }
    const box = $app.querySelector("#captions");
    const toLive = $app.querySelector("#toLive");
    box.addEventListener("scroll", () => toLive.classList.toggle("show", !nearBottom(box)));
    toLive.onclick = () => { box.scrollTop = box.scrollHeight; toLive.classList.remove("show"); };
  }

  // ---- charla en curso y "¿Qué me perdí?" ------------------------------------
  let recapMin = 10;
  let viewerTimer = null;
  const hhmm = (iso) => new Date(iso).toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });

  function renderNow(s) {
    const box = document.getElementById("now");
    if (!box) return;
    let title = "", meta = "";
    if (s.now) {
      title = s.now.title;
      meta = [s.now.speakers.join(", "), `hasta las ${hhmm(s.now.end)}`].filter(Boolean).join(", ");
    } else {
      title = s.description || "";
      if (s.next) meta = `Próxima: ${s.next.title}, a las ${hhmm(s.next.start)}`;
    }
    box.innerHTML = `
      <div class="now-text">
        ${title ? `<span class="now-title">${esc(title)}</span>` : ""}
        ${meta ? `<span class="now-meta">${esc(meta)}</span>` : ""}
      </div>
      <button class="recap-btn" id="recap">¿Qué me perdí?</button>`;
    box.querySelector("#recap").onclick = () => {
      document.getElementById("recapDlg").showModal();
      loadRecap(s);
    };
  }

  async function loadRecap(s) {
    const { sid, ch } = current;
    const body = document.getElementById("recapBody");
    const note = document.getElementById("recapNote");
    body.innerHTML = `<p class="recap-wait">Resumiendo los últimos ${recapMin} minutos…</p>`;
    note.textContent = "";
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/summary?channel=${encodeURIComponent(ch)}&minutes=${recapMin}`, { cache: "no-store" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || "No se pudo generar el resumen.");
      if (current?.sid !== sid) return;
      if (!data.bullets.length) {
        body.innerHTML = `<p>Todavía no hay suficiente texto en los últimos ${recapMin} minutos.</p>`;
        return;
      }
      body.innerHTML = `<ul>${data.bullets.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`;
      note.textContent = data.mode === "extractivo"
        ? "Sin resumen automático disponible: estas son frases textuales del tramo."
        : "Resumen automático a partir de la transcripción: puede tener errores.";
      const lang = s.channels.find((c) => c.id === ch)?.lang;
      if (tts.on) tts.say(data.bullets.join(" "), lang === "auto" ? "es" : lang);
    } catch (e) {
      body.innerHTML = `<p class="msg err">${esc(e.message)} Probá de nuevo en unos segundos.</p>`;
    }
  }

  // ---- "Tocá una frase y te la explico" -------------------------------------------
  async function loadExplain(s, seg, text) {
    const { sid, ch } = current;
    const body = document.getElementById("explainBody");
    const note = document.getElementById("explainNote");
    document.getElementById("explainQuote").textContent = text;
    body.innerHTML = `<p class="recap-wait">Buscando la explicación…</p>`;
    note.textContent = "";
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/explain?channel=${encodeURIComponent(ch)}&seg=${seg}`);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || "No se pudo generar la explicación.");
      const terms = data.terms || [];
      if (!data.explanation && !terms.length) {
        body.innerHTML = `<p>No encontramos términos para explicar en esta frase.</p>`;
      } else {
        body.innerHTML = (data.explanation ? `<p>${esc(data.explanation)}</p>` : "") +
          (terms.length ? `<dl class="terms">${terms.map((t) => `<dt>${esc(t.term)}</dt><dd>${esc(t.meaning)}</dd>`).join("")}</dl>` : "");
      }
      note.textContent = data.mode === "demo"
        ? "Modo demo: definiciones de ejemplo, sin inteligencia artificial."
        : "Explicación automática: puede tener errores.";
      if (tts.on) {
        const lang = s.channels.find((c) => c.id === ch)?.lang;
        tts.say([data.explanation, ...terms.map((t) => `${t.term}: ${t.meaning}`)].filter(Boolean).join(". "), lang === "auto" ? "es" : lang);
      }
    } catch (e) {
      body.innerHTML = `<p class="msg err">${esc(e.message)} Probá de nuevo en unos segundos.</p>`;
    }
  }

  function showHintOnce() {
    if (store.get("vv-hint-explain", "") === "1") return;
    const h = document.getElementById("hint");
    if (!h) return;
    store.set("vv-hint-explain", "1");
    h.hidden = false;
    setTimeout(() => { h.hidden = true; }, 7000);
  }

  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }

  function setConn(text, live) {
    const c = document.getElementById("conn");
    const l = document.getElementById("lamp");
    if (c) c.textContent = text;
    if (l) l.classList.toggle("on", !!live);
  }

  // ---- stream ---------------------------------------------------------------
  function closeStream() {
    if (source) { source.close(); source = null; }
    lines = new Map();
  }

  function connect() {
    closeStream();
    const { sid, ch } = current;
    const es = new EventSource(`/api/sessions/${encodeURIComponent(sid)}/stream?channel=${encodeURIComponent(ch)}`);
    source = es;
    es.addEventListener("snapshot", (e) => {
      const items = JSON.parse(e.data);
      batch(() => items.forEach((c) => upsert(c, false)));
      setConn("en vivo", true);
    });
    es.onmessage = (e) => batch(() => upsert(JSON.parse(e.data), true));
    es.onopen = () => setConn("en vivo", true);
    es.onerror = () => setConn("reconectando…", false);
  }

  function batch(fn) {
    const box = document.getElementById("captions");
    if (!box) return;
    const stick = nearBottom(box);
    fn();
    markRecent(box);
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function upsert(cap, announce) {
    const box = document.getElementById("captions");
    if (!box || !current || cap.session !== current.sid || cap.channel !== current.ch) return;
    document.getElementById("waiting")?.remove();
    let el = lines.get(cap.seg);
    if (el && el.dataset.final === "1" && !cap.final) return; // parcial atrasado
    if (!el) {
      el = document.createElement("p");
      el.className = "line";
      el.dataset.seg = cap.seg;
      // Normalmente llega al final; si no, lo insertamos en orden.
      let before = null;
      for (let n = box.lastElementChild; n && n.dataset.seg; n = n.previousElementSibling) {
        if (Number(n.dataset.seg) > cap.seg) before = n; else break;
      }
      box.insertBefore(el, before);
      lines.set(cap.seg, el);
      while (lines.size > 300) {
        const [k, old] = lines.entries().next().value;
        old.remove(); lines.delete(k);
      }
    }
    el.textContent = cap.text;
    el.dataset.final = cap.final ? "1" : "0";
    if (cap.final) { el.tabIndex = 0; el.title = "Tocá para que te la expliquemos"; showHintOnce(); }
    el.classList.toggle("partial", !cap.final);
    if (cap.final && announce) {
      if (tts.on) tts.say(cap.text, cap.lang);
      else $sr.textContent = cap.text;
    }
  }

  function markRecent(box) {
    const all = box.querySelectorAll(".line");
    all.forEach((n, i) => n.classList.toggle("recent", i >= all.length - 2));
  }

  // ---- pantalla encendida ------------------------------------------------------
  async function requestWake() {
    try { if ("wakeLock" in navigator && !wakeLock) wakeLock = await navigator.wakeLock.request("screen"); } catch { /* opcional */ }
  }
  function releaseWake() { try { wakeLock?.release(); } catch { /* nada */ } wakeLock = null; }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && current) { wakeLock = null; requestWake(); }
  });

  // ---- inicio ----------------------------------------------------------------
  (async () => {
    try {
      await loadSessions();
    } catch (e) {
      return renderPicker(e.message);
    }
    const p = new URLSearchParams(location.search);
    if (p.get("s")) open(p.get("s"), p.get("l"));
    else renderPicker();
  })();
})();
