"use strict";

/* Buy cards: the Don't Have cards by deck, and the Cardmarket scan that turns them into packages (ADR 0067) */

const BUY_POLL_MS = 1000;
const BUY_LANGUAGES = [["english", "English"], ["japanese", "Japanese"], ["both", "Both"], ["any", "Any"]];
const BUY_FOILS = [["any", "Any"], ["non-foil", "Non-foil"], ["foil", "Foil"]];
// The longest a seller may take to deliver, in days.
const BUY_DELIVERIES = [["any", "Any"], ["5", "5 days"], ["7", "1 week"], ["10", "10 days"], ["14", "2 weeks"]];
// The country names the summary spells out; more than this it counts them.
const BUY_COUNTRIES_NAMED = 2;
const BUY_MENU_KEYS = ["ArrowDown", "ArrowUp", "Home", "End", "Escape", "Tab"];
const BUY_CONDITIONS = [
  ["any", "Any"], ["MT", "Mint"], ["NM", "Near Mint"], ["EX", "Excellent"],
  ["GD", "Good"], ["LP", "Light Played"], ["PL", "Played"], ["PO", "Poor"],
];
const BUY_TREND_TIP = "Cardmarket's trend price of the card's cheapest printing";
const BUY_CURVE_KEYS = ["ArrowDown", "ArrowUp", "Home", "End"];
// The phases in which the Cardmarket window is the owner's, shown over the pane.
const BUY_PANE_PHASES = ["signin", "check"];

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
  // The detail of the package shown, which "Add to cart" confirms.
  detail: null,
  // True from a click on Scan or on the cart dialog's confirm until the bridge answers.
  requested: false,
  // True from the confirm until the run it started has ended, so the screen says "adding" through a handover.
  adding: false,
  // The cart report as drawn, as text, so an unchanged one is not drawn again.
  cartKey: "",
  // The price text on its way to the bridge, so Enter followed by the field's change sends it once.
  pricePending: null,
  polling: false,
  // Cardmarket's countries: null until fetched, then their names by id and the checkbox built for each.
  countries: null,
  countryNames: new Map(),
  countryBoxes: new Map(),
  loadingCountries: null,
  // What the bridge was last told about the pane, as text ("null" for no pane), and whether a report is out.
  paneSent: "null",
  paneSending: false,
};

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

function setupBuy() {
  $("buy-open").addEventListener("click", openBuy);
  $("buy-scan").addEventListener("click", startBuyScan);
  $("buy-cancel").addEventListener("click", cancelBuyScan);
  setupBuyCart();
  setupBuyChoice($("buy-language"), BUY_LANGUAGES, "language");
  setupBuyChoice($("buy-foil"), BUY_FOILS, "foil");
  setupBuyChoice($("buy-delivery"), BUY_DELIVERIES, "delivery");
  setupBuyCountries();
  const condition = $("buy-condition");
  condition.replaceChildren(...BUY_CONDITIONS.map(([value, label]) => el("option", { value, text: label })));
  condition.addEventListener("change", () => changeBuyFilters({ condition: condition.value }));
  const price = $("buy-max-price");
  price.addEventListener("change", commitBuyPrice);
  price.addEventListener("keydown", (event) => {
    if (event.key === "Enter") commitBuyPrice();
    else if (event.key === "Escape") price.value = buyPriceText(buyState.view?.filters.maxPriceCents ?? null);
  });
  $("buy-base-sellers").addEventListener("change", changeBuyBaseSellers);
  $("buy-curve").addEventListener("keydown", moveBetweenBuyRows);
  // The Cardmarket window covers the pane by screen position, so it follows the pane when it moves.
  window.addEventListener("resize", reportBuyPane);
  $("buy-body").addEventListener("scroll", reportBuyPane);
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
    // The summary names the chosen countries once their names are known.
    if (buyState.view && buyState.view.filters.countries.length) await loadBuyCountries();
  } catch (error) { noteFailure(error); }
}

