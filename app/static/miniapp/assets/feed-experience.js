const root = document.querySelector("#app");
const telegramApp = window.Telegram?.WebApp || null;
const REACTIONS_KEY = "banana-community-reactions-v1";
const FEED_CACHE_TTL_MS = 30_000;

const community = {
  items: [],
  filter: "hot",
  profileKey: "",
  loading: false,
  requestId: 0,
  lastLoadedAt: 0,
};

let renderScheduled = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeMediaUrl(value) {
  const raw = String(value || "").trim();
  if (raw.startsWith("/") || raw.startsWith("https://") || raw.startsWith("http://")) {
    return escapeHtml(raw);
  }
  return "";
}

function authHeaders(extra = {}) {
  const headers = { Accept: "application/json", ...extra };
  const initData = telegramApp?.initData || "";
  if (initData) {
    headers["X-Telegram-Init-Data"] = initData;
  }
  return headers;
}

function reactionStorageKey() {
  const userId = telegramApp?.initDataUnsafe?.user?.id || "guest";
  return `${REACTIONS_KEY}:${userId}`;
}

function storedReactions() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(reactionStorageKey()) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function reactionFor(taskId) {
  return String(storedReactions()[String(taskId)] || "");
}

function storeReaction(taskId, reaction) {
  const reactions = storedReactions();
  if (reaction) {
    reactions[String(taskId)] = reaction;
  } else {
    delete reactions[String(taskId)];
  }
  window.localStorage.setItem(reactionStorageKey(), JSON.stringify(reactions));
}

function showCommunityStatus(message) {
  const existing = root?.querySelector(".community-toast");
  existing?.remove();
  const toast = document.createElement("div");
  toast.className = "community-toast";
  toast.textContent = message;
  root?.appendChild(toast);
  window.setTimeout(() => toast.remove(), 2800);
}

function scheduleEnhance() {
  if (renderScheduled) {
    return;
  }
  renderScheduled = true;
  window.requestAnimationFrame(() => {
    renderScheduled = false;
    enhanceFeedPage();
  });
}

function feedGrid() {
  return root?.querySelector(".feed-grid") || null;
}

function isFeedPage() {
  return Boolean(root?.querySelector(".feed-head") && feedGrid());
}

async function loadCommunityFeed({ quiet = false } = {}) {
  const requestId = ++community.requestId;
  if (!quiet) {
    community.loading = true;
    renderCommunity();
  }
  try {
    const response = await fetch("/api/tma/app/feed?limit=60", {
      headers: authHeaders(),
    });
    if (!response.ok) {
      throw new Error("feed_load_failed");
    }
    const payload = await response.json();
    if (requestId !== community.requestId) {
      return;
    }
    community.items = Array.isArray(payload.items) ? payload.items : [];
    community.lastLoadedAt = Date.now();
  } catch {
    if (!quiet) {
      showCommunityStatus("Не удалось загрузить сообщество.");
    }
  } finally {
    if (requestId === community.requestId) {
      community.loading = false;
      renderCommunity();
    }
  }
}

function enhanceFeedPage() {
  if (!isFeedPage()) {
    return;
  }
  const grid = feedGrid();
  if (!grid || grid.dataset.communityEnhanced === "true") {
    return;
  }
  grid.dataset.communityEnhanced = "true";
  grid.classList.add("community-feed-root");
  if (community.items.length) {
    renderCommunity();
    if (Date.now() - community.lastLoadedAt > FEED_CACHE_TTL_MS) {
      loadCommunityFeed({ quiet: true });
    }
  } else {
    loadCommunityFeed();
  }
}

function score(item) {
  const likes = Number(item.likes) || 0;
  const dislikes = Number(item.dislikes) || 0;
  const shares = Number(item.shares) || 0;
  return likes - dislikes + shares * 3;
}

function filteredItems() {
  const items = [...community.items];
  if (community.filter === "new") {
    return items.sort((a, b) => new Date(b.published_at || 0) - new Date(a.published_at || 0));
  }
  if (community.filter === "image") {
    return items.filter((item) => item.media_type !== "video");
  }
  if (community.filter === "video") {
    return items.filter((item) => item.media_type === "video");
  }
  return items.sort((a, b) => score(b) - score(a));
}

function authorProfile(item) {
  const profile = item?.author_profile && typeof item.author_profile === "object"
    ? item.author_profile
    : {};
  return {
    key: String(profile.key || item?.author_key || "banana-user"),
    name: String(profile.name || item?.author || "BANANA user"),
    username: profile.username || item?.author_username || "",
    works: Number(profile.works) || 0,
    likes: Number(profile.likes) || 0,
    dislikes: Number(profile.dislikes) || 0,
    initial: String(item?.author_initial || profile.name || item?.author || "B").slice(0, 1).toUpperCase(),
  };
}

