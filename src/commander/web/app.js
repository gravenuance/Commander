"use strict";

// Every DOM node is built with textContent, never innerHTML: board text comes from people and Claude.
const POLL_MS = 2000;
const SEARCH_DELAY_MS = 150;
const STATUS_MS = 6000;
// A save changes the deck on Archidekt; its confirmation stays long enough to be noticed.
const SAVED_MS = 12000;
const META_BRACKET = 5;
const DECK_TAGS_SHOWN = 4;
const THREATS_SHOWN = 4;
const STOPS_SHOWN = 4;
const ANSWERS_SHOWN = 6;
const ACTIVITY_KEY = "commander.activityOpen";
const PANEL_KEY = "commander.panelOpen";
const SIZE_KEY = "commander.cardSize";
// Grid card widths; Scryfall's normal image is 488 px wide, sharp up to L even on a 2x screen.
const CARD_SIZES = { s: 124, m: 160, l: 200 };
const COLOUR = /^#[0-9a-f]{3,8}$/i;
// WCAG relative luminance at which black and white text have the same contrast on a colour.
const BLACK_INK_ABOVE = 0.179;

// Sets --label to the colour and --on-label to black or white, whichever reads better on it.
function setLabelColour(node, colour) {
  let hex = colour.slice(1);
  if (hex.length <= 4) hex = [...hex.slice(0, 3)].map((digit) => digit + digit).join("");
  const [red, green, blue] = [0, 2, 4]
    .map((at) => parseInt(hex.slice(at, at + 2), 16) / 255)
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  const luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  node.style.setProperty("--label", colour);
  node.style.setProperty("--on-label", luminance > BLACK_INK_ABOVE ? "#000000" : "#ffffff");
}

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

// Disables the controls while work runs, so a second click or Enter cannot send a second request.
async function whileBusy(controls, work) {
  if (controls.some((control) => control.disabled)) return;
  const focused = controls.find((control) => control === document.activeElement);
  for (const control of controls) {
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
  }
  try {
    await work();
  } finally {
    for (const control of controls) {
      control.disabled = false;
      control.removeAttribute("aria-busy");
    }
    const lost = !document.activeElement || document.activeElement === document.body;
    if (focused?.isConnected && lost) focused.focus();
  }
}

// Confirmations fade after ms; errors stay, announced at once, until dismissed.
let statusTimer = 0;
function showStatus(text, error = false, ms = STATUS_MS) {
  const box = $("status");
  const line = $("status-text");
  line.setAttribute("aria-live", error ? "assertive" : "polite");
  line.textContent = text;
  box.classList.toggle("error", error);
  box.hidden = false;
  clearTimeout(statusTimer);
  if (!error) statusTimer = setTimeout(hideStatus, ms);
}

function hideStatus() {
  clearTimeout(statusTimer);
  $("status").hidden = true;
}

const times = (copies, name) => (copies > 1 ? `${copies} × ${name}` : name);
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

// Counts deck requests, so an answer that arrives after a newer request was sent is dropped.
let deckRequest = 0;
async function loadDeck() {
  if (state.current === null) return;
  const sent = ++deckRequest;
  const view = await api("deck", state.current);
  if (sent !== deckRequest || view.id !== state.current) return;
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
  paintBracket();
  renderActivity(view.activity);
  loadMana();
  loadBracket();
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
      // The commander is never offered as a cut.
      if (group.name === "Commander") for (const card of node.querySelectorAll(".gcard")) card.dataset.commander = "1";
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
  if (label && COLOUR.test(card.labelColour)) setLabelColour(flag, card.labelColour);
  flag.addEventListener("click", () => openLabelMenu(flag, card.name, label));
  const swap = el("button", { class: "swap", type: "button", hidden: true });
  swap.addEventListener("click", () => swapIn(swap.dataset.card, card.name));
  const node = el("li", { class: "gcard", "data-name": card.name, title: card.name },
    cardImage(card.image, card.name, "face"),
    card.quantity > 1 ? el("span", { class: "qty", text: `×${card.quantity}`, "aria-label": `${card.quantity} copies` }) : null,
    el("span", { class: "sr-only", text: card.name }),
    flag,
    el("span", { class: "out-note", hidden: true }),
    swap);
  node.addEventListener("click", (event) => {
    if (!event.target.closest("button")) zoom(card);
  });
  if (card.image) {
    node.tabIndex = 0;
    node.addEventListener("keydown", (event) => {
      if (event.target === node && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        zoom(card);
      }
    });
  }
  return node;
}

// The card large in the middle of the screen, everything else faded; × or Esc closes it.
function zoom(card) {
  if (!card.image) return;
  const image = $("zoom-image");
  image.src = card.image.replace("/normal/", "/large/");
  image.alt = card.name;
  zoomOpener = document.activeElement;
  $("zoom").showModal();
  $("zoom-close").focus();
}

let zoomOpener = null;
function setupZoom() {
  const dialog = $("zoom");
  $("zoom-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (zoomOpener && zoomOpener.isConnected) zoomOpener.focus();
    zoomOpener = null;
  });
  // A click on the faded backdrop lands on the dialog itself, outside the image.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

/* Considering: every board card with somewhere to go, plus those already queued */

function considered(cards) {
  return sortSuggestions(cards.filter((c) => c.status === "add" || (c.status === "open" && c.targets.length)));
}

// Your cards, then Claude's, then meta ideas, then Archidekt deck ideas, then everything else. Within
// each group, cards over the deck's budget (ADR 0025) go last; then the most included first.
function suggestionGroup(card) {
  if (card.source === "you") return 0;
  if (card.source === "claude") return 1;
  if (card.origin === "meta") return 2;
  if (card.origin === "archidekt decks") return 3;
  return 4;
}

