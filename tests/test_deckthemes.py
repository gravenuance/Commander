import json
from collections.abc import Iterator
from dataclasses import replace

import pytest

from commander.archidekt import parse_deck
from commander.carddb import CardDb, Theme
from commander.deckprofile import Profile, ThemeSpec
from commander.deckthemes import (
    OVERRIDES,
    BroadKind,
    CoverRules,
    DeckThemes,
    DerivedTheme,
    Feature,
    FeatureKind,
    ThemeSource,
    broad_kind,
    cover,
    deck_tag_spec,
    derive_themes,
    merge_themes,
)
from commander.models import Deck, DeckCard
from conftest import MAGDA_ORACLE, Json, build_card_db

# Magda also carries a trivia tag, which never makes a theme, and a typal tag exists for Dwarves.
EXTRA_TAGS = [
    json.dumps(
        {
            "object": "tag",
            "id": f"eeeeeeee-0000-4000-8000-00000000000{index}",
            "slug": slug,
            "type": "oracle",
            "child_ids": [],
            "taggings": [{"oracle_id": MAGDA_ORACLE, "weight": "median"}] if tagged else [],
        }
    )
    for index, (slug, tagged) in enumerate([("symmetrical", True), ("typal-dwarf", False)])
]
SMALL = CoverRules(min_cards=1, min_gain=1)
TAG = FeatureKind.TAG
TYPE = FeatureKind.TYPE


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    with CardDb(build_card_db(tmp_path_factory.mktemp("themedb"), EXTRA_TAGS)) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    # Deck tags Dwarves, Vehicles and cEDH; Magda, Battered Golem, Mountain and Disruptor Flute in the deck.
    return parse_deck(raw_deck)


def filed(deck: Deck, name: str, *categories: str) -> Deck:
    """The deck with one card moved to these categories."""
    cards = tuple(replace(c, categories=categories) if c.name == name else c for c in deck.cards)
    return replace(deck, cards=cards)


def features(*cards: tuple[FeatureKind, str, set[int]]) -> dict[Feature, frozenset[str]]:
    return {(kind, value): frozenset(f"c{i}" for i in held) for kind, value, held in cards}


def test_the_cover_takes_distinctive_features_until_most_cards_are_covered() -> None:
    members = features(
        (TAG, "broad", {1, 2, 3, 4, 5, 6}),  # in 60% of all cards too: no lift
        (TAG, "leyline", {1, 2, 3, 4, 5}),
        (TYPE, "Dwarf", {6, 7, 8}),
        (TAG, "inside-leyline", {1, 2}),  # adds nothing once leyline is taken
    )
    base = dict(zip(members, [0.6, 0.001, 0.002, 0.0001], strict=True))
    picks, covered = cover(members, base, 10, CoverRules())
    assert picks == ((TAG, "leyline"), (TYPE, "Dwarf"))
    # 8 of 10 covered passes 70%, so nothing more is taken.
    assert covered == 8


def test_the_cover_stops_when_a_feature_adds_too_little_or_enough_are_taken() -> None:
    members = features((TAG, "a", {1, 2}), (TAG, "b", {3, 4}), (TAG, "c", {5, 6}), (TAG, "d", {7, 8}))
    base = dict.fromkeys(members, 0.001)
    picks, covered = cover(members, base, 10, CoverRules())
    # Three picks at most, though 6 of 10 is short of 70%.
    assert picks == ((TAG, "a"), (TAG, "b"), (TAG, "c"))
    assert covered == 6
    # Under 20% of the category is too thin a share to stand for it.
    assert cover(features((TAG, "a", {1, 2})), {(TAG, "a"): 0.001}, 20, CoverRules()) == ((), 0)


def test_a_specific_tag_beats_an_umbrella_that_covers_one_card_more() -> None:
    # Edgar's Anthem cards: typal-creature holds 6 of 7 but 5% of all cards; typal-vampire 5 of 7 and 0.25%.
    members = features((TAG, "typal-creature", {1, 2, 3, 4, 5, 6}), (TAG, "typal-vampire", {1, 2, 3, 4, 5}))
    base = {(TAG, "typal-creature"): 0.05, (TAG, "typal-vampire"): 0.0025}
    assert cover(members, base, 7, CoverRules()) == (((TAG, "typal-vampire"),), 5)


