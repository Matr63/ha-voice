(() => {
  "use strict";

  const BASE = String(window.HV_BASE || "").replace(/\/+$/, "");
  const PAGE = 50;
  const POLL_MS = 4000;
  const TABS = ["speaker", "diary", "voices", "tts", "assistant", "settings"];
  const TAB_ALIASES = { overview: "speaker", assist: "assistant", test: "voices" };
  const SECURE = !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const view = (name) => $(`.view[data-view="${name}"]`);

  /* ---------------- icons ---------------- */
  const ICONS = {
    mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>',
    userPlus: '<circle cx="9" cy="8" r="4"/><path d="M2 21v-1a6 6 0 0 1 6-6h2a6 6 0 0 1 6 6v1"/><line x1="19" x2="19" y1="8" y2="14"/><line x1="16" x2="22" y1="11" y2="11"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M8 16H3v5"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
    check: '<polyline points="20 6 9 17 4 12"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
    wave: '<path d="M2 12h2"/><path d="M6 8v8"/><path d="M10 4v16"/><path d="M14 7v10"/><path d="M18 9v6"/><path d="M22 12h-2"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    alert: '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    flask: '<path d="M9 2h6"/><path d="M10 2v6L4 19a2 2 0 0 0 1.8 3h12.4a2 2 0 0 0 1.8-3L14 8V2"/><path d="M7 15h10"/>',
    speaker: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/>',
    chip: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
    robot: '<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" x2="8" y1="16" y2="16"/><line x1="16" x2="16" y1="16" y2="16"/>',
    home: '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
  };
  const icon = (name, cls = "") =>
    `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;

  /* ---------------- API ---------------- */
  async function api(path, opts = {}) {
    const res = await fetch(BASE + path, { cache: "no-store", credentials: "same-origin", ...opts });
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (data && data.error) || (typeof data === "string" && data.trim()) || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }
  const postJson = (path, body, method = "POST") =>
    api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
  const sendAudio = (path, wav) => api(path, { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });

  /* ---------------- formatting ---------------- */
  const parseTs = (ts) => new Date(String(ts || "").replace(/(\.\d{3})\d+/, "$1"));
  const fmtTime = (ts) => parseTs(ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const dayKey = (ts) => {
    const d = parseTs(ts);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  function dayLabel(ts) {
    const d = parseTs(ts);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Сегодня";
    if (d.toDateString() === yesterday.toDateString()) return "Вчера";
    return d.toLocaleDateString("ru-RU", {
      weekday: "long",
      day: "numeric",
      month: "long",
      year: d.getFullYear() === today.getFullYear() ? undefined : "numeric",
    });
  }
  const fmtDur = (ms) => `${(Number(ms || 0) / 1000).toFixed(1)} с`;
  const pct = (v) => (v == null ? "—" : `${Math.round(Number(v) * 100)}%`);
  function plural(n, one, few, many) {
    const m10 = n % 10;
    const m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }
  function fmtAgo(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return "только что";
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m} мин назад`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} ч назад`;
    const d = Math.floor(h / 24);
    return `${d} ${plural(d, "день", "дня", "дней")} назад`;
  }
  const fmtAgoTs = (ts) => fmtAgo((Date.now() - parseTs(ts).getTime()) / 1000);
  function fmtUptime(s) {
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    return d ? `${d} д ${h} ч` : h ? `${h} ч ${m} мин` : `${m} мин`;
  }
  const levelPct = (level) => Math.min(100, Math.sqrt(Math.max(0, level) / 0.15) * 100);
  function dayRange(value) {
    const [y, m, d] = value.split("-").map(Number);
    const start = new Date(y, m - 1, d);
    const end = new Date(y, m - 1, d + 1);
    return [start.toISOString(), end.toISOString()];
  }
  function startOfTodayIso() {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.toISOString();
  }

  function hue(str) {
    let h = 7;
    for (const c of String(str)) h = (h * 31 + c.codePointAt(0)) % 360;
    return h;
  }
  const initial = (name) => (String(name || "?").trim()[0] || "?").toUpperCase();
  function speakerChip(id, name, guess = false) {
    if (!id) return `<span class="chip unknown">${icon("user")}Неизвестный</span>`;
    if (guess) return `<span class="chip unknown" title="Ближайший голос, но ниже порога">≈ ${esc(name)}</span>`;
    return `<span class="chip" style="--h:${hue(id)}">${esc(name)}</span>`;
  }
  const pill = (dot, text) => `<span class="pill"><span class="dot ${dot}"></span>${esc(text)}</span>`;
  const badge = (kind, ic, text) => `<span class="badge ${kind}">${icon(ic)}${esc(text)}</span>`;
  const banner = (kind, title, text, action = "") =>
    `<div class="banner ${kind}">${icon(kind === "info" ? "info" : "alert")}<div class="spacer"><b>${esc(title)}</b>${text}</div>${action}</div>`;
  const statCard = (label, value, hint) =>
    `<div class="card stat"><span class="stat-label">${esc(label)}</span><span class="stat-value">${esc(value)}</span><span class="stat-hint">${esc(hint)}</span></div>`;
  const emptyBox = (ic, text) => `<div class="empty">${icon(ic)}<div>${text}</div></div>`;

  /* ---------------- toasts & modal ---------------- */
  function toast(message, kind = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = `${icon(kind === "bad" ? "alert" : "check")}<span>${esc(message)}</span>`;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "bad" ? 6000 : 3000);
  }

  let modalSeq = 0;
  function openModal({ title, body, actions = [{ label: "Закрыть" }], onOpen, onClose }) {
    const dlg = $("#modal");
    const token = ++modalSeq;
    if (dlg.open) dlg.close();
    dlg.innerHTML = `<div class="modal-head">${esc(title)}</div><div class="modal-body"></div><div class="modal-foot"></div>`;
    const bodyEl = $(".modal-body", dlg);
    if (typeof body === "string") bodyEl.innerHTML = body;
    else bodyEl.appendChild(body);
    const foot = $(".modal-foot", dlg);
    for (const a of actions) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `btn ${a.kind || ""}`;
      b.textContent = a.label;
      b.addEventListener("click", async () => {
        if (!a.onClick) {
          dlg.close();
          return;
        }
        const label = b.innerHTML;
        b.disabled = true;
        b.innerHTML = `${icon("refresh", "spin")}${esc(a.label)}`;
        try {
          if ((await a.onClick(dlg)) !== false) dlg.close();
        } catch (err) {
          toast(err.message || String(err), "bad");
        } finally {
          b.disabled = false;
          b.innerHTML = label;
        }
      });
      foot.appendChild(b);
    }
    dlg.addEventListener(
      "close",
      () => {
        $$("audio", dlg).forEach((el) => el.pause());
        if (onClose) onClose();
        if (token === modalSeq) {
          dlg.innerHTML = "";
          state.modalOpen = false;
        }
      },
      { once: true }
    );
    dlg.showModal();
    state.modalOpen = true;
    if (onOpen) onOpen(dlg);
    const first = $("input:not([type=hidden]):not([disabled]):not([type=checkbox]), select", dlg);
    if (first) first.focus();
  }

  function confirmDialog(title, text, okLabel = "Удалить") {
    return new Promise((resolve) => {
      let ok = false;
      openModal({
        title,
        body: `<div>${esc(text)}</div>`,
        actions: [
          { label: "Отмена" },
          { label: okLabel, kind: "danger solid", onClick: () => { ok = true; } },
        ],
        onClose: () => resolve(ok),
      });
    });
  }

  function download(name, content, type) {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast(`Скопировано: ${text}`);
  }

  /* ---------------- audio ---------------- */
  function encodeWav(samples, rate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buffer);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF");
    v.setUint32(4, 36 + samples.length * 2, true);
    w(8, "WAVE");
    w(12, "fmt ");
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);
    v.setUint16(22, 1, true);
    v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    w(36, "data");
    v.setUint32(40, samples.length * 2, true);
    let o = 44;
    for (let i = 0; i < samples.length; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buffer;
  }

  async function decodeToWav16k(blob) {
    const buf = await blob.arrayBuffer();
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    let decoded;
    try {
      decoded = await ctx.decodeAudioData(buf);
    } finally {
      if (ctx.close) ctx.close();
    }
    const rate = 16000;
    const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
    const src = off.createBufferSource();
    src.buffer = decoded;
    src.connect(off.destination);
    src.start();
    const rendered = await off.startRendering();
    return { wav: encodeWav(rendered.getChannelData(0), rate), duration: decoded.duration };
  }

  async function recordAudio(seconds, onLevel, onTick) {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const data = new Float32Array(analyser.fftSize);
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    const stopped = new Promise((resolve) => { recorder.onstop = resolve; });
    recorder.start();
    const t0 = performance.now();
    let raf = 0;
    const loop = () => {
      analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (const x of data) sum += x * x;
      onLevel(Math.sqrt(sum / data.length));
      const left = seconds - (performance.now() - t0) / 1000;
      onTick(Math.max(0, left));
      if (left > 0) raf = requestAnimationFrame(loop);
      else recorder.stop();
    };
    loop();
    await stopped;
    cancelAnimationFrame(raf);
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    return new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
  }

  function createAudioInput({ seconds = 8, onChange } = {}) {
    const el = document.createElement("div");
    el.className = "ain";
    el.innerHTML = `
      <div class="row">
        <label class="btn">${icon("upload")}Загрузить файл<input type="file" accept="audio/*,.wav,.mp3,.m4a,.ogg,.opus,.webm,.aac,.flac" hidden></label>
        <button type="button" class="btn" data-rec ${SECURE ? "" : "disabled"} title="${SECURE ? "" : "Запись в браузере доступна только по HTTPS"}">${icon("mic")}<span>Записать ${seconds} с</span></button>
      </div>
      <div class="meter"><div class="meter-fill"></div></div>
      <div class="ain-status muted"></div>
      <audio controls hidden></audio>`;
    const fileInput = $("input[type=file]", el);
    const recBtn = $("[data-rec]", el);
    const recLabel = $("span", recBtn);
    const fill = $(".meter-fill", el);
    const status = $(".ain-status", el);
    const audioEl = $("audio", el);
    let wav = null;
    let url = null;
    let busy = false;

    const setStatus = (text, cls = "muted") => {
      status.className = `ain-status ${cls}`;
      status.textContent = text;
    };
    setStatus(
      SECURE
        ? "Загрузите аудиофайл или запишите голос с микрофона."
        : "Запись с микрофона браузер разрешает только по HTTPS. Загрузите файл — например, голосовую заметку с телефона (m4a, mp3, ogg, wav)."
    );

    const setResult = (res, label) => {
      wav = res.wav;
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(new Blob([wav], { type: "audio/wav" }));
      audioEl.src = url;
      audioEl.hidden = false;
      const d = res.duration;
      if (d < 2) setStatus(`${label}: ${d.toFixed(1)} с — коротковато, лучше 5–10 с речи`, "warn");
      else setStatus(`${label}: ${d.toFixed(1)} с — готово`, "ok");
      if (onChange) onChange(wav);
    };

    fileInput.addEventListener("change", async () => {
      const f = fileInput.files && fileInput.files[0];
      if (!f) return;
      setStatus("Обработка файла…");
      try {
        setResult(await decodeToWav16k(f), f.name);
      } catch (err) {
        setStatus(`Не удалось прочитать файл: ${err.message || err}`, "warn");
      }
      fileInput.value = "";
    });

    recBtn.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      recBtn.classList.add("recording");
      try {
        const blob = await recordAudio(
          seconds,
          (lvl) => { fill.style.width = `${levelPct(lvl)}%`; },
          (left) => { recLabel.textContent = `Идёт запись… ${Math.ceil(left)} с`; }
        );
        setStatus("Обработка записи…");
        setResult(await decodeToWav16k(blob), "Запись");
      } catch (err) {
        setStatus(`Микрофон недоступен: ${err.message || err}`, "warn");
      } finally {
        busy = false;
        recBtn.classList.remove("recording");
        recLabel.textContent = `Записать ${seconds} с`;
        fill.style.width = "0%";
      }
    });

    return { el, get: () => wav };
  }

  const PHRASES = [
    "Привет! Включи, пожалуйста, свет на кухне и поставь чайник. Через полчаса напомни проверить духовку.",
    "Какая погода будет завтра утром? Если пойдёт дождь, напомни взять зонт и закрыть окна в спальне.",
    "Поставь спокойную музыку в гостиной, сделай потише и выключи телевизор через двадцать минут.",
    "Сегодня отличный день. Давай откроем шторы, включим вентилятор и запланируем прогулку на вечер.",
  ];
  const randomPhrase = () => PHRASES[Math.floor(Math.random() * PHRASES.length)];

  /* ---------------- state ---------------- */
  const state = {
    tab: "speaker",
    status: null,
    statsToday: null,
    statsAll: null,
    recent: [],
    speakers: [],
    settings: null,
    gate: { events: [] },
    firmware: null,
    assistantLog: [],
    agents: null,
    wakeModels: null,
    ha: null,
    diary: { items: [], total: 0, q: "", speaker: "", day: "", seen: new Set(), seeded: false },
    playingId: null,
    modalOpen: false,
  };

  const loadSpeakers = async () => { state.speakers = (await api("/api/speakers")).speakers || []; };
  const loadStatsAll = async () => { state.statsAll = await api("/api/stats"); };
  const loadSettings = async () => { state.settings = await api("/api/settings"); };
  async function loadOverviewData() {
    const [today, recent] = await Promise.all([
      api(`/api/stats?since=${encodeURIComponent(startOfTodayIso())}`),
      api("/api/dialogues?limit=6"),
    ]);
    state.statsToday = today;
    state.recent = recent.utterances || [];
  }

  /* ---------------- header ---------------- */
  const SAT_STATES = {
    idle: ["ok", "Жду ключевое слово"],
    wake: ["ok live", "Услышала ключевое слово"],
    listening: ["ok live", "Слушаю команду"],
    thinking: ["warn live", "Думаю"],
    replying: ["ok live", "Отвечаю"],
    error: ["bad", "Ошибка"],
  };
  const BACKENDS = { ha: "Home Assistant", openai: "LLM (OpenAI-совместимый)", off: "выключен" };
  const satellite = () => (state.status && state.status.satellites && state.status.satellites[0]) || null;

  function renderPills() {
    const el = $("#pills");
    const s = state.status;
    if (!s) {
      el.innerHTML = pill("bad", "Нет связи с сервисом");
      return;
    }
    const sat = satellite();
    let col;
    if (!sat) col = pill("bad", "Колонка не подключена");
    else if (!sat.listening) col = pill("warn", "Колонка: микрофон выключен");
    else {
      const [dot, text] = SAT_STATES[sat.state] || ["ok", sat.state];
      col = pill(dot, `Колонка · ${text}`);
    }
    const stt = s.stt.online ? pill("ok", "GigaAM") : pill("warn live", "GigaAM загружается…");
    let tts;
    if (!s.tts || !s.tts.enabled) tts = pill("", "Kokoro выключен");
    else if (s.tts.online) tts = pill("ok", `Kokoro · ${s.tts.voice || ""}`);
    else tts = pill("warn live", "Kokoro загружается…");
    const a = s.assistant || {};
    const asst = a.backend === "off" ? pill("", "Ассистент выключен") : pill(a.ha_configured || a.backend === "openai" ? "ok" : "warn", `Ассистент · ${BACKENDS[a.backend] || a.backend}`);
    el.innerHTML = col + stt + tts + asst;
    $("#footer").textContent = `HA Voice ${s.version} · работает ${fmtUptime(s.uptime_s)}`;
  }

  /* ---------------- speaker (board) ---------------- */
  function recentItem(u) {
    return `<div class="utt"><div class="utt-time">${fmtTime(u.ts)}</div><div><div class="utt-head">${speakerChip(u.speaker_id, u.speaker_name)}</div><div class="utt-text">${esc(u.text)}</div></div><div></div></div>`;
  }

  const fmtBytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} МБ` : `${Math.round(n / 1024)} КБ`);
  const loadFirmware = async () => { state.firmware = await api("/api/firmware"); };

  function firmwareCard(sat) {
    const fw = state.firmware || { files: {}, ota: {} };
    const f = fw.files || {};
    const fileRow = (kind, label) => {
      const x = f[kind];
      return `<dt>${esc(label)}</dt><dd class="small">${x ? `${fmtBytes(x.size)} · ${x.source === "github" ? "с GitHub" : "загружен вручную"} · ${fmtAgoTs(new Date(x.ts * 1000).toISOString())}` : `<span class="muted">нет</span>`}</dd>
        <dd>${x ? `<a class="btn ghost icon sm" href="${BASE}/api/firmware/${kind}.bin" title="Скачать">${icon("download")}</a>` : ""}</dd>`;
    };
    const job = sat ? (fw.ota || {})[sat.ip] : null;
    let jobLine = "";
    if (job) {
      if (job.status === "uploading") jobLine = `<div class="small warn" style="margin-top:8px">${icon("refresh", "spin")} Прошиваю ${esc(job.ip)}… не выключайте колонку (1–2 мин)</div>`;
      else if (job.status === "done") jobLine = `<div class="small ok" style="margin-top:8px">${icon("check")} Прошито, колонка перезагружается и сама подключится</div>`;
      else jobLine = `<div class="small bad" style="margin-top:8px">${icon("alert")} ${esc(job.error || "ошибка")}</div>`;
    }
    const webSerial = window.isSecureContext && "serial" in navigator;
    return `
      <div class="card">
        <h3>${icon("chip")}Прошивка колонки</h3>
        <p class="card-sub">Первый раз — по USB. Дальше — по Wi‑Fi кнопкой «Обновить по сети», кабель не нужен.</p>
        <dl class="kv">
          ${fileRow("factory", "factory.bin (USB)")}
          ${fileRow("ota", "ota.bin (по сети)")}
        </dl>
        <div class="row" style="margin-top:10px;flex-wrap:wrap">
          <button class="btn" data-fw="fetch">${icon("refresh")}Скачать последнюю</button>
          <label class="btn ghost">${icon("upload")}Свой .bin<input type="file" accept=".bin" data-fw="upload" hidden></label>
          <button class="btn primary" data-fw="ota" ${sat && sat.proto >= 2 && f.ota ? "" : "disabled"} title="${sat ? "" : "Колонка не подключена"}">${icon("upload")}Обновить по сети</button>
        </div>
        ${jobLine}
        <details style="margin-top:12px">
          <summary class="small">Первая прошивка по USB</summary>
          <ol class="hint" style="margin:8px 0 0;padding-left:18px">
            <li>Подключите плату Waveshare кабелем USB‑C к компьютеру.</li>
            ${webSerial ? `<li>Нажмите <b>«Прошить по USB»</b> ниже и выберите порт.</li>` : `<li>Скачайте <b>factory.bin</b>, откройте <a href="https://web.esphome.io" target="_blank" rel="noopener">web.esphome.io</a> в Chrome/Edge → Connect → Install → выберите файл.</li>`}
            <li>После прошивки задайте Wi‑Fi: на том же сайте (Improv) или подключитесь к сети <b>HA-Voice-Setup</b> с телефона.</li>
            <li>Колонка сама найдёт HA Voice (по умолчанию <span class="mono">homeassistant.local</span>). Другой адрес — сущность «Сервер HA Voice» у устройства в HA или веб‑страница платы.</li>
          </ol>
          ${webSerial && f.factory ? `<div style="margin-top:10px"><esp-web-install-button manifest="${BASE}/api/firmware/manifest.json"><button slot="activate" class="btn primary">${icon("chip")}Прошить по USB</button></esp-web-install-button></div>` : ""}
        </details>
      </div>`;
  }

  let espToolsLoaded = false;
  function ensureEspWebTools() {
    if (espToolsLoaded || !(window.isSecureContext && "serial" in navigator)) return;
    espToolsLoaded = true;
    const sc = document.createElement("script");
    sc.type = "module";
    sc.src = "https://unpkg.com/esp-web-tools@10.1.0/dist/web/install-button.js?module";
    document.head.appendChild(sc);
  }

  function renderSpeaker() {
    const v = view("speaker");
    const s = state.status;
    if (!s) {
      v.innerHTML = banner("bad", "Сервис недоступен", "Проверьте, что дополнение запущено, и откройте его журнал.");
      return;
    }
    const sat = satellite();
    const st = state.settings || {};
    const today = state.statsToday || { period: 0, period_unknown: 0 };
    const fresh = sat && sat.last_audio_age_s != null && sat.last_audio_age_s < 5;
    const vad = st.diary_vad_rms;
    const banners = [];
    if (!s.stt.online) banners.push(banner("info", "Модель распознавания загружается", "При первом запуске скачиваются веса GigaAM. Обычно это 1–5 минут."));
    if (!sat)
      banners.push(
        banner(
          "warn",
          "Колонка не подключена",
          `Прошейте Waveshare прошивкой HA Voice (карточка «Прошивка» ниже) и подключите к Wi‑Fi. Она сама подключится к этому серверу на порт <b>${esc(s.ports.pcm)}</b>.`
        )
      );
    else if (sat.proto < 2)
      banners.push(banner("warn", "Старая прошивка (room_diary)", "Дневник работает, но колонка не умеет отвечать голосом и не показывает состояние. Обновите прошивку по USB."));
    if (sat && sat.wake_error) banners.push(banner("bad", "Ключевое слово не загрузилось", esc(sat.wake_error)));
    if (s.diary.dropped > 0) banners.push(banner("warn", "Дневник не успевает", `Пропущено реплик: ${s.diary.dropped}. Процессор не справляется.`));

    let mic = "—";
    if (sat) {
      if (!sat.listening) mic = "Микрофон выключен переключателем «Слушать»";
      else if (!fresh) mic = "Подключено, звука нет";
      else mic = sat.live_rms >= (vad ?? 1) ? "Слышу речь" : "Слышу фон";
    }
    const [dot, stateText] = sat ? SAT_STATES[sat.state] || ["", sat.state] : ["bad", "нет связи"];
    const wakeLabel = (s.wake && s.wake.word) || "alexa";
    const last = s.last_utterance;
    const lastCmd = (s.assistant || {}).last;
    const steps = [
      [s.stt.online, "Модель GigaAM загружена", "Загружается при первом запуске."],
      [!(s.tts && s.tts.enabled) || s.tts.online, "Kokoro готов отвечать голосом", "Скачивается при первом запуске."],
      [!!sat, "Колонка подключена", "Прошивка HA Voice + Wi‑Fi, см. «Прошивка колонки»."],
      [s.speakers > 0, "Записан хотя бы один голос", "Вкладка «Голоса» → «Записать с колонки»."],
      [(s.assistant || {}).backend === "off" || (s.assistant || {}).ha_configured, "Есть доступ к Home Assistant", "На HAOS — автоматически; в compose — адрес и токен в «Настройках»."],
    ];

    v.innerHTML = `
      ${banners.join("")}
      <div class="grid grid-2">
        <div class="card">
          <div class="row between"><h3>${icon("speaker")}${esc(sat ? sat.name : "Колонка")}</h3><span class="pill"><span class="dot ${dot}"></span>${esc(stateText)}</span></div>
          <div class="meter" style="margin-top:8px"><div class="meter-fill" style="width:${fresh ? levelPct(sat.live_rms) : 0}%"></div></div>
          <div class="row between small" style="margin-top:8px">
            <span>${esc(mic)}</span>
            <span class="mono muted">${fresh ? sat.live_rms.toFixed(3) : "—"}${vad != null ? ` / порог ${Number(vad).toFixed(3)}` : ""}</span>
          </div>
          ${sat ? `
          <dl class="kv" style="margin-top:12px">
            <dt>Адрес</dt><dd class="mono">${esc(sat.ip)}</dd><dd><a class="btn ghost icon sm" href="http://${esc(sat.ip)}" target="_blank" rel="noopener" title="Веб-страница платы">${icon("link")}</a></dd>
            <dt>Ключевое слово</dt><dd>${esc(wakeLabel)} · уверенность ${pct(sat.wake_score)}</dd><dd></dd>
            <dt>Прошивка</dt><dd class="small">${sat.proto >= 2 ? `HA Voice · ESPHome ${esc(sat.esphome || "?")}` : "room_diary (старая)"}</dd><dd></dd>
            <dt>На связи</dt><dd>${fmtUptime(sat.connected_s)}</dd><dd></dd>
          </dl>
          <div class="field" style="margin-top:12px">
            <label for="spVol">Громкость</label>
            <div class="range-row"><input type="range" id="spVol" min="0" max="100" step="5" value="${st.sat_volume ?? 60}"><output>${st.sat_volume ?? 60}%</output></div>
          </div>
          <div class="row" style="flex-wrap:wrap">
            <input class="input" id="spSay" placeholder="Сказать на колонке…" maxlength="400" style="flex:1;min-width:180px">
            <button class="btn primary" id="spSayBtn" ${sat.can_speak ? "" : "disabled"}>${icon("speaker")}Сказать</button>
            <button class="btn" id="spListen" title="Как будто сказали ключевое слово">${icon("mic")}Слушать команду</button>
          </div>` : ""}
        </div>
        ${firmwareCard(sat)}
      </div>
      <div class="grid grid-4" style="margin-top:14px">
        ${statCard("Голосов записано", s.speakers, s.speakers ? "узнаются по голосу" : "пока никого")}
        ${statCard("Реплик сегодня", today.period, `из них неизвестных: ${today.period_unknown}`)}
        ${statCard("Пустых распознаваний", s.diary.stt_empty || 0, "речь без текста, см. журнал")}
        ${statCard("Ассистент", BACKENDS[(s.assistant || {}).backend] || "—", "меняется во вкладке «Ассистент»")}
      </div>
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card">
          <h3>${icon("check")}Первые шаги</h3>
          <ul class="checklist">
            ${steps.map(([done, title, hint]) => `<li><span class="check ${done ? "done" : ""}">${icon("check")}</span><div><div>${esc(title)}</div>${done ? "" : `<div class="small muted">${esc(hint)}</div>`}</div></li>`).join("")}
          </ul>
          ${lastCmd ? `<div class="stat-label" style="margin-top:12px">Последняя команда</div><div style="margin-top:4px">«${esc(lastCmd.text || "")}»${lastCmd.reply ? ` → ${esc(lastCmd.reply)}` : ""}</div>` : ""}
          ${last ? `<div class="stat-label" style="margin-top:12px">Последняя реплика</div><div class="utt-head" style="margin-top:6px">${speakerChip(last.speaker_id, last.speaker_name)}<span class="utt-meta">${fmtAgoTs(last.ts)}</span></div><div>${esc(last.text)}</div>` : ""}
        </div>
        <div class="card">
          <div class="row between"><h3>${icon("book")}Дневник</h3><button class="btn sm ghost" data-goto="diary">Весь дневник →</button></div>
          ${state.recent.length ? state.recent.map(recentItem).join("") : emptyBox("book", "Реплик пока нет. Поговорите рядом с колонкой.")}
        </div>
      </div>`;
    ensureEspWebTools();
  }

  async function onSpeakerClick(ev) {
    const t = ev.target;
    const fw = t.closest("[data-fw]");
    try {
      if (t.closest("#spSayBtn")) {
        const text = $("#spSay").value.trim();
        if (!text) return;
        await postJson("/api/satellites/say", { text });
        toast("Говорю на колонке");
      } else if (t.closest("#spListen")) {
        await postJson("/api/satellites/listen", {});
        toast("Колонка слушает команду");
      } else if (fw && fw.dataset.fw === "fetch") {
        fw.disabled = true;
        fw.innerHTML = `${icon("refresh", "spin")}Скачиваю…`;
        state.firmware = await postJson("/api/firmware/fetch", {});
        toast("Прошивка скачана");
        renderSpeaker();
      } else if (fw && fw.dataset.fw === "ota") {
        const sat = satellite();
        if (!(await confirmDialog("Обновить прошивку по сети?", `Колонка ${sat ? sat.ip : ""} перезагрузится примерно через минуту.`, "Обновить"))) return;
        await postJson("/api/firmware/ota", { id: sat && sat.id });
        await loadFirmware();
        renderSpeaker();
      }
    } catch (err) {
      showError(err);
      if (fw && fw.dataset.fw === "fetch") renderSpeaker();
    }
  }

  async function onSpeakerChange(ev) {
    const t = ev.target;
    try {
      if (t.id === "spVol") {
        state.settings = await postJson("/api/settings", { sat_volume: Number(t.value) });
        toast(`Громкость ${t.value}%`);
      } else if (t.dataset.fw === "upload" && t.files && t.files[0]) {
        const file = t.files[0];
        const kind = /factory/i.test(file.name) ? "factory" : "ota";
        state.firmware = await api(`/api/firmware/upload/${kind}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
        toast(`${kind}.bin загружен`);
        renderSpeaker();
      }
    } catch (err) {
      showError(err);
    }
  }

  /* ---------------- diary ---------------- */
  function ensureDiaryShell() {
    const v = view("diary");
    if (v.dataset.ready) return;
    v.dataset.ready = "1";
    v.innerHTML = `
      <div class="toolbar">
        <label class="switch" title="Записывать и расшифровывать речь"><input type="checkbox" id="dEnabled"><span class="track"></span>Запись</label>
        <div class="input-icon">${icon("search")}<input class="input" id="dQ" placeholder="Поиск по тексту" autocomplete="off"></div>
        <select class="input" id="dSpeaker" title="Говорящий"></select>
        <input class="input" type="date" id="dDay" title="День">
        <button class="btn icon" id="dRefresh" title="Обновить">${icon("refresh")}</button>
        <button class="btn" id="dExportTxt" title="Скачать как текст">${icon("download")}TXT</button>
        <button class="btn" id="dExportJson" title="Скачать как JSON">${icon("download")}JSON</button>
      </div>
      <div class="row between small muted" id="dSummary" style="margin-bottom:6px"></div>
      <div id="dList"></div>
      <div class="row" style="justify-content:center;margin-top:12px"><button class="btn" id="dMore" hidden>Показать ещё</button></div>`;

    let qTimer = 0;
    $("#dQ").addEventListener("input", (e) => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => {
        state.diary.q = e.target.value.trim();
        loadDiary().catch(showError);
      }, 300);
    });
    $("#dSpeaker").addEventListener("change", (e) => {
      state.diary.speaker = e.target.value;
      loadDiary().catch(showError);
    });
    $("#dDay").addEventListener("change", (e) => {
      state.diary.day = e.target.value;
      loadDiary().catch(showError);
    });
    $("#dRefresh").addEventListener("click", () => loadDiary().catch(showError));
    $("#dMore").addEventListener("click", () => loadDiary({ append: true }).catch(showError));
    $("#dExportTxt").addEventListener("click", () => exportDiary("txt").catch(showError));
    $("#dExportJson").addEventListener("click", () => exportDiary("json").catch(showError));
    $("#dEnabled").addEventListener("change", async (e) => {
      try {
        const res = await postJson("/api/diary", { enabled: e.target.checked });
        toast(res.enabled ? "Дневник включён" : "Дневник выключен");
        if (state.status) state.status.diary.enabled = res.enabled;
        renderPills();
      } catch (err) {
        e.target.checked = !e.target.checked;
        showError(err);
      }
    });
    $("#dList").addEventListener("click", onDiaryClick);
  }

  function syncDiaryControls() {
    const sel = $("#dSpeaker");
    const current = state.diary.speaker;
    sel.innerHTML =
      `<option value="">Все говорящие</option><option value="__unknown__">Неизвестные</option>` +
      state.speakers.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
    sel.value = current;
    if (state.status) $("#dEnabled").checked = !!state.status.diary.enabled;
  }

  function diaryParams(extra = {}) {
    const d = state.diary;
    const params = new URLSearchParams(extra);
    if (d.q) params.set("q", d.q);
    if (d.speaker) params.set("speaker", d.speaker);
    if (d.day) {
      const [since, until] = dayRange(d.day);
      params.set("since", since);
      params.set("until", until);
    }
    return params;
  }

  async function loadDiary({ append = false } = {}) {
    const d = state.diary;
    const res = await api(`/api/dialogues?${diaryParams({ limit: PAGE, offset: append ? d.items.length : 0 })}`);
    d.items = append ? d.items.concat(res.utterances) : res.utterances;
    d.total = res.total;
    renderDiaryList();
  }

  function uttHtml(u, fresh) {
    const playing = state.playingId === u.id;
    const meta = [fmtDur(u.duration_ms)];
    if (u.speaker_id && u.score != null) meta.unshift(`сходство ${pct(u.score)}`);
    const assign = u.speaker_id
      ? `<button class="btn ghost icon" data-act="assign" title="Изменить говорящего">${icon("userPlus")}</button>`
      : `<button class="btn sm" data-act="assign" title="Указать, кто говорит">${icon("userPlus")}Кто это?</button>`;
    return `
      <div class="utt ${fresh ? "fresh" : ""}" data-id="${u.id}">
        <div class="utt-time">${fmtTime(u.ts)}</div>
        <div>
          <div class="utt-head">${speakerChip(u.speaker_id, u.speaker_name)}<span class="utt-meta">${meta.join(" · ")}</span></div>
          <div class="utt-text">${esc(u.text)}</div>
        </div>
        <div class="utt-actions">
          ${u.has_audio ? `<button class="btn ghost icon ${playing ? "playing" : ""}" data-act="play" title="Прослушать">${icon(playing ? "stop" : "play")}</button>` : ""}
          ${assign}
          <button class="btn ghost icon danger" data-act="delete" title="Удалить">${icon("trash")}</button>
        </div>
      </div>`;
  }

  function renderDiaryList() {
    const d = state.diary;
    const list = $("#dList");
    if (!list) return;
    const filtered = d.q || d.speaker || d.day;
    $("#dSummary").innerHTML = `<span>${filtered ? "Найдено" : "Всего"}: ${d.total} ${plural(d.total, "реплика", "реплики", "реплик")}</span>${
      state.status && state.status.diary.pcm_clients ? `<span class="row"><span class="dot ok live"></span>обновляется автоматически</span>` : ""
    }`;
    if (!d.items.length) {
      list.innerHTML = emptyBox(
        "book",
        filtered ? "Ничего не найдено — измените фильтры." : "Реплик пока нет. Поговорите рядом с колонкой."
      );
    } else {
      let html = "";
      let lastDay = "";
      for (const u of d.items) {
        const key = dayKey(u.ts);
        if (key !== lastDay) {
          html += `<div class="day-head">${esc(dayLabel(u.ts))}</div>`;
          lastDay = key;
        }
        html += uttHtml(u, d.seeded && !d.seen.has(u.id));
      }
      list.innerHTML = html;
    }
    d.items.forEach((u) => d.seen.add(u.id));
    d.seeded = true;
    $("#dMore").hidden = d.items.length >= d.total;
  }

  const player = new Audio();
  player.addEventListener("ended", () => {
    state.playingId = null;
    refreshPlayButtons();
  });
  function togglePlay(id) {
    if (state.playingId === id) {
      player.pause();
      state.playingId = null;
    } else {
      player.src = `${BASE}/api/dialogues/${id}/audio`;
      player.play().catch((err) => {
        state.playingId = null;
        refreshPlayButtons();
        showError(err);
      });
      state.playingId = id;
    }
    refreshPlayButtons();
  }
  function refreshPlayButtons() {
    $$('[data-act="play"]').forEach((b) => {
      const on = Number(b.closest("[data-id]").dataset.id) === state.playingId;
      b.classList.toggle("playing", on);
      b.innerHTML = icon(on ? "stop" : "play");
    });
  }

  async function onDiaryClick(ev) {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    const id = Number(btn.closest("[data-id]").dataset.id);
    const u = state.diary.items.find((x) => x.id === id);
    if (!u) return;
    const act = btn.dataset.act;
    if (act === "play") togglePlay(id);
    else if (act === "assign") openAssign(u);
    else if (act === "delete") {
      if (!(await confirmDialog("Удалить реплику?", `«${u.text}»`))) return;
      try {
        await api(`/api/dialogues/${id}`, { method: "DELETE" });
        state.diary.items = state.diary.items.filter((x) => x.id !== id);
        state.diary.total = Math.max(0, state.diary.total - 1);
        renderDiaryList();
        toast("Реплика удалена");
      } catch (err) {
        showError(err);
      }
    }
  }

  function openAssign(u) {
    const hasSpeakers = state.speakers.length > 0;
    const options =
      state.speakers
        .map((s) => `<option value="${esc(s.id)}" ${s.id === u.speaker_id ? "selected" : ""}>${esc(s.name)}</option>`)
        .join("") +
      `<option value="__new__" ${hasSpeakers ? "" : "selected"}>+ Новый голос…</option>` +
      (u.speaker_id ? `<option value="__none__">Неизвестный</option>` : "");
    const body = `
      <div class="phrase">«${esc(u.text)}»</div>
      ${u.has_audio ? `<audio controls src="${BASE}/api/dialogues/${u.id}/audio" style="width:100%"></audio>` : `<div class="small muted">Аудио этой реплики не сохранено — можно только поменять подпись.</div>`}
      <div class="field" style="margin:0"><label for="aSel">Кто говорит</label><select class="input" id="aSel">${options}</select></div>
      <div class="field" id="aNewWrap" style="margin:0"><label for="aName">Имя нового голоса</label><input class="input" id="aName" maxlength="40" placeholder="Например: Папа, Маша"></div>
      <label class="switch"><input type="checkbox" id="aLearn" ${u.has_audio ? "checked" : "disabled"}><span class="track"></span>Запомнить голос по этой фразе</label>`;

    openModal({
      title: "Кто это сказал?",
      body,
      onOpen: (dlg) => {
        const sel = $("#aSel", dlg);
        const sync = () => {
          $("#aNewWrap", dlg).hidden = sel.value !== "__new__";
          $("#aLearn", dlg).closest(".switch").hidden = sel.value === "__none__";
        };
        sel.addEventListener("change", sync);
        sync();
      },
      actions: [
        { label: "Отмена" },
        {
          label: "Сохранить",
          kind: "primary",
          onClick: async (dlg) => {
            const choice = $("#aSel", dlg).value;
            const learn = $("#aLearn", dlg).checked;
            let res;
            if (choice === "__none__") {
              res = await postJson(`/api/dialogues/${u.id}/unassign`, {});
            } else if (choice === "__new__") {
              const name = $("#aName", dlg).value.trim();
              if (!name) throw new Error("Введите имя");
              res = await postJson(`/api/dialogues/${u.id}/assign`, { name, enroll: true });
            } else {
              res = await postJson(`/api/dialogues/${u.id}/assign`, { speaker_id: choice, enroll: learn });
            }
            const idx = state.diary.items.findIndex((x) => x.id === u.id);
            if (idx >= 0 && res.utterance) state.diary.items[idx] = res.utterance;
            await loadSpeakers();
            syncDiaryControls();
            renderDiaryList();
            toast(res.learned ? "Сохранено, голос дообучен" : "Сохранено");
          },
        },
      ],
    });
  }

  async function exportDiary(format) {
    const res = await api(`/api/dialogues?${diaryParams({ limit: 5000, order: "asc" })}`);
    const rows = res.utterances || [];
    if (!rows.length) throw new Error("Нечего выгружать");
    const stamp = new Date().toISOString().slice(0, 10);
    if (format === "json") {
      download(`ha-voice-${stamp}.json`, JSON.stringify(rows, null, 2), "application/json");
      return;
    }
    let lastDay = "";
    const lines = [];
    for (const u of rows) {
      const key = dayKey(u.ts);
      if (key !== lastDay) {
        if (lines.length) lines.push("");
        lines.push(`=== ${parseTs(u.ts).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" })} ===`);
        lastDay = key;
      }
      lines.push(`[${fmtTime(u.ts)}] ${u.speaker_id ? u.speaker_name : "Неизвестный"}: ${u.text}`);
    }
    download(`ha-voice-${stamp}.txt`, lines.join("\n") + "\n", "text/plain;charset=utf-8");
  }

  /* ---------------- voices ---------------- */
  function renderVoices() {
    const v = $("#voicesMain");
    const counts = Object.fromEntries(((state.statsAll && state.statsAll.by_speaker) || []).map((x) => [x.speaker_id, x]));
    const cards = state.speakers
      .map((s) => {
        const c = counts[s.id];
        const enough = s.samples >= 3 || s.seconds >= 20;
        return `
          <div class="card voice-card" data-sid="${esc(s.id)}">
            <div class="voice-head">
              <div class="avatar" style="--h:${hue(s.id)}">${esc(initial(s.name))}</div>
              <div>
                <div class="voice-name">${esc(s.name)}</div>
                <div class="voice-meta">${s.samples} ${plural(s.samples, "образец", "образца", "образцов")} · ${s.seconds} с речи</div>
              </div>
            </div>
            <div class="small muted">${c ? `${c.count} ${plural(c.count, "реплика", "реплики", "реплик")} в дневнике · последняя ${fmtAgoTs(c.last_ts)}` : "В дневнике пока нет реплик"}</div>
            <div>${enough ? badge("ok", "check", "Образцов достаточно") : badge("warn", "alert", "Мало образцов — добавьте ещё 2–3")}</div>
            <div class="voice-actions">
              <button class="btn sm" data-act="sample">${icon("mic")}Ещё с колонки</button>
              <button class="btn sm ghost" data-act="rename">${icon("edit")}Переименовать</button>
              <span class="spacer"></span>
              <button class="btn sm ghost icon danger" data-act="delete" title="Удалить голос">${icon("trash")}</button>
            </div>
          </div>`;
      })
      .join("");

    const espOk = state.status && state.status.diary.pcm_clients > 0;
    v.innerHTML = `
      ${banner(
        espOk ? "info" : "warn",
        espOk ? "Лучший способ — запись с колонки" : "Колонка не подключена",
        espOk
          ? "Нажмите «Записать с колонки» — 8 секунд тем же микрофоном, что слышит команды. После записи можно прослушать и сохранить."
          : "Подключите колонку (вкладка «Колонка»). Пока можно загрузить файл или записать в браузере."
      )}
      <div class="row" style="margin-bottom:14px">
        <button class="btn primary" data-act="esp"${espOk ? "" : " disabled"}>${icon("mic")}Записать с колонки</button>
        <button class="btn" data-act="add">${icon("upload")}Файл / браузер</button>
      </div>
      <div class="grid grid-2">
        ${cards || emptyBox("user", "Голосов пока нет — запишите первый с колонки.")}
      </div>`;
  }

  async function onVoicesClick(ev) {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "add") return openAddVoice();
    if (act === "esp") return openEspCapture();
    const card = btn.closest("[data-sid]");
    const sp = state.speakers.find((s) => s.id === (card && card.dataset.sid));
    if (!sp) return;
    if (act === "sample") openEspCapture(sp);
    else if (act === "rename") openRename(sp);
    else if (act === "delete") {
      if (!(await confirmDialog(`Удалить голос «${sp.name}»?`, "Реплики в дневнике останутся, но станут «Неизвестный»."))) return;
      try {
        await api(`/api/speakers/${encodeURIComponent(sp.id)}`, { method: "DELETE" });
        toast("Голос удалён");
        await refreshVoices();
      } catch (err) {
        showError(err);
      }
    }
  }

  function openEspCapture(existing) {
    const seconds = 8;
    const body = document.createElement("div");
    body.className = "stack";
    body.innerHTML = `
      <div class="small muted">Встаньте у колонки и говорите обычным голосом. Можно прочитать:</div>
      <div class="phrase">${esc(randomPhrase())}</div>
      ${existing ? "" : `<div class="field" style="margin:0"><label for="vName">Имя</label><input class="input" id="vName" maxlength="40" placeholder="Например: Папа"></div>`}
      <div class="meter"><div class="meter-fill" id="cFill"></div></div>
      <div class="ain-status muted" id="cStatus">Нажмите «Начать запись»</div>
      <audio id="cAudio" controls hidden style="width:100%"></audio>
      <div class="row">
        <button type="button" class="btn primary" id="cStart">${icon("mic")}Начать запись (${seconds} с)</button>
        <button type="button" class="btn" id="cStop" hidden>Стоп</button>
        <button type="button" class="btn ghost" id="cAgain" hidden>${icon("refresh")}Ещё раз</button>
      </div>`;

    let timer = 0;
    let audioUrl = null;
    let ready = false;

    const cleanup = () => {
      clearInterval(timer);
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      api("/api/capture", { method: "DELETE" }).catch(() => {});
    };

    const setStatus = (text, cls = "muted") => {
      const el = $("#cStatus", body);
      el.className = `ain-status ${cls}`;
      el.textContent = text;
    };

    const showResult = async () => {
      ready = false;
      const st = await api("/api/capture");
      const fill = $("#cFill", body);
      fill.style.width = `${levelPct(st.live_rms)}%`;
      if (st.active) {
        setStatus(`Идёт запись… ${Math.ceil(st.remaining_s)} с · говорите`, "warn");
        return false;
      }
      if (!st.ready) {
        setStatus("Слишком мало звука. Подойдите ближе и запишите снова.", "warn");
        $("#cStart", body).hidden = false;
        $("#cStop", body).hidden = true;
        $("#cAgain", body).hidden = true;
        return false;
      }
      ready = true;
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      const res = await fetch(`${BASE}/api/capture/audio`, { cache: "no-store", credentials: "same-origin" });
      if (!res.ok) throw new Error("Не удалось получить запись");
      audioUrl = URL.createObjectURL(await res.blob());
      const audio = $("#cAudio", body);
      audio.src = audioUrl;
      audio.hidden = false;
      setStatus(`Готово: ${(st.duration_ms / 1000).toFixed(1)} с — прослушайте и сохраните`, "ok");
      $("#cStart", body).hidden = true;
      $("#cStop", body).hidden = true;
      $("#cAgain", body).hidden = false;
      return true;
    };

    const poll = () => {
      clearInterval(timer);
      timer = setInterval(async () => {
        try {
          if (await showResult()) clearInterval(timer);
        } catch (err) {
          clearInterval(timer);
          setStatus(err.message || String(err), "warn");
        }
      }, 400);
    };

    openModal({
      title: existing ? `Образец с колонки: ${existing.name}` : "Запись голоса с колонки",
      body,
      onOpen: (dlg) => {
        $("#cStart", dlg).addEventListener("click", async () => {
          try {
            ready = false;
            $("#cAudio", dlg).hidden = true;
            await postJson("/api/capture/start", { seconds });
            $("#cStart", dlg).hidden = true;
            $("#cStop", dlg).hidden = false;
            $("#cAgain", dlg).hidden = true;
            setStatus(`Идёт запись… ${seconds} с · говорите`, "warn");
            poll();
          } catch (err) {
            showError(err);
          }
        });
        $("#cStop", dlg).addEventListener("click", async () => {
          try {
            await postJson("/api/capture/stop", {});
            await showResult();
            clearInterval(timer);
          } catch (err) {
            showError(err);
          }
        });
        $("#cAgain", dlg).addEventListener("click", () => {
          ready = false;
          $("#cAudio", dlg).hidden = true;
          $("#cStart", dlg).hidden = false;
          $("#cAgain", dlg).hidden = true;
          setStatus("Нажмите «Начать запись»");
        });
      },
      onClose: cleanup,
      actions: [
        { label: "Отмена" },
        {
          label: "Сохранить голос",
          kind: "primary",
          onClick: async (dlg) => {
            if (!ready) throw new Error("Сначала сделайте запись и прослушайте");
            const payload = existing
              ? { speaker_id: existing.id }
              : { name: $("#vName", dlg).value.trim() };
            if (!existing && !payload.name) throw new Error("Введите имя");
            await postJson("/api/capture/enroll", payload);
            toast(existing ? "Образец добавлен" : `Голос «${payload.name}» сохранён`);
            await refreshVoices();
          },
        },
      ],
    });
  }

  async function refreshVoices() {
    await Promise.all([loadSpeakers(), loadStatsAll()]);
    renderVoices();
  }

  function audioForm({ withName }) {
    const ain = createAudioInput({ seconds: 8 });
    const body = document.createElement("div");
    body.className = "stack";
    body.innerHTML = `
      ${withName ? `<div class="field" style="margin:0"><label for="vName">Имя</label><input class="input" id="vName" maxlength="40" placeholder="Например: Папа"></div>` : ""}
      <div class="small muted">Говорите 5–10 секунд обычным голосом. Можно прочитать:</div>
      <div class="phrase">${esc(randomPhrase())}</div>`;
    body.appendChild(ain.el);
    return { ain, body };
  }

  function openAddVoice() {
    const { ain, body } = audioForm({ withName: true });
    openModal({
      title: "Новый голос",
      body,
      actions: [
        { label: "Отмена" },
        {
          label: "Сохранить",
          kind: "primary",
          onClick: async (dlg) => {
            const name = $("#vName", dlg).value.trim();
            if (!name) throw new Error("Введите имя");
            const wav = ain.get();
            if (!wav) throw new Error("Добавьте запись голоса");
            await sendAudio(`/api/speakers?name=${encodeURIComponent(name)}`, wav);
            toast(`Голос «${name}» сохранён`);
            await refreshVoices();
          },
        },
      ],
    });
  }

  function openAddSample(sp) {
    const { ain, body } = audioForm({ withName: false });
    openModal({
      title: `Ещё образец: ${sp.name}`,
      body,
      actions: [
        { label: "Отмена" },
        {
          label: "Добавить",
          kind: "primary",
          onClick: async () => {
            const wav = ain.get();
            if (!wav) throw new Error("Добавьте запись голоса");
            await sendAudio(`/api/speakers/${encodeURIComponent(sp.id)}/samples`, wav);
            toast("Образец добавлен");
            await refreshVoices();
          },
        },
      ],
    });
  }

  function openRename(sp) {
    openModal({
      title: "Переименовать",
      body: `<div class="field" style="margin:0"><label for="rName">Имя</label><input class="input" id="rName" maxlength="40" value="${esc(sp.name)}"></div>`,
      actions: [
        { label: "Отмена" },
        {
          label: "Сохранить",
          kind: "primary",
          onClick: async (dlg) => {
            const name = $("#rName", dlg).value.trim();
            if (!name) throw new Error("Введите имя");
            await postJson(`/api/speakers/${encodeURIComponent(sp.id)}`, { name }, "PATCH");
            toast("Переименовано");
            await refreshVoices();
          },
        },
      ],
    });
  }

  /* ---------------- test ---------------- */
  function ensureTest() {
    const v = $("#voicesTest");
    if (v.dataset.ready) return;
    v.dataset.ready = "1";
    v.innerHTML = `
      <div class="grid grid-2" style="margin-top:14px">
        <div class="card">
          <h3>${icon("flask")}Проверить запись</h3>
          <p class="card-sub">Загрузите или запишите фразу — покажу, что распознает GigaAM и узнает ли голос. Чтобы проверить ТВ, загрузите кусок передачи.</p>
          <div id="tIn"></div>
          <div class="row" style="margin-top:12px"><button class="btn primary" id="tRun" disabled>${icon("flask")}Проверить</button></div>
        </div>
        <div class="card" id="tOut"><h3>Результат</h3>${emptyBox("flask", "Здесь появится результат проверки")}</div>
      </div>`;
    const run = $("#tRun");
    const ain = createAudioInput({ seconds: 6, onChange: () => { run.disabled = false; } });
    $("#tIn").appendChild(ain.el);
    run.addEventListener("click", async () => {
      const wav = ain.get();
      if (!wav) return;
      run.disabled = true;
      const label = run.innerHTML;
      run.innerHTML = `${icon("refresh", "spin")}Распознаю…`;
      try {
        renderTestResult(await sendAudio("/api/test", wav));
      } catch (err) {
        showError(err);
      } finally {
        run.disabled = false;
        run.innerHTML = label;
      }
    });
  }

  function renderTestResult(res) {
    const th = res.threshold;
    const clamp = (x) => Math.max(0, Math.min(1, Number(x) || 0));
    const scores = (res.scores || [])
      .map(
        (sc) => `
        <div>
          <div class="row between small"><span>${esc(sc.name)}</span><b>${pct(sc.score)}</b></div>
          <div class="bar"><div class="bar-fill ${sc.score >= th ? "ok" : ""}" style="width:${clamp(sc.score) * 100}%"></div><div class="bar-mark" style="left:${th * 100}%" title="Порог"></div></div>
        </div>`
      )
      .join("");
    $("#tOut").innerHTML = `
      <h3>Результат</h3>
      <div class="field"><label>Распознанный текст</label><div class="phrase">${res.text ? esc(res.text) : "— ничего не распознано"}</div></div>
      <div class="row">
        ${res.assist_accepted ? badge("ok", "check", "Команда была бы выполнена") : badge("bad", "x", "Команда была бы отклонена")}
        <span class="small muted">${fmtDur(res.duration_ms)} · порог ${pct(th)}</span>
      </div>
      <div class="stack" style="margin-top:14px">
        ${res.best && res.best.matched ? `<div>Похоже на: ${speakerChip(res.best.id, res.best.name)}</div>` : ""}
        ${scores || `<div class="small muted">Голоса не записаны — проверка по голосу не выполняется.</div>`}
      </div>`;
  }

  /* ---------------- shared form helpers ---------------- */
  const FMT = {
    pct: (x) => `${Math.round(x * 100)}%`,
    rms: (x) => Number(x).toFixed(3),
    ms: (x) => `${x} мс`,
    sec: (x) => `${Math.round(x / 1000)} с`,
    vol: (x) => `${x}%`,
  };
  const switchField = (name, label, checked, hint) =>
    `<div class="field"><label class="switch"><input type="checkbox" name="${name}" ${checked ? "checked" : ""}><span class="track"></span>${esc(label)}</label>${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div>`;
  const rangeField = (name, label, value, min, max, step, fmt, hint, scale) => `
    <div class="field">
      <label for="f_${name}">${esc(label)}</label>
      <div class="range-row"><input type="range" id="f_${name}" name="${name}" min="${min}" max="${max}" step="${step}" value="${value}" data-fmt="${fmt}"><output>${FMT[fmt](value)}</output></div>
      ${scale ? `<div class="range-scale"><span>${esc(scale[0])}</span><span>${esc(scale[1])}</span></div>` : ""}
      ${hint ? `<div class="hint">${esc(hint)}</div>` : ""}
    </div>`;
  const textField = (name, label, value, hint, attrs = "") => `
    <div class="field">
      <label for="f_${name}">${esc(label)}</label>
      <input class="input" id="f_${name}" name="${name}" value="${esc(value ?? "")}" autocomplete="off" ${attrs}>
      ${hint ? `<div class="hint">${hint}</div>` : ""}
    </div>`;
  const selectField = (name, label, value, options, hint) => `
    <div class="field">
      <label for="f_${name}">${esc(label)}</label>
      <select class="input" id="f_${name}" name="${name}">${options
        .map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(l)}</option>`)
        .join("")}</select>
      ${hint ? `<div class="hint">${hint}</div>` : ""}
    </div>`;

  // Every settings form posts only its own fields; the server merges them.
  function bindSettingsForm(form, after) {
    form.addEventListener("input", (e) => {
      if (e.target.type === "range" && e.target.dataset.fmt) e.target.nextElementSibling.textContent = FMT[e.target.dataset.fmt](Number(e.target.value));
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {};
      for (const el of form.elements) {
        if (!el.name) continue;
        if (el.type === "checkbox") payload[el.name] = el.checked;
        else if (el.type === "number" || el.type === "range") payload[el.name] = Number(el.value);
        else payload[el.name] = el.value;
      }
      const btn = $("button[type=submit]", form);
      if (btn) btn.disabled = true;
      try {
        state.settings = await postJson("/api/settings", payload);
        toast("Сохранено");
        await poll();
        if (after) await after();
      } catch (err) {
        showError(err);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  }
  const saveRow = () => `<div class="row" style="margin-top:6px"><span class="spacer"></span><button type="submit" class="btn primary">${icon("check")}Сохранить</button></div>`;

  /* ---------------- tts (Kokoro) ---------------- */
  let ttsAudioUrl = null;
  async function playTts(text, voice) {
    const res = await fetch(BASE + "/api/tts", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice }),
    });
    const ct = res.headers.get("content-type") || "";
    if (!res.ok) {
      const data = ct.includes("json") ? await res.json() : await res.text();
      throw new Error((data && data.error) || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    if (ttsAudioUrl) URL.revokeObjectURL(ttsAudioUrl);
    ttsAudioUrl = URL.createObjectURL(blob);
    await new Audio(ttsAudioUrl).play();
  }

  function renderTts() {
    const v = view("tts");
    const s = state.settings;
    const st = state.status;
    if (!s) {
      v.innerHTML = emptyBox("alert", "Не удалось загрузить настройки");
      return;
    }
    const tts = (st && st.tts) || {};
    const sat = satellite();
    const voices = (tts.voices || [{ id: "sveta", label: "Света" }, { id: "masha", label: "Маша" }, { id: "dima", label: "Дима" }]).map((x) => [
      x.id,
      `${x.label}${x.gender ? (x.gender === "female" ? " · жен." : " · муж.") : ""}`,
    ]);
    const ttsStatus = !tts.enabled ? "выключена в конфигурации дополнения" : tts.online ? "готова" : "загружается";
    v.innerHTML = `
      <div class="grid grid-2">
        <form class="card" id="ttsForm" autocomplete="off">
          <h3>${icon("speaker")}Голос ответов — Kokoro-ru</h3>
          <p class="card-sub">Озвучка ${esc(ttsStatus)}. Ответы ассистента звучат на колонке этим голосом — Piper и карточка «Синтез речи» в Assist не нужны.</p>
          ${selectField("tts_voice", "Голос", s.tts_voice, voices)}
          ${rangeField("sat_volume", "Громкость колонки", s.sat_volume ?? 60, 0, 100, 5, "vol")}
          ${saveRow()}
        </form>
        <div class="card">
          <h3>${icon("play")}Проверка</h3>
          <div class="field">
            <label for="ttsText">Текст</label>
            <input class="input" id="ttsText" value="Привет! Я колонка HA Voice. Чем помочь?" maxlength="400">
          </div>
          <div class="row" style="flex-wrap:wrap">
            <button type="button" class="btn primary" id="ttsBoard" ${sat && sat.can_speak && tts.online ? "" : "disabled"}>${icon("speaker")}На колонке</button>
            <button type="button" class="btn" id="ttsBrowser" ${tts.online ? "" : "disabled"}>${icon("play")}В браузере</button>
          </div>
          <p class="small muted" style="margin-bottom:0">Kokoro также доступен как Wyoming TTS на порту ${esc(st && st.ports ? st.ports.tts : "10351")} — только если хотите использовать его в обычном Assist.</p>
        </div>
      </div>`;
    bindSettingsForm($("#ttsForm"), async () => renderTts());
    const voice = () => $("#f_tts_voice").value;
    $("#ttsBrowser").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        await playTts($("#ttsText").value.trim(), voice());
      } catch (err) {
        showError(err);
      } finally {
        btn.disabled = false;
      }
    });
    $("#ttsBoard").addEventListener("click", async () => {
      try {
        await postJson("/api/satellites/say", { text: $("#ttsText").value.trim(), voice: voice() });
        toast("Говорю на колонке");
      } catch (err) {
        showError(err);
      }
    });
  }

  /* ---------------- assistant ---------------- */
  async function loadAssistant() {
    const jobs = [api("/api/assistant/log?limit=100").then((r) => { state.assistantLog = r.log || []; })];
    if (!state.wakeModels) jobs.push(api("/api/wake/models").then((r) => { state.wakeModels = r.models || []; }));
    if (!state.agents)
      jobs.push(
        api("/api/assistant/agents")
          .then((r) => { state.agents = r.agents || []; })
          .catch(() => { state.agents = [{ id: "", name: "Home Assistant (встроенный)" }]; })
      );
    await Promise.all(jobs);
  }

  function logItem(e) {
    const who = e.speaker ? `<span class="chip" style="--h:${hue(e.speaker)}">${esc(e.speaker)}</span>` : "";
    const b = e.rejected
      ? badge("bad", "x", "Отклонено")
      : e.error && !e.reply
        ? badge("bad", "alert", "Ошибка")
        : (e.actions || []).length
          ? badge("ok", "check", "Выполнено")
          : badge("", "info", "Ответ");
    const actions = (e.actions || [])
      .map((a) => `<span class="mono small">${esc(a.service || a.type || "")} ${esc(Array.isArray(a.target) ? a.target.join(", ") : a.target || "")}</span>`)
      .join(" · ");
    return `
      <div class="event">
        <div class="utt-time">${fmtTime(e.ts)}</div>
        <div>${b}<div class="utt-meta" style="margin-top:4px">${esc(e.satellite === "panel" ? "из панели" : e.satellite || "")}</div></div>
        <div>
          <div class="utt-head">${who}${e.duration_ms ? `<span class="utt-meta">${fmtDur(e.duration_ms)}</span>` : ""}</div>
          <div class="event-text">«${esc(e.text || e.heard || "—")}»</div>
          ${e.reply ? `<div class="small" style="margin-top:4px">${icon("speaker")} ${esc(e.reply)}</div>` : ""}
          ${actions ? `<div style="margin-top:4px">${actions}</div>` : ""}
          ${e.error ? `<div class="small bad" style="margin-top:4px">${esc(e.error)}</div>` : ""}
        </div>
      </div>`;
  }

  function renderAssistantLog() {
    const el = $("#asLog");
    if (el) el.innerHTML = state.assistantLog.length ? state.assistantLog.map(logItem).join("") : emptyBox("robot", `Команд пока не было. Скажите «${esc(((state.status || {}).wake || {}).word || "alexa")}» рядом с колонкой.`);
  }

  function renderAssistant() {
    const v = view("assistant");
    const s = state.settings;
    if (!s) {
      v.innerHTML = emptyBox("alert", "Не удалось загрузить настройки");
      return;
    }
    const wake = (state.wakeModels || []).map((m) => [m.id, `${m.label}${m.kind === "custom" ? " (своё)" : ""}`]);
    const agents = (state.agents || []).map((a) => [a.id, a.name]);
    if (s.ha_agent_id && !agents.some(([id]) => id === s.ha_agent_id)) agents.push([s.ha_agent_id, s.ha_agent_id]);
    const backend = s.assistant_backend;
    v.innerHTML = `
      <div class="grid grid-2">
        <form class="card" id="asForm" autocomplete="off">
          <h3>${icon("robot")}Мозг ассистента</h3>
          <p class="card-sub">Фраза после ключевого слова распознаётся здесь (GigaAM), выполняется в Home Assistant, ответ звучит голосом Kokoro.</p>
          ${selectField("assistant_backend", "Кто отвечает", backend, [
            ["ha", "Агент Home Assistant (встроенные команды или ваш ИИ в HA)"],
            ["openai", "LLM: Ollama / OpenAI-совместимый, с управлением домом"],
            ["off", "Никто — только дневник"],
          ])}
          <div data-show="ha">
            ${selectField("ha_agent_id", "Агент разговора HA", s.ha_agent_id, agents, "Встроенный понимает «включи свет на кухне» без интернета. Агент с ИИ (OpenAI, Ollama, Gemini) настраивается в HA.")}
          </div>
          <div data-show="openai">
            ${textField("llm_url", "Адрес API", s.llm_url, "Ollama на этом ПК: <span class='mono'>http://127.0.0.1:11434/v1</span> (в compose — <span class='mono'>http://host.docker.internal:11434/v1</span>)")}
            ${textField("llm_model", "Модель", s.llm_model, "Нужна модель с tool calling, например qwen2.5:7b или llama3.1:8b.")}
            ${textField("llm_api_key", "API-ключ", s.llm_api_key, "Для Ollama не нужен.", 'type="password"')}
            ${textField("assistant_domains", "Что доступно ассистенту", s.assistant_domains, "Домены через запятую. Управлять можно светом, розетками, шторами и т.п.; датчики — только читать.")}
            <div class="field"><label for="f_llm_prompt">Дополнительные указания</label><textarea class="input" id="f_llm_prompt" name="llm_prompt" rows="3" placeholder="Например: обращайся ко мне на «ты», детскую называй комнатой Маши">${esc(s.llm_prompt || "")}</textarea></div>
          </div>
          ${saveRow()}
        </form>
        <form class="card" id="wwForm" autocomplete="off">
          <h3>${icon("mic")}Ключевое слово</h3>
          <p class="card-sub">openWakeWord работает на ПК и слушает колонку всегда, независимо от дневника.</p>
          ${selectField("wake_word", "Слово", s.wake_word, wake.length ? wake : [[s.wake_word, s.wake_word]], "Своё слово: положите обученную модель <span class='mono'>.onnx</span> в папку <span class='mono'>wakewords</span> дополнения и перезапустите.")}
          ${rangeField("wake_threshold", "Чувствительность", s.wake_threshold, 0.2, 0.9, 0.05, "pct", "Ниже — откликается легче (и чаще ошибается).", ["чутче", "строже"])}
          ${rangeField("command_silence_ms", "Пауза — конец команды", s.command_silence_ms, 300, 2000, 100, "ms")}
          ${selectField("wake_backend", "Где работает", s.wake_backend, [["local", "В этом дополнении (рекомендуется)"], ["wyoming", "Внешний Wyoming openWakeWord"]])}
          <div data-show-wake="wyoming">${textField("wake_uri", "Адрес Wyoming", s.wake_uri, "Аддон openWakeWord на HAOS: <span class='mono'>tcp://core-openwakeword:10400</span>")}</div>
          ${saveRow()}
        </form>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>${icon("flask")}Проверить фразу</h3>
        <div class="row" style="flex-wrap:wrap">
          <input class="input" id="asText" placeholder="включи свет на кухне" maxlength="300" style="flex:1;min-width:200px">
          <label class="switch"><input type="checkbox" id="asSpeak" checked><span class="track"></span>Ответить на колонке</label>
          <button class="btn primary" id="asAsk">${icon("robot")}Спросить</button>
        </div>
        <div id="asResult" class="small" style="margin-top:8px"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <div class="row between"><h3>${icon("shield")}Журнал команд</h3><span class="small muted">фильтр голосов: ${s.gate_require_match ? "чужим отказывать" : "выключен"}</span></div>
        <div id="asLog"></div>
      </div>`;

    const syncShow = () => {
      const b = $("#f_assistant_backend").value;
      $$("[data-show]", v).forEach((el) => { el.hidden = el.dataset.show !== b; });
      $$("[data-show-wake]", v).forEach((el) => { el.hidden = el.dataset.showWake !== $("#f_wake_backend").value; });
    };
    $("#f_assistant_backend").addEventListener("change", syncShow);
    $("#f_wake_backend").addEventListener("change", syncShow);
    syncShow();
    bindSettingsForm($("#asForm"));
    bindSettingsForm($("#wwForm"));
    $("#asAsk").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const text = $("#asText").value.trim();
      if (!text) return;
      btn.disabled = true;
      $("#asResult").innerHTML = `${icon("refresh", "spin")} Думаю…`;
      try {
        const res = await postJson("/api/assistant/ask", { text, speak: $("#asSpeak").checked });
        const r = res.result;
        $("#asResult").innerHTML = r.error && !r.speech ? `<span class="bad">${esc(r.error)}</span>` : `${icon("speaker")} ${esc(r.speech || "(без ответа)")}`;
        state.assistantLog.unshift(res.entry);
        renderAssistantLog();
      } catch (err) {
        $("#asResult").textContent = "";
        showError(err);
      } finally {
        btn.disabled = false;
      }
    });
    renderAssistantLog();
  }

  /* ---------------- settings ---------------- */
  function renderSettings() {
    const v = view("settings");
    const s = state.settings;
    const st = state.status;
    if (!s) {
      v.innerHTML = emptyBox("alert", "Не удалось загрузить настройки");
      return;
    }
    const ha = state.ha;
    const haLine = !ha ? "проверяю…" : ha.ok ? `подключено · Home Assistant ${esc(ha.version || "")}` : esc(ha.error || "нет доступа");
    v.innerHTML = `
      <div class="grid grid-2">
        <form class="card" id="haForm" autocomplete="off">
          <h3>${icon("home")}Home Assistant</h3>
          <p class="card-sub">Только как «дом»: датчики и сервисы для ассистента. Сейчас: <b>${haLine}</b></p>
          ${textField("ha_url", "Адрес", s.ha_url, "Пусто — на HAOS дополнение подключается само. Для docker compose: <span class='mono'>http://homeassistant.local:8123</span>", 'placeholder="автоматически (HAOS)"')}
          ${textField("ha_token", "Долгосрочный токен", s.ha_token, "Профиль HA → Безопасность → Долгосрочные токены доступа.", 'type="password" placeholder="автоматически (HAOS)"')}
          ${saveRow()}
        </form>
        <form class="card" id="gateForm" autocomplete="off">
          <h3>${icon("shield")}Чьи команды выполнять</h3>
          ${switchField("gate_require_match", "Отказывать незнакомым голосам и ТВ", s.gate_require_match, "Пока не записан ни один голос, выполняются все команды.")}
          ${rangeField("gate_threshold", "Порог узнавания голоса", s.gate_threshold, 0.3, 0.9, 0.01, "pct", "Ниже — легче пропускает (ТВ может проскочить). Выше — строже. Начните с 55–65%.", ["мягче", "строже"])}
          ${saveRow()}
        </form>
      </div>
      <form class="card" id="diaryForm" autocomplete="off" style="margin-top:14px">
        <h3>${icon("book")}Дневник и распознавание речи</h3>
        <div class="grid grid-2">
          <div>
            ${switchField("diary_enabled", "Записывать и расшифровывать речь", s.diary_enabled, "Ключевое слово работает и при выключенном дневнике.")}
            ${rangeField("diary_vad_rms", "Порог громкости речи", s.diary_vad_rms, 0.002, 0.06, 0.001, "rms", "Шум попадает в дневник — увеличьте; тихая речь теряется — уменьшите. Ориентир — индикатор на вкладке «Колонка».", ["чувствительнее", "грубее"])}
            ${rangeField("diary_silence_ms", "Пауза, разделяющая реплики", s.diary_silence_ms, 300, 3000, 100, "ms")}
          </div>
          <div>
            ${rangeField("diary_min_speech_ms", "Минимальная длина реплики", s.diary_min_speech_ms, 200, 3000, 100, "ms", "Короче — не расшифровывается (кашель, стук).")}
            ${rangeField("diary_max_utterance_ms", "Максимальная длина реплики", s.diary_max_utterance_ms, 3000, 30000, 1000, "sec")}
            <div class="field">
              <label for="f_keep">Хранить аудио последних реплик</label>
              <input class="input" type="number" id="f_keep" name="diary_keep_audio" min="0" max="20000" step="50" value="${s.diary_keep_audio}">
              <div class="hint">Нужно для прослушивания и «Кто это?». 0 — не хранить.</div>
            </div>
          </div>
        </div>
        ${saveRow()}
      </form>
      <div class="card" style="margin-top:14px">
        <h3>${icon("info")}О сервисе</h3>
        <dl class="kv">
          <dt>Версия</dt><dd>${esc(st ? st.version : window.HV_VERSION)}</dd><dd></dd>
          <dt>Модель STT</dt><dd class="mono">${esc(st ? st.stt.model : "—")}</dd><dd></dd>
          <dt>Работает</dt><dd>${st ? fmtUptime(st.uptime_s) : "—"}</dd><dd></dd>
          <dt>Колонка</dt><dd class="mono">${st ? `${location.hostname}:${st.ports.pcm}` : "—"}</dd><dd></dd>
          <dt>Необязательно для Assist</dt><dd class="mono small">${st ? `Wyoming STT ${st.ports.gate} · TTS ${st.ports.tts}` : "—"}</dd><dd></dd>
        </dl>
        <p class="small muted" style="margin-bottom:0">Настройки, сохранённые здесь, важнее параметров в конфигурации дополнения. Модель GigaAM и CPU/GPU меняются в конфигурации дополнения.</p>
      </div>`;
    const reload = async () => {
      state.ha = await api("/api/ha").catch((err) => ({ ok: false, error: err.message }));
      renderSettings();
    };
    bindSettingsForm($("#haForm"), reload);
    bindSettingsForm($("#gateForm"));
    bindSettingsForm($("#diaryForm"));
    if (!ha) reload();
  }

  /* ---------------- routing & polling ---------------- */
  function showError(err) {
    console.error(err);
    toast(err && err.message ? err.message : String(err), "bad");
  }

  async function refreshTab() {
    try {
      switch (state.tab) {
        case "speaker":
          await Promise.all([state.settings ? null : loadSettings(), loadOverviewData(), loadFirmware()]);
          renderSpeaker();
          break;
        case "diary":
          ensureDiaryShell();
          await loadSpeakers();
          syncDiaryControls();
          await loadDiary();
          break;
        case "voices":
          await refreshVoices();
          ensureTest();
          break;
        case "tts":
          await loadSettings();
          renderTts();
          break;
        case "assistant":
          await Promise.all([loadSettings(), loadAssistant()]);
          renderAssistant();
          break;
        case "settings":
          await loadSettings();
          renderSettings();
          break;
      }
    } catch (err) {
      showError(err);
    }
  }

  function setTab(tab) {
    tab = TAB_ALIASES[tab] || tab;
    if (!TABS.includes(tab)) tab = "speaker";
    state.tab = tab;
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === tab));
    if (location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
    refreshTab();
  }

  async function poll() {
    try {
      state.status = await api("/api/status");
    } catch {
      state.status = null;
    }
    renderPills();
  }

  // Do not repaint a tab while the user is typing or dragging in it.
  const editing = (name) => {
    const el = document.activeElement;
    return el && view(name).contains(el) && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);
  };

  async function tick() {
    await poll();
    if (state.modalOpen || document.hidden) return;
    try {
      if (state.tab === "speaker" && !editing("speaker")) {
        const fw = state.firmware && Object.values(state.firmware.ota || {}).some((j) => j.status === "uploading");
        await Promise.all([loadOverviewData(), fw ? loadFirmware() : null]);
        renderSpeaker();
      } else if (state.tab === "assistant") {
        state.assistantLog = (await api("/api/assistant/log?limit=100")).log || [];
        renderAssistantLog();
      } else if (state.tab === "diary" && state.diary.items.length <= PAGE && state.playingId == null) {
        await loadDiary();
      }
    } catch (err) {
      console.error(err);
    }
  }

  document.addEventListener("click", (ev) => {
    const go = ev.target.closest("[data-goto]");
    if (go) {
      setTab(go.dataset.goto);
      return;
    }
    const copy = ev.target.closest("[data-copy]");
    if (copy) copyText(copy.dataset.copy);
  });
  $("#tabs").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-tab]");
    if (b) setTab(b.dataset.tab);
  });
  view("speaker").addEventListener("click", onSpeakerClick);
  view("speaker").addEventListener("change", onSpeakerChange);
  view("speaker").addEventListener("input", (ev) => {
    if (ev.target.id === "spVol") ev.target.nextElementSibling.textContent = `${ev.target.value}%`;
  });
  view("speaker").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.id === "spSay") $("#spSayBtn").click();
  });
  $("#voicesMain").addEventListener("click", onVoicesClick);
  $("#voicesMain").addEventListener("keydown", (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target.closest('[data-act="add"]')) {
      ev.preventDefault();
      openAddVoice();
    }
  });
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (tab && tab !== state.tab) setTab(tab);
  });

  (async () => {
    await poll();
    setTab(location.hash.slice(1) || "speaker");
    setInterval(tick, POLL_MS);
  })();
})();
