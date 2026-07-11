import {
  DEFAULT_MODEL_BY_KIND,
  MODEL_CATALOG,
  TONE_FRAGMENTS,
  getModel,
} from "./catalog.js?v=20260711-refcounts2";
import { buildMiniAppPayload } from "./payload.js?v=20260630-feedrepeat1";
import { createPrompt, hasPrompt as promptExists, truncate } from "./prompt.js?v=20260611-phase2";
import {
  bootTelegram,
  closeTelegramApp,
  getTelegramApp,
  hasTelegramInitData,
  sendTelegramData,
  setViewportHeight,
  updateMainButton,
} from "./telegram.js?v=20260611-phase3";

const HISTORY_KEY = "banana-miniapp-history-v1";
const BALANCE_KEY = "banana-miniapp-balance-v1";
const BOT_USERNAME = "eva_nana_bot";

const telegramApp = getTelegramApp();
const root = document.querySelector("#app");

const imageModels = [
  { code: "nano-banana", title: "banana · 2K/4K", displayPrice: 10, quality: "2K/4K", maxImages: 1 },
  { code: "nano-banana-2", title: "banana-2 · 2K/4K", displayPrice: 15, quality: "2K/4K", maxImages: 14 },
  { code: "nano-banana-pro", title: "banana-pro · 2K/4K", displayPrice: 20, quality: "2K/4K", maxImages: 8 },
];

const videoModels = [
  { code: "seedance-2/video", title: "seedance-2", displayPrice: 18 },
];

const motionControlModels = [
  { code: "kling-2.6/video", title: "Motion Control · Kling 2.6", displayPrice: 12, unit: "/сек" },
  { code: "kling-3.0/video", title: "Motion Control · Kling 3.0", displayPrice: 16, unit: "/сек" },
];

const DEFAULT_MOTION_CONTROL_MODEL = "kling-2.6/video";

const trends = [
  {
    id: "say",
    title: "Я хочу сказать",
    badge: "top",
    kind: "video",
    prompt: "Трендовое видео: человек в комнате хочет сказать важную фразу, драматичный zoom, вайб 90-х",
    image: "assets/trend-say.webp?v=20260611-banana2",
    bg: "linear-gradient(135deg, rgba(255,63,159,.9), rgba(255,155,98,.72), rgba(7,17,33,.86))",
  },
  {
    id: "stadium",
    title: "Трансляция на стадионе",
    badge: "",
    kind: "video",
    prompt: "Персонаж попадает на огромный экран стадиона, фанаты вокруг, кинематографичный репортажный кадр",
    image: "assets/trend-stadium.webp?v=20260611-banana2",
    bg: "linear-gradient(135deg, rgba(30,63,151,.92), rgba(233,94,134,.72))",
  },
  {
    id: "paris",
    title: "Ты в Париже",
    badge: "new",
    kind: "image",
    prompt: "Трендовое фото в Париже у Эйфелевой башни, яркий отпускной кадр, вспышка камеры",
    image: "assets/trend-paris.webp?v=20260611-banana2",
    bg: "linear-gradient(135deg, rgba(99,207,246,.78), rgba(255,63,159,.82), rgba(7,17,33,.9))",
  },
  {
    id: "night",
    title: "Ночной город",
    badge: "top",
    kind: "image",
    prompt: "Футуристический киберпанк-город с неоновыми огнями, глянцевый ночной street style",
    image: "assets/trend-night.webp?v=20260611-banana2",
    bg: "linear-gradient(135deg, rgba(15,18,36,.98), rgba(0,229,255,.5), rgba(255,0,124,.68))",
  },
  {
    id: "camera",
    title: "Папарацци",
    badge: "top",
    kind: "image",
    prompt: "Фото знаменитости на красной дорожке, вспышки папарацци, гламурный журнальный стиль",
    image: "assets/trend-camera.webp?v=20260611-banana2",
    bg: "linear-gradient(135deg, rgba(255,0,124,.82), rgba(29,18,18,.92))",
  },
  {
    id: "studio",
    title: "Обложка артиста",
    badge: "new",
    kind: "image",
    prompt: "Трендовое фото в стиле 90-х, студийная обложка артиста, зерно пленки, яркий контраст",
    image: "assets/trend-studio.webp?v=20260611-banana2",
    bg: "linear-gradient(135deg, rgba(255,155,98,.78), rgba(255,63,159,.78), rgba(7,17,33,.92))",
  },
];

let tariffs = [];

const state = {
  tab: "home",
  selectedTariff: "",
  sheetOpen: false,
  trendsCollapsed: false,
  flow: "image",
  kind: "image",
  modelCode: DEFAULT_MODEL_BY_KIND.image,
  prompt: "",
  template: null,
  sourceFeedTaskId: null,
  modelMenuOpen: false,
  files: [],
  filePreviews: [],
  feedItems: [],
  feedLoading: false,
  packagesLoading: false,
  paymentLoading: false,
  customCredits: "10",
  customCreditRate: 1,
  customCreditMin: 1,
  customCreditMax: 100000,
  taskItems: [],
  taskLoading: false,
  taskStreamReady: false,
  status: "",
};

