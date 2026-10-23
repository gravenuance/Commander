"""The app page's save flow: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

WARNING = "could not read your card labels (HTTP 500); labelling in Archidekt's default colours"


def saved(mismatches: str = "[]", warnings: str = "[]") -> Answer:
    """The status line after a save the bridge answered with these mismatches and warnings."""
    return page(f"""
    state.current = null;
    stub.decks = async () => ({{ decks: [] }});
    stub.commit = async () => ({{ saved: true, changes: [1, 2], mismatches: {mismatches},
      warnings: {warnings}, applied: [] }});
    $("review-save").dataset.stamp = "stamp";
    await saveChanges();
    const status = $("status");
    return {{ text: $("status-text").textContent, error: status.classList.contains("error"),
      shown: !status.hidden, dialogError: !$("review-error").hidden }};
    """)


def test_a_clean_save_confirms_and_points_to_the_restore_point() -> None:
    assert saved() == {
        "text": "Saved 2 changes to Archidekt. Restore point in Activity.",
        "error": False,
        "shown": True,
        "dialogError": False,
    }


def test_a_save_whose_label_read_failed_keeps_the_warning_on_screen_until_dismissed() -> None:
    status = saved(warnings=f'["{WARNING}"]')

    assert status["text"] == f"Saved 2 changes to Archidekt, but {WARNING}"
    assert status["error"] is True


def test_a_save_that_differs_on_archidekt_says_how() -> None:
    status = saved(mismatches='["Arcane Signet is missing"]')

    assert status["text"] == "Saved 2 changes to Archidekt, but Archidekt differs: Arcane Signet is missing"
    assert status["error"] is True


def test_a_save_with_both_a_difference_and_a_warning_says_both() -> None:
    status = saved(mismatches='["Arcane Signet is missing"]', warnings=f'["{WARNING}"]')

    assert status["text"] == (
        f"Saved 2 changes to Archidekt, but Archidekt differs: Arcane Signet is missing; {WARNING}"
    )


def reviewed(answer: str) -> Answer:
    """The review dialog once the bridge answered the preview with this resolution."""
    return page(f"""
    state.current = 1;
    state.view = {{ short: "Kraum & Tymna" }};
    stub.preview = async () => ({{ deck: 1, problems: [], warnings: [], before: 100, after: 100,
      bracket: null, stamp: "s", {answer} }});
    await openReview();
    const empty = $("review-empty");
    return {{ empty: empty.hidden ? null : empty.textContent,
      notes: [...$("review-notes").children].map((li) => li.textContent),
      save: $("review-save").textContent, disabled: $("review-save").disabled }};
    """)


ALREADY = (
    "Arcane Signet: already in the deck on Archidekt, so its cut Disruptor Flute stays;"
    " saving marks it applied"
)
NOTHING = "Nothing to send to Archidekt: the deck there already has Arcane Signet."


def test_a_preview_with_nothing_to_send_says_why_and_offers_to_mark_the_card_applied() -> None:
    answer = reviewed(
        f'changes: [], skipped: ["{ALREADY}"], done: ["Arcane Signet"], nothingToSend: "{NOTHING}"'
    )

    assert answer == {
        "empty": NOTHING,
        "notes": [ALREADY],
        "save": "Mark 1 card applied",
        "disabled": False,
    }


def test_a_preview_with_nothing_to_send_and_nothing_done_says_why_and_offers_no_save() -> None:
    reason = "Nothing to send to Archidekt: remove Mountain: not in the deck"
    answer = reviewed(
        f'changes: [], skipped: ["remove Mountain: not in the deck"], done: [], nothingToSend: "{reason}"'
    )

    assert (answer["empty"], answer["disabled"]) == (reason, True)


def test_a_preview_with_changes_shows_no_empty_reason() -> None:
    answer = reviewed(
        'changes: [{ kind: "add", mark: "+", name: "Arcane Signet", text: "+ Arcane Signet" }],'
        " skipped: [], done: [], nothingToSend: null"
    )

    assert answer == {"empty": None, "notes": [], "save": "Save 1 change to Archidekt", "disabled": False}


def test_marking_cards_applied_closes_the_review_and_says_nothing_was_sent() -> None:
    answer = page("""
    state.current = null;
    stub.decks = async () => ({ decks: [] });
    stub.commit = async () => ({ saved: false, changes: [], problems: [], warnings: [], skipped: [],
      mismatches: [], applied: ["Arcane Signet"], before: 100, after: 100 });
    $("review-dialog").showModal();
    $("review-save").dataset.stamp = "stamp";
    await saveChanges();
    return { text: $("status-text").textContent, open: $("review-dialog").open };
    """)

    assert answer == {
        "text": "Marked Arcane Signet applied: the deck on Archidekt already had it, so nothing was sent.",
        "open": False,
    }


def test_a_save_that_empties_the_queue_does_not_leave_focus_on_the_disabled_review_button() -> None:
    seen = page("""
    const view = (pending) => ({
      id: 1, short: "D", name: "Deck", url: "https://archidekt.com/decks/1", bracket: null,
      effectiveBracket: null, archetype: "", tags: [], leanings: [], pending, stats: null, grid: [],
      cards: [], activity: [], stamp: "s" });
    state.current = 1;
    state.view = view(2);
    renderDeck();
    $("review").focus();
    state.view = view(0);
    renderDeck();
    return { focus: document.activeElement.id, disabled: $("review").disabled };
    """)
    assert seen == {"focus": "main", "disabled": True}
