"use strict";

// Every DOM node is built with textContent, never innerHTML: board text comes from people and Claude.
const POLL_MS = 2000;
const SEARCH_DELAY_MS = 150;
const STATUS_MS = 6000;
const META_BRACKET = 5;
const DECK_TAGS_SHOWN = 4;
const THREATS_SHOWN = 4;
const STOPS_SHOWN = 4;
const ANSWERS_SHOWN = 6;
const ACTIVITY_KEY = "commander.activityOpen";
const COLOUR = /^#[0-9a-f]{3,8}$/i;

const state = {
  decks: [],
  current: null,
  view: null,
  stamps: {},
  metaStamp: null,
  tiles: new Map(),
  groups: new Map(),
  gridCards: new Map(),
  collapsed: new Set(),
  tagsKey: null,
  leaningsKey: null,
  selected: null,
  hovered: null,
  results: [],
  active: -1,
  busy: false,
};

const $ = (id) => document.getElementById(id);

// Moves a node only when it is out of place, so focus inside an unchanged node survives a refresh.
function place(list, node, index) {
  if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

async function api(name, ...args) {
  const answer = await window.pywebview.api[name](...args);
  if (answer && answer.error) {
    showStatus(answer.error, true);
    throw new Error(answer.error);
  }
  return answer;
}

let statusTimer = 0;
function showStatus(text, error = false) {
  const box = $("status");
  box.textContent = text;
  box.classList.toggle("error", error);
  box.hidden = false;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { box.hidden = true; }, STATUS_MS);
}

const euro = (cents) => (cents === null || cents === undefined ? "" : `€${(cents / 100).toFixed(2)}`);
const pct = (share) => `${Math.round(share * 100)}%`;

function manaCost(cost) {
  const box = el("span", { class: "cost", "aria-label": cost ? `cost ${cost}` : undefined });
  for (const symbol of (cost || "").matchAll(/\{([^}]+)\}/g)) {
    const text = symbol[1];
    const colour = /^[WUBRG]$/.test(text) ? text : "";
    box.append(el("span", { class: `pip ${colour}`, text, "aria-hidden": "true" }));
  }
  return box;
}

function cardImage(src, name, className) {
  const box = el("span", { class: `${className} ${src ? "" : "missing"}` },
    el("span", { class: "art-name", text: name, "aria-hidden": "true" }));
  if (src) {
    const img = el("img", { alt: "", loading: "lazy", src });
    img.addEventListener("error", () => { box.classList.add("missing"); img.remove(); });
    box.prepend(img);
  }
  return box;
}

/* Decks */

function renderDecks() {
  const list = $("deck-list");
  const seen = new Set();
  for (const [index, deck] of state.decks.entries()) {
    const key = String(deck.id);
    seen.add(key);
    let item = list.querySelector(`li[data-id="${key}"]`);
    if (!item) {
      const button = el("button", { class: "deck-button", type: "button", onclick: () => openDeck(deck.id) });
      item = el("li", { "data-id": key }, button);
      list.append(item);
    }
    const button = item.firstChild;
    button.replaceChildren(
      el("span", { class: "short", text: deck.short, title: deck.short }),
      deck.queued ? el("span", { class: "pill", text: String(deck.queued), title: `${deck.queued} queued` }) : el("span"),
      el("span", { class: "full", text: deck.name, title: deck.name }),
    );
    button.setAttribute("aria-current", state.current === deck.id ? "true" : "false");
    button.setAttribute("aria-label", `${deck.short}, ${deck.name}${deck.queued ? `, ${deck.queued} queued` : ""}`);
    place(list, item, index);
  }
  for (const item of [...list.children]) if (!seen.has(item.dataset.id)) item.remove();
}

async function loadDecks() {
  const answer = await api("decks");
  state.decks = answer.decks;
  $("loading").hidden = true;
  $("no-decks").hidden = state.decks.length > 0;
  renderDecks();
  if (state.current === null && state.decks.length) await openDeck(state.decks[0].id);
}

async function openDeck(id) {
  if (state.current !== id) {
    state.current = id;
    state.view = null;
    state.selected = null;
    state.hovered = null;
    state.tiles.clear();
    state.groups.clear();
    state.gridCards.clear();
    state.collapsed.clear();
    state.tagsKey = null;
    state.leaningsKey = null;
    $("list-considered").replaceChildren();
    $("grid").replaceChildren();
    closeLabelMenu(false);
    renderDecks();
    renderMeta(id, null);
    $("meta-empty").hidden = true;
  }
  await loadDeck();
  loadMeta();
}