let statusTimer = null;
let taskStream = null;
const notifiedTaskIds = new Set();

function telegramUser() {
  return telegramApp?.initDataUnsafe?.user || {};
}

function telegramInitData() {
  return telegramApp?.initData || "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function loadHistory() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.slice(0, 12) : [];
  } catch {
    return [];
  }
}

function saveHistory(items) {
  window.localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 12)));
}

function currentBalance() {
  const saved = Number(window.localStorage.getItem(BALANCE_KEY));
  return Number.isFinite(saved) ? saved : 0;
}

function currentModelList() {
  if (state.flow === "motion-control") {
    return motionControlModels;
  }
  return state.kind === "motion" ? videoModels : imageModels;
}

function currentModelMeta() {
  const models = currentModelList();
  return models.find((item) => item.code === state.modelCode) || models[0];
}

function currentModel() {
  return getModel(currentModelMeta().code);
}

function clearFilePreviews() {
  state.filePreviews.forEach((item) => URL.revokeObjectURL(item.url));
  state.filePreviews = [];
}

function displayPrice() {
  return currentModelMeta().displayPrice || currentModel().price;
}

function displayPriceText() {
  const meta = currentModelMeta();
  return `${displayPrice()} кр.${meta.unit || ""}`;
}

function referenceLimitText(maxImages) {
  const count = Number(maxImages) || 1;
  return count === 1 ? "1 фото-референс" : `до ${count} фото-референсов`;
}

function setStatus(message) {
  state.status = message;
  if (statusTimer) {
    window.clearTimeout(statusTimer);
  }
  statusTimer = window.setTimeout(() => {
    state.status = "";
    render();
  }, 3400);
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.left = "-9999px";
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}

function selectedPrompt() {
  if (state.sourceFeedTaskId) {
    return truncate(state.prompt, 1500);
  }
  const model = currentModel();
  return createPrompt({
    subject: state.prompt,
    tone: "riot-product",
    aspect: "9:16",
    model,
    toneFragments: TONE_FRAGMENTS,
  });
}

function buildPayload() {
  const model = currentModel();
  return buildMiniAppPayload({
    state: {
      kind: model.kind,
      modelCode: currentModelMeta().code,
      aspect: "9:16",
      tone: "riot-product",
      subject: state.prompt,
      sourceFeedTaskId: state.sourceFeedTaskId,
    },
    model,
    prompt: selectedPrompt(),
  });
}

function addHistoryItem(payload) {
  const items = loadHistory();
  items.unshift({
    id: `${Date.now()}`,
    title: state.template?.title || "Генерация",
    model: currentModelMeta().title,
    cost: displayPrice(),
    status: hasTelegramInitData(telegramApp) ? "Отправлено" : "Скопировано",
    createdAt: new Date().toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }),
    prompt: payload.prompt,
  });
  saveHistory(items);
}

function telegramUserId() {
  const user = telegramUser();
  return user.id ? String(user.id) : "";
}

function authHeaders(extra = {}) {
  const initData = telegramInitData();
  const headers = { Accept: "application/json", ...extra };
  if (initData) {
    headers["X-Telegram-Init-Data"] = initData;
  }
  return headers;
}

function updateTelegramRunButton() {
  const visible = state.sheetOpen || state.tab === "create";
  updateMainButton(telegramApp, {
    text: state.sheetOpen ? `Отправить бриф · ${displayPriceText()}` : "Создать",
    enabled: state.sheetOpen ? promptExists(state.prompt) : true,
    visible,
  });
}

function openGenerator({
  kind = "image",
  prompt = "",
  template = null,
  modelCode = "",
  sourceFeedTaskId = null,
} = {}) {
  state.flow =
    kind === "motion-control"
      ? "motion-control"
      : kind === "motion" || kind === "video"
        ? "video"
        : "image";
  state.kind = state.flow === "image" ? "image" : "motion";
  state.modelCode =
    state.flow === "motion-control"
      ? DEFAULT_MOTION_CONTROL_MODEL
      : state.kind === "motion"
        ? DEFAULT_MODEL_BY_KIND.motion
        : DEFAULT_MODEL_BY_KIND.image;
  if (modelCode && currentModelList().some((item) => item.code === modelCode)) {
    state.modelCode = modelCode;
  }
  state.prompt = prompt;
  state.template = template;
  state.sourceFeedTaskId = sourceFeedTaskId;
  state.files = [];
  clearFilePreviews();
  state.modelMenuOpen = false;
  state.sheetOpen = true;
  render();
}

function closeGenerator() {
  state.sheetOpen = false;
  state.modelMenuOpen = false;
  state.status = "";
  state.flow = "image";
  state.sourceFeedTaskId = null;
  state.files = [];
  clearFilePreviews();
  render();
}

function sendGeneration() {
  if (!promptExists(state.prompt)) {
    setStatus("Добавьте подсказку для генерации.");
    render();
    return;
  }

  const payload = buildPayload();
  const rawPayload = JSON.stringify(payload);
  addHistoryItem(payload);

  if (sendTelegramData(telegramApp, rawPayload)) {
    setStatus("Заявка отправлена в Telegram.");
  } else {
    copyText(payload.prompt)
      .then(() => {
        setStatus("Prompt скопирован.");
        render();
      })
      .catch(() => {
        setStatus("Не удалось скопировать prompt.");
        render();
      });
  }
  render();
}

