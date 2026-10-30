"use strict";

/* A newer version of the app on GitHub: checked once at start, installed on a click (ADR 0059) */

function setupUpdate() {
  $("update-install").addEventListener("click", installUpdate);
}

// Shows the notice only when a newer version is out; a failed check shows nothing, as the log has it.
async function loadUpdate() {
  try {
    const answer = await window.pywebview.api.update();
    if (answer.error || !answer.latest) return;
    $("update-text").textContent = `Version ${answer.latest.replace(/^v/, "")} is out.`;
    $("update-notes").href = answer.page;
    $("update-install").hidden = !answer.canInstall;
    $("update").hidden = false;
  } catch { /* the notice stays hidden */ }
}

async function installUpdate() {
  setUpdateBusy(true);
  $("update-error").hidden = true;
  let done = false;
  try {
    const answer = await window.pywebview.api.install_update();
    // Once in place the app closes; the button stays busy until it does.
    done = !answer.error;
    if (answer.error) showUpdateError(answer.error);
  } catch {
    showUpdateError("The update failed; the details are in the log.");
  } finally {
    if (!done) setUpdateBusy(false);
  }
}

function setUpdateBusy(busy) {
  const button = $("update-install");
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.textContent = busy ? "Updating…" : "Update and restart";
}

function showUpdateError(message) {
  $("update-error").textContent = message;
  $("update-error").hidden = false;
}
