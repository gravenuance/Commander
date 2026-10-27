"""The images the app's decklist shows."""

from dataclasses import replace
from pathlib import Path

from commander.carddb import CardDb
from commander.deckview import card_images
from conftest import MAGDA_ORACLE

SCRYFALL = "https://cards.scryfall.io/normal/front"


def test_a_card_shows_its_latest_regular_printing_and_one_with_none_is_left_out(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        magda = db.cards([MAGDA_ORACLE])[MAGDA_ORACLE]
        tomb = db.card_named("Ancient Tomb")
        assert tomb is not None

        images = card_images(db, [magda, tomb, replace(magda, oracle_id="no-such-card")])

    # Magda's last printing in set order is the Secret Lair; the Tomb's, the gold-bordered World Championship
    # one, is no regular printing, so Ultimate Masters' stands.
    assert images == {
        MAGDA_ORACLE: f"{SCRYFALL}/3/5/35f229b2-5246-4399-8468-eb030842a17b.jpg",
        tomb.oracle_id: f"{SCRYFALL}/b/d/bd3d4b4b-cf31-4f89-8140-9650edb03c7b.jpg",
    }
