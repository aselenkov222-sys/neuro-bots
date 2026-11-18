const DAYS = [
  { id: 1, short: "Пн" },
  { id: 2, short: "Вт" },
  { id: 3, short: "Ср" },
  { id: 4, short: "Чт" },
  { id: 5, short: "Пт" },
  { id: 6, short: "Сб" },
  { id: 7, short: "Вс" }
];

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
  routine: null,
  routineTime: "09:00",
  routineHold: null,
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

const PULSE_INTERVAL_MS = 30000;
const BROWSER_WAIT_MS = 60000;
let pulseTimer = null;
let browserTimer = null;
let browserBusy = false;

function claimDesktop() {
  const bridge = window.__neuroDesktop;
  if (!state.me || !bridge || typeof bridge.id !== "string") return;
  api("/api/desktops", { method: "POST", body: { id: bridge.id } }).then((result) => {
    if (!result.ok || !result.data || result.data.working !== true) return;
    const current = window.__neuroDesktop;
    if (!current || typeof current.id !== "string" || typeof current.timezone !== "string") return;
    startPulse();
  }).catch(() => {});
}

function sendPulse() {
  const bridge = window.__neuroDesktop;
  if (!state.me || !bridge || typeof bridge.id !== "string" || typeof bridge.timezone !== "string") return;
  api(`/api/desktops/${bridge.id}/pulse`, {
    method: "POST",
    body: { timezone: bridge.timezone }
  }).catch(() => {});
}

function startPulse() {
  if (pulseTimer !== null) return;
  sendPulse();
  pulseTimer = setInterval(sendPulse, PULSE_INTERVAL_MS);
  if (browserTimer === null) browserTimer = setInterval(finishBrowserTask, 1000);
}

function stopPulse() {
  if (pulseTimer !== null) {
    clearInterval(pulseTimer);
    pulseTimer = null;
  }
  if (browserTimer !== null) {
    clearInterval(browserTimer);
    browserTimer = null;
  }
}

async function finishBrowserTask() {
  const bridge = window.__neuroDesktop;
  if (browserBusy || !state.me || !bridge) return;
  if (typeof bridge.id !== "string" || typeof bridge.openUrl !== "string" || typeof bridge.openToken !== "string") return;
  browserBusy = true;
  try {
    const task = await api(`/api/desktops/${bridge.id}/browser-task`);
    if (!task.ok || !task.data || typeof task.data.url !== "string" || !task.data.url) return;
    const bot = state.card;
    const payload = {
      url: task.data.url,
      bot_id: bot && typeof bot.id === "string" ? bot.id : "",
      scenario: bot && typeof bot.scenario === "string" ? bot.scenario : ""
    };
    if (typeof task.data.login === "string" && task.data.login && !("password" in task.data) && !("cookie" in task.data)) {
      payload.login = task.data.login;
    }
    let opened;
    try {
      opened = await fetch(`${bridge.openUrl.replace(/\/$/, "")}/sign-in`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Desktop-Open": bridge.openToken
        },
        body: JSON.stringify(payload)
      });
    } catch (error) {
      return;
    }
    if (!opened.ok) return;
    const result = await opened.json();
    if (!result || typeof result.text !== "string" || "password" in result || "cookie" in result) return;
    const report = { text: result.text };
    if (result.login === "entered") {
      report.login = "entered";
    } else if (result.login === "choose" && typeof result.site === "string" && Array.isArray(result.choices)) {
      report.login = "choose";
      report.site = result.site;
      report.choices = result.choices.filter((item) => typeof item === "string");
    } else if (result.login === "form" && typeof result.site === "string") {
      report.login = "form";
      report.site = result.site;
    }
    await api(`/api/desktops/${bridge.id}/browser-task`, {
      method: "POST",
      body: report
    });
  } finally {
    browserBusy = false;
  }
}

