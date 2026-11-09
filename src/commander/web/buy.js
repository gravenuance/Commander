"use strict";

/* Buy cards: the Don't Have cards by deck, and the Cardmarket scan that turns them into packages (ADR 0067) */

const BUY_POLL_MS = 1000;
const BUY_LANGUAGES = [["english", "English"], ["any", "Any"]];
const BUY_FOILS = [["any", "Any"], ["non-foil", "Non-foil"], ["foil", "Foil"]];
const BUY_CONDITIONS = [
  ["any", "Any"], ["MT", "Mint"], ["NM", "Near Mint"], ["EX", "Excellent"],
  ["GD", "Good"], ["LP", "Light Played"], ["PL", "Played"], ["PO", "Poor"],
];
const BUY_TREND_TIP = "Cardmarket's trend price of the card's cheapest printing";
const BUY_CURVE_KEYS = ["ArrowDown", "ArrowUp", "Home", "End"];

// What the screen keeps between visits: the last answer, the elements built from it and the keys that say
// when an answer is the one already drawn, so coming back redraws nothing that has not changed.
const buyState = {
  open: false,
  view: null,
  deckItems: new Map(),
  resultsKey: null,
  packages: new Map(),
  picked: null,
  packageRequest: 0,
  // True from a click on Scan until the bridge answers.
  requested: false,
  polling: false,
};

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

function setupBuy() {
  $("buy-open").addEventListener("click", openBuy);
  $("buy-scan").addEventListener("click", startBuyScan);
  $("buy-cancel").addEventListener("click", cancelBuyScan);
  setupBuyChoice($("buy-language"), BUY_LANGUAGES, "language");
  setupBuyChoice($("buy-foil"), BUY_FOILS, "foil");
  const condition = $("buy-condition");
  condition.replaceChildren(...BUY_CONDITIONS.map(([value, label]) => el("option", { value, text: label })));
  condition.addEventListener("change", () => changeBuyFilters({ condition: condition.value }));
  $("buy-base-sellers").addEventListener("change", changeBuyBaseSellers);
  $("buy-curve").addEventListener("keydown", moveBetweenBuyRows);
}

// A row of buttons of which one is pressed, as the card size choice is.
function setupBuyChoice(group, options, key) {
  group.replaceChildren(...options.map(([value, label]) => el("button", {
    type: "button", "data-value": value, "aria-pressed": "false", text: label,
    onclick: () => changeBuyFilters({ [key]: value }),
  })));
}

/* Opening and closing */

async function openBuy() {
  if (state.current !== null) closeDeckView();
  else $("deck").hidden = true;
  buyState.open = true;
  $("buy").hidden = false;
  $("buy-open").setAttribute("aria-current", "page");
  renderDecks();
  $("buy-title").focus();
  try {
    await refreshBuy();
  } catch (error) { noteFailure(error); }
}

// The screen stays built, with its last results, for the next visit.
function closeBuy() {
  buyState.open = false;
  $("buy").hidden = true;
  $("buy-open").removeAttribute("aria-current");
}

// Drops everything built for an account that is gone.
function buyForget() {
  closeBuy();
  buyState.view = null;
  buyState.deckItems.clear();
  buyState.resultsKey = null;
  buyState.packages.clear();
  buyState.picked = null;
  buyState.packageRequest++;
  $("buy-decks").replaceChildren();
  $("buy-curve").replaceChildren();
  $("buy-notes").replaceChildren();
  $("buy-sellers").replaceChildren();
  $("buy-package-notes").replaceChildren();
  $("buy-results").hidden = true;
  $("buy-package").hidden = true;
}

async function refreshBuy() {
  const epoch = accountState.epoch;
  const view = await api("buy");
  if (pageUserGone(epoch) || !buyState.open) return;
  acceptBuyView(view);
}

function acceptBuyView(view) {
  buyState.view = view;
  paintBuy();
  watchBuy();
}

/* Painting */

function paintBuy() {
  const view = buyState.view;
  if (!view) return;
  const included = view.decks.filter((deck) => deck.included);
  renderBuyDecks(view.decks, view.status.running);
  $("buy-summary").textContent = view.copies
    ? `${plural(view.copies, "card", "cards")} from ${plural(included.length, "deck", "decks")}`
    : "";
  $("buy-cards-block").hidden = view.decks.length === 0;
  $("buy-empty").hidden = view.copies > 0;
  $("buy-empty-text").textContent = view.decks.length ? "Tick a deck to scan." : "No Don't Have cards to buy.";
  paintBuyStatus(view.status);
}

