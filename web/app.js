const COLORS = [
  { value: "#c85a12", name: "Ржавый" },
  { value: "#1f7a4c", name: "Мох" },
  { value: "#5c6570", name: "Графит" },
  { value: "#3d7a62", name: "Хвоя" },
  { value: "#a34b12", name: "Медь" },
  { value: "#7d8b86", name: "Камень" }
];

const AVATAR_ICONS = [
  { id: "mail", name: "Почта" },
  { id: "chat", name: "Разговор" },
  { id: "table", name: "Таблица" },
  { id: "calendar", name: "Календарь" },
  { id: "box", name: "Коробка" },
  { id: "spark", name: "Искра" }
];

const ICONS = {
  mail: '<path d="M4 7h16v10H4z"/><path d="M4 7l8 6 8-6"/>',
  chat: '<path d="M6 7h12v8H9l-3 3z"/>',
  table: '<path d="M5 5h14v14H5z"/><path d="M5 10h14M5 15h14M10 5v14M15 5v14"/>',
  calendar: '<path d="M7 4v3M17 4v3"/><path d="M5 8h14"/><path d="M6 5h12v14H6z"/>',
  box: '<path d="M4 8l8-4 8 4-8 4z"/><path d="M4 8v8l8 4 8-4V8"/>',
  spark: '<path d="M12 3l1.4 4.6L18 9l-4.6 1.4L12 15l-1.4-4.6L6 9l4.6-1.4z"/>',
  send: '<path d="M4 12h11"/><path d="M11 7l5 5-5 5"/>'
};

const app = document.querySelector("#app");

const state = {
  creating: null,
  activeId: "cal",
  savedFlash: false,
  bots: [
    {
      id: "cal",
      name: "Календарь",
      description: "Собирает встречи на день.",
      color: "#1f7a4c",
      icon: "calendar",
      scenario: "Открыть календарь.\nСобрать встречи на сегодня.\nНаписать итог в основной чат.",
      draft: "Открыть календарь.\nСобрать встречи на сегодня.\nНаписать итог в основной чат.",
      messages: [
        { id: "c1", from: "user", text: "Что сегодня по встречам?" },
        { id: "c2", from: "bot", text: "Три встречи. Первая в 11:00, последняя в 16:30." }
      ]
    }
  ]
};

function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.spark}</svg>`;
}

function uid(prefix) {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}`;
}

function activeBot() {
  return state.bots.find((bot) => bot.id === state.activeId) || state.bots[0];
}

function screen() {
  const hash = location.hash || "#chat";
  if (hash === "#tariff" || hash === "#bots" || hash === "#bot" || hash === "#chat" || hash === "#register" || hash === "#login") {
    return hash.slice(1);
  }
  return "bot";
}

function topbar(title, lede, extra) {
  return `
    <header class="topbar">
      <div>
        <h1>${title}</h1>
        ${lede ? `<p class="lede">${lede}</p>` : ""}
      </div>
      ${extra ? `<div class="status">${extra}</div>` : ""}
    </header>`;
}

function picker(scope, color, chosenIcon) {
  const colors = COLORS.map((item) => `
    <button type="button" class="swatch ${item.value === color ? "is-on" : ""}" style="background:${item.value}" data-action="pick-color" data-scope="${scope}" data-color="${item.value}" aria-label="${item.name}" aria-pressed="${item.value === color ? "true" : "false"}"></button>`).join("");
  const icons = AVATAR_ICONS.map((item) => `
    <button type="button" class="icon-pick ${item.id === chosenIcon ? "is-on" : ""}" data-action="pick-icon" data-scope="${scope}" data-icon="${item.id}" aria-label="${item.name}" aria-pressed="${item.id === chosenIcon ? "true" : "false"}">${icon(item.id)}</button>`).join("");
  return `
    <fieldset>
      <legend>Цвет и значок</legend>
      <div class="swatches">${colors}</div>
      <div class="swatches" style="margin-top:8px">${icons}</div>
    </fieldset>`;
}

function tariffScreen() {
  return `
    ${topbar("Тариф", "")}
    <article class="tariff sheet"></article>`;
}

function registerScreen() {
  return `
    ${topbar("Регистрация", "")}
    <form class="panel sheet" data-form="register">
      <div class="fields">
        <label class="field" for="register-email">Почта
          <input id="register-email" name="email" type="email" autocomplete="email">
        </label>
        <label class="field" for="register-password">Пароль
          <input id="register-password" name="password" type="password" autocomplete="new-password">
        </label>
      </div>
      <div class="actions" style="margin-top:12px">
        <button class="btn-orange" type="submit">Зарегистрироваться</button>
        <a class="btn-quiet" href="#login">Вход</a>
      </div>
    </form>`;
}

