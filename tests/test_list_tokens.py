"""The Tokens section at the end of a deck's list.md (ADR 0061)."""

from dataclasses import replace
from pathlib import Path

from commander.archidekt import parse_deck
from commander.carddb import CARD_DB_FILE, CardDb
from commander.models import Deck
from commander.render import deck_list_markdown, kept_tokens_section
from commander.store import Workspace
from commander.tokens import DeckTokens, tokens_of_deck
from conftest import Json
from test_cli_tokens import MAGDA_DECK
from test_render_and_diff import EXPECTED_LIST
from test_syncrun import populated, sync_only, sync_profiles, three_decks
from test_tokens import deck_of

SECTION = """\

<!-- cmdr:tokens -->
## Tokens (6)

- Copy: Esika's Chariot, Stormsplitter
- G 2/2 Bear: Caller of the Claw
- G 2/2 Cat: Esika's Chariot
- The Monarch: Palace Jailer
- C Treasure: Magda, Brazen Outlaw
- W 1/1 Vampire (Lifelink): Charismatic Conqueror
- Check by hand: Monkey Cage
"""
MAGDA_ONLY = "\n<!-- cmdr:tokens -->\n## Tokens (1)\n\n- C Treasure: Magda, Brazen Outlaw\n"


def test_the_tokens_come_last_as_one_line_each_with_the_cards_that_make_them(token_db_file: Path) -> None:
    deck = replace(deck_of(*MAGDA_DECK), name="Magda deck")
    with CardDb(token_db_file) as db:
        text = deck_list_markdown(deck, tokens_of_deck(db, deck))
    assert text.endswith(SECTION)
    assert text.count("## Tokens") == 1
    assert text.removesuffix(SECTION) == deck_list_markdown(deck)


