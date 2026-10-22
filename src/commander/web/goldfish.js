"use strict";

/* Goldfish: how the deck plays out over thousands of games against an empty table, and how soon it wins (ADR 0054) */

const goldfishPanel = panel(fetchGoldfish, renderGoldfish, { wanted: () => railTab === "goldfish" });
// The deck whose report, error or spinner the tab shows, and the latest ask still out ({ deck }).
let goldfishDrawn = null;
let goldfishPending = null;

// An answer to draw whether it is a report or a refusal: the tab shows the refusal where the figures
// would be, so a refusal is not also a toast.
async function fetchGoldfish(deck) {
  try {
    return await window.pywebview.api.goldfish(deck);
  } catch (error) {
    noteFailure(error);
    return { error: "The deck could not be played; try again." };
  }
}

function resetGoldfish() {
  $("goldfish").replaceChildren();
  goldfishPanel.forget();
  goldfishDrawn = null;
  goldfishPending = null;
}

// Plays the open deck while the tab is open. The first ask takes seconds, so the tab shows a spinner until
// then; a reload of the same deck leaves what is on show alone until the new figures differ. A reload while
// the first ask is out asks again (the panel drops the older answer) but keeps the spinner as it is.
async function loadGoldfish() {
  const deck = state.current;
  if (deck === null || railTab !== "goldfish") return;
  if (goldfishDrawn !== deck && goldfishPending?.deck !== deck) {
    goldfishPanel.forget();
    $("goldfish").replaceChildren(el("div", { class: "state inline", role: "status" },
      el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "Playing the deck…" })));
  }
  const ask = { deck };
  goldfishPending = ask;
  try {
    await goldfishPanel.load();
  } finally {
    if (goldfishPending === ask) goldfishPending = null;
  }
}

function renderGoldfish(deck, report) {
  goldfishDrawn = deck;
  if (report.error) {
    $("goldfish").replaceChildren(el("div", { class: "goldfish-error", role: "alert" },
      el("p", { class: "field-error", text: report.error }),
      el("button", {
        class: "button small", type: "button", text: "Play the deck again",
        onclick: () => { goldfishDrawn = null; loadGoldfish(); },
      })));
    return;
  }
  const ways = report.waysToWin;
  const notModelled = report.notModelled.length
    ? el("details", { class: "fold" }, el("summary", { text: `Not modelled (${report.notModelled.length})` }),
      el("p", { text: report.notModelled.join(", ") }))
    : null;
  $("goldfish").replaceChildren(
    openingSection(report),
    commanderSection(report),
    manaSection(report),
    screwSection(report),
    ways ? waysSection(report, ways) : "",
    notModelled || "",
    el("p", {
      class: "muted small goldfish-source", title: `Seed ${report.seed}, policy ${report.policyVersion}`,
      text: `${report.games.toLocaleString("en-GB")} games against an empty table`,
    }));
}

const goldfishNumber = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

