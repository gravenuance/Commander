import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from commander import __version__
from commander.cli import main
from commander.credentials import SERVICE
from conftest import CARDS_URL, REFRESH_TOKEN, FakeArchidekt, Json, MemoryKeyring, card_entry

NO_CARDS = "card data: skipped, --no-cards\n"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LABELS_NONE = "card labels: none (from your Archidekt account)\n"


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err


@pytest.fixture
def archidekt(raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    return fake


def run(
    args: list[str],
    tmp_path: Path,
    fake: FakeArchidekt | None = None,
    *,
    cache: Path | None = None,
    now: datetime = NOW,
) -> Run:
    out, err = io.StringIO(), io.StringIO()
    env = {"APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "localappdata")}
    if cache is not None:
        env["CMDR_CACHE_DIR"] = str(cache)
    factory = (fake or FakeArchidekt()).client
    code = main(args, env=env, client_factory=factory, stdout=out, stderr=err, now=lambda: now)
    return Run(code, out.getvalue(), err.getvalue())


def sync(tmp_path: Path, fake: FakeArchidekt, *extra: str, cards: bool = False) -> Run:
    """Deck sync; card data stays out of it unless a test asks, so deck tests read only deck output."""
    flags = [] if cards else ["--no-cards"]
    args = ["sync", "--user", "MAKKIS", "--workspace", str(tmp_path / "decks"), *flags, *extra]
    return run(args, tmp_path, fake)


def test_no_arguments_prints_usage_and_exits_2(tmp_path: Path) -> None:
    result = run([], tmp_path)
    assert result.code == 2
    assert result.err.startswith("usage: cmdr")


def test_version(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--version"], tmp_path).code == 0
    assert capsys.readouterr().out == f"cmdr {__version__}\n"


def test_missing_user_is_a_usage_error(tmp_path: Path) -> None:
    result = run(["sync", "--workspace", str(tmp_path)], tmp_path)
    assert result.code == 2
    assert "pass --user" in result.err


def test_first_sync_saves_new_deck(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    result = sync(tmp_path, archidekt)
    assert result.code == 0
    assert result.out == "new        10004639  Honestly, though..\n1 new\n" + NO_CARDS
    assert (tmp_path / "decks" / "10004639-honestly-though" / "deck.json").is_file()


def test_unchanged_deck_is_not_refetched(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    archidekt.requests.clear()
    result = sync(tmp_path, archidekt)
    assert result.out == "1 unchanged\n" + NO_CARDS
    assert archidekt.requests == ["/api/decks/v3/"]


def test_a_deck_saved_before_deck_tags_is_refetched_once(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    snapshot = tmp_path / "decks" / "10004639-honestly-though" / "deck.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["version"] = 1
    del document["tags"]
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    archidekt.requests.clear()
    sync(tmp_path, archidekt)
    assert archidekt.requests == ["/api/decks/v3/", "/api/decks/10004639/"]
    assert json.loads(snapshot.read_text(encoding="utf-8"))["tags"] == ["Dwarves", "Vehicles", "cEDH"]
    archidekt.requests.clear()
    assert sync(tmp_path, archidekt).out == "1 unchanged\n" + NO_CARDS
    assert archidekt.requests == ["/api/decks/v3/"]


def test_a_deck_saved_before_card_labels_is_refetched_once(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    snapshot = tmp_path / "decks" / "10004639-honestly-though" / "deck.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["version"] = 2
    for card in document["cards"]:
        del card["label"]
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    archidekt.requests.clear()
    sync(tmp_path, archidekt)
    assert archidekt.requests == ["/api/decks/v3/", "/api/decks/10004639/"]
    assert {c["label"] for c in json.loads(snapshot.read_text(encoding="utf-8"))["cards"]} == {""}
    archidekt.requests.clear()
    assert sync(tmp_path, archidekt).out == "1 unchanged\n" + NO_CARDS
    assert archidekt.requests == ["/api/decks/v3/"]


def test_a_deck_whose_tags_alone_changed_is_refetched(
    tmp_path: Path, archidekt: FakeArchidekt, raw_deck: Json
) -> None:
    # Archidekt keeps a deck's edit time when only its tags change; the deck list shows the new tags.
    sync(tmp_path, archidekt)
    raw_deck["deckTags"] = [{"id": 9, "tag": 69, "deck": 10004639, "name": "Stax", "position": "M-1"}]
    archidekt.put(raw_deck)
    archidekt.requests.clear()
    sync(tmp_path, archidekt)
    assert archidekt.requests == ["/api/decks/v3/", "/api/decks/10004639/"]
    snapshot = tmp_path / "decks" / "10004639-honestly-though" / "deck.json"
    assert json.loads(snapshot.read_text(encoding="utf-8"))["tags"] == ["Stax"]


def test_force_refetches(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    archidekt.requests.clear()
    sync(tmp_path, archidekt, "--force")
    assert archidekt.requests == ["/api/decks/v3/", "/api/decks/10004639/"]


def test_edited_deck_reports_its_changes(tmp_path: Path, archidekt: FakeArchidekt, raw_deck: Json) -> None:
    sync(tmp_path, archidekt)
    raw_deck["updatedAt"] = "2026-10-14T10:00:00Z"
    raw_deck["cards"] = [c for c in raw_deck["cards"] if c["card"]["oracleCard"]["name"] != "Disruptor Flute"]
    raw_deck["cards"].append(card_entry(1, "Ancient Tomb", ["Land"]))
    archidekt.put(raw_deck)
    result = sync(tmp_path, archidekt)
    assert result.code == 0
    assert result.out == (
        "updated    10004639  Honestly, though..\n    + 1 Ancient Tomb\n    - 1 Disruptor Flute\n1 updated\n"
        + NO_CARDS
    )
    saved = json.loads((tmp_path / "decks/10004639-honestly-though/deck.json").read_text(encoding="utf-8"))
    assert saved["updatedAt"] == "2026-10-14T10:00:00Z"


def test_dry_run_writes_nothing(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    result = sync(tmp_path, archidekt, "--dry-run", cards=True)
    assert result.out == (
        "new        10004639  Honestly, though..\n"
        "1 new (dry run, nothing written)\n"
        "card data: skipped, dry run\n"
    )
    assert archidekt.scryfall.requests == []
    assert not (tmp_path / "decks").exists()


def test_deck_gone_from_archidekt_is_kept_and_reported(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    archidekt.decks.clear()
    result = sync(tmp_path, archidekt)
    assert result.out == "missing    10004639  Honestly, though..\n1 missing\n" + NO_CARDS
    assert (tmp_path / "decks" / "10004639-honestly-though" / "deck.json").is_file()


def test_one_failing_deck_does_not_stop_the_others(
    tmp_path: Path, archidekt: FakeArchidekt, raw_deck: Json
) -> None:
    other = {**raw_deck, "id": 222, "name": "Other"}
    archidekt.put(other)
    archidekt.failing[10004639] = 404
    result = sync(tmp_path, archidekt)
    assert result.code == 1
    assert result.out == (
        "new             222  Other\n"
        "failed     10004639  Honestly, though..\n"
        "    https://archidekt.com/api/decks/10004639/ returned HTTP 404\n"
        "1 new, 1 failed\n" + NO_CARDS
    )
    assert (tmp_path / "decks" / "222-other").is_dir()


def deleted_on_archidekt(tmp_path: Path, archidekt: FakeArchidekt, keyring: MemoryKeyring) -> None:
    """Two decks synced while signed in, then one of them deleted on Archidekt."""
    keyring.secrets[(SERVICE, "makkis")] = REFRESH_TOKEN
    archidekt.put({**archidekt.decks[10004639], "id": 222, "name": "Other"})
    sync(tmp_path, archidekt)
    del archidekt.decks[222]


def test_sync_lists_a_deck_deleted_on_archidekt_as_removed_and_deletes_its_folder(
    tmp_path: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    deleted_on_archidekt(tmp_path, archidekt, memory_keyring)

    result = sync(tmp_path, archidekt)

    assert result.out == "removed         222  Other\n1 unchanged, 1 removed\n" + NO_CARDS + LABELS_NONE
    assert not (tmp_path / "decks" / "222-other").exists()


def test_sync_dry_run_says_what_it_would_remove(
    tmp_path: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    deleted_on_archidekt(tmp_path, archidekt, memory_keyring)

    result = sync(tmp_path, archidekt, "--dry-run")

    assert result.out == (
        "would remove       222  Other\n1 unchanged, 1 would remove (dry run, nothing written)\n"
        "card data: skipped, dry run\n"
    )
    assert (tmp_path / "decks" / "222-other" / "deck.json").is_file()


def test_sync_json_gives_the_removed_status(
    tmp_path: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    deleted_on_archidekt(tmp_path, archidekt, memory_keyring)

    rows = json.loads(sync(tmp_path, archidekt, "--json").out)["decks"]

    assert {row["id"]: row["status"] for row in rows} == {10004639: "unchanged", 222: "removed"}


def test_sync_json_output(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    document = json.loads(sync(tmp_path, archidekt, "--json").out)
    assert document["cardData"] == {"status": "skipped", "detail": "--no-cards"}
    rows = document["decks"]
    assert len(rows) == 1
    assert rows[0]["id"] == 10004639
    assert rows[0]["status"] == "new"
    assert {"name": "Mountain", "quantity": 6} in rows[0]["added"]


def test_list_after_sync(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    result = run(["list", "--workspace", str(tmp_path / "decks")], tmp_path)
    assert result.code == 0
    assert result.out == "10004639\tB5\tHonestly, though..\tMagda, Brazen Outlaw\n"


def test_list_json(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    rows = json.loads(run(["list", "--json", "--workspace", str(tmp_path / "decks")], tmp_path).out)
    assert rows[0]["commanders"] == ["Magda, Brazen Outlaw"]
    assert rows[0]["cards"] == 9


def test_init_then_sync_uses_saved_settings(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    decks = tmp_path / "decks"
    assert run(["init", "--user", "MAKKIS", "--workspace", str(decks)], tmp_path).code == 0
    result = run(["sync", "--no-cards"], tmp_path, archidekt)
    assert result.code == 0
    assert (decks / "10004639-honestly-though").is_dir()


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    args = ["init", "--user", "MAKKIS", "--workspace", str(tmp_path)]
    run(args, tmp_path)
    second = run(args, tmp_path)
    assert second.code == 2
    assert "pass --force" in second.err
    assert run([*args, "--force"], tmp_path).code == 0


def test_maybeboard_only_edit_is_unchanged_but_saved(
    tmp_path: Path, archidekt: FakeArchidekt, raw_deck: Json
) -> None:
    sync(tmp_path, archidekt)
    raw_deck["updatedAt"] = "2026-10-14T10:00:00Z"
    raw_deck["cards"].append(card_entry(1, "Sol Ring", ["Maybeboard"]))
    archidekt.put(raw_deck)
    result = sync(tmp_path, archidekt)
    assert result.out == "1 unchanged\n" + NO_CARDS
    listing = (tmp_path / "decks/10004639-honestly-though/list.md").read_text(encoding="utf-8")
    assert "- 1 Sol Ring" in listing


def test_sync_downloads_card_data_then_reports_it_current(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    first = sync(tmp_path, archidekt, cards=True)
    assert first.code == 0
    assert first.out.endswith("card data: updated, Scryfall data of 2026-10-15, 10,028 printings\n")
    assert (tmp_path / "localappdata" / "Commander" / "cards.sqlite").is_file()
    second = sync(tmp_path, archidekt, cards=True)
    assert second.out == "1 unchanged\ncard data: current, Scryfall data of 2026-10-15\n"


def test_failed_card_refresh_fails_the_run_but_keeps_the_decks(
    tmp_path: Path, archidekt: FakeArchidekt
) -> None:
    archidekt.scryfall.failing[CARDS_URL] = 404
    result = sync(tmp_path, archidekt, cards=True)
    assert result.code == 1
    assert result.out.endswith(
        f"card data: failed, {CARDS_URL} returned HTTP 404; the previous card data is kept\n"
    )
    assert (tmp_path / "decks" / "10004639-honestly-though" / "deck.json").is_file()


def test_sync_json_reports_card_data(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    document = json.loads(sync(tmp_path, archidekt, "--json", cards=True).out)
    assert document["cardData"] == {
        "status": "updated",
        "detail": "Scryfall data of 2026-10-15, 10,028 printings",
    }


@pytest.fixture
def decks(tmp_path: Path, archidekt: FakeArchidekt) -> Path:
    sync(tmp_path, archidekt)
    return tmp_path / "decks"


def research(args: list[str], tmp_path: Path, decks: Path, cache: Path, now: datetime = NOW) -> Run:
    return run([*args, "--workspace", str(decks)], tmp_path, cache=cache, now=now)


ANALYSIS = """\
Honestly, though.. (10004639)
Magda, Brazen Outlaw; bracket 5 on Archidekt
9 cards, 6 lands, average mana value 2.33 (nonland)

mana curve (nonland)
   0    0
   1    0
   2    2  ##
   3    1  #
   4    0
   5    0
   6    0
  7+    0

types: Creature 2, Artifact 2, Land 6
pips: R 1

  role         yours  tags
  ramp             1     1
  draw             -     0
  removal          -     1
  board wipe       -     1
  tutor            -     1
  protection       -     0
  recursion        -     0
  counterspell     -     0

game changers: none

"""


def test_analyze_one_deck(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    result = research(["analyze", "magda"], tmp_path, decks, card_db_file.parent)
    assert (result.code, result.err) == (0, "")
    assert result.out == ANALYSIS


def test_analyze_details_lists_role_differences(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    result = research(["analyze", "10004639", "--details"], tmp_path, decks, card_db_file.parent)
    expected = ANALYSIS.replace(
        "  ramp             1     1\n",
        "  ramp             1     1\n"
        "      only yours: Battered Golem\n"
        "      only tags:  Magda, Brazen Outlaw\n",
    )
    assert result.out == expected


def test_analyze_all_decks_is_a_table(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    result = research(["analyze"], tmp_path, decks, card_db_file.parent)
    header, row, note = result.out.splitlines()
    assert header.split() == [
        "id",
        "lands",
        "avg",
        "mv",
        "ramp",
        "draw",
        "removal",
        "wipes",
        "tutors",
        "protect",
        "recursion",
        "counters",
        "gc",
        "name",
    ]
    assert row.split() == [
        "10004639",
        "6",
        "2.33",
        "1",
        "0",
        "1",
        "1",
        "1",
        "0",
        "0",
        "0",
        "0",
        "Honestly,",
        "though..",
    ]
    assert note.startswith("role counts come from Scryfall tags")


def test_analyze_json(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    [deck] = json.loads(research(["analyze", "--json"], tmp_path, decks, card_db_file.parent).out)
    assert deck["curve"] == [0, 0, 2, 1, 0, 0, 0, 0]
    assert deck["roles"][0] == {
        "role": "ramp",
        "yours": 1,
        "tagged": 1,
        "onlyYours": ["Battered Golem"],
        "onlyTagged": ["Magda, Brazen Outlaw"],
    }


def test_price_one_deck(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    result = research(["price", "honestly"], tmp_path, decks, card_db_file.parent)
    assert result.code == 0
    assert result.out == (
        "Cardmarket prices in EUR, from Scryfall data of 2026-10-15\n"
        "\n"
        "Honestly, though.. (10004639)\n"
        "value €10.87; with the cheapest printings €9.73\n"
        "       €7.63   1x Magda, Brazen Outlaw\n"
        "       €2.17   1x Disruptor Flute\n"
        "       €0.72   6x Mountain\n"
        "       €0.35   1x Battered Golem\n"
    )


def test_price_top_limits_the_card_list(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    result = research(["price", "honestly", "--top", "2"], tmp_path, decks, card_db_file.parent)
    assert result.out.endswith("       €2.17   1x Disruptor Flute\n  ... 2 more; --top 0 lists them all\n")


def test_price_all_decks_is_a_table(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    result = research(["price"], tmp_path, decks, card_db_file.parent)
    assert result.out == (
        "Cardmarket prices in EUR, from Scryfall data of 2026-10-15\n"
        "      value    cheapest  deck\n"
        "     €10.87       €9.73  Honestly, though.. (10004639)\n"
        "     €10.87       €9.73  total\n"
    )


def test_price_json_uses_cents(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    [deck] = json.loads(research(["price", "--json"], tmp_path, decks, card_db_file.parent).out)
    assert (deck["totalCents"], deck["cheapestTotalCents"]) == (1087, 973)
    assert deck["cards"][0] == {
        "name": "Magda, Brazen Outlaw",
        "quantity": 1,
        "unitCents": 763,
        "totalCents": 763,
        "cheapestCents": 679,
        "source": "printing",
    }


def test_old_card_data_gets_a_warning(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    later = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    result = research(["price"], tmp_path, decks, card_db_file.parent, now=later)
    assert result.code == 0
    assert result.err == "cmdr: card data is from 2026-10-15; run `cmdr sync` for fresh prices\n"
    assert research(["price"], tmp_path, decks, card_db_file.parent).err == ""


def test_research_without_card_data_says_to_sync(tmp_path: Path, decks: Path) -> None:
    result = research(["analyze"], tmp_path, decks, tmp_path / "empty-cache")
    assert result.code == 1
    assert "run `cmdr sync` to download it" in result.err


@pytest.mark.parametrize(
    ("query", "message"),
    [("zzz", "no deck matches 'zzz'"), ("", "the deck to look up is empty")],
)
def test_unknown_deck_is_a_usage_error(
    tmp_path: Path, decks: Path, card_db_file: Path, query: str, message: str
) -> None:
    result = research(["analyze", query], tmp_path, decks, card_db_file.parent)
    assert result.code == 2
    assert message in result.err


def test_ambiguous_deck_lists_the_matches(
    tmp_path: Path, archidekt: FakeArchidekt, raw_deck: Json, card_db_file: Path
) -> None:
    archidekt.put({**raw_deck, "id": 222, "name": "Honestly Again"})
    sync(tmp_path, archidekt)
    result = research(["price", "honestly"], tmp_path, tmp_path / "decks", card_db_file.parent)
    assert result.code == 2
    assert "matches 2 decks (10004639 Honestly, though..; 222 Honestly Again); use the deck id" in result.err


def test_empty_workspace_says_to_sync(tmp_path: Path, card_db_file: Path) -> None:
    result = research(["price"], tmp_path, tmp_path / "nothing", card_db_file.parent)
    assert result.code == 2
    assert "run `cmdr sync` first" in result.err


def test_negative_top_is_a_usage_error(tmp_path: Path, decks: Path, card_db_file: Path) -> None:
    assert research(["price", "--top", "-1"], tmp_path, decks, card_db_file.parent).code == 2


def bracket(args: list[str], tmp_path: Path, fake: FakeArchidekt, cache: Path, now: datetime = NOW) -> Run:
    return run(
        ["bracket", *args, "--workspace", str(tmp_path / "decks")], tmp_path, fake, cache=cache, now=now
    )


BRACKET = """\
Honestly, though.. (10004639)
Magda, Brazen Outlaw; bracket 5 on Archidekt
estimate: bracket 4+ (Ruthless), Commander Spellbook 2026-10-15
game changers: none
tutors (1): Magda, Brazen Outlaw
mass land denial: unknown (not in the tag data)
extra turns: none
(tutors, mass land denial and extra turns come from Scryfall tags)

combos (3)
  Spicy      2-card    Magda, Brazen Outlaw + Clock of Omens + Firdoch Core
  Exhibition 2-card?   Ashling, the Limitless + Greenwarden of Murasa + Temur Battlecrier
  Exhibition 3-card    Magda, Brazen Outlaw + Battered Golem + Stalactite Dagger
? Commander Spellbook counts it as arguably two cards

bracket estimates and combos from Commander Spellbook, https://commanderspellbook.com
"""


@pytest.mark.usefixtures("decks")
def test_bracket_one_deck(tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path) -> None:
    result = bracket(["magda"], tmp_path, archidekt, own_cache)
    assert (result.code, result.err) == (0, "")
    assert result.out == BRACKET
    assert archidekt.spellbook.bodies[0]["commanders"] == [{"card": "Magda, Brazen Outlaw", "quantity": 1}]


@pytest.mark.usefixtures("decks")
def test_bracket_details_show_results_and_links(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    result = bracket(["magda", "--details"], tmp_path, archidekt, own_cache)
    lines = result.out.splitlines()
    spicy = lines.index("  Spicy      2-card    Magda, Brazen Outlaw + Clock of Omens + Firdoch Core")
    assert lines[spicy + 1].strip().startswith("-> Infinite artifact ETB, Infinite artifact tokens")
    assert lines[spicy + 2].strip() == "https://commanderspellbook.com/combo/1486-1651-7298/"


@pytest.mark.usefixtures("decks")
def test_bracket_answer_is_reused_until_refresh(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    bracket(["magda"], tmp_path, archidekt, own_cache)
    assert bracket(["magda"], tmp_path, archidekt, own_cache).out == BRACKET
    assert len(archidekt.spellbook.bodies) == 1
    bracket(["magda", "--refresh"], tmp_path, archidekt, own_cache)
    assert len(archidekt.spellbook.bodies) == 2


def test_bracket_table_flags_a_deck_set_below_the_estimate(
    tmp_path: Path, raw_deck: Json, own_cache: Path
) -> None:
    raw_deck["edhBracket"] = 3
    fake = FakeArchidekt()
    fake.put(raw_deck)
    sync(tmp_path, fake)
    result = bracket([], tmp_path, fake, own_cache)
    header, row, *notes = result.out.splitlines()
    assert header.split() == [
        "id",
        "set",
        "estimate",
        "gc",
        "tutors",
        "mld",
        "turns",
        "2-card",
        "combos",
        "name",
    ]
    assert row.split() == [
        "10004639",
        "3",
        "4+",
        "Ruthless",
        "0",
        "1",
        "-",
        "0",
        "1",
        "3",
        "!",
        "Honestly,",
        "though..",
    ]
    assert notes == [
        (
            "! the Archidekt bracket, the profile's ceiling and the estimate disagree; "
            "`cmdr bracket <deck>` says why"
        ),
        "set = bracket on Archidekt; tutors, mld and turns come from Scryfall tags",
        "bracket estimates and combos from Commander Spellbook, https://commanderspellbook.com",
    ]


def test_bracket_names_what_lifts_the_estimate_above_the_set_bracket(
    tmp_path: Path, raw_deck: Json, own_cache: Path
) -> None:
    raw_deck["edhBracket"] = 2
    fake = FakeArchidekt()
    fake.put(raw_deck)
    sync(tmp_path, fake)
    lines = bracket(["magda"], tmp_path, fake, own_cache).out.splitlines()
    start = lines.index("  ! the estimate (4+) is above the Archidekt bracket (2)")
    # Set to 2, the Ruthless and Spicy combos and the Powerful game changer all lift it; ordered by tag.
    assert lines[start + 1 : start + 5] == [
        "  because of:",
        (
            "    Ruthless   a fast two-card combo that wins: "
            "Magda, Brazen Outlaw + Clock of Omens + Firdoch Core"
        ),
        (
            "    Spicy      a fast combo of about two cards: "
            "Ashling, the Limitless + Greenwarden of Murasa + Temur Battlecrier"
        ),
        "    Powerful   a game changer: Ancient Tomb",
    ]


@pytest.mark.usefixtures("decks")
def test_bracket_json(tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path) -> None:
    [deck] = json.loads(bracket(["--json"], tmp_path, archidekt, own_cache).out)
    assert deck["bracket"] == 5
    assert deck["estimate"] == {
        "source": "fetched",
        "fetchedAt": "2026-10-15T12:00:00+00:00",
        "error": None,
        "tag": "Ruthless",
        "minimumBracket": 4,
        "brackets": "4+",
    }
    assert deck["setBelowEstimate"] is False
    # Bracket 5 fits Ruthless, and the fixture deck has no profile.
    assert (deck["ceiling"], deck["check"]) == (None, {"notes": [], "causes": []})
    assert (deck["tutors"], deck["massLandDenial"], deck["extraTurns"]) == (
        ["Magda, Brazen Outlaw"],
        None,
        [],
    )
    assert [c["twoCard"] for c in deck["combos"]] == ["yes", "arguably", "no"]
    assert deck["combos"][0]["url"] == "https://commanderspellbook.com/combo/1486-1651-7298/"


@pytest.mark.usefixtures("decks")
def test_bracket_without_spellbook_still_lists_the_cards_and_exits_1(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    archidekt.spellbook.status = 404
    result = bracket(["magda"], tmp_path, archidekt, own_cache)
    assert result.code == 1
    assert result.err.startswith("cmdr: Honestly, though..: no bracket estimate: ")
    assert "HTTP 404" in result.err
    assert "estimate: unavailable\n" in result.out
    assert "tutors (1): Magda, Brazen Outlaw\n" in result.out
    assert "\ncombos (" not in result.out


@pytest.mark.usefixtures("decks")
def test_bracket_falls_back_to_an_older_answer_with_a_warning(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    bracket(["magda"], tmp_path, archidekt, own_cache)
    archidekt.spellbook.status = 404
    later = NOW + timedelta(days=8)
    result = bracket(["magda"], tmp_path, archidekt, own_cache, now=later)
    assert result.code == 0
    assert (
        "cmdr: Honestly, though..: showing the bracket estimate from 2026-10-15; refreshing it failed: "
        in result.err
    )
    assert "estimate: bracket 4+ (Ruthless), Commander Spellbook 2026-10-15\n" in result.out


@pytest.mark.usefixtures("decks")
def test_bracket_needs_card_data(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    result = bracket([], tmp_path, archidekt, tmp_path / "empty-cache")
    assert result.code == 1
    assert "run `cmdr sync`" in result.err
    assert archidekt.spellbook.bodies == []


def suggest(args: list[str], tmp_path: Path, fake: FakeArchidekt, cache: Path, now: datetime = NOW) -> Run:
    return run(
        ["suggest", *args, "--workspace", str(tmp_path / "decks")], tmp_path, fake, cache=cache, now=now
    )


def synced(tmp_path: Path, raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    sync(tmp_path, fake)
    return fake


SUGGEST_CREDIT = (
    "combos from Commander Spellbook, https://commanderspellbook.com; "
    "popularity ranks from EDHREC, https://edhrec.com, via Scryfall\n"
)
SUGGEST = (
    """\
prices: each card's cheapest printing in EUR, Scryfall data of 2026-10-15

Honestly, though.. (10004639)
Magda, Brazen Outlaw; bracket 5 on Archidekt; colours R

cards that complete a combo (Commander Spellbook 2026-10-15)
     €16.93  Aggravated Assault (1 combo: Spicy)
          -  Maskwood Nexus (1 combo: Exhibition)
             also needs: Creature with Saddle 4 or less
      €6.68  Splinter Twin (1 combo: Spicy)

roles below target (nonland cards by Scryfall tags or your categories; most played ideas first)
  ramp 2 of 10
      €0.30  Arcane Signet
          -  Mind Stone
     €33.80  Jeska's Will
  draw 0 of 10
             no card fits
  removal 1 of 8
          -  Chaos Warp
      €1.98  Lightning Bolt
  board wipe 1 of 2
             no card fits

cards that fit the deck's themes (most played first)
      €0.14  High-Speed Hoverbike (fits Vehicles)

cards to consider cutting: none, every nonland card has a role or is in a combo
"""
    + SUGGEST_CREDIT
)


@pytest.mark.usefixtures("decks")
def test_suggest_one_deck(tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path) -> None:
    result = suggest(["magda"], tmp_path, archidekt, own_cache)
    assert (result.code, result.err) == (0, "")
    assert result.out == SUGGEST
    assert archidekt.spellbook.combo_bodies[0]["commanders"] == [
        {"card": "Magda, Brazen Outlaw", "quantity": 1}
    ]


@pytest.mark.usefixtures("decks")
def test_suggest_details_show_each_combo(tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path) -> None:
    lines = suggest(["magda", "-d"], tmp_path, archidekt, own_cache).out.splitlines()
    twin = lines.index("      €6.68  Splinter Twin (1 combo: Spicy)")
    assert lines[twin + 1].strip() == "Spicy: with Battered Golem -> Infinite creature tokens with haste"
    assert lines[twin + 2].strip() == "https://commanderspellbook.com/combo/3459-4702/"


def test_suggest_marks_ideas_that_raise_the_bracket(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    raw_deck["edhBracket"] = 2
    out = suggest(["magda", "--any-price"], tmp_path, synced(tmp_path, raw_deck), own_cache).out
    assert "     €16.93  Aggravated Assault (1 combo: Spicy)  ! bracket 3+ (Spicy combo)\n" in out
    assert "     €33.80  Jeska's Will  ! bracket 3+ (game changer)\n" in out
    assert "! adding it lifts the deck to at least that bracket; --within-bracket leaves these out\n" in out


def test_suggest_keeps_to_the_decks_budget_below_bracket_5(
    tmp_path: Path, raw_deck: Json, own_cache: Path
) -> None:
    raw_deck["edhBracket"] = 3
    out = suggest(["magda"], tmp_path, synced(tmp_path, raw_deck), own_cache).out
    # Magda 6.79, Disruptor Flute 2.17 and Battered Golem 0.35 average 3.10, so within budget runs to
    # 3.10 x 1.25 = 3.88; 25% over Magda is 8.49.
    assert "budget: average card €3.10, within budget up to €3.88, nothing over €8.49\n" in out
    # Splinter Twin (6.68) stays, marked; Aggravated Assault (16.93) and Jeska's Will (33.80) go.
    assert "      €6.68  Splinter Twin (1 combo: Spicy) (over budget)\n" in out
    assert "Aggravated Assault" not in out
    assert "Jeska's Will" not in out


def test_suggest_within_bracket_hides_them(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    raw_deck["edhBracket"] = 2
    out = suggest(
        ["magda", "--within-bracket", "--any-price"], tmp_path, synced(tmp_path, raw_deck), own_cache
    ).out
    assert "Aggravated Assault" not in out
    assert "Jeska's Will" not in out
    assert "3 left out because they would lift the deck above bracket 2\n" in out
    assert "!" not in out


@pytest.mark.parametrize(
    ("price", "shown"), [("10", ["Splinter Twin"]), ("6,68", ["Splinter Twin"]), ("6.67", []), ("6.679", [])]
)
@pytest.mark.usefixtures("decks")
def test_suggest_max_price(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path, price: str, shown: list[str]
) -> None:
    [deck] = json.loads(suggest(["--json", "--max-price", price], tmp_path, archidekt, own_cache).out)
    assert [p["name"] for p in deck["comboPieces"]] == shown


@pytest.mark.parametrize(
    "args", [["--max-price", "abc"], ["--max-price", "-1"], ["--max-price", "nan"], ["-n", "0"]]
)
@pytest.mark.usefixtures("decks")
def test_suggest_bad_options_are_usage_errors(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path, args: list[str]
) -> None:
    assert suggest(args, tmp_path, archidekt, own_cache).code == 2
    assert archidekt.spellbook.combo_bodies == []


@pytest.mark.usefixtures("decks")
def test_suggest_count_and_more(tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path) -> None:
    out = suggest(["magda", "-n", "1"], tmp_path, archidekt, own_cache).out
    assert "  ... 2 more; --count lists more\n" in out
    assert "Mind Stone" not in out


def test_suggest_lists_cut_candidates(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    for card in raw_deck["cards"]:
        if card["categories"] == ["Maybeboard"]:
            card["categories"] = ["Creature"]
    out = suggest(["magda"], tmp_path, synced(tmp_path, raw_deck), own_cache).out
    assert (
        "\ncards to consider cutting (no role, in no combo; least played first, by EDHREC rank)\n"
        "    #11,271  Tempestra, Dame of Games\n"
        "     #9,733  High-Speed Hoverbike\n"
    ) in out


def test_suggest_targets_come_from_the_config_file(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    sync(tmp_path, archidekt)
    config = tmp_path / "appdata" / "Commander" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("version = 1\n\n[targets]\nramp = 1\nremoval = 1\n", encoding="utf-8")
    out = suggest(["magda"], tmp_path, archidekt, own_cache).out
    assert "  ramp 1 of" not in out
    assert "  at target: ramp 2/1, removal 1/1\n" in out


def test_suggest_table(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    raw_deck["edhBracket"] = 3
    result = suggest(["--any-price"], tmp_path, synced(tmp_path, raw_deck), own_cache)
    header, row, *notes = result.out.splitlines()
    assert header.split() == ["id", "set", "combo", "ramp", "draw", "removal", "wipes", "cuts", "name"]
    assert row.split() == ["10004639", "3", "3", "2/10", "0/10", "1/8", "1/2", "0", "Honestly,", "though.."]
    assert notes == [
        "combo = cards that would complete a combo; roles = nonland cards tagged / target",
        "`cmdr suggest <deck>` lists the cards",
        SUGGEST_CREDIT.rstrip("\n"),
    ]


def test_suggest_json(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    raw_deck["edhBracket"] = 2
    [deck] = json.loads(
        suggest(["--json", "--any-price"], tmp_path, synced(tmp_path, raw_deck), own_cache).out
    )
    assert (deck["bracket"], deck["colorIdentity"], deck["hidden"], deck["cuts"]) == (2, "R", 0, [])
    assert deck["combos"] == {"source": "fetched", "fetchedAt": "2026-10-15T12:00:00+00:00", "error": None}
    assault = deck["comboPieces"][0]
    assert (assault["name"], assault["priceCents"], assault["raises"]) == (
        "Aggravated Assault",
        1693,
        {"bracket": 3, "reason": "Spicy combo"},
    )
    assert assault["combos"] == [
        {
            "id": "1486-3750",
            "url": "https://commanderspellbook.com/combo/1486-3750/",
            "tag": "Spicy",
            "cards": ["Magda, Brazen Outlaw", "Aggravated Assault"],
            "requirements": [],
            "results": ["Infinite combat phases"],
            "popularity": 10497,
        }
    ]
    assert deck["moreComboPieces"] == 0
    ramp = deck["roles"][0]
    assert (ramp["role"], ramp["have"], ramp["target"], ramp["short"]) == ("ramp", 2, 10, True)
    assert ramp["ideas"][2] == {
        "name": "Jeska's Will",
        "priceCents": 3380,
        "edhrecRank": 107,
        "raises": {"bracket": 3, "reason": "game changer"},
        "themed": False,
        "fits": [],
        "withinBudget": True,
    }


def test_suggest_json_lists_cuts(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    for card in raw_deck["cards"]:
        if card["categories"] == ["Maybeboard"]:
            card["categories"] = ["Creature"]
    [deck] = json.loads(suggest(["--json", "-n", "1"], tmp_path, synced(tmp_path, raw_deck), own_cache).out)
    assert deck["cuts"] == [{"name": "Tempestra, Dame of Games", "priceCents": 24, "edhrecRank": 11271}]


@pytest.mark.usefixtures("decks")
def test_suggest_reuses_the_combo_list_until_refresh(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    suggest(["magda"], tmp_path, archidekt, own_cache)
    assert suggest(["magda"], tmp_path, archidekt, own_cache).out == SUGGEST
    assert len(archidekt.spellbook.combo_bodies) == 1
    suggest(["magda", "--refresh"], tmp_path, archidekt, own_cache)
    assert len(archidekt.spellbook.combo_bodies) == 2
    assert archidekt.spellbook.bodies == []


@pytest.mark.usefixtures("decks")
def test_suggest_without_spellbook_still_suggests_roles_and_exits_1(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    archidekt.spellbook.combos_status = 404
    result = suggest(["magda"], tmp_path, archidekt, own_cache)
    assert result.code == 1
    assert result.err.startswith("cmdr: Honestly, though..: no combo list: ")
    assert "cards that complete a combo: unknown, Commander Spellbook could not be reached\n" in result.out
    assert "      €0.30  Arcane Signet\n" in result.out


@pytest.mark.usefixtures("decks")
def test_suggest_falls_back_to_an_older_combo_list(
    tmp_path: Path, archidekt: FakeArchidekt, own_cache: Path
) -> None:
    suggest(["magda"], tmp_path, archidekt, own_cache)
    archidekt.spellbook.combos_status = 404
    result = suggest(["magda"], tmp_path, archidekt, own_cache, now=NOW + timedelta(days=8))
    assert result.code == 0
    assert "showing the combo list from 2026-10-15; refreshing it failed: " in result.err
    assert "      €6.68  Splinter Twin (1 combo: Spicy)\n" in result.out


@pytest.mark.usefixtures("decks")
def test_suggest_needs_card_data_before_asking_spellbook(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    result = suggest([], tmp_path, archidekt, tmp_path / "empty-cache")
    assert result.code == 1
    assert "run `cmdr sync`" in result.err
    assert archidekt.spellbook.combo_bodies == []


def test_an_emoji_deck_name_survives_a_redirected_cp1252_stdout(
    tmp_path: Path, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_deck["name"] = "Honestly 🎲 though"
    fake = FakeArchidekt()
    fake.put(raw_deck)
    assert sync(tmp_path, fake).code == 0
    raw_out, raw_err = io.BytesIO(), io.BytesIO()
    # What a Windows pipe gives a process: the ANSI codepage, not a terminal.
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw_out, encoding="cp1252", write_through=True))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw_err, encoding="cp1252", write_through=True))
    env = {"APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "localappdata")}
    code = main(["list", "--workspace", str(tmp_path / "decks")], env=env, now=lambda: NOW)
    assert code == 0
    assert "Honestly 🎲 though" in raw_out.getvalue().decode("utf-8")


def _broken_client() -> httpx.Client:
    raise RuntimeError("boom")


def _sync_with_broken_client(tmp_path: Path, *flags: str) -> Run:
    out, err = io.StringIO(), io.StringIO()
    env = {"APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "localappdata")}
    args = [*flags, "sync", "--user", "MAKKIS", "--workspace", str(tmp_path / "decks"), "--no-cards"]
    code = main(args, env=env, client_factory=_broken_client, stdout=out, stderr=err, now=lambda: NOW)
    return Run(code, out.getvalue(), err.getvalue())


def test_an_unexpected_error_is_one_line_on_stderr_and_exit_1(tmp_path: Path) -> None:
    result = _sync_with_broken_client(tmp_path)
    assert (result.code, result.err) == (1, "cmdr: unexpected error: RuntimeError: boom\n")


def test_an_unexpected_error_keeps_its_traceback_for_debug_output(tmp_path: Path) -> None:
    result = _sync_with_broken_client(tmp_path, "-vv")
    assert result.code == 1
    assert "Traceback (most recent call last)" in result.err
    assert "cmdr: unexpected error: RuntimeError: boom\n" in result.err
