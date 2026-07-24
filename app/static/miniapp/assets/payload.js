function allowedValue(value, allowed, fallback) {
  const normalized = String(value || "").trim();
  return Array.isArray(allowed) && allowed.includes(normalized) ? normalized : fallback;
}

export function buildMiniAppPayload({ state, model, prompt }) {
  const selected = window.BANANA_MODEL_SETTINGS?.[state.modelCode] || {};
  const defaultAspect = model.defaultAspectRatio || state.aspect || "auto";
  const defaultResolution = model.defaultResolution || "1K";
  const defaultDuration = model.defaultDuration || "5";
  const defaultOrientation = model.defaultCharacterOrientation || "image";
  const aspectRatio = allowedValue(
    selected.aspectRatio || state.aspect,
    model.aspectRatios,
    defaultAspect,
  );
  const resolution = allowedValue(
    selected.resolution,
    model.resolutions,
    defaultResolution,
  );
  const duration = allowedValue(
    selected.duration,
    model.durations,
    defaultDuration,
  );
  const characterOrientation = allowedValue(
    selected.characterOrientation,
    model.characterOrientations,
    defaultOrientation,
  );

  const payload = {
    source: "pink_lab",
    version: 5,
    kind: model.kind,
    model_code: state.modelCode,
    model_title: model.label,
    provider_key: model.providerKey,
    aspect_ratio: aspectRatio,
    resolution,
    duration,
    prompt,
    fields: {
      tone: state.tone,
      aspect: aspectRatio,
      subject: state.subject.trim(),
    },
  };
  if (Array.isArray(model.characterOrientations) && model.characterOrientations.length) {
    payload.character_orientation = characterOrientation;
  }
  if (model.backgroundSource) {
    payload.background_source = model.backgroundSource;
  }
  if (state.sourceFeedTaskId) {
    payload.source_feed_task_id = Number(state.sourceFeedTaskId);
  }
  return payload;
}
