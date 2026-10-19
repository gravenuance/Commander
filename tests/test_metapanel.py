"""The Meta section: each meta card once with what of the deck's it stops, combos with the deck's answers."""

from typing import cast

from commander.answers import AnswerKind
from commander.keycards import KeyCard
from commander.meta import MetaAnswer, OwnCombo, Threat
from commander.metacombos import AnswerRole, ComboAnswer, MetaCombo
from commander.metapanel import Json, combo_groups, combo_rows, stopper_rows, untouched

KINNAN = "Kinnan, Bonder Prodigy"
KRAUM = "Kraum, Ludevic's Opus / Tymna the Weaver"


def threat(name: str, hits: tuple[str, ...], exposure: float) -> Threat:
    return Threat(name, (AnswerKind.COUNTERSPELL,), hits, exposure, ((KINNAN, 0.9), (KRAUM, 0.5)))


def test_combos_keep_their_order_with_the_decks_that_play_them_and_the_answers() -> None:
    combo = MetaCombo(
        "1-2",
        "https://commanderspellbook.com/combo/1-2/",
        ("Underworld Breach", "Lotus Petal", "Brain Freeze"),
        (),
        ("Infinite mana",),
        0.3,
        ((KINNAN, 0.25),),
        (ComboAnswer("Grafdigger's Cage", "locks the graveyard", AnswerRole.STAX),),
    )
    assert combo_rows([combo]) == [
        {
            "pieces": ["Underworld Breach", "Lotus Petal", "Brain Freeze"],
            "results": ["Infinite mana"],
            "url": "https://commanderspellbook.com/combo/1-2/",
            "exposure": 0.3,
            "rising": False,
            "decks": [{"short": "Kinnan", "name": KINNAN, "share": 0.25}],
            "answers": [
                {"card": "Grafdigger's Cage", "how": "locks the graveyard", "role": "stax", "catch": ""}
            ],
        }
    ]


def test_a_rising_combo_says_so() -> None:
    combo = MetaCombo(
        "1-2", "https://commanderspellbook.com/combo/1-2/", ("A", "B"), (), (), 0.1, (), (), rising=True
    )
    assert [row["rising"] for row in combo_rows([combo])] == [True]


def made_up(combo_id: str, exposure: float, *pieces: str) -> MetaCombo:
    return MetaCombo(combo_id, f"https://example.test/{combo_id}", pieces, (), (), exposure, (), ())


def test_combos_come_in_groups_with_their_shared_pieces_and_the_others() -> None:
    first = made_up("1-2", 0.3, "Kinnan", "Thassa's Oracle", "Walking Ballista")
    second = made_up("3-4", 0.2, "Kinnan", "Thassa's Oracle", "Basalt Monolith")
    alone = made_up("5-6", 0.25, "Sol Ring", "Mana Vault")
    groups = combo_groups([first, second, alone])
    assert [
        (g["shared"], [(c["pieces"], c["others"]) for c in cast("list[Json]", g["combos"])]) for g in groups
    ] == [
        (
            ["Kinnan", "Thassa's Oracle"],
            [
                (["Kinnan", "Thassa's Oracle", "Walking Ballista"], ["Walking Ballista"]),
                (["Kinnan", "Thassa's Oracle", "Basalt Monolith"], ["Basalt Monolith"]),
            ],
        ),
        ([], [(["Sol Ring", "Mana Vault"], ["Sol Ring", "Mana Vault"])]),
    ]
    # A grouped combo is still a whole row of combo_rows, plus its others.
    [row, *_] = cast("list[Json]", groups[0]["combos"])
    assert row == {**combo_rows([first])[0], "others": ["Walking Ballista"]}


def own_combo(*answers: MetaAnswer) -> OwnCombo:
    return OwnCombo(
        "3-4",
        "https://commanderspellbook.com/combo/3-4/",
        ("Squee, the Immortal", "Food Chain"),
        ("Infinite creature ETB",),
        answers,
    )


def test_each_meta_card_shows_once_with_every_combo_and_key_card_it_stops() -> None:
    silence = MetaAnswer("Silence", "limits casting", AnswerRole.STAX, "", 0.12345, ((KINNAN, 0.9),))
    force = MetaAnswer("Force of Will", "", AnswerRole.COUNTER, "", 0.4, ((KINNAN, 0.9), (KRAUM, 0.5)))
    threats = [
        threat("Force of Will", ("Drannith Magistrate", "Stony Silence"), 0.4),
        threat("Fierce Guardianship", ("Stony Silence",), 0.6),
    ]
    rows = stopper_rows([own_combo(silence, force)], threats)
    # Stax first; counters by how often they show up.
    assert [(r["card"], r["role"]) for r in rows] == [
        ("Silence", "stax"),
        ("Fierce Guardianship", "counter"),
        ("Force of Will", "counter"),
    ]
    assert rows[0] == {
        "card": "Silence",
        "role": "stax",
        "exposure": 0.1235,
        "decks": [{"short": "Kinnan", "name": KINNAN, "share": 0.9}],
        "combos": [
            {
                "pieces": "Squee, the Immortal + Food Chain",
                "url": "https://commanderspellbook.com/combo/3-4/",
                "how": "limits casting",
            }
        ],
        "keys": [],
        "kinds": [],
    }
    force_row = rows[2]
    assert force_row["keys"] == ["Drannith Magistrate", "Stony Silence"]
    assert force_row["kinds"] == ["counterspell"]
    assert force_row["decks"] == [
        {"short": "Kinnan", "name": KINNAN, "share": 0.9},
        {"short": "Kraum/Tymna", "name": KRAUM, "share": 0.5},
    ]


def test_removal_that_also_locks_a_combo_is_filed_as_stax() -> None:
    cage = MetaAnswer("Grafdigger's Cage", "locks the graveyard", AnswerRole.STAX, "", 0.2, ((KINNAN, 0.4),))
    removal = Threat("Grafdigger's Cage", (AnswerKind.CREATURE_REMOVAL,), ("Squee, the Immortal",), 0.2, ())
    assert [r["role"] for r in stopper_rows([own_combo(cage)], [removal])] == ["stax"]


def test_key_cards_inside_a_combo_the_card_stops_are_not_repeated() -> None:
    counter = MetaAnswer("Force of Will", "", AnswerRole.COUNTER, "", 0.4, ())
    threats = [threat("Force of Will", ("Food Chain", "Stony Silence"), 0.4)]
    assert stopper_rows([own_combo(counter)], threats)[0]["keys"] == ["Stony Silence"]


def test_untouched_lists_key_cards_nothing_hits_and_combos_nothing_stops() -> None:
    keys = [KeyCard("Food Chain", ("why",)), KeyCard("Stony Silence", ("why",))]
    threats = [threat("Force of Will", ("Stony Silence",), 0.4)]
    assert untouched(keys, [own_combo()], threats) == {
        "keys": ["Food Chain"],
        "combos": [
            {"pieces": "Squee, the Immortal + Food Chain", "url": "https://commanderspellbook.com/combo/3-4/"}
        ],
    }
