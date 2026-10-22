"use strict";

/* Settings: the profile's commanders that play alike, the tag search, grace cards and themes to ignore */

const DEFAULT_GRACE = 3;
const settingsState = {
  deck: null, adjacent: [], tagSearch: false, grace: [], maxGrace: 10, ignore: [], themes: [], leanings: [], max: 8, pairWith: null,
};

async function openSettings() {
  const deck = state.current;
  try {
    const answer = await api("settings", deck);
    Object.assign(settingsState, {
      deck, adjacent: answer.adjacent, tagSearch: answer.tagSearch, grace: answer.grace, maxGrace: answer.maxGrace,
      ignore: answer.ignore, themes: answer.themes, leanings: answer.leanings, max: answer.maxAdjacent, pairWith: null,
    });
    $("settings-grace-name").value = "";
    $("settings-grace-factor").value = String(DEFAULT_GRACE);
    $("settings-deck").textContent = $("deck-short").textContent;
    $("settings-error").hidden = true;
    renderSettings();
    renderGrace();
    $("settings-dialog").showModal();
    $("settings-adjacent-input").focus();
  } catch { /* shown in the status line */ }
}

function renderSettings() {
  const s = settingsState;
  const chip = (text, remove, ...extra) => el("li", { class: "deck-chip setting-chip" }, el("span", { text }), ...extra,
    el("button", {
      class: "chip-remove", type: "button", text: "×", "aria-label": `Remove ${text}`, title: `Remove ${text}`,
      onclick: remove,
    }));
  $("settings-adjacent").replaceChildren(...s.adjacent.map((group, index) => chip(group.join(" + "), () => {
    s.adjacent.splice(index, 1);
    s.pairWith = null;
    renderSettings();
  }, group.length === 1 ? el("button", {
    class: "chip-pair", type: "button", text: s.pairWith === index ? "picking partner…" : "+ partner",
    "aria-pressed": String(s.pairWith === index), "aria-label": `Pair ${group[0]} with a partner`,
    onclick: () => {
      s.pairWith = s.pairWith === index ? null : index;
      renderSettings();
      $("settings-adjacent-input").focus();
    },
  }) : null)));
  $("settings-tag-search").checked = s.tagSearch;
  const input = $("settings-adjacent-input");
  const full = s.adjacent.length >= s.max && s.pairWith === null;
  input.disabled = full;
  input.placeholder = full ? `At most ${s.max}` : s.pairWith !== null ? `Partner for ${s.adjacent[s.pairWith][0]}` : "Add a commander";
  $("settings-ignore").replaceChildren(...s.ignore.map((name, index) => chip(name, () => {
    s.ignore.splice(index, 1);
    renderSettings();
  })));
  // The deck's own themes and leanings, one click to ignore.
  const ignored = new Set(s.ignore.map((name) => name.toLowerCase()));
  const offers = [...s.themes.map((t) => ({ name: t.name, kind: t.kind })), ...s.leanings.map((tag) => ({ name: tag, kind: "leaning" }))]
    .filter((o) => !ignored.has(o.name.toLowerCase()));
  $("settings-offers").replaceChildren(...(offers.length ? [
    el("p", { class: "muted small", text: "This deck's themes:" }),
    el("ul", { class: "deck-chips" }, ...offers.map((o) => el("li", {},
      el("button", { class: "deck-chip offer", type: "button", text: `+ ${o.name}`, title: `Ignore this ${o.kind}`,
        onclick: () => addIgnore(o.name) })))),
  ] : []));
}

// One row per grace card; typing in a factor changes the state alone, so the box keeps its focus.
function renderGrace() {
  const s = settingsState;
  $("settings-grace-factor").max = String(s.maxGrace);
  $("settings-grace").replaceChildren(...s.grace.map((card, index) => {
    const factor = el("input", {
      class: "grace-factor", type: "number", min: "1", max: String(s.maxGrace), step: "any",
      "aria-label": `Factor for ${card.name}`,
      oninput: (event) => { card.factor = event.target.value === "" ? null : Number(event.target.value); },
    });
    factor.value = String(card.factor);
    return el("li", {},
      el("span", { class: "grace-name", text: card.name }),
      factor,
      el("button", {
        class: "chip-remove", type: "button", text: "×", "aria-label": `Remove ${card.name}`,
        title: `Remove ${card.name}`,
        onclick: () => {
          s.grace.splice(index, 1);
          renderGrace();
          // The clicked button is gone: focus goes to the row that took its place, or to the add field.
          ($("settings-grace").children[index]?.querySelector("input") ?? $("settings-grace-name")).focus();
        },
      }));
  }));
}