async function loadDeck() {
  if (state.current === null) return;
  const view = await api("deck", state.current);
  if (view.id !== state.current) return;
  state.view = view;
  state.stamps[String(view.id)] = view.stamp;
  renderDeck();
}

/* Deck view */

function renderDeck() {
  const view = state.view;
  $("deck").hidden = false;
  $("deck-short").textContent = view.short;
  const link = $("deck-link");
  link.textContent = view.name;
  link.href = view.url;
  $("deck-bracket").textContent = view.bracket ? `Bracket ${view.bracket}` : "";
  $("deck-archetype").textContent = view.archetype || "";
  renderTags(view.tags);
  renderLeanings(view.leanings);
  const pending = view.pending;
  const review = $("review");
  review.disabled = pending === 0;
  review.textContent = pending ? `Review ${pending} change${pending === 1 ? "" : "s"}` : "Review changes";
  $("meta").hidden = view.bracket !== META_BRACKET;

  renderGrid(view.grid);
  renderConsidered(view.cards);
  paint();
  renderActivity(view.activity);
}

function renderTags(tags) {
  const key = JSON.stringify(tags);
  if (key === state.tagsKey) return;
  state.tagsKey = key;
  $("deck-tags").replaceChildren(...tags.map((tag) => el("li", { class: "tag", text: tag })));
}

// What the list plays far more of than recent decks with its commander: cut only like for like.
function renderLeanings(leanings) {
  const key = JSON.stringify(leanings);
  if (key === state.leaningsKey) return;
  state.leaningsKey = key;
  const items = leanings.map((leaning) =>
    el("li", {
      class: "leaning",
      text: `${leaning.tag} ${leaning.cards.length}`,
      title: `${leaning.cards.length} cards, recent decks ${leaning.peerMean} on average: ${leaning.cards.join(", ")}`,
    }),
  );
  const box = $("deck-leanings");
  if (!items.length) {
    box.replaceChildren();
    return;
  }
  box.replaceChildren(
    el("span", { class: "leanings-label", text: "Leans into", title: "Cut only for ideas that serve the same" }),
    el("ul", { class: "tags", "aria-label": "What the list leans into" }, ...items),
  );
}

/* The deck as a card grid, one stack per card type */

function renderGrid(groups) {
  const grid = $("grid");
  const seen = new Set();
  groups.forEach((group, index) => {
    seen.add(group.name);
    const key = JSON.stringify(group);
    let known = state.groups.get(group.name);
    if (!known || known.key !== key) {
      const node = buildGroup(group);
      if (known) known.node.replaceWith(node);
      known = { key, node, names: group.cards.map((c) => c.name) };
      state.groups.set(group.name, known);
    }
    place(grid, known.node, index);
  });
  for (const [name, known] of [...state.groups]) {
    if (!seen.has(name)) { known.node.remove(); state.groups.delete(name); }
  }
  state.gridCards.clear();
  for (const node of grid.querySelectorAll(".gcard")) state.gridCards.set(node.dataset.name, node);
}

