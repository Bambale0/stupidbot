const MAX_PROMPT_LENGTH = 1800;

export function truncate(value, maxLength = MAX_PROMPT_LENGTH) {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 3).trim()}...`;
}

export function hasPrompt(subject) {
  return subject.trim().length > 0;
}

export function createPrompt({ subject, tone, aspect, model, toneFragments }) {
  const cleanSubject = subject.trim();
  const parts = [
    `Create: ${cleanSubject}.`,
    `Style: ${toneFragments[tone]}.`,
    `Aspect ratio: ${aspect}.`,
    "Palette: riot black, hot pink, acid yellow, cyan sparks, rough paper grain.",
    "Avoid: readable text, watermark, generic stock look, dull beige background, low resolution.",
  ];

  if (model.kind === "motion") {
    parts.splice(
      3,
      0,
      "Motion: image-to-video, controlled camera drift, stable subject, zine poster comes alive.",
    );
  }

  return truncate(parts.join(" "));
}
