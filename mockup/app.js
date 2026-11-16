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
  shield: '<path d="M12 3l7 3v6c0 4.2-2.8 7-7 9-4.2-2-7-4.8-7-9V6z"/>',
  lock: '<rect x="6" y="11" width="12" height="9" rx="1.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  send: '<path d="M4 12h11"/><path d="M11 7l5 5-5 5"/>'
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
      nodeId: "s3",
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
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.spark}</svg>`;
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
  return `${days.join(", ")} в ${bot.routine.time}`;
}

function earlierClash(bot) {
  if (!bot.scenario.trim() || !bot.routine.days.length) return null;
  return state.bots.find((other) => {
    if (other.id === bot.id || !other.scenario.trim() || !other.routine.days.length) return false;
    if (other.routine.time !== bot.routine.time) return false;
    return other.routine.days.some((day) => bot.routine.days.includes(day));
  }) || null;
}

function screen() {
  const hash = location.hash || "#bot";
  if (hash === "#tariff" || hash === "#bots" || hash === "#bot" || hash === "#chat" || hash === "#password") {
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
      <div class="status">
        <span class="pill green"><i class="dot" aria-hidden="true"></i>Рабочий десктоп</span>
        ${extra || ""}
      </div>
    </header>`;
}

function switchHtml(id, checked, label) {
  return `
    <label class="switch">
      <input id="${id}" type="checkbox" ${checked ? "checked" : ""}>
      <span class="switch-ui" aria-hidden="true"></span>
      ${label}
    </label>`;
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
  const cards = [
    ["a", "Тариф 1", "Лимит рутин меньше. Квота на период меньше."],
    ["b", "Тариф 2", "Лимит рутин больше. Квота на период больше."]
  ].map(([id, name, text]) => `
    <button type="button" class="tariff ${state.tariffId === id ? "raised is-on" : "sheet"}" data-action="pick-tariff" data-id="${id}" aria-pressed="${state.tariffId === id ? "true" : "false"}">
      <span class="radio" aria-hidden="true"></span>
      <strong>${name}</strong>
      <span>${text} Число ботов и модель тариф не меняет.</span>
      <span class="price">Название и цена не зафиксированы</span>
    </button>`).join("");

  return `
    ${topbar("Выбор тарифа", "До первой оплаты других экранов нет.")}
    <div class="tariff-grid">${cards}</div>
    <div class="pay-row">
      <button type="button" class="btn-orange" data-action="pay" ${state.tariffId ? "" : "disabled"}>Оплатить</button>
      <p class="hint">После оплаты открывается список ботов. Сумму задаёт владелец продукта, в макете её нет.</p>
    </div>`;
}

function botCard(bot) {
  return `
    <button type="button" class="bot-card sheet" data-action="open-bot" data-id="${bot.id}">
      <span class="avatar" style="--av:${bot.color}">${icon(bot.icon)}</span>
      <span>
        <strong>${esc(bot.name)}</strong>
        ${bot.main ? '<em class="pill green">Главный</em>' : ""}
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
          <span class="hint">Необязательно, короче двух фраз.</span>
        </label>
        <label class="field" for="create-scenario">Сценарий
          <textarea id="create-scenario" rows="4">${esc(creating.scenario)}</textarea>
          <span class="hint">Можно не заполнять. Без сценария рутину создать нельзя.</span>
        </label>
      </div>
      <div class="actions" style="margin-top:12px">
        <button class="btn-orange" type="submit" id="save-bot" ${creating.name.trim() ? "" : "disabled"}>Сохранить бота</button>
        <button class="btn-quiet" type="button" data-action="cancel-create">Отмена</button>
      </div>
    </form>` : "";

  return `
    ${topbar("Боты", "", '<button type="button" class="btn-orange" data-action="open-create">Создать бота</button>')}
    ${state.notice ? `<p class="notice" role="status">${esc(state.notice)}</p>` : ""}
    <div class="bot-grid">${state.bots.map(botCard).join("")}</div>
    ${form}`;
}

