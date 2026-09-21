// =============================================================================
// ChefBot Mini App — фронтенд без сборки (vanilla JS).
// Экраны рендерятся из <template> в index.html в контейнер #app.
// Навигация — простой стек экранов, back управляется кнопкой Telegram BackButton.
// =============================================================================

const tg = window.Telegram ? window.Telegram.WebApp : null;

// ------------------------------------------------------------------ Telegram
function initTelegram() {
  if (!tg) return;
  tg.ready();
  tg.expand();
  tg.enableClosingConfirmation && tg.enableClosingConfirmation();
  applyTheme();
  tg.onEvent("themeChanged", applyTheme);
  tg.onEvent("backButtonClicked", goBack);
}

function applyTheme() {
  const scheme = tg && tg.colorScheme === "dark" ? "dark" : "light";
  document.documentElement.dataset.tgScheme = scheme;
}

function haptic(type = "light") {
  if (!tg || !tg.HapticFeedback) return;
  if (type === "success" || type === "error" || type === "warning") {
    tg.HapticFeedback.notificationOccurred(type);
  } else {
    tg.HapticFeedback.impactOccurred(type);
  }
}

function getInitData() {
  return tg && tg.initData ? tg.initData : "";
}

// ------------------------------------------------------------------ Диплинки на рецепты
let botUsername = "";

async function getBotUsername() {
  if (botUsername) return botUsername;
  try {
    const cfg = await api("/api/config");
    botUsername = cfg.bot_username || "";
  } catch (e) {
    // офлайн/ошибка сети - оставляем пустым, вызывающий код покажет тост
  }
  return botUsername;
}

async function shareRecipe(id, name) {
  const username = await getBotUsername();
  if (!username) {
    flashToast("Не удалось получить ссылку — попробуйте позже");
    return;
  }
  api(`/api/recipes/${id}/share`, { method: "POST" }).catch(() => {});
  const deepLink = `https://t.me/${username}?startapp=recipe_${id}`;
  const shareText = `Смотри рецепт «${name}» в ChefBot!`;
  if (tg && tg.openTelegramLink) {
    tg.openTelegramLink(
      `https://t.me/share/url?url=${encodeURIComponent(deepLink)}&text=${encodeURIComponent(shareText)}`
    );
  } else if (navigator.share) {
    try {
      await navigator.share({ title: name, text: shareText, url: deepLink });
    } catch (e) {
      // пользователь отменил - ничего не делаем
    }
  } else if (navigator.clipboard) {
    await navigator.clipboard.writeText(deepLink);
    flashToast("Ссылка скопирована");
  } else {
    flashToast(deepLink);
  }
}

// ------------------------------------------------------------------ Мгновенные ачивки
// Некоторые ачивки неудобно вычислять постфактум (использование калькулятора
// порций, конкретная фраза в поиске) - выдаются сразу с фронтенда, см.
// INSTANT_KEYS в backend/achievements.py. Сервер сам проверяет, что ключ
// известен и ещё не выдан, так что достаточно вызвать и, если разблокировано,
// показать тост.
async function tryUnlockInstant(key) {
  try {
    const result = await api(`/api/achievements/unlock/${key}`, { method: "POST" });
    if (result.unlocked && result.achievement) {
      flashToast(`${result.achievement.emoji} Новая ачивка: ${result.achievement.title}`);
    }
  } catch (e) {
    // не мешаем пользователю, если ачивку не удалось проверить
  }
}

function showNewAchievements(list) {
  (list || []).forEach((a, i) => {
    setTimeout(() => flashToast(`${a.emoji} Новая ачивка: ${a.title}`), (i + 1) * 2400);
  });
}

