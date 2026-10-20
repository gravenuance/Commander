"use strict";

// The deck as a card grid, with its zoom, label menu and card size.
const SIZE_KEY = "commander.cardSize";
// Grid card widths; Scryfall's normal image is 488 px wide, sharp up to L even on a 2x screen.
const CARD_SIZES = { s: 124, m: 160, l: 200 };
const COLOUR = /^#[0-9a-f]{3,8}$/i;
// WCAG relative luminance at which black and white text have the same contrast on a colour.
const BLACK_INK_ABOVE = 0.179;

// Sets --label to the colour and --on-label to black or white, whichever reads better on it.
function setLabelColour(node, colour) {
  let hex = colour.slice(1);
  if (hex.length <= 4) hex = [...hex.slice(0, 3)].map((digit) => digit + digit).join("");
  const [red, green, blue] = [0, 2, 4]
    .map((at) => parseInt(hex.slice(at, at + 2), 16) / 255)
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  const luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  node.style.setProperty("--label", colour);
  node.style.setProperty("--on-label", luminance > BLACK_INK_ABOVE ? "#000000" : "#ffffff");
}

function manaCost(cost) {
  const box = el("span", { class: "cost", "aria-label": cost ? `cost ${cost}` : undefined });
  for (const symbol of (cost || "").matchAll(/\{([^}]+)\}/g)) {
    const text = symbol[1];
    const colour = /^[WUBRG]$/.test(text) ? text : "";
    box.append(el("span", { class: `pip ${colour}`, text, "aria-hidden": "true" }));
  }
  return box;
}

function cardImage(src, name, className) {
  const box = el("span", { class: `${className} ${src ? "" : "missing"}` },
    el("span", { class: "art-name", text: name, "aria-hidden": "true" }));
  if (src) {
    const img = el("img", { alt: "", loading: "lazy", src });
    img.addEventListener("error", () => { box.classList.add("missing"); img.remove(); });
    box.prepend(img);
  }
  return box;
}

/* The deck as a card grid, one stack per card type, then the categories outside the deck */

// A group's key tells the deck's groups from a category outside the deck that shares a name.
const groupKey = (group) => group.key ?? group.name;

function renderGrid(groups) {
  const grid = $("grid");
  const seen = new Set();
  groups.forEach((group, index) => {
    const id = groupKey(group);
    seen.add(id);
    const key = JSON.stringify([group, state.view?.grace]);
    let known = state.groups.get(id);
    if (!known || known.key !== key) {
      const node = buildGroup(group);
      if (known) known.node.replaceWith(node);
      known = { key, node, names: group.cards.map((c) => c.name) };
      // The commander is never offered as a cut.
      if (id === "Commander") for (const card of node.querySelectorAll(".gcard")) card.dataset.commander = "1";
      state.groups.set(id, known);
    }
    place(grid, known.node, index);
  });
  for (const [id, known] of [...state.groups]) {
    if (!seen.has(id)) { known.node.remove(); state.groups.delete(id); }
  }
  // Only the deck's own cards can be cut or lit as targets: not those outside it, nor those still to come in.
  state.gridCards.clear();
  for (const node of grid.querySelectorAll(".gcard")) {
    if (!node.classList.contains("incoming") && !node.closest(".outside")) state.gridCards.set(node.dataset.name, node);
  }
}

