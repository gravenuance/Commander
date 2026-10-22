"use strict";

/* Previews: upcoming cards that fit the deck, to look at only; once out they come through the ideas (ADR 0055) */

// Release days are calendar dates, so they are shown as written, never shifted by the viewer's time zone.
const OUT_DATE = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

const previewsPanel = panel((deck) => api("previews", deck), renderPreviews, {
  wanted: () => $("h-previews").getAttribute("aria-expanded") === "true",
});

// The panel shows only for a deck with fitting previews; an open one rereads them as the deck changes.
function showPreviews(view) {
  const count = view.previews;
  $("previews").hidden = !count;
  $("count-previews").textContent = count ? `(${count})` : "";
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
  const out = OUT_DATE.format(new Date(`${card.out}T00:00:00Z`));
  const body = el("div", { class: "card-body" },
    el("div", { class: "card-title" }, el("span", { class: "card-name", text: card.name }), manaCost(card.manaCost)),
    el("div", { class: "card-meta" },
      el("span", { class: "type", text: card.typeLine, title: card.typeLine }),
      el("span", { text: card.set }),
      el("span", { class: "badge status", text: `Out ${out}` })),
    el("p", { class: "fits small", text: `Fits: ${card.fits.map((tag) => `#${tag}`).join(" ")}` }),
    card.text ? el("details", { class: "fold" }, el("summary", { text: "Rules text" }), el("p", { text: card.text })) : null);
  return el("article", { class: "card preview", role: "listitem", "aria-label": card.name }, art, body);
}