// The screen stays built, with its last results, for the next visit.
function closeBuy() {
  closeBuyCountries(false);
  buyState.open = false;
  $("buy").hidden = true;
  $("buy-open").removeAttribute("aria-current");
  paintBuyPane();
}

// Drops everything built for an account that is gone.
function buyForget() {
  closeBuy();
  buyState.view = null;
  buyState.deckItems.clear();
  buyState.resultsKey = null;
  buyState.packages.clear();
  buyState.picked = null;
  buyState.detail = null;
  buyState.adding = false;
  buyState.packageRequest++;
  $("buy-decks").replaceChildren();
  $("buy-curve").replaceChildren();
  $("buy-notes").replaceChildren();
  $("buy-sellers").replaceChildren();
  $("buy-package-notes").replaceChildren();
  $("buy-package-arrival").textContent = "";
  $("buy-results").hidden = true;
  $("buy-package").hidden = true;
  $("buy-cart-dialog").close();
  renderBuyCart(null);
}

// A bridge call whose answer, refusal included, is dropped (undefined) when the account was left or the
// screen forgotten while it was out.
async function buyApi(name, ...args) {
  const epoch = accountState.epoch;
  const answer = await window.pywebview.api[name](...args);
  return pageUserGone(epoch) || !buyState.view ? undefined : checkAnswer(answer);
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
  // One box for the deck: ticked for all its cards, unticked for none, mixed for some.
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
  return { item, box, name, count, fold, list, known: deck.known, key: null, cards: [] };
}

function buyDeckCount(deck) {
  if (!deck.known) return "Labels unknown; sync";
  const all = plural(deck.copies, "card", "cards");
  return deck.tick === "some" ? `${deck.tickedCopies} of ${all}` : all;
}

function paintBuyDeck(entry, deck, running) {
  entry.known = deck.known;
  entry.box.checked = deck.tick === "all";
  entry.box.indeterminate = deck.tick === "some";
  if (deck.tick === "some") entry.box.setAttribute("aria-checked", "mixed");
  else entry.box.removeAttribute("aria-checked");
  entry.box.disabled = running || !deck.known;
  entry.box.setAttribute("aria-label", `Include ${deck.short}`);
  entry.name.textContent = deck.short;
  entry.name.title = deck.name;
  entry.count.textContent = buyDeckCount(deck);
  entry.fold.disabled = !deck.known;
  entry.fold.setAttribute("aria-label", `${deck.short} cards to buy`);
  // The card boxes are rebuilt only when the cards change, so a tick keeps the focus it was made with.
  const key = JSON.stringify(deck.cards.map((card) => [card.name, card.quantity]));
  if (entry.key !== key) {
    entry.key = key;
    entry.cards = deck.cards.map((card, index) => buildBuyCard(deck, card, index));
    entry.list.replaceChildren(...entry.cards.map((built) => built.item));
  }
  for (const [index, card] of deck.cards.entries()) {
    const built = entry.cards[index];
    built.box.checked = card.included;
    built.box.disabled = running;
    built.box.setAttribute("aria-label", `Include ${card.name} from ${deck.short}`);
  }
}

function buildBuyCard(deck, card, index) {
  const box = el("input", { type: "checkbox", id: `buy-card-${deck.id}-${index}` });
  box.addEventListener("change", () => setBuyCard(deck.id, card.name, box.checked));
  const item = el("li", {}, el("label", { class: "buy-card-tick", for: box.id },
    box, el("span", { text: times(card.quantity, card.name) })));
  return { item, box };
}

