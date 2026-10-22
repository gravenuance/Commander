"use strict";

// Runs a test program against the app page's real scripts, under a minimal fake DOM built from the real
// index.html. Called by tests/pagerun.py: stdin is {"web": <folder of the page>, "program": <JS body>}, stdout
// is {"result": <what the program returned>} or {"error": <stack>}.
//
// The program is the body of an async function. It sees the page's globals (state, el, $, buildTile ...) and:
//   document, window  the fake DOM; `window.pywebview.api` forwards to `stub`
//   stub              the bridge's methods; set `stub.decks = async () => ({ decks: [] })` (the page's own
//                     `api()` function keeps that name); a call to an unset one fails
//   calls             every bridge call so far, as [name, ...args]
//   timers            callbacks the page scheduled with setTimeout (never run on their own)
//   storage           what the page keeps in localStorage, as a plain object
//   fire(node, type, init)  dispatches an event that bubbles to the document, awaits its async handlers and
//                     returns it: fire(button, "click"), fire(document, "keydown", { key: "Escape" })
//   settle()          waits for the async work the page started without handing back its promise
//   walk(node)        every element under a node, in document order

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const VOID_TAGS = new Set(["meta", "link", "br", "hr", "img", "input"]);
// Events the browser fires on their target alone, so that only a capturing listener above it hears them.
const NON_BUBBLING = new Set(["mouseenter", "mouseleave", "focus", "blur", "scroll"]);
const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };

class FakeText {
  constructor(data) {
    this.data = data;
    this.parentNode = null;
  }
  get textContent() { return this.data; }
}

