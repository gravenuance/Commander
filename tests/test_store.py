import json
from dataclasses import replace
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.models import Deck
from commander.store import Workspace, WorkspaceError, folder_name, parse_snapshot, slugify, snapshot_json
from conftest import Json


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    return parse_deck(raw_deck)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("dainjosos🦖🦖", "dainjosos"),
        ("Let's not play together 🥂", "let-s-not-play-together"),
        ("Etali, Primal Conqueror // Etali", "etali-primal-conqueror-etali"),
        ("Æther Vial Déjà vu", "ther-vial-deja-vu"),
        ("🦖🦖", ""),
        ("a" * 80, "a" * 60),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_folder_name_falls_back_to_id_when_name_has_no_letters(deck: Deck) -> None:
    assert folder_name(replace(deck, name="🦖")) == "10004639"
    assert folder_name(deck) == "10004639-honestly-though"


def test_save_writes_three_files(tmp_path: Path, deck: Deck) -> None:
    folder = Workspace(tmp_path).save(deck)
    assert folder == tmp_path / "10004639-honestly-though"
    assert sorted(p.name for p in folder.iterdir()) == ["deck.json", "list.md", "notes.md"]


def test_snapshot_round_trips(deck: Deck) -> None:
    assert parse_snapshot(json.loads(snapshot_json(deck))) == deck


def test_load_returns_saved_deck(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    workspace.save(deck)
    assert workspace.load(deck.id) == deck
    assert workspace.load(1) is None


def test_save_never_overwrites_notes(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    notes = workspace.save(deck) / "notes.md"
    notes.write_text("my plans", encoding="utf-8")
    workspace.save(deck)
    assert notes.read_text(encoding="utf-8") == "my plans"


def test_save_renames_folder_when_deck_is_renamed(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    (workspace.save(deck) / "notes.md").write_text("keep me", encoding="utf-8")
    folder = workspace.save(replace(deck, name="Magda Treasure"))
    assert folder.name == "10004639-magda-treasure"
    assert [p.name for p in tmp_path.iterdir()] == ["10004639-magda-treasure"]
    assert (folder / "notes.md").read_text(encoding="utf-8") == "keep me"


def test_two_folders_for_one_deck_is_an_error(tmp_path: Path, deck: Deck) -> None:
    (tmp_path / "10004639-a").mkdir()
    (tmp_path / "10004639-b").mkdir()
    with pytest.raises(WorkspaceError, match="more than one folder for deck 10004639"):
        Workspace(tmp_path).load(deck.id)


def test_unreadable_snapshot_is_skipped(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    folder = workspace.save(deck)
    (folder / "deck.json").write_text("{ not json", encoding="utf-8")
    assert workspace.load(deck.id) is None
    assert workspace.load_all() == []


def test_snapshot_from_a_newer_version_is_skipped(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    folder = workspace.save(deck)
    document = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
    document["version"] = 99
    (folder / "deck.json").write_text(json.dumps(document), encoding="utf-8")
    assert workspace.load(deck.id) is None


def test_snapshot_keeps_the_deck_tags(tmp_path: Path, deck: Deck) -> None:
    folder = Workspace(tmp_path).save(deck)
    document = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert document["tags"] == ["Dwarves", "Vehicles", "cEDH"]


def test_a_snapshot_from_before_deck_tags_loads_with_them_unknown(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    folder = workspace.save(deck)
    document = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
    document["version"] = 1
    del document["tags"]
    (folder / "deck.json").write_text(json.dumps(document), encoding="utf-8")
    assert workspace.load(deck.id) == replace(deck, tags=None)


def test_a_snapshot_missing_its_tags_is_skipped(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    folder = workspace.save(deck)
    document = json.loads((folder / "deck.json").read_text(encoding="utf-8"))
    del document["tags"]
    (folder / "deck.json").write_text(json.dumps(document), encoding="utf-8")
    assert workspace.load(deck.id) is None


def test_load_all_ignores_unrelated_folders(tmp_path: Path, deck: Deck) -> None:
    workspace = Workspace(tmp_path)
    workspace.save(deck)
    (tmp_path / "drafts").mkdir()
    assert [s.deck.id for s in workspace.load_all()] == [10004639]


def test_missing_workspace_is_empty(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "absent")
    assert workspace.load_all() == []
    assert workspace.load(1) is None


def test_deck_folders_come_from_folder_names(tmp_path: Path) -> None:
    for name in ("10004639-magda", "22344867", "notes", "x-123"):
        (tmp_path / name).mkdir()
    (tmp_path / "123-file.txt").write_text("", encoding="utf-8")
    assert Workspace(tmp_path).deck_folders() == [
        (10004639, tmp_path / "10004639-magda"),
        (22344867, tmp_path / "22344867"),
    ]
    assert Workspace(tmp_path / "missing").deck_folders() == []