function buildGroup(group) {
  const id = groupKey(group);
  const inDeck = group.inDeck !== false;
  const open = !state.collapsed.has(id);
  const stackId = `stack-${id.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  const toggle = el("button", { class: "group-toggle", type: "button", "aria-expanded": String(open), "aria-controls": stackId },
    el("span", { class: "chevron", "aria-hidden": "true" }),
    el("span", { class: "group-name", text: group.name }),
    el("span", { class: "count", text: `(${group.count})` }),
    group.cents ? el("span", { class: "group-price", text: euro(group.cents) }) : null,
    inDeck ? null : el("span", { class: "badge status outside-badge", text: "Not in deck" }));
  const stack = el("ul", { class: "stack", id: stackId, "aria-label": inDeck ? group.name : `${group.name}, not in the deck`, hidden: !open },
    ...group.cards.map((card) => buildGridCard(card, inDeck)));
  toggle.addEventListener("click", () => {
    const opening = stack.hidden;
    stack.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    if (opening) state.collapsed.delete(id);
    else state.collapsed.add(id);
  });
  return el("section", { class: `group ${inDeck ? "" : "outside"}` }, el("h3", { class: "group-title" }, toggle), stack);
}

// The copies badge: a queued count change shows as "×6 → 7", or Out when the card is to come out.
function copiesBadge(card) {
  const change = card.pending?.kind === "count" ? card.pending : null;
  if (change) {
    const text = change.count === 0 ? "Out" : `×${change.was} → ${change.count}`;
    const label = change.count === 0 ? "Queued to come out" : `${change.was} copies, ${change.count} after saving`;
    return el("span", { class: "qty pending", text, "aria-label": label, title: "Queued; saved through Review" });
  }
  return card.quantity > 1 ? el("span", { class: "qty", text: `×${card.quantity}`, "aria-label": `${card.quantity} copies` }) : null;
}

// + and - on a deck card, queued on the board; on a card still to come in, - takes it off the queue.
function countTools(card) {
  const incoming = card.pending?.kind === "add";
  const change = card.pending?.kind === "count" ? card.pending : null;
  const now = change ? change.count : card.quantity;
  const button = (text, step, label, onclick) => el("button", {
    class: "count-step", type: "button", text, "data-step": step, "aria-label": label, title: label, onclick,
  });
  let fewer = null;
  if (incoming) fewer = button("−", "-1", `Don't add ${card.name}`, () => decide({ id: card.pending.id, name: card.name }, "reopen"));
  else if (card.less) fewer = button("−", "-1", now === 1 ? `Take ${card.name} out` : `One fewer ${card.name}`, () => stepCount(card.name, -1));
  const back = change && change.count < change.was;
  const more = card.more ? button("+", "1", back ? `Put back one ${card.name}` : `One more ${card.name}`, () => stepCount(card.name, 1)) : null;
  if (!fewer && !more) return null;
  return el("span", { class: "count-tools", role: "group", "aria-label": `Copies of ${card.name}` }, fewer, more);
}

async function stepCount(name, step) {
  const tools = state.gridCards.get(name)?.querySelector(".count-tools");
  const controls = tools ? [...tools.querySelectorAll("button")] : [];
  await whileBusy(controls, async () => {
    try {
      const answer = await api("count", state.current, name, step);
      const change = answer.change;
      if (!change) showStatus(`${name}: no change queued`);
      else if (change.count === 0) showStatus(`${name}: queued to come out`);
      else showStatus(`${name}: ${change.was} → ${change.count}, queued`);
      await refresh();
    } catch { /* shown in the status line */ }
  });
  // The grid was rebuilt; keep the keyboard on this card's + or -, or the other one when it is gone.
  const again = state.gridCards.get(name)?.querySelector(".count-tools");
  const target = again?.querySelector(`[data-step="${step}"]`) ?? again?.querySelector(".count-step");
  const lost = !document.activeElement || document.activeElement === document.body;
  if (target && lost) target.focus();
}

function buildGridCard(card, inDeck = true) {
  const label = card.label || "";
  const incoming = card.pending?.kind === "add";
  const leaving = card.pending?.kind === "count" && card.pending.count === 0;
  let flag = null;
  // A label is set on an entry in the deck; a card still to come in gets Don't Have when it is saved.
  if (inDeck && !incoming) {
    flag = el("button", {
      class: `flag ${label ? "set" : ""}`, type: "button", "aria-haspopup": "menu",
      "aria-label": `Label for ${card.name}: ${label || "none"}`, title: label || "Set label",
    });
    if (label && COLOUR.test(card.labelColour)) setLabelColour(flag, card.labelColour);
    flag.addEventListener("click", () => openLabelMenu(flag, card.name, label));
  }
  const swap = el("button", { class: "swap", type: "button", hidden: true });
  swap.addEventListener("click", () => swapIn(swap.dataset.card, card.name));
  const note = incoming ? `New · ${card.category}` : leaving ? "Coming out" : "";
  const classes = ["gcard", incoming ? "incoming" : "", leaving ? "leaving" : ""].filter(Boolean).join(" ");
  const node = el("li", { class: classes, "data-name": card.name, title: card.name },
    cardImage(card.image, card.name, "face"),
    copiesBadge(card),
    el("span", { class: "sr-only", text: card.name }),
    card.share ? el("span", { class: "share", text: card.share.label, title: [card.share.tip, graceLine(state.view?.grace, GRACE_TIP_CARDS)].filter(Boolean).join("\n") }) : null,
    flag,
    note ? el("span", { class: "pending-note", text: note }) : null,
    el("span", { class: "out-note", hidden: true }),
    swap,
    countTools(card));
  node.addEventListener("click", (event) => {
    if (!event.target.closest("button")) zoom(card);
  });
  if (card.image) {
    node.tabIndex = 0;
    node.addEventListener("keydown", (event) => {
      if (event.target === node && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        zoom(card);
      }
    });
  }
  return node;
}

