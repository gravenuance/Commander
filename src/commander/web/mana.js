"use strict";

/* Mana: the deck's mana base against Frank Karsten's published numbers (ADR 0019) */

const COLOUR_NAMES = { W: "White", U: "Blue", B: "Black", R: "Red", G: "Green", C: "Colourless" };
const KARSTEN_LANDS = "https://www.tcgplayer.com/content/article/How-Many-Lands-Do-You-Need-in-Your-Deck-An-Updated-Analysis/cd1c1a24-d439-4a8e-b369-b936edb0b38a/";
const KARSTEN_SOURCES = "https://www.tcgplayer.com/content/article/How-Many-Sources-Do-You-Need-to-Consistently-Cast-Your-Spells-A-2022-Update/dc23a7d2-0a16-4c0b-ad36-586fcca03ad8/";

const manaPanel = panel((deck) => api("mana", deck), renderMana, { wanted: () => railTab === "mana" });

function resetMana() {
  $("mana").replaceChildren();
  manaPanel.forget();
}

function renderMana(deck, m) {
  const l = m.lands;
  const c = m.comparison;
  const cedh = state.view && state.view.effectiveBracket === CEDH_BRACKET;
  const fmt = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));
  const lands = el("section", { class: "mana-section" },
    el("h3", { text: "Lands" }),
    el("p", { class: "mana-big" }, el("strong", { text: fmt(l.lands) }), document.createTextNode(" lands"),
      l.landSpells ? el("span", { class: "muted small", text: ` (${l.landSpells} land/spell card${l.landSpells === 1 ? "" : "s"} as part of a land)` }) : null),
    c ? el("p", { class: "mana-line" },
      el("span", { text: `${c.lists} ${c.label}: median ${fmt(c.lands)} (${fmt(c.low)}–${fmt(c.high)})` }),
      el("span", { class: "muted small", text: `Mana rocks, dorks and rituals: yours ${c.deckManaCards}, theirs ${fmt(c.manaCards)}` }))
      : el("p", { class: "muted small", text: m.compareWith ? `No ${m.compareWith} to compare with yet; they come with the next refresh.` : "Nothing to compare with." }),
    el("p", { class: `mana-line ${cedh ? "muted" : ""}` },
      el("span", { text: `Karsten's formula: ${fmt(l.suggested)}` }),
      el("span", { class: "muted small", text: `average mana value ${fmt(l.averageManaValue)}, ${l.cheapDraw} cheap draw, ${l.cheapRamp} cheap ramp` }),
      cedh ? el("span", { class: "muted small", text: "Made for casual games; the tournament lists are the better guide here." }) : null));
  const colours = el("section", { class: "mana-section" }, el("h3", { text: "Colour sources" }),
    ...m.colours.map((colour) => {
      const short = colour.needs.filter((n) => n.short);
      const hardest = colour.needs[0];
      return el("div", { class: "mana-colour" },
        el("p", { class: "mana-colour-head" },
          el("span", { class: `pip ${colour.colour}`, text: colour.colour, title: COLOUR_NAMES[colour.colour] }),
          el("span", { text: `${fmt(colour.lands)} from lands` }),
          el("span", { class: short.length ? "badge status rejected" : "muted small",
            text: short.length ? `${short.length} short` : hardest ? `all ${colour.needs.length} cards fine` : "no cards need it" })),
        short.length ? el("ul", { class: "mana-needs" }, ...short.map(needRow)) : null);
    }));
  const tapped = el("section", { class: "mana-section" }, el("h3", { text: "Turn one" }),
    m.turnOne.length
      ? el("ul", { class: "mana-needs" }, ...m.turnOne.map(needRow))
      : el("p", { class: "muted small", text: "Every one-drop has enough untapped sources." }),
    el("p", { class: "muted small", text: `Lands that enter tapped: ${fmt(m.tappedLands)}` }));
  const skipped = m.skipped.length
    ? el("details", { class: "fold" }, el("summary", { text: `Not counted (${m.skipped.length})` }),
      el("p", { text: `Cast for an alternative cost, or paid with hybrid or Phyrexian mana: ${m.skipped.join(", ")}` }))
    : null;
  const source = el("p", { class: "muted small mana-source" }, document.createTextNode("From Frank Karsten's "),
    el("a", { href: KARSTEN_LANDS, target: "_blank", rel: "noopener", text: "land count" }), document.createTextNode(" and "),
    el("a", { href: KARSTEN_SOURCES, target: "_blank", rel: "noopener", text: "colour source" }), document.createTextNode(" articles."));
  $("mana").replaceChildren(lands, colours, tapped, skipped || "", m.plan ? planSection(deck, m.plan) : "", source);
}

