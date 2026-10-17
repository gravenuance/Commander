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
  results: [],
  active: -1,
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