function paintBuyFilters() {
  const view = buyState.view;
  const running = view.status.running;
  for (const [id, key] of [["buy-language", "language"], ["buy-foil", "foil"], ["buy-delivery", "delivery"]]) {
    for (const button of $(id).children) {
      button.setAttribute("aria-pressed", String(button.dataset.value === view.filters[key]));
      button.disabled = running;
    }
  }
  const condition = $("buy-condition");
  condition.value = view.filters.condition;
  condition.disabled = running;
  const price = $("buy-max-price");
  price.value = buyPriceText(view.filters.maxPriceCents);
  price.disabled = running;
  paintBuyCountries(view.filters.countries, running);
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
  if (!running) buyState.adding = false;
  const adding = status.phase === "cart" || buyState.adding;
  const cart = $("buy-cart");
  cart.disabled = running || buyState.requested;
  if (running) cart.setAttribute("aria-busy", "true");
  else cart.removeAttribute("aria-busy");
  const cancel = $("buy-cancel");
  cancel.hidden = !running;
  cancel.disabled = status.cancelling;
  cancel.textContent = status.cancelling ? "Cancelling…" : adding ? "Cancel adding" : "Cancel scan";
  // The run that starts from the cart button disables it, which would drop the focus.
  if (running && document.activeElement === cart) cancel.focus();

  const stopped = status.phase === "stopped";
  const error = $("buy-error");
  error.hidden = !stopped;
  error.textContent = stopped ? status.message : "";
  error.title = stopped ? status.detail : "";
  $("buy-progress").textContent = progressLine(status);
  for (const entry of buyState.deckItems.values()) {
    entry.box.disabled = running || !entry.known;
    for (const built of entry.cards) built.box.disabled = running;
  }
  renderBuyResults(status);
  renderBuyCart(status.cart ?? null);
  paintBuyPane();
}

// While the owner signs in or passes Cloudflare's check, the pane takes the screen below the scan row.
function paintBuyPane() {
  const phase = buyState.view?.status.phase;
  const covered = buyState.open && BUY_PANE_PHASES.includes(phase);
  $("buy-pane").hidden = !covered;
  $("buy-body").classList.toggle("covered", covered);
  reportBuyPane();
}

// The pane's rectangle in CSS pixels with the device pixel ratio, or null while no pane shows.
function buyPaneArea() {
  const pane = $("buy-pane");
  if (pane.hidden) return null;
  const rect = pane.getBoundingClientRect();
  return [rect.left, rect.top, rect.width, rect.height, window.devicePixelRatio || 1];
}

// Tells the bridge where the pane is when that changed, one report at a time so they arrive in order. The
// bridge reads a zero-sized area as no pane.
async function reportBuyPane() {
  if (buyState.paneSending) return;
  buyState.paneSending = true;
  try {
    for (let area = buyPaneArea(); JSON.stringify(area) !== buyState.paneSent; area = buyPaneArea()) {
      buyState.paneSent = JSON.stringify(area);
      try {
        await api("buy_pane", ...(area ?? [0, 0, 0, 0, 1]));
      } catch (error) {
        // Forgotten, so that the next paint sends it again.
        buyState.paneSent = "";
        noteFailure(error);
        return;
      }
    }
  } finally {
    buyState.paneSending = false;
  }
}

function progressLine(status) {
  if (status.running) {
    const unit = status.phase === "cart" || buyState.adding ? "card" : "page";
    return status.cancelling ? `Stopping after this ${unit}…` : status.progress;
  }
  if (status.phase === "cancelled") return status.message;
  if (status.phase === "results" || status.phase === "carted") {
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
    const status = await buyApi("buy_scan");
    if (status) {
      paintBuyStatus(status);
      watchBuy();
    }
  } catch (error) { noteFailure(error); } finally {
    buyState.requested = false;
    if (buyState.view) paintBuyStatus(buyState.view.status);
  }
}

async function cancelBuyScan() {
  try {
    const status = await buyApi("buy_cancel");
    if (status) paintBuyStatus(status);
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
    const status = await buyApi("buy_status");
    if (!status) return;
    paintBuyStatus(status);
  } catch (error) {
    noteFailure(error);
    return;
  }
  watchBuy();
}

