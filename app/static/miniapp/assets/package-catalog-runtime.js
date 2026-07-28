const PACKAGE_ENDPOINT = "/api/tma/app/packages";
const PAYMENT_ENDPOINT = "/api/tma/app/payments";
const root = document.querySelector("#app");
const telegramApp = window.Telegram?.WebApp || null;

let packages = [];
let selectedPackageId = "";
let confirmedSignature = "";
let paymentAttemptKey = "";
let loading = false;
let paymentLoading = false;
let statusMessage = "";
let refreshQueued = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function moneyParts(value) {
  const normalized = String(value ?? "0").trim().replace(",", ".");
  const match = normalized.match(/^(\d+)(?:\.(\d{1,2}))?$/);
  if (!match) {
    return { rubles: 0n, kopecks: "00", minor: 0n };
  }
  const rubles = BigInt(match[1]);
  const kopecks = String(match[2] || "").padEnd(2, "0").slice(0, 2);
  return {
    rubles,
    kopecks,
    minor: rubles * 100n + BigInt(kopecks),
  };
}

function formatRub(value) {
  const { rubles, kopecks } = moneyParts(value);
  const grouped = rubles.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return kopecks === "00" ? `${grouped} ₽` : `${grouped},${kopecks} ₽`;
}

function countValue(value) {
  const parsed = Number.parseInt(String(value ?? "0"), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function packageContents(item) {
  if (item.amount_text) {
    return String(item.amount_text);
  }
  const parts = [];
  if (item.is_unlimited) {
    const days = countValue(item.duration_days);
    parts.push(days ? `Безлимит ${days} д.` : "Безлимит");
  }
  const photo = countValue(item.photo_credits);
  const video = countValue(item.video_credits);
  const common = countValue(item.credits);
  if (photo) parts.push(`${photo} фото`);
  if (video) parts.push(`${video} видео`);
  if (common) parts.push(`${common} универсальных`);
  return parts.join(" · ") || "Пакет";
}

function normalizePackage(item) {
  const id = String(item.id ?? item.package_id ?? "");
  const price = String(item.price_rub ?? "0");
  if (!id || moneyParts(price).minor <= 0n) {
    return null;
  }
  return {
    id,
    code: String(item.code || id),
    title: String(item.title || "Пакет"),
    description: String(item.description || ""),
    terms: String(item.terms || ""),
    contents: packageContents(item),
    price,
    isUnlimited: Boolean(item.is_unlimited),
    hasVideo: countValue(item.video_credits) > 0,
  };
}

function packageSignature(item) {
  if (!item) return "";
  return JSON.stringify([
    item.id,
    item.code,
    item.title,
    item.description,
    item.terms,
    item.contents,
    item.price,
    item.isUnlimited,
  ]);
}

function newIdempotencyKey() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  const random = new Uint8Array(18);
  window.crypto?.getRandomValues?.(random);
  const entropy = Array.from(random, (value) => value.toString(16).padStart(2, "0")).join("");
  return `pay_${Date.now().toString(36)}_${entropy || Math.random().toString(36).slice(2)}`;
}

function resetPaymentAttempt() {
  paymentAttemptKey = newIdempotencyKey();
}

function packageSection() {
  return root?.querySelector(".tariff-list") || null;
}

function packagesVisible() {
  return Boolean(packageSection());
}

function setStatus(message) {
  statusMessage = String(message || "");
  renderPackageCatalog();
}

function renderPackageCard(item) {
  const selected = item.id === selectedPackageId;
  const tag = item.isUnlimited ? "подписка" : item.hasVideo ? "фото + видео" : "фото";
  return `
    <button class="tariff-card ${selected ? "is-selected" : ""}" type="button" data-tariff="${escapeHtml(item.id)}">
      <span class="hit">${escapeHtml(tag)}</span>
      <div>
        <h3>${escapeHtml(item.title)}</h3>
        <strong>${escapeHtml(item.contents)}</strong>
        ${item.description ? `<p class="subtle">${escapeHtml(item.description)}</p>` : ""}
      </div>
      <div class="price">${escapeHtml(formatRub(item.price))}</div>
    </button>
  `;
}

function renderPackageCatalog() {
  const section = packageSection();
  if (!section) return;

  const selected = packages.find((item) => item.id === selectedPackageId) || packages[0] || null;
  if (selected && selected.id !== selectedPackageId) {
    selectedPackageId = selected.id;
    confirmedSignature = packageSignature(selected);
    resetPaymentAttempt();
  }

  section.dataset.dynamicPackageCatalog = "1";
  section.innerHTML = `
    <div class="section-row">
      <p class="subtle">Цены и состав пакетов загружаются напрямую из админки.</p>
      <button class="ghost-pill" type="button" data-package-refresh ${loading ? "disabled" : ""}>↻</button>
    </div>
    ${statusMessage ? `<p class="app-status">${escapeHtml(statusMessage)}</p>` : ""}
    ${loading && !packages.length ? '<p class="subtle">Загрузка тарифов...</p>' : ""}
    ${packages.map(renderPackageCard).join("")}
    ${!loading && !packages.length ? `
      <div class="empty-state">
        <h3>Пакеты не настроены</h3>
        <p class="subtle">Активные пакеты с положительной ценой появятся после настройки администратором.</p>
      </div>
    ` : ""}
    ${selected ? `
      <button class="pay-button" type="button" data-action="pay" ${paymentLoading || loading ? "disabled" : ""}>
        ${paymentLoading ? "Создаю оплату..." : `Оплатить ${escapeHtml(formatRub(selected.price))}`}
      </button>
      ${selected.terms ? `<p class="subtle">${escapeHtml(selected.terms)}</p>` : ""}
    ` : ""}
  `;
}

async function fetchPackageCatalog({ forPayment = false, notifyChanges = false } = {}) {
  if (loading) {
    return { changed: false, package: packages.find((item) => item.id === selectedPackageId) || null };
  }
  loading = true;
  renderPackageCatalog();
  const previousSignature = confirmedSignature;

  try {
    const response = await fetch(PACKAGE_ENDPOINT, {
      method: "GET",
      cache: "no-store",
      headers: { Accept: "application/json", "Cache-Control": "no-cache" },
    });
    if (!response.ok) {
      throw new Error("package_catalog_unavailable");
    }
    const payload = await response.json();
    packages = (Array.isArray(payload.items) ? payload.items : [])
      .map(normalizePackage)
      .filter(Boolean);

    if (!packages.some((item) => item.id === selectedPackageId)) {
      selectedPackageId = packages[0]?.id || "";
    }
    const selected = packages.find((item) => item.id === selectedPackageId) || null;
    const currentSignature = packageSignature(selected);
    const changed = Boolean(previousSignature && currentSignature && previousSignature !== currentSignature);

    if (!confirmedSignature || !changed || !forPayment) {
      confirmedSignature = currentSignature;
    }
    if (changed) {
      resetPaymentAttempt();
    }
    if (changed && notifyChanges) {
      statusMessage = "Тариф обновлён администратором. Проверьте новую цену и состав.";
    }
    return { changed, package: selected };
  } catch {
    statusMessage = "Не удалось обновить тарифы. Проверьте подключение и повторите.";
    return { changed: false, package: null };
  } finally {
    loading = false;
    renderPackageCatalog();
  }
}

function authHeaders() {
  const headers = {
    Accept: "application/json",
    "Content-Type": "application/json",
    "X-Idempotency-Key": paymentAttemptKey || newIdempotencyKey(),
  };
  const initData = telegramApp?.initData || "";
  if (initData) {
    headers["X-Telegram-Init-Data"] = initData;
  }
  return headers;
}

function openPaymentUrl(url) {
  if (telegramApp?.openLink) {
    telegramApp.openLink(url);
  } else {
    window.location.href = url;
  }
}

async function createSelectedPackagePayment() {
  if (paymentLoading) return;
  if (!telegramApp?.initData) {
    setStatus("Оплата доступна только внутри Telegram.");
    return;
  }

  const refreshed = await fetchPackageCatalog({ forPayment: true });
  if (!refreshed.package) {
    setStatus("Пакет недоступен. Обновите список тарифов.");
    return;
  }
  if (refreshed.changed) {
    confirmedSignature = packageSignature(refreshed.package);
    resetPaymentAttempt();
    setStatus("Цена или состав изменились. Проверьте тариф и нажмите оплатить ещё раз.");
    return;
  }

  if (!paymentAttemptKey) resetPaymentAttempt();
  paymentLoading = true;
  statusMessage = "";
  renderPackageCatalog();
  try {
    const response = await fetch(PAYMENT_ENDPOINT, {
      method: "POST",
      cache: "no-store",
      headers: authHeaders(),
      body: JSON.stringify({ package_id: Number(refreshed.package.id) || refreshed.package.id }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 404) {
        await fetchPackageCatalog({ notifyChanges: true });
        resetPaymentAttempt();
        throw new Error("package_unavailable");
      }
      if (response.status === 409) {
        throw new Error("payment_in_progress");
      }
      if (response.status === 429) {
        throw new Error("payment_rate_limited");
      }
      throw new Error(String(payload.detail || "payment_failed"));
    }
    if (payload.payment_url) {
      statusMessage = "Открываю оплату...";
      renderPackageCatalog();
      openPaymentUrl(String(payload.payment_url));
      return;
    }
    statusMessage = payload.status === "manual_pending"
      ? "Заявка создана. Администратор подтвердит оплату."
      : "Платёж создан, но ссылка не вернулась.";
  } catch (error) {
    if (error instanceof Error && error.message === "package_unavailable") {
      statusMessage = "Пакет изменён или выключен. Выберите актуальный тариф.";
    } else if (error instanceof Error && error.message === "payment_in_progress") {
      statusMessage = "Этот платёж уже создаётся. Не нажимайте повторно.";
    } else if (error instanceof Error && error.message === "payment_rate_limited") {
      statusMessage = "Слишком много попыток оплаты. Повторите через минуту.";
    } else {
      statusMessage = "Не удалось создать оплату. Попробуйте позже.";
    }
  } finally {
    paymentLoading = false;
    renderPackageCatalog();
  }
}

function schedulePackageRefresh({ notifyChanges = false } = {}) {
  if (refreshQueued) return;
  refreshQueued = true;
  window.queueMicrotask(() => {
    refreshQueued = false;
    if (!packagesVisible()) return;
    if (packages.length) renderPackageCatalog();
    void fetchPackageCatalog({ notifyChanges });
  });
}

root?.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;

  const tab = target.closest('[data-tab="packages"]');
  if (tab) {
    window.setTimeout(() => schedulePackageRefresh({ notifyChanges: true }), 0);
    return;
  }

  if (!packagesVisible()) return;

  const refresh = target.closest("[data-package-refresh]");
  if (refresh) {
    event.preventDefault();
    event.stopImmediatePropagation();
    void fetchPackageCatalog({ notifyChanges: true });
    return;
  }

  const tariff = target.closest("[data-tariff]");
  if (tariff) {
    event.preventDefault();
    event.stopImmediatePropagation();
    selectedPackageId = String(tariff.getAttribute("data-tariff") || "");
    confirmedSignature = packageSignature(packages.find((item) => item.id === selectedPackageId));
    resetPaymentAttempt();
    statusMessage = "";
    renderPackageCatalog();
    return;
  }

  const action = target.closest("[data-action]")?.getAttribute("data-action");
  if (action === "pay") {
    event.preventDefault();
    event.stopImmediatePropagation();
    void createSelectedPackagePayment();
  } else if (action === "pay-custom") {
    event.preventDefault();
    event.stopImmediatePropagation();
    setStatus("Свободная покупка универсальных кредитов отключена. Выберите пакет администратора.");
  }
}, true);

const observer = new MutationObserver(() => {
  const section = packageSection();
  if (!section || section.dataset.dynamicPackageCatalog === "1") return;
  if (packages.length) renderPackageCatalog();
  schedulePackageRefresh({ notifyChanges: true });
});

if (root) {
  observer.observe(root, { childList: true, subtree: true });
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && packagesVisible()) {
    schedulePackageRefresh({ notifyChanges: true });
  }
});

window.addEventListener("focus", () => {
  if (packagesVisible()) schedulePackageRefresh({ notifyChanges: true });
});

resetPaymentAttempt();
schedulePackageRefresh();
