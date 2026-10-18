"use strict";

const DECK_TAGS_SHOWN = 4;
const THREATS_SHOWN = 4;
const STOPS_SHOWN = 4;
const ANSWERS_SHOWN = 6;

/* Meta (bracket 5): what in the meta stops the deck's key cards, and what stops the meta's combos */

const metaState = { busy: false };

function metaWanted() {
  return state.view && state.view.id === state.current && state.view.bracket === META_BRACKET
    && $("h-meta").getAttribute("aria-expanded") === "true";
}

// An answer without a report reads the same as none.
const metaPanel = panel((deck) => api("meta", deck), renderMeta, {
  wanted: () => metaWanted() && !metaState.busy,
  keyOf: (answer) => JSON.stringify(answer && answer.report ? answer : null),
});

// The sync refreshes the meta answers: the panel shows it is updating and rereads them when the sync ends.
function setMetaUpdating(on) {
  metaState.busy = on;
  $("meta-busy").hidden = !on;
}

function renderMeta(deck, answer) {
  const report = answer ? answer.report : null;
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
