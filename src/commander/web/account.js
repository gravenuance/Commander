"use strict";

/* Account: who is signed in, signing in and out, and the app's sync (ADR 0023) */

const accountState = { user: null, signedIn: false, syncing: false };

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
  const name = $("account-name");
  name.textContent = a.user || "";
  name.title = !a.user ? "" : a.signedIn ? `Signed in to Archidekt as ${a.user}` : `${a.user}, not signed in`;
  $("sign-in").hidden = Boolean(a.user && a.signedIn);
  $("sign-out").hidden = !a.user;
  paintSync(a.sync.running, !a.user, a.sync.running ? a.sync.progress : a.user ? "" : NO_ACCOUNT_NOTE);
  setMetaUpdating(a.sync.running);
  if (wasSyncing && !a.sync.running && a.sync.summary) {
    showStatus(a.sync.summary, a.sync.failed);
    reloadAfterSync();
  }
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
    const switched = answer.user !== accountState.user;
    $("signin-dialog").close();
    renderAccount(answer);
    if (switched) await showAccountDecks();
  } catch {
    $("signin-error").textContent = "Signing in failed; the details are in the log.";
    $("signin-error").hidden = false;
  } finally {
    // The password leaves the page however the call ends: a retry types it again.
    password.value = "";
    submit.disabled = false;
  }
}

// Another account's decks live in another folder: start the deck list over.
async function showAccountDecks() {
  state.current = null;
  state.view = null;
  state.stamps = {};
  $("deck").hidden = true;
  await loadDecks();
}

async function signOut() {
  try {
    renderAccount(await api("sign_out"));
    openSignIn();
  } catch { /* shown in the status line */ }
}

async function startSync() {
  try {
    await api("sync");
    await loadAccount();
  } catch { /* shown in the status line */ }
}