function buildGroup(group) {
  const open = !state.collapsed.has(group.name);
  const stackId = `stack-${group.name.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  const toggle = el("button", { class: "group-toggle", type: "button", "aria-expanded": String(open), "aria-controls": stackId },
    el("span", { class: "chevron", "aria-hidden": "true" }),
    el("span", { class: "group-name", text: group.name }),
    el("span", { class: "count", text: `(${group.count})` }),
    group.cents ? el("span", { class: "group-price", text: euro(group.cents) }) : null);
  const stack = el("ul", { class: "stack", id: stackId, "aria-label": group.name, hidden: !open },
    ...group.cards.map(buildGridCard));
  toggle.addEventListener("click", () => {
    const opening = stack.hidden;
    stack.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening) state.collapsed.delete(group.name);
    else state.collapsed.add(group.name);
  });
  return el("section", { class: "group" }, el("h3", { class: "group-title" }, toggle), stack);
}

function buildGridCard(card) {
  const label = card.label || "";
  const flag = el("button", {
    class: `flag ${label ? "set" : ""}`, type: "button", "aria-haspopup": "menu",
    "aria-label": `Label for ${card.name}: ${label || "none"}`, title: label || "Set label",
  });
  if (label && COLOUR.test(card.labelColour)) flag.style.setProperty("--label", card.labelColour);
  flag.addEventListener("click", () => openLabelMenu(flag, card.name, label));
  const swap = el("button", { class: "swap", type: "button", hidden: true });
  swap.addEventListener("click", () => swapIn(swap.dataset.card, card.name));
  return el("li", { class: "gcard", "data-name": card.name, title: card.name },
    cardImage(card.image, card.name, "face"),
    card.quantity > 1 ? el("span", { class: "qty", text: `×${card.quantity}`, "aria-label": `${card.quantity} copies` }) : null,
    el("span", { class: "sr-only", text: card.name }),
    flag,
    el("span", { class: "out-note", hidden: true }),
    swap);
}

/* Considered: every board card with somewhere to go, plus those already queued */

function considered(cards) {
  return cards.filter((c) => c.status === "add" || (c.status === "open" && c.targets.length));
}

function renderConsidered(cards) {
  const shown = considered(cards);
  const live = new Set(shown.map((c) => c.id));
  for (const id of [...state.tiles.keys()]) {
    if (!live.has(id)) {
      state.tiles.get(id).node.remove();
      state.tiles.delete(id);
    }
  }
  if (!live.has(state.selected)) state.selected = null;
  if (!live.has(state.hovered)) state.hovered = null;
  const list = $("list-considered");
  shown.forEach((card, index) => place(list, tileFor(card), index));
  $("count-considered").textContent = shown.length ? `(${shown.length})` : "";
  $("empty-considered").hidden = shown.length > 0;
}

function tileFor(card) {
  const key = JSON.stringify(card);
  const known = state.tiles.get(card.id);
  if (known && known.key === key) return known.node;
  const node = buildTile(card);
  if (known) known.node.replaceWith(node);
  state.tiles.set(card.id, { key, node, card });
  return node;
}

function buildTile(card) {
  const queued = card.status === "add";
  const pick = el("button", {
    class: "pick", type: "button", "aria-pressed": "false", title: card.name,
    "aria-label": `${card.name}: show what it could replace`,
    onclick: () => select(card.id),
  }, el("span", { class: "card-name", text: card.name }));
  pick.addEventListener("focus", () => hover(card.id));
  pick.addEventListener("blur", () => hover(null));

  const meta = el("div", { class: "card-meta" },
    el("span", { class: "type", text: card.typeLine, title: card.typeLine }),
    card.priceCents !== null ? el("span", { text: euro(card.priceCents) }) : null,
    el("span", {
      class: `badge ${card.source}`,
      text: card.origin ?? card.source,
      title: card.origin ? `compiled by ${card.source} from the ${card.origin}` : `added by ${card.source}`,
    }),
    card.gameChanger ? el("span", { class: "badge status gc", text: "Game changer" }) : null);

  const targets = el("div", { class: "targets", hidden: true, role: "group", "aria-label": `Replace with ${card.name}` },
    ...card.targets.map((t) => el("button", {
      class: `target ${t.source}`, type: "button", text: t.name, title: t.reason,
      "aria-label": `Replace ${t.name}: ${t.reason}`, onclick: () => swapIn(card.id, t.name),
    })));

  const reasoning = [card.reason, card.note].filter(Boolean).join("\n");
  const body = el("div", { class: "card-body" },
    el("div", { class: "card-title" }, pick, manaCost(card.manaCost)),
    meta,
    queued ? el("p", { class: "replacing" },
      el("span", { text: `Replacing ${card.choice}` }),
      el("button", { class: "button quiet small", type: "button", text: "Undo", "aria-label": `Undo ${card.name} replacing ${card.choice}`,
        onclick: () => decide(card, "reopen") })) : null,
    targets,
    card.text ? el("details", { class: "fold" }, el("summary", { text: "Rules text" }), el("p", { text: card.text })) : null,
    reasoning ? el("details", { class: "fold" }, el("summary", { text: "Reasoning" }), el("p", { text: reasoning })) : null,
    card.dropping ? el("p", { class: "note", text: "Leaves the Maybeboard when you save" }) : null);

  const reject = el("button", {
    class: "button quiet icon reject", type: "button", text: "×", title: "Turn down",
    "aria-label": `Turn down ${card.name}`, onclick: () => decide(card, "reject"),
  });
  const tile = el("article", { class: `card ${queued ? "queued" : ""}`, role: "listitem", "data-id": card.id, "aria-label": card.name },
    cardImage(card.image, card.name, "art"), body, reject);
  tile.addEventListener("mouseenter", () => hover(card.id));
  tile.addEventListener("mouseleave", () => hover(null));
  tile.addEventListener("click", (event) => {
    if (!event.target.closest("button, a, summary, details")) select(card.id);
  });
  return tile;
}

function hover(id) {
  if (state.hovered === id) return;
  state.hovered = id;
  paint();
}

function select(id) {
  state.selected = state.selected === id ? null : id;
  paint();
  if (state.selected === null) return;
  // Bring the first target into view, opening its group if it was folded away.
  const card = state.tiles.get(id).card;
  const first = card.targets.map((t) => state.gridCards.get(t.name)).find(Boolean);
  if (!first) return;
  const stack = first.parentElement;
  if (stack.hidden) stack.previousElementSibling.querySelector("button").click();
  first.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Lights the grid for the considered card in focus: targets tinted, cards already queued out marked.
function paint() {
  const cards = [...state.tiles.values()].map((t) => t.card);
  const taken = new Map(cards.filter((c) => c.status === "add" && c.choice).map((c) => [c.choice, c]));
  const activeId = state.selected ?? state.hovered;
  const active = cards.find((c) => c.id === activeId) || null;
  const lit = new Map(active ? active.targets.filter((t) => !taken.has(t.name) || taken.get(t.name) === active).map((t) => [t.name, t]) : []);

  for (const [name, node] of state.gridCards) {
    const target = lit.get(name);
    const out = taken.get(name);
    node.classList.toggle("target", Boolean(target));
    node.classList.toggle("out", Boolean(out));
    const note = node.querySelector(".out-note");
    note.hidden = !out;
    note.textContent = out ? `→ ${out.name}` : "";
    const swap = node.querySelector(".swap");
    swap.hidden = !target;
    if (target) {
      swap.dataset.card = active.id;
      swap.setAttribute("aria-label", `Replace ${name} with ${active.name}`);
      swap.title = target.reason;
    }
  }
  for (const { node, card } of state.tiles.values()) {
    const on = card.id === state.selected;
    node.classList.toggle("selected", on);
    node.classList.toggle("lit", card.id === activeId);
    node.querySelector(".pick").setAttribute("aria-pressed", String(on));
    node.querySelector(".targets").hidden = !on;
  }
  $("grid").classList.toggle("choosing", Boolean(active));
}

async function swapIn(cardId, target) {
  const known = state.tiles.get(cardId);
  if (!known) return;
  try {
    await api("decide", state.current, cardId, "add", target, "");
    state.selected = null;
    showStatus(`${known.card.name} replaces ${target}`);
    await refresh();
  } catch { /* the status line already says what went wrong */ }
}

async function decide(card, decision) {
  try {
    await api("decide", state.current, card.id, decision, null, "");
    if (decision === "reject") showStatus(`Turned down ${card.name}`);
    await refresh();
    $("main").focus();
  } catch { /* shown in the status line */ }
}

/* Label menu: the corner flag on each grid card */

const menuState = { flag: null, name: null };

function openLabelMenu(flag, name, current) {
  if (menuState.flag === flag) { closeLabelMenu(true); return; }
  const menu = $("label-menu");
  menuState.flag = flag;
  menuState.name = name;
  menu.setAttribute("aria-label", `Label for ${name}`);
  menu.replaceChildren(...state.view.labels.map((label) => {
    const item = el("button", {
      class: "menu-item", type: "button", role: "menuitemradio", tabindex: "-1",
      "aria-checked": String(label.name === current),
      onclick: () => chooseLabel(name, current, label.name),
    }, el("span", { class: "swatch", "aria-hidden": "true" }), el("span", { text: label.name || "No label" }));
    if (COLOUR.test(label.colour)) item.style.setProperty("--label", label.colour);
    return item;
  }));
  flag.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  const box = flag.getBoundingClientRect();
  const height = menu.offsetHeight;
  const top = box.bottom + 4 + height > window.innerHeight ? Math.max(4, box.top - 4 - height) : box.bottom + 4;
  menu.style.top = `${top}px`;
  menu.style.left = `${Math.min(box.left, window.innerWidth - menu.offsetWidth - 4)}px`;
  (menu.querySelector('[aria-checked="true"]') || menu.firstChild).focus();
}

function closeLabelMenu(refocus) {
  const flag = menuState.flag;
  if (!flag) return;
  $("label-menu").hidden = true;
  flag.setAttribute("aria-expanded", "false");
  menuState.flag = null;
  menuState.name = null;
  if (refocus && flag.isConnected) flag.focus();
}

function setupLabelMenu() {
  const menu = $("label-menu");
  menu.addEventListener("keydown", (event) => {
    const items = [...menu.children];
    const at = items.indexOf(document.activeElement);
    const go = (index) => { event.preventDefault(); items[(index + items.length) % items.length].focus(); };
    if (event.key === "ArrowDown") go(at + 1);
    else if (event.key === "ArrowUp") go(at - 1);
    else if (event.key === "Home") go(0);
    else if (event.key === "End") go(items.length - 1);
    else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeLabelMenu(true); }
    else if (event.key === "Tab") closeLabelMenu(false);
  });
  document.addEventListener("mousedown", (event) => {
    if (menuState.flag && !menu.contains(event.target) && event.target !== menuState.flag) closeLabelMenu(false);
  });
  window.addEventListener("resize", () => closeLabelMenu(false));
  $("grid").addEventListener("scroll", () => closeLabelMenu(false), true);
}

async function chooseLabel(name, current, label) {
  const flag = menuState.flag;
  closeLabelMenu(true);
  if (label === current) return;
  flag.classList.add("saving");
  flag.setAttribute("aria-busy", "true");
  try {
    await api("set_label", state.current, name, label);
    showStatus(label ? `${name}: ${label}` : `${name}: no label`);
    await loadDeck();
    // The grid was rebuilt around the new label; keep the keyboard on this card's flag.
    const again = state.gridCards.get(name)?.querySelector(".flag");
    const lost = !document.activeElement || document.activeElement === document.body;
    if (again && lost) again.focus();
  } catch { /* shown in the status line */ }
  flag.classList.remove("saving");
  flag.removeAttribute("aria-busy");
}

/* Activity rail */

function setupActivity() {
  const toggle = $("activity-toggle");
  let open = false;
  try { open = localStorage.getItem(ACTIVITY_KEY) === "1"; } catch { /* storage is a convenience */ }
  const show = (value) => {
    toggle.setAttribute("aria-expanded", String(value));
    $("activity").hidden = !value;
    $("app").classList.toggle("activity-open", value);
    try { localStorage.setItem(ACTIVITY_KEY, value ? "1" : "0"); } catch { /* as above */ }
  };
  toggle.addEventListener("click", () => show(toggle.getAttribute("aria-expanded") !== "true"));
  show(open);
}

function renderActivity(entries) {
  const list = $("activity");
  list.replaceChildren(...entries.map((entry) => {
    const when = new Date(entry.at);
    const what = [entry.action, entry.card].filter(Boolean).join(" ");
    return el("li", {},
      el("time", { datetime: entry.at, text: when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) }),
      el("span", {}, el("span", { class: `who ${entry.actor}`, text: entry.actor }), document.createTextNode(` ${what}`)),
      entry.detail ? el("span", { class: "detail", text: entry.detail }) : null);
  }));
}

/* Collapsible panels */

function setupPanels() {
  for (const toggle of document.querySelectorAll(".panel-toggle")) {
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      $(toggle.getAttribute("aria-controls")).hidden = !open;
      if (open && toggle.id === "h-meta") loadMeta();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selected !== null && !document.querySelector("dialog[open]")) {
      state.selected = null;
      paint();
    }
  });
}

/* Proposing a card */

let searchTimer = 0;
function setupPropose() {
  const input = $("propose-input");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, SEARCH_DELAY_MS);
  });
  input.addEventListener("keydown", (event) => {
    const count = state.results.length;
    if (event.key === "ArrowDown" && count) { event.preventDefault(); highlight((state.active + 1) % count); }
    else if (event.key === "ArrowUp" && count) { event.preventDefault(); highlight((state.active - 1 + count) % count); }
    else if (event.key === "Enter") {
      event.preventDefault();
      const name = state.active >= 0 ? state.results[state.active].name : input.value.trim();
      if (name) propose(name);
    } else if (event.key === "Escape") closeResults();
  });
  input.addEventListener("blur", () => setTimeout(closeResults, 150));
}

async function search() {
  const text = $("propose-input").value.trim();
  if (text.length < 2 || state.current === null) { closeResults(); return; }
  try {
    const answer = await api("search", state.current, text);
    state.results = answer.cards;
    state.active = -1;
    const list = $("propose-results");
    list.replaceChildren(...answer.cards.map((card, index) => el("li", {
      role: "option", id: `result-${index}`, "aria-selected": "false",
      onmousedown: (event) => { event.preventDefault(); propose(card.name); },
    }, el("span", { text: card.name }), el("span", { class: "type", text: card.typeLine }))));
    list.hidden = answer.cards.length === 0;
    $("propose-box").setAttribute("aria-expanded", String(!list.hidden));
  } catch { closeResults(); }
}

function highlight(index) {
  state.active = index;
  const list = $("propose-results");
  [...list.children].forEach((item, i) => item.setAttribute("aria-selected", String(i === index)));
  $("propose-input").setAttribute("aria-activedescendant", `result-${index}`);
  list.children[index]?.scrollIntoView({ block: "nearest" });
}

function closeResults() {
  state.results = [];
  state.active = -1;
  $("propose-results").hidden = true;
  $("propose-box").setAttribute("aria-expanded", "false");
  $("propose-input").removeAttribute("aria-activedescendant");
}

async function propose(name) {
  closeResults();
  try {
    const answer = await api("propose", state.current, name);
    $("propose-input").value = "";
    await refresh();
    const shown = state.view && considered(state.view.cards).some((c) => c.id === answer.card.id);
    showStatus(shown ? `${answer.card.name} is considered` : `${answer.card.name} has nothing to replace`, !shown);
  } catch { /* shown in the status line */ }
}

/* Review and save */

function setupReview() {
  const dialog = $("review-dialog");
  $("review").addEventListener("click", openReview);
  $("review-cancel").addEventListener("click", () => dialog.close());
  $("review-save").addEventListener("click", saveChanges);
  dialog.addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
  dialog.addEventListener("close", () => $("review").focus());
}

function busy(text) {
  state.busy = Boolean(text);
  $("review-busy").hidden = !text;
  $("review-busy-text").textContent = text || "";
  $("review-save").disabled = state.busy;
  $("review-cancel").disabled = state.busy;
}

function showResolution(result) {
  $("review-size").textContent = result.before === result.after
    ? `${result.after} cards in the deck`
    : `${result.before} → ${result.after} cards in the deck`;
  $("review-changes").replaceChildren(...result.changes.map((c) => el("li", { class: c.mark === "+" ? "add" : "cut", text: c.text.replace(/^[+-] /, "") })));
  $("review-problems").replaceChildren(...result.problems.map((text) => el("li", { text })));
  $("review-warnings").replaceChildren(...result.warnings.map((text) => el("li", { text })));
}

async function openReview() {
  const dialog = $("review-dialog");
  $("review-deck").textContent = state.view.short;
  showResolution({ before: 0, after: 0, changes: [], problems: [], warnings: [] });
  $("review-size").textContent = "";
  const save = $("review-save");
  save.textContent = "Save to Archidekt";
  dialog.showModal();
  busy("Checking the deck on Archidekt…");
  try {
    const result = await api("preview", state.current);
    showResolution(result);
    busy("");
    const count = result.changes.length;
    save.textContent = `Save ${count} change${count === 1 ? "" : "s"} to Archidekt`;
    save.disabled = count === 0 || result.problems.length > 0;
    (save.disabled ? $("review-cancel") : save).focus();
  } catch {
    busy("");
    save.disabled = true;
  }
}

async function saveChanges() {
  busy("Saving to Archidekt…");
  try {
    const result = await api("commit", state.current);
    busy("");
    if (!result.saved) {
      showResolution(result);
      $("review-save").disabled = true;
      return;
    }
    $("review-dialog").close();
    const missed = result.mismatches.length ? `; Archidekt differs: ${result.mismatches.join("; ")}` : "";
    showStatus(`Saved. Restore point “${result.snapshot}” is on Archidekt${missed}`, Boolean(missed));
    await refresh();
  } catch {
    busy("");
  }
}

/* Meta (bracket 5): what in the meta stops the deck's key cards, and what stops the meta's combos */

const metaState = { key: null, deck: null, busy: false };

function metaWanted() {
  return state.view && state.view.id === state.current && state.view.bracket === META_BRACKET
    && $("h-meta").getAttribute("aria-expanded") === "true";
}

function setupMeta() {
  $("meta-refresh").addEventListener("click", refreshMeta);
}

async function loadMeta() {
  if (!metaWanted() || metaState.busy) return;
  const deck = state.current;
  try {
    const answer = await api("meta", deck);
    if (deck === state.current) renderMeta(deck, answer);
  } catch { /* shown in the status line */ }
}

async function refreshMeta() {
  const deck = state.current;
  metaState.busy = true;
  $("meta-busy").hidden = false;
  $("meta-refresh").disabled = true;
  try {
    const answer = await api("refresh_meta", deck);
    if (deck === state.current) renderMeta(deck, answer);
    if (!answer.report && answer.errors.length) showStatus(answer.errors[0], true);
    await loadDeck();
  } catch { /* shown in the status line */ }
  metaState.busy = false;
  $("meta-busy").hidden = true;
  $("meta-refresh").disabled = false;
}

function renderMeta(deck, answer) {
  const report = answer ? answer.report : null;
  const key = JSON.stringify(report ? answer : null);
  if (metaState.deck === deck && metaState.key === key) return;
  metaState.deck = deck;
  metaState.key = key;
  $("meta-empty").hidden = Boolean(report);
  $("meta-thin").hidden = !report || !report.sample.thin;
  if (!report) {
    $("meta-sample").textContent = "";
    $("meta-body").replaceChildren();
    return;
  }
  const s = report.sample;
  const fetched = report.sources.map((src) => src.fetchedAt).filter(Boolean).sort()[0];
  $("meta-sample").textContent = [
    s.known ? `${s.withLists} lists since ${report.since}` : "No EDHTop16 lists",
    fetched ? new Date(fetched).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "",
  ].filter(Boolean).join(" · ");
  $("meta-body").replaceChildren(metaKeys(answer.keys), metaStoppers(answer.stoppers, answer.untouched), metaCombos(answer.combos));
}

function deckChips(decks) {
  const chips = decks.slice(0, DECK_TAGS_SHOWN).map((d) =>
    el("li", { class: "deck-chip", text: `${d.short} ${pct(d.share)}`, title: `${d.name}: ${pct(d.share)} of lists` }));
  const rest = decks.slice(DECK_TAGS_SHOWN);
  if (rest.length) {
    chips.push(el("li", { class: "deck-chip more", text: `+${rest.length}`, title: rest.map((d) => `${d.short} ${pct(d.share)}`).join(", ") }));
  }
  return el("ul", { class: "deck-chips", "aria-label": "Played by" }, ...chips);
}

function metaSection(title, ...children) {
  const id = `meta-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  return el("section", { class: "meta-section", "aria-labelledby": id }, el("h3", { id, text: title }), ...children);
}

