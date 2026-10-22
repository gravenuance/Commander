"use strict";

const POLL_MS = 2000;

/* Keeping up with changes from the CLI and Claude */

// Whether the last reload of the deck list failed, so the next poll asks again whatever the stamps say.
let decksStale = false;

// The open deck's failed reload: it is asked for again once its stamp changes, or after this many polls, so a
// deck that stays unreadable neither repeats its error every tick nor holds up the rest of the poll.
const DECK_RETRY_TICKS = 8;
let failedDeck = null;

function deckRetryDue(key, stamp) {
  if (!failedDeck || failedDeck.key !== key || failedDeck.stamp !== stamp) return true;
  return ++failedDeck.skipped >= DECK_RETRY_TICKS;
}

async function poll() {
  try {
    const answer = await window.pywebview.api.stamps();
    if (!answer.error && accountState.active) {
      const stamps = answer.stamps;
      const key = String(state.current);
      const changed = decksStale || JSON.stringify(stamps) !== JSON.stringify(state.stamps);
      // A deck missing from the stamps was deleted by a sync; the deck list reload closes it.
      const currentChanged = state.current !== null && key in stamps && stamps[key] !== state.stamps[key];
      // While a save runs, the deck is reloaded when it ends; its old stamp stays so the change is not lost.
      const deferred = currentChanged && state.busy;
      const reloadDeck = currentChanged && !deferred && deckRetryDue(key, stamps[key]);
      const [decks, deck] = await Promise.allSettled([
        changed ? loadDecks() : null,
        reloadDeck ? loadDeck() : null,
      ]);
      // The open deck's stamp is loadDeck's to set, so a failed reload of it keeps the old one. The deck open
      // now may not be the one the poll started with.
      const open = String(state.current);
      state.stamps = state.current === null ? stamps : { ...stamps, [open]: state.stamps[open] };
      decksStale = decks.status === "rejected";
      if (reloadDeck) failedDeck = deck.status === "rejected" ? { key, stamp: stamps[key], skipped: 0 } : null;
      // A failed load is the bridge's to show; the rest of the poll still runs, and a script error is logged.
      for (const load of [decks, deck]) if (load.status === "rejected") noteFailure(load.reason);
      // `cmdr meta` and a running sync fill the same cache; reread the open panel from it.
      if (accountState.syncing) await loadAccount();
      const metaChanged = answer.meta !== state.metaStamp;
      state.metaStamp = answer.meta;
      if (metaChanged) await metaPanel.load();
    }
  } catch (error) {
    // A missed poll is retried on the next tick.
    noteFailure(error);
  }
  setTimeout(poll, POLL_MS);
}

window.addEventListener("pywebviewready", async () => {
  setupPropose();
  setupTiles();
  setupSharePopover();
  setupReview();
  setupPanels();
  setupCardSize();
  setupZoom();
  $("export").addEventListener("click", exportDeck);
  $("copy-to-buy").addEventListener("click", copyCardsToBuy);
  setupSettings();
  setupLabelMenu();
  setupActivity();
  setupBracket();
  setupAccount();
  setupTopdeck();
  loadTopdeck();
  $("status-close").addEventListener("click", hideStatus);
  try {
    const account = await loadAccount();
    if (!account) {
      // The account could not be read: no lists either, and the note says why.
      showSignedOut();
      paintSync(false, true, ACCOUNT_FAILED_NOTE);
    } else if (!account.signedIn) {
      // No lists until a sign-in succeeds; the dialog opens over the empty page.
      showSignedOut();
      openSignIn();
    } else {
      enterAccount(account);
      await loadDecks();
      const answer = await window.pywebview.api.stamps();
      if (!answer.error) {
        state.stamps = answer.stamps;
        state.metaStamp = answer.meta;
      }
    }
  } catch {
    $("loading").hidden = true;
  }
  setTimeout(poll, POLL_MS);
});
