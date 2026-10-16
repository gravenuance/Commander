"""The Meta section: threats grouped under the deck cards they stop, combos with the deck's answers."""

from typing import cast

from commander.answers import AnswerKind
from commander.keycards import KeyCard
from commander.meta import MetaAnswer, OwnCombo, Threat
from commander.metacombos import AnswerRole, ComboAnswer, MetaCombo
from commander.metapanel import Json, combo_rows, own_combo_rows, piece_threats

KINNAN = "Kinnan, Bonder Prodigy"
KRAUM = "Kraum, Ludevic's Opus / Tymna the Weaver"


def threats_of(row: Json) -> list[Json]:
    return cast("list[Json]", row["threats"])


def threat(name: str, hits: tuple[str, ...], exposure: float) -> Threat:
    return Threat(name, (AnswerKind.COUNTERSPELL,), hits, exposure, ((KINNAN, 0.9), (KRAUM, 0.5)))


def test_each_key_card_lists_what_stops_it_most_exposed_first() -> None:
    threats = [
        threat("Force of Will", ("Drannith Magistrate", "Stony Silence"), 0.4),
        threat("Fierce Guardianship", ("Stony Silence",), 0.6),
    ]
    keys = [
        KeyCard(name, ("why",))
        for name in ("Drannith Magistrate", "Ellivere of the Wild Court", "Stony Silence")
    ]
    rows = piece_threats(keys, threats)
    assert rows[0]["reasons"] == ["why"]
    assert [(r["card"], [t["name"] for t in threats_of(r)]) for r in rows] == [
        ("Stony Silence", ["Fierce Guardianship", "Force of Will"]),
        ("Drannith Magistrate", ["Force of Will"]),
        ("Ellivere of the Wild Court", []),
    ]
    first = threats_of(rows[0])[0]
    assert first == {
        "name": "Fierce Guardianship",
        "kinds": ["counterspell"],
        "exposure": 0.6,
        "decks": [
            {"short": "Kinnan", "name": KINNAN, "share": 0.9},
            {"short": "Kraum/Tymna", "name": KRAUM, "share": 0.5},
        ],
    }


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
            "decks": [{"short": "Kinnan", "name": KINNAN, "share": 0.25}],
            "answers": [
                {"card": "Grafdigger's Cage", "how": "locks the graveyard", "role": "stax", "catch": ""}
            ],
        }
    ]


def test_own_combos_list_the_meta_answers_with_the_decks_playing_them() -> None:
    combo = OwnCombo(
        "3-4",
        "https://commanderspellbook.com/combo/3-4/",
        ("Squee, the Immortal", "Food Chain"),
        ("Infinite creature ETB",),
        (MetaAnswer("Silence", "limits casting", AnswerRole.STAX, "", 0.12345, ((KINNAN, 0.9),)),),
    )
    assert own_combo_rows([combo]) == [
        {
            "pieces": ["Squee, the Immortal", "Food Chain"],
            "results": ["Infinite creature ETB"],
            "url": "https://commanderspellbook.com/combo/3-4/",
            "answers": [
                {
                    "card": "Silence",
                    "how": "limits casting",
                    "role": "stax",
                    "catch": "",
                    "exposure": 0.1235,
                    "decks": [{"short": "Kinnan", "name": KINNAN, "share": 0.9}],
                }
            ],
        }
    ]
