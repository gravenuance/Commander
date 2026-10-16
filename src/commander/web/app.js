"use strict";

// Every DOM node is built with textContent, never innerHTML: board text comes from people and Claude.
const POLL_MS = 2000;
const SEARCH_DELAY_MS = 150;
const STATUS_MS = 6000;
const STATUS_TEXT = { open: "", add: "Queued", rejected: "Turned down", applied: "In deck" };

const state = {
  decks: [],
  current: null,
  view: null,
  stamps: {},
  metaStamp: null,
  choices: new Map(),
  tiles: new Map(),
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

function manaCost(cost) {
  const box = el("span", { class: "cost", "aria-label": cost ? `cost ${cost}` : undefined });
  for (const symbol of (cost || "").matchAll(/\{([^}]+)\}/g)) {
    const text = symbol[1];
    const colour = /^[WUBRG]$/.test(text) ? text : "";
    box.append(el("span", { class: `pip ${colour}`, text, "aria-hidden": "true" }));
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
    state.choices.clear();
    state.tiles.clear();
    for (const name of ["suggested", "considered", "decided"]) $(`list-${name}`).replaceChildren();
    renderDecks();
    renderMeta(id, null);
    $("meta-empty").hidden = true;
    loadMeta();
  }
  await loadDeck();
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
  const pending = view.pending;
  const review = $("review");
  review.disabled = pending === 0;
  review.textContent = pending ? `Review ${pending} change${pending === 1 ? "" : "s"}` : "Review changes";

  const groups = { suggested: [], considered: [], decided: [] };
  for (const card of view.cards) {
    const settled = card.status === "rejected" || card.status === "applied";
    groups[settled ? "decided" : card.section].push(card);
  }
  const live = new Set(view.cards.map((c) => c.id));
  for (const id of [...state.tiles.keys()]) {
    if (!live.has(id)) {
      state.tiles.get(id).node.remove();
      state.tiles.delete(id);
      state.choices.delete(id);
    }
  }
  for (const [name, cards] of Object.entries(groups)) {
    const list = $(`list-${name}`);
    cards.forEach((card, index) => place(list, tileFor(card), index));
    $(`count-${name}`).textContent = cards.length ? `(${cards.length})` : "";
    const empty = $(`empty-${name}`);
    if (empty) empty.hidden = cards.length > 0;
  }
  renderActivity(view.activity);
}

function tileFor(card) {
  const key = JSON.stringify(card);
  const known = state.tiles.get(card.id);
  if (known && known.key === key) return known.node;
  const node = buildTile(card);
  if (known) known.node.replaceWith(node);
  state.tiles.set(card.id, { key, node });
  return node;
}

function buildTile(card) {
  const decided = card.status !== "open";
  const art = el("img", { class: "art", alt: "", loading: "lazy", src: card.image || undefined });
  if (!card.image) art.classList.add("missing");
  art.addEventListener("error", () => art.classList.add("missing"));

  const meta = el("div", { class: "card-meta" },
    el("span", { class: "type", text: card.typeLine, title: card.typeLine }),
    card.priceCents !== null ? el("span", { text: euro(card.priceCents) }) : null,
    el("span", { class: `badge ${card.source}`, text: card.source, title: `added by ${card.source}` }),
    card.gameChanger ? el("span", { class: "badge status gc", text: "Game changer" }) : null,
    STATUS_TEXT[card.status] ? el("span", { class: `badge status ${card.status}`, text: STATUS_TEXT[card.status] }) : null,
  );

  const body = el("div", { class: "card-body" },
    el("div", { class: "card-title" }, el("h3", { class: "card-name", text: card.name, title: card.name }), manaCost(card.manaCost)),
    meta,
    el("p", { class: "reason", text: card.reason }),
    card.text ? el("details", { class: "rules" }, el("summary", { text: "Rules text" }), el("p", { text: card.text })) : null,
  );

  if (card.status === "open" && card.replace.length) body.append(cutPicker(card));
  if (card.status === "add" && card.choice) body.append(el("p", { class: "note", text: `Cuts ${card.choice}` }));
  if (card.dropping) body.append(el("p", { class: "note", text: "Leaves the Maybeboard when you save" }));
  if (card.note) body.append(el("p", { class: "note", text: card.note }));
  body.append(actionsFor(card, decided));

  const tile = el("article", { class: `card ${card.status === "add" ? "queued" : card.status}`, "data-id": card.id, "aria-label": card.name }, art, body);
  return tile;
}

