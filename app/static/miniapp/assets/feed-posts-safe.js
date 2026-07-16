const root = document.querySelector("#app");
const telegramApp = window.Telegram?.WebApp || null;

let requestedPostId = readRequestedPostId();
let openingFeed = false;
let enhanceQueued = false;

function readRequestedPostId() {
  const raw = new URLSearchParams(window.location.search).get("post") || "";
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : 0;
}

function canonicalPostUrl(taskId) {
  const url = new URL(window.location.href);
  url.hash = "";
  url.search = "";
  url.searchParams.set("post", String(Number(taskId)));
  return url.toString();
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

function showStatus(message) {
  const existing = root?.querySelector(".feed-post-toast");
  existing?.remove();
  const toast = document.createElement("div");
  toast.className = "feed-post-toast";
  toast.textContent = message;
  root?.appendChild(toast);
  window.setTimeout(() => toast.remove(), 2600);
}

async function sharePost(taskId) {
  const url = canonicalPostUrl(taskId);
  try {
    if (navigator.share) {
      await navigator.share({
        title: "Пост в BANANA",
        text: "Посмотрите эту работу в ленте BANANA",
        url,
      });
      return;
    }
    await copyText(url);
    showStatus("Ссылка на пост скопирована.");
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return;
    }
    try {
      await copyText(url);
      showStatus("Ссылка на пост скопирована.");
    } catch {
      showStatus("Не удалось скопировать ссылку.");
    }
  }
}

function protectCommunityPrompt(card) {
  const body = card.querySelector(".community-card-body");
  const prompt = body?.querySelector(":scope > p");
  if (prompt && prompt.dataset.promptProtected !== "true") {
    prompt.dataset.promptProtected = "true";
    prompt.classList.add("community-protected-prompt");
    prompt.textContent = "Промпт автора скрыт и защищён.";
  }
  const repeat = body?.querySelector("[data-feed-repeat]");
  if (repeat && repeat.dataset.privateRepeat !== "true") {
    repeat.dataset.privateRepeat = "true";
    repeat.textContent = "Создать своё ↗";
    repeat.setAttribute("aria-label", "Создать свою работу с собственным промптом");
  }
}

function addCommunityPostLink(card) {
  const taskId = Number(card.dataset.communityCard || 0);
  const actions = card.querySelector(".community-card-actions");
  if (!taskId || !actions || actions.querySelector("[data-community-post-link]")) {
    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "community-link-button";
  button.dataset.communityPostLink = String(taskId);
  button.textContent = "Ссылка";
  button.setAttribute("aria-label", "Поделиться ссылкой на пост");
  actions.prepend(button);
}

function addAuthorWorkLink(card) {
  const repeat = card.querySelector("[data-feed-repeat]");
  const taskId = Number(repeat?.dataset.feedRepeat || 0);
  if (!taskId || card.querySelector("[data-community-post-link]")) {
    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "community-author-link";
  button.dataset.communityPostLink = String(taskId);
  button.textContent = "⌁";
  button.setAttribute("aria-label", "Ссылка на пост");
  card.appendChild(button);
}

function addLegacyPostLink(card) {
  const repeat = card.querySelector("[data-feed-repeat]");
  const taskId = Number(repeat?.dataset.feedRepeat || 0);
  const actions = card.querySelector(".feed-actions");
  if (!taskId || !actions || actions.querySelector("[data-community-post-link]")) {
    return;
  }
  card.querySelector(".feed-caption")?.remove();
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.communityPostLink = String(taskId);
  button.innerHTML = '<span class="action-icon">🔗</span><span>Ссылка</span>';
  actions.appendChild(button);
}

function permalinkBar(targetFound) {
  const bar = document.createElement("div");
  bar.className = "community-permalink-bar";
  bar.innerHTML = `
    <div>
      <span>ПРЯМАЯ ССЫЛКА</span>
      <strong>${targetFound ? "Открыт выбранный пост" : "Пост не найден среди доступных работ"}</strong>
    </div>
    <button type="button" data-community-all-posts>Вся лента</button>
  `;
  return bar;
}

function applyPermalinkMode(cards) {
  const grid = root?.querySelector(".community-feed-root");
  if (!grid) {
    return;
  }
  const existingBar = grid.querySelector(".community-permalink-bar");
  if (!requestedPostId) {
    existingBar?.remove();
    cards.forEach((card) => {
      if (card.hidden) {
        card.hidden = false;
      }
      card.classList.remove("is-permalink-post");
    });
    return;
  }
  if (!cards.length) {
    return;
  }

  const target = cards.find((card) => Number(card.dataset.communityCard || 0) === requestedPostId);
  cards.forEach((card) => {
    const shouldHide = Boolean(target) && card !== target;
    if (card.hidden !== shouldHide) {
      card.hidden = shouldHide;
    }
    card.classList.toggle("is-permalink-post", card === target);
  });

  const targetFound = Boolean(target);
  const statusText = targetFound
    ? "Открыт выбранный пост"
    : "Пост не найден среди доступных работ";
  if (existingBar) {
    const strong = existingBar.querySelector("strong");
    if (strong && strong.textContent !== statusText) {
      strong.textContent = statusText;
    }
    return;
  }

  const mosaic = grid.querySelector(".community-mosaic");
  const anchor = mosaic || grid.firstElementChild;
  if (anchor) {
    anchor.before(permalinkBar(targetFound));
  } else {
    grid.prepend(permalinkBar(targetFound));
  }
}

function clearPermalinkMode() {
  requestedPostId = 0;
  const url = new URL(window.location.href);
  url.searchParams.delete("post");
  window.history.replaceState({}, "", url);
  queueEnhance();
}

function ensureFeedOpened() {
  if (!requestedPostId || openingFeed || root?.querySelector(".feed-head")) {
    return;
  }
  const feedTab = root?.querySelector('[data-tab="feed"]');
  if (!feedTab) {
    return;
  }
  openingFeed = true;
  feedTab.click();
  window.setTimeout(() => {
    openingFeed = false;
    queueEnhance();
  }, 0);
}

function enhanceFeedPosts() {
  ensureFeedOpened();

  const communityCards = Array.from(root?.querySelectorAll(".community-card[data-community-card]") || []);
  communityCards.forEach((card) => {
    protectCommunityPrompt(card);
    addCommunityPostLink(card);
  });
  applyPermalinkMode(communityCards);

  const authorWorks = Array.from(root?.querySelectorAll(".community-author-work") || []);
  authorWorks.forEach(addAuthorWorkLink);

  const legacyCards = Array.from(root?.querySelectorAll(".feed-card") || []);
  legacyCards.forEach(addLegacyPostLink);
}

function queueEnhance() {
  if (enhanceQueued) {
    return;
  }
  enhanceQueued = true;
  window.requestAnimationFrame(() => {
    enhanceQueued = false;
    enhanceFeedPosts();
  });
}

root?.addEventListener(
  "click",
  (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) {
      return;
    }
    const link = target.closest("[data-community-post-link]");
    if (link) {
      event.preventDefault();
      event.stopPropagation();
      sharePost(link.dataset.communityPostLink);
      return;
    }
    if (target.closest("[data-community-all-posts]")) {
      event.preventDefault();
      clearPermalinkMode();
    }
  },
  true,
);

const observer = new MutationObserver(queueEnhance);
if (root) {
  observer.observe(root, { childList: true, subtree: true });
}

telegramApp?.ready?.();
queueEnhance();