function shortList(items, shown, build) {
  const list = el("ul", { class: "meta-list" }, ...items.slice(0, shown).map(build));
  if (items.length <= shown) return list;
  return el("div", {}, list, el("details", { class: "fold" },
    el("summary", { text: `${items.length - shown} more` }), el("ul", { class: "meta-list" }, ...items.slice(shown).map(build))));
}

// The deck's key cards; hovering one says why it is key.
function metaKeys(keys) {
  return metaSection("Key cards",
    el("ul", { class: "deck-chips key-chips" }, ...keys.map((k) => el("li", { class: "deck-chip", text: k.name, title: k.reasons.join("\n") }))));
}

const ANSWER_ROLES = [["stax", "Stax"], ["removal", "Removal"], ["counter", "Counters"]];

// Each meta card that stops a combo of the deck's or hits a key card, once, grouped by how it stops them.
function metaStoppers(stoppers, untouched) {
  const groups = ANSWER_ROLES.map(([role, label]) => {
    const found = stoppers.filter((s) => s.role === role);
    if (!found.length) return null;
    return el("div", { class: "role-group" }, el("p", { class: "role-name", text: `${label} (${found.length})` }),
      shortList(found, THREATS_SHOWN, stopperRow));
  });
  const clear = [
    ...untouched.combos.map((c) => el("li", { class: "deck-chip stops combo" },
      el("a", { href: c.url, target: "_blank", rel: "noopener", text: c.pieces, title: "Open on Commander Spellbook" }))),
    ...untouched.keys.map((name) => el("li", { class: "deck-chip stops", text: name })),
  ];
  return metaSection("What stops you",
    stoppers.length ? el("div", { class: "role-groups" }, ...groups) : el("p", { class: "empty", text: "Nothing in the meta stops your combos or key cards." }),
    clear.length ? el("div", { class: "untouched" }, el("p", { class: "role-name", text: "Untouched" }), el("ul", { class: "deck-chips" }, ...clear)) : null);
}