function formatPublished(value) {
  if (!value) {
    return "сейчас";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "сейчас";
  }
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "short" });
}

function renderCommunity() {
  const grid = feedGrid();
  if (!grid || !isFeedPage()) {
    return;
  }
  grid.dataset.communityEnhanced = "true";
  grid.classList.add("community-feed-root");
  grid.innerHTML = community.profileKey ? renderAuthorPage() : renderCommunityGallery();
}

function renderCommunityGallery() {
  const items = filteredItems();
  return `
    <section class="community-hero">
      <div>
        <span class="community-kicker">BANANA COMMUNITY</span>
        <h2>Не лента.<br><em>Живая галерея.</em></h2>
        <p>Смотрите идеи, оценивайте работы и создавайте свою версию в один тап.</p>
      </div>
      <div class="community-orbit" aria-hidden="true">
        <span>✦</span><span>◉</span><span>↗</span>
      </div>
    </section>
    <div class="community-filters" role="tablist" aria-label="Фильтры ленты">
      ${filterButton("hot", "Для вас", "✦")}
      ${filterButton("new", "Новое", "◷")}
      ${filterButton("image", "Фото", "▧")}
      ${filterButton("video", "Видео", "▶")}
    </div>
    ${community.loading ? '<div class="community-loading"><span></span><p>Собираю лучшие работы…</p></div>' : ""}
    ${!community.loading && items.length ? `<div class="community-mosaic">${items.map(renderCommunityCard).join("")}</div>` : ""}
    ${!community.loading && !items.length ? renderCommunityEmpty() : ""}
  `;
}

function filterButton(value, label, icon) {
  return `
    <button class="community-filter ${community.filter === value ? "is-active" : ""}"
      type="button" data-community-filter="${value}" role="tab"
      aria-selected="${community.filter === value}">
      <span>${icon}</span>${label}
    </button>
  `;
}

function cardClass(item, index) {
  if (index === 0) {
    return "is-spotlight";
  }
  if (item.media_type === "video") {
    return index % 4 === 0 ? "is-wide is-video" : "is-video";
  }
  if (index % 5 === 2) {
    return "is-tall";
  }
  return "";
}

function renderCommunityCard(item, index) {
  const profile = authorProfile(item);
  const mediaUrl = safeMediaUrl(item.media_url);
  const reaction = reactionFor(item.id);
  const media = item.media_type === "video"
    ? `<video src="${mediaUrl}" muted playsinline loop autoplay preload="metadata"></video>`
    : `<img src="${mediaUrl}" alt="Работа ${escapeHtml(profile.name)}" loading="lazy" decoding="async" />`;
  const prompt = String(item.prompt || "").trim();
  return `
    <article class="community-card ${cardClass(item, index)}" data-community-card="${Number(item.id)}">
      <div class="community-media">
        ${mediaUrl ? media : '<div class="community-media-missing">✦</div>'}
        <div class="community-media-shade"></div>
        <button class="community-author" type="button" data-community-author="${escapeHtml(profile.key)}">
          <span class="community-avatar" style="--avatar-seed:${Number(item.id) % 360}deg">${escapeHtml(profile.initial)}</span>
          <span><strong>${escapeHtml(profile.name)}</strong><small>${formatPublished(item.published_at)}</small></span>
        </button>
        <div class="community-reactions" aria-label="Оценки работы">
          <button class="community-reaction ${reaction === "like" ? "is-active is-like" : ""}"
            type="button" data-community-reaction="like" data-community-id="${Number(item.id)}"
            aria-label="Нравится">
            <span>♥</span><b>${Number(item.likes) || 0}</b>
          </button>
          <button class="community-reaction ${reaction === "dislike" ? "is-active is-dislike" : ""}"
            type="button" data-community-reaction="dislike" data-community-id="${Number(item.id)}"
            aria-label="Не нравится">
            <span>↓</span><b>${Number(item.dislikes) || 0}</b>
          </button>
        </div>
        ${item.media_type === "video" ? '<span class="community-video-badge">▶ VIDEO</span>' : ""}
      </div>
      <div class="community-card-body">
        <div class="community-card-meta">
          <span>${escapeHtml(item.model_code || "AI model")}</span>
          <span>score ${score(item)}</span>
        </div>
        ${prompt ? `<p>${escapeHtml(prompt)}</p>` : '<p class="is-muted">Автор оставил идею загадкой.</p>'}
        <div class="community-card-actions">
          <button type="button" class="community-profile-button" data-community-author="${escapeHtml(profile.key)}">Профиль</button>
          <button type="button" class="community-repeat-button" data-feed-repeat="${Number(item.id)}">Создать свою ↗</button>
        </div>
      </div>
    </article>
  `;
}