function goldfishSection(title, ...children) {
  const id = `goldfish-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  return el("section", { class: "goldfish-section", "aria-labelledby": id },
    el("h3", { id, text: title }), ...children);
}

// A table of figures by turn: one row per figure, one column per turn the bracket shows.
function turnTable(label, turns, rows) {
  return el("table", { class: "goldfish-table", "aria-label": label },
    el("thead", {}, el("tr", {},
      el("th", { scope: "col", class: "muted", text: "Turn" }),
      ...turns.map((turn) => el("th", { scope: "col", text: String(turn) })))),
    el("tbody", {}, ...rows.map((row) => el("tr", { class: row.flag ? "faster" : null },
      el("th", { scope: "row" }, ...row.head),
      ...row.cells.map((cell) => el("td", { text: cell }))))));
}

function openingSection(report) {
  const rows = [
    ["Kept on seven cards", report.opening.seven],
    ["Kept on six cards", report.opening.six],
    ["Kept on five or fewer", report.opening.fiveOrFewer],
  ];
  return goldfishSection("Opening hand",
    el("ul", { class: "goldfish-rows" }, ...rows.map(([name, share]) => el("li", {},
      el("span", { text: name }), el("strong", { text: pct(share) })))),
    el("p", { class: "muted small", text: report.keepRule }));
}

function commanderSection(report) {
  const medians = report.commanders.map((c) => {
    const when = c.medianTurn === null ? "never" : `turn ${c.medianTurn}`;
    return report.commanders.length > 1 ? `${c.name}: ${when}` : when;
  });
  return goldfishSection("Commander cast by turn",
    turnTable("Commander cast by turn", report.turns, report.commanders.map((c) => ({
      head: [c.name], cells: c.castBy.map(pct),
    }))),
    el("p", { class: "muted small", text: `Median turn ${report.commanders.length > 1 ? "— " : ""}${medians.join("; ")}` }));
}

function manaSection(report) {
  return goldfishSection("Mana by turn",
    turnTable("Mana by turn", report.turns, [
      { head: ["Median"], cells: report.mana.median.map(goldfishNumber) },
      { head: ["Worst tenth"], cells: report.mana.worstTenth.map(goldfishNumber) },
    ]));
}

function screwSection(report) {
  const { share, turn } = report.colourScrew;
  return goldfishSection("Colour screw",
    el("p", { class: "goldfish-big" }, el("strong", { text: pct(share) }),
      document.createTextNode(turn === null ? " of games" : ` of games by turn ${turn}`)));
}

function waysSection(report, ways) {
  if (!ways.combos.length && !ways.finishers.length) return "";
  const faster = new Set(ways.fasterThanBracket.map((f) => f.id));
  const warning = ways.fasterThanBracket.length
    ? el("div", { class: "goldfish-warning", role: "group", "aria-labelledby": "goldfish-faster-title" },
      el("p", { id: "goldfish-faster-title", class: "goldfish-warning-title" },
        el("span", { "aria-hidden": "true", text: "⚠ " }),
        document.createTextNode(`Faster than bracket ${report.bracket}`)),
      el("ul", {}, ...ways.fasterThanBracket.map((f) => el("li", {
        text: `${f.label}: by turn ${f.turn} in ${pct(f.share)} of games`,
      }))))
    : null;
  const combos = ways.combos.length
    ? turnTable("Combos landed by turn", report.turns, ways.combos.map((combo) => ({
      flag: faster.has(combo.id),
      head: [
        faster.has(combo.id) ? el("span", { class: "sr-only", text: "Faster than its bracket: " }) : null,
        el("span", { class: "goldfish-name", text: combo.label }),
        combo.requirements.length
          ? el("span", { class: "muted small goldfish-detail", text: `Needs ${combo.requirements.join(", ")}` })
          : null,
        combo.fetchedShare > 0
          ? el("span", { class: "muted small goldfish-detail", text: `Tutor fetches a piece in ${pct(combo.fetchedShare)}` })
          : null,
      ],
      cells: combo.landedBy.map(pct),
    })))
    : null;
  const finishers = ways.finishers.length
    ? turnTable("Finishers castable by turn", report.turns, ways.finishers.map((f) => ({
      head: [el("span", { class: "goldfish-name", text: f.name })], cells: f.castableBy.map(pct),
    })))
    : null;
  return goldfishSection("Ways to win",
    warning || "",
    combos ? el("p", { class: "goldfish-group", text: "Combos landed" }) : "", combos || "",
    finishers ? el("p", { class: "goldfish-group", text: "Finishers castable" }) : "", finishers || "");
}

/* Figures a swap moves, shared by the Considering fold and Review */

// A figure's value in words: a share to one decimal (a swap's change is often under a point), a turn or mana.
function figureValue(figure, value) {
  if (value === null) return "never";
  if (figure.unit === "share") return `${(value * 100).toFixed(1)}%`;
  return figure.unit === "turn" ? `turn ${Math.round(value)}` : goldfishNumber(value);
}

// "Better" or "Worse" in words and a glyph, so the direction never rests on colour; no mark where neither
// way is better.
function figureMark(better) {
  if (better === null) return null;
  return el("span", { class: `figure-mark ${better ? "better" : "worse"}` },
    el("span", { "aria-hidden": "true", text: better ? "▲ " : "▼ " }), document.createTextNode(better ? "Better" : "Worse"));
}

function goldfishFigures(figures) {
  return el("ul", { class: "goldfish-figures" }, ...figures.map((figure) => el("li", { class: "goldfish-figure" },
    el("span", { class: "figure-label", text: figure.label }),
    el("span", { class: "figure-change" },
      el("span", { class: "sr-only", text: "from " }),
      document.createTextNode(figureValue(figure, figure.before)),
      el("span", { class: "sr-only", text: " to" }),
      el("span", { "aria-hidden": "true", text: " → " }),
      document.createTextNode(figureValue(figure, figure.after))),
    figureMark(figure.better))));
}