function sortSuggestions(cards) {
  return [...cards].sort((a, b) =>
    suggestionGroup(a) - suggestionGroup(b)
    || (a.withinBudget === false) - (b.withinBudget === false)
    || (b.inclusion ?? -1) - (a.inclusion ?? -1));
}

// Ideas with no deck card named to replace yet; selecting one offers every deck card as its cut.
function uncut(cards) {
  return sortSuggestions(cards.filter((c) => c.status === "open" && !c.targets.length));
}

function renderConsidered(cards) {
  const shown = considered(cards);
  const waiting = uncut(cards);
  const live = new Set([...shown, ...waiting].map((c) => c.id));
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
  const waitingList = $("list-uncut");
  waiting.forEach((card, index) => place(waitingList, tileFor(card), index));
  $("count-considered").textContent = shown.length ? `(${shown.length})` : "";
  $("uncut").hidden = !waiting.length;
  $("count-uncut").textContent = waiting.length ? `(${waiting.length})` : "";
  $("empty-considered").hidden = shown.length + waiting.length > 0;
  renderRejected(cards);
}

// The cards turned down, latest first, with why; closed until opened.
function renderRejected(cards) {
  const rejected = cards.filter((c) => c.status === "rejected")
    .sort((a, b) => (b.updatedAt || "").localeCompare(a.updatedAt || "") || a.name.localeCompare(b.name));
  const key = JSON.stringify(rejected.map((c) => [c.id, c.note, c.updatedAt]));
  if (state.rejectedKey === key) return;
  state.rejectedKey = key;
  $("rejected").hidden = !rejected.length;
  $("count-rejected").textContent = rejected.length ? `(${rejected.length})` : "";
  $("list-rejected").replaceChildren(...rejected.map((card) => el("li", { class: "rejected-card" },
    el("p", { class: "rejected-head" },
      el("span", { class: "card-name", text: card.name }),
      card.updatedAt ? el("span", { class: "muted small", text: new Date(card.updatedAt).toLocaleDateString(undefined, { dateStyle: "medium" }) }) : null,
      el("button", { class: "button quiet small", type: "button", text: "Reconsider", "aria-label": `Reconsider ${card.name}`,
        onclick: () => decide(card, "reopen") })),
    card.note ? el("p", { class: "muted small rejected-note", text: card.note }) : null)));
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
  }, el("span", { class: "card-name", text: times(card.copies, card.name) }));
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
    card.gameChanger ? el("span", { class: "badge status gc", text: "Game changer" }) : null,
    card.withinBudget === false
      ? el("span", { class: "badge status over", text: "Over budget", title: "Over 25% above the deck's average card" })
      : null);

  const targets = el("div", { class: "targets", hidden: true, role: "group", "aria-label": `Replace with ${card.name}` },
    ...(card.targets.length
      ? card.targets.map((t) => el("button", {
        class: `target ${t.source}`, type: "button", text: t.name, title: t.reason,
        "aria-label": `Replace ${t.name}: ${t.reason}`, onclick: () => swapIn(card.id, t.name),
      }))
      : [el("p", { class: "muted small", text: "Pick the deck card it replaces" })]));

  const reasoning = [card.reason, card.note].filter(Boolean).join("\n");
  const body = el("div", { class: "card-body" },
    el("div", { class: "card-title" }, pick, manaCost(card.manaCost)),
    meta,
    queued ? el("p", { class: "replacing" },
      el("span", { text: `Replacing ${times(card.copies, card.choice)}` }),
      el("button", { class: "button quiet small", type: "button", text: "Undo", "aria-label": `Undo ${card.name} replacing ${card.choice}`,
        onclick: () => decide(card, "reopen") })) : null,
    targets,
    card.text ? el("details", { class: "fold" }, el("summary", { text: "Rules text" }), el("p", { text: card.text })) : null,
    reasoning ? el("details", { class: "fold" }, el("summary", { text: "Reasoning" }), el("p", { text: reasoning })) : null,
    card.dropping ? el("p", { class: "note", text: "Leaves the Maybeboard when you save" }) : null);

  const why = rejectForm(card);
  body.append(why);
  const reject = el("button", {
    class: "button quiet icon reject", type: "button", text: "×", title: "Turn down",
    "aria-label": `Turn down ${card.name}`, onclick: () => {
      why.hidden = false;
      why.querySelector("input").focus();
    },
  });
  const tile = el("article", { class: `card ${queued ? "queued" : ""}`, role: "listitem", "data-id": card.id, "aria-label": card.name },
    cardImage(card.image, card.name, "art"), body, reject);
  tile.addEventListener("mouseenter", () => hover(card.id));
  tile.addEventListener("mouseleave", () => hover(null));
  tile.addEventListener("click", (event) => {
    if (!event.target.closest("button, a, summary, details, form")) select(card.id);
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
    // A clicked idea can replace any deck card not already going out; the suggested ones stand out.
    const swap = node.querySelector(".swap");
    const chosen = active && state.selected === active.id && !out && !node.dataset.commander;
    swap.hidden = !target && !chosen;
    swap.classList.toggle("any", !target);
    if (!swap.hidden) {
      swap.dataset.card = active.id;
      swap.setAttribute("aria-label", `Replace ${name} with ${active.name}`);
      swap.title = target ? target.reason : `Replace ${name} (your pick, not suggested)`;
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
  // Every swap control goes quiet, since a second one would queue a second replacement.
  const controls = [...document.querySelectorAll(".swap, .targets button")];
  await whileBusy(controls, async () => {
    try {
      await api("decide", state.current, cardId, "add", target, "");
      state.selected = null;
      showStatus(`${known.card.name} replaces ${target}`);
      await refresh();
    } catch { /* the status line already says what went wrong */ }
  });
}

// Asks why a card is turned down; the reason is kept on the board and shows under Turned down.
function rejectForm(card) {
  const input = el("input", { type: "text", class: "input small", placeholder: "Why? (optional)", maxlength: "200",
    "aria-label": `Why turn down ${card.name}` });
  const form = el("form", { class: "reject-form", hidden: true },
    input,
    el("button", { class: "button small", type: "submit", text: "Turn down" }),
    el("button", { class: "button quiet small", type: "button", text: "Cancel", onclick: () => { form.hidden = true; } }));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    decide(card, "reject", input.value.trim());
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      form.hidden = true;
    }
  });
  return form;
}