// The card large in the middle of the screen, everything else faded; × or Esc closes it.
function zoom(card) {
  if (!card.image) return;
  const image = $("zoom-image");
  image.src = card.image.replace("/normal/", "/large/");
  image.alt = card.name;
  zoomOpener = document.activeElement;
  $("zoom").showModal();
  $("zoom-close").focus();
}

let zoomOpener = null;
function setupZoom() {
  const dialog = $("zoom");
  $("zoom-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (zoomOpener && zoomOpener.isConnected) zoomOpener.focus();
    zoomOpener = null;
  });
  // A click on the faded backdrop lands on the dialog itself, outside the image.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

/* Label menu: the corner flag on each grid card */

const menuState = { flag: null, name: null };

function openLabelMenu(flag, name, current) {
  if (menuState.flag === flag) { closeLabelMenu(true); return; }
  const menu = $("label-menu");
  menuState.flag = flag;
  menuState.name = name;
  menu.setAttribute("aria-label", `Label for ${name}`);
  menu.replaceChildren(...state.view.labels.map((label) => {
    const item = el("button", {
      class: "menu-item", type: "button", role: "menuitemradio", tabindex: "-1",
      "aria-checked": String(label.name === current),
      onclick: () => chooseLabel(name, current, label.name),
    }, el("span", { class: "swatch", "aria-hidden": "true" }), el("span", { text: label.name || "No label" }));
    if (COLOUR.test(label.colour)) setLabelColour(item, label.colour);
    return item;
  }));
  flag.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  const box = flag.getBoundingClientRect();
  const height = menu.offsetHeight;
  const top = box.bottom + 4 + height > window.innerHeight ? Math.max(4, box.top - 4 - height) : box.bottom + 4;
  menu.style.top = `${top}px`;
  menu.style.left = `${Math.min(box.left, window.innerWidth - menu.offsetWidth - 4)}px`;
  (menu.querySelector('[aria-checked="true"]') || menu.firstChild).focus();
}

function closeLabelMenu(refocus) {
  const flag = menuState.flag;
  if (!flag) return;
  $("label-menu").hidden = true;
  flag.setAttribute("aria-expanded", "false");
  menuState.flag = null;
  menuState.name = null;
  if (refocus && flag.isConnected) flag.focus();
}

function setupLabelMenu() {
  const menu = $("label-menu");
  menu.addEventListener("keydown", (event) => {
    const items = [...menu.children];
    const at = items.indexOf(document.activeElement);
    const go = (index) => { event.preventDefault(); items[(index + items.length) % items.length].focus(); };
    if (event.key === "ArrowDown") go(at + 1);
    else if (event.key === "ArrowUp") go(at - 1);
    else if (event.key === "Home") go(0);
    else if (event.key === "End") go(items.length - 1);
    else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeLabelMenu(true); }
    else if (event.key === "Tab") closeLabelMenu(false);
  });
  document.addEventListener("mousedown", (event) => {
    if (menuState.flag && !menu.contains(event.target) && event.target !== menuState.flag) closeLabelMenu(false);
  });
  window.addEventListener("resize", () => closeLabelMenu(false));
  $("grid").addEventListener("scroll", () => closeLabelMenu(false), true);
}

async function chooseLabel(name, current, label) {
  const flag = menuState.flag;
  closeLabelMenu(true);
  if (label === current) return;
  flag.classList.add("saving");
  await whileBusy([flag], async () => {
    try {
      await api("set_label", state.current, name, label);
      showStatus(label ? `${name}: ${label}` : `${name}: no label`);
      await loadDeck();
    } catch { /* shown in the status line */ }
  });
  flag.classList.remove("saving");
  // The grid was rebuilt around the new label; keep the keyboard on this card's flag.
  const again = state.gridCards.get(name)?.querySelector(".flag");
  const lost = !document.activeElement || document.activeElement === document.body;
  if (again && lost) again.focus();
}

// The grid's card size, remembered per viewer.
function setupCardSize() {
  const apply = (size) => {
    document.documentElement.style.setProperty("--card-w", `${CARD_SIZES[size]}px`);
    for (const button of document.querySelectorAll(".grid-tools .size")) {
      button.setAttribute("aria-pressed", String(button.dataset.size === size));
    }
  };
  let size = "m";
  try { size = localStorage.getItem(SIZE_KEY) || "m"; } catch { /* storage is a convenience */ }
  apply(CARD_SIZES[size] ? size : "m");
  for (const button of document.querySelectorAll(".grid-tools .size")) {
    button.addEventListener("click", () => {
      apply(button.dataset.size);
      try { localStorage.setItem(SIZE_KEY, button.dataset.size); } catch { /* as above */ }
    });
  }
}
