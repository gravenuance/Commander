"use strict";

// The right-hand rail: the activity log and the mana tab's switch.
const ACTIVITY_KEY = "commander.activityOpen";

/* Activity rail */

// The right-hand rail: Activity or Mana open, or both folded to their labels. Remembered per viewer.
const RAIL_TABS = ["activity", "mana"];
let railTab = null;

function setupActivity() {
  let open = null;
  try {
    const stored = localStorage.getItem(ACTIVITY_KEY);
    open = stored === "1" ? "activity" : RAIL_TABS.includes(stored) ? stored : null;
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
  showRail(open);
}

function showRail(tab) {
  railTab = tab;
  for (const name of RAIL_TABS) {
    $(`${name}-toggle`).setAttribute("aria-expanded", String(name === tab));
    $(name).hidden = name !== tab;
  }
  $("activity-pane").classList.toggle("open", tab !== null);
  try { localStorage.setItem(ACTIVITY_KEY, tab || "0"); } catch { /* as above */ }
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