function replyPending(messages, text) {
  if (!messages.length) return true;
  const last = messages[messages.length - 1];
  return last.role === "user" && last.text === text;
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

function routineLocked() {
  const bot = state.card;
  return !bot || typeof bot.scenario !== "string" || bot.scenario.trim() === "";
}

function routineBlock() {
  const locked = routineLocked();
  const routine = state.routine;
  const hold = state.routineHold;
  const selected = hold
    ? hold.body.weekdays
    : routine && Array.isArray(routine.weekdays) ? routine.weekdays : [];
  const time = hold
    ? hold.body.time
    : routine && typeof routine.time === "string" ? routine.time : state.routineTime;
  const paused = routine ? routine.paused === true : false;
  const warn = hold
    ? `<p class="warn" id="routine-warn" role="status">В это время уже стоит рутина бота «${esc(hold.name)}». Она создана раньше и пойдёт первой. Эта выполнит весь сценарий после неё.</p>
       <button type="button" class="btn-green" data-action="keep-routine-time">Оставить это время</button>`
    : "";
  const days = DAYS.map((day) => `
    <button type="button" class="day ${selected.includes(day.id) ? "is-on" : ""}" data-action="toggle-day" data-day="${day.id}" aria-pressed="${selected.includes(day.id) ? "true" : "false"}" ${locked ? "disabled" : ""}>${day.short}</button>`).join("");
  return `
    <section class="routine sheet">
      <div>
        <h2>Рутина</h2>
        <p class="hint">Дни недели и одно время. Своего текста у рутины нет.</p>
        ${locked ? '<p class="hint" id="routine-locked">Сначала заполните сценарий.</p>' : ""}
      </div>
      <div>
        <div class="routine-controls">
          <div class="days" role="group" aria-label="Дни недели">${days}</div>
          <label class="field" for="routine-time" style="width:auto">Время
            <input id="routine-time" type="time" value="${esc(time || "09:00")}" ${locked ? "disabled" : ""}>
          </label>
          <button type="button" class="btn-green" data-action="toggle-pause" ${locked || !routine ? "disabled" : ""}>${paused ? "Снять паузу" : "Пауза"}</button>
        </div>
        ${warn}
      </div>
    </section>`;
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
    </section>
    ${routineBlock()}`;
}

function accessSite(message) {
  if (!message || message.role !== "assistant" || typeof message.text !== "string") return "";
  const match = message.text.match(/^Сайт (\S+) просит вход\. Записи для него нет\.$/);
  return match ? match[1] : "";
}

function accessLink(site, bot) {
  const bridge = window.__neuroDesktop;
  if (!site || !bridge || typeof bridge.openUrl !== "string" || !bot || typeof bot.id !== "string" || !bot.id) return "";
  const href = `${bridge.openUrl.replace(/\/$/, "")}/?bot=${encodeURIComponent(bot.id)}&site=${encodeURIComponent(site)}`;
  return `<p><a href="${esc(href)}" target="_blank" rel="opener">Ввести доступ</a></p>`;
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
  const link = accessLink(accessSite(message), bot);
  return `<article class="bubble bot">${avatar}<div><p>${text}</p>${link}<p class="meta">${when}</p></div></article>`;
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
  if (result.ok && result.data && Array.isArray(result.data.bots)) {
    state.bots = result.data.bots;
    publishBotNames();
  }
}

function publishBotNames() {
  const bridge = window.__neuroDesktop;
  if (!bridge || typeof bridge.openUrl !== "string" || typeof bridge.openToken !== "string") return;
  const bots = [];
  for (const bot of state.bots) {
    if (!bot || typeof bot.id !== "string" || typeof bot.name !== "string" || !bot.id || !bot.name) continue;
    bots.push({ id: bot.id, name: bot.name });
  }
  fetch(`${bridge.openUrl.replace(/\/$/, "")}/bot-names`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Desktop-Open": bridge.openToken
    },
    body: JSON.stringify({ bots })
  }).catch(() => {});
}

async function loadCard() {
  if (!state.activeId) return;
  const result = await api(`/api/bots/${state.activeId}`);
  note(result);
  if (!result.ok || !result.data) return;
  state.card = result.data;
  state.draft = result.data.scenario;
  await loadRoutine();
}

async function loadRoutine() {
  if (!state.activeId) return;
  const result = await api(`/api/bots/${state.activeId}/routines`);
  if (!result.ok || !Array.isArray(result.data)) {
    note(result);
    state.routine = null;
    return;
  }
  state.routine = result.data[0] || null;
  if (state.routine && typeof state.routine.time === "string") {
    state.routineTime = state.routine.time;
  }
}

function schedulesOverlap(leftDays, leftTime, rightDays, rightTime) {
  if (leftTime !== rightTime) return false;
  if (!Array.isArray(leftDays) || !Array.isArray(rightDays)) return false;
  return leftDays.some((day) => rightDays.includes(day));
}

async function accountRoutines() {
  const listed = await api("/api/bots");
  const bots = listed.ok && listed.data && Array.isArray(listed.data.bots)
    ? listed.data.bots
    : null;
  if (!bots) return null;
  const found = [];
  for (const bot of bots) {
    if (!bot || typeof bot.id !== "string" || typeof bot.name !== "string") continue;
    const routines = await api(`/api/bots/${bot.id}/routines`);
    if (!routines.ok || !Array.isArray(routines.data)) return null;
    for (const routine of routines.data) {
      if (!routine || typeof routine.id !== "string") continue;
      found.push({
        id: routine.id,
        name: bot.name,
        weekdays: routine.weekdays,
        time: routine.time
      });
    }
  }
  return found;
}

async function proposeRoutine(body, routineId) {
  const others = await accountRoutines();
  if (!others) {
    render();
    return;
  }
  const clash = others.find((item) => {
    if (routineId && item.id === routineId) return false;
    return schedulesOverlap(body.weekdays, body.time, item.weekdays, item.time);
  });
  if (clash) {
    state.routineHold = { body, routineId: routineId || null, name: clash.name };
    render();
    return;
  }
  state.routineHold = null;
  await saveRoutine(body, routineId);
}

async function keepHeldRoutine() {
  const hold = state.routineHold;
  if (!hold) return;
  state.routineHold = null;
  await saveRoutine(hold.body, hold.routineId);
}

async function saveRoutine(body, routineId) {
  if (!state.activeId) return;
  const path = routineId
    ? `/api/bots/${state.activeId}/routines/${routineId}`
    : `/api/bots/${state.activeId}/routines`;
  const result = await api(path, {
    method: routineId ? "PATCH" : "POST",
    body
  });
  note(result);
  if (result.ok && result.data && result.data.id) state.routine = result.data;
  render();
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
  else if (Object.prototype.hasOwnProperty.call(changes, "name")) state.bots.push(result.data);
  if (Object.prototype.hasOwnProperty.call(changes, "name")) publishBotNames();
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
  publishBotNames();
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
  const started = Date.now();
  let listed = result;
  while (Date.now() - started <= BROWSER_WAIT_MS) {
    await finishBrowserTask();
    const next = await api(`/api/bots/${state.activeId}/messages`);
    if (next.ok && next.data) listed = next;
    const messages = listed.data && listed.data.messages;
    if (!messages || !replyPending(messages, text)) break;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  state.messages = listed.data.messages;
  render();
  const next = document.getElementById("composer");
  if (next) next.focus();
}

function restoreFocus(target) {
  if (!target || !target.dataset || !target.dataset.action) return;
  const action = target.dataset.action;
  const match = [...document.querySelectorAll(`[data-action="${action}"]`)].find((el) => {
    return ["id", "color", "icon", "scope", "day"].every((key) => {
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
  if (target.id === "routine-time" && state.card && !routineLocked()) {
    const next = target.value;
    if (!next) return;
    state.routineTime = next;
    const hold = state.routineHold;
    const routine = state.routine;
    const weekdays = hold
      ? hold.body.weekdays
      : routine && Array.isArray(routine.weekdays) ? routine.weekdays : [];
    if (weekdays.length === 0) return;
    const routineId = hold ? hold.routineId : routine && routine.id;
    if (!hold && routine && next === routine.time) return;
    proposeRoutine({ weekdays, time: next }, routineId);
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
    state.routine = null;
    state.routineTime = "09:00";
    state.routineHold = null;
    location.hash = "#bot";
    return;
  }
  if (action === "save-scenario") {
    state.savedFlash = true;
    patchBot({ scenario: state.draft });
  }
  if (action === "toggle-day") toggleDay(Number(target.dataset.day));
  if (action === "toggle-pause") togglePause();
  if (action === "keep-routine-time") keepHeldRoutine();
  restoreFocus(target);
});

async function toggleDay(day) {
  if (!state.card || !state.activeId || routineLocked()) return;
  if (!Number.isInteger(day) || day < 1 || day > 7) return;
  const routine = state.routine;
  const hold = state.routineHold;
  const selected = hold
    ? hold.body.weekdays.slice()
    : routine && Array.isArray(routine.weekdays) ? routine.weekdays.slice() : [];
  const next = selected.includes(day)
    ? selected.filter((item) => item !== day)
    : selected.concat(day).sort((left, right) => left - right);
  const time = hold
    ? hold.body.time
    : routine && typeof routine.time === "string" ? routine.time : state.routineTime;
  const routineId = hold ? hold.routineId : routine ? routine.id : null;
  if (routineId && next.length === 0) {
    state.routineHold = null;
    const result = await api(`/api/bots/${state.activeId}/routines/${routineId}`, {
      method: "DELETE"
    });
    note(result);
    if (result.ok) state.routine = null;
    render();
    return;
  }
  if (next.length === 0) {
    state.routineHold = null;
    render();
    return;
  }
  proposeRoutine({ weekdays: next, time }, routineId);
}

async function togglePause() {
  const routine = state.routine;
  if (!state.card || !routine || routineLocked()) return;
  saveRoutine({ paused: routine.paused !== true }, routine.id);
}

async function sendConfirmation() {
  const result = await api("/api/confirmation", { method: "POST" });
  note(result);
  if (result.ok && result.data) state.me = result.data;
  render();
}

async function signOut() {
  stopPulse();
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
  state.routine = null;
  state.routineTime = "09:00";
  state.routineHold = null;
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

window.addEventListener("message", (event) => {
  const bridge = window.__neuroDesktop;
  if (!bridge || typeof bridge.openUrl !== "string" || typeof bridge.id !== "string") return;
  let origin = "";
  try {
    origin = new URL(bridge.openUrl).origin;
  } catch (error) {
    return;
  }
  if (event.origin !== origin) return;
  const data = event.data;
  if (!data || typeof data !== "object" || "password" in data || "cookie" in data) return;
  if (data.login !== "saved" || typeof data.site !== "string" || !data.site) return;
  api(`/api/desktops/${bridge.id}/browser-task`, {
    method: "POST",
    body: { login: "saved", site: data.site }
  }).then((result) => {
    if (!result.ok || !state.activeId) return null;
    return api(`/api/bots/${state.activeId}/messages`);
  }).then((listed) => {
    if (!listed || !listed.ok || !listed.data) return;
    state.messages = listed.data.messages;
    render();
  }).catch(() => {});
});

window.addEventListener("hashchange", () => {
  showCurrent();
});
window.addEventListener("neuro-desktop", () => {
  claimDesktop();
});
boot();
