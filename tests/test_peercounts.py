"""How many cards of each role and theme the commander's recent decks hold."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from commander.archidekt import PeerDeck
from commander.carddb import CardDb, Role, Theme
from commander.metadata import PeerList
from commander.peercounts import RoleBand, peer_counts
from conftest import build_card_db

COMMANDER = "Filler Card 100"
EXTRA_COMBAT = "cccccccc-0000-4000-8000-000000000001"
THEMED = [f"eeeeeeee-0000-4000-8000-{i:012d}" for i in (1, 2, 3, 4, 100)]
# Ancient Tomb is a land that the ramp tag and the made-up theme both hold.
TOMB = "23467047-6dba-4498-b783-1ebc4f74b8c2"
THEME_TAG = json.dumps(
    {
        "object": "tag",
        "id": EXTRA_COMBAT,
        "slug": "extra-combat",
        "type": "oracle",
        "child_ids": [],
        "taggings": [{"oracle_id": o, "weight": "median"} for o in [*THEMED, TOMB]],
    }
)
RAMP_ROCKS = ("Arcane Signet", "Mind Stone", "Jeska's Will", "Mana Crypt")


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    with CardDb(build_card_db(tmp_path_factory.mktemp("peercountsdb"), [THEME_TAG])) as opened:
        yield opened


def peers(count: int = 5) -> list[PeerList]:
    """Deck i holds the commander, Ancient Tomb, i ramp spells and Filler Cards 1 to i."""
    when = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        PeerList(
            PeerDeck(i, f"peer {i}", "someone", 10, when, 3),
            (COMMANDER, "Ancient Tomb", *RAMP_ROCKS[:i], *(f"Filler Card {n}" for n in range(1, i + 1))),
        )
        for i in range(count)
    ]


THEME = Theme(tag_ids=(EXTRA_COMBAT,))


def test_a_role_has_the_peers_25th_percentile_as_its_floor_and_their_median_as_its_middle(db: CardDb) -> None:
    found = peer_counts(peers(), {COMMANDER.casefold()}, db, {})
    assert found is not None
    # Ramp counts are 1 to 5 with the Tomb.
    assert found.roles[Role.RAMP] == RoleBand(2.0, 3.0)
    assert found.roles[Role.DRAW] == RoleBand(0.0, 0.0)


def test_a_theme_has_the_median_count_of_the_peers_without_the_commander(db: CardDb) -> None:
    found = peer_counts(peers(), {COMMANDER.casefold()}, db, {"Extra Combat": THEME})
    assert found is not None
    assert found.themes == {"Extra Combat": 3.0}


def test_a_nonland_only_theme_does_not_count_lands(db: CardDb) -> None:
    found = peer_counts(
        peers(), {COMMANDER.casefold()}, db, {"extra-combat": THEME}, nonland_only={"extra-combat"}
    )
    assert found is not None
    assert found.themes == {"extra-combat": 2.0}


def test_fewer_than_three_peer_decks_give_no_counts(db: CardDb) -> None:
    assert peer_counts(peers(2), {COMMANDER.casefold()}, db, {"Extra Combat": THEME}) is None