function loginScreen() {
  return `
    ${topbar("Вход", "")}
    <form class="panel sheet" data-form="login">
      <div class="fields">
        <label class="field" for="login-email">Почта
          <input id="login-email" name="email" type="email" autocomplete="email">
        </label>
        <label class="field" for="login-password">Пароль
          <input id="login-password" name="password" type="password" autocomplete="current-password">
        </label>
      </div>
      <div class="actions" style="margin-top:12px">
        <button class="btn-orange" type="submit">Войти</button>
        <a class="btn-quiet" href="#register">Регистрация</a>
      </div>
    </form>`;
}

function botCard(bot) {
  return `
    <button type="button" class="bot-card sheet" data-action="open-bot" data-id="${bot.id}">
      <span class="avatar" style="--av:${bot.color}">${icon(bot.icon)}</span>
      <span>
        <strong>${esc(bot.name)}</strong>
        <small>${esc(bot.description || "Без описания")}</small>
      </span>
    </button>`;
}

function botsScreen() {
  const creating = state.creating;
  const form = creating ? `
    <form class="panel sheet" data-form="create">
      <h2>Новый бот</h2>
      <div class="fields">
        <label class="field" for="create-name">Имя
          <input id="create-name" type="text" value="${esc(creating.name)}" required aria-describedby="create-name-hint">
          <span class="hint" id="create-name-hint">Пустое имя не сохраняется.</span>
        </label>
        ${picker("create", creating.color, creating.icon)}
        <label class="field" for="create-desc">Описание
          <textarea id="create-desc" rows="2">${esc(creating.description)}</textarea>
          <span class="hint">Необязательно.</span>
        </label>
        <label class="field" for="create-scenario">Сценарий
          <textarea id="create-scenario" rows="4">${esc(creating.scenario)}</textarea>
          <span class="hint">Можно не заполнять.</span>
        </label>
      </div>
      <div class="actions" style="margin-top:12px">
        <button class="btn-orange" type="submit" id="save-bot" ${creating.name.trim() ? "" : "disabled"}>Сохранить бота</button>
        <button class="btn-quiet" type="button" data-action="cancel-create">Отмена</button>
      </div>
    </form>` : "";

  return `
    ${topbar("Боты", "", '<button type="button" class="btn-orange" data-action="open-create">Создать бота</button>')}
    <div class="bot-grid">${state.bots.map(botCard).join("")}</div>
    ${form}`;
}

function botScreen() {
  const bot = activeBot();
  const dirty = bot.draft !== bot.scenario;
  return `
    ${topbar(esc(bot.name), "Карточка бота", '<a class="btn-green" href="#chat">В чат</a>')}
    <section class="identity raised">
      <span class="avatar lg" style="--av:${bot.color}">${icon(bot.icon)}</span>
      <div class="identity-main">
        <label class="ink-label" for="bot-name">Имя</label>
        <input class="name-input" id="bot-name" type="text" value="${esc(bot.name)}">
        <label class="ink-label" for="bot-desc">Описание</label>
        <textarea class="desc-input" id="bot-desc" rows="2">${esc(bot.description)}</textarea>
        ${picker("card", bot.color, bot.icon)}
      </div>
    </section>
    <section class="editor sheet">
      <div class="section-label">
        <h2>Текст сценария</h2>
        <span id="dirty" class="dirty" ${dirty ? "" : "hidden"}>Есть несохранённая правка</span>
      </div>
      <textarea id="scenario" aria-label="Текст сценария">${esc(bot.draft)}</textarea>
      <p class="hint" style="margin:10px 0">Сохранение заменяет прежний сценарий целиком. Написанные сообщения остаются как были.</p>
      ${state.savedFlash ? '<p class="flash" role="status">Сценарий сохранён. Прежний текст заменён целиком.</p>' : ""}
      <button type="button" class="btn-orange" data-action="save-scenario">Сохранить сценарий</button>
    </section>`;
}

function messageHtml(message, bot) {
  if (message.from === "bot") {
    return `<article class="bubble bot"><span class="avatar sm" style="--av:${bot.color}">${icon(bot.icon)}</span><div><p>${esc(message.text)}</p></div></article>`;
  }
  return `<article class="bubble user"><p>${esc(message.text)}</p></article>`;
}

function chatScreen() {
  const bot = activeBot();
  const thread = bot.messages.map((message) => messageHtml(message, bot)).join("");
  return `
    ${topbar("Чаты", "")}
    <section class="zone thread" aria-label="Переписка с ${esc(bot.name)}">
      <div class="thread-head">
        <h2 class="thread-title">${esc(bot.name)}</h2>
        <a class="btn-quiet" href="#bot">Карточка</a>
      </div>
      <div class="messages">${thread}</div>
      <form class="composer" data-form="send">
        <label class="sr-only" for="composer">Сообщение</label>
        <input id="composer" type="text" placeholder="Написать в этот чат" autocomplete="off">
        <button class="btn-orange icon-btn" type="submit" aria-label="Отправить">${icon("send")}</button>
      </form>
    </section>`;
}

