"use strict";

// A save changes the deck on Archidekt; its confirmation stays long enough to be noticed.
const SAVED_MS = 12000;

/* Review and save */

function setupReview() {
  const dialog = $("review-dialog");
  $("review").addEventListener("click", openReview);
  $("review-cancel").addEventListener("click", () => dialog.close());
  $("review-save").addEventListener("click", saveChanges);
  dialog.addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
  dialog.addEventListener("close", () => $("review").focus());
}

function busy(text) {
  state.busy = Boolean(text);
  $("review-busy").hidden = !text;
  $("review-busy-text").textContent = text || "";
  $("review-save").disabled = state.busy;
  $("review-cancel").disabled = state.busy;
}

function showResolution(result) {
  $("review-size").textContent = result.before === result.after
    ? `${result.after} cards in the deck`
    : `${result.before} → ${result.after} cards in the deck`;
  $("review-changes").replaceChildren(...result.changes.map((c) => el("li", { class: c.mark === "+" ? "add" : "cut", text: c.text.replace(/^[+-] /, "") })));
  $("review-problems").replaceChildren(...result.problems.map((text) => el("li", { text })));
  const warnings = [...result.warnings, ...bracketWarnings(result.bracket)];
  $("review-warnings").replaceChildren(...warnings.map((text) => el("li", { text })));
  showReviewBracket(result.bracket);
}

// Spellbook's estimate for the deck once the changes are saved; absent when nothing counted changes.
function showReviewBracket(bracket) {
  const line = $("review-bracket");
  line.hidden = !bracket;
  if (!bracket) return;
  const now = bracket.before && bracket.before !== bracket.after ? `, now ${bracket.before}` : "";
  line.textContent = bracket.after
    ? `Bracket after saving: ${bracket.after} (${bracket.afterLabel})${now}`
    : "Bracket after saving: unknown, Commander Spellbook didn't answer";
  line.title = bracket.unavailable || "";
  line.classList.toggle("warn", bracket.check.notes.length > 0);
}

function bracketWarnings(bracket) {
  if (!bracket) return [];
  const causes = bracket.check.causes.map((c) => `${c.tag}: ${c.reason}: ${c.cards.join(" + ")}`);
  return [...bracket.check.notes, ...causes];
}

async function openReview() {
  const dialog = $("review-dialog");
  $("review-deck").textContent = state.view.short;
  showResolution({ before: 0, after: 0, changes: [], problems: [], warnings: [] });
  $("review-size").textContent = "";
  const save = $("review-save");
  save.textContent = "Save to Archidekt";
  dialog.showModal();
  busy("Checking the deck on Archidekt…");
  try {
    const result = await api("preview", state.current);
    showResolution(result);
    save.dataset.stamp = result.stamp;
    busy("");
    const count = result.changes.length;
    save.textContent = `Save ${count} change${count === 1 ? "" : "s"} to Archidekt`;
    save.disabled = count === 0 || result.problems.length > 0;
    (save.disabled ? $("review-cancel") : save).focus();
  } catch {
    busy("");
    save.disabled = true;
  }
}

async function saveChanges() {
  busy("Saving to Archidekt…");
  try {
    const result = await api("commit", state.current, $("review-save").dataset.stamp);
    busy("");
    if (!result.saved) {
      showResolution(result);
      $("review-save").disabled = true;
      return;
    }
    $("review-dialog").close();
    const count = result.changes.length;
    const saved = `Saved ${count} change${count === 1 ? "" : "s"} to Archidekt`;
    if (result.mismatches.length) showStatus(`${saved}, but Archidekt differs: ${result.mismatches.join("; ")}`, true);
    else showStatus(`${saved}. Restore point in Activity.`, false, SAVED_MS);
    await refresh();
  } catch {
    busy("");
  }
}
