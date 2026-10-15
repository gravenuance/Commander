import io
import json
from pathlib import Path

import pytest

from commander import __version__
from commander.cli import main
from conftest import FakeArchidekt, Json, card_entry


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


def run(args: list[str], tmp_path: Path, fake: FakeArchidekt | None = None) -> Run:
    out, err = io.StringIO(), io.StringIO()
    env = {"APPDATA": str(tmp_path / "appdata")}
    factory = (fake or FakeArchidekt()).client
    code = main(args, env=env, client_factory=factory, stdout=out, stderr=err)
    return Run(code, out.getvalue(), err.getvalue())


def sync(tmp_path: Path, fake: FakeArchidekt, *extra: str) -> Run:
    return run(["sync", "--user", "MAKKIS", "--workspace", str(tmp_path / "decks"), *extra], tmp_path, fake)


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
    assert result.out == "new        10004639  Honestly, though..\n1 new\n"
    assert (tmp_path / "decks" / "10004639-honestly-though" / "deck.json").is_file()


def test_unchanged_deck_is_not_refetched(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    archidekt.requests.clear()
    result = sync(tmp_path, archidekt)
    assert result.out == "1 unchanged\n"
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
    )
    saved = json.loads((tmp_path / "decks/10004639-honestly-though/deck.json").read_text(encoding="utf-8"))
    assert saved["updatedAt"] == "2026-10-14T10:00:00Z"


def test_dry_run_writes_nothing(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    result = sync(tmp_path, archidekt, "--dry-run")
    assert result.out == "new        10004639  Honestly, though..\n1 new (dry run, nothing written)\n"
    assert not (tmp_path / "decks").exists()


def test_deck_gone_from_archidekt_is_kept_and_reported(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    sync(tmp_path, archidekt)
    archidekt.decks.clear()
    result = sync(tmp_path, archidekt)
    assert result.out == "missing    10004639  Honestly, though..\n1 missing\n"
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
        "1 new, 1 failed\n"
    )
    assert (tmp_path / "decks" / "222-other").is_dir()


def test_sync_json_output(tmp_path: Path, archidekt: FakeArchidekt) -> None:
    rows = json.loads(sync(tmp_path, archidekt, "--json").out)
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
    result = run(["sync"], tmp_path, archidekt)
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
    assert result.out == "1 unchanged\n"
    listing = (tmp_path / "decks/10004639-honestly-though/list.md").read_text(encoding="utf-8")
    assert "- 1 Sol Ring" in listing
