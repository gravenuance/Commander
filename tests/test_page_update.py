"""The notice of a newer version (ADR 0059): shown only when one is out, with its notes and, in the exe, the
button that installs it. Its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const newer = { current: "1.3.1", latest: "v1.4.0",
  page: "https://github.com/gravenuance/Commander/releases/tag/v1.4.0", canInstall: true };
const notice = () => ({
  hidden: $("update").hidden, text: $("update-text").textContent, notes: $("update-notes").href,
  button: $("update-install").hidden, error: $("update-error").hidden ? null : $("update-error").textContent,
});
setupUpdate();
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_no_notice_when_the_app_is_current_or_the_check_failed() -> None:
    seen = run("""
    stub.update = async () => ({ current: "1.3.1", latest: null });
    await loadUpdate();
    const current = $("update").hidden;
    stub.update = async () => ({ error: "GitHub could not be reached" });
    await loadUpdate();
    return { current, failed: $("update").hidden };
    """)
    assert seen == {"current": True, "failed": True}


def test_a_newer_version_shows_its_number_its_notes_and_the_button() -> None:
    seen = run("""
    stub.update = async () => newer;
    await loadUpdate();
    return notice();
    """)
    assert seen == {
        "hidden": False,
        "text": "Version 1.4.0 is out.",
        "notes": "https://github.com/gravenuance/Commander/releases/tag/v1.4.0",
        "button": False,
        "error": None,
    }


def test_run_from_source_the_notice_has_no_button() -> None:
    seen = run("""
    stub.update = async () => ({ ...newer, canInstall: false });
    await loadUpdate();
    return notice();
    """)
    assert seen["hidden"] is False
    assert seen["button"] is True


def test_the_button_installs_and_a_failure_says_why_with_the_button_usable_again() -> None:
    seen = run("""
    stub.update = async () => newer;
    await loadUpdate();
    stub.install_update = async () => ({ error: "the downloaded Commander.exe does not match its checksum" });
    await fire($("update-install"), "click");
    await settle();
    const asked = calls.filter((c) => c[0] === "install_update").length;
    return { asked, disabled: $("update-install").disabled, ...notice() };
    """)
    assert seen["asked"] == 1
    assert seen["disabled"] is False
    assert seen["error"] == "the downloaded Commander.exe does not match its checksum"


def test_the_button_is_busy_while_the_update_runs_and_stays_so_once_it_is_in_place() -> None:
    seen = run("""
    stub.update = async () => newer;
    await loadUpdate();
    let finish;
    stub.install_update = () => new Promise((resolve) => { finish = resolve; });
    const button = $("update-install");
    const clicked = fire(button, "click");
    await settle();
    const during = { disabled: button.disabled, busy: button.getAttribute("aria-busy"),
      label: button.textContent };
    finish({ ok: true, restarted: true });
    await clicked;
    await settle();
    return { during, after: { disabled: button.disabled, label: button.textContent } };
    """)
    assert seen["during"] == {"disabled": True, "busy": "true", "label": "Updating…"}
    # The app closes now; until it does the button cannot start a second update.
    assert seen["after"] == {"disabled": True, "label": "Updating…"}
