export function buildMiniAppPayload({ state, model, prompt }) {
  const payload = {
    source: "pink_lab",
    version: 3,
    kind: model.kind,
    model_code: state.modelCode,
    model_title: model.label,
    provider_key: model.providerKey,
    aspect_ratio: state.aspect,
    resolution: "2K",
    prompt,
    fields: {
      tone: state.tone,
      aspect: state.aspect,
      subject: state.subject.trim(),
    },
  };
  if (state.sourceFeedTaskId) {
    payload.source_feed_task_id = Number(state.sourceFeedTaskId);
  }
  return payload;
}
