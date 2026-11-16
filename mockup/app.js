const DAYS = [
  { id: 1, short: "Пн" },
  { id: 2, short: "Вт" },
  { id: 3, short: "Ср" },
  { id: 4, short: "Чт" },
  { id: 5, short: "Пт" },
  { id: 6, short: "Сб" },
  { id: 7, short: "Вс" }
];

const COLORS = ["#c85a12", "#1f7a4c", "#5c6570", "#3d7a62", "#a34b12", "#7d8b86"];
const AVATAR_ICONS = ["mail", "chat", "table", "calendar", "box", "spark"];

const ICONS = {
  mail: '<path d="M4 7h16v10H4z"/><path d="M4 7l8 6 8-6"/>',
  chat: '<path d="M6 7h12v8H9l-3 3z"/>',
  table: '<path d="M5 5h14v14H5z"/><path d="M5 10h14M5 15h14M10 5v14M15 5v14"/>',
  calendar: '<path d="M7 4v3M17 4v3"/><path d="M5 8h14"/><path d="M6 5h12v14H6z"/>',
  box: '<path d="M4 8l8-4 8 4-8 4z"/><path d="M4 8v8l8 4 8-4V8"/>',
  spark: '<path d="M12 3l1.4 4.6L18 9l-4.6 1.4L12 15l-1.4-4.6L6 9l4.6-1.4z"/>',
  shield: '<path d="M12 3l7 3v6c0 4.2-2.8 7-7 9-4.2-2-7-4.8-7-9V6z"/>',
  lock: '<rect x="6" y="11" width="12" height="9" rx="1.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>'
};

const app = document.querySelector("#app");

const state = {
  tariffId: "b",
  notice: "",
  creating: null,
  activeId: "mail",
  activeChatId: "main",
  vault: { login: "", password: "", shared: false, saved: false },
  savedFlash: false,
  bots: [
    {
      id: "mail",
      name: "Почта",
      description: "Разбирает входящие и собирает таблицу.",
      color: "#c85a12",
      icon: "mail",
      main: true,
      scenario: [
        "Открыть почту.",
        "Прочитать новые письма.",
        "Собрать таблицу по письмам клиентов.",
        "Если нужно отправить ответ, остановиться и спросить.",
        "Написать итог в основной чат."
      ].join("\n"),
      draft: "",
      routine: { days: [1, 3, 5], time: "09:00", paused: false },
      chats: [
        {
          id: "main",
          title: "Основной",
          kind: "main",
          messages: [
            { id: "u1", from: "user", text: "Проверь почту и собери таблицу." },
            { id: "b1", from: "bot", text: "Прочитал новые письма. Таблица прикреплена к этому ответу и лежит в библиотеке." },
            { id: "f1", from: "file", name: "письма.xlsx" },
            { id: "g1", from: "gate", text: "Готовлю отправку ответа клиенту по письму «Счёт за октябрь».", answer: null },
            { id: "b2", from: "bot", text: "Сайт почта.пример просит вход. Записи для него нет.", ask: "password" }
          ]
        },
        {
          id: "extra",
          title: "Счёт",
          kind: "extra",
          messages: []
        }
      ]
    }
  ]
};

state.bots[0].draft = state.bots[0].scenario;

function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function icon(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.spark}</svg>`;
}

function uid(prefix) {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}`;
}

function activeBot() {
  return state.bots.find((bot) => bot.id === state.activeId) || state.bots[0];
}

function activeChat(bot) {
  return bot.chats.find((chat) => chat.id === state.activeChatId) || bot.chats[0];
}

function isGate(line) {
  const text = line.toLowerCase();
  if (/отправ|заплат|оплат|удал|опублик/.test(text)) return true;
  return /парол/.test(text) && /смен/.test(text);
}

function routineLabel(bot) {
  if (!bot.scenario.trim()) return "Ждёт сценарий";
  if (bot.routine.paused) return "На паузе";
  const days = DAYS.filter((day) => bot.routine.days.includes(day.id)).map((day) => day.short);
  if (!days.length) return "Дни не выбраны";
  return `${days.join(", ")} · ${bot.routine.time}`;
}

function screen() {
  const hash = location.hash || "#bot";
  if (hash === "#tariff" || hash === "#bots" || hash === "#bot" || hash === "#chat" || hash === "#password") {
    return hash.slice(1);
  }
  return "bot";
}

function topbar(kicker, title, extra) {
  return `
    <header class="topbar">
      <div>
        <p class="kicker">${kicker}</p>
        <h1>${title}</h1>
      </div>
      <div class="status">
        <span class="pill green"><i class="dot"></i>Рабочий десктоп</span>
        ${extra || ""}
      </div>
    </header>`;
}