// ------------------------------------------------------------------ API
const API_BASE = ""; // фронтенд и API на одном домене (см. backend/main.py)

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  const initData = getInitData();
  if (initData) headers["X-Telegram-Init-Data"] = initData;

  const res = await fetch(API_BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`API ${path} -> ${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ------------------------------------------------------------------ Роутинг
const app = document.getElementById("app");
const history_ = []; // стек { name, params }
let current = null;

function render(name, params = {}) {
  current = { name, params };
  history_.push(current);
  updateBackButton();
  paint();
}

function goBack() {
  if (history_.length <= 1) return;
  history_.pop();
  current = history_[history_.length - 1];
  updateBackButton();
  paint();
}

function updateBackButton() {
  if (!tg) return;
  if (history_.length <= 1) tg.BackButton.hide();
  else tg.BackButton.show();
}

function paint() {
  const tpl = document.getElementById(`tpl-${current.name}`);
  app.innerHTML = "";

  // Внутри Telegram навигацию назад даёт нативная кнопка BackButton в шапке
  // чата. Вне Telegram (например, открыли ссылку в обычном браузере для
  // отладки) такой кнопки нет — показываем свою, чтобы не оставлять человека
  // без возможности вернуться.
  if (!tg && history_.length > 1) {
    const backBtn = document.createElement("button");
    backBtn.className = "fallback-back-btn";
    backBtn.textContent = "← Назад";
    backBtn.addEventListener("click", goBack);
    app.appendChild(backBtn);
  }

  app.appendChild(tpl.content.cloneNode(true));
  wireGlobalActions();
  SCREENS[current.name](current.params).catch(showError);
}

function wireGlobalActions() {
  app.querySelectorAll("[data-action]").forEach((el) => {
    el.addEventListener("click", () => {
      haptic();
      const action = el.dataset.action;
      if (action === "open-shopping") render("shopping");
      if (action === "open-search") render("search");
      if (action === "open-fridge") render("fridge");
      if (action === "open-favorites") render("favorites");
      if (action === "open-random") openRandom();
      if (action === "open-achievements") render("achievements");
    });
  });
}

function showError(err) {
  console.error(err);
  app.innerHTML = `<div class="empty-state">😕 Что-то пошло не так.<br/>${escapeHtml(err.message || String(err))}</div>`;
}

// ------------------------------------------------------------------ Утилиты
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function formatAmount(v) {
  if (Number.isInteger(v)) return String(v);
  return String(Math.round(v * 100) / 100);
}

function capitalize(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

const STARS = { 1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★" };

function recipeMediaHtml(r) {
  if (r.photo_url) {
    return `<span class="recipe-media">
      <img class="recipe-card-photo" src="${escapeHtml(r.photo_url)}" alt="" loading="lazy"
           onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
      <span class="recipe-emoji" style="display:none">${r.emoji || "🍽"}</span>
    </span>`;
  }
  return `<span class="recipe-media"><span class="recipe-emoji">${r.emoji || "🍽"}</span></span>`;
}

function recipeCardHtml(r) {
  return `
    <button class="recipe-card" data-recipe-id="${r.id}">
      ${recipeMediaHtml(r)}
      <span class="recipe-card-body">
        <span class="recipe-card-name">${escapeHtml(r.name)}</span>
        <span class="recipe-card-meta">
          <span>⏱ ${r.time_minutes} мин</span>
          <span>💰 ${escapeHtml(r.price_level)}</span>
        </span>
      </span>
      <span class="recipe-card-fav">${r.is_favorite ? "❤️" : ""}</span>
    </button>`;
}

function wireRecipeCards(container) {
  container.querySelectorAll("[data-recipe-id]").forEach((el) => {
    el.addEventListener("click", () => {
      haptic();
      render("recipe", { id: Number(el.dataset.recipeId) });
    });
  });
}

async function openRandom() {
  const recipe = await api("/api/recipes/random");
  render("recipe", { id: recipe.id, viaRandom: true });
}

async function refreshShoppingBadge() {
  try {
    const items = await api("/api/shopping-list");
    const uncheckedCount = items.filter((i) => !i.is_checked).length;
    const badge = document.getElementById("shopping-badge");
    if (!badge) return;
    if (uncheckedCount > 0) {
      badge.textContent = String(uncheckedCount);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  } catch (e) {
    // тихо игнорируем — бейдж не критичен для основного сценария
  }
}

async function refreshAchievementsProgress() {
  const badge = document.getElementById("achievements-progress");
  if (!badge) return;
  try {
    const list = await api("/api/achievements");
    const unlockedCount = list.filter((a) => a.unlocked).length;
    badge.textContent = `${unlockedCount}/${list.length}`;
  } catch (e) {
    badge.textContent = "";
  }
}

let toastTimer = null;
function flashToast(message) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.style.cssText = `
      position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
      background: var(--ink); color: var(--bg-elevated); padding: 12px 18px;
      border-radius: 999px; font-size: 13.5px; font-weight: 600; z-index: 999;
      box-shadow: 0 8px 24px rgba(0,0,0,.25); max-width: 90vw; text-align: center;
      transition: opacity .3s ease;
    `;
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = "0"; }, 2200);
}

// =============================================================================
// ЭКРАНЫ
// =============================================================================
const SCREENS = {
  async home() {
    const categories = await api("/api/categories");
    const grid = document.getElementById("category-grid");
    grid.innerHTML = categories
      .map(
        (c) => `
      <button class="jar-card" data-category-id="${c.id}" data-category-name="${escapeHtml(c.name)}">
        <span class="jar-lid" style="background:${c.color}22; color:${c.color}">${c.emoji}</span>
        <span class="jar-info">
          <span class="jar-name">${escapeHtml(c.name)}</span>
          <span class="jar-count">${c.recipe_count} рецептов</span>
        </span>
      </button>`
      )
      .join("");

    grid.querySelectorAll("[data-category-id]").forEach((el) => {
      el.addEventListener("click", () => {
        haptic();
        render("category", { id: Number(el.dataset.categoryId), name: el.dataset.categoryName });
      });
    });

    await refreshShoppingBadge();
    refreshAchievementsProgress();
  },

  async achievements() {
    const list = await api("/api/achievements");
    const unlockedCount = list.filter((a) => a.unlocked).length;
    document.getElementById("achievements-summary").textContent =
      `Разблокировано ${unlockedCount} из ${list.length}`;

    const byCategory = new Map();
    list.forEach((a) => {
      if (!byCategory.has(a.category)) byCategory.set(a.category, []);
      byCategory.get(a.category).push(a);
    });

    const container = document.getElementById("achievements-list");
    container.innerHTML = [...byCategory.entries()]
      .map(
        ([category, items]) => `
      <h3 class="achievement-category-title">${escapeHtml(category)}</h3>
      ${items
        .map(
          (a) => `
        <div class="achievement-card ${a.unlocked ? "" : "is-locked"}">
          <span class="achievement-card-emoji">${a.emoji}</span>
          <span>
            <span class="achievement-card-title">${escapeHtml(a.title)}</span>
            <span class="achievement-card-description">${escapeHtml(a.description)}</span>
          </span>
        </div>`
        )
        .join("")}`
      )
      .join("");
  },

  async category({ id, name }) {
    document.getElementById("category-title").textContent = name;
    const recipes = await api(`/api/categories/${id}/recipes`);
    const list = document.getElementById("recipe-list");
    list.innerHTML = recipes.length
      ? recipes.map(recipeCardHtml).join("")
      : `<p class="empty-state">Пока нет рецептов в этой категории.</p>`;
    wireRecipeCards(list);
  },

  async search() {
    const input = document.getElementById("search-input");
    const results = document.getElementById("search-results");
    input.focus();

    let timer = null;
    let mortySauceChecked = false;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(runSearch, 350);
      if (!mortySauceChecked && input.value.trim().toLowerCase().includes("сычуаньский соус")) {
        mortySauceChecked = true;
        tryUnlockInstant("morty_sauce");
      }
    });

    async function runSearch() {
      const q = input.value.trim();
      if (!q) {
        results.innerHTML = "";
        return;
      }
      const recipes = await api(`/api/search?q=${encodeURIComponent(q)}`);
      results.innerHTML = recipes.length
        ? recipes.map(recipeCardHtml).join("")
        : `<p class="empty-state">По запросу «${escapeHtml(q)}» ничего не нашлось 😕</p>
           <button class="btn btn--primary" id="ai-generate-btn" style="margin-top:12px">📖 Получить рецепт</button>`;
      wireRecipeCards(results);

      const generateBtn = document.getElementById("ai-generate-btn");
      if (generateBtn) {
        generateBtn.onclick = async () => {
          haptic();
          generateBtn.disabled = true;
          generateBtn.textContent = "Ищу рецепт... это может занять до минуты";
          try {
            const recipe = await api("/api/recipes/generate", {
              method: "POST",
              body: { name: q, platform: tg ? tg.platform : null },
            });
            haptic("success");
            render("recipe", { id: recipe.id });
          } catch (e) {
            flashToast("Не удалось сгенерировать рецепт 😕 Попробуйте ещё раз");
            generateBtn.disabled = false;
            generateBtn.textContent = "📖 Получить рецепт";
          }
        };
      }
    }
  },

  async favorites() {
    const recipes = await api("/api/favorites");
    const list = document.getElementById("favorites-list");
    list.innerHTML = recipes.length
      ? recipes.map(recipeCardHtml).join("")
      : `<p class="empty-state">❤️ В избранном пока пусто.<br/>Добавляйте рецепты со страницы блюда.</p>`;
    wireRecipeCards(list);
  },

  async fridge() {
    const { ingredients } = await api("/api/ingredients/common");
    const commonLower = new Set(ingredients.map((n) => n.toLowerCase()));
    const selected = new Set();
    // Продукты, добавленные через поиск (не входят в стандартные плашки) -
    // держим отдельно, чтобы показывать их как свои чипы даже после того,
    // как результаты поиска очистятся.
    const extraSelected = new Set();

    const chipsEl = document.getElementById("fridge-chips");
    const searchInput = document.getElementById("fridge-search-input");
    const searchResultsEl = document.getElementById("fridge-search-results");
    const extraEl = document.getElementById("fridge-selected-extra");
    const resultsEl = document.getElementById("fridge-results");

    chipsEl.innerHTML = ingredients
      .map((name) => `<button class="chip" data-name="${escapeHtml(name)}">${escapeHtml(name)}</button>`)
      .join("");

    chipsEl.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        haptic();
        const name = chip.dataset.name;
        if (selected.has(name)) {
          selected.delete(name);
          chip.classList.remove("is-active");
        } else {
          selected.add(name);
          chip.classList.add("is-active");
        }
        renderFridgeCta();
      });
    });

    function renderExtraChips() {
      extraEl.innerHTML = Array.from(extraSelected)
        .map((name) => `<button class="chip is-active" data-name="${escapeHtml(name)}">${escapeHtml(name)} ✕</button>`)
        .join("");
      extraEl.querySelectorAll(".chip").forEach((chip) => {
        chip.addEventListener("click", () => {
          haptic();
          const name = chip.dataset.name;
          extraSelected.delete(name);
          selected.delete(name);
          renderExtraChips();
          renderFridgeCta();
        });
      });
    }

    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(runIngredientSearch, 300);
    });

    async function runIngredientSearch() {
      const q = searchInput.value.trim();
      if (!q) {
        searchResultsEl.innerHTML = "";
        return;
      }
      const { ingredients: found } = await api(`/api/ingredients/search?q=${encodeURIComponent(q)}`);
      // Не дублируем продукты, которые и так есть среди стандартных плашек
      // или уже выбраны через поиск.
      const fresh = found.filter(
        (name) => !commonLower.has(name.toLowerCase()) && !extraSelected.has(name)
      );
      searchResultsEl.innerHTML = fresh.length
        ? fresh.map((name) => `<button class="chip" data-name="${escapeHtml(name)}">➕ ${escapeHtml(name)}</button>`).join("")
        : `<p class="empty-state">Ничего не нашлось</p>`;

      searchResultsEl.querySelectorAll(".chip").forEach((chip) => {
        chip.addEventListener("click", () => {
          haptic();
          const name = chip.dataset.name;
          extraSelected.add(name);
          selected.add(name);
          searchInput.value = "";
          searchResultsEl.innerHTML = "";
          renderExtraChips();
          renderFridgeCta();
        });
      });
    }

    function renderFridgeCta() {
      resultsEl.innerHTML = selected.size
        ? `<button class="btn btn--primary fridge-cta" id="fridge-find-btn">🔍 Найти блюда (${selected.size})</button>`
        : "";
      const btn = document.getElementById("fridge-find-btn");
      if (btn) btn.addEventListener("click", findByFridge);
    }

    async function findByFridge() {
      haptic();
      resultsEl.innerHTML = `<p class="empty-state">Ищем рецепты…</p>`;
      const matches = await api("/api/fridge/match", {
        method: "POST",
        body: { ingredients: Array.from(selected) },
      });

      if (!matches.length) {
        resultsEl.innerHTML = `<p class="empty-state">Не нашлось рецептов с такими продуктами 🙂<br/>Попробуйте отметить больше.</p>`;
        return;
      }

      resultsEl.innerHTML = matches
        .map(
          (m) => `
        <button class="recipe-card" data-recipe-id="${m.recipe.id}">
          ${recipeMediaHtml(m.recipe)}
          <span class="recipe-card-body">
            <span class="recipe-card-name">${escapeHtml(m.recipe.name)}</span>
            <span class="recipe-card-meta"><span>⏱ ${m.recipe.time_minutes} мин</span></span>
            ${m.missing.length ? `<span class="recipe-card-missing">Не хватает: ${m.missing.slice(0, 3).map(escapeHtml).join(", ")}${m.missing.length > 3 ? "…" : ""}</span>` : ""}
          </span>
          <span class="match-pill">${m.percent}%</span>
        </button>`
        )
        .join("");
      wireRecipeCards(resultsEl);
    }
  },

  async recipe({ id, viaRandom }) {
    let portions = null;

    async function load() {
      const url = portions ? `/api/recipes/${id}?portions=${portions}` : `/api/recipes/${id}`;
      const recipe = await api(url);
      portions = recipe.portions;
      paintRecipe(recipe);
    }

    function paintRecipe(recipe) {
      document.getElementById("recipe-title").textContent = recipe.name;

      const heroPhoto = document.getElementById("recipe-hero-photo");
      if (recipe.photo_url) {
        heroPhoto.innerHTML = `<img src="${escapeHtml(recipe.photo_url)}" alt="" loading="lazy"
          onerror="this.closest('.recipe-hero-photo').hidden = true;">`;
        heroPhoto.hidden = false;
      } else {
        heroPhoto.hidden = true;
        heroPhoto.innerHTML = "";
      }

      const favBtn = document.getElementById("recipe-fav-btn");
      favBtn.textContent = recipe.is_favorite ? "❤️" : "🤍";
      favBtn.onclick = async () => {
        haptic();
        const result = await api("/api/favorites/toggle", {
          method: "POST",
          body: { recipe_id: id },
        });
        favBtn.textContent = result.is_favorite ? "❤️" : "🤍";
        showNewAchievements(result.new_achievements);
      };

      document.getElementById("recipe-edit-btn").onclick = () => {
        haptic();
        render("recipe-edit", { id });
      };

      document.getElementById("recipe-share-btn").onclick = () => {
        haptic();
        shareRecipe(id, recipe.name);
      };

      const effectiveTime = recipe.custom_time_minutes ?? recipe.time_minutes;
      document.getElementById("recipe-meta").innerHTML = `
        <span class="meta-pill">${STARS[recipe.difficulty] || "★★☆☆☆"}</span>
        <span class="meta-pill">⏱ ${effectiveTime} мин${recipe.custom_time_minutes != null ? " ✏️" : ""}</span>
        <span class="meta-pill">💰 ${escapeHtml(recipe.price_level)}</span>
        <span class="meta-pill">🔥 ${recipe.calories} ккал</span>
        <span class="meta-pill">🌍 ${escapeHtml(recipe.cuisine)}</span>`;

      document.getElementById("recipe-description").textContent = recipe.description || "";

      const sourceEl = document.getElementById("recipe-source");
      if (recipe.source_url) {
        let domain = recipe.source_url;
        try { domain = new URL(recipe.source_url).hostname.replace(/^www\./, ""); } catch (e) {}
        sourceEl.innerHTML = `Источник: <a href="${escapeHtml(recipe.source_url)}" target="_blank" rel="noopener">${escapeHtml(domain)}</a>`;
        sourceEl.hidden = false;
      } else {
        sourceEl.hidden = true;
        sourceEl.innerHTML = "";
      }

      const portionsControl = document.getElementById("portions-control");
      const options = [2, 4, 6, 8];
      portionsControl.innerHTML = options
        .map((p) => `<button class="portion-btn ${p === portions ? "is-active" : ""}" data-portions="${p}">${p}</button>`)
        .join("");
      portionsControl.querySelectorAll("[data-portions]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          haptic();
          portions = Number(btn.dataset.portions);
          if (portions >= 6) {
            tryUnlockInstant("tactic_garrison");
          }
          // Ачивки "на компанию" - калькулятор порций для конкретных типов
          // блюд, см. INSTANT_KEYS в backend/achievements.py.
          const feastText = [
            recipe.name, recipe.description, recipe.category_name, recipe.cuisine,
            ...recipe.ingredients.map((i) => i.name),
          ].join(" ").toLowerCase();
          if (portions >= 12 && /индейк|куриц|пирог/.test(feastText)) {
            tryUnlockInstant("great_hall_feast");
          }
          if (portions >= 20 && /мясной пирог|пирог с мясом/.test(feastText)) {
            tryUnlockInstant("north_remembers");
          }
          if (portions >= 20 && /дамплинг|пельмен|лапш|рис\b/.test(feastText)) {
            tryUnlockInstant("faceless_feast");
          }
          if (portions >= 15 && /рыба|морепродукт|креветк|кальмар|на кости|ребра|рёбра/.test(feastText)) {
            tryUnlockInstant("feed_the_crew");
          }
          await load();
        });
      });

      document.getElementById("ingredient-list").innerHTML = recipe.ingredients
        .map(
          (ing) => `
        <li class="ingredient-row">
          <span class="ingredient-name">${escapeHtml(capitalize(ing.name))}</span>
          <span class="ingredient-amount">${ing.unit === "по вкусу" ? "по вкусу" : `${formatAmount(ing.amount)} ${escapeHtml(ing.unit)}`}</span>
        </li>`
        )
        .join("");

      document.querySelector('[data-action="add-to-shopping"]').onclick = async () => {
        const result = await api("/api/shopping-list/add-recipe", { method: "POST", body: { recipe_id: id, portions } });
        haptic("success");
        flashToast("Продукты добавлены в список покупок 🛒");
        refreshShoppingBadge();
        showNewAchievements(result.new_achievements);
      };

      document.querySelector('[data-action="start-cooking"]').onclick = () => {
        if (!recipe.steps.length) {
          flashToast("Для этого рецепта пока нет пошаговой инструкции");
          return;
        }
        render("cooking", { id, portions, name: recipe.name, steps: recipe.steps, viaRandom });
      };
    }

    await load();
  },

  async "recipe-edit"({ id }) {
    const recipe = await api(`/api/recipes/${id}`);

    const timeInput = document.getElementById("edit-time-input");
    timeInput.value = recipe.custom_time_minutes ?? recipe.time_minutes;
    timeInput.placeholder = String(recipe.time_minutes);

    const stepsListEl = document.getElementById("edit-steps-list");
    stepsListEl.innerHTML = recipe.steps
      .map(
        (step) => `
      <div class="edit-step-row">
        <p class="edit-step-original"><b>Шаг ${step.step_number}.</b> ${escapeHtml(step.text)}</p>
        <textarea class="edit-step-note-input" data-step-number="${step.step_number}"
          placeholder="Например: добавить лимон">${escapeHtml(step.note || "")}</textarea>
      </div>`
      )
      .join("");

    if (!recipe.steps.length) {
      stepsListEl.innerHTML = `<p class="empty-state">Для этого рецепта пока нет пошаговой инструкции</p>`;
    }

    document.getElementById("edit-save-btn").onclick = async () => {
      haptic();
      const stepNotes = {};
      stepsListEl.querySelectorAll("[data-step-number]").forEach((el) => {
        stepNotes[el.dataset.stepNumber] = el.value;
      });
      const rawTime = timeInput.value.trim();
      const timeMinutes = rawTime ? Number(rawTime) : null;

      const result = await api(`/api/recipes/${id}/customize`, {
        method: "PUT",
        body: { time_minutes: timeMinutes, step_notes: stepNotes, platform: tg ? tg.platform : null },
      });
      haptic("success");
      flashToast("Правки сохранены ✏️");
      (result.new_achievements || []).forEach((a, i) => {
        setTimeout(() => flashToast(`${a.emoji} Новая ачивка: ${a.title}`), (i + 1) * 2400);
      });
      goBack();
    };

    document.getElementById("edit-reset-btn").onclick = async () => {
      haptic();
      await api(`/api/recipes/${id}/customize`, { method: "DELETE" });
      haptic("success");
      flashToast("Правки сброшены — рецепт как в базе");
      goBack();
    };
  },

  async cooking({ id, name, steps, viaRandom }) {
    let stepIndex = 0;
    document.getElementById("cooking-recipe-name").textContent = name;

    const progressEl = document.getElementById("cooking-progress");
    progressEl.innerHTML = steps.map(() => "<span></span>").join("");

    function paintStep() {
      [...progressEl.children].forEach((el, i) => el.classList.toggle("is-done", i <= stepIndex));
      const step = steps[stepIndex];
      document.getElementById("cooking-step").innerHTML = `
        <span class="cooking-step-index">ШАГ ${stepIndex + 1} ИЗ ${steps.length}</span>
        <span class="cooking-step-text">${escapeHtml(step.text)}</span>
        ${step.timer_minutes ? `<span class="cooking-step-timer">⏱ ${step.timer_minutes} мин</span>` : ""}
        ${step.note ? `<span class="cooking-step-note">💡 ${escapeHtml(step.note)}</span>` : ""}
      `;
      document.getElementById("cooking-prev").disabled = stepIndex === 0;
      const nextBtn = document.getElementById("cooking-next");
      nextBtn.textContent = stepIndex === steps.length - 1 ? "✅ Готово!" : "Далее ➡";
    }

    document.getElementById("cooking-prev").addEventListener("click", () => {
      haptic();
      if (stepIndex > 0) { stepIndex--; paintStep(); }
    });
    document.getElementById("cooking-next").addEventListener("click", async () => {
      haptic();
      if (stepIndex < steps.length - 1) {
        stepIndex++;
        paintStep();
        return;
      }
      haptic("success");
      try {
        const result = await api(`/api/recipes/${id}/cook`, {
          method: "POST",
          body: { via_random: !!viaRandom },
        });
        (result.new_achievements || []).forEach((a, i) => {
          setTimeout(() => flashToast(`${a.emoji} Новая ачивка: ${a.title}`), i * 2400);
        });
      } catch (e) {
        // не мешаем пользователю, если запись приготовления не удалась
      }
      goBack();
    });

    paintStep();
  },

  async shopping() {
    const items = await api("/api/shopping-list");
    const list = document.getElementById("shopping-list");
    const empty = document.getElementById("shopping-empty");

    empty.hidden = items.length > 0;
    list.innerHTML = items
      .map(
        (i) => `
      <button class="shopping-item ${i.is_checked ? "is-checked" : ""}" data-item-id="${i.id}">
        <span class="shopping-check">${i.is_checked ? "✓" : ""}</span>
        <span class="shopping-name">${escapeHtml(capitalize(i.ingredient_name))}</span>
        <span class="shopping-amount">${i.unit === "по вкусу" ? "по вкусу" : `${formatAmount(i.amount)} ${escapeHtml(i.unit)}`}</span>
      </button>`
      )
      .join("");

    list.querySelectorAll("[data-item-id]").forEach((el) => {
      el.addEventListener("click", async () => {
        haptic();
        await api(`/api/shopping-list/${el.dataset.itemId}/toggle`, { method: "POST" });
        await SCREENS.shopping();
      });
    });

    document.getElementById("clear-checked-btn").onclick = async () => {
      haptic();
      const result = await api("/api/shopping-list/clear-checked", { method: "POST" });
      await SCREENS.shopping();
      refreshShoppingBadge();
      showNewAchievements(result.new_achievements);
    };
  },
};

// ------------------------------------------------------------------ Старт
initTelegram();

// Диплинк вида t.me/BOT?startapp=recipe_42 - открываем сразу карточку
// блюда, минуя главный экран (см. shareRecipe() и кнопку "Поделиться").
// Кладём "home" в историю без отрисовки, чтобы кнопка "назад" вела на
// главный экран, а не сразу закрывала приложение.
const startParam = tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : null;
const deepLinkMatch = startParam && /^recipe_(\d+)$/.exec(startParam);
if (deepLinkMatch) {
  history_.push({ name: "home", params: {} });
  render("recipe", { id: Number(deepLinkMatch[1]) });
} else {
  render("home");
}
