"use strict";

/* Bracket: Spellbook's estimate beside the set bracket and the ceiling, and the cards that lift it (ADR 0021) */

const bracketState = { deck: null, key: null, marks: new Map() };

function setupBracket() {
  const chip = $("bracket-check");
  chip.addEventListener("click", () => {
    if (!chip.hasAttribute("aria-expanded")) return;
    const opening = $("bracket-detail").hidden;
    $("bracket-detail").hidden = !opening;
    chip.setAttribute("aria-expanded", String(opening));
  });
}

async function loadBracket() {
  if (state.current === null) return;
  const deck = state.current;
  try {
    const answer = await api("bracket", deck);
    if (deck === state.current) renderBracket(deck, answer);
  } catch { /* shown in the status line */ }
}

function renderBracket(deck, b) {
  const key = JSON.stringify(b);
  if (bracketState.deck === deck && bracketState.key === key) return;
  const newDeck = bracketState.deck !== deck;
  Object.assign(bracketState, { deck, key, marks: bracketMarks(b.causes) });
  const chip = $("bracket-check");
  const detail = $("bracket-detail");
  const gap = b.notes.length > 0;
  chip.hidden = !b.estimate && !gap;
  chip.textContent = b.estimate ? `Estimate ${b.estimate.brackets}` : "No estimate";
  chip.title = b.estimate ? `Commander Spellbook: ${b.estimate.tag}` : (b.unavailable || "");
  chip.classList.toggle("warn", gap);
  if (gap) {
    chip.setAttribute("aria-controls", "bracket-detail");
    if (newDeck || !chip.hasAttribute("aria-expanded")) chip.setAttribute("aria-expanded", "false");
  } else {
    chip.removeAttribute("aria-controls");
    chip.removeAttribute("aria-expanded");
  }
  detail.hidden = !gap || chip.getAttribute("aria-expanded") !== "true";
  detail.replaceChildren(
    ...b.notes.map((note) => el("p", { class: "bracket-note", text: note })),
    b.causes.length ? el("ul", { class: "causes", "aria-label": "What lifts the estimate" }, ...b.causes.map(causeItem)) : null,
  );
  paintBracket();
}

function causeItem(cause) {
  const cards = cause.comboUrl
    ? el("a", { href: cause.comboUrl, target: "_blank", rel: "noopener", text: cause.cards.join(" + "), title: "Open on Commander Spellbook" })
    : el("span", { text: cause.cards.join(", ") });
  return el("li", {},
    el("span", { class: "cause-tag", text: cause.brackets, title: cause.tag }),
    el("span", { text: `${cause.reason}: ` }),
    cards);
}

// Per card: the highest bracket a cause lifts it to (causes come highest first), and every reason.
function bracketMarks(causes) {
  const marks = new Map();
  for (const cause of causes) {
    for (const name of cause.cards) {
      const mark = marks.get(name) || { brackets: cause.brackets, reasons: [] };
      mark.reasons.push(`${cause.brackets}: ${cause.reason}`);
      marks.set(name, mark);
    }
  }
  return marks;
}

function paintBracket() {
  const marks = bracketState.deck === state.current ? bracketState.marks : new Map();
  for (const [name, node] of state.gridCards) {
    const mark = marks.get(name);
    let badge = node.querySelector(".bracket-mark");
    if (!mark) {
      if (badge) badge.remove();
      continue;
    }
    if (!badge) {
      badge = el("span", { class: "bracket-mark" });
      node.append(badge);
    }
    badge.textContent = mark.brackets;
    badge.title = mark.reasons.join("\n");
    badge.setAttribute("aria-label", `Lifts the bracket to ${mark.reasons.join("; ")}`);
  }
}