/* Choices */

async function setBuyDeck(id, included) {
  await changeBuyView(() => buyApi("buy_include", id, included), paintBuy);
}

async function setBuyCard(deckId, card, included) {
  await changeBuyView(() => buyApi("buy_include_card", deckId, card, included), paintBuy);
}

async function changeBuyFilters(change) {
  const filters = { maxPrice: buyPriceText(buyState.view.filters.maxPriceCents), ...buyState.view.filters, ...change };
  await changeBuyView(() => buyApi(
    "buy_filters", filters.language, filters.condition, filters.foil, filters.countries, filters.delivery,
    filters.maxPrice,
  ), paintBuyFilters);
}

// A choice the bridge answers with the new view: drawn if the screen is still the account's, and on a
// refusal `undo` puts the saved state back.
async function changeBuyView(ask, undo) {
  try {
    const view = await ask();
    if (view) acceptBuyView(view);
  } catch (error) {
    noteFailure(error);
    if (buyState.view) undo();
  }
}

// The cap as the field shows it: whole euros plain, otherwise with both decimals; blank for any price.
function buyPriceText(cents) {
  if (cents === null || cents === undefined) return "";
  return cents % 100 === 0 ? String(cents / 100) : (cents / 100).toFixed(2);
}

// Enter and leaving the field both land here; the bridge judges the text, and a refusal puts the saved cap back.
async function commitBuyPrice() {
  const text = $("buy-max-price").value.trim();
  if (text === buyState.pricePending || !buyState.view) return;
  if (text === buyPriceText(buyState.view.filters.maxPriceCents)) return;
  buyState.pricePending = text;
  try {
    await changeBuyFilters({ maxPrice: text });
  } finally {
    buyState.pricePending = null;
  }
}

async function changeBuyBaseSellers() {
  await changeBuyView(() => buyApi("buy_base_sellers", Number($("buy-base-sellers").value)), paintBuyFilters);
}

/* Seller location: a button that opens a list of checkboxes, one per Cardmarket country; none ticked is any */

function setupBuyCountries() {
  const picker = $("buy-picker");
  $("buy-countries").addEventListener("click", toggleBuyCountries);
  picker.addEventListener("keydown", moveInBuyCountries);
  document.addEventListener("mousedown", (event) => {
    if (!picker.contains(event.target)) closeBuyCountries(false);
  });
}

// The names are fetched once, when the list is first needed; a failure shows its message and tries again next time.
function loadBuyCountries() {
  if (buyState.countries) return Promise.resolve();
  buyState.loadingCountries ??= api("buy_countries").then((answer) => {
    buyState.countries = answer.countries;
    buyState.countryNames = new Map(answer.countries.map((country) => [country.id, country.name]));
    buildBuyCountries(answer.countries);
    if (buyState.view) paintBuyFilters();
  }).finally(() => { buyState.loadingCountries = null; });
  return buyState.loadingCountries;
}

function buildBuyCountries(countries) {
  const clear = el("button", {
    id: "buy-countries-clear", class: "menu-item", type: "button", text: "Clear countries",
    onclick: () => changeBuyFilters({ countries: [] }),
  });
  const boxes = new Map();
  const items = countries.map((country) => {
    const box = el("input", { type: "checkbox", id: `buy-country-${country.id}` });
    box.addEventListener("change", () => changeBuyFilters({ countries: pickedBuyCountries() }));
    boxes.set(country.id, box);
    return el("label", { class: "menu-item", for: box.id }, box, el("span", { text: country.name }));
  });
  buyState.countryBoxes = boxes;
  $("buy-countries-menu").replaceChildren(clear, ...items);
}

function pickedBuyCountries() {
  return [...buyState.countryBoxes].filter(([, box]) => box.checked).map(([id]) => id).sort((a, b) => a - b);
}