// A card already listed (case aside) takes the new factor instead of a second row.
function addGrace() {
  const s = settingsState;
  const problem = $("settings-error");
  const name = $("settings-grace-name").value.trim();
  const factor = Number($("settings-grace-factor").value);
  if (!name) {
    $("settings-grace-name").focus();
    return;
  }
  if (!(factor > 1 && factor <= s.maxGrace)) {
    problem.textContent = `Enter a factor over 1 and at most ${s.maxGrace}.`;
    problem.hidden = false;
    $("settings-grace-factor").focus();
    return;
  }
  problem.hidden = true;
  const held = s.grace.find((card) => card.name.toLowerCase() === name.toLowerCase());
  if (held) held.factor = factor;
  else s.grace.push({ name, factor });
  $("settings-grace-name").value = "";
  $("settings-grace-factor").value = String(DEFAULT_GRACE);
  renderGrace();
  $("settings-grace-name").focus();
}

function addAdjacent(text) {
  const s = settingsState;
  if (s.pairWith !== null && s.adjacent[s.pairWith]) {
    const second = text.split("+").pop().trim();
    if (!second) return;
    s.adjacent[s.pairWith] = [s.adjacent[s.pairWith][0], second];
    s.pairWith = null;
    $("settings-adjacent-input").value = "";
    renderSettings();
    return;
  }
  const group = text.split("+").map((name) => name.trim()).filter(Boolean);
  if (!group.length || group.length > 2) {
    showStatus("A commander, or two partners joined by +", true);
    return;
  }
  const key = group.map((n) => n.toLowerCase()).sort().join("|");
  if (!settingsState.adjacent.some((g) => g.map((n) => n.toLowerCase()).sort().join("|") === key)) settingsState.adjacent.push(group);
  $("settings-adjacent-input").value = "";
  renderSettings();
}

function addIgnore(name) {
  const text = name.trim();
  if (text && !settingsState.ignore.some((n) => n.toLowerCase() === text.toLowerCase())) settingsState.ignore.push(text);
  $("settings-ignore-input").value = "";
  renderSettings();
}

async function saveSettings() {
  const s = settingsState;
  const problem = $("settings-error");
  problem.hidden = true;
  try {
    await api("save_settings", s.deck, s.adjacent, s.ignore, s.tagSearch, s.grace);
    $("settings-dialog").close();
    showStatus("Settings saved to the deck's profile.toml");
    await refresh();
  } catch (error) {
    // The modal dialog covers the status line, so the refusal is repeated inside it.
    problem.textContent = error instanceof Error ? error.message : String(error);
    problem.hidden = false;
  }
}

function setupSettings() {
  $("settings").addEventListener("click", openSettings);
  $("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
  $("settings-save").addEventListener("click", saveSettings);
  $("settings-tag-search").addEventListener("change", (event) => { settingsState.tagSearch = event.target.checked; });
  $("settings-grace-add").addEventListener("click", addGrace);
  for (const id of ["settings-grace-name", "settings-grace-factor"]) {
    $(id).addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addGrace();
    });
  }
  const adjacent = $("settings-adjacent-input");
  // A partner pair is typed "First + Second": matches are for the name after the last +. The second name,
  // typed or picked through "+ partner", may be any legal card, so the bridge is asked in partner mode.
  combobox(adjacent, $("settings-adjacent-results"), async (text) => {
    const parts = text.split("+");
    const typed = parts.pop().trim();
    if (typed.length < 2) return [];
    const head = parts.map((p) => p.trim()).filter(Boolean);
    const picking = settingsState.pairWith !== null;
    const answer = await api("find_commanders", typed, picking || head.length > 0);
    return answer.names.map((name) => ({ label: name, value: picking ? name : [...head, name].join(" + ") }));
  }, addAdjacent);
  combobox($("settings-ignore-input"), $("settings-ignore-results"), async (text) => {
    const needle = text.trim().toLowerCase();
    if (needle.length < 2) return [];
    const own = [...settingsState.themes.map((t) => ({ label: t.name, value: t.name, hint: t.kind })),
      ...settingsState.leanings.map((tag) => ({ label: tag, value: tag, hint: "leaning" }))]
      .filter((o) => o.label.toLowerCase().includes(needle));
    const answer = await api("find_tags", text);
    const seen = new Set(own.map((o) => o.label.toLowerCase()));
    return [...own, ...answer.tags.filter((t) => !seen.has(t.name)).map((t) => ({ label: t.name, value: t.name, hint: `Tagger tag, ${t.cards} cards` }))];
  }, addIgnore);
}
