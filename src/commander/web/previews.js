"use strict";

/* Previews: upcoming cards that fit the deck; one can be moved to Considering and queued there (ADR 0055) */

const previewsPanel = panel((deck) => api("previews", deck), renderPreviews, {
  wanted: () => $("h-previews").getAttribute("aria-expanded") === "true",
});

// The panel shows only for a deck with fitting previews; an open one rereads them as the deck changes.
function showPreviews(view) {
  const count = view.previews;
  $("previews").hidden = !count;
  $("count-previews").textContent = count ? String(count) : "";
  if (count) previewsPanel.load();
}

function resetPreviews() {
  $("list-previews").replaceChildren();
  previewsPanel.forget();
}

function renderPreviews(deck, answer) {
  $("list-previews").replaceChildren(...answer.cards.map(previewTile));
}

function previewTile(card) {
  const art = el("button", {
    class: "preview-art", type: "button", "aria-label": `Zoom ${card.name}`, title: card.name,
    onclick: () => zoom(card),
  }, cardImage(card.image, card.name, "art"));
  const move = el("button", {
    class: "button small move", type: "button", text: "Move to Considering",
    "aria-label": `Move ${card.name} to Considering`, title: "Queue it for the deck like any suggestion",
  });
  move.addEventListener("click", () => moveToConsidering(card, move));
  const body = el("div", { class: "card-body" },
    el("div", { class: "card-title" }, el("span", { class: "card-name", text: card.name }), manaCost(card.manaCost)),
    el("div", { class: "card-meta" },
      el("span", { class: "type", text: card.typeLine, title: card.typeLine }),
      el("span", { text: card.set }),
      el("span", { class: "badge status", text: outLabel(card.out) }),
      ...card.against.map((name) => el("span", { class: "badge status against", text: `Works against ${name}` }))),
    el("p", { class: "fits small", text: `Fits: ${card.fits.map((tag) => `#${tag}`).join(" ")}` }),
    card.text ? el("details", { class: "fold" }, el("summary", { text: "Rules text" }), el("p", { text: card.text })) : null,
    el("div", { class: "preview-actions" }, move));
  return el("article", { class: "card preview", role: "listitem", "aria-label": card.name }, art, body);
}

// The card goes onto the board, so it leaves this panel and the next fit takes its place; focus stays in the
// list, on the tile now at the card's place or the one before it.
async function moveToConsidering(card, button) {
  const index = [...$("list-previews").children].findIndex((tile) => tile.contains(button));
  await whileBusy([button], async () => {
    try {
      await api("move_preview", state.current, card.name);
      showStatus(`${card.name} moved to Considering`);
      await refresh();
      await previewsPanel.load();
      const tiles = [...$("list-previews").children];
      const next = tiles[Math.min(index, tiles.length - 1)]?.querySelector(".move");
      (next ?? $("h-previews")).focus();
    } catch (error) { noteFailure(error); }
  });
}
