"""The Tokens group the app's decklist ends with (ADR 0061)."""

from pathlib import Path
from typing import cast

from commander.board import Board
from commander.carddb import CardDb
from commander.deckview import decklist_json
from commander.models import Deck
from commander.scryfall import parse_card
from conftest import Json
from test_tokens import ALL_CASES, build, deck_of, real

SCRYFALL = "https://cards.scryfall.io/normal/front"


def grid_of(deck: Deck, db: CardDb) -> list[Json]:
    view = decklist_json(deck, Board(()), db, colours={}, share=lambda _: None)
    return cast("list[Json]", view["grid"])


def test_the_last_group_holds_the_decks_tokens_outside_the_deck_and_in_no_total(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        *groups, tokens = grid_of(deck_of(*ALL_CASES), db)
    assert {key: tokens[key] for key in ("key", "kind", "name", "inDeck", "count", "cents")} == {
        "key": "tokens-made",
        "kind": "tokens",
        "name": "Tokens",
        "inDeck": False,
        "count": 9,
        "cents": 0,
    }
    assert [c["name"] for c in tokens["cards"]] == [
        "Copy",
        "Vampire",
        "Bear",
        "Cat",
        "Elspeth, Sun's Champion Emblem",
        "Soldier",
        "The Monarch",
        "Treasure",
        "Undercity // The Initiative",
    ]
    # The deck's own 11 cards are all in its groups, and the tokens add none.
    assert sum(g["count"] for g in groups if g["inDeck"]) == 11


def test_a_token_carries_its_image_its_makers_and_nothing_to_change(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        tokens = grid_of(deck_of(*ALL_CASES), db)[-1]
    by_name = {c["name"]: c for c in tokens["cards"]}
    assert by_name["Treasure"] == {
        "name": "Treasure",
        "quantity": 1,
        "image": f"{SCRYFALL}/b/8/b8e8c3b4-3b8d-48a4-b427-a2b8ac3e2f8b.jpg",
        "category": "Tokens",
        "label": "",
        "labelColour": "",
        "cents": 0,
        "share": None,
        "pending": None,
        "more": False,
        "less": False,
        "tokenKind": "token",
        "makers": ["Magda, Brazen Outlaw"],
    }
    assert by_name["Copy"]["makers"] == ["Esika's Chariot", "Stormsplitter"]
    assert by_name["Copy"]["tokenKind"] == "helper"
    assert by_name["Copy"]["image"] == f"{SCRYFALL}/2/1/2115c141-f2fb-4bfd-a8b5-2943bad9f418.jpg"
    assert by_name["Elspeth, Sun's Champion Emblem"]["tokenKind"] == "emblem"
    assert all(not c["more"] and not c["less"] and c["pending"] is None for c in tokens["cards"])


def test_a_deck_whose_cards_make_no_token_has_no_tokens_group(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        grid = grid_of(deck_of("Battered Golem", "Mountain", "Disruptor Flute"), db)
    assert [g["name"] for g in grid] == ["Commander", "Artifact", "Land"]


def test_a_token_the_card_data_has_no_printing_for_has_no_image(tmp_path: Path) -> None:
    storm = parse_card(real("scryfall_tokens.jsonl", "Stormsplitter", "pblb"))
    assert storm is not None
    with build(tmp_path, storm) as db:
        tokens = grid_of(deck_of("Stormsplitter"), db)[-1]
    [copy] = tokens["cards"]
    assert (copy["name"], copy["image"], copy["makers"]) == ("Copy", "", ["Stormsplitter"])