function stopperRow(s) {
  const how = s.role === "stax" ? s.combos.map((c) => c.how).find(Boolean) : s.kinds.join(", ") || s.combos.map((c) => c.how).find(Boolean);
  const all = [...s.combos.map((c) => ({ combo: c, text: shortCombo(c.pieces) })), ...s.keys.map((name) => ({ text: name }))];
  const stops = all.slice(0, STOPS_SHOWN).map((item) => item.combo
    ? el("li", { class: "deck-chip stops combo", title: [item.combo.pieces, item.combo.how].filter(Boolean).join(": ") },
      el("a", { href: item.combo.url, target: "_blank", rel: "noopener", text: item.text }))
    : el("li", { class: "deck-chip stops", text: item.text }));
  const rest = all.slice(STOPS_SHOWN);
  if (rest.length) stops.push(el("li", { class: "deck-chip more", text: `+${rest.length}`, title: rest.map((item) => item.text).join("\n") }));
  return el("li", { class: "stopper" },
    el("p", { class: "threat" }, el("span", { class: "threat-name", text: s.card }), how ? el("span", { class: "muted", text: how }) : null, deckChips(s.decks)),
    el("ul", { class: "deck-chips", "aria-label": "Stops" }, ...stops));
}

// "Magda, Brazen Outlaw + Clock of Omens" reads as "Magda + Clock of Omens"; the full names show on hover.
function shortCombo(pieces) {
  return pieces.split(" + ").map((name) => name.split(",")[0]).join(" + ");
}