def test_a_deck_that_makes_no_token_has_no_tokens_section(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    assert deck_list_markdown(deck, DeckTokens((), ())) == EXPECTED_LIST
    assert deck_list_markdown(deck, None) == EXPECTED_LIST


def test_cards_to_check_by_hand_alone_make_no_section(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    assert deck_list_markdown(deck, DeckTokens((), ("Monkey Cage",))) == EXPECTED_LIST


def test_the_tokens_section_is_found_again_in_a_list(token_db_file: Path) -> None:
    deck = replace(deck_of(*MAGDA_DECK), name="Magda deck")
    with CardDb(token_db_file) as db:
        text = deck_list_markdown(deck, tokens_of_deck(db, deck))
    assert kept_tokens_section(text) == SECTION
    assert kept_tokens_section(EXPECTED_LIST) == ""
    assert kept_tokens_section("") == ""
    # A list from before the marker, or a category named Tokens: nothing to carry, and nothing doubled.
    assert kept_tokens_section(EXPECTED_LIST + "\n## Tokens (1)\n\n- 1 Treasure Map\n") == ""


def test_saving_a_deck_keeps_the_section_it_had_and_writing_the_list_replaces_it(
    tmp_path: Path, token_db_file: Path
) -> None:
    space = Workspace(tmp_path / "decks")
    deck = replace(deck_of(*MAGDA_DECK), name="Magda deck")
    folder = space.save(deck)
    assert "## Tokens" not in (folder / "list.md").read_text(encoding="utf-8")

    with CardDb(token_db_file) as db:
        space.write_list(folder, deck, tokens_of_deck(db, deck))
    assert (folder / "list.md").read_text(encoding="utf-8").endswith(SECTION)

    # A save that knows no card data (a board save, an edit of a label) leaves the section as it was.
    edited = replace(deck, updated_at="2026-10-16T00:00:00Z", cards=deck.cards[:-1])
    space.save(edited)
    text = (folder / "list.md").read_text(encoding="utf-8")
    assert text.endswith(SECTION)
    assert "- 1 Palace Jailer" not in text.removesuffix(SECTION)

    space.write_list(folder, edited, DeckTokens((), ()))
    assert "## Tokens" not in (folder / "list.md").read_text(encoding="utf-8")


def test_writing_the_list_of_a_folder_that_is_gone_does_nothing(tmp_path: Path) -> None:
    gone = tmp_path / "decks" / "1-gone"
    Workspace.write_list(gone, deck_of("Magda, Brazen Outlaw"), DeckTokens((), ()))
    assert not gone.exists()


def test_a_sync_gives_each_deck_its_tokens_section(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    assert "## Tokens" not in (stored.folder / "list.md").read_text(encoding="utf-8")

    outcome = sync_profiles(tmp_path, fake)

    assert not outcome.warnings
    assert (stored.folder / "list.md").read_text(encoding="utf-8").endswith(MAGDA_ONLY)


def test_a_sync_without_card_data_leaves_the_section_as_it_was(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    sync_profiles(tmp_path, fake)
    (tmp_path / "cache" / CARD_DB_FILE).unlink()

    outcome = sync_profiles(tmp_path, fake)

    assert not outcome.warnings
    assert (stored.folder / "list.md").read_text(encoding="utf-8").endswith(MAGDA_ONLY)


def test_a_sync_takes_the_section_off_a_deck_that_no_longer_makes_tokens(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    sync_profiles(tmp_path, fake)
    magda_only = [c for c in raw_deck["cards"] if c["card"]["oracleCard"]["name"] != "Magda, Brazen Outlaw"]
    raw_deck["cards"] = magda_only
    raw_deck["updatedAt"] = "2026-10-20T00:00:00Z"
    fake.put(raw_deck)

    sync_profiles(tmp_path, fake)

    assert "## Tokens" not in (stored.folder / "list.md").read_text(encoding="utf-8")


def test_a_sync_of_one_deck_lists_that_decks_tokens_alone(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    sync_profiles(tmp_path, fake)
    lists = {stored.deck.id: stored.folder / "list.md" for stored in Workspace(tmp_path / "decks").load_all()}
    for path in lists.values():
        path.write_text(path.read_text(encoding="utf-8").removesuffix(MAGDA_ONLY), encoding="utf-8")

    sync_only(tmp_path, fake, [102], [], force=True)

    assert lists[102].read_text(encoding="utf-8").endswith(MAGDA_ONLY)
    for deck_id in (101, 103):
        assert "## Tokens" not in lists[deck_id].read_text(encoding="utf-8")


def with_tokens_category(deck: Deck) -> Deck:
    """The deck with two of its cards filed under a category named Tokens, as some of the owner's decks do."""
    cards = tuple(
        replace(card, categories=("Tokens",)) if card.name in {"Esika's Chariot", "Monkey Cage"} else card
        for card in deck.cards
    )
    return replace(deck, cards=cards)


def test_a_category_named_tokens_is_not_taken_for_the_generated_section(tmp_path: Path) -> None:
    space = Workspace(tmp_path / "decks")
    deck = with_tokens_category(replace(deck_of(*MAGDA_DECK), name="Magda deck"))
    folder = space.save(deck)
    first = (folder / "list.md").read_text(encoding="utf-8")
    assert first.count("## Tokens (2)") == 1
    for _ in range(3):
        space.save(deck)
        assert (folder / "list.md").read_text(encoding="utf-8") == first


def test_the_real_section_survives_saves_beside_a_tokens_category(
    tmp_path: Path, token_db_file: Path
) -> None:
    space = Workspace(tmp_path / "decks")
    deck = with_tokens_category(replace(deck_of(*MAGDA_DECK), name="Magda deck"))
    folder = space.save(deck)
    with CardDb(token_db_file) as db:
        space.write_list(folder, deck, tokens_of_deck(db, deck))
    written = (folder / "list.md").read_text(encoding="utf-8")
    assert written.endswith(SECTION)
    assert written.count("## Tokens (") == 2
    for _ in range(3):
        space.save(deck)
        assert (folder / "list.md").read_text(encoding="utf-8") == written
