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
