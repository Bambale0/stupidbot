export const MODEL_CATALOG = {
  "nano-banana": {
    kind: "image",
    label: "Nano Banana 2 Lite",
    providerKey: "gemini-3.1-flash-lite-image",
    price: 2,
    minImages: 0,
    maxImages: 14,
    fallbackMaxImages: 10,
    resolutions: ["1K"],
    defaultResolution: "1K",
    aspectRatios: ["auto", "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9"],
    defaultAspectRatio: "auto",
  },
  "nano-banana-pro": {
    kind: "image",
    label: "Nano Banana Pro",
    providerKey: "gemini-3-pro-image",
    price: 4,
    minImages: 0,
    maxImages: 14,
    resolutions: ["1K", "2K", "4K"],
    defaultResolution: "1K",
    aspectRatios: ["auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
    defaultAspectRatio: "auto",
  },
  "nano-banana-2": {
    kind: "image",
    label: "Nano Banana 2",
    providerKey: "gemini-3.1-flash-image",
    price: 3,
    minImages: 0,
    maxImages: 14,
    resolutions: ["512", "1K", "2K", "4K"],
    defaultResolution: "1K",
    aspectRatios: ["auto", "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9"],
    defaultAspectRatio: "auto",
  },
  "kling-2.6/video": {
    kind: "motion",
    label: "Kling 2.6 Motion Control",
    providerKey: "kling-2.6/motion-control",
    price: 12,
    unit: "/сек",
    minImages: 1,
    maxImages: 1,
    maxVideos: 1,
    durationMin: 3,
    durationMax: 30,
    mode: "720p",
    characterOrientations: ["image", "video"],
    defaultCharacterOrientation: "image",
    imageFormats: ["JPEG", "PNG"],
    videoFormats: ["MP4", "MOV", "MKV"],
  },
  "kling-3.0/video": {
    kind: "motion",
    label: "Kling 3.0 Motion Control",
    providerKey: "kling-3.0/motion-control",
    price: 16,
    unit: "/сек",
    minImages: 1,
    maxImages: 1,
    maxVideos: 1,
    durationMin: 3,
    durationMax: 30,
    mode: "720p",
    characterOrientations: ["image", "video"],
    defaultCharacterOrientation: "image",
    backgroundSource: "input_video",
    imageFormats: ["JPEG", "PNG"],
    videoFormats: ["MP4", "MOV"],
    minDimension: 341,
    aspectRatioRange: "2:5–5:2",
  },
  "seedance-2/video": {
    kind: "motion",
    label: "Seedance 2.0",
    providerKey: "doubao-seedance-2-0",
    price: 18,
    minImages: 0,
    maxImages: 1,
    fallbackMaxImages: 2,
    durations: ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"],
    defaultDuration: "5",
    resolutions: ["480p", "720p", "1080p"],
    defaultResolution: "720p",
    aspectRatios: ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    defaultAspectRatio: "16:9",
    imageFormats: ["JPEG", "PNG", "WebP"],
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
