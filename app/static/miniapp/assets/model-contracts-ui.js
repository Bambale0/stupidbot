import { MODEL_CATALOG } from "./catalog.js?v=20260724-provider2";

const root = document.querySelector("#app");
window.BANANA_MODEL_SETTINGS = window.BANANA_MODEL_SETTINGS || {};

let activeModelCode = "nano-banana-2";
let enhanceQueued = false;

function modelFor(code = activeModelCode) {
  return MODEL_CATALOG[code] || MODEL_CATALOG["nano-banana-2"];
}

function ensureSettings(code) {
  const model = modelFor(code);
  const settings = window.BANANA_MODEL_SETTINGS[code] || {};
  const normalized = {
    aspectRatio: settings.aspectRatio || model.defaultAspectRatio || model.aspectRatios?.[0] || "auto",
    resolution: settings.resolution || model.defaultResolution || model.resolutions?.[0] || "1K",
    duration: settings.duration || model.defaultDuration || model.durations?.[0] || "5",
    characterOrientation:
      settings.characterOrientation ||
      model.defaultCharacterOrientation ||
      model.characterOrientations?.[0] ||
      "image",
  };
  window.BANANA_MODEL_SETTINGS[code] = normalized;
  return normalized;
}

function optionList(values, selected) {
  return (values || [])
    .map((value) => `<option value="${String(value)}" ${String(value) === String(selected) ? "selected" : ""}>${String(value)}</option>`)
    .join("");
}

function documentedAspectCount(model) {
  return (model.aspectRatios || []).filter((value) => value !== "auto").length;
}

function capabilityText(model) {
  if (model.kind === "image") {
    const references = model.minImages === 0
      ? `0–${model.maxImages} референсов`
      : `${model.maxImages} референс`;
    return `${references} · ${(model.resolutions || []).join("/")} · ${documentedAspectCount(model)} форматов`;
  }
  if (activeModelCode.startsWith("kling")) {
    const geometry = model.minDimension
      ? ` · от ${model.minDimension}px · ${model.aspectRatioRange}`
      : "";
    return `1 фото + 1 видео · ${model.durationMin}–${model.durationMax} сек · ${model.mode}${geometry}`;
  }
  return `0–${model.maxImages} изображений · ${model.durations?.[0]}–${model.durations?.at(-1)} сек · ${(model.resolutions || []).join("/")}`;
}

function renderControls() {
  const model = modelFor();
  const settings = ensureSettings(activeModelCode);
  const controls = [];
  if (model.aspectRatios?.length) {
    controls.push(`
      <label class="field-label model-contract-field">
        Соотношение сторон
        <select data-model-setting="aspectRatio">${optionList(model.aspectRatios, settings.aspectRatio)}</select>
      </label>
    `);
  }
  if (model.resolutions?.length) {
    controls.push(`
      <label class="field-label model-contract-field">
        Качество
        <select data-model-setting="resolution">${optionList(model.resolutions, settings.resolution)}</select>
      </label>
    `);
  }
  if (model.durations?.length) {
    controls.push(`
      <label class="field-label model-contract-field">
        Длительность
        <select data-model-setting="duration">${optionList(model.durations, settings.duration)}</select>
      </label>
    `);
  }
  if (model.characterOrientations?.length) {
    controls.push(`
      <label class="field-label model-contract-field">
        Ориентация персонажа
        <select data-model-setting="characterOrientation">${optionList(model.characterOrientations, settings.characterOrientation)}</select>
      </label>
    `);
  }
  return `
    <section class="model-contract-panel" data-model-contract-panel="${activeModelCode}">
      <strong>${model.label}</strong>
      <small>${capabilityText(model)}</small>
      <div class="model-contract-grid">${controls.join("")}</div>
    </section>
  `;
}

function updateModelMenu() {
  root?.querySelectorAll("[data-model]").forEach((button) => {
    const code = button.dataset.model;
    const model = MODEL_CATALOG[code];
    if (!model) return;
    const title = button.querySelector("span");
    const meta = button.querySelector("small");
    if (title) title.textContent = model.label;
    if (meta) meta.textContent = capabilityText(model);
  });
}

function updateHandoff() {
  const model = modelFor();
  const handoff = root?.querySelector(".handoff-card small");
  if (!handoff) return;
  if (model.kind === "image") {
    const fallbackNote = model.fallbackMaxImages && model.fallbackMaxImages < model.maxImages
      ? ` При fallback KIE используются первые ${model.fallbackMaxImages}.`
      : "";
    handoff.textContent = `Референсы необязательны. Модель принимает до ${model.maxImages} изображений; можно продолжить только с промптом.${fallbackNote}`;
  } else if (activeModelCode === "seedance-2/video") {
    handoff.textContent = `Text-to-video или одно стартовое изображение в основном Comet-пути. Длительность ${model.durations[0]}–${model.durations.at(-1)} сек.`;
  } else {
    handoff.textContent = `Одно изображение персонажа и одно видео движения ${model.durationMin}–${model.durationMax} сек. Форматы видео: ${model.videoFormats.join(", ")}.`;
  }
}

function enhanceSheet() {
  updateModelMenu();
  const form = root?.querySelector(".sheet .form-stack");
  if (!form) return;
  const existing = form.querySelector("[data-model-contract-panel]");
  if (existing?.dataset.modelContractPanel === activeModelCode) {
    updateHandoff();
    return;
  }
  existing?.remove();
  const promptLabel = form.querySelector("textarea#promptInput")?.closest("label");
  if (promptLabel) {
    promptLabel.insertAdjacentHTML("beforebegin", renderControls());
  }
  const primary = form.querySelector('[data-action="send-generation"]');
  if (primary) {
    primary.textContent = modelFor().minImages === 0
      ? "✧ Далее: промпт или референсы"
      : "✧ Далее: загрузить референсы";
  }
  updateHandoff();
}

function queueEnhance() {
  if (enhanceQueued) return;
  enhanceQueued = true;
  requestAnimationFrame(() => {
    enhanceQueued = false;
    enhanceSheet();
  });
}

root?.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const modelButton = target.closest("[data-model]");
  if (modelButton?.dataset.model && MODEL_CATALOG[modelButton.dataset.model]) {
    activeModelCode = modelButton.dataset.model;
    ensureSettings(activeModelCode);
    queueEnhance();
    return;
  }
  const modeButton = target.closest("[data-open-kind]");
  const kind = modeButton?.dataset.openKind;
  if (kind === "image") activeModelCode = "nano-banana-2";
  if (kind === "motion") activeModelCode = "seedance-2/video";
  if (kind === "motion-control") activeModelCode = "kling-2.6/video";
  if (kind) queueEnhance();
}, true);

root?.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLSelectElement) || !target.dataset.modelSetting) return;
  const settings = ensureSettings(activeModelCode);
  settings[target.dataset.modelSetting] = target.value;
}, true);

const observer = new MutationObserver(queueEnhance);
if (root) observer.observe(root, { childList: true, subtree: true });
queueEnhance();
