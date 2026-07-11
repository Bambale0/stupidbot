export function getTelegramApp() {
  return window.Telegram?.WebApp || null;
}

export function hasTelegramInitData(telegramApp) {
  return Boolean(telegramApp?.initData);
}

export function setViewportHeight(telegramApp) {
  const height = telegramApp?.viewportStableHeight || window.innerHeight;
  document.documentElement.style.setProperty("--tg-viewport-height", `${height}px`);
}

export function bootTelegram(telegramApp, { onMainButtonClick, onViewportChanged }) {
  if (!telegramApp) {
    return;
  }

  telegramApp.ready();
  telegramApp.expand();
  if (typeof telegramApp.isVersionAtLeast !== "function" || telegramApp.isVersionAtLeast("6.1")) {
    telegramApp.setHeaderColor("#08070b");
    telegramApp.setBackgroundColor("#08070b");
  }
  telegramApp.MainButton?.onClick(onMainButtonClick);
  telegramApp.onEvent?.("viewportChanged", onViewportChanged);
}

export function updateMainButton(telegramApp, { text, enabled }) {
  if (!telegramApp?.MainButton) {
    return;
  }

  telegramApp.MainButton.setText(text);
  if (enabled) {
    telegramApp.MainButton.enable?.();
  } else {
    telegramApp.MainButton.disable?.();
  }
  telegramApp.MainButton.show();
}

export function sendTelegramData(telegramApp, payload) {
  if (!hasTelegramInitData(telegramApp) || typeof telegramApp.sendData !== "function") {
    return false;
  }

  telegramApp.HapticFeedback?.impactOccurred?.("medium");
  telegramApp.sendData(payload);
  return true;
}

export function closeTelegramApp(telegramApp) {
  if (!telegramApp?.close) {
    return false;
  }

  telegramApp.close();
  return true;
}