def test_covering_the_whole_category_beats_a_narrower_tag() -> None:
    # Edgar's Counters cards: all seven put +1/+1 counters; five are Vampires, as the whole deck is.
    members = features((TAG, "repeatable-pp-counters", set(range(7))), (TAG, "typal-vampire", set(range(5))))
    base = {(TAG, "repeatable-pp-counters"): 1698 / 32115, (TAG, "typal-vampire"): 82 / 32115}
    assert cover(members, base, 7, CoverRules()) == (((TAG, "repeatable-pp-counters"),), 7)


def test_a_cover_of_under_half_the_category_stands_for_nothing() -> None:
    # Two of six stax pieces share a tag; that says little about the other four.
    members = features((TAG, "hate-activation", {1, 2}))
    assert cover(members, {(TAG, "hate-activation"): 0.001}, 6, CoverRules()) == ((), 2)


def test_ties_go_to_a_type_then_by_name() -> None:
    members = features((TAG, "combat-ramp", {1}), (TYPE, "Dwarf", {1}), (TYPE, "Berserker", {1}))
    base = dict.fromkeys(members, 0.01)
    assert cover(members, base, 1, SMALL) == (((TYPE, "Berserker"),), 1)


@pytest.mark.parametrize(
    ("name", "spec"),
    [
        ("Dwarves", ThemeSpec(types=("Dwarf",), tags=("typal-dwarf",))),
        ("Vehicles", ThemeSpec(types=("Vehicle",))),
        ("Combat Ramp", ThemeSpec(tags=("combat-ramp",))),
        ("Ramp", None),
        ("Flash", ThemeSpec(keywords=("flash",))),
        ("Stax", OVERRIDES["stax"]),
        ("Go-wide", ThemeSpec(tags=("repeatable-creature-tokens", "anthem"))),
        ("Spaghetti", ThemeSpec()),
        ("cEDH", None),
        ("Casual", None),
        # The owner tags decks that depend on enter triggers this way: how they play, not cards to pick.
        ("ETB Effects", None),
        # A role suggest already has targets for, and a card type: too broad to pick cards by.
        ("Draw", None),
        ("Creatures", None),
        ("Reanimator", ThemeSpec(tags=("reanimate",))),
    ],
)
def test_deck_tags_match_types_keywords_and_tags_by_name(
    db: CardDb, name: str, spec: ThemeSpec | None
) -> None:
    assert deck_tag_spec(name, db) == spec


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("cEDH", BroadKind.PLAY_STYLE),
        ("Tempo", BroadKind.PLAY_STYLE),
        ("Draw", BroadKind.ROLE),
        ("Board Wipes", BroadKind.ROLE),
        ("Creatures", BroadKind.CARD_TYPE),
        ("Instants", BroadKind.CARD_TYPE),
        # An override names the cards for these, though they hold a card type.
        ("Artifacts", None),
        ("Dwarves", None),
    ],
)
def test_a_deck_tag_too_broad_for_a_theme_is_a_play_style_a_role_or_a_card_type(
    name: str, kind: BroadKind | None
) -> None:
    assert broad_kind(name) is kind


def test_the_stax_override_names_real_tags() -> None:
    assert OVERRIDES["stax"] == ThemeSpec(
        tags=("rule-of-law", "prevent-cast", "hate-activation", "prevent-activation", "hatebear")
    )


def test_themes_come_from_deck_tags_and_the_owners_own_categories(db: CardDb, deck: Deck) -> None:
    found = derive_themes(filed(deck, "Magda, Brazen Outlaw", "Commander", "Brutes 🪓"), db, SMALL)
    by_name = {theme.name: theme for theme in found.themes}
    assert by_name["Dwarves"] == DerivedTheme(
        ThemeSource.DECK_TAG, "Dwarves", ThemeSpec(types=("Dwarf",), tags=("typal-dwarf",))
    )
    assert by_name["Vehicles"] == DerivedTheme(
        ThemeSource.DECK_TAG, "Vehicles", ThemeSpec(types=("Vehicle",))
    )
    # Magda's trivia tag is left out; Berserker wins the tie among her one-card features.
    assert by_name["Brutes 🪓"] == DerivedTheme(
        ThemeSource.CATEGORY, "Brutes 🪓", ThemeSpec(types=("Berserker",)), covered=1, size=1
    )
    # Roles, card types, the commander, combo pieces and the Maybeboard are never themes.
    considered = {t.name for t in found.themes if t.source is ThemeSource.CATEGORY} | set(found.vague)
    assert considered <= {"Brutes 🪓", "Stax"}