function buyCountriesSummary(ids) {
  if (!ids.length) return ["Any country", ""];
  const names = ids.map((id) => buyState.countryNames.get(id));
  if (names.some((name) => name === undefined)) return [plural(ids.length, "country", "countries"), ""];
  const spelled = names.sort((a, b) => a.localeCompare(b)).join(", ");
  return [ids.length <= BUY_COUNTRIES_NAMED ? spelled : plural(ids.length, "country", "countries"), spelled];
}

function paintBuyCountries(ids, running) {
  const chosen = new Set(ids);
  for (const [id, box] of buyState.countryBoxes) {
    box.checked = chosen.has(id);
    box.disabled = running;
  }
  const [text, full] = buyCountriesSummary(ids);
  $("buy-countries-text").textContent = text;
  $("buy-countries").title = full;
  $("buy-countries").disabled = running;
  const clear = $("buy-countries-clear");
  if (clear) clear.disabled = running || ids.length === 0;
  if (running) closeBuyCountries(false);
}

async function toggleBuyCountries() {
  if (!$("buy-countries-menu").hidden) {
    closeBuyCountries(true);
    return;
  }
  try {
    await loadBuyCountries();
  } catch (error) {
    noteFailure(error);
    return;
  }
  openBuyCountries();
}

function buyCountryStops() {
  return [...$("buy-countries-menu").querySelectorAll("button, input")].filter((stop) => !stop.disabled);
}

function openBuyCountries() {
  $("buy-countries-menu").hidden = false;
  $("buy-countries").setAttribute("aria-expanded", "true");
  const stops = buyCountryStops();
  (stops.find((stop) => stop.checked) || stops[0])?.focus();
}

function closeBuyCountries(refocus) {
  const menu = $("buy-countries-menu");
  if (menu.hidden) return;
  menu.hidden = true;
  $("buy-countries").setAttribute("aria-expanded", "false");
  if (refocus) $("buy-countries").focus();
}

// Arrows, Home and End move between the countries; Escape closes the list and returns to its button, and
// Tab leaves it for the next control.
function moveInBuyCountries(event) {
  if ($("buy-countries-menu").hidden || !BUY_MENU_KEYS.includes(event.key)) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeBuyCountries(true);
    return;
  }
  if (event.key === "Tab") {
    closeBuyCountries(false);
    return;
  }
  const stops = buyCountryStops();
  if (!stops.length) return;
  const at = stops.indexOf(document.activeElement);
  const last = stops.length - 1;
  const next = {
    ArrowDown: at < last ? at + 1 : 0, ArrowUp: at > 0 ? at - 1 : last, Home: 0, End: last,
  }[event.key];
  event.preventDefault();
  stops[next].focus();
}

/* The packages */

