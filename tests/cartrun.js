"use strict";

// Runs the cart's add script and answer script against a fake page, as `test_cmcart` does. Reads one JSON
// object on stdin: `start` and `answer` (the scripts), `token` (the page's token, null for no token field)
// and `reply` (the text the fake server answers with). Prints what the scripts did as JSON.
const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const calls = [];
const selectors = [];

class FormData {
  constructor() { this.fields = []; }
  append(name, value) { this.fields.push([name, value]); }
}

const window = {};
const document = {
  querySelector(selector) {
    selectors.push(selector);
    return input.token === null ? null : { value: input.token };
  },
};
const fetch = (url, options) => {
  calls.push({
    url, method: options.method, credentials: options.credentials, headers: options.headers,
    fields: options.body.fields,
  });
  return Promise.resolve({ status: 200, text: () => Promise.resolve(input.reply) });
};

const context = vm.createContext({ window, document, FormData, fetch, String, JSON });
const started = vm.runInContext(input.start, context);
const early = vm.runInContext(input.answer, context);
setImmediate(() => {
  const answer = vm.runInContext(input.answer, context);
  const again = vm.runInContext(input.answer, context);
  console.log(JSON.stringify({ started, selectors, calls, early, answer, again }));
});
