"use strict";

// The cards under consideration, picking the deck card each replaces, and proposing a card.

/* Considering: every board card with somewhere to go, plus those already queued */

// An open card the main deck holds is no idea (ADR 0046); a queued one stays for Review to mark applied.
function isOpenIdea(card) {
  return card.status === "open" && !card.held;
}

function considering(cards) {
  return sortSuggestions(cards.filter((c) => c.status === "add" || (isOpenIdea(c) && c.targets.length)));
}

// Your cards, then Claude's, then meta ideas, then the deck's own commander's Archidekt decks, then alike
// commanders', then tagged decks', then everything else. Within each group, cards over the deck's budget
// (ADR 0025) go last; then the largest share the pill shows first.
const ORIGIN_RANK = ["meta", "archidekt decks", "adjacent decks", "tagged decks"];

function suggestionGroup(card) {
  if (card.source === "you") return 0;
  if (card.source === "claude") return 1;
  const rank = ORIGIN_RANK.indexOf(card.origin);
  return rank < 0 ? 2 + ORIGIN_RANK.length : 2 + rank;
}

// Where an idea came from, as the badge says it and as its tooltip spells it out.
function originLabel(card) {
  switch (card.origin) {
    case "meta": return "Tournament lists";
    case "archidekt decks": return `Archidekt: ${state.view?.short ?? "this commander"} decks`;
    case "adjacent decks": return "Archidekt: alike commanders";
    case "tagged decks": return card.originTag ? `Archidekt: tagged ${card.originTag}` : "Archidekt: tagged decks";
    default: return card.origin ?? card.source;
  }
}

function originTitle(card) {
  const from = {
    "meta": "EDHTop16 tournament lists",
    "archidekt decks": "recent Archidekt decks of this commander",
    "adjacent decks": "recent Archidekt decks of commanders that play alike",
    "tagged decks": card.originTag ? `recent Archidekt decks tagged ${card.originTag}` : "recent Archidekt decks with a theme tag of the deck",
  }[card.origin];
  return from ? `compiled by ${card.source} from ${from}` : `added by ${card.source}`;
}

// Without a share (no list was read) the badge still says where the idea came from.
function sourceBadge(card) {
  return el("span", { class: `badge ${card.source}`, text: originLabel(card), title: originTitle(card) });
}

// The colour key of each origin, as app.css names its --origin-* tokens; ideas of no origin are neutral.
const ORIGIN_KEY = { "meta": "meta", "archidekt decks": "decks", "adjacent decks": "adjacent", "tagged decks": "tagged" };

function originKey(origin) {
  return ORIGIN_KEY[origin] ?? "none";
}

// The primary source in words, for the pill's accessible name.
function originPhrase(card) {
  switch (card.origin) {
    case "meta": return "mostly tournament lists";
    case "archidekt decks": return `mostly ${state.view?.short ?? "this commander"} Archidekt decks`;
    case "adjacent decks": return "mostly alike commanders' decks";
    case "tagged decks": return card.originTag ? `mostly decks tagged ${card.originTag}` : "mostly tagged decks";
    default: return `added by ${card.source}`;
  }
}

function sortSuggestions(cards) {
  return [...cards].sort((a, b) =>
    suggestionGroup(a) - suggestionGroup(b)
    || (a.withinBudget === false) - (b.withinBudget === false)
    || pillShare(b).value - pillShare(a).value);
}

// Ideas with no deck card named to replace yet; selecting one offers every deck card as its cut.
function uncut(cards) {
  return sortSuggestions(cards.filter((c) => isOpenIdea(c) && !c.targets.length));
}

