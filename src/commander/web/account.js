"use strict";

/* Account: who is signed in, signing in and out, and the app's sync (ADR 0023) */

// `active` is whether the page shows an account: it is false until a sign-in succeeds, and again after any way
// out of the sign-in dialog without one. It is the page's own state; the bridge keeps the config's account.
// `epoch` counts the changes of that, so a deck answer that outlives its account is dropped.
const accountState = { user: null, signedIn: false, syncing: false, active: false, epoch: 0 };

// Each sync button with the line under it that says why it cannot be pressed, or what the sync is doing.
const SYNC_ROWS = [["sync-decks", "sync-status"], ["no-decks-sync", "no-decks-note"]];
const SYNC_TIP = "Pull your decks, then refresh the meta and ideas";
const NO_ACCOUNT_NOTE = "Sign in to sync";
const ACCOUNT_FAILED_NOTE = "Could not read the account; see the log";

function setupAccount() {
  $("sign-in").addEventListener("click", openSignIn);
  $("sign-out").addEventListener("click", signOut);
  $("sync-decks").addEventListener("click", startSync);
  $("no-decks-sync").addEventListener("click", startSync);
  $("signin-cancel").addEventListener("click", () => $("signin-dialog").close());
  $("signed-out-sign-in").addEventListener("click", openSignIn);
  // Not now, Escape and every other way out land here: without a sign-in the page shows no one.
  $("signin-dialog").addEventListener("close", () => {
    if (accountState.active && accountState.signedIn) return;
    showSignedOut();
    $("signed-out-sign-in").focus();
  });
  $("signin-form").addEventListener("submit", (event) => {
    event.preventDefault();
    signIn();
  });
}

async function loadAccount() {
  const answer = await window.pywebview.api.account();
  if (answer.error) {
    paintSync(false, true, ACCOUNT_FAILED_NOTE);
    return null;
  }
  renderAccount(answer);
  return answer;
}

function renderAccount(a) {
  const wasSyncing = accountState.syncing;
  Object.assign(accountState, { user: a.user, signedIn: a.signedIn, syncing: a.sync.running });
  paintFetchStates(a.sync.running ? a.sync.decks : {});
  syncMetaUpdating();
  if (!accountState.active) {
    paintSignedOut();
    return;
  }
  const name = $("account-name");
  name.textContent = a.user || "";
  name.title = !a.user ? "" : a.signedIn ? `Signed in to Archidekt as ${a.user}` : `${a.user}, not signed in`;
  $("sign-in").hidden = Boolean(a.user && a.signedIn);
  $("sign-out").hidden = !a.user;
  paintSync(a.sync.running, !a.user, a.sync.running ? a.sync.progress : a.user ? "" : NO_ACCOUNT_NOTE);
  if (wasSyncing && !a.sync.running && a.sync.summary) {
    showStatus(a.sync.summary, a.sync.failed);
    reloadAfterSync();
  }
}

// The foot of the deck list while the page has no account: no name, Sync off with its note, Sign in offered.
function paintSignedOut() {
  const name = $("account-name");
  name.textContent = "";
  name.title = "";
  $("sign-in").hidden = false;
  $("sign-out").hidden = true;
  paintSync(false, true, NO_ACCOUNT_NOTE);
}

// A sync button is disabled while a sync runs, and without an account to sync; the note says which.
function paintSync(running, blocked, note) {
  for (const [buttonId, noteId] of SYNC_ROWS) {
    const button = $(buttonId);
    button.hidden = false;
    button.disabled = running || blocked;
    button.title = note || SYNC_TIP;
    if (running) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
    button.querySelector(".spinner").hidden = !running;
    $(noteId).textContent = note;
  }
}

async function reloadAfterSync() {
  try {
    await loadDecks();
    await loadDeck();
    await metaPanel.load();
  } catch { /* shown in the status line */ }
}

// True when an answer asked for under `epoch` belongs to an account the page no longer shows.
function pageUserGone(epoch) {
  return epoch !== accountState.epoch || !accountState.active;
}

// The page shows this account from now on; the caller loads its decks. `answer` is null when it could not be read.
function enterAccount(answer) {
  accountState.epoch++;
  accountState.active = true;
  $("signed-out").hidden = true;
  if (answer) renderAccount(answer);
}

// Empties the page of the account's decks: the list, the deck, the rail panels and the name, then offers Sign in.
function showSignedOut() {
  accountState.epoch++;
  accountState.active = false;
  state.decks = [];
  state.current = null;
  state.view = null;
  state.stamps = {};
  renderDecks();
  renderActivity([]);
  $("mana").replaceChildren();
  manaPanel.forget();
  showRailFor(false);
  $("deck").hidden = true;
  $("loading").hidden = true;
  $("no-decks").hidden = true;
  $("copy-to-buy").hidden = true;
  $("signed-out").hidden = false;
  paintSignedOut();
}

function openSignIn() {
  $("signin-user").value = accountState.user || "";
  $("signin-password").value = "";
  $("signin-error").hidden = true;
  $("signin-dialog").showModal();
  (accountState.user ? $("signin-password") : $("signin-user")).focus();
}

async function signIn() {
  const submit = $("signin-submit");
  const password = $("signin-password");
  submit.disabled = true;
  $("signin-error").hidden = true;
  try {
    const answer = await window.pywebview.api.sign_in($("signin-user").value, password.value);
    if (answer.error) {
      $("signin-error").textContent = answer.error;
      $("signin-error").hidden = false;
      password.focus();
      return;
    }
    enterAccount(answer);
    $("signin-dialog").close();
    await showAccountDecks();
    // The dialog's opener is hidden by now, so focus would fall to the page body.
    ($("deck-list").querySelector(".deck-button") ?? $("sync-decks")).focus();
  } catch {
    $("signin-error").textContent = "Signing in failed; the details are in the log.";
    $("signin-error").hidden = false;
  } finally {
    // The password leaves the page however the call ends: a retry types it again.
    password.value = "";
    submit.disabled = false;
  }
}

// The page starts over with the account's decks, which may live in another folder than the last ones.
async function showAccountDecks() {
  state.current = null;
  state.view = null;
  state.stamps = {};
  $("deck").hidden = true;
  showRailFor(false);
  $("loading").hidden = false;
  try {
    await loadDecks();
  } finally {
    $("loading").hidden = true;
  }
}

async function signOut() {
  try {
    const answer = await api("sign_out");
    showSignedOut();
    renderAccount(answer);
    openSignIn();
  } catch { /* shown in the status line */ }
}

async function startSync() {
  try {
    await api("sync");
    await loadAccount();
  } catch { /* shown in the status line */ }
}