function metaCombos(combos) {
  const rows = combos.map((c) => el("div", { class: "meta-row" },
    el("p", { class: "meta-card" },
      el("a", { href: c.url, target: "_blank", rel: "noopener", text: c.pieces.join(" + "), title: "Open on Commander Spellbook" })),
    c.results.length ? el("p", { class: "muted small", text: c.results.slice(0, 2).join("; ") }) : null,
    deckChips(c.decks),
    comboAnswers(c.answers)));
  return metaSection("Meta combos",
    rows.length ? el("div", { class: "meta-rows" }, ...rows) : el("p", { class: "empty", text: "No combos in the meta." }));
}

function comboAnswers(answers) {
  if (!answers.length) return el("p", { class: "answers" }, el("span", { class: "badge status rejected", text: "No answer" }));
  // Stax answers say how they stop the combo; removal and counters only need the card and any catch.
  const shown = (a) => (a.role === "stax" ? `${a.card} (${a.how})` : a.catch ? `${a.card} (${a.catch})` : a.card);
  const lines = ANSWER_ROLES.map(([role, label]) => {
    const names = answers.filter((a) => a.role === role).map(shown);
    if (!names.length) return null;
    const text = names.length > ANSWERS_SHOWN ? `${names.slice(0, ANSWERS_SHOWN).join(", ")} and ${names.length - ANSWERS_SHOWN} more` : names.join(", ");
    return el("p", { class: "answers", title: names.join(", ") }, el("strong", { text: `${label}: ` }), document.createTextNode(text));
  });
  return el("div", {}, ...lines);
}