function renderConsidering(cards) {
  const shown = considering(cards);
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
  const list = $("list-considering");
  shown.forEach((card, index) => place(list, tileFor(card), index));
  const waitingList = $("list-uncut");
  waiting.forEach((card, index) => place(waitingList, tileFor(card), index));
  $("count-considering").textContent = shown.length ? `(${shown.length})` : "";
  $("uncut").hidden = !waiting.length;
  $("count-uncut").textContent = waiting.length ? `(${waiting.length})` : "";
  $("empty-considering").hidden = shown.length + waiting.length > 0;
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
  const key = JSON.stringify([card, state.view?.grace]);
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
    card.share || card.pool ? sharePill(card) : sourceBadge(card),
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
  const doing = card.choice ? `Replacing ${times(card.copies, card.choice)}` : `Adding to ${card.category ?? card.typeCategory ?? "the deck"}`;
  const body = el("div", { class: "card-body" },
    el("div", { class: "card-title" }, pick, manaCost(card.manaCost)),
    meta,
    queued ? el("p", { class: "replacing" },
      el("span", { text: doing }),
      el("button", { class: "button quiet small", type: "button", text: "Undo", "aria-label": `Undo ${card.name}: ${doing}`,
        onclick: () => decide(card, "reopen") })) : null,
    targets,
    plainAdd(card),
    card.text ? el("details", { class: "fold" }, el("summary", { text: "Rules text" }), el("p", { text: card.text })) : null,
    reasoning ? el("details", { class: "fold" }, el("summary", { text: "Reasoning" }), el("p", { text: reasoning })) : null);

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
    if (!event.target.closest("button, a, summary, details, form, .share-pop")) select(card.id);
  });
  return tile;
}

/* The share pill and its breakdown */

// The pool's decks an alike or tagged idea was found in, in words.
function poolName(card) {
  return card.origin === "tagged decks" && card.originTag ? `${card.originTag}-tagged decks` : "alike commanders' decks";
}

// What the pill shows and orders by: an alike or tagged idea's share of its pool (when the board has the
// numbers), any other idea's combined share.
function pillShare(card) {
  if ((card.origin === "adjacent decks" || card.origin === "tagged decks") && card.pool) {
    const label = pct(card.pool.share);
    return { value: card.pool.share, label, name: `${label} of ${poolName(card)}` };
  }
  if (!card.share) return { value: -1, label: "", name: originPhrase(card) };
  return { value: card.share.share, label: card.share.label, name: `${card.share.label} played; ${originPhrase(card)}` };
}

// The share the idea is shown by, coloured by the origin it is credited to; it opens the breakdown beside it.
function sharePill(card) {
  const popId = `share-pop-${card.id}`;
  const shown = pillShare(card);
  const pill = el("button", {
    class: `badge share-pill origin-${originKey(card.origin)}`, type: "button", text: shown.label,
    "aria-expanded": "false", "aria-controls": popId,
    "aria-label": shown.name,
    title: `${shown.name}: show breakdown`,
  });
  const pop = sharePopover(card, popId);
  pill.addEventListener("click", () => toggleShare(pill, pop));
  return el("span", { class: "share-wrap" }, pill, pop);
}

function sharePopover(card, popId) {
  const rows = shareRows(card);
  const grace = card.share ? graceLine(state.view?.grace) : "";
  return el("div", { class: "share-pop", id: popId, role: "group", "aria-label": `${card.name}: share breakdown`, hidden: true },
    card.share
      ? el("p", { class: "share-total", text: `Combined ${card.share.label} · ${card.share.count} of ${card.share.total}` })
      : null,
    grace ? el("p", { class: "muted share-grace", text: grace }) : null,
    card.origin ? null : el("p", { class: "muted", text: `Added by ${card.source}` }),
    rows.length ? el("ul", { class: "share-rows" }, ...rows.map((row) => shareRow(card, row))) : null);
}

// One row per source that plays the card, in the order the tile groups sources; the primary one always shows.
// A pooled idea has no combined share when no list was read for the commander.
function shareRows(card) {
  const { meta, archidekt } = card.share ?? {};
  const rows = [];
  if (meta && (meta.count || card.origin === "meta")) rows.push({ origin: "meta", part: meta, unit: "lists" });
  if (archidekt && (archidekt.count || card.origin === "archidekt decks")) {
    rows.push({ origin: "archidekt decks", part: archidekt, unit: "decks" });
  }
  // An alike or tagged idea was found in a pool of its own; the bridge sends the pool's numbers.
  if (card.origin === "adjacent decks" || card.origin === "tagged decks") {
    rows.push({ origin: card.origin, part: card.pool, unit: "decks" });
  }
  return rows;
}