function renderBuyDecks(decks, running) {
  const list = $("buy-decks");
  const seen = new Set(decks.map((deck) => deck.id));
  for (const [id, entry] of buyState.deckItems) {
    if (seen.has(id)) continue;
    entry.item.remove();
    buyState.deckItems.delete(id);
  }
  for (const [index, deck] of decks.entries()) {
    let entry = buyState.deckItems.get(deck.id);
    if (!entry) {
      entry = buildBuyDeck(deck);
      buyState.deckItems.set(deck.id, entry);
    }
    paintBuyDeck(entry, deck, running);
    place(list, entry.item, index);
  }
}

function buildBuyDeck(deck) {
  const box = el("input", { type: "checkbox", id: `buy-tick-${deck.id}` });
  box.addEventListener("change", () => setBuyDeck(deck.id, box.checked));
  const name = el("span", { class: "buy-deck-name" });
  const count = el("span", { class: "count" });
  const list = el("ul", { class: "buy-cards", id: `buy-cards-${deck.id}`, hidden: true });
  const fold = el("button", {
    class: "disclosure buy-fold", type: "button", "aria-expanded": "false", "aria-controls": list.id,
  }, el("span", { class: "end" }, count, knob()));
  fold.addEventListener("click", () => {
    const open = fold.getAttribute("aria-expanded") !== "true";
    fold.setAttribute("aria-expanded", String(open));
    list.hidden = !open;
  });
  const tick = el("label", { class: "buy-tick", for: box.id }, box, name);
  const item = el("li", { class: "buy-deck", "data-id": String(deck.id) },
    el("div", { class: "buy-deck-head" }, tick, fold), list);
  return { item, box, name, count, fold, list, known: deck.known, key: null };
}

function paintBuyDeck(entry, deck, running) {
  entry.known = deck.known;
  entry.box.checked = deck.included;
  entry.box.disabled = running || !deck.known;
  entry.box.setAttribute("aria-label", `Include ${deck.short}`);
  entry.name.textContent = deck.short;
  entry.name.title = deck.name;
  entry.count.textContent = deck.known ? plural(deck.copies, "card", "cards") : "Labels unknown; sync";
  entry.fold.disabled = !deck.known;
  entry.fold.setAttribute("aria-label", `${deck.short} cards to buy`);
  const key = JSON.stringify(deck.cards);
  if (entry.key === key) return;
  entry.key = key;
  entry.list.replaceChildren(...deck.cards.map((card) => el("li", { text: times(card.quantity, card.name) })));
}

function paintBuyFilters() {
  const view = buyState.view;
  const running = view.status.running;
  for (const [id, key] of [["buy-language", "language"], ["buy-foil", "foil"]]) {
    for (const button of $(id).children) {
      button.setAttribute("aria-pressed", String(button.dataset.value === view.filters[key]));
      button.disabled = running;
    }
  }
  const condition = $("buy-condition");
  condition.value = view.filters.condition;
  condition.disabled = running;
  const base = $("buy-base-sellers");
  base.value = String(view.baseSellers);
  base.disabled = running;
}

function paintBuyStatus(status) {
  const view = buyState.view;
  view.status = status;
  const running = status.running;
  paintBuyFilters();
  const scan = $("buy-scan");
  scan.disabled = running || buyState.requested || view.copies === 0;
  scan.title = view.copies === 0 ? "Tick a deck to scan" : "";
  if (running) scan.setAttribute("aria-busy", "true");
  else scan.removeAttribute("aria-busy");
  scan.querySelector(".spinner").hidden = !running;
  $("buy-scan-label").textContent = status.phase === "idle" ? "Scan Cardmarket" : "Scan Cardmarket again";
  const cancel = $("buy-cancel");
  cancel.hidden = !running;
  cancel.disabled = status.cancelling;
  cancel.textContent = status.cancelling ? "Cancelling…" : "Cancel scan";

  const stopped = status.phase === "stopped";
  const error = $("buy-error");
  error.hidden = !stopped;
  error.textContent = stopped ? status.message : "";
  error.title = stopped ? status.detail : "";
  $("buy-progress").textContent = progressLine(status);
  for (const entry of buyState.deckItems.values()) entry.box.disabled = running || !entry.known;
  renderBuyResults(status);
}