class FakeElement {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.style = { setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; } };
    this.value = "";
    this.offsetTop = 0;
    this.offsetHeight = 0;
    this.offsetWidth = 0;
  }
  getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; }

  // Attributes, and the properties the page uses that reflect them.
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  #flag(name, on) { if (on) this.attrs.set(name, ""); else this.attrs.delete(name); }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(on) { this.#flag("hidden", on); }
  get disabled() { return this.attrs.has("disabled"); }
  set disabled(on) { this.#flag("disabled", on); }
  get open() { return this.attrs.has("open"); }
  set open(on) { this.#flag("open", on); }
  get id() { return this.attrs.get("id") ?? ""; }
  set id(value) { this.attrs.set("id", value); }
  get title() { return this.attrs.get("title") ?? ""; }
  set title(value) { this.attrs.set("title", value); }
  get href() { return this.attrs.get("href") ?? ""; }
  set href(value) { this.attrs.set("href", value); }
  get className() { return this.attrs.get("class") ?? ""; }
  set className(value) { this.attrs.set("class", value); }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    return {
      add: (name) => { if (!names().includes(name)) this.className = [...names(), name].join(" "); },
      remove: (name) => { this.className = names().filter((n) => n !== name).join(" "); },
      contains: (name) => names().includes(name),
      toggle: (name, force) => {
        const on = force ?? !names().includes(name);
        if (on && !names().includes(name)) this.className = [...names(), name].join(" ");
        if (!on) this.className = names().filter((n) => n !== name).join(" ");
        return on;
      },
    };
  }
  get dataset() {
    const key = (prop) => `data-${prop.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
    return new Proxy({}, {
      get: (_, prop) => this.attrs.get(key(String(prop))),
      set: (_, prop, value) => { this.attrs.set(key(String(prop)), String(value)); return true; },
      has: (_, prop) => this.attrs.has(key(String(prop))),
    });
  }

  // The tree.
  get children() { return this.childNodes.filter((n) => n instanceof FakeElement); }
  get firstChild() { return this.childNodes[0] ?? null; }
  get parentElement() { return this.parentNode; }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument.documentElement;
  }
  get previousElementSibling() {
    const siblings = this.parentNode?.children ?? [];
    return siblings[siblings.indexOf(this) - 1] ?? null;
  }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(text) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    if (text !== "" && text !== null && text !== undefined) this.#adopt(new FakeText(String(text)), null);
  }
  #adopt(child, before) {
    child.parentNode?.removeChild?.(child);
    child.parentNode = this;
    const at = before === null ? this.childNodes.length : this.childNodes.indexOf(before);
    this.childNodes.splice(at, 0, child);
  }
  append(...nodes) {
    for (const node of nodes) this.#adopt(typeof node === "string" ? new FakeText(node) : node, null);
  }
  prepend(...nodes) {
    for (const node of [...nodes].reverse()) this.#adopt(typeof node === "string" ? new FakeText(node) : node, this.childNodes[0] ?? null);
  }
  insertBefore(node, before) { this.#adopt(node, before); return node; }
  replaceChildren(...nodes) {
    this.textContent = "";
    this.append(...nodes);
  }
  removeChild(child) {
    this.childNodes.splice(this.childNodes.indexOf(child), 1);
    child.parentNode = null;
  }
  remove() { this.parentNode?.removeChild(this); }
  after(node) {
    const siblings = this.parentNode.childNodes;
    this.parentNode.insertBefore(node, siblings[siblings.indexOf(this) + 1] ?? null);
  }
  replaceWith(node) {
    const parent = this.parentNode;
    parent.insertBefore(node, this);
    this.remove();
  }
  contains(node) {
    for (let at = node; at; at = at.parentNode) if (at === this) return true;
    return false;
  }

  // Selectors: compounds of tag, #id, .class and [attr] / [attr="v"], descendants by space, lists by comma.
  matches(selector) { return selector.split(",").some((part) => this.#matchesChain(part.trim().split(/\s+/))); }
  #matchesChain(chain) {
    if (!this.#matchesCompound(chain[chain.length - 1])) return false;
    let index = chain.length - 2;
    for (let up = this.parentNode; up && index >= 0; up = up.parentNode) {
      if (up.#matchesCompound(chain[index])) index--;
    }
    return index < 0;
  }
  #matchesCompound(compound) {
    const parts = compound.match(/^([a-z][a-z0-9]*)?((?:[#.][\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/);
    if (!parts) throw new Error(`unsupported selector ${compound}`);
    if (parts[1] && parts[1].toUpperCase() !== this.tagName) return false;
    for (const part of parts[2].match(/[#.][\w-]+|\[[^\]]+\]/g) ?? []) {
      if (part[0] === "#") { if (this.id !== part.slice(1)) return false; }
      else if (part[0] === ".") { if (!this.classList.contains(part.slice(1))) return false; }
      else {
        const [name, quoted] = part.slice(1, -1).split("=");
        if (!this.attrs.has(name) || (quoted !== undefined && this.attrs.get(name) !== quoted.slice(1, -1))) return false;
      }
    }
    return true;
  }
  closest(selector) {
    for (let node = this; node instanceof FakeElement; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }
  querySelectorAll(selector) { return walk(this).filter((node) => node.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }

  // Events bubble to the document (all but NON_BUBBLING), with capture listeners of the ancestors first.
  addEventListener(type, listener, capture = false) {
    const list = this.listeners.get(type) ?? [];
    list.push({ listener, capture: capture === true });
    this.listeners.set(type, list);
  }
  dispatchEvent(event) {
    event.target ??= this;
    const doc = this.ownerDocument;
    const route = [];
    for (let node = this; node; node = node.parentNode) route.push(node);
    if (route[route.length - 1] === doc.documentElement) route.push(doc);
    const fire = (node, capture) => {
      for (const entry of node.listeners.get(event.type) ?? []) {
        if (entry.capture !== capture) continue;
        event.currentTarget = node;
        const out = entry.listener.call(node, event);
        if (out && typeof out.then === "function") event.pending.push(out);
        if (event.stopped) return false;
      }
      return true;
    };
    if (![...route].reverse().every((node) => fire(node, true))) return !event.defaultPrevented;
    (NON_BUBBLING.has(event.type) ? [this] : route).every((node) => fire(node, false));
    return !event.defaultPrevented;
  }
  focus() {
    this.ownerDocument.activeElement = this;
    this.dispatchEvent(makeEvent("focus"));
  }
  blur() {
    if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
  }
  click() { this.dispatchEvent(makeEvent("click")); }
  scrollIntoView() {}
  select() {}
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    // Focus inside a closed dialog is lost, as in the browser when the opener is hidden by then.
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    this.dispatchEvent(makeEvent("close"));
  }
}

function makeEvent(type, init = {}) {
  return {
    type, stopped: false, defaultPrevented: false, target: null, key: undefined, pending: [], ...init,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.stopped = true; },
  };
}

// Dispatches an event and waits for the async handlers it started.
async function fire(node, type, init = {}) {
  const event = makeEvent(type, init);
  node.dispatchEvent(event);
  await Promise.all(event.pending);
  return event;
}

function walk(root) {
  const found = [];
  const visit = (node) => {
    for (const child of node.children) {
      found.push(child);
      visit(child);
    }
  };
  visit(root);
  return found;
}

class FakeDocument extends FakeElement {
  constructor() {
    super(null, "#document");
    this.ownerDocument = this;
    this.documentElement = new FakeElement(this, "html");
    this.body = new FakeElement(this, "body");
    this.documentElement.append(this.body);
    this.activeElement = this.body;
  }
  get isConnected() { return true; }
  createElement(tag) { return new FakeElement(this, tag); }
  createTextNode(text) { return new FakeText(text); }
  getElementById(id) { return this.body.querySelector(`#${id}`); }
  querySelector(selector) { return this.body.querySelector(selector); }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
}

// A small parser for index.html, which is well-formed: tags, attributes, text and the script list.
function buildPage(doc, html) {
  const scripts = [...html.matchAll(/<script src="([^"]+)"><\/script>/g)].map((m) => m[1]);
  const body = html.split("<body>", 2)[1].replace(/<script[\s\S]*?<\/script>/g, "");
  const decode = (text) => text.replace(/&(?:amp|lt|gt|quot|#39);/g, (m) => ENTITIES[m]);
  let at = doc.body;
  for (const token of body.matchAll(/<!--[\s\S]*?-->|<\/([a-z0-9]+)>|<([a-z0-9]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/g)) {
    if (token[1]) {
      at = at.parentNode;
    } else if (token[2]) {
      const node = doc.createElement(token[2]);
      for (const attr of token[3].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(attr[1], decode(attr[2] ?? ""));
      at.append(node);
      if (!VOID_TAGS.has(token[2])) at = node;
    } else if (token[4] && token[4].trim()) {
      at.append(new FakeText(decode(token[4])));
    }
  }
  return scripts;
}

async function main() {
  const { web, program } = JSON.parse(fs.readFileSync(0, "utf8"));
  const document = new FakeDocument();
  const scripts = buildPage(document, fs.readFileSync(path.join(web, "index.html"), "utf8"));

  const calls = [];
  const timers = [];
  const storage = {};
  const stub = new Proxy({}, {
    get: (target, name) => target[name] ?? (async (...args) => { throw new Error(`no stub for bridge method ${String(name)}${JSON.stringify(args)}`); }),
  });
  const bridge = new Proxy({}, {
    get: (_, name) => (...args) => {
      calls.push([name, ...args]);
      return stub[name](...args);
    },
  });
  const window = new FakeElement(document, "window");
  window.pywebview = { api: bridge };
  window.innerWidth = 1280;
  window.innerHeight = 800;
  const sandbox = {
    document, window, console: { log() {}, warn() {}, error() {} }, navigator: {},
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    clearTimeout: () => {},
    localStorage: {
      getItem: (key) => (key in storage ? storage[key] : null),
      setItem: (key, value) => { storage[key] = String(value); },
    },
    stub, calls, timers, storage, walk,
    fire,
    settle: () => new Promise((resolve) => setImmediate(resolve)),
  };
  vm.createContext(sandbox);
  for (const script of scripts) {
    vm.runInContext(fs.readFileSync(path.join(web, script), "utf8"), sandbox, { filename: script });
  }
  const result = await vm.runInContext(`(async () => {\n${program}\n})()`, sandbox, { filename: "program" });
  return { result: result === undefined ? null : result };
}

main().then(
  (out) => process.stdout.write(JSON.stringify(out)),
  (error) => {
    process.stdout.write(JSON.stringify({ error: String(error && error.stack ? error.stack : error) }));
    process.exitCode = 1;
  },
);