function tariffScreen() {
  const cards = [
    ["a", "Тариф 1", "Лимит рутин меньше. Квота на период меньше."],
    ["b", "Тариф 2", "Лимит рутин больше. Квота на период больше."]
  ].map(([id, name, text]) => `
    <button type="button" class="tariff ${state.tariffId === id ? "is-on" : ""}" data-action="pick-tariff" data-id="${id}">
      <span class="radio"></span>
      <strong>${name}</strong>
      <span>${text} Число ботов и модель тариф не меняет.</span>
      <span class="price">Название и цена не зафиксированы</span>
    </button>`).join("");

  return `
    ${topbar("До первой оплаты", "Выбор тарифа")}
    <div class="tariff-grid">${cards}</div>
    <div class="pay-row">
      <button type="button" class="btn-orange" data-action="pay" ${state.tariffId ? "" : "disabled"}>Оплатить</button>
      <p class="hint">После оплаты открывается список ботов. Сумму задаёт владелец продукта, в макете её нет.</p>
    </div>`;
}

function botCard(bot) {
  return `
    <button type="button" class="bot-card" data-action="open-bot" data-id="${bot.id}">
      <span class="avatar" style="--av:${bot.color}">${icon(bot.icon)}</span>
      <span>
        <strong>${esc(bot.name)}</strong>
        ${bot.main ? '<em class="pill green">Главный</em>' : ""}
        <small>${esc(bot.description || "Без описания")}</small>
      </span>
    </button>`;
}

function picker(scope, color, chosenIcon) {
  const colors = COLORS.map((item) => `
    <button type="button" class="swatch ${item === color ? "is-on" : ""}" style="background:${item}" data-action="pick-color" data-scope="${scope}" data-color="${item}" aria-label="Цвет"></button>`).join("");
  const icons = AVATAR_ICONS.map((name) => `
    <button type="button" class="icon-pick ${name === chosenIcon ? "is-on" : ""}" data-action="pick-icon" data-scope="${scope}" data-icon="${name}" aria-label="${name}">${icon(name)}</button>`).join("");
  return `<div class="swatches">${colors}</div><div class="swatches">${icons}</div>`;
}

function botsScreen() {
  const creating = state.creating;
  const form = creating ? `
    <form class="panel" data-form="create">
      <h2>Новый бот</h2>
      <div class="fields">
        <label class="field">Имя
          <input id="create-name" type="text" value="${esc(creating.name)}" placeholder="Без имени не сохраняется">
        </label>
        ${picker("create", creating.color, creating.icon)}
        <label class="field">Описание
          <textarea id="create-desc" rows="2" placeholder="Необязательно, короче двух фраз">${esc(creating.description)}</textarea>
        </label>
        <label class="field">Сценарий
          <textarea id="create-scenario" rows="4" placeholder="Можно оставить пустым. Без сценария рутина не создаётся.">${esc(creating.scenario)}</textarea>
        </label>
      </div>
      <div class="actions" style="margin-top:12px">
        <button class="btn-orange" type="submit" id="save-bot" ${creating.name.trim() ? "" : "disabled"}>Сохранить бота</button>
        <button class="btn-quiet" type="button" data-action="cancel-create">Отмена</button>
      </div>
    </form>` : "";

  return `
    ${topbar("Аккаунт", "Боты", '<button type="button" class="btn-orange" data-action="open-create">Создать бота</button>')}
    ${state.notice ? `<p class="notice">${esc(state.notice)}</p>` : ""}
    <div class="bot-grid">${state.bots.map(botCard).join("")}</div>
    ${form}`;
}

function schemeOf(bot) {
  const lines = bot.scenario.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!lines.length) {
    return `<div class="node-card"><small>Схема</small><strong>Сценарий пуст. Рутину создать нельзя.</strong></div>`;
  }
  const steps = lines.map((line) => {
    const gate = isGate(line);
    return `
      <li class="node ${gate ? "gate" : ""}">
        <span class="bead"></span>
        <div class="node-card ${gate ? "gate" : ""}">
          <small>${gate ? "Ждёт «Да»" : "Шаг"}</small>
          <strong>${esc(line)}</strong>
        </div>
      </li>`;
  }).join("");
  return `
    <ol class="scheme">
      <li class="node start">
        <span class="bead"></span>
        <div class="node-card"><small>Расписание</small><strong>${esc(routineLabel(bot))}</strong></div>
      </li>
      ${steps}
      <li class="node end">
        <span class="bead"></span>
        <div class="node-card"><small>Куда приходит результат</small><strong>Основной чат</strong></div>
      </li>
    </ol>`;
}

