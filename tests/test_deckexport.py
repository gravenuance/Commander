"""The deck as TopDeck.gg decklist text, and the decks as a Cardmarket wants list."""

from commander.deckexport import WantsList, cardmarket_wants_text, topdeck_text
from commander.models import Deck, DeckCard


def card(
    name: str, quantity: int = 1, *categories: str, in_deck: bool = True, label: str | None = ""
) -> DeckCard:
    return DeckCard(
        name, quantity, categories or ("Ramp",), in_deck, "sid", 1, 1, "tst", "1", "Normal", label
    )


def make_deck(name: str, *cards: DeckCard, deck_id: int = 1) -> Deck:
    return Deck(deck_id, name, "me", 5, "2026-10-16", cards)


def dont_have(name: str, quantity: int = 1) -> DeckCard:
    return card(name, quantity, label="Don't Have")


def test_commanders_come_first_then_the_rest_by_name_with_front_faces_only() -> None:
    deck = Deck(
        1,
        "test",
        "me",
        5,
        "2026-10-16",
        (
            card("Mountain", 6, "Land"),
            card("Sundering Eruption // Volcanic Fissure"),
            card("Magda, Brazen Outlaw", 1, "Commander"),
            card("Arcane Signet"),
            card("High-Speed Hoverbike", 1, "Maybeboard", in_deck=False),
        ),
    )
    assert topdeck_text(deck).splitlines() == [
        "~~Commanders~~",
        "1 Magda, Brazen Outlaw",
        "",
        "~~Mainboard~~",
        "1 Arcane Signet",
        "6 Mountain",
        "1 Sundering Eruption",
    ]


def test_wants_sum_a_card_over_the_decks_and_count_every_copy() -> None:
    first = make_deck("first", dont_have("Sol Ring"), dont_have("Dark Ritual", 2), card("Island", 6, "Land"))
    second = make_deck("second", dont_have("Sol Ring"), dont_have("Brainstorm"), deck_id=2)

    assert cardmarket_wants_text([first, second]) == WantsList(
        "1 Brainstorm\n2 Dark Ritual\n2 Sol Ring\n", 5, ()
    )


def test_wants_hold_only_main_deck_cards_labelled_dont_have() -> None:
    deck = make_deck(
        "test",
        dont_have("Sol Ring"),
        card("Arcane Signet", label="Getting"),
        card("Mana Crypt", label="Proxy"),
        card("Mountain", 6, "Land"),
        card("Chrome Mox", 1, "Maybeboard", in_deck=False, label="Don't Have"),
        card("Lotus Petal", 1, "Sideboard", in_deck=False, label="Don't Have"),
    )

    assert cardmarket_wants_text([deck]) == WantsList("1 Sol Ring\n", 1, ())


def test_wants_keep_a_two_faced_name_whole_and_sort_by_name_ignoring_case() -> None:
    deck = make_deck(
        "test",
        dont_have("Zealous Conscripts"),
        dont_have("Sundering Eruption // Volcanic Fissure"),
        dont_have("abrade"),
        dont_have("Brainstorm"),
    )

    assert cardmarket_wants_text([deck]).text.splitlines() == [
        "1 abrade",
        "1 Brainstorm",
        "1 Sundering Eruption // Volcanic Fissure",
        "1 Zealous Conscripts",
    ]


def test_wants_skip_decks_without_known_labels_and_name_them_in_order() -> None:
    known = make_deck("known", dont_have("Sol Ring"))
    unknown = make_deck("Zeta", dont_have("Brainstorm"), card("Mountain", label=None), deck_id=2)
    other = make_deck("alpha", card("Dark Ritual", label=None), deck_id=3)

    assert cardmarket_wants_text([known, unknown, other]) == WantsList("1 Sol Ring\n", 1, ("alpha", "Zeta"))


def test_wants_are_empty_when_nothing_is_to_buy() -> None:
    deck = make_deck("test", card("Arcane Signet", label="Getting"), card("Mountain", 6, "Land"))

    assert cardmarket_wants_text([deck]) == WantsList("", 0, ())
    assert cardmarket_wants_text([]) == WantsList("", 0, ())