function cutPicker(card) {
  const name = `cut-${card.id}`;
  const box = el("fieldset", { class: "cuts" }, el("legend", { text: "Cut" }));
  const chosen = state.choices.get(card.id) ?? card.replace[0].name;
  state.choices.set(card.id, chosen);
  for (const option of card.replace) {
    const input = el("input", { type: "radio", name, value: option.name, checked: option.name === chosen });
    input.addEventListener("change", () => {
      state.choices.set(card.id, option.name);
      const add = box.parentElement.querySelector("button[data-action=add]");
      if (add) add.textContent = `Add, cut ${option.name}`;
    });
    box.append(el("label", { class: "cut" }, input,
      el("span", {}, document.createTextNode(option.name), option.reason ? el("span", { class: "why", text: option.reason }) : null)));
  }
  return box;
}

function actionsFor(card, decided) {
  const row = el("div", { class: "card-actions" });
  if (card.status === "applied") return row;
  if (!decided) {
    const cut = card.replace.length ? state.choices.get(card.id) ?? card.replace[0].name : null;
    row.append(
      el("button", { class: "button primary small", type: "button", "data-action": "add", text: cut ? `Add, cut ${cut}` : "Add card",
        onclick: () => decide(card, "add") }),
      el("button", { class: "button small", type: "button", text: "Turn down", onclick: () => decide(card, "reject") }),
    );
  } else {
    row.append(el("button", { class: "button small", type: "button", text: "Reopen", onclick: () => decide(card, "reopen") }));
  }
  row.append(el("span", { class: "spacer" }),
    el("button", { class: "button quiet icon", type: "button", text: "×", title: "Remove from board",
      "aria-label": `Remove ${card.name} from the board`, onclick: () => removeCard(card) }));
  return row;
}

async function decide(card, decision) {
  const choice = decision === "add" && card.replace.length ? state.choices.get(card.id) ?? card.replace[0].name : null;
  try {
    await api("decide", state.current, card.id, decision, choice, "");
    await refresh();
    const tile = document.querySelector(`.card[data-id="${card.id}"] button`);
    (tile || $("main")).focus();
  } catch { /* the status line already says what went wrong */ }
}