function botScreen() {
  const bot = activeBot();
  const dirty = bot.draft !== bot.scenario;
  const locked = !bot.scenario.trim();
  const days = DAYS.map((day) => `
    <button type="button" class="day ${bot.routine.days.includes(day.id) ? "is-on" : ""}" data-action="toggle-day" data-day="${day.id}" ${locked ? "disabled" : ""}>${day.short}</button>`).join("");

  return `
    ${topbar("Карточка бота", esc(bot.name), '<a class="btn-green" href="#chat">В чат</a>')}
    <section class="identity">
      <span class="avatar lg" style="--av:${bot.color}">${icon(bot.icon)}</span>
      <div class="identity-main">
        <input class="name-input" id="bot-name" type="text" value="${esc(bot.name)}" aria-label="Имя бота">
        <textarea class="desc-input" id="bot-desc" rows="2" aria-label="Описание">${esc(bot.description)}</textarea>
        ${picker("card", bot.color, bot.icon)}
      </div>
      <label class="switch"><input id="main-toggle" type="checkbox" ${bot.main ? "checked" : ""}> Главный</label>
    </section>
    <div class="split">
      <section class="scheme-wrap">
        <div class="section-label">
          <h2>Схема</h2>
          <span class="meta">Вид сохранённого текста</span>
        </div>
        ${schemeOf(bot)}
      </section>
      <section class="editor">
        <div class="section-label">
          <h2>Текст сценария</h2>
          <span id="dirty" class="dirty" ${dirty ? "" : "hidden"}>Есть несохранённая правка</span>
        </div>
        <textarea id="scenario" aria-label="Текст сценария">${esc(bot.draft)}</textarea>
        <p class="hint" style="margin:10px 0">Сохранение заменяет прежний сценарий целиком. Написанные сообщения остаются как были.</p>
        ${state.savedFlash ? '<p class="flash">Сохранено. Прежний текст заменён целиком.</p>' : ""}
        <button type="button" class="btn-orange" data-action="save-scenario">Сохранить сценарий</button>
      </section>
    </div>
    <section class="routine">
      <div>
        <h2>Рутина</h2>
        <p class="hint">Дни недели и одно время. Своего текста у рутины нет.</p>
      </div>
      <div class="routine-controls">
        <div class="days">${days}</div>
        <input id="routine-time" type="time" value="${esc(bot.routine.time)}" ${locked ? "disabled" : ""}>
        <button type="button" class="btn-green" data-action="toggle-pause" ${locked ? "disabled" : ""}>${bot.routine.paused ? "Снять паузу" : "Пауза"}</button>
        <button type="button" class="btn-green" data-action="check-now" ${locked ? "disabled" : ""}>Проверить сейчас</button>
      </div>
    </section>`;
}

function messageHtml(message) {
  if (message.from === "file") {
    return `<div class="file">${icon("table")} ${esc(message.name)}</div>`;
  }
  if (message.from === "gate") {
    const actions = message.answer
      ? `<p class="flash">Вы ответили «${message.answer === "yes" ? "Да" : "Нет"}».</p>`
      : `<div class="actions"><button type="button" class="btn-orange" data-action="gate-yes" data-id="${message.id}">Да</button><button type="button" class="btn-quiet" data-action="gate-no" data-id="${message.id}">Нет</button></div>`;
    return `<article class="bubble gate"><small>Опасное действие</small><p>${esc(message.text)}</p>${actions}</article>`;
  }
  const ask = message.ask === "password"
    ? (state.vault.saved
      ? '<p class="flash">Доступ сохранён.</p>'
      : '<a class="btn-green" href="#password">Ввести доступ</a>')
    : "";
  return `<article class="bubble ${message.from}"><p>${esc(message.text)}</p>${ask}</article>`;
}