function progressLine(status) {
  if (status.running) return status.cancelling ? "Stopping after this page…" : status.progress;
  if (status.phase === "cancelled") return status.message;
  if (status.phase === "results") {
    const rows = status.results.rows.length;
    return rows ? `Found ${plural(rows, "package", "packages")}.` : "Nothing to buy: no offers found.";
  }
  return "";
}

/* Scanning */

async function startBuyScan() {
  if (buyState.requested) return;
  buyState.requested = true;
  try {
    const status = await api("buy_scan");
    paintBuyStatus(status);
    watchBuy();
  } catch (error) { noteFailure(error); } finally {
    buyState.requested = false;
    if (buyState.view) paintBuyStatus(buyState.view.status);
  }
}

async function cancelBuyScan() {
  try {
    paintBuyStatus(await api("buy_cancel"));
  } catch (error) { noteFailure(error); }
}

// While a scan runs the screen asks how it is going once a second; the timer ends with the scan.
function watchBuy() {
  const view = buyState.view;
  if (!view || !view.status.running || buyState.polling) return;
  buyState.polling = true;
  setTimeout(pollBuy, BUY_POLL_MS);
}

async function pollBuy() {
  buyState.polling = false;
  if (!buyState.open || !buyState.view) return;
  try {
    paintBuyStatus(await api("buy_status"));
  } catch (error) {
    noteFailure(error);
    return;
  }
  watchBuy();
}

/* Choices */

async function setBuyDeck(id, included) {
  try {
    acceptBuyView(await api("buy_include", id, included));
  } catch (error) {
    noteFailure(error);
    paintBuy();
  }
}

async function changeBuyFilters(change) {
  const filters = { ...buyState.view.filters, ...change };
  try {
    acceptBuyView(await api("buy_filters", filters.language, filters.condition, filters.foil));
  } catch (error) {
    noteFailure(error);
    paintBuyFilters();
  }
}

async function changeBuyBaseSellers() {
  try {
    acceptBuyView(await api("buy_base_sellers", Number($("buy-base-sellers").value)));
  } catch (error) {
    noteFailure(error);
    paintBuyFilters();
  }
}

/* The packages */

function renderBuyResults(status) {
  const results = status.results;
  const key = results ? JSON.stringify([results, status.phase]) : "";
  if (key === buyState.resultsKey) return;
  buyState.resultsKey = key;
  buyState.packages.clear();
  $("buy-results").hidden = !results;
  if (!results) {
    $("buy-curve").replaceChildren();
    $("buy-notes").replaceChildren();
    clearBuyPackage();
    return;
  }
  $("buy-partial").hidden = status.phase === "results";
  $("buy-partial").title = status.message;
  $("buy-curve").replaceChildren(...results.rows.map(buildBuyRow));
  $("buy-notes").replaceChildren(...buyResultNotes(results));
  if (results.rows.some((row) => row.sellers === buyState.picked)) markBuyRows();
  else clearBuyPackage();
  if (!results.rows.length) $("buy-curve").append(el("p", { class: "empty", text: "No package covers these cards." }));
}

function buildBuyRow(row) {
  const saving = row.savingCents > 0 ? `Saves ${euro(row.savingCents)}` : "";
  return el("button", {
    class: "curve-row", type: "button", "aria-pressed": "false", "data-sellers": String(row.sellers),
    onclick: () => pickBuyRow(row.sellers),
  },
  el("span", { class: "curve-sellers", text: plural(row.sellers, "seller", "sellers") }),
  el("span", { class: "num", text: euro(row.totalCents) }),
  el("span", { class: "num", text: String(row.cards) }),
  el("span", { class: "num", text: euro(row.shippingCents) }),
  el("span", { class: "num", text: saving }),
  el("span", { class: "curve-badge" }, row.cheapest ? el("span", { class: "badge status", text: "Cheapest" }) : null,
    row.optimal ? null : el("span", { class: "badge status gc", title: "The solver ran out of time", text: "Estimate" })));
}