async function loadFeed() {
  state.feedLoading = true;
  render();
  try {
    const response = await fetch("/api/tma/app/feed?limit=40", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    state.feedItems = Array.isArray(payload.items) ? payload.items : [];
  } catch {
    setStatus("Не удалось загрузить ленту.");
  } finally {
    state.feedLoading = false;
    render();
  }
}

async function loadTasks({ quiet = false } = {}) {
  if (!telegramInitData()) {
    return;
  }
  if (!quiet) {
    state.taskLoading = true;
    render();
  }
  try {
    const response = await fetch("/api/tma/app/tasks?limit=40", { headers: authHeaders() });
    if (!response.ok) {
      throw new Error("tasks failed");
    }
    const payload = await response.json();
    applyTaskItems(Array.isArray(payload.items) ? payload.items : []);
  } catch {
    if (!quiet) {
      setStatus("Не удалось загрузить историю.");
    }
  } finally {
    state.taskLoading = false;
    render();
  }
}

function applyTaskItems(items) {
  const previous = new Map(state.taskItems.map((item) => [String(item.id), item.status]));
  state.taskItems = items;
  for (const item of items) {
    const id = String(item.id);
    const becameReady = item.status === "success" && previous.get(id) && previous.get(id) !== "success";
    const unseenReady = item.status === "success" && !previous.has(id) && !notifiedTaskIds.has(id);
    if ((becameReady || unseenReady) && item.media_url) {
      notifiedTaskIds.add(id);
      setStatus("Генерация готова. Результат уже в Истории.");
      if (navigator.vibrate) {
        navigator.vibrate(35);
      }
    }
  }
}

function connectTaskStream() {
  const initData = telegramInitData();
  if (!initData || taskStream || !window.EventSource) {
    return;
  }
  const params = new URLSearchParams({ init_data: initData, limit: "40" });
  taskStream = new EventSource(`/api/tma/app/tasks/stream?${params.toString()}`);
  taskStream.addEventListener("tasks", (event) => {
    try {
      const payload = JSON.parse(event.data || "{}");
      applyTaskItems(Array.isArray(payload.items) ? payload.items : []);
      state.taskStreamReady = true;
      render();
    } catch {
      // Ignore malformed stream events; polling fallback keeps history fresh.
    }
  });
  taskStream.onerror = () => {
    state.taskStreamReady = false;
  };
}

async function feedAction(taskId, action) {
  try {
    const response = await fetch(`/api/tma/app/feed/${taskId}/action`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ action }),
    });
    if (!response.ok) {
      throw new Error("feed action failed");
    }
    await loadFeed();
  } catch {
    setStatus("Действие доступно внутри Telegram.");
    render();
  }
}

async function loadPackages({ quiet = false } = {}) {
  if (state.packagesLoading) {
    return;
  }
  if (!quiet) {
    state.packagesLoading = true;
    render();
  }
  try {
    const response = await fetch("/api/tma/app/packages", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error("packages failed");
    }
    const payload = await response.json();
    tariffs = Array.isArray(payload.items) ? payload.items.map(normalizeTariff) : [];
    state.customCreditRate = Number(payload.custom_credit_price_rub) || state.customCreditRate;
    state.customCreditMin = Number(payload.custom_credit_min) || state.customCreditMin;
    state.customCreditMax = Number(payload.custom_credit_max) || state.customCreditMax;
    if (!tariffs.find((item) => item.id === state.selectedTariff)) {
      state.selectedTariff = tariffs[0]?.id || "";
    }
  } catch {
    setStatus("Не удалось загрузить пакеты.");
  } finally {
    state.packagesLoading = false;
    render();
  }
}

function openPaymentUrl(url) {
  if (telegramApp?.openLink) {
    telegramApp.openLink(url);
    return;
  }
  window.location.href = url;
}

async function createPaymentRequest(body, messages = {}) {
  if (state.paymentLoading) {
    return;
  }
  if (!telegramInitData()) {
    setStatus("Откройте Mini App внутри Telegram для оплаты.");
    render();
    return;
  }

  state.paymentLoading = true;
  render();
  try {
    const response = await fetch("/api/tma/app/payments", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) {
        throw new Error("telegram_required");
      }
      if (response.status === 404) {
        throw new Error("package_unavailable");
      }
      if (response.status === 400) {
        throw new Error("invalid_payment_request");
      }
      throw new Error(String(payload.detail || "payment_failed"));
    }
    if (payload.payment_url) {
      setStatus("Открываю оплату...");
      openPaymentUrl(String(payload.payment_url));
      return;
    }
    if (payload.status === "manual_pending") {
      setStatus("Заявка создана. Администратор подтвердит оплату.");
      return;
    }
    setStatus("Платеж создан, но ссылка не вернулась.");
  } catch (error) {
    if (error instanceof Error && error.message === "telegram_required") {
      setStatus("Оплата доступна внутри Telegram.");
    } else if (error instanceof Error && error.message === "invalid_payment_request") {
      setStatus(messages.invalid || "Проверьте количество кредитов.");
    } else if (error instanceof Error && error.message === "package_unavailable") {
      setStatus(messages.unavailable || "Пакет недоступен. Обновите список тарифов.");
    } else {
      setStatus("Не удалось создать оплату. Попробуйте позже.");
    }
  } finally {
    state.paymentLoading = false;
    render();
  }
}