function shareRow(card, row) {
  // A pool idea from before its numbers were stored gets them on the next sync.
  const numbers = row.part
    ? `${pct(row.part.share)} · ${row.part.count} of ${row.part.total} ${row.unit}`
    : "Counted on the next sync";
  return el("li", { class: "share-row" },
    el("span", { class: `share-swatch origin-${originKey(row.origin)}`, "aria-hidden": "true" }),
    el("span", { class: "share-source", text: originLabel({ origin: row.origin, originTag: card.originTag }) }),
    el("span", { class: "share-numbers", text: numbers }));
}

// The pill and breakdown showing now; one at a time.
let openShare = null;

function toggleShare(pill, pop) {
  const again = openShare !== null && openShare.pill === pill;
  closeShare(false);
  if (again) return;
  pop.style.top = `${pill.offsetTop + pill.offsetHeight + 4}px`;
  pop.hidden = false;
  pill.setAttribute("aria-expanded", "true");
  openShare = { pill, pop };
}

function closeShare(refocus) {
  if (openShare === null) return;
  const { pill, pop } = openShare;
  openShare = null;
  pill.setAttribute("aria-expanded", "false");
  pop.hidden = true;
  if (refocus) pill.focus();
}

function setupSharePopover() {
  // Captured, so Escape closes the breakdown before deck.js lets go of the selected tile.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || openShare === null || !openShare.pill.isConnected) return;
    event.stopPropagation();
    closeShare(true);
  }, true);
  document.addEventListener("click", (event) => {
    if (openShare !== null && !openShare.pill.contains(event.target) && !openShare.pop.contains(event.target)) closeShare(false);
  });
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

// Lights the grid for the considering card in focus: targets tinted, cards already queued out marked.
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
    node.querySelector(".add-plain").hidden = !on || (card.status === "add" && !card.choice);
  }
  $("grid").classList.toggle("choosing", Boolean(active));
}

// Adding with no cut, filed under one of the deck's categories, its card type's first.
function plainAdd(card) {
  const preset = card.category ?? card.typeCategory ?? "";
  const categories = [...new Set([...(state.view?.categories ?? []), preset].filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));
  const select = el("select", { class: "input small", "aria-label": `Category for ${card.name}`, title: "Category it is filed under" },
    ...categories.map((name) => el("option", { value: name, text: name })));
  select.value = preset;
  const add = el("button", { class: "button small", type: "button", text: "Add without a cut",
    onclick: () => addPlain(card, select.value) });
  return el("div", { class: "add-plain", role: "group", "aria-label": `Add ${card.name} without a cut`, hidden: true }, select, add);
}

async function addPlain(card, category) {
  const tile = state.tiles.get(card.id)?.node;
  const controls = tile ? [...tile.querySelectorAll(".add-plain select, .add-plain button")] : [];
  await whileBusy(controls, async () => {
    try {
      await api("decide", state.current, card.id, "add", null, "", category);
      state.selected = null;
      showStatus(`${card.name} queued to add to ${category}`);
      await refresh();
    } catch { /* shown in the status line */ }
  });
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

/* Proposing a card */

function setupPropose() {
  combobox($("propose-input"), $("propose-results"), findCards, propose);
}

async function findCards(text) {
  const typed = text.trim();
  if (typed.length < 2 || state.current === null) return [];
  const answer = await api("search", state.current, typed);
  return answer.cards.map((card) => ({ label: card.name, value: card.name, hint: card.typeLine }));
}

async function propose(name) {
  await whileBusy([$("propose-input")], async () => {
    try {
      const answer = await api("propose", state.current, name);
      $("propose-input").value = "";
      await refresh();
      const cards = state.view ? state.view.cards : [];
      if (uncut(cards).some((c) => c.id === answer.card.id)) {
        // The panel may be folded away, and its tile is only on show while it is open.
        showPanel($("h-uncut"), true);
        select(answer.card.id);
        showStatus(`${answer.card.name}: pick the deck card it replaces, or add it without one`);
        return;
      }
      const shown = considering(cards).some((c) => c.id === answer.card.id);
      // Selected, its cuts light up and its add without a cut is on offer.
      if (shown) {
        showPanel($("h-considering"), true);
        select(answer.card.id);
      }
      showStatus(shown ? `${answer.card.name} is under consideration` : `${answer.card.name} is not under consideration`, !shown);
    } catch { /* shown in the status line */ }
  });
}