// The suggested mana base below bracket 5 (ADR 0024): land count, basics by colour, then named swaps.
function planSection(deck, p) {
  const section = el("section", { class: "mana-section mana-plan", "aria-labelledby": "mana-plan-title" },
    el("h3", { id: "mana-plan-title", tabindex: "-1", text: "Suggested changes" }));
  if (p.unchanged) {
    section.append(el("p", { class: "muted small", text: "Meets Karsten's numbers; nothing to change." }));
    return section;
  }
  if (p.landChange) {
    const run = Math.round(p.lands + p.landChange);
    section.append(el("p", { class: "mana-line" },
      el("span", {}, el("strong", { text: `Run ${run} lands` }),
        el("span", { class: "muted", text: ` (${p.landChange > 0 ? "+" : "−"}${Math.abs(p.landChange)})` })),
      p.cutCandidates.length
        ? el("span", { class: "muted small", text: `Room from: ${p.cutCandidates.join(", ")}` })
        : el("span", { class: "muted small", text: "Fewer basics, as below." })));
  }
  section.append(el("ul", { class: "mana-basics", "aria-label": "Basic lands and cards short of sources" },
    ...p.basics.map((b) => {
      const outcome = p.colours.find((c) => c.colour === b.colour);
      return el("li", { class: `mana-basic${b.now === b.after ? "" : " changed"}` },
        el("span", { class: `pip ${b.colour}`, text: b.colour, title: COLOUR_NAMES[b.colour] }),
        el("span", {}, document.createTextNode(`${b.name} ${b.now} → ${b.after}`),
          b.replaced ? el("span", { class: "muted small", text: ` (−${b.replaced} for the lands below)` }) : null),
        outcome ? el("span", { class: "muted small", text: `${outcome.shortNow} → ${outcome.shortAfter} short` }) : null);
    })));
  const within = p.swaps.filter((s) => s.withinBudget);
  const over = p.swaps.filter((s) => !s.withinBudget);
  const budget = p.budgetCents !== null ? ` (up to ${euro(p.budgetCents)})` : "";
  if (within.length) section.append(swapGroup(over.length ? `Within budget${budget}` : "", within));
  if (over.length) section.append(swapGroup("Over budget", over));
  if (p.stillShort.length) {
    section.append(el("p", { class: "muted small", text: `Still short: ${p.stillShort.join(", ")}` }));
  }
  const waiting = p.swaps.filter((s) => !s.board).length;
  if (waiting) {
    section.append(el("button", {
      class: "button primary small", type: "button", text: `Put ${waiting} swap${waiting === 1 ? "" : "s"} on the board`,
      onclick: (event) => manaToBoard(deck, event.currentTarget),
    }));
  }
  return section;
}

// Swaps under a small heading; with no heading when every swap is within budget.
function swapGroup(title, swaps) {
  const list = el("ul", { class: "mana-swaps", "aria-label": title || "Swaps" }, ...swaps.map((s) =>
    el("li", { class: "mana-swap", title: s.reason },
      el("span", { class: "swap-add", text: `+ ${times(s.copies, s.add)}` }),
      el("span", { class: "muted small", text: `for ${times(s.copies, s.cut)}` }),
      s.board ? el("span", { class: "badge status", text: s.board === "rejected" ? "Turned down" : "On the board" })
        : s.cents !== null ? el("span", { class: "muted small", text: euro(s.cents) }) : null)));
  return title ? el("div", { class: "mana-swap-group" }, el("p", { class: "mana-group-title", text: title }), list) : list;
}

async function manaToBoard(deck, button) {
  button.disabled = true;
  try {
    const answer = await api("mana_to_board", deck);
    const n = answer.added.length;
    showStatus(`${n} swap${n === 1 ? "" : "s"} put under Considering`);
    if (deck === state.current) await loadDeck();
    await manaPanel.load();
    // The clicked button went with the old panel; keep the keyboard in the plan.
    const active = document.activeElement;
    const lost = !active || active === document.body || !active.isConnected;
    if (lost && deck === state.current) {
      ($("mana").querySelector(".button.primary") ?? $("mana-plan-title"))?.focus();
    }
  } catch {
    button.disabled = false; /* shown in the status line */
  }
}

function needRow(n) {
  return el("li", { class: "mana-need", title: `${n.pattern}: needs ${n.needed} sources, has ${n.have}` },
    el("span", { class: "card-name", text: n.card }),
    el("span", { class: "muted small", text: n.pattern }),
    el("span", { class: "mana-gap", text: `${n.have} / ${n.needed}` }));
}
