export const MODEL_CATALOG = {
  "nano-banana": {
    kind: "image",
    label: "Nano Banana 2 Lite",
    providerKey: "nano-banana-2-lite",
    price: 2,
    maxImages: 10,
  },
  "nano-banana-pro": {
    kind: "image",
    label: "Banana Pro",
    providerKey: "gemini-3-pro-image-preview",
    price: 4,
    maxImages: 8,
  },
  "nano-banana-2": {
    kind: "image",
    label: "Banana 2",
    providerKey: "gemini-3.1-flash-image-preview",
    price: 3,
    maxImages: 14,
  },
  "kling-2.6/video": {
    kind: "motion",
    label: "Kling 2.6",
    providerKey: "kling-v2-6",
    price: 12,
  },
  "kling-3.0/video": {
    kind: "motion",
    label: "Клинг 3 Std",
    providerKey: "kling-v2-master",
    price: 16,
  },
  "seedance-2/video": {
    kind: "motion",
    label: "Seedance 2",
    providerKey: "doubao-seedance-2-0",
    price: 18,
  },
};

export const DEFAULT_MODEL_BY_KIND = {
  image: "nano-banana-2",
  motion: "seedance-2/video",
};

export const TONE_FRAGMENTS = {
  "riot-product":
    "Prompt Riot Zine product shot, black paper, hot pink banana currency, torn poster edges, acid yellow stamp",
  photocopy:
    "photocopy grain, scratched black wall, imperfect zine collage, rough paper texture, neon ink accents",
  "neon-poster":
    "bold neon poster, high contrast black paper, cyan and pink spray marks, strong editorial silhouette",
  "mutant-cute":
    "mutant cute mascot object, playful pink banana, punk sticker energy, tactile handmade finish",
};

export const HELPER_FRAGMENTS = {
  "product shot": "premium product shot",
  "poster zine layout": "poster zine layout",
  "neon riot contrast": "neon riot contrast",
  "fashion editorial": "fashion editorial",
  "clean background": "clean background",
  "photocopy grain": "photocopy grain",
};

export function getDefaultModelCode(kind) {
  return DEFAULT_MODEL_BY_KIND[kind] || DEFAULT_MODEL_BY_KIND.image;
}

export function getModel(modelCode) {
  return MODEL_CATALOG[modelCode] || MODEL_CATALOG[getDefaultModelCode("image")];
}

export function isSupportedKind(kind) {
  return Boolean(DEFAULT_MODEL_BY_KIND[kind]);
}
