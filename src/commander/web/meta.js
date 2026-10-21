"use strict";

const DECK_TAGS_SHOWN = 4;
const THREATS_SHOWN = 4;
const STOPS_SHOWN = 4;
const ANSWERS_SHOWN = 6;

/* Meta (bracket 5): what in the meta stops the deck's key cards, and what stops the meta's combos */

// The open deck, and whether its own fetch in a running sync was pending at the last look.
const metaState = { deck: null, updating: false };
const PENDING_STEPS = ["queued", "fetching"];

function metaWanted() {
  return state.view && state.view.id === state.current && state.view.bracket === META_BRACKET
    && $("h-meta").getAttribute("aria-expanded") === "true";
}

// An answer without a report reads the same as none, except for the reason a source is missing.
const metaPanel = panel((deck) => api("meta", deck), renderMeta, {
  wanted: metaWanted,
  keyOf: (answer) => JSON.stringify(answer && answer.report ? answer : { why: answer ? answer.unavailable : null }),
});

// The panel reads the cache at any time. It says it is updating only while the sync has this deck queued or
// fetching, and rereads the cache once that fetch ends.
function syncMetaUpdating() {
  const deck = state.current;
  const updating = PENDING_STEPS.includes(fetchStates[String(deck)]);
  const finished = metaState.updating && metaState.deck === deck && !updating;
  Object.assign(metaState, { deck, updating });
  $("meta-busy").hidden = !updating;
  if (finished) metaPanel.load();
}

function renderMeta(deck, answer) {
  const report = answer ? answer.report : null;
  const why = answer && answer.unavailable ? answer.unavailable : "";
  $("meta-why").textContent = why;
  $("meta-why").hidden = !why;
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
    s.known ? `${s.withLists} lists since ${report.since}` : "No TopDeck lists",
    fetched ? new Date(fetched).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "",
  ].filter(Boolean).join(" · ");
  $("meta-body").replaceChildren(metaKeys(answer.keys), metaStoppers(answer.stoppers, answer.untouched), metaCombos(answer.comboGroups, answer.racing));
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

// A section folds on its own, and stays folded across decks and refreshes. The viewer's choice is kept by id.
function foldable(id, className, heading, ...children) {
  const key = `${PANEL_KEY}.${id}`;
  let open = true;
  try { open = localStorage.getItem(key) !== "0"; } catch { /* storage is a convenience */ }
  const section = el("details", { class: className, open }, el("summary", {}, heading), ...children);
  section.addEventListener("toggle", () => {
    try { localStorage.setItem(key, section.open ? "1" : "0"); } catch { /* as above */ }
  });
  return section;
}

function metaSection(title, ...children) {
  const id = `meta-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  return foldable(id, "meta-section", el("h3", { id, text: title }), ...children);
}

// A category inside "What stops you" folds like a section, by its name, so the choice holds across decks.
function stopCategory(name, label, ...children) {
  const id = `meta-stops-${name}`;
  return foldable(id, "meta-section role-group", el("h4", { id, class: "role-name", text: label }), ...children);
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
    return stopCategory(role, `${label} (${found.length})`, shortList(found, THREATS_SHOWN, stopperRow));
  });
  const clear = [
    ...untouched.combos.map((c) => el("li", { class: "deck-chip stops combo" },
      el("a", { href: c.url, target: "_blank", rel: "noopener", text: c.pieces, title: "Open on Commander Spellbook" }))),
    ...untouched.keys.map((name) => el("li", { class: "deck-chip stops", text: name })),
  ];
  return metaSection("What stops you",
    stoppers.length ? el("div", { class: "role-groups" }, ...groups) : el("p", { class: "empty", text: "Nothing in the meta stops your combos or key cards." }),
    clear.length ? stopCategory("untouched", "Untouched", el("ul", { class: "deck-chips" }, ...clear)) : null);
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

// A combo shows only the pieces outside its group's shared ones; the full list is on hover.
function comboRow(c, racing) {
  return el("div", { class: "meta-row" },
    el("p", { class: "meta-card" },
      el("a", { href: c.url, target: "_blank", rel: "noopener", text: c.others.length ? c.others.join(" + ") : "Only the shared pieces",
        title: `Open on Commander Spellbook: ${c.pieces.join(" + ")}` }),
      c.rising ? el("span", { class: "badge status rising", text: "Rising", title: "Twice as common in the last 30 days as in the 30 before" }) : null),
    c.results.length ? el("p", { class: "muted small", text: c.results.slice(0, 2).join("; ") }) : null,
    deckChips(c.decks),
    comboAnswers(c.answers, racing));
}

// Combos that share pieces come grouped under those pieces, as the server ordered them (ADR 0038).
function metaCombos(groups, racing) {
  const rows = groups.map((g) => g.shared.length
    ? el("div", { class: "combo-group" }, el("p", { class: "combo-shared", text: `Shared: ${g.shared.join(", ")}` }),
      ...g.combos.map((c) => comboRow(c, racing)))
    : comboRow(g.combos[0], racing));
  return metaSection("Meta combos",
    rows.length ? el("div", { class: "meta-rows" }, ...rows) : el("p", { class: "empty", text: "No combos in the meta." }));
}

function comboAnswers(answers, racing) {
  // A Turbo deck races the meta's combos rather than answering them (ADR 0031).
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