async function removeCard(card) {
  try {
    await api("remove", state.current, card.id);
    await refresh();
    $("main").focus();
  } catch { /* shown in the status line */ }
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
    showStatus(`${answer.card.name} is on the board`);
    await refresh();
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

/* Meta: tournament and Archidekt lists for the deck's commander */

const metaState = { key: null, deck: null, busy: false };
const pct = (share) => `${Math.round(share * 100)}%`;

function setupMeta() {
  $("meta").addEventListener("toggle", () => { if ($("meta").open) loadMeta(); });
  $("meta-refresh").addEventListener("click", refreshMeta);
}

async function loadMeta() {
  if (state.current === null || !$("meta").open || metaState.busy) return;
  const deck = state.current;
  try {
    const answer = await api("meta", deck);
    if (deck === state.current) renderMeta(deck, answer.report);
  } catch { /* shown in the status line */ }
}

async function refreshMeta() {
  const deck = state.current;
  metaState.busy = true;
  $("meta-busy").hidden = false;
  $("meta-refresh").disabled = true;
  try {
    const answer = await api("refresh_meta", deck);
    if (deck === state.current) renderMeta(deck, answer.report);
    if (!answer.report && answer.errors.length) showStatus(answer.errors[0], true);
  } catch { /* shown in the status line */ }
  metaState.busy = false;
  $("meta-busy").hidden = true;
  $("meta-refresh").disabled = false;
}

function renderMeta(deck, report) {
  const key = JSON.stringify(report);
  if (metaState.deck === deck && metaState.key === key) return;
  metaState.deck = deck;
  metaState.key = key;
  $("meta-empty").hidden = Boolean(report);
  $("meta-thin").hidden = !report || !report.sample.thin;
  $("meta-count").textContent = report ? `(${report.missing.length})` : "";
  if (!report) {
    $("meta-sample").textContent = "";
    $("meta-body").replaceChildren();
    return;
  }
  const s = report.sample;
  const fetched = report.sources.map((src) => src.fetchedAt).filter(Boolean).sort()[0];
  $("meta-sample").textContent = [
    s.known ? `${s.withLists} lists, ${s.topCuts} top cuts since ${report.since}` : "No EDHTop16 lists",
    fetched ? `data from ${new Date(fetched).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}` : "",
  ].filter(Boolean).join(" · ");
  const peers = report.peers.length;
  $("meta-body").replaceChildren(
    metaTable("Staples you don't play", ["Lists", "Archidekt", "Card", ""], report.missing.map((c) => [
      pct(c.share), `${c.peerLists}/${peers}`,
      el("span", {}, document.createTextNode(c.name), c.onSideboard ? el("span", { class: "badge status", text: "Maybeboard" }) : null),
      el("button", { class: "button small", type: "button", text: "Consider", "aria-label": `Consider ${c.name}`, onclick: () => propose(c.name) }),
    ])),
    metaTable("Spicy picks", ["Spice", "Lists", "Card", "Best finish"], report.spicy.map((p) => [
      String(p.spice), String(p.lists), p.name, `${p.best.standing} of ${p.best.size}, ${p.best.date}`,
    ])),
    metaTable("Your own picks", ["Lists", "Archidekt", "Card"], report.outliers.map((c) => [
      pct(c.share), `${c.peerLists}/${peers}`,
      el("span", {}, document.createTextNode(c.name), c.kept ? el("span", { class: "badge status", text: "Kept" }) : null),
    ])),
    metaThreats(report),
    metaCombos(report),
    metaTable("Top commanders", ["Entries", "Top cuts", "Conversion", "Commander"], report.field.map((f) => [
      String(f.entries), String(f.topCuts), pct(f.conversion), f.commander,
    ])),
    metaTable("Archidekt decks", ["Views", "Updated", "Deck"], report.peers.map((p) => [
      String(p.views), p.updatedAt.slice(0, 10),
      el("a", { href: p.url, target: "_blank", rel: "noopener", text: p.name, title: "Open on Archidekt" }),
    ])),
  );
}

function metaThreats(report) {
  const total = report.field.reduce((sum, f) => sum + f.metaShare, 0);
  const rows = report.threats.slice(0, 15).map((t) => [
    total ? pct(t.exposure / total) : "",
    t.name,
    t.kinds.join(", "),
    t.hits.length === report.keyPieces.length ? "every key piece" : t.hits.join(", "),
  ]);
  return metaTable("Meta answers to your key pieces", ["Share", "Card", "Kind", "Hits"], rows);
}

const ANSWER_ROLES = [["stax", "Stax"], ["removal", "Removal"], ["counter", "Counter"]];
const ANSWERS_SHOWN = 6;

function metaCombos(report) {
  const total = report.field.reduce((sum, f) => sum + f.metaShare, 0);
  const rows = report.combos.map((c) => [
    total ? pct(c.exposure / total) : "",
    el("div", {},
      el("a", { href: c.url, target: "_blank", rel: "noopener", text: c.pieces.join(" + "), title: "Open on Commander Spellbook" }),
      el("div", { class: "muted", text: c.results.slice(0, 2).join("; ") }),
      c.requirements.length ? el("div", { class: "muted", text: `Also needs: ${c.requirements.join("; ")}` }) : null),
    c.playedBy.slice(0, 3).map((p) => `${p.short} ${pct(p.share)}`).join(", "),
    comboAnswers(c.answers),
  ]);
  return metaTable("Meta combos", ["Share", "Combo", "Played by", "Your answers"], rows);
}

function comboAnswers(answers) {
  if (!answers.length) return el("span", { class: "badge status rejected", text: "No answer" });
  // Stax answers say how they stop the combo; removal and counters only need the card and any catch.
  const shown = (a) => (a.role === "stax" ? `${a.card} (${a.how})` : a.catch ? `${a.card} (${a.catch})` : a.card);
  const lines = ANSWER_ROLES.map(([role, label]) => {
    const names = answers.filter((a) => a.role === role).map(shown);
    if (!names.length) return null;
    const shown = names.length > ANSWERS_SHOWN ? `${names.slice(0, ANSWERS_SHOWN).join(", ")} and ${names.length - ANSWERS_SHOWN} more` : names.join(", ");
    return el("div", { title: names.join(", ") }, el("strong", { text: `${label}: ` }), document.createTextNode(shown));
  });
  return el("div", {}, ...lines);
}

function metaTable(title, headings, rows) {
  const id = `meta-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  const section = el("section", { class: "meta-section", "aria-labelledby": id }, el("h3", { id, text: title }));
  if (!rows.length) {
    section.append(el("p", { class: "empty", text: "None" }));
    return section;
  }
  const head = el("tr", {}, ...headings.map((h) => el("th", { scope: "col", text: h })));
  const body = rows.map((cells) => el("tr", {}, ...cells.map((cell) => el("td", {}, typeof cell === "string" ? document.createTextNode(cell) : cell))));
  section.append(el("table", { class: "meta-table" }, el("thead", {}, head), el("tbody", {}, ...body)));
  return section;
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
