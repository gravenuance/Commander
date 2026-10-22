"use strict";

const POLL_MS = 2000;

/* Keeping up with changes from the CLI and Claude */

// Whether the last reload of the deck list failed, so the next poll asks again whatever the stamps say.
let decksStale = false;

async function poll() {
  try {
    const answer = await window.pywebview.api.stamps();
    if (!answer.error && accountState.active) {
      const stamps = answer.stamps;
      const key = String(state.current);
      const changed = decksStale || JSON.stringify(stamps) !== JSON.stringify(state.stamps);
      const currentChanged = state.current !== null && stamps[key] !== state.stamps[key];
      // While a save runs, the deck is reloaded when it ends; its old stamp stays so the change is not lost.
      const deferred = currentChanged && state.busy;
      const [decks, deck] = await Promise.allSettled([
        changed ? loadDecks() : null,
        currentChanged && !deferred ? loadDeck() : null,
      ]);
      // The open deck's stamp is loadDeck's to set, so a failed reload of it is tried again next tick.
      state.stamps = state.current === null ? stamps : { ...stamps, [key]: state.stamps[key] };
      decksStale = decks.status === "rejected";
      for (const load of [decks, deck]) if (load.status === "rejected") throw load.reason;
      // `cmdr meta` and a running sync fill the same cache; reread the open panel from it.
      if (accountState.syncing) await loadAccount();
      const metaChanged = answer.meta !== state.metaStamp;
      state.metaStamp = answer.meta;
      if (metaChanged) await metaPanel.load();
    }
  } catch { /* a missed poll is retried on the next tick */ }
  setTimeout(poll, POLL_MS);
}

window.addEventListener("pywebviewready", async () => {
  setupPropose();
  setupHover();
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
