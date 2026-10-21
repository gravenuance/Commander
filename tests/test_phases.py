"""The phases of a sync that run at once, shown as one line."""

import threading

from commander.phases import ActivePhases

IDLE = "Compiling ideas"


def shown_by(phases_lines: list[str]) -> ActivePhases:
    return ActivePhases(phases_lines.append, idle=IDLE)


def test_two_phases_running_at_once_both_show_in_the_order_they_started() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start("Archidekt: recent Edgar Markov decks")
    phases.start("TopDeck: waiting 30 s (rate limit)")
    assert lines == [
        "Archidekt: recent Edgar Markov decks",
        "Archidekt: recent Edgar Markov decks · TopDeck: waiting 30 s (rate limit)",
    ]


def test_a_finished_phase_leaves_the_line_and_the_last_one_leaves_the_idle_text() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start("Archidekt: recent Edgar Markov decks")
    phases.start("TopDeck: recent tournaments")
    phases.done("Archidekt: recent Edgar Markov decks")
    assert lines[-1] == "TopDeck: recent tournaments"
    phases.done("TopDeck: recent tournaments")
    assert lines[-1] == IDLE


def test_a_phase_that_never_started_or_ended_twice_changes_nothing() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.done("Spellbook")
    phases.start("TopDeck: recent tournaments")
    phases.done("TopDeck: recent tournaments")
    phases.done("TopDeck: recent tournaments")
    assert lines == ["TopDeck: recent tournaments", IDLE]


def test_the_same_phase_twice_at_once_needs_both_to_finish() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start("Archidekt: recent Magda decks")
    phases.start("Archidekt: recent Magda decks")
    phases.done("Archidekt: recent Magda decks")
    assert lines[-1] == "Archidekt: recent Magda decks"
    phases.done("Archidekt: recent Magda decks")
    assert lines[-1] == IDLE


def test_more_than_twelve_words_show_the_first_phase_and_how_many_more() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start("Archidekt: recent Edgar Markov decks")
    phases.start("TopDeck: recent tournaments")
    # Twelve words exactly still fit.
    phases.start("Commander Spellbook: Edgar combos")
    assert lines[-1] == (
        "Archidekt: recent Edgar Markov decks · TopDeck: recent tournaments"
        " · Commander Spellbook: Edgar combos"
    )
    phases.start("Archidekt: recent decks tagged Aristocrats")
    assert lines[-1] == "Archidekt: recent Edgar Markov decks +3 more"


def test_replacing_a_phase_keeps_its_place_and_says_whether_it_was_there() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start("TopDeck: recent tournaments")
    phases.start("Archidekt: recent Edgar Markov decks")
    assert phases.replace("TopDeck: recent tournaments", "TopDeck: waiting 30 s (rate limit)")
    assert lines[-1] == "TopDeck: waiting 30 s (rate limit) · Archidekt: recent Edgar Markov decks"
    assert not phases.replace("Spellbook", "anything")
    assert lines[-1] == "TopDeck: waiting 30 s (rate limit) · Archidekt: recent Edgar Markov decks"


def test_phases_started_from_several_threads_are_all_kept() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    threads = [threading.Thread(target=phases.start, args=(f"Step {n}",)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for n in range(20):
        phases.done(f"Step {n}")
    assert lines[-1] == IDLE
    assert len(lines) == 40


LONG_STEP = "Archidekt: recent Kraum, Ludevic's Opus / Tymna the Weaver decks tagged Partner Tempo Value"


def test_one_phase_longer_than_twelve_words_is_cut_to_twelve() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start(LONG_STEP)
    assert lines == ["Archidekt: recent Kraum, Ludevic's Opus / Tymna the Weaver decks tagged Partner…"]


def test_a_long_first_phase_is_cut_so_the_count_of_the_others_still_fits_twelve_words() -> None:
    lines: list[str] = []
    phases = shown_by(lines)
    phases.start(LONG_STEP)
    phases.start("TopDeck: recent tournaments")
    assert lines[-1] == "Archidekt: recent Kraum, Ludevic's Opus / Tymna the Weaver decks… +1 more"