async function decide(card, decision, note = "") {
  const tile = state.tiles.get(card.id)?.node;
  const controls = tile ? [...tile.querySelectorAll("button, input")] : [];
  await whileBusy(controls, async () => {
    try {
      await api("decide", state.current, card.id, decision, null, note);
      if (decision === "reject") showStatus(`Turned down ${card.name}`);
      await refresh();
      $("main").focus();
    } catch { /* shown in the status line */ }
  });
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
    if (COLOUR.test(label.colour)) setLabelColour(item, label.colour);
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
  await whileBusy([flag], async () => {
    try {
      await api("set_label", state.current, name, label);
      showStatus(label ? `${name}: ${label}` : `${name}: no label`);
      await loadDeck();
    } catch { /* shown in the status line */ }
  });
  flag.classList.remove("saving");
  // The grid was rebuilt around the new label; keep the keyboard on this card's flag.
  const again = state.gridCards.get(name)?.querySelector(".flag");
  const lost = !document.activeElement || document.activeElement === document.body;
  if (again && lost) again.focus();
}

/* Activity rail */

// The right-hand rail: Activity or Mana open, or both folded to their labels. Remembered per viewer.
const RAIL_TABS = ["activity", "mana"];
let railTab = null;

function setupActivity() {
  let open = null;
  try {
    const stored = localStorage.getItem(ACTIVITY_KEY);
    open = stored === "1" ? "activity" : RAIL_TABS.includes(stored) ? stored : null;
  } catch { /* storage is a convenience */ }
  for (const tab of RAIL_TABS) {
    $(`${tab}-toggle`).addEventListener("click", () => showRail(railTab === tab ? null : tab));
  }
  // The open rail covers part of the main view, so Escape from inside it folds it and returns focus to its tab.
  $("activity-pane").addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || railTab === null) return;
    event.stopPropagation();
    const toggle = $(`${railTab}-toggle`);
    showRail(null);
    toggle.focus();
  });
  showRail(open);
}

function showRail(tab) {
  railTab = tab;
  for (const name of RAIL_TABS) {
    $(`${name}-toggle`).setAttribute("aria-expanded", String(name === tab));
    $(name).hidden = name !== tab;
  }
  $("activity-pane").classList.toggle("open", tab !== null);
  try { localStorage.setItem(ACTIVITY_KEY, tab || "0"); } catch { /* as above */ }
  if (tab === "mana") loadMana();
}

/* Bracket: Spellbook's estimate beside the set bracket and the ceiling, and the cards that lift it (ADR 0021) */

const bracketState = { deck: null, key: null, marks: new Map() };

function setupBracket() {
  const chip = $("bracket-check");
  chip.addEventListener("click", () => {
    if (!chip.hasAttribute("aria-expanded")) return;
    const opening = $("bracket-detail").hidden;
    $("bracket-detail").hidden = !opening;
    chip.setAttribute("aria-expanded", String(opening));
  });
}

async function loadBracket() {
  if (state.current === null) return;
  const deck = state.current;
  try {
    const answer = await api("bracket", deck);
    if (deck === state.current) renderBracket(deck, answer);
  } catch { /* shown in the status line */ }
}

function renderBracket(deck, b) {
  const key = JSON.stringify(b);
  if (bracketState.deck === deck && bracketState.key === key) return;
  const newDeck = bracketState.deck !== deck;
  Object.assign(bracketState, { deck, key, marks: bracketMarks(b.causes) });
  const chip = $("bracket-check");
  const detail = $("bracket-detail");
  const gap = b.notes.length > 0;
  chip.hidden = !b.estimate && !gap;
  chip.textContent = b.estimate ? `Estimate ${b.estimate.brackets}` : "No estimate";
  chip.title = b.estimate ? `Commander Spellbook: ${b.estimate.tag}` : (b.unavailable || "");
  chip.classList.toggle("warn", gap);
  if (gap) {
    chip.setAttribute("aria-controls", "bracket-detail");
    if (newDeck || !chip.hasAttribute("aria-expanded")) chip.setAttribute("aria-expanded", "false");
  } else {
    chip.removeAttribute("aria-controls");
    chip.removeAttribute("aria-expanded");
  }
  detail.hidden = !gap || chip.getAttribute("aria-expanded") !== "true";
  detail.replaceChildren(
    ...b.notes.map((note) => el("p", { class: "bracket-note", text: note })),
    b.causes.length ? el("ul", { class: "causes", "aria-label": "What lifts the estimate" }, ...b.causes.map(causeItem)) : null,
  );
  paintBracket();
}

