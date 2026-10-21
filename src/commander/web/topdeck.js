"use strict";

/* TopDeck.gg: the API key the tournament lists need, kept in Windows Credential Manager (ADR 0047) */

const TOPDECK_TIPS = { true: "TopDeck key saved", false: "No TopDeck key saved" };

function setupTopdeck() {
  $("topdeck-open").addEventListener("click", openTopdeck);
  $("topdeck-close").addEventListener("click", () => $("topdeck-dialog").close());
  $("topdeck-test").addEventListener("click", testTopdeckKey);
  $("topdeck-remove").addEventListener("click", removeTopdeckKey);
  $("topdeck-form").addEventListener("submit", (event) => {
    event.preventDefault();
    saveTopdeckKey();
  });
  // The key never stays in the page: closing the dialog by any way clears the field.
  $("topdeck-dialog").addEventListener("close", () => {
    $("topdeck-key").value = "";
    $("topdeck-open").focus();
  });
}

async function openTopdeck() {
  $("topdeck-key").value = "";
  $("topdeck-error").hidden = true;
  $("topdeck-dialog").showModal();
  $("topdeck-key").focus();
  await readTopdeck(() => window.pywebview.api.topdeck());
}

// Paints what a bridge call answered: the saved state, or the refusal. Returns whether it succeeded.
async function readTopdeck(call, okText) {
  setTopdeckBusy(true);
  try {
    const answer = await call();
    if (answer.error) {
      showTopdeckError(answer.error);
      return false;
    }
    $("topdeck-error").hidden = true;
    if ("hasKey" in answer) paintTopdeck(answer.hasKey, okText);
    else $("topdeck-state").textContent = okText || "";
    return true;
  } catch {
    showTopdeckError("TopDeck could not be reached; the details are in the log.");
    return false;
  } finally {
    setTopdeckBusy(false);
  }
}

function paintTopdeck(hasKey, note) {
  $("topdeck-state").textContent = note || TOPDECK_TIPS[hasKey];
  $("topdeck-remove").hidden = !hasKey;
  $("topdeck-open").title = TOPDECK_TIPS[hasKey];
}

function showTopdeckError(text) {
  $("topdeck-error").textContent = text;
  $("topdeck-error").hidden = false;
}

function setTopdeckBusy(busy) {
  for (const id of ["topdeck-test", "topdeck-save", "topdeck-remove"]) $(id).disabled = busy;
}

// Asks TopDeck whether it takes the typed key; with none typed, the saved one.
function testTopdeckKey() {
  const typed = $("topdeck-key").value;
  return readTopdeck(() => window.pywebview.api.test_topdeck_key(typed), "TopDeck accepts the key.");
}

async function saveTopdeckKey() {
  const typed = $("topdeck-key").value;
  const saved = await readTopdeck(() => window.pywebview.api.save_topdeck_key(typed), "Key saved. Sync fetches the tournaments.");
  // A saved key leaves the page; a refused one stays, so a typo can be mended.
  if (saved) $("topdeck-key").value = "";
}

// The opener's tip says whether a key is saved, before the dialog is ever opened.
async function loadTopdeck() {
  try {
    const answer = await window.pywebview.api.topdeck();
    if (!answer.error) $("topdeck-open").title = TOPDECK_TIPS[answer.hasKey];
  } catch { /* the tip stays empty; the dialog reads the state again */ }
}

async function removeTopdeckKey() {
  await readTopdeck(() => window.pywebview.api.remove_topdeck_key(), "Key removed.");
}