function nodesOf(bot) {
  const lines = bot.scenario.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!lines.length) {
    return [{
      id: "empty",
      kind: "empty",
      title: "Сценарий пуст",
      body: "Рутину создать нельзя. С ботом можно разговаривать и давать разовые поручения, в том числе действие в браузере."
    }];
  }
  const steps = lines.map((line, index) => ({
    id: `s${index}`,
    kind: isGate(line) ? "gate" : "step",
    title: line.replace(/\.$/, ""),
    body: "Пункт сохранённого сценария. Схема показывает сохранённый текст, а не черновик."
  }));
  return [
    {
      id: "start",
      kind: "start",
      title: routineLabel(bot),
      body: "Запуск читает текущий сценарий и выполняет его целиком. Своего текста у рутины нет."
    },
    ...steps,
    {
      id: "end",
      kind: "end",
      title: "Основной чат",
      body: "Сюда пишутся результат запуска, пропуск, сбой, очередь и ожидание «Да»."
    }
  ];
}

function layoutNodes(nodes) {
  const columns = [118, 340];
  const top = 64;
  const gap = 104;
  const placed = nodes.map((node, index) => ({
    ...node,
    x: columns[index % 2],
    y: top + Math.floor(index / 2) * gap
  }));
  const rows = Math.ceil(nodes.length / 2);
  const height = Math.max(420, top + Math.max(rows - 1, 0) * gap + 76);
  return { placed, height };
}

function linkEnds(from, to) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const halfW = 94;
  const halfH = 32;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const sign = Math.sign(dx) || 1;
    return { x1: from.x + sign * halfW, y1: from.y, x2: to.x - sign * halfW, y2: to.y };
  }
  const sign = Math.sign(dy) || 1;
  return { x1: from.x, y1: from.y + sign * halfH, x2: to.x, y2: to.y - sign * halfH };
}

function linkPath(from, to) {
  const { x1, y1, x2, y2 } = linkEnds(from, to);
  const dx = x2 - x1;
  const dy = y2 - y1;
  if (Math.abs(dy) < 12) {
    return `M ${x1} ${y1} C ${x1 + dx * 0.5} ${y1}, ${x2 - dx * 0.5} ${y2}, ${x2} ${y2}`;
  }
  return `M ${x1} ${y1} C ${x1 + dx * 0.15} ${y1 + dy * 0.55}, ${x2 - dx * 0.15} ${y2 - dy * 0.45}, ${x2} ${y2}`;
}

function detailOf(node, nodes) {
  const index = nodes.findIndex((item) => item.id === node.id);
  const next = nodes[index + 1];
  const nextLine = next ? `Дальше: ${next.title}.` : "";
  if (node.kind === "step" || node.kind === "gate") {
    const stepCount = nodes.filter((item) => item.kind === "step" || item.kind === "gate").length;
    const stepIndex = nodes.slice(0, index + 1).filter((item) => item.kind === "step" || item.kind === "gate").length;
    return {
      kind: node.kind === "gate" ? "Ждёт «Да»" : `Шаг ${stepIndex} из ${stepCount}`,
      title: node.title,
      body: node.kind === "gate"
        ? "Это опасное действие. В основном чате появятся «Да» и «Нет». Пока ответа нет, следующий запуск этой рутины не начинается."
        : node.body,
      next: nextLine
    };
  }
  const kinds = {
    empty: "Сценарий",
    start: "Расписание",
    end: "Куда приходит результат"
  };
  return { kind: kinds[node.kind] || "Узел", title: node.title, body: node.body, next: nextLine };
}