function causeItem(cause) {
  const cards = cause.comboUrl
    ? el("a", { href: cause.comboUrl, target: "_blank", rel: "noopener", text: cause.cards.join(" + "), title: "Open on Commander Spellbook" })
    : el("span", { text: cause.cards.join(", ") });
  return el("li", {},
    el("span", { class: "cause-tag", text: cause.brackets, title: cause.tag }),
    el("span", { text: `${cause.reason}: ` }),
    cards);
}

// Per card: the highest bracket a cause lifts it to (causes come highest first), and every reason.
function bracketMarks(causes) {
  const marks = new Map();
  for (const cause of causes) {
    for (const name of cause.cards) {
      const mark = marks.get(name) || { brackets: cause.brackets, reasons: [] };
      mark.reasons.push(`${cause.brackets}: ${cause.reason}`);
      marks.set(name, mark);
    }
  }
  return marks;
}

function paintBracket() {
  const marks = bracketState.deck === state.current ? bracketState.marks : new Map();
  for (const [name, node] of state.gridCards) {
    const mark = marks.get(name);
    let badge = node.querySelector(".bracket-mark");
    if (!mark) {
      if (badge) badge.remove();
      continue;
    }
    if (!badge) {
      badge = el("span", { class: "bracket-mark" });
      node.append(badge);
    }
    badge.textContent = mark.brackets;
    badge.title = mark.reasons.join("\n");
    badge.setAttribute("aria-label", `Lifts the bracket to ${mark.reasons.join("; ")}`);
  }
}

/* Account: who is signed in, signing in and out, and the app's sync (ADR 0023) */

const accountState = { user: null, signedIn: false, syncing: false };

function setupAccount() {
  $("sign-in").addEventListener("click", openSignIn);
  $("sign-out").addEventListener("click", signOut);
  $("sync-decks").addEventListener("click", startSync);
  $("no-decks-sync").addEventListener("click", startSync);
  $("signin-cancel").addEventListener("click", () => $("signin-dialog").close());
  $("signin-form").addEventListener("submit", (event) => {
    event.preventDefault();
    signIn();
  });
}

async function loadAccount() {
  const answer = await window.pywebview.api.account();
  if (answer.error) return null;
  renderAccount(answer);
  return answer;
}

function renderAccount(a) {
  const wasSyncing = accountState.syncing;
  Object.assign(accountState, { user: a.user, signedIn: a.signedIn, syncing: a.sync.running });
  const name = $("account-name");
  name.textContent = a.user || "";
  name.title = !a.user ? "" : a.signedIn ? `Signed in to Archidekt as ${a.user}` : `${a.user}, not signed in`;
  $("sign-in").hidden = Boolean(a.user && a.signedIn);
  $("sign-out").hidden = !a.user;
  for (const id of ["sync-decks", "no-decks-sync"]) {
    $(id).hidden = !a.user;
    $(id).disabled = a.sync.running;
  }
  const status = $("sync-status");
  status.textContent = a.sync.running ? a.sync.progress : "";
  status.title = status.textContent;
  if (wasSyncing && !a.sync.running && a.sync.summary) {
    showStatus(a.sync.summary, a.sync.failed);
    loadDecks();
  }
}

function openSignIn() {
  $("signin-user").value = accountState.user || "";
  $("signin-password").value = "";
  $("signin-error").hidden = true;
  $("signin-dialog").showModal();
  (accountState.user ? $("signin-password") : $("signin-user")).focus();
}

async function signIn() {
  const submit = $("signin-submit");
  const password = $("signin-password");
  submit.disabled = true;
  $("signin-error").hidden = true;
  try {
    const answer = await window.pywebview.api.sign_in($("signin-user").value, password.value);
    // The password leaves the page either way: a retry types it again.
    password.value = "";
    if (answer.error) {
      $("signin-error").textContent = answer.error;
      $("signin-error").hidden = false;
      password.focus();
      return;
    }
    const switched = answer.user !== accountState.user;
    $("signin-dialog").close();
    renderAccount(answer);
    if (switched) await showAccountDecks();
  } catch {
    $("signin-error").textContent = "Signing in failed; the details are in the log.";
    $("signin-error").hidden = false;
  } finally {
    submit.disabled = false;
  }
}

// Another account's decks live in another folder: start the deck list over.
async function showAccountDecks() {
  state.current = null;
  state.view = null;
  state.stamps = {};
  $("deck").hidden = true;
  await loadDecks();
}

async function signOut() {
  try {
    renderAccount(await api("sign_out"));
    openSignIn();
  } catch { /* shown in the status line */ }
}

async function startSync() {
  try {
    await api("sync");
    await loadAccount();
  } catch { /* shown in the status line */ }
}

/* Mana: the deck's mana base against Frank Karsten's published numbers (ADR 0019) */

const manaState = { deck: null, key: null };
const COLOUR_NAMES = { W: "White", U: "Blue", B: "Black", R: "Red", G: "Green", C: "Colourless" };
const KARSTEN_LANDS = "https://www.tcgplayer.com/content/article/How-Many-Lands-Do-You-Need-in-Your-Deck-An-Updated-Analysis/cd1c1a24-d439-4a8e-b369-b936edb0b38a/";
const KARSTEN_SOURCES = "https://www.tcgplayer.com/content/article/How-Many-Sources-Do-You-Need-to-Consistently-Cast-Your-Spells-A-2022-Update/dc23a7d2-0a16-4c0b-ad36-586fcca03ad8/";

async function loadMana() {
  if (railTab !== "mana" || state.current === null) return;
  const deck = state.current;
  try {
    const answer = await api("mana", deck);
    if (deck === state.current) renderMana(deck, answer);
  } catch { /* shown in the status line */ }
}

