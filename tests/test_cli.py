import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander import __version__
from commander.cli import main
from conftest import CARDS_URL, FakeArchidekt, Json, card_entry

NO_CARDS = "card data: skipped, --no-cards\n"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


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
    assert second.code == 1
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
    assert first.out.endswith("card data: updated, Scryfall data of 2026-10-15, 10,016 printings\n")
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
        "detail": "Scryfall data of 2026-10-15, 10,016 printings",
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
