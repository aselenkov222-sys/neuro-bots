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
  me: null,
  creating: null,
  activeId: null,
  savedFlash: false,
  draft: "",
  notice: "",
  holdTariff: false,
  bots: [],
  card: null,
  messages: []
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

async function api(path, options) {
  const settings = options || {};
  const headers = new Headers(settings.headers || {});
  const init = {
    method: settings.method || "GET",
    credentials: "same-origin",
    headers
  };
  if (settings.body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(settings.body);
  }
  const response = await fetch(path, init);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : null;
  return { ok: response.ok, status: response.status, data };
}

function claimDesktop() {
  const bridge = window.__neuroDesktop;
  if (!state.me || !bridge || typeof bridge.id !== "string") return;
  api("/api/desktops", { method: "POST", body: { id: bridge.id } }).catch(() => {});
}

function screen() {
  const hash = location.hash;
  if (hash === "#tariff" || hash === "#bots" || hash === "#bot" || hash === "#chat" || hash === "#register" || hash === "#login") {
    return hash.slice(1);
  }
  return "";
}

function desiredScreen() {
  const requested = screen();
  if (requested === "login" || requested === "register") return requested;
  if (!state.me) return "register";
  if (state.holdTariff || !state.me.email_confirmed) return "tariff";
  if ((requested === "bot" || requested === "chat") && state.activeId) return requested;
  return "bots";
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

function formatLocal(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

function logoutControl() {
  return '<button type="button" class="btn-quiet" data-action="logout">Выход</button>';
}

function noticeHtml() {
  if (!state.notice) return "";
  return `<p class="flash" role="status">${esc(state.notice)}</p>`;
}

function note(result) {
  const message = result && result.data && result.data.message;
  if (typeof message === "string") state.notice = message;
  else if (result && result.ok) state.notice = "";
}

function tariffScreen() {
  const resend = state.me && state.me.email_confirmed === false
    ? '<div class="actions" style="margin-top:12px"><button type="button" class="btn-orange" data-action="resend">Прислать новую</button></div>'
    : "";
  return `
    ${topbar("Тариф", "", logoutControl())}
    ${noticeHtml()}
    <article class="tariff sheet"></article>
    ${resend}`;
}

function registerScreen() {
  return `
    ${topbar("Регистрация", "")}
    ${noticeHtml()}
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
    ${noticeHtml()}
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
    ${topbar("Боты", "", '<button type="button" class="btn-orange" data-action="open-create">Создать бота</button>' + logoutControl())}
    ${noticeHtml()}
    <div class="bot-grid">${state.bots.map(botCard).join("")}</div>
    ${form}`;
}

function botScreen() {
  const bot = state.card;
  if (!bot) return `${topbar("Карточка бота", "", logoutControl())}${noticeHtml()}`;
  const dirty = state.draft !== bot.scenario;
  return `
    ${topbar(esc(bot.name), "Карточка бота", '<a class="btn-green" href="#chat">В чат</a>' + logoutControl())}
    ${noticeHtml()}
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
      <textarea id="scenario" aria-label="Текст сценария">${esc(state.draft)}</textarea>
      <p class="hint" style="margin:10px 0">Сохранение заменяет прежний сценарий целиком. Написанные сообщения остаются как были.</p>
      ${state.savedFlash ? '<p class="flash" role="status">Сценарий сохранён. Прежний текст заменён целиком.</p>' : ""}
      <button type="button" class="btn-orange" data-action="save-scenario">Сохранить сценарий</button>
    </section>`;
}

function messageHtml(message, bot) {
  const when = esc(formatLocal(message.updated_at));
  const text = esc(message.text || "");
  if (message.role === "user") {
    return `<article class="bubble user"><p>${text}</p><p class="meta">${when}</p></article>`;
  }
  const avatar = message.role === "assistant" && bot
    ? `<span class="avatar sm" style="--av:${bot.color}">${icon(bot.icon)}</span>`
    : "";
  return `<article class="bubble bot">${avatar}<div><p>${text}</p><p class="meta">${when}</p></div></article>`;
}

function chatScreen() {
  const bot = state.card || { name: "", color: "#c85a12", icon: "spark" };
  const thread = state.messages.map((message) => messageHtml(message, bot)).join("");
  return `
    ${topbar("Чаты", "", logoutControl())}
    ${noticeHtml()}
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
  const name = desiredScreen();
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

async function loadBots() {
  const result = await api("/api/bots");
  note(result);
  if (result.ok && result.data) state.bots = result.data.bots;
}

async function loadCard() {
  if (!state.activeId) return;
  const result = await api(`/api/bots/${state.activeId}`);
  note(result);
  if (!result.ok || !result.data) return;
  state.card = result.data;
  state.draft = result.data.scenario;
}

async function loadMessages() {
  if (!state.activeId) return;
  if (!state.card || state.card.id !== state.activeId) await loadCard();
  const result = await api(`/api/bots/${state.activeId}/messages`);
  note(result);
  if (result.ok && result.data) state.messages = result.data.messages;
}

async function showCurrent() {
  const desired = desiredScreen();
  if (location.hash !== `#${desired}`) {
    location.hash = `#${desired}`;
    return;
  }
  if (desired === "bots") await loadBots();
  if (desired === "bot") await loadCard();
  if (desired === "chat") await loadMessages();
  render();
}

async function patchBot(changes) {
  if (!state.activeId) return;
  const result = await api(`/api/bots/${state.activeId}`, { method: "PATCH", body: changes });
  note(result);
  if (!result.ok || !result.data) {
    render();
    return;
  }
  state.card = result.data;
  if (Object.prototype.hasOwnProperty.call(changes, "scenario")) state.draft = result.data.scenario;
  const index = state.bots.findIndex((item) => item.id === result.data.id);
  if (index >= 0) state.bots[index] = result.data;
  render();
}

async function submitRegister() {
  const email = document.getElementById("register-email").value;
  const password = document.getElementById("register-password").value;
  const result = await api("/api/register", { method: "POST", body: { email, password } });
  note(result);
  if (!result.ok) {
    render();
    return;
  }
  state.me = result.data;
  claimDesktop();
  location.hash = "#tariff";
}

async function submitLogin() {
  const email = document.getElementById("login-email").value;
  const password = document.getElementById("login-password").value;
  const result = await api("/api/login", { method: "POST", body: { email, password } });
  note(result);
  if (!result.ok) {
    render();
    return;
  }
  state.me = result.data;
  state.activeId = null;
  claimDesktop();
  location.hash = result.data.email_confirmed ? "#bots" : "#tariff";
}

async function submitCreate() {
  const draft = state.creating;
  if (!draft || !draft.name.trim()) return;
  const result = await api("/api/bots", {
    method: "POST",
    body: {
      name: draft.name.trim(),
      description: draft.description,
      scenario: draft.scenario,
      color: draft.color,
      icon: draft.icon
    }
  });
  note(result);
  if (!result.ok || !result.data) {
    render();
    return;
  }
  state.bots.push(result.data);
  state.creating = null;
  render();
}

async function submitMessage() {
  const input = document.getElementById("composer");
  const text = input.value.trim();
  if (!text || !state.activeId) return;
  const result = await api(`/api/bots/${state.activeId}/messages`, {
    method: "POST",
    body: { text }
  });
  note(result);
  if (!result.ok || !result.data) {
    render();
    return;
  }
  state.messages = result.data.messages;
  render();
  const next = document.getElementById("composer");
  if (next) next.focus();
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

app.addEventListener("input", (event) => {
  const target = event.target;
  if (target.id === "scenario") {
    state.draft = target.value;
    state.savedFlash = false;
    const dirty = document.getElementById("dirty");
    if (dirty && state.card) dirty.hidden = state.draft === state.card.scenario;
    const flash = app.querySelector(".flash");
    if (flash && flash.textContent.startsWith("Сценарий сохранён")) flash.remove();
  }
  if (target.id === "create-name" && state.creating) {
    state.creating.name = target.value;
    const save = document.getElementById("save-bot");
    if (save) save.disabled = !target.value.trim();
  }
  if (target.id === "create-desc" && state.creating) state.creating.description = target.value;
  if (target.id === "create-scenario" && state.creating) state.creating.scenario = target.value;
});

app.addEventListener("change", (event) => {
  const target = event.target;
  if (target.id === "bot-name" && state.card) {
    const next = target.value.trim();
    if (!next) target.value = state.card.name;
    else if (next !== state.card.name) patchBot({ name: next });
  }
  if (target.id === "bot-desc" && state.card && target.value !== state.card.description) {
    patchBot({ description: target.value });
  }
});

app.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target.dataset.form;
  if (form === "register") submitRegister();
  if (form === "login") submitLogin();
  if (form === "create") submitCreate();
  if (form === "send") submitMessage();
});