function renderAuthorPage() {
  const works = community.items.filter(
    (item) => authorProfile(item).key === community.profileKey,
  );
  const profile = authorProfile(works[0]);
  if (!works.length) {
    community.profileKey = "";
    return renderCommunityGallery();
  }
  return `
    <section class="community-profile-page">
      <button class="community-back" type="button" data-community-back>← В сообщество</button>
      <header class="community-profile-cover">
        <div class="community-profile-glow"></div>
        <span class="community-profile-avatar">${escapeHtml(profile.initial)}</span>
        <div>
          <span class="community-kicker">CREATOR PROFILE</span>
          <h2>${escapeHtml(profile.name)}</h2>
          ${profile.username ? `<p>@${escapeHtml(String(profile.username).replace(/^@/, ""))}</p>` : ""}
        </div>
      </header>
      <div class="community-profile-stats">
        <article><strong>${Math.max(profile.works, works.length)}</strong><span>работ</span></article>
        <article><strong>${profile.likes}</strong><span>лайков</span></article>
        <article><strong>${profile.dislikes}</strong><span>дизлайков</span></article>
      </div>
      <div class="community-profile-title">
        <h3>Работы автора</h3>
        <span>${works.length}</span>
      </div>
      <div class="community-author-grid">
        ${works.map(renderAuthorWork).join("")}
      </div>
    </section>
  `;
}

function renderAuthorWork(item) {
  const mediaUrl = safeMediaUrl(item.media_url);
  const media = item.media_type === "video"
    ? `<video src="${mediaUrl}" muted playsinline loop autoplay preload="metadata"></video>`
    : `<img src="${mediaUrl}" alt="" loading="lazy" />`;
  return `
    <article class="community-author-work">
      ${mediaUrl ? media : '<div class="community-media-missing">✦</div>'}
      <div><span>♥ ${Number(item.likes) || 0}</span><span>↓ ${Number(item.dislikes) || 0}</span></div>
      <button type="button" data-feed-repeat="${Number(item.id)}" aria-label="Повторить работу">↗</button>
    </article>
  `;
}

function renderCommunityEmpty() {
  return `
    <div class="community-empty">
      <span>✦</span>
      <h3>Здесь ждут вашу работу</h3>
      <p>Опубликуйте готовую генерацию из истории — и она появится в галерее сообщества.</p>
    </div>
  `;
}

async function sendReaction(taskId, type) {
  if (!telegramApp?.initData) {
    showCommunityStatus("Оценивать работы можно внутри Telegram.");
    return;
  }
  const signedTaskId = type === "dislike" ? -Math.abs(Number(taskId)) : Math.abs(Number(taskId));
  try {
    const response = await fetch(`/api/tma/app/feed/${signedTaskId}/action`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ action: "like" }),
    });
    if (!response.ok) {
      throw new Error("reaction_failed");
    }
    const payload = await response.json();
    storeReaction(taskId, payload.new ? type : "");
    const item = community.items.find((row) => String(row.id) === String(taskId));
    if (item) {
      if (type === "like") {
        item.likes = Number(payload.likes) || 0;
      } else {
        item.dislikes = Number(payload.likes) || 0;
      }
    }
    renderCommunity();
    await loadCommunityFeed({ quiet: true });
    if (navigator.vibrate) {
      navigator.vibrate(24);
    }
  } catch {
    showCommunityStatus("Не удалось сохранить реакцию.");
  }
}

root?.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) {
    return;
  }
  if (target.closest('[data-action="refresh-feed"]')) {
    loadCommunityFeed();
    return;
  }
  const filter = target.closest("[data-community-filter]");
  if (filter) {
    community.filter = String(filter.dataset.communityFilter || "hot");
    community.profileKey = "";
    renderCommunity();
    return;
  }
  const author = target.closest("[data-community-author]");
  if (author) {
    community.profileKey = String(author.dataset.communityAuthor || "");
    renderCommunity();
    root.scrollTo?.({ top: 0, behavior: "smooth" });
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  if (target.closest("[data-community-back]")) {
    community.profileKey = "";
    renderCommunity();
    return;
  }
  const reaction = target.closest("[data-community-reaction]");
  if (reaction) {
    event.preventDefault();
    sendReaction(reaction.dataset.communityId, reaction.dataset.communityReaction);
  }
});

const observer = new MutationObserver(scheduleEnhance);
if (root) {
  observer.observe(root, { childList: true, subtree: true });
}
scheduleEnhance();