// What the scan could not place, each in plain words.
function buyResultNotes(results) {
  const notes = [
    ["Not found on Cardmarket:", results.notFound],
    ["No offers for these filters:", results.noOffers],
    ["Left out of Cardmarket's wants list:", results.unmatched],
    ["Another quantity on Cardmarket's wants list:",
      results.quantityMismatch.map((m) => `${m.card} (${m.wanted} wanted, ${m.got} listed)`)],
  ];
  return notes.filter(([, names]) => names.length)
    .map(([label, names]) => el("li", {}, el("strong", { text: `${label} ` }), names.join(", ")));
}

function markBuyRows() {
  for (const row of $("buy-curve").children) {
    row.setAttribute("aria-pressed", String(row.dataset.sellers === String(buyState.picked)));
  }
}

async function pickBuyRow(sellers) {
  buyState.picked = sellers;
  markBuyRows();
  const sent = ++buyState.packageRequest;
  try {
    let detail = buyState.packages.get(sellers);
    if (!detail) {
      detail = await api("buy_package", sellers);
      buyState.packages.set(sellers, detail);
    }
    if (sent === buyState.packageRequest) renderBuyPackage(detail);
  } catch (error) { noteFailure(error); }
}

function clearBuyPackage() {
  buyState.picked = null;
  buyState.packageRequest++;
  markBuyRows();
  $("buy-package").hidden = true;
  $("buy-sellers").replaceChildren();
  $("buy-package-notes").replaceChildren();
}

function renderBuyPackage(detail) {
  $("buy-package").hidden = false;
  $("buy-package-title").textContent = `${plural(detail.sellers, "seller", "sellers")} · ${euro(detail.totalCents)}`;
  $("buy-package-sub").textContent = `Cards ${euro(detail.cardsCents)} · shipping ${euro(detail.shippingCents)}`;
  $("buy-sellers").replaceChildren(...detail.shipments.map(buildBuySeller));
  const notes = [];
  if (detail.missing.length) {
    notes.push(el("li", {}, el("strong", { text: "No usable offer: " }), detail.missing.join(", ")));
  }
  if (detail.short.length) {
    const names = detail.short.map((s) => `${s.card} (${s.wanted} wanted, ${s.available} offered)`);
    notes.push(el("li", {}, el("strong", { text: "Fewer copies offered than wanted: " }), names.join(", ")));
  }
  $("buy-package-notes").replaceChildren(...notes);
}

function buildBuySeller(shipment) {
  const how = `${shipment.method} ${euro(shipment.shippingCents)}, ${shipment.tracked ? "tracked" : "not tracked"}`;
  const head = el("tr", {}, ...["Card", "Set", "Condition", "Language", "Foil", "Price", "Trend"].map((label, index) =>
    el("th", { scope: "col", class: index >= 5 ? "num" : "", text: label, title: label === "Trend" ? BUY_TREND_TIP : undefined })));
  const rows = shipment.picks.map((pick) => el("tr", {},
    el("td", { text: times(pick.copies, pick.card) }),
    el("td", { text: pick.expansion }),
    el("td", { text: pick.condition }),
    el("td", { text: pick.language }),
    el("td", { text: pick.foil ? "Foil" : "" }),
    el("td", { class: "num", text: euro(pick.priceCents) }),
    el("td", { class: "num", text: pick.trendCents === null ? "–" : euro(pick.trendCents) })));
  return el("section", { class: "buy-seller", "aria-label": `Seller ${shipment.seller}` },
    el("h3", { class: "buy-seller-name", text: shipment.seller }),
    el("p", { class: "muted small", text: `${shipment.country} · ${how}` }),
    el("div", { class: "buy-table" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows))));
}

// Up and down move between the packages; Enter and Space press the one in focus, as for any button.
function moveBetweenBuyRows(event) {
  if (!BUY_CURVE_KEYS.includes(event.key)) return;
  const rows = [...$("buy-curve").children].filter((node) => node.dataset.sellers !== undefined);
  const at = rows.indexOf(document.activeElement);
  if (!rows.length) return;
  event.preventDefault();
  const last = rows.length - 1;
  const next = { ArrowDown: Math.min(at + 1, last), ArrowUp: Math.max(at - 1, 0), Home: 0, End: last }[event.key];
  rows[next].focus();
}
