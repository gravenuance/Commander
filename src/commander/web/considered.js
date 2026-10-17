"use strict";

// The cards under consideration, picking the deck card each replaces, and proposing a card.

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
