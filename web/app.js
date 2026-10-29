(() => {
  const $ = (id) => document.getElementById(id);
  let lastBlob = null;

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  async function refreshSpeakers() {
    const data = await api("/api/speakers");
    const ul = $("speakers");
    ul.innerHTML = "";
    for (const s of data.speakers || []) {
      const li = document.createElement("li");
      li.innerHTML = `<span><strong>${escapeHtml(s.name)}</strong> <span class="meta">${s.samples || 0} сэмпл.</span></span>`;
      const btn = document.createElement("button");
      btn.textContent = "Удалить";
      btn.className = "danger";
      btn.onclick = async () => {
        await api(`/api/speakers/${encodeURIComponent(s.id)}`, { method: "DELETE" });
        refreshSpeakers();
      };
      li.appendChild(btn);
      ul.appendChild(li);
    }
  }

  async function refreshDialogues() {
    const data = await api("/api/dialogues?limit=40");
    const ul = $("dialogues");
    ul.innerHTML = "";
    for (const u of data.utterances || []) {
      const li = document.createElement("li");
      li.innerHTML =
        `<span><strong>${escapeHtml(u.speaker_name || "unknown")}</strong>: ${escapeHtml(u.text)}</span>` +
        `<span class="meta">${escapeHtml((u.ts || "").replace("T", " ").slice(0, 19))}</span>`;
      ul.appendChild(li);
    }
  }

  async function refreshDiary() {
    const d = await api("/api/diary");
    $("diaryEnabled").checked = !!d.enabled;
    $("diaryStatus").textContent = d.last_text
      ? `Последнее: [${d.last_speaker || "?"}] ${d.last_text}`
      : "Пока пусто";
  }

  $("diaryEnabled").addEventListener("change", async (e) => {
    await api("/api/diary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: e.target.checked }),
    });
  });

  $("refreshBtn").onclick = () => {
    refreshDialogues();
    refreshDiary();
  };

  $("recBtn").onclick = async () => {
    $("enrollMsg").textContent = "Идёт запись…";
    $("enrollBtn").disabled = true;
    lastBlob = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks = [];
      rec.ondataavailable = (ev) => chunks.push(ev.data);
      const done = new Promise((resolve) => {
        rec.onstop = () => resolve();
      });
      rec.start();
      await new Promise((r) => setTimeout(r, 4000));
      rec.stop();
      stream.getTracks().forEach((t) => t.stop());
      await done;
      lastBlob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      $("enrollMsg").textContent = "Запись готова — сохраните.";
      $("enrollBtn").disabled = !$("speakerName").value.trim();
    } catch (err) {
      $("enrollMsg").textContent = String(err);
    }
  };

  $("speakerName").addEventListener("input", () => {
    $("enrollBtn").disabled = !lastBlob || !$("speakerName").value.trim();
  });

  $("enrollBtn").onclick = async () => {
    if (!lastBlob) return;
    const name = $("speakerName").value.trim();
    // Decode browser audio to PCM16 via AudioContext
    const ab = await lastBlob.arrayBuffer();
    const ctx = new AudioContext({ sampleRate: 16000 });
    const decoded = await ctx.decodeAudioData(ab.slice(0));
    const ch = decoded.getChannelData(0);
    const pcm = new Int16Array(ch.length);
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    await ctx.close();
    const r = await fetch(`/api/enroll?name=${encodeURIComponent(name)}&rate=16000`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: pcm.buffer,
    });
    if (!r.ok) {
      $("enrollMsg").textContent = await r.text();
      return;
    }
    $("enrollMsg").textContent = `Сохранено: ${name}`;
    lastBlob = null;
    $("enrollBtn").disabled = true;
    refreshSpeakers();
  };

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  refreshSpeakers();
  refreshDialogues();
  refreshDiary();
  setInterval(() => {
    refreshDialogues();
    refreshDiary();
  }, 8000);
})();