function renderMana(deck, m) {
  const key = JSON.stringify(m);
  if (manaState.deck === deck && manaState.key === key) return;
  Object.assign(manaState, { deck, key });
  const l = m.lands;
  const c = m.comparison;
  const cedh = state.view && state.view.bracket === META_BRACKET;
  const fmt = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));
  const lands = el("section", { class: "mana-section" },
    el("h3", { text: "Lands" }),
    el("p", { class: "mana-big" }, el("strong", { text: fmt(l.lands) }), document.createTextNode(" lands"),
      l.landSpells ? el("span", { class: "muted small", text: ` (${l.landSpells} land/spell card${l.landSpells === 1 ? "" : "s"} as part of a land)` }) : null),
    c ? el("p", { class: "mana-line" },
      el("span", { text: `${c.lists} ${c.label}: median ${fmt(c.lands)} (${fmt(c.low)}–${fmt(c.high)})` }),
      el("span", { class: "muted small", text: `Mana rocks, dorks and rituals: yours ${c.deckManaCards}, theirs ${fmt(c.manaCards)}` }))
      : el("p", { class: "muted small", text: m.compareWith ? `No ${m.compareWith} to compare with yet; they come with the next refresh.` : "Nothing to compare with." }),
    el("p", { class: `mana-line ${cedh ? "muted" : ""}` },
      el("span", { text: `Karsten's formula: ${fmt(l.suggested)}` }),
      el("span", { class: "muted small", text: `average mana value ${fmt(l.averageManaValue)}, ${l.cheapDraw} cheap draw, ${l.cheapRamp} cheap ramp` }),
      cedh ? el("span", { class: "muted small", text: "Made for casual games; the tournament lists are the better guide here." }) : null));
  const colours = el("section", { class: "mana-section" }, el("h3", { text: "Colour sources" }),
    ...m.colours.map((colour) => {
      const short = colour.needs.filter((n) => n.short);
      const hardest = colour.needs[0];
      return el("div", { class: "mana-colour" },
        el("p", { class: "mana-colour-head" },
          el("span", { class: `pip ${colour.colour}`, text: colour.colour, title: COLOUR_NAMES[colour.colour] }),
          el("span", { text: `${fmt(colour.lands)} from lands` }),
          el("span", { class: short.length ? "badge status rejected" : "muted small",
            text: short.length ? `${short.length} short` : hardest ? `all ${colour.needs.length} cards fine` : "no cards need it" })),
        short.length ? el("ul", { class: "mana-needs" }, ...short.map(needRow)) : null);
    }));
  const tapped = el("section", { class: "mana-section" }, el("h3", { text: "Turn one" }),
    m.turnOne.length
      ? el("ul", { class: "mana-needs" }, ...m.turnOne.map(needRow))
      : el("p", { class: "muted small", text: "Every one-drop has enough untapped sources." }),
    el("p", { class: "muted small", text: `Lands that enter tapped: ${fmt(m.tappedLands)}` }));
  const skipped = m.skipped.length
    ? el("details", { class: "fold" }, el("summary", { text: `Not counted (${m.skipped.length})` }),
      el("p", { text: `Cast for an alternative cost, or paid with hybrid or Phyrexian mana: ${m.skipped.join(", ")}` }))
    : null;
  const source = el("p", { class: "muted small mana-source" }, document.createTextNode("From Frank Karsten's "),
    el("a", { href: KARSTEN_LANDS, target: "_blank", rel: "noopener", text: "land count" }), document.createTextNode(" and "),
    el("a", { href: KARSTEN_SOURCES, target: "_blank", rel: "noopener", text: "colour source" }), document.createTextNode(" articles."));
  $("mana").replaceChildren(lands, colours, tapped, skipped || "", m.plan ? planSection(deck, m.plan) : "", source);
}

// The suggested mana base below bracket 5 (ADR 0024): land count, basics by colour, then named swaps.
function planSection(deck, p) {
  const section = el("section", { class: "mana-section mana-plan", "aria-labelledby": "mana-plan-title" },
    el("h3", { id: "mana-plan-title", text: "Suggested changes" }));
  if (p.unchanged) {
    section.append(el("p", { class: "muted small", text: "Meets Karsten's numbers; nothing to change." }));
    return section;
  }
  if (p.landChange) {
    const run = Math.round(p.lands + p.landChange);
    section.append(el("p", { class: "mana-line" },
      el("span", {}, el("strong", { text: `Run ${run} lands` }),
        el("span", { class: "muted", text: ` (${p.landChange > 0 ? "+" : "−"}${Math.abs(p.landChange)})` })),
      p.cutCandidates.length
        ? el("span", { class: "muted small", text: `Room from: ${p.cutCandidates.join(", ")}` })
        : el("span", { class: "muted small", text: "Fewer basics, as below." })));
  }
  section.append(el("ul", { class: "mana-basics", "aria-label": "Basic lands and cards short of sources" },
    ...p.basics.map((b) => {
      const outcome = p.colours.find((c) => c.colour === b.colour);
      return el("li", { class: `mana-basic${b.now === b.after ? "" : " changed"}` },
        el("span", { class: `pip ${b.colour}`, text: b.colour, title: COLOUR_NAMES[b.colour] }),
        el("span", {}, document.createTextNode(`${b.name} ${b.now} → ${b.after}`),
          b.replaced ? el("span", { class: "muted small", text: ` (−${b.replaced} for the lands below)` }) : null),
        outcome ? el("span", { class: "muted small", text: `${outcome.shortNow} → ${outcome.shortAfter} short` }) : null);
    })));
  const within = p.swaps.filter((s) => s.withinBudget);
  const over = p.swaps.filter((s) => !s.withinBudget);
  const budget = p.budgetCents !== null ? ` (up to ${euro(p.budgetCents)})` : "";
  if (within.length) section.append(swapGroup(over.length ? `Within budget${budget}` : "", within));
  if (over.length) section.append(swapGroup("Over budget", over));
  if (p.stillShort.length) {
    section.append(el("p", { class: "muted small", text: `Still short: ${p.stillShort.join(", ")}` }));
  }
  const waiting = p.swaps.filter((s) => !s.board).length;
  if (waiting) {
    section.append(el("button", {
      class: "button primary small", type: "button", text: `Put ${waiting} swap${waiting === 1 ? "" : "s"} on the board`,
      onclick: (event) => manaToBoard(deck, event.currentTarget),
    }));
  }
  return section;
}

