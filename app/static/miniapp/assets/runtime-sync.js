const telegramApp = window.Telegram?.WebApp || null;

function partnerCodeForTelegramId(value) {
  const numeric = Number(value);
  if (!Number.isSafeInteger(numeric) || numeric <= 0) return "";
  return `u${numeric.toString(36)}`;
}

function referralUrl() {
  const userId = telegramApp?.initDataUnsafe?.user?.id;
  const partnerCode = partnerCodeForTelegramId(userId);
  if (!partnerCode) return "";
  const currentCode = document.querySelector(".ref-row code")?.textContent?.trim() || "";
  const candidate = currentCode.startsWith("http") ? currentCode : `https://${currentCode}`;
  try {
    const url = new URL(candidate);
    url.searchParams.set("start", `ref_${partnerCode}`);
    return url.toString();
  } catch {
    return `https://t.me/eva_nana_bot?start=ref_${partnerCode}`;
  }
}

function stripStaticPrice(text) {
  return String(text || "")
    .replace(/^\s*\d+\s*кр\.(?:\/сек)?\s*(?:·\s*)?/i, "")
    .replace(/\s*\(\s*\d+\s*кр\.(?:\/сек)?\s*\)\s*/gi, " ")
    .replace(/\s*·\s*$/, "")
    .trim();
}

function patchRuntimeUi() {
  document.querySelectorAll(".custom-credit-panel").forEach((element) => element.remove());
  document.querySelectorAll(".tariff-card").forEach((element) => {
    if (/безлимит/i.test(element.textContent || "")) element.remove();
  });
  const balanceButton = document.querySelector(".balance-pill");
  if (balanceButton) balanceButton.textContent = "Пополнить";
  document.querySelectorAll(".model-option small").forEach((element) => {
    const clean = stripStaticPrice(element.textContent);
    element.textContent = clean || "Цена подтвердится в Telegram";
  });
  const submitButton = document.querySelector('[data-action="send-generation"]');
  if (submitButton) submitButton.textContent = "✧ Далее: отправить референсы";
  const ref = referralUrl();
  if (ref) {
    const code = document.querySelector(".ref-row code");
    const copyButton = document.querySelector(".ref-row [data-copy]");
    if (code) code.textContent = ref.replace(/^https:\/\//, "");
    if (copyButton) copyButton.dataset.copy = ref;
  }
  const mainButton = telegramApp?.MainButton;
  if (mainButton?.isVisible && /\d+\s*кр\./i.test(mainButton.text || "")) {
    mainButton.setText("Отправить бриф");
  }
}

let scheduled = false;
const observer = new MutationObserver(() => {
  if (scheduled) return;
  scheduled = true;
  window.requestAnimationFrame(() => {
    scheduled = false;
    patchRuntimeUi();
  });
});
observer.observe(document.documentElement, { childList: true, subtree: true });
patchRuntimeUi();