async function createPayment() {
  const selected = tariffs.find((item) => item.id === state.selectedTariff) || tariffs[0] || null;
  if (!selected) {
    setStatus("Выберите пакет для оплаты.");
    render();
    return;
  }
  await createPaymentRequest(
    { package_id: Number(selected.id) || selected.id },
    { unavailable: "Пакет недоступен. Обновите список тарифов." },
  );
}

async function createCustomPayment() {
  const credits = customCreditCount();
  if (credits < state.customCreditMin || credits > state.customCreditMax) {
    setStatus(`Введите от ${state.customCreditMin} до ${state.customCreditMax} кредитов.`);
    render();
    return;
  }
  await createPaymentRequest(
    { credits },
    {
      invalid: `Введите от ${state.customCreditMin} до ${state.customCreditMax} кредитов.`,
      unavailable: "Не удалось создать заявку. Попробуйте позже.",
    },
  );
}

function customCreditCount() {
  const value = Number(String(state.customCredits || "").replace(/\D/g, ""));
  return Number.isFinite(value) ? Math.floor(value) : 0;
}

function customCreditTotal() {
  return customCreditCount() * state.customCreditRate;
}

function formatRub(value) {
  return `${Number(value || 0).toLocaleString("ru-RU")} ₽`;
}

function normalizeTariff(item) {
  const id = String(item.id || item.code || "");
  const title = String(item.title || "Пакет");
  const contents = String(item.amount_text || packageContents(item));
  const isUnlimited = Boolean(item.is_unlimited);
  const hasVideo = Number(item.video_credits) > 0;
  return {
    id,
    code: String(item.code || id),
    title,
    icon: isUnlimited ? "crown" : hasVideo ? "rocket" : "sprout",
    contents,
    price: Number(item.price_rub) || 0,
    tag: isUnlimited ? "30 дней" : hasVideo ? "video" : "",
  };
}

function packageContents(item) {
  const parts = [];
  if (item.is_unlimited) {
    parts.push(item.duration_days ? `Безлимит ${item.duration_days} д.` : "Безлимит");
  }
  if (Number(item.photo_credits) > 0) {
    parts.push(`${Number(item.photo_credits)} фото`);
  }
  if (Number(item.video_credits) > 0) {
    parts.push(`${Number(item.video_credits)} видео`);
  }
  if (Number(item.credits) > 0) {
    parts.push(`${Number(item.credits)} унив.`);
  }
  return parts.join(" · ") || "Пакет";
}

function renderAppShell(content) {
  return `
    <div class="top-strip">
      <div class="community">BANANA mini app</div>
      <button class="balance-pill" type="button" data-tab="packages">${currentBalance()} ✦</button>
    </div>
    ${state.status && !state.sheetOpen ? `<p class="app-status">${escapeHtml(state.status)}</p>` : ""}
    ${content}
    ${renderNav()}
    ${state.sheetOpen ? renderSheet() : ""}
  `;
}

function renderHome() {
  return renderAppShell(`
    <section class="hero-card">
      <img class="hero-media" src="assets/banana-currency.png?v=20260611-riot1" alt="" />
      <div class="hero-copy">
        <h2>BANANA</h2>
        <p>В один клик · трендовые фото и видео</p>
      </div>
    </section>

    <section>
      <div class="section-row">
        <div>
          <h2>Тренды</h2>
          <p class="subtle">Актуальные стили и визуальные направления</p>
        </div>
        <button class="ghost-pill" type="button" data-action="toggle-trends">
          ${state.trendsCollapsed ? "Показать v" : "Скрыть ^"}
        </button>
      </div>
      ${
        state.trendsCollapsed
          ? ""
          : `<div class="trend-grid">${trends.map(renderTrendCard).join("")}</div>`
      }
    </section>
  `);
}

function renderTrendCard(item) {
  return `
    <button class="trend-card" type="button" data-trend="${escapeHtml(item.id)}" style="--trend-bg: ${item.bg};">
      <img src="${escapeHtml(item.image)}" alt="" loading="eager" decoding="sync" />
      ${item.badge ? `<span class="badge">${escapeHtml(item.badge)}</span>` : "<span></span>"}
      <strong>${escapeHtml(item.title)}</strong>
    </button>
  `;
}

function renderCreate() {
  return renderAppShell(`
    <header class="page-head">
      <div class="page-title">
        <h1>Создать</h1>
        <p class="subtle">Выберите формат генерации</p>
      </div>
    </header>
    <section class="create-grid">
      <button class="mode-tile" style="--tile-a:#a600ff;--tile-b:#651dff" type="button" data-open-kind="image">
        <div>
          <span>▧</span>
          <strong>Картинка</strong>
        </div>
      </button>
      <button class="mode-tile" style="--tile-a:#1558ff;--tile-b:#243dff" type="button" data-open-kind="motion">
        <div>
          <span>▣</span>
          <strong>Видео</strong>
        </div>
      </button>
      <button class="mode-tile is-wide" style="--tile-a:#ff3f9f;--tile-b:#243dff" type="button" data-open-kind="motion-control">
        <div>
          <span>◈</span>
          <strong>Motion Control</strong>
        </div>
      </button>
    </section>
  `);
}