function chatScreen() {
  const bot = activeBot();
  const chat = activeChat(bot);
  state.activeChatId = chat.id;
  const items = bot.chats.map((item) => `
    <button type="button" class="chat-item ${item.id === chat.id ? "is-on" : ""}" data-action="open-chat" data-id="${item.id}">
      <b>${esc(item.title)}</b>
      <span>${item.kind === "main" ? "Нельзя удалить" : "Дополнительный"}</span>
    </button>`).join("");
  const thread = chat.messages.length
    ? chat.messages.map(messageHtml).join("")
    : '<p class="empty">Чат пустой. История из основного сюда не копируется.</p>';
  const remove = chat.kind === "extra"
    ? '<button type="button" class="btn-quiet" data-action="delete-chat">Удалить чат</button>'
    : "";

  return `
    ${topbar("Чат", esc(bot.name), '<a class="btn-quiet" href="#bot">Карточка</a>')}
    <div class="chat">
      <aside class="col list">
        <div class="section-label"><h2>Чаты</h2></div>
        ${items}
        <button type="button" class="btn-green" data-action="add-chat">Ещё чат</button>
      </aside>
      <section class="col thread">
        <div class="messages">${thread}</div>
        ${remove}
        <form class="composer" data-form="send">
          <input id="composer" type="text" placeholder="Написать в этот чат" autocomplete="off">
          <button class="btn-orange icon-btn" type="submit" aria-label="Отправить">${icon("chat")}</button>
        </form>
      </section>
      <aside class="col side">
        <span class="avatar" style="--av:${bot.color}">${icon(bot.icon)}</span>
        <strong>${esc(bot.name)}</strong>
        <small>${esc(bot.description || "Без описания")}</small>
        ${bot.main ? '<span class="pill green">Главный</span>' : ""}
        <span class="pill">${esc(routineLabel(bot))}</span>
        <p class="hint">«Да» и «Нет» приходят в основной чат. Главный бот сам их не нажимает.</p>
      </aside>
    </div>`;
}

function passwordScreen() {
  const bot = activeBot();
  return `
    <section class="vault-screen">
      <form class="vault" data-form="password" autocomplete="off">
        <span class="shield">${icon("shield")}</span>
        <div>
          <p class="kicker">Защищённая форма</p>
          <h1>Доступ для бота</h1>
        </div>
        <label class="field">Сайт
          <input type="text" value="почта.пример" readonly>
        </label>
        <label class="field">Логин
          <input id="vault-login" type="text" value="${esc(state.vault.login)}" autocomplete="off">
        </label>
        <label class="field">Пароль
          <span class="lock">${icon("lock")}<input id="vault-password" type="password" value="${esc(state.vault.password)}" autocomplete="new-password"></span>
        </label>
        <label class="switch">
          <input id="vault-shared" type="checkbox" ${state.vault.shared ? "checked" : ""}>
          Сделать запись общей
        </label>
        <p class="hint">Пока переключатель выключен, запись личная для «${esc(bot.name)}». Пароль в чат не попадает. После сохранения там будет только факт, что доступ сохранён.</p>
        <div class="actions">
          <button class="btn-orange" type="submit" ${state.vault.login.trim() && state.vault.password ? "" : "disabled"}>Сохранить доступ</button>
          <a class="btn-quiet" href="#chat">В чат</a>
        </div>
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
    password: passwordScreen
  }[name];
  app.innerHTML = view();
  document.querySelectorAll("[data-screen]").forEach((link) => {
    link.setAttribute("aria-current", link.getAttribute("href") === `#${name}` ? "page" : "false");
  });
  const theme = document.documentElement.dataset.theme || "light";
  document.querySelectorAll("[data-theme-set]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.themeSet === theme ? "true" : "false");
  });
  if (!caret) return;
  const next = document.getElementById("scenario");
  if (!next) return;
  next.focus();
  next.setSelectionRange(caret[0], caret[1]);
  next.scrollTop = caret[2];
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("nb-theme", theme); } catch (error) {}
  render();
}

document.querySelector(".theme-switch").addEventListener("click", (event) => {
  const button = event.target.closest("[data-theme-set]");
  if (button) setTheme(button.dataset.themeSet);
});

app.addEventListener("input", (event) => {
  const bot = activeBot();
  const target = event.target;
  if (target.id === "scenario") {
    bot.draft = target.value;
    state.savedFlash = false;
    const dirty = document.getElementById("dirty");
    if (dirty) dirty.hidden = bot.draft === bot.scenario;
    const flash = app.querySelector(".flash");
    if (flash && flash.textContent.startsWith("Сохранено")) flash.remove();
  }
  if (target.id === "bot-desc") bot.description = target.value;
  if (target.id === "create-name" && state.creating) {
    state.creating.name = target.value;
    const save = document.getElementById("save-bot");
    if (save) save.disabled = !target.value.trim();
  }
  if (target.id === "create-desc" && state.creating) state.creating.description = target.value;
  if (target.id === "create-scenario" && state.creating) state.creating.scenario = target.value;
  if (target.id === "vault-login") {
    state.vault.login = target.value;
    refreshPasswordButton();
  }
  if (target.id === "vault-password") {
    state.vault.password = target.value;
    refreshPasswordButton();
  }
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
  if (target.id === "main-toggle") {
    state.bots.forEach((item) => { item.main = false; });
    bot.main = target.checked;
  }
  if (target.id === "routine-time") {
    bot.routine.time = target.value || bot.routine.time;
    render();
  }
  if (target.id === "vault-shared") state.vault.shared = target.checked;
});