function renderBuyResults(status) {
  const results = status.results;
  const key = results ? JSON.stringify([results, status.message]) : "";
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
  $("buy-partial").hidden = !results.partial;
  $("buy-partial").title = status.message;
  $("buy-curve").replaceChildren(...results.rows.map(buildBuyRow));
  $("buy-notes").replaceChildren(...buyResultNotes(results));
  if (results.rows.some((row) => row.sellers === buyState.picked)) refreshBuyPackage();
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
    ["Over your max price:", results.overPrice.map((c) => `${c.card} (cheapest ${euro(c.cheapestCents)})`)],
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

// New results may have changed the package of the count on show, so it is asked for again, and a
// confirmation of the old one is withdrawn.
function refreshBuyPackage() {
  markBuyRows();
  const dialog = $("buy-cart-dialog");
  if (dialog.open) dialog.close();
  void pickBuyRow(buyState.picked);
}

async function pickBuyRow(sellers) {
  buyState.picked = sellers;
  markBuyRows();
  const sent = ++buyState.packageRequest;
  try {
    let detail = buyState.packages.get(sellers);
    if (!detail) {
      detail = await buyApi("buy_package", sellers);
      if (!detail) return;
      buyState.packages.set(sellers, detail);
    }
    if (sent === buyState.packageRequest) renderBuyPackage(detail);
  } catch (error) { noteFailure(error); }
}

function clearBuyPackage() {
  buyState.picked = null;
  buyState.detail = null;
  buyState.packageRequest++;
  markBuyRows();
  $("buy-package").hidden = true;
  $("buy-sellers").replaceChildren();
  $("buy-package-notes").replaceChildren();
  $("buy-package-arrival").textContent = "";
}

function renderBuyPackage(detail) {
  buyState.detail = detail;
  $("buy-package").hidden = false;
  $("buy-package-title").textContent = `${plural(detail.sellers, "seller", "sellers")} · ${euro(detail.totalCents)}`;
  $("buy-package-sub").textContent = `Cards ${euro(detail.cardsCents)} · shipping ${euro(detail.shippingCents)}`;
  const arrival = $("buy-package-arrival");
  const unknown = detail.withoutEstimate ? ` · ${plural(detail.withoutEstimate, "seller", "sellers")} without an estimate` : "";
  arrival.textContent = detail.slowestDays === null ? "" : `Arrives in about ${plural(detail.slowestDays, "day", "days")}${unknown}`;
  $("buy-sellers").replaceChildren(...detail.shipments.map(buildBuySeller));
  const notes = [];
  if (detail.leftOutNoEstimate) {
    notes.push(el("li", {}, el("strong", { text: "Left out, no delivery estimate: " }),
      plural(detail.leftOutNoEstimate, "offer", "offers")));
  }
  if (detail.missing.length) {
    notes.push(el("li", {}, el("strong", { text: "No usable offer: " }), detail.missing.join(", ")));
  }
  if (detail.short.length) {
    const names = detail.short.map((s) => `${s.card} (${s.wanted} wanted, ${s.available} offered)`);
    notes.push(el("li", {}, el("strong", { text: "Fewer copies offered than wanted: " }), names.join(", ")));
  }
  $("buy-package-notes").replaceChildren(...notes);
}

// A seller's days to you: its own estimate, or "about" when it comes from its country.
function buyDeliveryText(shipment) {
  if (shipment.deliveryDays === null) return "no delivery estimate";
  const days = plural(shipment.deliveryDays, "day", "days");
  if (shipment.deliveryBasis === "own") return days;
  const source = shipment.deliveryBasis === "country" ? `${shipment.country} median` : "Cardmarket's average";
  return `about ${days} (${source})`;
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
    el("p", { class: "muted small", text: `${shipment.country} · ${how} · ${buyDeliveryText(shipment)}` }),
    el("div", { class: "buy-table" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows))));
}

/* The cart: a confirmation that names what goes in, then a report of what Cardmarket's cart holds */

function setupBuyCart() {
  const dialog = $("buy-cart-dialog");
  $("buy-cart").addEventListener("click", openBuyCart);
  $("buy-cart-cancel").addEventListener("click", () => dialog.close());
  $("buy-cart-confirm").addEventListener("click", confirmBuyCart);
  // Escape closes the dialog natively; any way of closing it returns to the button that opened it.
  dialog.addEventListener("close", () => $("buy-cart").focus());
}

const buyCartCards = (detail) => detail.shipments
  .reduce((all, shipment) => all + shipment.picks.reduce((some, pick) => some + pick.copies, 0), 0);

function buildBuyCartSeller(shipment) {
  const picks = shipment.picks.map((pick) => el("li", {},
    el("span", { text: times(pick.copies, pick.card) }),
    el("span", { class: "num", text: euro(pick.priceCents) })));
  return el("section", { class: "buy-seller", "aria-label": `Seller ${shipment.seller}` },
    el("h3", { class: "buy-seller-name", text: shipment.seller }),
    el("ul", { class: "buy-cart-picks" }, ...picks),
    el("p", { class: "muted small", text: `Shipping ${euro(shipment.shippingCents)}` }));
}