function renderFeed() {
  return renderAppShell(`
    <header class="page-head feed-head">
      <div class="page-title">
        <h1>BANANA</h1>
      </div>
      <div class="feed-head-actions">
        <button class="ghost-pill" type="button" data-action="refresh-feed">🔄</button>
      </div>
    </header>
    <section class="feed-grid">
      ${state.feedLoading ? '<p class="subtle">Загрузка...</p>' : ""}
      ${state.feedItems.length ? state.feedItems.map(renderFeedItem).join("") : renderEmptyFeed()}
    </section>
  `);
}

function renderFeedItem(item) {
  const media = item.media_type === "video"
    ? `<video src="${escapeHtml(item.media_url)}" muted playsinline loop class="feed-img"></video>`
    : `<img src="${escapeHtml(item.media_url)}" alt="" loading="lazy" class="feed-img" />`;
  const promptText = item.prompt || "";
  const authorName = escapeHtml(item.author || "BANANA user");
  const initials = authorName.charAt(0).toUpperCase();
  return `
    <article class="feed-card">
      <div class="feed-card-header">
        <div class="feed-avatar">${initials}</div>
        <span class="feed-author">${authorName}</span>
      </div>
      <div class="feed-media">${media}</div>
      <div class="feed-card-footer">
        <div class="feed-actions">
          <button type="button" data-feed-action="like" data-feed-id="${Number(item.id)}">
            <span class="action-icon">${item.liked_by_me ? "❤️" : "🤍"}</span>
            <span>${Number(item.likes) || 0}</span>
          </button>
          <button type="button" data-feed-action="share" data-feed-id="${Number(item.id)}">
            <span class="action-icon">📤</span>
            <span>${Number(item.shares) || 0}</span>
          </button>
          <button type="button" data-feed-repeat="${Number(item.id)}" class="repeat-btn">
            🔄 Повторить
          </button>
        </div>
        ${promptText ? `<div class="feed-caption"><strong>${authorName}</strong> ${escapeHtml(promptText)}</div>` : ""}
        <div class="feed-model-tag">${escapeHtml(item.model_code || "model")}</div>
      </div>
    </article>
  `;
}

function renderEmptyFeed() {
  return `
    <div class="empty-state feed-empty">
      <div class="empty-icon">📸</div>
      <h3>Пока пусто</h3>
      <p class="subtle">Здесь появятся публичные работы.<br>Опубликуйте свою генерацию из Telegram-бота.</p>
    </div>
  `;
}

function renderPackages() {
  const selected = tariffs.find((item) => item.id === state.selectedTariff) || tariffs[0] || null;
  return renderAppShell(`
    <header class="page-head">
      <div class="page-title">
        <h1>Выберите тариф</h1>
      </div>
    </header>
    <section class="tariff-list">
      <div class="custom-credit-panel">
        <div>
          <h3>Свое количество</h3>
          <p class="subtle">1 универсальный кредит = ${formatRub(state.customCreditRate)}</p>
        </div>
        <label class="custom-credit-field">
          <span>Кредиты</span>
          <input
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            data-custom-credits
            value="${escapeHtml(state.customCredits)}"
            aria-label="Количество кредитов"
          />
        </label>
        <div class="custom-credit-total">
          <span>Итого</span>
          <strong data-custom-total>${formatRub(customCreditTotal())}</strong>
        </div>
        <button class="pay-button" type="button" data-action="pay-custom" ${state.paymentLoading ? "disabled" : ""}>
          ${state.paymentLoading ? "Создаю оплату..." : "Купить кредиты"}
        </button>
      </div>
      ${state.packagesLoading ? '<p class="subtle">Загрузка...</p>' : ""}
      ${tariffs.map(renderTariffCard).join("")}
      ${!state.packagesLoading && !tariffs.length ? renderEmptyPackages() : ""}
      ${
        selected
          ? `<button class="pay-button" type="button" data-action="pay" ${state.paymentLoading ? "disabled" : ""}>${
              state.paymentLoading ? "Создаю оплату..." : `Пополнить: ${escapeHtml(selected.title)}`
            }</button>`
          : ""
      }
    </section>
  `);
}

function renderTariffCard(item) {
  const selected = item.id === state.selectedTariff;
  return `
    <button class="tariff-card ${selected ? "is-selected" : ""}" type="button" data-tariff="${escapeHtml(item.id)}">
      ${item.tag ? `<span class="hit">${escapeHtml(item.tag)}</span>` : ""}
      <div>
        <h3>${escapeHtml(item.title)} ${tariffIcon(item.icon)}</h3>
        <strong>${escapeHtml(item.contents)}</strong>
      </div>
      <div class="price">${item.price} ₽</div>
    </button>
  `;
}

