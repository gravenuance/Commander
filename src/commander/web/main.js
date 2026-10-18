"use strict";

const POLL_MS = 2000;

/* Keeping up with changes from the CLI and Claude */

async function poll() {
  try {
    const answer = await window.pywebview.api.stamps();
    if (!answer.error && accountState.active) {
      const stamps = answer.stamps;
      const key = String(state.current);
      const changed = JSON.stringify(stamps) !== JSON.stringify(state.stamps);
      const currentChanged = state.current !== null && stamps[key] !== state.stamps[key];
      // While a save runs, the deck is reloaded when it ends; keep the old stamp so the change is not lost.
      const deferred = currentChanged && state.busy;
      state.stamps = deferred ? { ...stamps, [key]: state.stamps[key] } : stamps;
      if (changed) await loadDecks();
      if (currentChanged && !deferred) await loadDeck();
      // `cmdr meta` fills the same cache; reread the open panel from it. A running sync rereads it at its end.
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
  $("status-close").addEventListener("click", hideStatus);
  try {
    const account = await loadAccount();
    if (account && !account.signedIn) {
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