function render() {
  const scenario = document.getElementById("scenario");
  const caret = scenario && document.activeElement === scenario
    ? [scenario.selectionStart, scenario.selectionEnd, scenario.scrollTop]
    : null;
  const name = screen();
  const view = {
    tariff: tariffScreen,
    bots: botsScreen,
    bot: botScreen,
    chat: chatScreen,
    register: registerScreen,
    login: loginScreen
  }[name];
  app.classList.toggle("home", name === "chat");
  app.innerHTML = view();
  document.querySelectorAll("[data-screen]").forEach((link) => {
    link.setAttribute("aria-current", link.getAttribute("href") === `#${name}` ? "page" : "false");
  });
  const theme = document.documentElement.dataset.theme || "light";
  document.querySelectorAll("[data-theme-set]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.themeSet === theme ? "true" : "false");
  });
  const log = document.querySelector(".messages");
  if (log) log.scrollTop = log.scrollHeight;
  if (!caret) return;
  const next = document.getElementById("scenario");
  if (!next) return;
  next.focus();
  next.setSelectionRange(caret[0], caret[1]);
  next.scrollTop = caret[2];
}

function restoreFocus(target) {
  if (!target || !target.dataset || !target.dataset.action) return;
  const action = target.dataset.action;
  const match = [...document.querySelectorAll(`[data-action="${action}"]`)].find((el) => {
    return ["id", "color", "icon", "scope"].every((key) => {
      if (target.dataset[key] == null || target.dataset[key] === "") return el.dataset[key] == null || el.dataset[key] === "";
      return el.dataset[key] === target.dataset[key];
    });
  });
  if (match) match.focus();
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

app.addEventListener("input", (event) => {
  const bot = activeBot();
  const target = event.target;
  if (target.id === "scenario") {
    bot.draft = target.value;
    state.savedFlash = false;
    const dirty = document.getElementById("dirty");
    if (dirty) dirty.hidden = bot.draft === bot.scenario;
    const flash = app.querySelector(".flash");
    if (flash && flash.textContent.startsWith("Сценарий сохранён")) flash.remove();
  }
  if (target.id === "bot-desc") bot.description = target.value;
  if (target.id === "create-name" && state.creating) {
    state.creating.name = target.value;
    const save = document.getElementById("save-bot");
    if (save) save.disabled = !target.value.trim();
  }
  if (target.id === "create-desc" && state.creating) state.creating.description = target.value;
  if (target.id === "create-scenario" && state.creating) state.creating.scenario = target.value;
});

app.addEventListener("change", (event) => {
  const bot = activeBot();
  const target = event.target;
  if (target.id === "bot-name") {
    const next = target.value.trim();
    if (!next) target.value = bot.name;
    else bot.name = next;
    const title = app.querySelector("h1");
    if (title && screen() === "bot") title.textContent = bot.name;
  }
});

app.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target.dataset.form;
  if (form === "create") saveBot();
  if (form === "send") sendMessage();
});

app.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  const bot = activeBot();
  if (action === "open-create") {
    state.creating = { name: "", description: "", scenario: "", color: COLORS[0].value, icon: "spark" };
    render();
    const field = document.getElementById("create-name");
    if (field) field.focus();
    return;
  }
  if (action === "cancel-create") {
    state.creating = null;
    render();
  }
  if (action === "pick-color" || action === "pick-icon") {
    const holder = target.dataset.scope === "create" ? state.creating : bot;
    if (!holder) return;
    if (action === "pick-color") holder.color = target.dataset.color;
    else holder.icon = target.dataset.icon;
    render();
  }
  if (action === "open-bot") {
    state.activeId = target.dataset.id;
    state.savedFlash = false;
    go("#bot");
    return;
  }
  if (action === "save-scenario") {
    bot.scenario = bot.draft;
    state.savedFlash = true;
    render();
  }
  restoreFocus(target);
});

function saveBot() {
  const draft = state.creating;
  if (!draft || !draft.name.trim()) return;
  const bot = {
    id: uid("bot"),
    name: draft.name.trim(),
    description: draft.description.trim(),
    color: draft.color,
    icon: draft.icon,
    scenario: draft.scenario.trim(),
    draft: draft.scenario.trim(),
    messages: []
  };
  state.bots.push(bot);
  state.activeId = bot.id;
  state.creating = null;
  state.savedFlash = false;
  go("#bot");
}

function sendMessage() {
  const input = document.getElementById("composer");
  const text = input.value.trim();
  if (!text) return;
  const bot = activeBot();
  bot.messages.push({ id: uid("user"), from: "user", text });
  bot.messages.push({
    id: uid("bot"),
    from: "bot",
    text: "Сообщение осталось в этом чате. В макете модель не вызывается."
  });
  render();
  const next = document.getElementById("composer");
  if (next) next.focus();
}

window.addEventListener("hashchange", render);
render();