function renderEmptyPackages() {
  return `
    <div class="empty-state">
      <h3>Пакеты не настроены</h3>
      <p class="subtle">Попробуйте обновить экран позже.</p>
    </div>
  `;
}

function tariffIcon(icon) {
  const icons = { sprout: "↗", rocket: "▲", bolt: "⚡", crown: "♕" };
  return icons[icon] || "";
}

function renderHistory() {
  const remoteItems = state.taskItems;
  const localItems = loadHistory();
  return renderAppShell(`
    <header class="page-head">
      <div class="page-title">
        <h1>История генераций</h1>
      </div>
      <button class="ghost-pill" type="button" data-action="refresh-history">Обновить</button>
    </header>
    <section class="history-stack">
      <div class="history-note">
        <span>i</span>
        <p>${state.taskStreamReady ? "Результаты приходят сюда автоматически." : "История обновляется при открытии и по кнопке."}</p>
      </div>
      <div class="section-row">
        <h3>${remoteItems.length || localItems.length || 0} генерация</h3>
      </div>
      ${state.taskLoading ? '<p class="subtle">Загрузка...</p>' : ""}
      ${remoteItems.length ? remoteItems.map(renderTaskItem).join("") : localItems.map(renderHistoryItem).join("")}
      ${!remoteItems.length && !localItems.length ? renderEmptyHistory() : ""}
      ${localItems.length && !remoteItems.length ? '<button class="danger-button" type="button" data-action="clear-history">Очистить историю</button>' : ""}
    </section>
  `);
}

function renderTaskItem(item) {
  const statusText = {
    submitted: "Отправлено",
    waiting: "В очереди",
    queuing: "В очереди",
    generating: "Создается",
    success: "Готово",
    fail: "Ошибка",
  }[item.status] || item.status || "Статус";
  const media = item.media_url
    ? item.media_type === "video"
      ? `<video src="${escapeHtml(item.media_url)}" muted playsinline controls></video>`
      : `<img src="${escapeHtml(item.media_url)}" alt="" loading="lazy" />`
    : "";
  return `
    <article class="history-card result-card">
      <div class="history-thumb result-thumb">${media}</div>
      <div>
        <h3>${escapeHtml(item.media_type === "video" ? "Видео" : "Изображение")} #${Number(item.id) || ""}</h3>
        <small>${escapeHtml(formatTaskDate(item.created_at))} · ${escapeHtml(item.model_code || "model")}</small>
        <p>${escapeHtml(item.prompt || "без prompt")}</p>
        <span class="status-ready ${item.status === "fail" ? "is-error" : ""}">${escapeHtml(statusText)}</span>
      </div>
      <strong>${Number(item.cost_credits) || 0} ✦</strong>
    </article>
  `;
}

function renderHistoryItem(item) {
  return `
    <article class="history-card">
      <div class="history-thumb" aria-hidden="true"></div>
      <div>
        <h3>${escapeHtml(item.title)}</h3>
        <small>${escapeHtml(item.createdAt)} · ${escapeHtml(item.model)}</small>
        <span class="status-ready">${escapeHtml(item.status)}</span>
      </div>
      <strong>${Number(item.cost) || 0} ✦</strong>
    </article>
  `;
}