/* Keeping up with changes from the CLI and Claude */

async function refresh() {
  await Promise.all([loadDecks(), loadDeck()]);
}

async function poll() {
  try {
    const answer = await window.pywebview.api.stamps();
    if (!answer.error) {
      const stamps = answer.stamps;
      const key = String(state.current);
      const changed = JSON.stringify(stamps) !== JSON.stringify(state.stamps);
      const currentChanged = state.current !== null && stamps[key] !== state.stamps[key];
      // While a save runs, the deck is reloaded when it ends; keep the old stamp so the change is not lost.
      const deferred = currentChanged && state.busy;
      state.stamps = deferred ? { ...stamps, [key]: state.stamps[key] } : stamps;
      if (changed) await loadDecks();
      if (currentChanged && !deferred) await loadDeck();
      // `cmdr meta` fills the same cache; reread the open panel from it. A refresh here renders itself.
      const metaChanged = answer.meta !== state.metaStamp;
      state.metaStamp = answer.meta;
      if (metaChanged) await loadMeta();
    }
  } catch { /* a missed poll is retried on the next tick */ }
  setTimeout(poll, POLL_MS);
}

window.addEventListener("pywebviewready", async () => {
  setupPropose();
  setupReview();
  setupMeta();
  setupPanels();
  setupLabelMenu();
  setupActivity();
  try {
    await loadDecks();
    const answer = await window.pywebview.api.stamps();
    if (!answer.error) {
      state.stamps = answer.stamps;
      state.metaStamp = answer.meta;
    }
  } catch {
    $("loading").hidden = true;
  }
  setTimeout(poll, POLL_MS);
});