function schemeBlock(bot) {
  const nodes = nodesOf(bot);
  const selected = nodes.find((node) => node.id === bot.nodeId) || nodes[0];
  const { placed, height } = layoutNodes(nodes);
  const links = placed.slice(1).map((node, index) => {
    const from = placed[index];
    const gate = node.kind === "gate" ? " to-gate" : "";
    const d = linkPath(from, node);
    return `<path class="glow${gate}" d="${d}"></path><path class="core${gate}" d="${d}"></path>`;
  }).join("");
  const buttons = placed.map((node) => `
    <button type="button" class="flow-node ${node.kind} ${node.id === selected.id ? "is-on" : ""}" style="--x:${node.x}px; --y:${node.y}px" data-action="select-node" data-id="${node.id}" data-kind="${node.kind}" data-y="${node.y}" aria-pressed="${node.id === selected.id ? "true" : "false"}" tabindex="${node.id === selected.id ? "0" : "-1"}">
      <b>${esc(node.title)}</b>
    </button>`).join("");
  const detail = detailOf(selected, nodes);
  return `
    <div class="scheme-stage">
      <div class="flow-canvas" style="--flow-h:${height}px">
        <svg class="flow-links" aria-hidden="true">${links}</svg>
        ${buttons}
      </div>
      <aside class="detail raised" aria-live="polite">
        <p class="kind">${esc(detail.kind)}</p>
        <h2>${esc(detail.title)}</h2>
        <p>${esc(detail.body)}</p>
        ${detail.next ? `<p class="meta">${esc(detail.next)}</p>` : ""}
      </aside>
    </div>`;
}