function formatTaskDate(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderEmptyHistory() {
  return `
    <div class="empty-state">
      <h3>Пока пусто</h3>
      <p class="subtle">После первой генерации карточка появится здесь.</p>
    </div>
  `;
}

function renderProfile() {
  const user = telegramUser();
  const name = user.first_name || user.username || "BANANA";
  const id = user.id || "1350766405";
  const ref = `t.me/${BOT_USERNAME}?start=${id}`;
  return renderAppShell(`
    <section class="profile-head">
      <div class="avatar">${escapeHtml(String(name).slice(0, 1).toUpperCase())}</div>
      <div>
        <h2>${escapeHtml(name)}</h2>
        <p class="subtle">ID: ${escapeHtml(id)}</p>
      </div>
    </section>
    <section class="profile-stack">
      <article class="info-card lime">
        <h2>Реферальная программа</h2>
        <p>Получайте 30% с покупок приглашенных пользователей. Амбасадоры получают 50%.</p>
        <p style="margin-top:10px">Хотите стать амбасадором? Напишите в поддержку.</p>
        <div class="ref-row">
          <code>${escapeHtml(ref)}</code>
          <button class="copy-button" type="button" data-copy="${escapeHtml(ref)}">□</button>
        </div>
      </article>
      <article class="info-card">
        <h2>Настройки</h2>
        <button class="danger-button" type="button" data-action="subscription">Отмена подписок</button>
        <p class="subtle" style="margin-top:10px">Управление подписками и их отмена</p>
      </article>
      <article class="info-card">
        <h2>История платежей</h2>
        <p class="subtle">Платежи и заявки на пополнение отображаются в Telegram-боте.</p>
      </article>
    </section>
  `);
}

function renderNav() {
  const items = [
    ["home", "⌂", "Главная"],
    ["packages", "▭", "Пополнить"],
    ["create", "✧", ""],
    ["feed", "◎", "Лента"],
    ["history", "◷", "История"],
    ["profile", "♙", "Профиль"],
  ];
  return `
    <nav class="bottom-nav" aria-label="Навигация">
      ${items
        .map(([tab, icon, label]) => {
          const active = state.tab === tab;
          const fab = tab === "create";
          return `
            <button class="nav-button ${active ? "is-active" : ""} ${fab ? "fab" : ""}" type="button" data-tab="${tab}" aria-label="${escapeHtml(label || "Создать")}">
              <span>${icon}</span>
              ${label ? `<small>${escapeHtml(label)}</small>` : "<small>&nbsp;</small>"}
            </button>
          `;
        })
        .join("")}
    </nav>
  `;
}

function generatorTitle() {
  if (state.template?.title) {
    return state.template.title;
  }
  if (state.flow === "motion-control") {
    return "Motion Control";
  }
  return state.kind === "motion" ? "Создайте видео" : "Создать изображение";
}

function generatorSubtitle() {
  if (state.template) {
    return "Генерация по трендовому шаблону";
  }
  if (state.flow === "motion-control") {
    return "Kling с изображением персонажа и видео-референсом движения";
  }
  return "Опишите идею — потом пришлите референсы в чат";
}

function renderMediaHandoff() {
  if (state.flow === "motion-control") {
    return `
      <div class="handoff-card">
        <span class="upload-icon">◈</span>
        <strong>Motion Control</strong>
        <small>После брифа бот попросит изображение персонажа, затем видео-референс движения 3-30 сек.</small>
      </div>
    `;
  }
  if (state.kind === "motion") {
    return `
      <div class="handoff-card">
        <span class="upload-icon">▣</span>
        <strong>Видео-референс</strong>
        <small>После брифа бот попросит изображение для видео и длительность.</small>
      </div>
    `;
  }
  const maxImages = currentModelMeta().maxImages || currentModel().maxImages || 1;
  return `
    <div class="handoff-card">
      <span class="upload-icon">▧</span>
      <strong>Фото-референс</strong>
      <small>Шаг 1: отправьте бриф. Шаг 2: в чате Telegram пришлите ${escapeHtml(referenceLimitText(maxImages))}. Генерация стартует после референсов.</small>
    </div>
  `;
}

function renderSheet() {
  const modelMeta = currentModelMeta();
  const title = generatorTitle();
  const subtitle = generatorSubtitle();
  return `
    <div class="sheet-backdrop" data-action="close-sheet-bg">
      <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetTitle">
        <div class="sheet-handle"></div>
        <header class="sheet-head">
          <div>
            <h2 id="sheetTitle">${escapeHtml(title)}</h2>
            <p class="subtle">${escapeHtml(subtitle)}</p>
          </div>
          <button class="icon-button" type="button" data-action="close-sheet" aria-label="Закрыть">×</button>
        </header>
        <div class="form-stack">
          ${renderMediaHandoff()}
          <label class="field-label">
            Модель ИИ
            <span class="select-wrap">
              <button class="select-button" type="button" data-action="toggle-models">
                <span>⚙ ${escapeHtml(modelMeta.title)}</span>
                <span>${state.modelMenuOpen ? "^" : "⌄"}</span>
              </button>
              ${state.modelMenuOpen ? renderModelMenu() : ""}
            </span>
          </label>
          <label class="field-label">
            Подсказка
            <textarea id="promptInput" maxlength="1500" placeholder="Футуристический киберпанк-город с неоновыми огнями...">${escapeHtml(state.prompt)}</textarea>
          </label>
          <p class="field-error">${escapeHtml(state.status)}</p>
          <div class="sheet-actions">
            <button class="primary-button" type="button" data-action="send-generation">✧ Далее: отправить референсы (${displayPriceText()})</button>
            ${state.template ? '<button class="outline-button" type="button" data-action="back-to-template">← Назад к шаблону</button>' : ""}
          </div>
        </div>
      </section>
    </div>
  `;
}

function renderFileList() {
  if (!state.filePreviews.length) {
    return "";
  }
  return `
    <div class="preview-grid" aria-label="Выбранные изображения">
      ${state.filePreviews
        .map(
          (file) => `
            <figure class="preview-card">
              <img src="${escapeHtml(file.url)}" alt="" />
              <figcaption>${escapeHtml(file.name)}</figcaption>
            </figure>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderModelMenu() {
  return `
    <div class="model-menu">
      ${currentModelList()
        .map((item) => {
          const active = item.code === state.modelCode;
          return `
            <button class="model-option ${active ? "is-active" : ""}" type="button" data-model="${escapeHtml(item.code)}">
              <span>${escapeHtml(item.title)}</span>
              <small>${item.displayPrice} кр.${item.unit || ""}${item.quality ? ` · ${escapeHtml(item.quality)}` : ""}${item.maxImages ? ` · ${escapeHtml(referenceLimitText(item.maxImages))}` : ""}</small>
            </button>
          `;
        })
        .join("")}
    </div>
  `;
}

function render() {
  if (state.kind === "image" && !MODEL_CATALOG[state.modelCode]) {
    state.modelCode = DEFAULT_MODEL_BY_KIND.image;
  }
  if (state.kind === "motion" && !MODEL_CATALOG[state.modelCode]) {
    state.modelCode = DEFAULT_MODEL_BY_KIND.motion;
  }

  const pages = {
    home: renderHome,
    packages: renderPackages,
    create: renderCreate,
    feed: renderFeed,
    history: renderHistory,
    profile: renderProfile,
  };
  root.innerHTML = (pages[state.tab] || pages.home)();
  updateTelegramRunButton();
}

function bindGlobalEvents() {
  root.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) {
      return;
    }

    const tabButton = target.closest("[data-tab]");
    if (tabButton) {
      state.tab = tabButton.dataset.tab;
      state.sheetOpen = false;
      if (state.tab === "feed" && !state.feedItems.length) {
        loadFeed();
        return;
      }
      if (state.tab === "packages" && !tariffs.length) {
        loadPackages();
        return;
      }
      if (state.tab === "history") {
        loadTasks();
        return;
      }
      render();
      return;
    }

    const trendButton = target.closest("[data-trend]");
    if (trendButton) {
      const trend = trends.find((item) => item.id === trendButton.dataset.trend);
      if (trend) {
        openGenerator({ kind: trend.kind, prompt: trend.prompt, template: trend });
      }
      return;
    }

    const openKind = target.closest("[data-open-kind]");
    if (openKind) {
      openGenerator({ kind: openKind.dataset.openKind });
      return;
    }

    const tariffButton = target.closest("[data-tariff]");
    if (tariffButton) {
      state.selectedTariff = tariffButton.dataset.tariff;
      render();
      return;
    }

    const modelButton = target.closest("[data-model]");
    if (modelButton) {
      state.modelCode = modelButton.dataset.model;
      state.modelMenuOpen = false;
      render();
      return;
    }

    const copyButton = target.closest("[data-copy]");
    if (copyButton) {
      copyText(copyButton.dataset.copy || "").then(() => {
        setStatus("Ссылка скопирована.");
        render();
      });
      return;
    }

    const actionButton = target.closest("[data-action]");
    if (!actionButton) {
      return;
    }

    const action = actionButton.dataset.action;
    if (action === "close-sheet") {
      closeGenerator();
    } else if (action === "close-sheet-bg" && target.classList.contains("sheet-backdrop")) {
      closeGenerator();
    } else if (action === "toggle-models") {
      state.modelMenuOpen = !state.modelMenuOpen;
      render();
    } else if (action === "toggle-trends") {
      state.trendsCollapsed = !state.trendsCollapsed;
      render();
    } else if (action === "send-generation") {
      sendGeneration();
    } else if (action === "back-to-template") {
      closeGenerator();
    } else if (action === "pay") {
      createPayment();
    } else if (action === "pay-custom") {
      createCustomPayment();
    } else if (action === "clear-history") {
      saveHistory([]);
      render();
    } else if (action === "refresh-history") {
      loadTasks();
    } else if (action === "refresh-feed") {
      loadFeed();
    } else if (action === "subscription") {
      setStatus("Управление подписками доступно через Telegram-бота.");
      render();
    }
  });

  root.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || !target.matches("[data-custom-credits]")) {
      return;
    }
    state.customCredits = target.value.replace(/\D/g, "");
    target.value = state.customCredits;
    const total = root.querySelector("[data-custom-total]");
    if (total) {
      total.textContent = formatRub(customCreditTotal());
    }
  });

  root.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const feedButton = target?.closest("[data-feed-action]");
    if (feedButton) {
      feedAction(feedButton.dataset.feedId, feedButton.dataset.feedAction);
      return;
    }
    const repeatButton = target?.closest("[data-feed-repeat]");
    if (repeatButton) {
      const item = state.feedItems.find((row) => String(row.id) === String(repeatButton.dataset.feedRepeat));
      if (item) {
        openGenerator({
          kind: item.media_type === "video" ? "motion" : "image",
          prompt: item.prompt || "",
          modelCode: item.model_code || "",
          sourceFeedTaskId: Number(item.id) || null,
          template: { title: "Повтор из ленты" },
        });
      }
    }
  });

  root.addEventListener("input", (event) => {
    const target = event.target;
    if (target instanceof HTMLTextAreaElement && target.id === "promptInput") {
      state.prompt = truncate(target.value, 1500);
      updateTelegramRunButton();
    }

    if (target instanceof HTMLInputElement && target.id === "imageFiles") {
      clearFilePreviews();
      const files = Array.from(target.files || []).slice(0, 3);
      state.files = files.map((file) => file.name);
      state.filePreviews = files.map((file) => ({
        name: file.name,
        url: URL.createObjectURL(file),
      }));
      render();
    }
  });
}

function boot() {
  setViewportHeight(telegramApp);
  bootTelegram(telegramApp, {
    onMainButtonClick: () => {
      if (state.sheetOpen) {
        sendGeneration();
      } else {
        state.tab = "create";
        render();
      }
    },
    onViewportChanged: () => setViewportHeight(telegramApp),
  });
  bindGlobalEvents();
  loadTasks({ quiet: true });
  connectTaskStream();
  window.setInterval(() => loadTasks({ quiet: true }), 15000);
  render();
}

boot();
