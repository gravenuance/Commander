"use strict";

// The deck list, the open deck's header, and what keeps the page in step with the deck.

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
      deck.error ? el("span", { class: "pill error", text: "!", title: deck.error }) : deck.queued ? el("span", { class: "pill", text: String(deck.queued), title: `${deck.queued} queued` }) : el("span"),
      el("span", { class: "full", text: deck.name, title: deck.name }),
    );
    button.setAttribute("aria-current", state.current === deck.id ? "true" : "false");
    button.setAttribute("aria-label", `${deck.short}, ${deck.name}${deck.error ? `, ${deck.error}` : deck.queued ? `, ${deck.queued} queued` : ""}`);
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

async function refresh() {
  await Promise.all([loadDecks(), loadDeck()]);
}