function refreshPasswordButton() {
  const button = app.querySelector('[data-form="password"] button[type="submit"]');
  if (button) button.disabled = !(state.vault.login.trim() && state.vault.password);
}

app.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target.dataset.form;
  if (form === "create") saveBot();
  if (form === "send") sendMessage();
  if (form === "password") savePassword();
});

app.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  const bot = activeBot();
  if (action === "pick-tariff") {
    state.tariffId = target.dataset.id;
    render();
  }
  if (action === "pay") {
    state.notice = "Подписка в макете включена. Дальше список ботов.";
    go("#bots");
  }
  if (action === "open-create") {
    state.creating = { name: "", description: "", scenario: "", color: COLORS[0], icon: "spark" };
    render();
  }
  if (action === "cancel-create") {
    state.creating = null;
    render();
  }
  if (action === "pick-color" || action === "pick-icon") {
    const scope = target.dataset.scope;
    const holder = scope === "create" ? state.creating : bot;
    if (!holder) return;
    if (action === "pick-color") holder.color = target.dataset.color;
    else holder.icon = target.dataset.icon;
    render();
  }
  if (action === "open-bot") {
    state.activeId = target.dataset.id;
    state.activeChatId = "main";
    state.savedFlash = false;
    go("#bot");
  }
  if (action === "save-scenario") {
    bot.scenario = bot.draft;
    state.savedFlash = true;
    render();
  }
  if (action === "toggle-day") {
    const day = Number(target.dataset.day);
    const days = bot.routine.days;
    bot.routine.days = days.includes(day) ? days.filter((item) => item !== day) : days.concat(day).sort();
    render();
  }
  if (action === "toggle-pause") {
    bot.routine.paused = !bot.routine.paused;
    render();
  }
  if (action === "check-now") {
    const chat = bot.chats.find((item) => item.kind === "main");
    chat.messages.push({
      id: uid("check"),
      from: "bot",
      text: "Проверка поставлена в очередь браузера. Результат придёт в этот чат."
    });
    state.activeChatId = chat.id;
    go("#chat");
  }
  if (action === "open-chat") {
    state.activeChatId = target.dataset.id;
    render();
  }
  if (action === "add-chat") {
    const chat = { id: uid("chat"), title: "Дополнительный", kind: "extra", messages: [] };
    bot.chats.push(chat);
    state.activeChatId = chat.id;
    render();
  }
  if (action === "delete-chat") {
    const chat = activeChat(bot);
    if (chat.kind === "main") return;
    bot.chats = bot.chats.filter((item) => item.id !== chat.id);
    state.activeChatId = bot.chats[0].id;
    render();
  }
  if (action === "gate-yes" || action === "gate-no") {
    const chat = activeChat(bot);
    const message = chat.messages.find((item) => item.id === target.dataset.id);
    if (!message || message.answer) return;
    message.answer = action === "gate-yes" ? "yes" : "no";
    chat.messages.push({
      id: uid("gate"),
      from: "bot",
      text: message.answer === "yes"
        ? "Отправил ответ. Остальное по сценарию доделал."
        : "Отправку пропустил. Остальное по сценарию доделал."
    });
    render();
  }
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
    main: false,
    scenario: draft.scenario.trim(),
    draft: draft.scenario.trim(),
    routine: { days: [], time: "09:00", paused: false },
    chats: [{ id: "main", title: "Основной", kind: "main", messages: [] }]
  };
  state.bots.push(bot);
  state.activeId = bot.id;
  state.activeChatId = "main";
  state.creating = null;
  state.savedFlash = false;
  go("#bot");
}

function sendMessage() {
  const input = document.getElementById("composer");
  const text = input.value.trim();
  if (!text) return;
  const bot = activeBot();
  const chat = activeChat(bot);
  chat.messages.push({ id: uid("user"), from: "user", text });
  chat.messages.push({
    id: uid("bot"),
    from: "bot",
    text: "Сообщение осталось в этом чате. В макете модель не вызывается."
  });
  render();
}

function savePassword() {
  if (!state.vault.login.trim() || !state.vault.password) return;
  const bot = activeBot();
  const chat = bot.chats.find((item) => item.kind === "main");
  chat.messages.push({ id: uid("vault"), from: "bot", text: "Доступ сохранён." });
  state.vault.login = "";
  state.vault.password = "";
  state.vault.shared = false;
  state.vault.saved = true;
  state.activeChatId = chat.id;
  go("#chat");
}

window.addEventListener("hashchange", render);
render();