def test_a_category_says_what_the_deck_tags_do_not(db: CardDb, deck: Deck) -> None:
    # Magda is a Dwarf Berserker in combat-ramp; with both tribes in the deck tags, the ramp is what's left.
    tagged = replace(filed(deck, "Magda, Brazen Outlaw", "Brutes"), tags=("Dwarves", "Berserkers"))
    by_name = {theme.name: theme for theme in derive_themes(tagged, db, SMALL).themes}
    assert by_name["Brutes"].spec == ThemeSpec(tags=("combat-ramp",))


def test_a_category_the_deck_tags_explain_is_neither_a_theme_nor_vague(
    db: CardDb, deck: Deck, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Magda's last feature is her tutor tag, which only an override can name: Tutors is a role.
    monkeypatch.setitem(OVERRIDES, "dwarf tutors", ThemeSpec(tags=("tutor",)))
    tagged = replace(
        filed(deck, "Magda, Brazen Outlaw", "Brutes"),
        tags=("Dwarves", "Berserkers", "Combat Ramp", "Dwarf Tutors"),
    )
    # Ramp is left out too: it is the tag above combat-ramp, which says no more about these cards.
    found = derive_themes(tagged, db, SMALL)
    assert "Brutes" not in {t.name for t in found.themes} | set(found.vague)


def test_a_card_counts_in_each_of_its_categories(db: CardDb, deck: Deck) -> None:
    found = derive_themes(filed(deck, "Magda, Brazen Outlaw", "Brutes", "Outlaws"), db, SMALL)
    assert {"Brutes", "Outlaws"} <= {t.name for t in found.themes}


def test_a_category_with_nothing_distinctive_is_reported(db: CardDb, deck: Deck) -> None:
    found = derive_themes(filed(deck, "Magda, Brazen Outlaw", "Brutes"), db, replace(SMALL, min_lift=1e9))
    assert "Brutes" in found.vague
    assert not [t for t in found.themes if t.source is ThemeSource.CATEGORY]


def test_unknown_cards_and_small_categories_are_left_out(db: CardDb, deck: Deck) -> None:
    stranger = DeckCard("Nobody Knows", 1, ("Mystery",), True, "no-such-id", 1, 99, "xxx", "1", "Normal")  # noqa: FBT003 — positional like the model
    found = derive_themes(replace(deck, cards=(*deck.cards, stranger)), db, CoverRules())
    assert "Mystery" not in {t.name for t in found.themes} | set(found.vague)
    # The real rules want 5 cards in a category; the fixture deck's categories are all smaller.
    assert not [t for t in found.themes if t.source is ThemeSource.CATEGORY]


def test_unmatched_deck_tags_are_reported_and_a_deck_without_any_has_no_themes(
    db: CardDb, deck: Deck
) -> None:
    assert derive_themes(replace(deck, tags=("Spaghetti",), cards=()), db).unmatched == ("Spaghetti",)
    assert derive_themes(replace(deck, tags=None, cards=()), db) == DeckThemes((), (), ())
    assert derive_themes(replace(deck, cards=()), db).broad == ("cEDH",)


RAMP_ID = "2f3e4ad7-5e60-41b4-bdbc-653f16869cf6"
TYPAL_DWARF_ID = "eeeeeeee-0000-4000-8000-000000000001"


def test_the_profiles_themes_come_first_then_the_derived_ones(db: CardDb, deck: Deck) -> None:
    merged = merge_themes(deck, Profile(themes=ThemeSpec(types=("Dwarf",), tags=("ramp",))), db)
    # Dwarf is named once, though both the profile and the Dwarves deck tag name it.
    assert merged.resolved.themes == Theme(types=("Dwarf", "Vehicle"), tag_ids=(RAMP_ID, TYPAL_DWARF_ID))
    assert [t.name for t in merged.used] == ["Dwarves", "Vehicles"]
    # In the labels, the profile keeps only what no deck tag names: Dwarves already says Dwarf.
    assert merged.resolved.labelled == (
        ("profile", Theme(tag_ids=(RAMP_ID,))),
        ("Dwarves", Theme(types=("Dwarf",), tag_ids=(TYPAL_DWARF_ID,))),
        ("Vehicles", Theme(types=("Vehicle",))),
    )
    assert (merged.profile_own, merged.profile_covered) == (
        ThemeSpec(tags=("ramp",)),
        ThemeSpec(types=("Dwarf",)),
    )


def test_a_profile_wholly_named_by_deck_tags_has_no_label(db: CardDb, deck: Deck) -> None:
    merged = merge_themes(deck, Profile(themes=ThemeSpec(types=("dwarf", "Vehicle"))), db)
    assert [name for name, _ in merged.resolved.labelled] == ["Dwarves", "Vehicles"]
    # The ranking still counts the profile's words: they are in the merged theme either way.
    assert merged.resolved.themes.types == ("dwarf", "Vehicle")


def test_deck_tags_meaning_the_same_share_one_label(db: CardDb, deck: Deck) -> None:
    merged = merge_themes(replace(deck, tags=("Dwarves", "Vehicles", "Dwarf")), Profile(), db)
    assert [name for name, _ in merged.resolved.labelled] == ["Dwarves/Dwarf", "Vehicles"]
    # Each still shows on its own where the deck's themes are listed.
    assert [t.name for t in merged.used] == ["Dwarves", "Vehicles", "Dwarf"]


def test_the_profile_ignores_derived_themes_by_name(db: CardDb, deck: Deck) -> None:
    merged = merge_themes(deck, Profile(ignore_themes=("VEHICLES", "Spaghetti", "cEDH")), db)
    assert [t.name for t in merged.used] == ["Dwarves"]
    assert merged.resolved.themes == Theme(types=("Dwarf",), tag_ids=(TYPAL_DWARF_ID,))
    # Spaghetti names nothing the deck has; cEDH is a deck tag, if one that makes no theme.
    assert merged.unknown_ignores == ("Spaghetti",)


def test_what_the_profile_excludes_leaves_the_derived_themes(db: CardDb, deck: Deck) -> None:
    merged = merge_themes(deck, Profile(exclude=ThemeSpec(types=("vehicle",), tags=("typal-dwarf",))), db)
    # The Vehicles theme had nothing else, so it goes; Dwarves keeps its type.
    assert [(t.name, t.spec) for t in merged.used] == [("Dwarves", ThemeSpec(types=("Dwarf",)))]
    assert merged.resolved.themes == Theme(types=("Dwarf",))


def test_a_deck_with_no_derived_themes_keeps_the_profiles_own(db: CardDb, deck: Deck) -> None:
    merged = merge_themes(
        replace(deck, tags=None), Profile(themes=ThemeSpec(tags=("ramp", "no-such-tag"))), db
    )
    assert merged.used == ()
    assert merged.resolved.themes == Theme(tag_ids=(RAMP_ID,))
    assert merged.resolved.unknown_tags == ("no-such-tag",)


def test_a_cards_tags_include_every_tag_above_them(db: CardDb) -> None:
    assert {"combat-ramp", "ramp", "symmetrical"} <= db.tag_lineage([MAGDA_ORACLE])[MAGDA_ORACLE]


def test_a_tags_ancestors_are_every_tag_above_it(db: CardDb) -> None:
    assert db.tag_ancestors(["combat-ramp"]) == frozenset({"ramp"})
    assert db.tag_ancestors(["ramp", "no-such-tag"]) == frozenset()


def test_theme_size_counts_commander_legal_cards(db: CardDb) -> None:
    assert db.theme_size(Theme(types=("Dwarf",))) == 1
    assert db.theme_size(Theme(keywords=("flash",))) == 2
    assert db.theme_size(Theme()) == 0