function botScreen() {
  const bot = activeBot();
  const dirty = bot.draft !== bot.scenario;
  const locked = !bot.scenario.trim();
  const clash = earlierClash(bot);
  const days = DAYS.map((day) => `
    <button type="button" class="day ${bot.routine.days.includes(day.id) ? "is-on" : ""}" data-action="toggle-day" data-day="${day.id}" aria-pressed="${bot.routine.days.includes(day.id) ? "true" : "false"}" ${locked ? "disabled" : ""}>${day.short}</button>`).join("");
  const warn = clash
    ? `<p class="warn" role="status">В это время уже стоит рутина бота «${esc(clash.name)}». Она создана раньше и пойдёт первой. Эта выполнит весь сценарий после неё.</p>`
    : "";

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
      ${switchHtml("main-toggle", bot.main, "Главный")}
    </section>
    ${schemeBlock(bot)}
    <section class="editor sheet">
      <div class="section-label">
        <h2>Текст сценария</h2>
        <span id="dirty" class="dirty" ${dirty ? "" : "hidden"}>Есть несохранённая правка</span>
      </div>
      <textarea id="scenario" aria-label="Текст сценария">${esc(bot.draft)}</textarea>
      <p class="hint" style="margin:10px 0">Сохранение заменяет прежний сценарий целиком. Написанные сообщения остаются как были.</p>
      ${state.savedFlash ? '<p class="flash" role="status">Сценарий сохранён. Прежний текст заменён целиком.</p>' : ""}
      <button type="button" class="btn-orange" data-action="save-scenario">Сохранить сценарий</button>
    </section>
    <section class="routine sheet">
      <div>
        <h2>Рутина</h2>
        <p class="hint">Дни недели и одно время. Своего текста у рутины нет.</p>
        ${locked ? '<p class="hint">Сначала заполните сценарий.</p>' : ""}
      </div>
      <div>
        <div class="routine-controls">
          <div class="days" role="group" aria-label="Дни недели">${days}</div>
          <label class="field" for="routine-time" style="width:auto">Время
            <input id="routine-time" type="time" value="${esc(bot.routine.time)}" ${locked ? "disabled" : ""}>
          </label>
          <button type="button" class="btn-green" data-action="toggle-pause" ${locked ? "disabled" : ""}>${bot.routine.paused ? "Снять паузу" : "Пауза"}</button>
          <button type="button" class="btn-green" data-action="check-now" ${locked ? "disabled" : ""}>Проверить сейчас</button>
        </div>
        ${warn}
      </div>
    </section>`;
}

function filesOf(bot) {
  const names = [];
  bot.chats.forEach((chat) => {
    chat.messages.forEach((message) => {
      if (message.from === "file") names.push(message.name);
    });
  });
  return names;
}

function messageHtml(message, bot) {
  if (message.from === "file") {
    return `<div class="file">${icon("table")} ${esc(message.name)}</div>`;
  }
  if (message.from === "gate") {
    const actions = message.answer
      ? `<p class="flash" role="status">Вы ответили «${message.answer === "yes" ? "Да" : "Нет"}».</p>`
      : `<div class="actions"><button type="button" class="btn-orange" data-action="gate-yes" data-id="${message.id}">Да</button><button type="button" class="btn-quiet" data-action="gate-no" data-id="${message.id}">Нет</button></div>`;
    return `<article class="bubble gate"><span class="tag">Опасное действие</span><p>${esc(message.text)}</p>${actions}</article>`;
  }
  const ask = message.ask === "password"
    ? (state.vault.saved
      ? '<p class="flash" role="status">Доступ сохранён.</p>'
      : '<a class="btn-green" href="#password">Ввести доступ</a>')
    : "";
  if (message.from === "bot") {
    return `<article class="bubble bot"><span class="avatar sm" style="--av:${bot.color}">${icon(bot.icon)}</span><div><p>${esc(message.text)}</p>${ask}</div></article>`;
  }
  return `<article class="bubble user"><p>${esc(message.text)}</p>${ask}</article>`;
}

function chatPreview(chat) {
  const last = [...chat.messages].reverse().find((message) => message.text || message.name);
  if (!last) return "Пусто";
  if (last.from === "file") return last.name;
  return last.text;
}

function chatScreen() {
  const bot = activeBot();
  const chat = activeChat(bot);
  state.activeChatId = chat.id;
  const items = bot.chats.map((item) => {
    const waiting = item.messages.some((message) => message.from === "gate" && !message.answer);
    return `
      <button type="button" class="chat-item ${item.id === chat.id ? "is-on" : ""}" data-action="open-chat" data-id="${item.id}" aria-pressed="${item.id === chat.id ? "true" : "false"}">
        <b>${esc(item.title)}</b>
        <span>${waiting ? "Ждёт «Да». " : ""}${esc(chatPreview(item))}</span>
      </button>`;
  }).join("");
  const thread = chat.messages.length
    ? chat.messages.map((message) => messageHtml(message, bot)).join("")
    : '<p class="empty">Чат пустой. История из основного сюда не копируется.</p>';
  const remove = chat.kind === "extra"
    ? '<button type="button" class="btn-quiet" data-action="delete-chat">Удалить чат</button>'
    : "";
  const files = filesOf(bot);
  const library = files.length
    ? files.map((name) => `<div class="file">${icon("table")} ${esc(name)}</div>`).join("")
    : '<p class="empty">Файлов пока нет.</p>';

  return `
    ${topbar(esc(bot.name), "Чат", '<a class="btn-quiet" href="#bot">Карточка</a>')}
    <div class="chat">
      <aside class="zone list">
        <h2>Чаты</h2>
        ${items}
        <button type="button" class="btn-green" data-action="add-chat">Ещё чат</button>
      </aside>
      <section class="zone thread">
        <div class="thread-head">
          <div>
            <h2>${esc(chat.title)}</h2>
            <p class="meta">${chat.kind === "main" ? "Этот чат нельзя удалить" : "Дополнительный чат"}</p>
          </div>
          ${remove}
        </div>
        <div class="messages">${thread}</div>
        <form class="composer" data-form="send">
          <label class="sr-only" for="composer">Сообщение</label>
          <input id="composer" type="text" placeholder="Написать в этот чат" autocomplete="off">
          <button class="btn-orange icon-btn" type="submit" aria-label="Отправить">${icon("send")}</button>
        </form>
      </section>
      <aside class="zone side">
        <div class="side-block">
          <span class="avatar" style="--av:${bot.color}">${icon(bot.icon)}</span>
          <strong>${esc(bot.name)}</strong>
          <p class="meta">${esc(bot.description || "Без описания")}</p>
          ${bot.main ? '<span class="pill green">Главный</span>' : ""}
          <span class="pill">${esc(routineLabel(bot))}</span>
        </div>
        <div class="library">
          <h2>Библиотека</h2>
          ${library}
        </div>
        <p class="hint">«Да» и «Нет» нажимает только пользователь. Главный бот сам их не нажимает.</p>
      </aside>
    </div>`;
}

function passwordScreen() {
  const bot = activeBot();
  return `
    <section class="vault-screen">
      <form class="vault raised" data-form="password" autocomplete="off">
        <span class="shield">${icon("shield")}</span>
        <div>
          <p class="meta">Защищённая форма</p>
          <h1>Доступ для бота</h1>
        </div>
        <label class="field" for="vault-site">Сайт
          <input id="vault-site" type="text" value="почта.пример" readonly>
        </label>
        <label class="field" for="vault-login">Логин
          <input id="vault-login" type="text" value="${esc(state.vault.login)}" autocomplete="off">
        </label>
        <label class="field" for="vault-password">Пароль
          <span class="lock">${icon("lock")}<input id="vault-password" type="password" value="${esc(state.vault.password)}" autocomplete="new-password"></span>
        </label>
        ${switchHtml("vault-shared", state.vault.shared, "Сделать запись общей")}
        <p class="hint">Пока переключатель выключен, запись личная для «${esc(bot.name)}». Пароль в чат не попадает. После сохранения там будет только факт, что доступ сохранён.</p>
        <div class="actions">
          <button class="btn-orange" type="submit" ${state.vault.login.trim() && state.vault.password ? "" : "disabled"}>Сохранить доступ</button>
          <a class="btn-quiet" href="#chat">В чат</a>
        </div>
      </form>
    </section>`;
}

function fitScheme() {
  const canvas = document.querySelector(".flow-canvas");
  if (!canvas) return;
  const nodes = [...canvas.querySelectorAll(".flow-node")];
  if (!nodes.length || canvas.clientWidth < 480) return;
  const width = canvas.clientWidth;
  const columns = [
    Math.max(108, width * 0.24),
    Math.min(width * 0.72, width - 112)
  ];
  const placed = nodes.map((node, index) => {
    const x = columns[index % 2];
    const y = Number(node.dataset.y);
    node.style.setProperty("--x", `${x}px`);
    return { x, y, kind: node.dataset.kind };
  });
  const svg = canvas.querySelector(".flow-links");
  if (!svg) return;
  svg.innerHTML = placed.slice(1).map((node, index) => {
    const d = linkPath(placed[index], node);
    const gate = node.kind === "gate" ? " to-gate" : "";
    return `<path class="glow${gate}" d="${d}"></path><path class="core${gate}" d="${d}"></path>`;
  }).join("");
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
  const log = document.querySelector(".messages");
  if (log) log.scrollTop = log.scrollHeight;
  fitScheme();
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
    return ["id", "day", "color", "icon", "scope"].every((key) => {
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

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("nb-theme", theme); } catch (error) {}
  render();
}

document.querySelector(".theme-switch").addEventListener("click", (event) => {
  const button = event.target.closest("[data-theme-set]");
  if (!button) return;
  setTheme(button.dataset.themeSet);
  button.focus();
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
    const field = document.getElementById("routine-time");
    if (field) field.focus();
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

app.addEventListener("keydown", (event) => {
  const node = event.target.closest(".flow-node");
  if (!node) return;
  if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft"].includes(event.key)) return;
  const nodes = [...app.querySelectorAll(".flow-node")];
  const index = nodes.indexOf(node);
  const delta = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1;
  const next = nodes[index + delta];
  if (!next) return;
  event.preventDefault();
  next.click();
});

app.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  const bot = activeBot();
  if (action === "pick-tariff") {
    state.tariffId = target.dataset.id;
    render();
  }
  if (action === "pay") {
    state.notice = "Подписка активна.";
    go("#bots");
    return;
  }
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
    state.activeChatId = "main";
    state.savedFlash = false;
    go("#bot");
    return;
  }
  if (action === "save-scenario") {
    bot.scenario = bot.draft;
    state.savedFlash = true;
    const nodes = nodesOf(bot);
    if (!nodes.some((node) => node.id === bot.nodeId)) bot.nodeId = nodes[0].id;
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
    return;
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
  if (action === "select-node") {
    bot.nodeId = target.dataset.id;
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
    main: false,
    nodeId: "start",
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
  const next = document.getElementById("composer");
  if (next) next.focus();
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
window.addEventListener("resize", fitScheme);
render();
