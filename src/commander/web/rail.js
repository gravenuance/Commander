"use strict";

// The right-hand rail: the activity log and the mana tab's switch.
const ACTIVITY_KEY = "commander.activityOpen";

/* Activity rail */

// The right-hand rail: Activity or Mana open, or both folded to their labels. Remembered per viewer. It
// belongs to the open deck, so it is hidden, its panel folded, while no deck is open.
const RAIL_TABS = ["activity", "mana"];
let railTab = null;
// The tab the viewer last left open; a deck opening shows the rail that way again.
let railWanted = null;

function setupActivity() {
  try {
    const stored = localStorage.getItem(ACTIVITY_KEY);
    railWanted = stored === "1" ? "activity" : RAIL_TABS.includes(stored) ? stored : null;
  } catch { /* storage is a convenience */ }
  for (const tab of RAIL_TABS) {
    $(`${tab}-toggle`).addEventListener("click", () => showRail(railTab === tab ? null : tab));
  }
  // The open rail covers part of the main view, so Escape from inside it folds it and returns focus to its tab.
  $("activity-pane").addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || railTab === null) return;
    event.stopPropagation();
    const toggle = $(`${railTab}-toggle`);
    showRail(null);
    toggle.focus();
  });
  showRailFor(state.current !== null);
}

// Shows the rail while a deck is open, as the viewer left it; hides it and folds its panel when none is.
function showRailFor(deckOpen) {
  const pane = $("activity-pane");
  const shown = !pane.hidden;
  pane.hidden = !deckOpen;
  if (!deckOpen) paintRail(null);
  else if (!shown) paintRail(railWanted);
}

// The viewer's choice of tab, remembered.
function showRail(tab) {
  railWanted = tab;
  try { localStorage.setItem(ACTIVITY_KEY, tab || "0"); } catch { /* as above */ }
  paintRail(tab);
}

function paintRail(tab) {
  railTab = tab;
  for (const name of RAIL_TABS) {
    $(`${name}-toggle`).setAttribute("aria-expanded", String(name === tab));
    $(name).hidden = name !== tab;
  }
  $("activity-pane").classList.toggle("open", tab !== null);
  if (tab === "mana") manaPanel.load();
}

function renderActivity(entries) {
  const list = $("activity");
  list.replaceChildren(...entries.map((entry) => {
    const when = new Date(entry.at);
    const what = [entry.action, entry.card].filter(Boolean).join(" ");
    return el("li", {},
      el("time", { datetime: entry.at, text: when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) }),
      el("span", {}, el("span", { class: `who ${entry.actor}`, text: entry.actor }), document.createTextNode(` ${what}`)),
      entry.detail ? el("span", { class: "detail", text: entry.detail }) : null);
  }));
}
