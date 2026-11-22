"use strict";

(() => {
  const SLOTS = ["0900", "1400"];
  const CLOCKS = { "0900": "09:00", "1400": "14:00" };
  const elements = Object.fromEntries([
    "sync-status", "forecast-panel", "forecast-info", "forecast-date", "forecast-time",
    "previous-edition", "forecast-model", "forecast-initial", "forecast-generated",
    "pdf-link", "png-link", "print-button", "message", "sheet-viewport", "forecast-image",
  ].map(id => [id, document.getElementById(id)]));
  const entries = new Map();
  const errors = new Map();
  const pendingSlots = new Set();
  const loadedImages = new Map();
  const timestampFormatter = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const dayFormatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
  });
  let configError = "";
  let refreshing = false;
  let firstLoad = true;
  const initialSlot = new URL(location.href).searchParams.get("time");
  let selectedExplicitly = SLOTS.includes(initialSlot);
  let selected = selectedExplicitly ? initialSlot : preferredSlot();

  class DataError extends Error {}

  function dateInJapan(value = new Date()) {
    const parts = Object.fromEntries(dayFormatter.formatToParts(value).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function preferredSlot() {
    const hour = Number(new Intl.DateTimeFormat("en", {
      timeZone: "Asia/Tokyo", hour: "2-digit", hourCycle: "h23",
    }).format(new Date()));
    return hour < 10 ? "0900" : "1400";
  }

  function safeUrl(raw, base = location.href) {
    if (typeof raw !== "string" || !raw.trim()) throw new DataError("ファイルの URL が不正です");
    const value = raw.trim();
    const url = new URL(value, base);
    if (!["https:", "http:"].includes(url.protocol)) throw new DataError("ファイルの URL が不正です");
    if (!/^https?:\/\//i.test(value) && url.origin !== location.origin) {
      throw new DataError("ファイルの URL が不正です");
    }
    return url.href;
  }

  function parsedTime(value) {
    if (typeof value !== "string" || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) {
      throw new DataError("予報の日時データが不正です");
    }
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) throw new DataError("予報の日時データが不正です");
    return parsed;
  }

  function asset(file, base) {
    if (!file || typeof file !== "object") throw new DataError("予報ファイルの形式が一致しません");
    const url = new URL(safeUrl(file.url || file.path, base));
    if (typeof file.sha256 === "string" && /^[a-f0-9]{64}$/i.test(file.sha256)) {
      url.searchParams.set("v", file.sha256.slice(0, 16));
    }
    return url.href;
  }

  function normalize(raw, base, expectedSlot) {
    if (!raw || typeof raw !== "object" || !SLOTS.includes(raw.slot)) {
      throw new DataError("予報データの形式が一致しません");
    }
    if (expectedSlot && raw.slot !== expectedSlot) throw new DataError("予報対象時刻が一致しません");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(raw.valid_date || "") || raw.valid_time_jst !== CLOCKS[raw.slot]) {
      throw new DataError("予報対象日時が不正です");
    }
    const valid = parsedTime(raw.valid_at_jst);
    if (dateInJapan(valid) !== raw.valid_date ||
        !raw.valid_at_jst.startsWith(`${raw.valid_date}T${CLOCKS[raw.slot]}:00+09:00`)) {
      throw new DataError("予報対象日時が一致しません");
    }
    if (!["MSM", "GSM", "UNAVAILABLE"].includes(raw.model)) {
      throw new DataError("予報モデルの形式が一致しません");
    }
    const generated = parsedTime(raw.image_generated_time_jst);
    const initial = raw.initial_time_jst ? parsedTime(raw.initial_time_jst) : null;
    if (!initial && raw.model !== "UNAVAILABLE") throw new DataError("予報の初期時刻がありません");
    return {
      slot: raw.slot, date: raw.valid_date, model: raw.model, initial, generated,
      png: asset(raw.files?.png, base), pdf: asset(raw.files?.pdf, base),
    };
  }

  async function fetchJson(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      try { return await response.json(); }
      catch { throw new DataError("予報データを読み取れません"); }
    } finally { clearTimeout(timer); }
  }

  function loadImage(url) {
    if (loadedImages.has(url)) return loadedImages.get(url);
    const promise = new Promise((resolve, reject) => {
      const image = new Image();
      const timer = setTimeout(() => { image.onload = null; image.onerror = null; reject(new Error("Image timeout")); }, 20000);
      image.onload = () => { clearTimeout(timer); resolve(); };
      image.onerror = () => { clearTimeout(timer); reject(new Error("Image unavailable")); };
      image.src = url;
    });
    loadedImages.set(url, promise);
    promise.catch(() => loadedImages.delete(url));
    return promise;
  }

  function newerThan(candidate, existing) {
    return !existing || candidate.date > existing.date ||
      (candidate.date === existing.date && candidate.generated >= existing.generated);
  }

  async function accept(entry) {
    if (!newerThan(entry, entries.get(entry.slot))) return;
    await loadImage(entry.png);
    // The metadata and image advance together, after the new image has loaded.
    if (newerThan(entry, entries.get(entry.slot))) entries.set(entry.slot, entry);
    errors.delete(entry.slot);
    render();
  }

  function failure(error, slot) {
    errors.set(slot, error instanceof DataError ? error.message : "更新を確認できませんでした");
    console.error(`Forecast ${slot}:`, error);
  }

  function render() {
    const today = dateInJapan();
    for (const slot of SLOTS) {
      const tab = document.getElementById(`tab-${slot}`);
      const active = slot === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(`date-${slot}`).textContent = entries.has(slot)
        ? entries.get(slot).date.replaceAll("-", ".") : (errors.has(slot) ? "未取得" : pendingSlots.has(slot) ? "準備中" : "—");
    }
    elements["forecast-panel"].setAttribute("aria-labelledby", `tab-${selected}`);
    const entry = entries.get(selected);
    elements["forecast-info"].hidden = !entry;
    elements["sheet-viewport"].hidden = !entry;
    if (entry) {
      elements["forecast-date"].textContent = entry.date.replaceAll("-", ".");
      elements["forecast-time"].textContent = `${CLOCKS[selected]} JST`;
      elements["previous-edition"].hidden = entry.date >= today;
      elements["forecast-model"].textContent = entry.model === "UNAVAILABLE" ? "取得不可" : entry.model;
      elements["forecast-initial"].textContent = entry.initial ? `${timestampFormatter.format(entry.initial)} JST` : "—";
      elements["forecast-generated"].textContent = `${timestampFormatter.format(entry.generated)} JST`;
      elements["pdf-link"].href = entry.pdf;
      elements["png-link"].href = entry.png;
      elements["forecast-image"].alt = `${entry.date} ${CLOCKS[selected]} JST 九州 NAV LOG`;
      if (elements["forecast-image"].src !== entry.png) elements["forecast-image"].src = entry.png;
    }
    const error = errors.get(selected) || configError;
    elements.message.textContent = error || (entry ? "" : refreshing ? "予報を読み込んでいます" :
      pendingSlots.has(selected) ? "予報を準備中です" : "予報を取得できませんでした");
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    configError = "";
    render();
    let config = { automatic_updates_enabled: false, blob_base_url: null, bootstrap_manifest_url: "/bootstrap/manifest.json" };
    try {
      const received = await fetchJson(safeUrl("/config.json"));
      if (!received || typeof received.automatic_updates_enabled !== "boolean" ||
          typeof received.bootstrap_manifest_url !== "string" ||
          !(received.blob_base_url === null || typeof received.blob_base_url === "string") ||
          !(received.static_base_url == null || typeof received.static_base_url === "string") ||
          !(received.available_slots == null || (Array.isArray(received.available_slots) &&
            received.available_slots.length <= 2 && received.available_slots.every(slot => SLOTS.includes(slot)) &&
            new Set(received.available_slots).size === received.available_slots.length))) {
        throw new DataError("表示設定の形式が一致しません");
      }
      config = received;
    } catch (error) {
      configError = error instanceof DataError ? error.message : "表示設定を確認できませんでした";
    }
    elements["sync-status"].textContent = configError ? "自動更新の設定を確認できません" :
      config.automatic_updates_enabled ? "" : "自動更新の設定待ち";
    pendingSlots.clear();
    if (config.static_base_url && Array.isArray(config.available_slots)) {
      for (const slot of SLOTS) if (!config.available_slots.includes(slot)) {
        pendingSlots.add(slot);
        errors.delete(slot);
      }
    }

    try {
      const bootstrapUrl = safeUrl(config.bootstrap_manifest_url);
      const bootstrap = await fetchJson(bootstrapUrl);
      if (bootstrap?.schema !== "kyushu-daily-publication-1" || !Array.isArray(bootstrap.forecasts) ||
          (bootstrap.forecasts.length < 1 && !(config.static_base_url && pendingSlots.size === 2)) ||
          bootstrap.forecasts.length > 2) {
        throw new DataError("予報データの形式が一致しません");
      }
      const seen = new Set();
      await Promise.all(bootstrap.forecasts.map(async raw => {
        const slot = raw?.slot;
        if (!SLOTS.includes(slot) || seen.has(slot)) throw new DataError("予報データの形式が一致しません");
        seen.add(slot);
        try { await accept(normalize(raw, bootstrapUrl, slot)); }
        catch (error) { failure(error, slot); }
      }));
    } catch (error) {
      for (const slot of SLOTS) failure(error, slot);
    }

    const publicationBase = config.static_base_url || config.blob_base_url;
    if (publicationBase) {
      try {
        const sourceBase = safeUrl(publicationBase).replace(/\/$/, "");
        await Promise.all(SLOTS.filter(slot => !pendingSlots.has(slot)).map(async slot => {
          try {
            const url = safeUrl(`${sourceBase}/latest/${slot}.json`);
            const pointer = await fetchJson(url);
            if (pointer?.schema !== "kyushu-daily-pointer-1") throw new DataError("予報データの形式が一致しません");
            await accept(normalize(pointer, url, slot));
          } catch (error) { failure(error, slot); }
        }));
      } catch (error) {
        for (const slot of SLOTS) failure(error, slot);
      }
    }
    if (firstLoad && !selectedExplicitly && entries.size) {
      const today = dateInJapan();
      if (entries.get(selected)?.date !== today) {
        const current = SLOTS.find(slot => entries.get(slot)?.date === today);
        if (current) selected = current;
      }
    }
    firstLoad = false;
    refreshing = false;
    render();
  }

  function select(slot, focus = false) {
    if (!SLOTS.includes(slot)) return;
    selected = slot;
    selectedExplicitly = true;
    const url = new URL(location.href);
    url.searchParams.set("time", slot);
    history.replaceState(null, "", url);
    render();
    if (focus) document.getElementById(`tab-${slot}`).focus();
  }

  for (const slot of SLOTS) {
    const tab = document.getElementById(`tab-${slot}`);
    tab.addEventListener("click", () => select(slot));
    tab.addEventListener("keydown", event => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        select(event.key === "Home" ? "0900" : event.key === "End" ? "1400" :
          selected === "0900" ? "1400" : "0900", true);
      }
    });
  }
  elements["print-button"].addEventListener("click", () => window.print());
  window.addEventListener("popstate", () => {
    const slot = new URL(location.href).searchParams.get("time");
    if (SLOTS.includes(slot)) { selected = slot; selectedExplicitly = true; render(); }
  });
  window.addEventListener("unhandledrejection", () => {
    elements.message.textContent = "表示中に問題が発生しました。ページを再読み込みしてください";
  });
  window.addEventListener("error", () => {
    elements.message.textContent = "表示中に問題が発生しました。ページを再読み込みしてください";
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  setInterval(() => { if (!document.hidden) refresh(); }, 5 * 60 * 1000);
  setInterval(() => { if (!document.hidden) render(); }, 60 * 1000);
  render();
  refresh();
})();