// Swaps under a small heading; with no heading when every swap is within budget.
function swapGroup(title, swaps) {
  const list = el("ul", { class: "mana-swaps", "aria-label": title || "Swaps" }, ...swaps.map((s) =>
    el("li", { class: "mana-swap", title: s.reason },
      el("span", { class: "swap-add", text: `+ ${times(s.copies, s.add)}` }),
      el("span", { class: "muted small", text: `for ${times(s.copies, s.cut)}` }),
      s.board ? el("span", { class: "badge status", text: s.board === "rejected" ? "Turned down" : "On the board" })
        : s.cents !== null ? el("span", { class: "muted small", text: euro(s.cents) }) : null)));
  return title ? el("div", { class: "mana-swap-group" }, el("p", { class: "mana-group-title", text: title }), list) : list;
}

async function manaToBoard(deck, button) {
  button.disabled = true;
  try {
    const answer = await api("mana_to_board", deck);
    const n = answer.added.length;
    showStatus(`${n} swap${n === 1 ? "" : "s"} put under Considering`);
    if (deck === state.current) await loadDeck();
    loadMana();
  } catch {
    button.disabled = false; /* shown in the status line */
  }
}

function needRow(n) {
  return el("li", { class: "mana-need", title: `${n.pattern}: needs ${n.needed} sources, has ${n.have}` },
    el("span", { class: "card-name", text: n.card }),
    el("span", { class: "muted small", text: n.pattern }),
    el("span", { class: "mana-gap", text: `${n.have} / ${n.needed}` }));
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

/* Settings: the profile's commanders that play alike and themes to ignore */

const settingsState = { deck: null, adjacent: [], ignore: [], themes: [], leanings: [], max: 8, pairWith: null };

async function openSettings() {
  const deck = state.current;
  try {
    const answer = await api("settings", deck);
    Object.assign(settingsState, {
      deck, adjacent: answer.adjacent, ignore: answer.ignore, themes: answer.themes,
      leanings: answer.leanings, max: answer.maxAdjacent, pairWith: null,
    });
    $("settings-deck").textContent = $("deck-short").textContent;
    renderSettings();
    $("settings-dialog").showModal();
    $("settings-adjacent-input").focus();
  } catch { /* shown in the status line */ }
}

function renderSettings() {
  const s = settingsState;
  const chip = (text, remove, ...extra) => el("li", { class: "deck-chip setting-chip" }, el("span", { text }), ...extra,
    el("button", { class: "chip-remove", type: "button", text: "×", "aria-label": `Remove ${text}`, onclick: remove }));
  $("settings-adjacent").replaceChildren(...s.adjacent.map((group, index) => chip(group.join(" + "), () => {
    s.adjacent.splice(index, 1);
    s.pairWith = null;
    renderSettings();
  }, group.length === 1 ? el("button", {
    class: "chip-pair", type: "button", text: s.pairWith === index ? "picking partner…" : "+ partner",
    "aria-pressed": String(s.pairWith === index), "aria-label": `Pair ${group[0]} with a partner`,
    onclick: () => {
      s.pairWith = s.pairWith === index ? null : index;
      renderSettings();
      $("settings-adjacent-input").focus();
    },
  }) : null)));
  const input = $("settings-adjacent-input");
  const full = s.adjacent.length >= s.max && s.pairWith === null;
  input.disabled = full;
  input.placeholder = full ? `At most ${s.max}` : s.pairWith !== null ? `Partner for ${s.adjacent[s.pairWith][0]}` : "Add a commander";
  $("settings-ignore").replaceChildren(...s.ignore.map((name, index) => chip(name, () => {
    s.ignore.splice(index, 1);
    renderSettings();
  })));
  // The deck's own themes and leanings, one click to ignore.
  const ignored = new Set(s.ignore.map((name) => name.toLowerCase()));
  const offers = [...s.themes.map((t) => ({ name: t.name, kind: t.kind })), ...s.leanings.map((tag) => ({ name: tag, kind: "leaning" }))]
    .filter((o) => !ignored.has(o.name.toLowerCase()));
  $("settings-offers").replaceChildren(...(offers.length ? [
    el("p", { class: "muted small", text: "This deck's themes:" }),
    el("ul", { class: "deck-chips" }, ...offers.map((o) => el("li", {},
      el("button", { class: "deck-chip offer", type: "button", text: `+ ${o.name}`, title: `Ignore this ${o.kind}`,
        onclick: () => addIgnore(o.name) })))),
  ] : []));
}

function addAdjacent(text) {
  const s = settingsState;
  if (s.pairWith !== null && s.adjacent[s.pairWith]) {
    s.adjacent[s.pairWith] = [s.adjacent[s.pairWith][0], text.trim()];
    s.pairWith = null;
    $("settings-adjacent-input").value = "";
    renderSettings();
    return;
  }
  const group = text.split("+").map((name) => name.trim()).filter(Boolean);
  if (!group.length || group.length > 2) {
    showStatus("A commander, or two partners joined by +", true);
    return;
  }
  const key = group.map((n) => n.toLowerCase()).sort().join("|");
  if (!settingsState.adjacent.some((g) => g.map((n) => n.toLowerCase()).sort().join("|") === key)) settingsState.adjacent.push(group);
  $("settings-adjacent-input").value = "";
  renderSettings();
}

function addIgnore(name) {
  const text = name.trim();
  if (text && !settingsState.ignore.some((n) => n.toLowerCase() === text.toLowerCase())) settingsState.ignore.push(text);
  $("settings-ignore-input").value = "";
  renderSettings();
}

async function saveSettings() {
  const s = settingsState;
  try {
    await api("save_settings", s.deck, s.adjacent, s.ignore);
    $("settings-dialog").close();
    showStatus("Settings saved to the deck's profile.toml");
    await refresh();
  } catch { /* shown in the status line */ }
}

// A text box that offers matches as you type; arrows move, Enter takes the highlighted match or the text.
function autocomplete(input, list, find, pick) {
  let timer = 0;
  let sent = 0;
  let items = [];
  let active = -1;
  const close = () => { items = []; active = -1; list.hidden = true; input.parentElement.setAttribute("aria-expanded", "false"); };
  const mark = (index) => {
    active = index;
    [...list.children].forEach((item, i) => item.setAttribute("aria-selected", String(i === index)));
    list.children[index]?.scrollIntoView({ block: "nearest" });
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const mine = ++sent;
    timer = setTimeout(async () => {
      let found = [];
      try { found = await find(input.value); } catch { /* the status line says what went wrong */ }
      // A newer keystroke supersedes this answer; a box that lost focus gets no list back.
      if (mine !== sent) return;
      if (document.activeElement !== input) { close(); return; }
      items = found;
      active = -1;
      list.replaceChildren(...items.map((item) => el("li", {
        role: "option", "aria-selected": "false",
        onmousedown: (event) => { event.preventDefault(); close(); pick(item.value); },
      }, el("span", { text: item.label }), item.hint ? el("span", { class: "type", text: item.hint }) : null)));
      list.hidden = items.length === 0;
      input.parentElement.setAttribute("aria-expanded", String(!list.hidden));
    }, SEARCH_DELAY_MS);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" && items.length) { event.preventDefault(); mark((active + 1) % items.length); }
    else if (event.key === "ArrowUp" && items.length) { event.preventDefault(); mark((active - 1 + items.length) % items.length); }
    else if (event.key === "Enter") {
      event.preventDefault();
      const value = active >= 0 ? items[active].value : input.value.trim();
      sent++;
      close();
      if (value) pick(value);
    } else if (event.key === "Escape" && !list.hidden) { event.preventDefault(); event.stopPropagation(); close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

function setupSettings() {
  $("settings").addEventListener("click", openSettings);
  $("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
  $("settings-save").addEventListener("click", saveSettings);
  const adjacent = $("settings-adjacent-input");
  // A partner pair is typed "First + Second": matches are for the name after the last +.
  autocomplete(adjacent, $("settings-adjacent-results"), async (text) => {
    const parts = text.split("+");
    const typed = parts.pop().trim();
    if (typed.length < 2) return [];
    const head = parts.map((p) => p.trim()).filter(Boolean);
    const answer = await api("find_commanders", typed);
    return answer.names.map((name) => ({ label: name, value: [...head, name].join(" + ") }));
  }, addAdjacent);
  autocomplete($("settings-ignore-input"), $("settings-ignore-results"), async (text) => {
    const needle = text.trim().toLowerCase();
    if (needle.length < 2) return [];
    const own = [...settingsState.themes.map((t) => ({ label: t.name, value: t.name, hint: t.kind })),
      ...settingsState.leanings.map((tag) => ({ label: tag, value: tag, hint: "leaning" }))]
      .filter((o) => o.label.toLowerCase().includes(needle));
    const answer = await api("find_tags", text);
    const seen = new Set(own.map((o) => o.label.toLowerCase()));
    return [...own, ...answer.tags.filter((t) => !seen.has(t.name)).map((t) => ({ label: t.name, value: t.name, hint: `Tagger tag, ${t.cards} cards` }))];
  }, addIgnore);
}

// Copies the saved deck as TopDeck.gg decklist text.
async function exportDeck() {
  try {
    const answer = await api("export", state.current);
    await copyText(answer.text);
    showStatus(`Copied ${answer.cards} cards for TopDeck.gg; queued changes are not included`);
  } catch { /* shown in the status line */ }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // The app's page may not be allowed the clipboard API; a selected text area copies everywhere.
    const area = el("textarea", { class: "sr-only", readonly: true });
    area.value = text;
    document.body.append(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) {
      showStatus("Could not copy the decklist", true);
      throw new Error("copy failed");
    }
  }
}

// The grid's card size, remembered per viewer.
function setupCardSize() {
  const apply = (size) => {
    document.documentElement.style.setProperty("--card-w", `${CARD_SIZES[size]}px`);
    for (const button of document.querySelectorAll(".grid-tools .size")) {
      button.setAttribute("aria-pressed", String(button.dataset.size === size));
    }
  };
  let size = "m";
  try { size = localStorage.getItem(SIZE_KEY) || "m"; } catch { /* storage is a convenience */ }
  apply(CARD_SIZES[size] ? size : "m");
  for (const button of document.querySelectorAll(".grid-tools .size")) {
    button.addEventListener("click", () => {
      apply(button.dataset.size);
      try { localStorage.setItem(SIZE_KEY, button.dataset.size); } catch { /* as above */ }
    });
  }
}

// Each panel remembers whether it was folded, per viewer.
function setupPanels() {
  for (const toggle of document.querySelectorAll(".panel-toggle")) {
    const key = `${PANEL_KEY}.${toggle.id}`;
    const show = (open) => {
      toggle.setAttribute("aria-expanded", String(open));
      $(toggle.getAttribute("aria-controls")).hidden = !open;
    };
    try { if (localStorage.getItem(key) === "0") show(false); } catch { /* storage is a convenience */ }
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      show(open);
      try { localStorage.setItem(key, open ? "1" : "0"); } catch { /* as above */ }
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
let searchSent = 0;
function setupPropose() {
  const input = $("propose-input");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchSent++;
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
  const mine = ++searchSent;
  try {
    const answer = await api("search", state.current, text);
    // A newer keystroke or a proposal supersedes this answer; a box that lost focus gets no list back.
    if (mine !== searchSent) return;
    if (document.activeElement !== $("propose-input")) { closeResults(); return; }
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
  searchSent++;
  closeResults();
  await whileBusy([$("propose-input")], async () => {
    try {
      const answer = await api("propose", state.current, name);
      $("propose-input").value = "";
      await refresh();
      const cards = state.view ? state.view.cards : [];
      if (uncut(cards).some((c) => c.id === answer.card.id)) {
        $("uncut").open = true;
        select(answer.card.id);
        showStatus(`${answer.card.name}: pick the deck card it replaces`);
        return;
      }
      const shown = considered(cards).some((c) => c.id === answer.card.id);
      showStatus(shown ? `${answer.card.name} is under consideration` : `${answer.card.name} is not under consideration`, !shown);
    } catch { /* shown in the status line */ }
  });
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
  const warnings = [...result.warnings, ...bracketWarnings(result.bracket)];
  $("review-warnings").replaceChildren(...warnings.map((text) => el("li", { text })));
  showReviewBracket(result.bracket);
}

// Spellbook's estimate for the deck once the changes are saved; absent when nothing counted changes.
function showReviewBracket(bracket) {
  const line = $("review-bracket");
  line.hidden = !bracket;
  if (!bracket) return;
  const now = bracket.before && bracket.before !== bracket.after ? `, now ${bracket.before}` : "";
  line.textContent = bracket.after
    ? `Bracket after saving: ${bracket.after} (${bracket.afterLabel})${now}`
    : "Bracket after saving: unknown, Commander Spellbook didn't answer";
  line.title = bracket.unavailable || "";
  line.classList.toggle("warn", bracket.check.notes.length > 0);
}

function bracketWarnings(bracket) {
  if (!bracket) return [];
  const causes = bracket.check.causes.map((c) => `${c.tag}: ${c.reason}: ${c.cards.join(" + ")}`);
  return [...bracket.check.notes, ...causes];
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
    const count = result.changes.length;
    const saved = `Saved ${count} change${count === 1 ? "" : "s"} to Archidekt`;
    if (result.mismatches.length) showStatus(`${saved}, but Archidekt differs: ${result.mismatches.join("; ")}`, true);
    else showStatus(`${saved}. Restore point in Activity.`, false, SAVED_MS);
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
  $("meta-body").replaceChildren(metaKeys(answer.keys), metaStoppers(answer.stoppers, answer.untouched), metaCombos(answer.combos, answer.racing));
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

// A Meta section folds on its own, and stays folded across decks and refreshes.
function metaSection(title, ...children) {
  const id = `meta-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  const key = `${PANEL_KEY}.${id}`;
  let open = true;
  try { open = localStorage.getItem(key) !== "0"; } catch { /* storage is a convenience */ }
  const section = el("details", { class: "meta-section", open },
    el("summary", {}, el("h3", { id, text: title })), ...children);
  section.addEventListener("toggle", () => {
    try { localStorage.setItem(key, section.open ? "1" : "0"); } catch { /* as above */ }
  });
  return section;
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

function metaCombos(combos, racing) {
  const rows = combos.map((c) => el("div", { class: "meta-row" },
    el("p", { class: "meta-card" },
      el("a", { href: c.url, target: "_blank", rel: "noopener", text: c.pieces.join(" + "), title: "Open on Commander Spellbook" })),
    c.results.length ? el("p", { class: "muted small", text: c.results.slice(0, 2).join("; ") }) : null,
    deckChips(c.decks),
    comboAnswers(c.answers, racing)));
  return metaSection("Meta combos",
    rows.length ? el("div", { class: "meta-rows" }, ...rows) : el("p", { class: "empty", text: "No combos in the meta." }));
}

function comboAnswers(answers, racing) {
  // A Turbo deck races the meta's combos rather than answering them (ADR 0022).
  if (!answers.length && racing) return el("p", { class: "answers" }, el("span", { class: "badge status racing", text: "Racing", title: "Turbo: racing, not answering" }));
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
      if (accountState.syncing) await loadAccount();
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
  setupCardSize();
  setupZoom();
  $("export").addEventListener("click", exportDeck);
  setupSettings();
  setupLabelMenu();
  setupActivity();
  setupBracket();
  setupAccount();
  $("status-close").addEventListener("click", hideStatus);
  try {
    const account = await loadAccount();
    if (account && !account.user) openSignIn();
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
