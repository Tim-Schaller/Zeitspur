/* Zeitspur: Zeitstrahl-Oberflaeche - reines Vanilla-JS, kommuniziert ausschliesslich ueber window.pywebview.api.
   Sicherheitsregel: Alle Inhalte aus der Datenbank (Fenstertitel, OCR-Text) werden nur als textContent
   eingefuegt, nie als innerHTML. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MIN = 60000, HOUR = 3600000;
  const LANE_LABEL_W = 96;

  const state = {
    api: null,
    info: null,           // letzter get_state()
    date: null,           // 'YYYY-MM-DD'
    day: null,            // get_day()-Ergebnis
    view: null,           // {start, end} in ms
    selected: null,       // {block, lane, index, entryId}
    query: "",
    visible: true,
    pollTimer: null,
    dayTimer: null,
    searchTimer: null,
    mode: "timeline",     // 'timeline' | 'setup' | 'settings'
    map: null,            // Leaflet-Karte, erst bei Bedarf erzeugt
    mapLayer: null,
    dragging: null,
  };

  // ------------------------------------------------------------------ Hilfen
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") throw new Error("innerHTML ist nicht erlaubt");
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
      else node.setAttribute(k, v);
    }
    for (const c of children) {
      if (c === null || c === undefined) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const pad2 = (n) => String(n).padStart(2, "0");
  const fmtTime = (ms) => { const d = new Date(ms); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
  const fmtTimeS = (ms) => { const d = new Date(ms); return `${fmtTime(ms)}:${pad2(d.getSeconds())}`; };
  const fmtDate = (iso) => { const [y, m, d] = iso.split("-"); return `${d}.${m}.${y}`; };
  const isoDate = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  const todayIso = () => isoDate(new Date());
  function addDays(iso, n) { const [y, m, d] = iso.split("-").map(Number); const dt = new Date(y, m - 1, d + n); return isoDate(dt); }
  function fmtDuration(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  }
  function weekday(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"][new Date(y, m - 1, d).getDay()];
  }
  function showBanner(text, isError = false) {
    const b = $("banner");
    if (!text) { b.hidden = true; return; }
    b.textContent = text; b.className = "banner" + (isError ? " error" : ""); b.hidden = false;
  }
  function errMessage(err) {
    if (!err) return "Unbekannter Fehler";
    if (typeof err === "string") return err;
    return err.message || err.name || String(err);
  }

  // ------------------------------------------------------------------ Start
  async function init() {
    state.api = window.pywebview.api;
    bindEvents();
    try {
      const info = await state.api.get_state();
      applyState(info);
      if (info.first_run) { await showSetup("setup"); return; }
      state.date = info.today;
      $("dayPicker").value = state.date;
      await loadDay();
      startPolling();
    } catch (e) {
      showBanner("Start fehlgeschlagen: " + errMessage(e), true);
    }
  }
  if (window.pywebview && window.pywebview.api) init();
  else window.addEventListener("pywebviewready", init);

  // ------------------------------------------------------------------ Zustand / Polling
  function applyState(info) {
    state.info = info;
    if (typeof info.window_visible === "boolean") state.visible = info.window_visible;  // kein Polling bei verstecktem Fenster
    const pill = $("statePill");
    pill.textContent = info.capture_label + (info.capture_detail && info.capture_state === "excluded" ? `: ${info.capture_detail}` : "");
    pill.className = "pill " + info.capture_state;
    const pb = $("pauseBtn");
    pb.textContent = info.paused ? "Fortsetzen" : "Pause";
    pb.className = "btn" + (info.paused ? " active" : "");
    pb.disabled = !info.ready;
    updateMapButton();   // haengt allein an map_enabled - nicht daran, ob der Tag Ereignisse hat
    renderUpdate(info.update);
    if (!info.first_run) {
      if (!info.tesseract_available) showBanner("Texterkennung nicht verfügbar: tesseract.exe wurde nicht gefunden. Screenshots werden gespeichert, aber die Volltextsuche bleibt leer.", true);
      else if (info.capture_state === "no_disk") showBanner("Zu wenig freier Speicherplatz – die Aufnahme ist pausiert.", true);
      else if (info.capture_state === "error") showBanner("Aufnahmefehler: " + (info.capture_detail || "siehe Protokoll"), true);
      else if (info.capture_state === "paused") showBanner("Die Aufnahme ist pausiert. Über „Fortsetzen“ oder das Tray-Menü geht es weiter.");
      else showBanner("");
    }
  }
  function renderUpdate(u) {
    // Nur zeigen, was der Nutzer wissen muss: angeboten, laedt, bereit, installiert, gescheitert.
    // Eine Pruefung ohne Netz (Fehler ohne angebotene Version) bleibt still - die naechste klappt meist.
    const bar = $("updateBar");
    const active = u && (["available", "downloading", "ready", "installing"].includes(u.status) || (u.status === "error" && u.version));
    bar.hidden = !active;
    if (!active) return;
    const v = u.version;
    let text, button = "";
    if (u.status === "available") {
      text = u.auto ? `Zeitspur ${v} ist verfügbar und wird im Hintergrund geladen.` : `Zeitspur ${v} ist verfügbar.`;
      if (u.error) text += ` Laden fehlgeschlagen: ${u.error}`;
      button = "Jetzt aktualisieren";
    } else if (u.status === "downloading") {
      text = `Zeitspur ${v} wird geladen … ${Math.round((u.progress || 0) * 100)} %`;
    } else if (u.status === "ready") {
      text = u.auto
        ? `Zeitspur ${v} ist geladen und geprüft. Es wird installiert, sobald der PC ${fmtIdle(u.idle_s)} nicht benutzt wird.`
        : `Zeitspur ${v} ist geladen und geprüft.`;
      button = "Jetzt installieren";
    } else if (u.status === "installing") {
      text = `Zeitspur ${v} wird installiert – das Fenster schließt sich, Zeitspur startet gleich wieder.`;
    } else {
      text = `Das Update auf ${v} ist fehlgeschlagen: ${u.error || "siehe Protokoll"}`;
      button = "Erneut versuchen";
    }
    $("updateText").textContent = text;
    $("updateBtn").hidden = !button;
    $("updateBtn").textContent = button;
    $("updateNotesBtn").hidden = !u.notes;
    $("updateNotes").textContent = u.notes || "";
    if (!u.notes) $("updateNotes").hidden = true;
  }
  function fmtIdle(seconds) {
    const s = Number(seconds) || 300;
    if (s < 60) return `${Math.round(s)} Sekunden`;
    const m = Math.round(s / 60);
    return m === 1 ? "1 Minute" : `${m} Minuten`;
  }
  function versionText(info) {
    const v = `Zeitspur ${info.version || ""}`;
    const u = info.update;
    if (!(info.features && info.features.updates)) return `${v} – eigener Build, aktualisiert sich nicht selbst`;
    if (!u) return `${v} – sucht nach der Einrichtung regelmäßig nach Updates`;   // Update-Dienst startet mit der Aufnahme
    switch (u.status) {
      case "checking": return `${v} – suche nach Updates …`;
      case "current": return `${v} – auf dem neuesten Stand (geprüft um ${fmtTime(u.checked_ms)})`;
      case "available": case "downloading": case "ready": return `${v} – Update auf ${u.version} verfügbar`;
      case "installing": return `${v} – Update auf ${u.version} wird installiert`;
      case "error": return u.version ? `${v} – Update auf ${u.version} fehlgeschlagen`
                                     : `${v} – letzte Update-Prüfung fehlgeschlagen (${u.error || "keine Verbindung"})`;
      default: return `${v} – sucht wenige Minuten nach dem Start nach Updates`;
    }
  }
  function renderVersionLine() {
    const info = state.info || {};
    $("versionText").textContent = versionText(info);
    $("checkUpdatesBtn").hidden = !info.update;
  }
  async function checkUpdates() {
    const btn = $("checkUpdatesBtn");
    const before = (state.info && state.info.update && state.info.update.checked_ms) || 0;
    btn.disabled = true;
    $("versionText").textContent = versionText({ ...state.info, update: { ...(state.info.update || {}), status: "checking" } });
    try {
      await state.api.check_updates();
      // Die Pruefung laeuft im Update-Thread - nachfragen, bis ein neues Ergebnis da ist (hoechstens ~30 s)
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 750));
        applyState(await state.api.get_state());
        const u = state.info.update;
        if (u && u.checked_ms > before && u.status !== "checking") break;
      }
    } catch (e) {
      showBanner("Update-Prüfung: " + errMessage(e), true);
    } finally {
      renderVersionLine();
      btn.disabled = false;
    }
  }
  async function startUpdate() {
    $("updateBtn").disabled = true;
    try {
      renderUpdate(await state.api.update_now());
      setTimeout(() => refresh(), 1500);   // der Update-Thread uebernimmt gleich - Fortschritt nachziehen
    } catch (e) {
      showBanner("Update: " + errMessage(e), true);
    } finally {
      $("updateBtn").disabled = false;
    }
  }
  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollState, 5000);
    state.dayTimer = setInterval(refreshTodayIfChanged, 30000);
  }
  function stopPolling() {
    clearInterval(state.pollTimer); clearInterval(state.dayTimer);
    state.pollTimer = state.dayTimer = null;
  }
  async function pollState() {
    if (!state.visible || state.mode !== "timeline") return;
    try { applyState(await state.api.get_state()); updateNowMarker(); } catch (e) { /* Fenster evtl. im Abbau */ }
  }
  async function refreshTodayIfChanged() {
    if (!state.visible || state.mode !== "timeline" || state.date !== todayIso() || !state.day) return;
    try {
      const day = await state.api.get_day(state.date);
      const evFp = (d) => `${(d.events || []).length}:${Math.max(0, ...(d.events || []).map((e) => e.ts_end || 0))}`;
      if (day.total_entries !== state.day.total_entries || day.last_ms !== state.day.last_ms || evFp(day) !== evFp(state.day)) {
        state.day = day;
        if (state.view && day.last_ms && day.last_ms > state.view.end && state.view.end - state.view.start < 6 * HOUR) {
          const span = state.view.end - state.view.start;
          state.view = { start: day.last_ms + 5 * MIN - span, end: day.last_ms + 5 * MIN };
        }
        render();
      }
    } catch (e) { /* ignorieren */ }
  }
  async function refresh() {
    try { applyState(await state.api.get_state()); } catch (e) { return; }
    if (state.mode === "timeline" && state.info && state.info.ready) {
      if (!state.date) { state.date = state.info.today; $("dayPicker").value = state.date; }
      await loadDay(true);
      if (!state.pollTimer) startPolling();
    }
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
  window.addEventListener("focus", () => { if (state.mode === "timeline") refresh(); });

  window.zeitspur = {
    onShown() { state.visible = true; refresh(); },
    onHidden() { state.visible = false; },
    refresh,
  };

  // ------------------------------------------------------------------ Tag laden
  async function loadDay(keepView = false) {
    if (!state.info || !state.info.ready) return;
    try {
      const day = await state.api.get_day(state.date);
      state.day = day;
      $("dayPicker").value = state.date;
      if (!keepView || !state.view) {
        if (day.first_ms) {
          state.view = { start: Math.max(day.start_ms, day.first_ms - 15 * MIN), end: Math.min(day.end_ms, day.last_ms + 15 * MIN) };
          if (state.view.end - state.view.start < 30 * MIN) state.view.end = state.view.start + 30 * MIN;
        } else {
          state.view = { start: day.start_ms + 6 * HOUR, end: day.start_ms + 20 * HOUR };
        }
      }
      if (!keepView) { state.selected = null; $("detail").hidden = true; }
      render();
    } catch (e) {
      showBanner("Tag konnte nicht geladen werden: " + errMessage(e), true);
    }
  }
  function setDate(iso, keepView = false) {
    state.date = iso;
    state.selected = null;
    $("detail").hidden = true;
    return loadDay(keepView);
  }

  // ------------------------------------------------------------------ Rendering
  function trackWidth() { return Math.max(50, $("timeline").clientWidth - LANE_LABEL_W); }
  function xAtTime(t) { const v = state.view; return (t - v.start) / (v.end - v.start) * trackWidth(); }
  function timeAtX(x) { const v = state.view; return v.start + x / trackWidth() * (v.end - v.start); }

  function render() {
    if (!state.day || !state.view) return;
    renderMeta();
    renderAxis();
    renderLanes();
    updateNowMarker();
    $("emptyHint").hidden = state.day.total_entries > 0;
  }
  function renderMeta() {
    const d = state.day;
    const meta = $("dayMeta");
    const parts = [
      el("span", {}, el("b", { text: `${weekday(d.date)}, ${fmtDate(d.date)}` })),
      el("span", {}, "Einträge: ", el("b", { text: String(d.total_entries) })),
      el("span", {}, "Aktive Zeit: ", el("b", { text: fmtDuration(d.active_ms) })),
      d.first_ms ? el("span", {}, "Zeitraum: ", el("b", { text: `${fmtTime(d.first_ms)} – ${fmtTime(d.last_ms)}` })) : null,
      el("span", {}, "Ansicht: ", el("b", { text: `${fmtTime(state.view.start)} – ${fmtTime(state.view.end)}` })),
    ];
    meta.replaceChildren(...parts.filter(Boolean));  // replaceChildren(null) wuerde den Text "null" einfuegen
  }
  function tickStep(spanMs) {
    if (spanMs > 12 * HOUR) return HOUR;
    if (spanMs > 6 * HOUR) return 30 * MIN;
    if (spanMs > 3 * HOUR) return 15 * MIN;
    if (spanMs > 90 * MIN) return 10 * MIN;
    if (spanMs > 40 * MIN) return 5 * MIN;
    if (spanMs > 12 * MIN) return 2 * MIN;
    return MIN;
  }
  function renderAxis() {
    const axis = $("axis");
    axis.replaceChildren();
    const v = state.view, step = tickStep(v.end - v.start);
    const first = Math.ceil(v.start / step) * step;
    const width = trackWidth();
    for (let t = first; t <= v.end; t += step) {
      const x = xAtTime(t);
      if (x < 0 || x > width) continue;
      const major = (t % HOUR) === 0;
      axis.append(el("div", { class: "tick" + (major ? " major" : ""), style: { left: `${x}px` }, text: fmtTime(t) }));
    }
  }
  function renderLanes() {
    const lanes = $("lanes");
    lanes.replaceChildren();
    const v = state.view, step = tickStep(v.end - v.start), width = trackWidth();
    renderEventsLane(lanes, v, step, width);
    const multi = state.day.lanes.length > 1;
    const laneData = state.day.lanes.length ? state.day.lanes : [{ monitor_id: 1, blocks: [] }];
    laneData.forEach((lane, laneIdx) => {
      const row = el("div", { class: "lane" });
      row.append(el("div", { class: "lane-label", text: multi ? `Monitor ${lane.monitor_id}` : "Bildschirm" }));
      const track = el("div", { class: "lane-track" });
      for (let t = Math.ceil(v.start / step) * step; t <= v.end; t += step) {
        track.append(el("div", { class: "gridline", style: { left: `${xAtTime(t)}px` } }));
      }
      for (const block of lane.blocks) {
        if (block.ts_end < v.start || block.ts_start > v.end) continue;
        const x1 = Math.max(0, xAtTime(block.ts_start));
        const x2 = Math.min(width, xAtTime(Math.max(block.ts_end, block.ts_start + 1000)));
        const w = Math.max(3, x2 - x1);
        const selected = state.selected && state.selected.block === block;
        const node = el("div", {
          class: "block" + (selected ? " selected" : "") + (block.ocr_pending ? " pending" : ""),
          style: { left: `${x1}px`, width: `${w}px`, background: block.color, color: block.text_color },
          title: `${block.window_title || "(ohne Titel)"}\n${appLabel(block.process_name)}\n${fmtTime(block.ts_start)} – ${fmtTime(block.ts_end)} · ${block.count} Bild(er)`,
        });
        if (w > 48) {
          // Der Fenstertitel sagt, woran gearbeitet wurde - der Programmname steht klein dahinter.
          node.append(el("div", { class: "b-app", text: block.window_title || appLabel(block.process_name) }));
          if (w > 90 && block.window_title) node.append(el("div", { class: "b-title", text: appLabel(block.process_name) }));
        }
        node.addEventListener("click", (e) => {
          e.stopPropagation();
          if (state.suppressClick) { state.suppressClick = false; return; }  // war ein Ziehen, kein Klick
          const rect = track.getBoundingClientRect();
          selectBlock(block, laneIdx, timeAtX(e.clientX - rect.left));
        });
        track.append(node);
      }
      row.append(track);
      lanes.append(row);
    });
  }
  function appLabel(name) {
    if (!name) return "Unbekannt";
    return name.replace(/\.exe$/i, "");
  }
  function renderEventsLane(lanes, v, step, width) {
    const all = (state.day && state.day.events) || [];
    updateMapButton();
    if (!all.length) return;
    // Je Zeile, die ein Plugin angibt, eine eigene Spur: Aufenthalte dauern oft Stunden und wuerden
    // Termine sonst verdecken. Bekannte Zeilen in fester Reihenfolge, weitere alphabetisch dahinter.
    const order = ["Termine", "Orte"];
    const rank = (n) => (order.indexOf(n) + 1) || 99;
    const names = [...new Set(all.map((e) => e.lane || "Termine"))]
      .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
    for (const name of names) {
      renderEventRow(lanes, v, step, width, name, all.filter((e) => (e.lane || "Termine") === name));
    }
  }
  function renderEventRow(lanes, v, step, width, label, events) {
    if (!events.length) return;
    const row = el("div", { class: "lane events-lane" });
    row.append(el("div", { class: "lane-label", text: label }));
    const track = el("div", { class: "lane-track" });
    for (let t = Math.ceil(v.start / step) * step; t <= v.end; t += step) {
      track.append(el("div", { class: "gridline", style: { left: `${xAtTime(t)}px` } }));
    }
    for (const ev of events) {
      if (ev.ts_end < v.start || ev.ts_start > v.end) continue;
      const x1 = Math.max(0, xAtTime(ev.ts_start));
      const x2 = Math.min(width, xAtTime(Math.max(ev.ts_end, ev.ts_start + 60000)));
      const w = Math.max(4, x2 - x1);
      const parts = [`${ev.source_label}${ev.category_label ? " · " + ev.category_label : ""}`, ev.subject,
                     `${ev.start_label}–${ev.end_label} · ${ev.duration_label}`];
      if (ev.location) parts.push("Ort: " + ev.location);
      if (ev.organizer) parts.push("Organisator: " + ev.organizer);
      if (ev.attendees) parts.push("Teilnehmer: " + ev.attendees);
      const node = el("div", {
        class: "event event-" + (ev.source || "x"),
        style: { left: `${x1}px`, width: `${w}px`, background: ev.color },
        title: parts.join("\n"),
      });
      if (w > 44) node.append(el("span", { class: "ev-label", text: ev.subject }));
      if (ev.geo && mapAllowed()) {
        node.style.cursor = "pointer";
        node.addEventListener("click", () => openMap(ev));
      }
      track.append(node);
    }
    row.append(track);
    lanes.append(row);
  }
  // ------------------------------------------------------------------ Karte
  // Wichtig: Leaflet ist mitgeliefert und holt von sich aus nichts aus dem Netz. Erst wenn hier eine
  // Karte erzeugt wird, laedt sie Kacheln - und das geschieht nur, wenn die Karte eingeschaltet ist
  // und der Nutzer sie oeffnet.
  function locationEvents() {
    return ((state.day && state.day.events) || []).filter((e) => e.geo);
  }
  function mapAllowed() {
    return !!(state.info && state.info.map_enabled && window.L);
  }
  function updateMapButton() {
    const btn = $("mapBtn");
    if (!btn) return;
    // Auch ohne Aufenthalte oeffnen: dann zeigt die Karte den eingestellten Startpunkt.
    btn.hidden = !mapAllowed();
    if (btn.hidden) $("mapPanel").hidden = true;
  }
  function mapHome() {
    const h = (state.info && state.info.map_home) || {};
    const lat = Number(h.lat), lon = Number(h.lon);
    if (!isFinite(lat) || !isFinite(lon)) return null;
    return { lat, lon, zoom: Number(h.zoom) || 15, label: h.label || "" };
  }
  function ensureMap() {
    if (state.map) return state.map;
    const url = (state.info && state.info.map_tile_url) || "";
    state.map = L.map("mapCanvas", { attributionControl: true });
    const home = mapHome();
    // Startausschnitt sofort setzen: Leaflet braucht vor dem ersten Layer eine Ansicht, und ohne
    // Aufenthalte bliebe die Karte sonst ohne Bezugspunkt.
    state.map.setView(home ? [home.lat, home.lon] : [51.163, 10.448], home ? home.zoom : 6);
    L.tileLayer(url, {
      maxZoom: 19,
      // Namensnennung ist bei OpenStreetMap-Daten Pflicht (ODbL).
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende',
    }).addTo(state.map);
    state.mapLayer = L.layerGroup().addTo(state.map);
    if (home && home.label) {  // fester Bezugspunkt, sobald einer benannt ist - unabhaengig von den Tagesdaten
      L.circleMarker([home.lat, home.lon], { radius: 7, color: "#2f6fed", fillColor: "#2f6fed", fillOpacity: .5, weight: 2 })
        .bindTooltip(home.label).addTo(state.map);
    }
    return state.map;
  }
  function renderMap(focus) {
    const events = locationEvents();
    if (!mapAllowed()) return;
    const map = ensureMap();
    if (!events.length) {
      const home = mapHome();
      state.mapLayer.clearLayers();
      setTimeout(() => {
        map.invalidateSize();
        if (home) map.setView([home.lat, home.lon], home.zoom);
      }, 0);
      $("mapHint").textContent = (home && home.label ? `Keine Aufenthalte an diesem Tag – Startpunkt: ${home.label}. ` : "Keine Aufenthalte an diesem Tag. ")
        + "Kartendaten von OpenStreetMap (werden beim Anzeigen aus dem Netz geladen)";
      return;
    }
    state.mapLayer.clearLayers();
    const bounds = [];
    for (const ev of events) {
      const isFocus = focus && focus.id === ev.id;
      const tip = `${ev.subject}
${ev.start_label}–${ev.end_label} · ${ev.duration_label}`;
      if (ev.geo.path) {
        const line = L.polyline(ev.geo.path, { color: isFocus ? "#d23f3f" : "#0f8a6a", weight: isFocus ? 5 : 4, opacity: .9 });
        line.bindTooltip(tip); line.addTo(state.mapLayer);
        ev.geo.path.forEach((pt) => bounds.push(pt));
        const ends = [ev.geo.path[0], ev.geo.path[ev.geo.path.length - 1]];
        ends.forEach((pt, i) => L.circleMarker(pt, { radius: 5, color: "#0f8a6a", fillColor: i ? "#d23f3f" : "#ffffff", fillOpacity: 1, weight: 2 })
          .bindTooltip(i ? "Ziel" : "Start").addTo(state.mapLayer));
      } else {
        const m = L.circleMarker([ev.geo.lat, ev.geo.lon], {
          radius: isFocus ? 11 : 8, color: "#0f8a6a", fillColor: isFocus ? "#d23f3f" : "#0f8a6a", fillOpacity: .85, weight: 2 });
        m.bindTooltip(tip); m.addTo(state.mapLayer);
        bounds.push([ev.geo.lat, ev.geo.lon]);
      }
    }
    // Leaflet muss nach dem Einblenden neu vermessen werden, sonst bleibt die Karte grau.
    setTimeout(() => {
      map.invalidateSize();
      if (focus && focus.geo) {
        if (focus.geo.path) map.fitBounds(focus.geo.path, { padding: [30, 30] });
        else map.setView([focus.geo.lat, focus.geo.lon], 16);
      } else if (bounds.length) {
        map.fitBounds(bounds, { padding: [30, 30] });
      }
    }, 0);
    const visits = events.filter((e) => !e.geo.path).length;
    $("mapHint").textContent = `${visits} Aufenthalte, ${events.length - visits} Fahrten · Kartendaten von OpenStreetMap (werden beim Anzeigen aus dem Netz geladen)`;
  }
  function openMap(focus) {
    if (!mapAllowed()) return;
    $("mapPanel").hidden = false;
    renderMap(focus);
  }
  function toggleMap() {
    const panel = $("mapPanel");
    if (panel.hidden) openMap(null); else panel.hidden = true;
  }
  function updateNowMarker() {
    const marker = $("nowMarker");
    if (!state.view || state.date !== todayIso()) { marker.hidden = true; return; }
    const now = Date.now();
    if (now < state.view.start || now > state.view.end) { marker.hidden = true; return; }
    marker.style.left = `${LANE_LABEL_W + xAtTime(now)}px`;
    marker.hidden = false;
  }

  // ------------------------------------------------------------------ Zoom / Pan
  function clampView(start, end) {
    const d = state.day;
    const minSpan = MIN, maxSpan = 26 * HOUR;
    let span = Math.min(maxSpan, Math.max(minSpan, end - start));
    const lo = d.start_ms - HOUR, hi = d.end_ms + HOUR;
    if (start < lo) start = lo;
    if (start + span > hi) start = hi - span;
    return { start, end: start + span };
  }
  function onWheel(e) {
    if (!state.view) return;
    e.preventDefault();
    const rect = $("lanes").getBoundingClientRect();
    const x = e.clientX - rect.left - LANE_LABEL_W;
    const t = timeAtX(Math.max(0, x));
    const factor = Math.pow(1.25, e.deltaY / 100);
    const v = state.view;
    let span = (v.end - v.start) * factor;
    span = Math.min(26 * HOUR, Math.max(MIN, span));
    const ratio = Math.max(0, Math.min(1, x / trackWidth()));
    state.view = clampView(t - ratio * span, t + (1 - ratio) * span);
    render();
  }
  function onMouseDown(e) {
    if (e.button !== 0 || !state.view) return;
    state.suppressClick = false;
    state.dragging = { x: e.clientX, start: state.view.start, end: state.view.end, moved: false };
    $("lanes").classList.add("dragging");
  }
  function onMouseMove(e) {
    const d = state.dragging;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (Math.abs(dx) > 3) d.moved = true;
    const msPerPx = (d.end - d.start) / trackWidth();
    state.view = clampView(d.start - dx * msPerPx, d.end - dx * msPerPx);
    render();
  }
  function onMouseUp() {
    if (state.dragging) {
      if (state.dragging.moved) state.suppressClick = true;  // verhindert, dass das folgende click den Block oeffnet
      $("lanes").classList.remove("dragging");
      state.dragging = null;
    }
  }
  function onDoubleClick() {
    if (!state.day) return;
    state.view = { start: state.day.start_ms, end: state.day.end_ms };
    render();
  }

  // ------------------------------------------------------------------ Auswahl / Details
  function selectBlock(block, laneIdx, atMs) {
    let index = 0;
    if (block.count > 1 && block.ts_end > block.ts_start) {
      const ratio = (atMs - block.ts_start) / (block.ts_end - block.ts_start);
      index = Math.max(0, Math.min(block.count - 1, Math.floor(ratio * block.count)));
    }
    state.selected = { block, lane: laneIdx, index, entryId: block.ids[index] };
    render();
    showEntry(block.ids[index]);
  }
  async function showEntry(entryId) {
    const detail = $("detail");
    try {
      const [entry, thumb] = await Promise.all([state.api.get_entry(entryId), state.api.get_thumbnail(entryId, 900)]);
      if (!entry) { detail.hidden = true; return; }
      if (state.selected) state.selected.entryId = entryId;
      $("thumb").src = thumb ? thumb.data_uri : "";
      $("detailTitle").textContent = entry.window_title || "(ohne Fenstertitel)";
      $("detailMeta").replaceChildren(
        el("span", {}, "App: ", el("b", { text: entry.process_name || "unbekannt" })),
        el("span", {}, "Zeit: ", el("b", { text: `${fmtDate(entry.date)} ${entry.start_label} – ${entry.end_label}` })),
        el("span", {}, "Dauer: ", el("b", { text: entry.duration_label })),
        el("span", {}, `Monitor ${entry.monitor_id} · ${entry.width}×${entry.height}`),
        el("span", {}, entry.ocr_status_label + (entry.ocr_conf ? ` (Ø ${Math.round(entry.ocr_conf)} %)` : "")),
      );
      renderOcr(entry.ocr_text || (entry.ocr_status === 0 ? "… wird noch erkannt …" : ""), searchTerms());
      const sel = state.selected;
      if (sel && sel.block) {
        $("entryPos").textContent = `Bild ${sel.index + 1} von ${sel.block.count} im Block`;
      } else {
        $("entryPos").textContent = "";
      }
      $("prevEntry").disabled = !(sel && sel.index > 0) && !entry.prev_id;
      $("nextEntry").disabled = !(sel && sel.index < sel.block.count - 1) && !entry.next_id;
      detail.dataset.entryId = String(entryId);
      detail.dataset.prevId = entry.prev_id || "";
      detail.dataset.nextId = entry.next_id || "";
      detail.hidden = false;
    } catch (e) {
      showBanner("Eintrag konnte nicht geladen werden: " + errMessage(e), true);
    }
  }
  function searchTerms() {
    return (state.query || "").split(/\s+/).map((t) => t.replace(/["*]/g, "")).filter((t) => t.length >= 2 && !/^(OR|AND|NOT)$/i.test(t));
  }
  function renderOcr(text, terms) {
    const pre = $("ocrText");
    pre.replaceChildren();
    if (!text) { pre.textContent = "(kein Text erkannt)"; return; }
    if (!terms.length) { pre.textContent = text; return; }
    const lower = text.toLowerCase();
    const lowerTerms = terms.map((t) => t.toLowerCase());
    let pos = 0;
    while (pos < text.length) {
      let best = -1, bestLen = 0;
      for (const t of lowerTerms) {
        const i = lower.indexOf(t, pos);
        if (i !== -1 && (best === -1 || i < best)) { best = i; bestLen = t.length; }
      }
      if (best === -1) { pre.append(text.slice(pos)); break; }
      if (best > pos) pre.append(text.slice(pos, best));
      pre.append(el("mark", { text: text.slice(best, best + bestLen) }));
      pos = best + bestLen;
    }
  }
  function stepEntry(direction) {
    const sel = state.selected;
    const detail = $("detail");
    if (sel && sel.block) {
      const next = sel.index + direction;
      if (next >= 0 && next < sel.block.count) {
        sel.index = next;
        showEntry(sel.block.ids[next]);
        return;
      }
    }
    const id = direction < 0 ? detail.dataset.prevId : detail.dataset.nextId;
    if (id) jumpToEntry(Number(id));
  }
  async function jumpToEntry(entryId, dateHint = null) {
    try {
      const entry = await state.api.get_entry(entryId);
      if (!entry) return;
      if (entry.date !== state.date) { await setDate(entry.date); }
      const found = findBlock(entryId);
      if (found) {
        state.selected = { block: found.block, lane: found.lane, index: found.index, entryId };
        const span = Math.max(30 * MIN, Math.min(state.view.end - state.view.start, 2 * HOUR));
        const center = entry.ts_start;
        if (center < state.view.start || center > state.view.end) state.view = clampView(center - span / 2, center + span / 2);
        render();
      }
      showEntry(entryId);
    } catch (e) {
      showBanner("Sprung fehlgeschlagen: " + errMessage(e), true);
    }
  }
  function findBlock(entryId) {
    if (!state.day) return null;
    for (let l = 0; l < state.day.lanes.length; l++) {
      for (const block of state.day.lanes[l].blocks) {
        const i = block.ids.indexOf(entryId);
        if (i !== -1) return { block, lane: l, index: i };
      }
    }
    return null;
  }
  async function deleteCurrentEntry() {
    const id = Number($("detail").dataset.entryId);
    if (!id) return;
    if (!confirm("Diesen Eintrag (Screenshot und Text) endgültig löschen?")) return;
    try {
      await state.api.delete_entry(id);
      $("detail").hidden = true;
      state.selected = null;
      await loadDay(true);
    } catch (e) {
      showBanner("Löschen fehlgeschlagen: " + errMessage(e), true);
    }
  }

  // ------------------------------------------------------------------ Suche
  function onSearchInput() {
    clearTimeout(state.searchTimer);
    const q = $("searchInput").value.trim();
    state.query = q;
    if (q.length < 2) { $("results").hidden = true; return; }
    state.searchTimer = setTimeout(runSearch, 300);
  }
  async function runSearch() {
    const q = state.query;
    if (q.length < 2) return;
    try {
      const rows = await state.api.search(q, null, null, 150);
      const list = $("resultList");
      list.replaceChildren();
      $("resultsTitle").textContent = rows.length ? `${rows.length} Treffer für „${q}“` : `Keine Treffer für „${q}“`;
      for (const r of rows) {
        const li = el("li", { title: `${r.process_name} – ${r.window_title}` },
          el("span", { class: "r-time", text: `${fmtDate(r.date)} ${r.time_label}` }),
          el("span", { class: "r-app" }, r.window_title || appLabel(r.process_name),
              el("small", { text: r.window_title ? appLabel(r.process_name) : "" })),
          snippetNode(r.snippet));
        li.addEventListener("click", () => jumpToEntry(r.id, r.date));
        list.append(li);
      }
      $("results").hidden = false;
    } catch (e) {
      showBanner("Suche fehlgeschlagen: " + errMessage(e), true);
    }
  }
  function snippetNode(snippet) {
    const span = el("span", { class: "r-snip" });
    if (!snippet) { span.textContent = "(Treffer im Fenstertitel)"; return span; }
    // FTS-Snippet markiert Treffer mit STX/ETX (\x02..\x03) - eckige Klammern im Text stoeren so nicht.
    const parts = snippet.split(/(\x02[^\x03]*\x03)/);
    for (const p of parts) {
      if (!p) continue;
      if (p.startsWith("\x02") && p.endsWith("\x03")) span.append(el("mark", { text: p.slice(1, -1) }));
      else span.append(p);
    }
    return span;
  }

  // ------------------------------------------------------------------ Einstellungen / Ersteinrichtung
  const FIELDS = [
    { section: "Aufnahme" },
    { key: "capture_interval_seconds", label: "Aufnahmeintervall (Sekunden)", type: "number", min: 2, max: 60, help: "Wie oft der Bildschirm geprüft wird (2–60). Unveränderte Bilder werden nicht erneut gespeichert." },
    { key: "idle_pause_minutes", label: "Pause bei Inaktivität nach (Minuten)", type: "number", min: 1, max: 240, help: "Ohne Maus-/Tastatureingaben pausiert die Aufnahme automatisch." },
    { key: "change_threshold", label: "Änderungsschwelle (Anteil Pixel)", type: "number", min: 0, max: 1, step: 0.001, help: "0.01 = 1 % der Bildpunkte müssen sich ändern, damit ein neues Bild gespeichert wird." },
    { key: "max_block_minutes", label: "Spätestens neues Bild nach (Minuten)", type: "number", min: 1, max: 240 },
    { section: "Speicherung" },
    { key: "retention_days", label: "Aufbewahrung (Tage)", type: "number", min: 1, max: 3650, help: "Ältere Einträge werden täglich gelöscht und der Platz freigegeben." },
    { key: "db_path", label: "Speicherort der Datenbank", type: "path", help: "Verschlüsselte SQLCipher-Datei (AES-256). Eine Änderung erfordert einen Neustart." },
    { key: "max_image_width", label: "Maximale Bildbreite (Pixel)", type: "number", min: 640, max: 7680, help: "Größere Screenshots werden vor dem Speichern verkleinert. Kleinere Werte sparen viel Platz." },
    { key: "webp_quality", label: "WebP-Qualität (30–95)", type: "number", min: 30, max: 95 },
    { key: "max_db_size_gb", label: "Maximale Datenbankgröße (GB)", type: "number", min: 0.1, step: 0.1, help: "Wird sie überschritten, werden die ältesten Tage zuerst gelöscht." },
    { key: "min_free_disk_gb", label: "Aufnahme pausieren unter freiem Speicher (GB)", type: "number", min: 0, step: 0.5 },
    { section: "Texterkennung" },
    { key: "ocr_language", label: "OCR-Sprachen", type: "text", help: "Tesseract-Sprachcodes, z. B. deu+eng (Neustart erforderlich)." },
    { key: "ocr_psm", label: "OCR-Segmentierung (PSM)", type: "select", options: [[3, "3 – automatisch (Standard)"], [4, "4 – Spaltenlayout"], [6, "6 – ein Textblock"], [11, "11 – verstreuter Text"], [12, "12 – verstreut mit Ausrichtung"]], help: "Neustart erforderlich." },
    { key: "ocr_min_confidence", label: "Mindestkonfidenz für Wörter (%)", type: "number", min: 0, max: 100, help: "Unsichere Wörter werden verworfen (Neustart erforderlich)." },
    { section: "Datenschutz" },
    { key: "excluded_process_regex", label: "Ausgeschlossene Prozesse (Regex, je Zeile)", type: "textarea", help: "Ist eines dieser Programme im Vordergrund, wird nicht aufgenommen. Groß-/Kleinschreibung egal." },
    { key: "excluded_title_regex", label: "Ausgeschlossene Fenstertitel (Regex, je Zeile)", type: "textarea", help: "z. B. .*Inkognito.* oder .*Banking.*" },
    { section: "Karte und Orte", feature: "locations" },
    { key: "map_enabled", feature: "locations", label: "Karte anzeigen (lädt Kacheln aus dem Netz)", type: "bool", help: "Zeigt Aufenthalte und Fahrten auf einer OpenStreetMap-Karte. ACHTUNG: Nur dafür holt Zeitspur Daten aus dem Internet – der Kachel-Server erfährt dabei, welche Gegenden Sie sich ansehen. Standardmäßig aus." },
    { key: "map_tile_url", feature: "locations", label: "Kachel-Adresse", type: "text", help: "Nur ändern, wenn Sie einen eigenen Kachel-Server betreiben. Muss https sein und {z}/{x}/{y} enthalten." },
    { key: "map_home_label", feature: "locations", label: "Startpunkt der Karte (Name)", type: "text", help: "Mit Namen wird der Startpunkt als Bezugspunkt auf der Karte angezeigt und zählt als bekannter Ort – z. B. „Büro“. Leer: kein Bezugspunkt." },
    { key: "map_home_lat", feature: "locations", label: "Startpunkt: Breite", type: "number", step: 0.0000001, min: -90, max: 90 },
    { key: "map_home_lon", feature: "locations", label: "Startpunkt: Länge", type: "number", step: 0.0000001, min: -180, max: 180 },
    { key: "map_home_zoom", feature: "locations", label: "Startpunkt: Zoomstufe", type: "number", min: 1, max: 19, help: "15 zeigt die nähere Umgebung, 17 die einzelne Straße." },
    { key: "known_places", feature: "locations", label: "Bekannte Orte (je Zeile)", type: "textarea", help: "Macht aus Koordinaten Namen – Claude sagt dann „du warst im Büro“ statt Zahlen. Format: Name;Breite;Länge  (optional ;Radius in Metern, Standard 150). Beispiel: Büro;52.5163;13.3777;200 – ein benannter Startpunkt oben zählt automatisch mit." },
    { section: "Synchronisierung" },
    { key: "events_sync_minutes", label: "Termin-/Anruf-Sync alle (Minuten)", type: "number", min: 1, max: 1440 },
    { key: "events_window_days", label: "Sync-Fenster (± Tage)", type: "number", min: 1, max: 90 },
    { section: "Start" },
    { key: "autostart", label: "Automatisch mit Windows starten", type: "bool", help: "Legt einen Autostart-Eintrag nur für Ihr Benutzerkonto an (keine Administratorrechte). Zeitspur startet dann versteckt im Infobereich; die Aufnahme beginnt kurz verzögert. Gilt sofort, ohne Neustart." },
    { section: "Updates", feature: "updates" },
    { key: "auto_update", feature: "updates", label: "Updates automatisch installieren", type: "bool", help: "Neue Versionen werden im Hintergrund geladen, auf ihre digitale Signatur geprüft und installiert, sobald der PC eine Weile nicht benutzt wird (siehe unten). Zeitspur startet danach von selbst wieder. Ausgeschaltet erscheint nur ein Hinweis mit dem Knopf „Jetzt aktualisieren“." },
    { key: "update_idle_minutes", feature: "updates", label: "Installieren nach Leerlauf von (Minuten)", type: "number", min: 1, max: 240, help: "So lange dürfen Maus und Tastatur unbenutzt sein, bevor ein geladenes Update installiert wird – damit es nie mitten in der Arbeit passiert. Standard: 5 Minuten." },
    { section: "Sonstiges" },
    { key: "log_level", label: "Protokollstufe", type: "select", options: [["INFO", "INFO (Standard)"], ["WARNING", "WARNING"], ["ERROR", "ERROR"], ["DEBUG", "DEBUG – ausführlich (Neustart)"]] },
  ];

  async function showSetup(mode) {
    state.mode = mode;
    stopPolling();
    $("timelineView").hidden = true;
    $("setupView").hidden = false;
    $("daynav").style.visibility = "hidden";
    const firstRun = mode === "setup";
    $("setupTitle").textContent = firstRun ? "Willkommen bei Zeitspur – Ersteinrichtung" : "Einstellungen";
    $("setupIntro").textContent = firstRun
      ? "Zeitspur läuft im Hintergrund, speichert regelmäßig verschlüsselte Screenshots mit erkanntem Text und löscht sie nach Ablauf der Aufbewahrungsfrist. Die Werte lassen sich später in den Einstellungen ändern. Nach dem Speichern wird der Schlüssel erzeugt und die Datenbank angelegt."
      : "Änderungen werden gespeichert; die meisten wirken sofort, einige erst nach einem Neustart des Dienstes.";
    $("saveBtn").textContent = firstRun ? "Speichern und starten" : "Speichern";
    $("cancelBtn").hidden = firstRun;
    $("formError").hidden = true;
    $("formNote").textContent = "";
    // Zustand auffrischen, BEVOR die Panels gebaut werden: sonst entscheidet ein veraltetes (womoeglich
    // leeres) state.info darueber, ob "Zugangsdaten gespeichert" dasteht - und meldet faelschlich "keine".
    try { applyState(await state.api.get_state()); } catch (e) { /* Panels nutzen dann den letzten Stand */ }
    renderVersionLine();
    let cfg = {};
    try { cfg = await state.api.get_config(); } catch (e) { showBanner("Konfiguration konnte nicht gelesen werden: " + errMessage(e), true); }
    buildForm(cfg);
    if (!firstRun) await buildPluginsPanel(cfg);
  }
  // ------------------------------------------------------------------ Plugins
  // Die Oberflaeche kennt kein Plugin beim Namen: Name, Beschreibung, Zugangsdaten- und Einstellungsfelder
  // kommen aus plugins.py. Ein dort ergaenztes Plugin erscheint hier ohne weitere Aenderung.
  async function buildPluginsPanel(cfg) {
    const holder = $("formFields");
    holder.append(el("div", { class: "section-title", text: "Plugins" }));
    holder.append(el("div", { class: "field" }, el("div", { class: "help", style: { gridColumn: "1 / -1" } },
      "Termine, Anrufe und Orte kommen aus Plugins. Sie sind mitgeliefert, aber erst aktiv, wenn Sie sie " +
      "hinzufügen. Zugangsdaten werden per DPAPI geschützt gespeichert, nicht in der config.yaml.")));
    const box = el("div", { class: "plugins-box", id: "pluginsBox" });
    holder.append(box);
    await fillPlugins(box, cfg);
  }
  async function fillPlugins(box, cfg) {
    let list = [];
    try { list = await state.api.list_plugins(); }
    catch (e) { box.replaceChildren(el("div", { class: "help", text: "Plugins konnten nicht geladen werden: " + errMessage(e) })); return; }
    box.replaceChildren(...list.map((p) => pluginCard(p, cfg, () => fillPlugins(box, cfg))));
  }
  function pluginBadge(p) {
    if (!p.installed) return { text: "nicht hinzugefügt", cls: "" };
    if (p.problem) return { text: "⚠ " + p.problem, cls: "warn" };
    return { text: "● eingerichtet", cls: "ok" };
  }
  function pluginCard(p, cfg, refresh) {
    const card = el("div", { class: "plugin-card" + (p.installed ? " installed" : "") });
    const status = el("span", { class: "muted" });
    const badgeInfo = pluginBadge(p);
    const badge = el("span", { class: "plugin-badge " + badgeInfo.cls, text: badgeInfo.text });
    card.append(
      el("div", { class: "plugin-head" },
        el("span", { class: "plugin-dot", style: { background: p.color } }),
        el("b", { text: p.name }), badge),
      el("div", { class: "plugin-desc", text: p.description }));

    if (!p.installed) {
      const add = el("button", { type: "button", class: "btn small", text: "Hinzufügen" });
      if (!p.available) { add.disabled = true; status.textContent = p.unavailable_reason || ""; }
      add.addEventListener("click", async () => {
        add.disabled = true;
        status.textContent = "Füge hinzu …";
        try { await state.api.add_plugin(p.id); await refresh(); }
        catch (e) { status.textContent = errMessage(e); add.disabled = false; }
      });
      card.append(el("div", { class: "plugin-actions" }, add, status));
      return card;
    }

    // Installiert: Einrichtung. Zugangsdaten werden nie vorbelegt (nur auf "Anzeigen"); leer gelassen
    // bleiben die gespeicherten erhalten. Einstellungen kommen aus config.yaml und sind vorbelegt.
    const inputs = {};
    const addField = (f, value, isCredential) => {
      const id = `plg_${p.id}_${f.key}`;
      let input;
      if (f.kind === "list") {
        input = el("textarea", { id });
        input.value = Array.isArray(value) ? value.join("\n") : (value || "");
      } else {
        const stored = isCredential && p.has_credentials;
        input = el("input", { id, type: f.kind === "secret" ? "password" : "text", autocomplete: "off",
          placeholder: stored ? "(gespeichert – zum Ändern neu eingeben)" : (f.placeholder || "") });
        if (!isCredential) input.value = value == null ? "" : String(value);
      }
      inputs[f.key] = input;
      const field = el("div", { class: "field" }, el("label", { for: id, text: f.label }), input);
      if (f.help) field.append(el("div", { class: "help", text: f.help }));
      card.append(field);
    };
    for (const f of p.credential_fields) addField(f, "", true);
    for (const f of p.setting_fields) addField(f, cfg[f.key], false);

    const saveBtn = el("button", { type: "button", class: "btn small", text: "Speichern & testen" });
    const testBtn = el("button", { type: "button", class: "btn small", text: "Verbindung testen" });
    const removeBtn = el("button", { type: "button", class: "btn small danger", text: "Entfernen" });
    const actions = el("div", { class: "plugin-actions" });
    if (p.credential_fields.length || p.setting_fields.length) actions.append(saveBtn);
    actions.append(testBtn);

    if (p.credential_fields.length) {
      const showBtn = el("button", { type: "button", class: "btn small", text: "Anzeigen" });
      showBtn.addEventListener("click", async () => {
        try {
          const c = await state.api.reveal_plugin_credentials(p.id);
          for (const f of p.credential_fields) {
            inputs[f.key].value = c[f.key] || "";
            if (f.kind === "secret") inputs[f.key].type = "text";
          }
          status.textContent = "Gespeicherte Werte eingeblendet – zum Kopieren markieren.";
        } catch (e) { status.textContent = errMessage(e); }
      });
      actions.append(showBtn);
    }
    actions.append(removeBtn, status);
    card.append(actions);

    saveBtn.addEventListener("click", async () => {
      status.textContent = "Speichere …";
      saveBtn.disabled = true;
      try {
        if (p.setting_fields.length) {
          const settings = {};
          for (const f of p.setting_fields) settings[f.key] = inputs[f.key].value;
          await state.api.save_config(settings);
          for (const f of p.setting_fields) cfg[f.key] = inputs[f.key].value;
        }
        const creds = {};
        let entered = false;
        for (const f of p.credential_fields) {
          creds[f.key] = inputs[f.key].value;
          if (creds[f.key].trim()) entered = true;
        }
        let res;
        if (entered) {
          status.textContent = "Speichere und teste …";
          res = await state.api.save_plugin_credentials(p.id, creds);
        } else if (p.credential_fields.length && !p.has_credentials) {
          throw new Error("Bitte die Zugangsdaten eingeben.");
        } else {
          status.textContent = "Teste …";
          res = await state.api.test_plugin(p.id);
        }
        for (const f of p.credential_fields) if (f.kind === "secret") inputs[f.key].value = "";
        await refresh();
        // Nach dem Neuaufbau steht die Karte frisch da - die Meldung dort anzeigen.
        const fresh = $(`plg_${p.id}_status`);
        if (fresh) fresh.textContent = res.message || "Gespeichert.";
      } catch (e) {
        status.textContent = errMessage(e);
        saveBtn.disabled = false;
      }
    });
    status.id = `plg_${p.id}_status`;
    testBtn.addEventListener("click", async () => {
      status.textContent = "Teste …";
      try { status.textContent = (await state.api.test_plugin(p.id)).message; }
      catch (e) { status.textContent = errMessage(e); }
    });
    removeBtn.addEventListener("click", async () => {
      if (!confirm(`Plugin „${p.name}“ entfernen?\n\nDie gespeicherten Zugangsdaten und alle Ereignisse dieses ` +
                   "Plugins werden gelöscht. Erneutes Hinzufügen holt die Ereignisse des Sync-Fensters zurück.")) return;
      removeBtn.disabled = true;
      try {
        const res = await state.api.remove_plugin(p.id);
        await refresh();
        showBanner(`„${p.name}“ entfernt – ${res.removed_events} Ereignisse gelöscht.`, false);
      } catch (e) { status.textContent = errMessage(e); removeBtn.disabled = false; }
    });
    return card;
  }
  // Was diese Ausgabe kann (edition.py). Fehlt die Angabe (aelteres Programm), gilt alles als vorhanden.
  function hasFeature(name) {
    const f = state.info && state.info.features;
    return !f || f[name] !== false;
  }
  function buildForm(cfg) {
    const holder = $("formFields");
    holder.replaceChildren();
    for (const f of FIELDS) {
      if (f.feature && !hasFeature(f.feature)) continue;   // in dieser Ausgabe nicht enthalten
      if (f.section) { holder.append(el("div", { class: "section-title", text: f.section })); continue; }
      const value = cfg[f.key];
      let input;
      if (f.type === "textarea") {
        input = el("textarea", { name: f.key, id: `f_${f.key}` });
        input.value = Array.isArray(value) ? value.join("\n") : (value || "");
      } else if (f.type === "select") {
        input = el("select", { name: f.key, id: `f_${f.key}` });
        for (const [v, label] of f.options) input.append(el("option", { value: String(v), text: label }));
        input.value = String(value);
      } else if (f.type === "bool") {
        input = el("select", { name: f.key, id: `f_${f.key}` });
        input.append(el("option", { value: "true", text: "Ein" }));
        input.append(el("option", { value: "false", text: "Aus" }));
        input.value = value ? "true" : "false";
      } else if (f.type === "path") {
        input = el("input", { type: "text", name: f.key, id: `f_${f.key}` });
        input.value = value || "";
      } else {
        input = el("input", { type: f.type, name: f.key, id: `f_${f.key}`, min: f.min, max: f.max, step: f.step || (f.type === "number" ? "any" : undefined) });
        input.value = value === undefined || value === null ? "" : String(value);
      }
      const field = el("div", { class: "field" }, el("label", { for: `f_${f.key}`, text: f.label }));
      if (f.type === "path") {
        const btn = el("button", { type: "button", class: "btn small", text: "Ordner wählen …" });
        btn.addEventListener("click", async () => {
          try { const p = await state.api.choose_folder(); if (p) input.value = p; } catch (e) { /* abgebrochen */ }
        });
        field.append(el("div", { class: "row" }, input, btn));
      } else {
        field.append(input);
      }
      if (f.help) field.append(el("div", { class: "help", text: f.help }));
      holder.append(field);
    }
  }
  function collectForm() {
    const values = {};
    for (const f of FIELDS) {
      if (!f.key) continue;
      const input = $(`f_${f.key}`);
      if (!input) continue;
      values[f.key] = input.value;
    }
    return values;
  }
  async function submitForm(e) {
    e.preventDefault();
    const values = collectForm();
    const setup = state.mode === "setup";
    $("formError").hidden = true;
    $("saveBtn").disabled = true;
    if (setup) {
      // Schlüssel erzeugen, Datenbank anlegen und Aufnahme starten dauert ein paar Sekunden - sagen, was passiert
      $("saveBtn").textContent = "Wird eingerichtet …";
      $("formNote").textContent = "Schlüssel und verschlüsselte Datenbank werden angelegt, danach startet die Aufnahme.";
    }
    try {
      if (setup) {
        const info = await state.api.complete_setup(values);
        if (!info.ok) { throw new Error("Die Datenbank konnte nicht angelegt werden. Details im Protokoll."); }
        applyState(info);
        await leaveSetup();
        state.date = info.today;
        await loadDay();
        startPolling();
      } else {
        const res = await state.api.save_config(values);
        $("formNote").textContent = res.restart_required ? "Gespeichert. Einige Änderungen wirken erst nach einem Neustart des Dienstes." : "Gespeichert.";
        setTimeout(() => { leaveSetup().then(() => refresh()); }, 900);
      }
    } catch (err) {
      if (setup) $("formNote").textContent = "";
      const box = $("formError");
      box.textContent = errMessage(err);
      box.hidden = false;
    } finally {
      $("saveBtn").disabled = false;
      if (setup) $("saveBtn").textContent = "Speichern und starten";
    }
  }
  async function leaveSetup() {
    state.mode = "timeline";
    $("setupView").hidden = true;
    $("timelineView").hidden = false;
    $("daynav").style.visibility = "visible";
    if (!state.pollTimer) startPolling();   // showSetup hatte es gestoppt
  }

  // ------------------------------------------------------------------ Events
  function bindEvents() {
    $("updateBtn").addEventListener("click", startUpdate);
    $("checkUpdatesBtn").addEventListener("click", checkUpdates);
    $("updateNotesBtn").addEventListener("click", () => { const n = $("updateNotes"); n.hidden = !n.hidden; });
    $("prevDay").addEventListener("click", () => setDate(addDays(state.date, -1)));
    $("nextDay").addEventListener("click", () => setDate(addDays(state.date, 1)));
    $("todayBtn").addEventListener("click", () => setDate(todayIso()));
    $("dayPicker").addEventListener("change", (e) => { if (e.target.value) setDate(e.target.value); });
    $("searchInput").addEventListener("input", onSearchInput);
    $("searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(state.searchTimer); runSearch(); } if (e.key === "Escape") { e.target.value = ""; onSearchInput(); } });
    $("closeResults").addEventListener("click", () => { $("results").hidden = true; });
    $("mapBtn").addEventListener("click", () => toggleMap());
    $("closeMap").addEventListener("click", () => { $("mapPanel").hidden = true; });
    $("pauseBtn").addEventListener("click", async () => {
      try { applyState(await state.api.set_paused(!state.info.paused)); } catch (e) { showBanner(errMessage(e), true); }
    });
    $("settingsBtn").addEventListener("click", () => {
      if (state.mode === "settings") leaveSetup().then(() => refresh());
      else if (state.mode === "timeline") showSetup("settings");
    });
    $("cancelBtn").addEventListener("click", () => leaveSetup().then(() => refresh()));
    $("setupForm").addEventListener("submit", submitForm);
    $("prevEntry").addEventListener("click", () => stepEntry(-1));
    $("nextEntry").addEventListener("click", () => stepEntry(1));
    $("deleteEntry").addEventListener("click", deleteCurrentEntry);
    const tl = $("timeline");
    tl.addEventListener("wheel", onWheel, { passive: false });
    $("lanes").addEventListener("mousedown", onMouseDown);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    $("lanes").addEventListener("dblclick", onDoubleClick);
    window.addEventListener("resize", () => render());
    document.addEventListener("keydown", (e) => {
      if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (e.key === "ArrowLeft" && !$("detail").hidden) stepEntry(-1);
      if (e.key === "ArrowRight" && !$("detail").hidden) stepEntry(1);
    });
  }
})();