function openBuyCart() {
  const detail = buyState.detail;
  if (!detail || $("buy-cart").disabled) return;
  const cards = plural(buyCartCards(detail), "card", "cards");
  $("buy-cart-dialog-title").textContent = `Add ${cards} to your Cardmarket cart?`;
  $("buy-cart-confirm").textContent = `Add ${cards} to cart`;
  $("buy-cart-dialog-sellers").replaceChildren(...detail.shipments.map(buildBuyCartSeller));
  $("buy-cart-dialog-total").textContent = `Total ${euro(detail.totalCents)} · cards ${euro(detail.cardsCents)}`
    + ` · shipping ${euro(detail.shippingCents)}`;
  $("buy-cart-dialog").showModal();
  $("buy-cart-cancel").focus();
}

async function confirmBuyCart() {
  const detail = buyState.detail;
  $("buy-cart-dialog").close();
  if (!detail || buyState.requested) return;
  buyState.requested = true;
  buyState.adding = true;
  try {
    const status = await buyApi("buy_add_to_cart", detail.sellers, detail.check);
    if (status) {
      paintBuyStatus(status);
      watchBuy();
    } else buyState.adding = false;
  } catch (error) {
    buyState.adding = false;
    noteFailure(error);
  } finally {
    buyState.requested = false;
    if (buyState.view) paintBuyStatus(buyState.view.status);
  }
}

function buyCartTitle(cart) {
  const added = plural(cart.added, "card", "cards");
  if (cart.status === "cancelled") return `Cancelled: ${added} added to your cart`;
  if (cart.status === "stopped") return `Stopped: ${added} added to your cart`;
  return cart.added ? `Added ${added} to your cart` : "Nothing was added to your cart";
}

const BUY_CART_COUNTS = [
  ["already", "already in your cart"], ["gone", "gone"], ["short", "short of the copies asked"],
  ["noAnswer", "without an answer"], ["notSent", "not added"],
];

function buyCartItemText(item) {
  switch (item.outcome) {
    case "gone": return item.message || "No longer available.";
    case "short": return `${item.inCart ?? 0} of ${item.copies} in the cart`;
    case "already": return "Already in your cart";
    case "no-answer": return "No answer from Cardmarket; check your cart";
    default: return "Not added";
  }
}

function buildBuyCartItem(item) {
  return el("li", {}, el("strong", { text: `${times(item.copies, item.card)} ` }),
    `(${item.seller}): ${buyCartItemText(item)}`);
}

// A seller whose cart shipping costs another price than the package's estimate; both are shown.
function buildBuyCartShipping(row) {
  return el("li", {}, el("strong", { text: `${row.seller} ` }),
    `Shipping: estimate ${euro(row.estimateCents)} ${row.estimateMethod} → cart ${euro(row.cartCents)} ${row.cartMethod}`);
}

function renderBuyCart(cart) {
  const key = cart ? JSON.stringify(cart) : "";
  if (key === buyState.cartKey) return;
  buyState.cartKey = key;
  $("buy-cart-report").hidden = !cart;
  if (!cart) {
    $("buy-cart-items").replaceChildren();
    $("buy-cart-shipping").replaceChildren();
    return;
  }
  $("buy-cart-title").textContent = buyCartTitle(cart);
  $("buy-cart-reason").hidden = !cart.reason;
  $("buy-cart-reason").textContent = cart.reason;
  $("buy-cart-counts").textContent = BUY_CART_COUNTS.filter(([field]) => cart[field] > 0)
    .map(([field, label]) => `${cart[field]} ${label}`).join(" · ");
  $("buy-cart-items").replaceChildren(...cart.items.map(buildBuyCartItem));
  $("buy-cart-shipping").replaceChildren(...cart.shipping.filter((row) => row.differs).map(buildBuyCartShipping));
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