app.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  if (action === "resend") {
    sendConfirmation();
    return;
  }
  if (action === "logout") {
    signOut();
    return;
  }
  if (action === "open-create") {
    state.creating = { name: "", description: "", scenario: "", color: "#c85a12", icon: "spark" };
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
    const scope = target.dataset.scope;
    if (scope === "create" && state.creating) {
      if (action === "pick-color") state.creating.color = target.dataset.color;
      else state.creating.icon = target.dataset.icon;
      render();
    }
    if (scope === "card" && state.card) {
      if (action === "pick-color") patchBot({ color: target.dataset.color });
      else patchBot({ icon: target.dataset.icon });
    }
  }
  if (action === "open-bot") {
    state.activeId = target.dataset.id;
    state.savedFlash = false;
    state.card = null;
    location.hash = "#bot";
    return;
  }
  if (action === "save-scenario") {
    state.savedFlash = true;
    patchBot({ scenario: state.draft });
  }
  restoreFocus(target);
});

async function sendConfirmation() {
  const result = await api("/api/confirmation", { method: "POST" });
  note(result);
  if (result.ok && result.data) state.me = result.data;
  render();
}

async function signOut() {
  const result = await api("/api/logout", { method: "POST" });
  note(result);
  state.me = null;
  state.holdTariff = false;
  state.activeId = null;
  state.creating = null;
  state.savedFlash = false;
  state.draft = "";
  state.bots = [];
  state.card = null;
  state.messages = [];
  if (location.hash !== "#login") location.hash = "#login";
  else render();
}

async function boot() {
  if (location.pathname === "/api/confirm-email") {
    const result = await api(`${location.pathname}${location.search}`, {
      headers: { Accept: "application/json" }
    });
    note(result);
    const destination = result.data && result.data.destination;
    const next = destination === "bots" || destination === "login" || destination === "tariff"
      ? destination
      : "register";
    const me = await api("/api/me");
    state.me = me.status === 401 || !me.ok ? null : me.data;
    state.holdTariff = next === "tariff";
    history.replaceState(null, "", `/#${next}`);
    claimDesktop();
    await showCurrent();
    return;
  }
  const me = await api("/api/me");
  state.me = me.status === 401 || !me.ok ? null : me.data;
  claimDesktop();
  await showCurrent();
}

window.addEventListener("hashchange", () => {
  showCurrent();
});
window.addEventListener("neuro-desktop", () => {
  claimDesktop();
});
boot();
