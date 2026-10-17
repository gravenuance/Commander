"use strict";

// Every DOM node is built with textContent, never innerHTML: board text comes from people and Claude.
//
// Shared by the page scripts: the open deck's state and the helpers every area uses.
const SEARCH_DELAY_MS = 150;
const STATUS_MS = 6000;
const META_BRACKET = 5;
const PANEL_KEY = "commander.panelOpen";

const state = {
  decks: [],
  current: null,
  view: null,
  stamps: {},
  metaStamp: null,
  tiles: new Map(),
  groups: new Map(),
  gridCards: new Map(),
  collapsed: new Set(),
  tagsKey: null,
  leaningsKey: null,
  selected: null,
  hovered: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);

// Moves a node only when it is out of place, so focus inside an unchanged node survives a refresh.
function place(list, node, index) {
  if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

async function api(name, ...args) {
  const answer = await window.pywebview.api[name](...args);
  if (answer && answer.error) {
    showStatus(answer.error, true);
    throw new Error(answer.error);
  }
  return answer;
}

// Disables the controls while work runs, so a second click or Enter cannot send a second request.
async function whileBusy(controls, work) {
  if (controls.some((control) => control.disabled)) return;
  const focused = controls.find((control) => control === document.activeElement);
  for (const control of controls) {
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
  }
  try {
    await work();
  } finally {
    for (const control of controls) {
      control.disabled = false;
      control.removeAttribute("aria-busy");
    }
    const lost = !document.activeElement || document.activeElement === document.body;
    if (focused?.isConnected && lost) focused.focus();
  }
}

// Confirmations fade after ms; errors stay, announced at once, until dismissed.
let statusTimer = 0;
function showStatus(text, error = false, ms = STATUS_MS) {
  const box = $("status");
  const line = $("status-text");
  line.setAttribute("aria-live", error ? "assertive" : "polite");
  line.textContent = text;
  box.classList.toggle("error", error);
  box.hidden = false;
  clearTimeout(statusTimer);
  if (!error) statusTimer = setTimeout(hideStatus, ms);
}

function hideStatus() {
  clearTimeout(statusTimer);
  $("status").hidden = true;
}

const times = (copies, name) => (copies > 1 ? `${copies} × ${name}` : name);
const euro = (cents) => (cents === null || cents === undefined ? "" : `€${(cents / 100).toFixed(2)}`);
const pct = (share) => `${Math.round(share * 100)}%`;

// A panel that loads an answer for the open deck and draws it. `load()` drops an answer that arrives after
// the deck changed; `render(deck, answer)` skips one the panel already shows. The renderer is called with
// (deck, answer, fresh), fresh meaning this deck was not on show before. `wanted` says when a request is
// worth sending; `keyOf` decides which answers count as the same.
function panel(loader, renderer, { wanted = () => true, keyOf = JSON.stringify } = {}) {
  const shown = { deck: null, key: null };
  function render(deck, answer) {
    const key = keyOf(answer);
    if (shown.deck === deck && shown.key === key) return;
    const fresh = shown.deck !== deck;
    shown.deck = deck;
    shown.key = key;
    renderer(deck, answer, fresh);
  }
  async function load() {
    if (state.current === null || !wanted()) return;
    const deck = state.current;
    try {
      const answer = await loader(deck);
      if (deck === state.current) render(deck, answer);
    } catch { /* shown in the status line */ }
  }
  return { load, render };
}

// A text box that offers matches as you type, as an ARIA combobox over a listbox: arrows move through the
// matches, Enter takes the highlighted one or the typed text, Escape closes the list. `find(text)` answers
// [{ label, value, hint? }] and `pick(value)` takes the choice. An answer that a newer keystroke has
// superseded, or that arrives after the box lost focus, is dropped.
function combobox(input, list, find, pick) {
  let timer = 0;
  let sent = 0;
  let items = [];
  let active = -1;
  const close = () => {
    items = [];
    active = -1;
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  };
  const mark = (index) => {
    active = index;
    [...list.children].forEach((item, i) => item.setAttribute("aria-selected", String(i === index)));
    input.setAttribute("aria-activedescendant", list.children[index].id);
    list.children[index].scrollIntoView({ block: "nearest" });
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const mine = ++sent;
    timer = setTimeout(async () => {
      let found = [];
      try { found = await find(input.value); } catch { /* the status line says what went wrong */ }
      if (mine !== sent) return;
      if (document.activeElement !== input) { close(); return; }
      items = found;
      active = -1;
      input.removeAttribute("aria-activedescendant");
      list.replaceChildren(...items.map((item, index) => el("li", {
        role: "option", id: `${list.id}-option-${index}`, "aria-selected": "false",
        onmousedown: (event) => { event.preventDefault(); close(); pick(item.value); },
      }, el("span", { text: item.label }), item.hint ? el("span", { class: "type", text: item.hint }) : null)));
      list.hidden = items.length === 0;
      input.setAttribute("aria-expanded", String(!list.hidden));
    }, SEARCH_DELAY_MS);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" && items.length) { event.preventDefault(); mark((active + 1) % items.length); }
    else if (event.key === "ArrowUp" && items.length) { event.preventDefault(); mark((active - 1 + items.length) % items.length); }
    else if (event.key === "Enter") {
      event.preventDefault();
      const value = active >= 0 ? items[active].value : input.value.trim();
      sent++;
      close();
      if (value) pick(value);
    } else if (event.key === "Escape" && !list.hidden) { event.preventDefault(); event.stopPropagation(); close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}
