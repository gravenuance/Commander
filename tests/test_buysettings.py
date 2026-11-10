"""A Buy settings file with a misshaped field keeps its other fields; only a file that is no JSON object at
all is moved aside (ADR 0067)."""

import json
import logging
from pathlib import Path

import pytest

from commander.buysettings import BuyStore, LanguageChoice


def store_of(tmp_path: Path, document: object) -> tuple[BuyStore, Path]:
    path = tmp_path / "buy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return BuyStore(path), path


class TestMisshapedFields:
    def test_a_leftout_account_that_is_no_list_is_dropped_and_the_rest_stays(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store, path = store_of(
            tmp_path,
            {
                "version": 3,
                "filters": {"language": "japanese"},
                "baseSellers": 30,
                "leftOut": {"makkis": 2, "other": [5]},
            },
        )

        with caplog.at_level(logging.WARNING):
            assert store.filters().language is LanguageChoice.JAPANESE
            assert store.base_sellers() == 30
            assert store.left_out("makkis") == frozenset()
            assert store.left_out("other") == frozenset({5})

        assert not path.with_name("buy.json.bak").exists()
        assert "leftOut.makkis" in caplog.text

    def test_leftout_that_is_no_object_is_dropped_and_the_rest_stays(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store, path = store_of(tmp_path, {"version": 3, "baseSellers": 30, "leftOut": [1, 2]})

        with caplog.at_level(logging.WARNING):
            assert store.base_sellers() == 30
            assert store.left_out("makkis") == frozenset()

        assert not path.with_name("buy.json.bak").exists()
        assert "leftOut" in caplog.text

    @pytest.mark.parametrize("version", [1, 3])
    def test_filters_that_are_no_object_fall_back_to_the_defaults_and_the_rest_stays(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, version: int
    ) -> None:
        store, path = store_of(tmp_path, {"version": version, "filters": "english", "baseSellers": 30})

        with caplog.at_level(logging.WARNING):
            assert store.filters().language is LanguageChoice.ENGLISH
            assert store.base_sellers() == 30

        assert not path.with_name("buy.json.bak").exists()
        assert "filters" in caplog.text

    @pytest.mark.parametrize(
        ("left_out_cards", "kept"),
        [
            ("nope", {}),
            ({"makkis": 7}, {}),
            ({"makkis": {"3": "Sol Ring", "4": ["Mana Crypt"]}}, {4: frozenset({"Mana Crypt"})}),
        ],
    )
    def test_misshaped_left_out_cards_are_dropped_down_to_the_entry_and_the_rest_stays(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        left_out_cards: object,
        kept: dict[int, frozenset[str]],
    ) -> None:
        left_out = {"makkis": [1]}
        store, path = store_of(
            tmp_path,
            {"version": 3, "baseSellers": 30, "leftOutCards": left_out_cards, "leftOut": left_out},
        )

        with caplog.at_level(logging.WARNING):
            assert store.left_out_cards("makkis") == kept
            assert store.base_sellers() == 30
            assert store.left_out("makkis") == frozenset({1})

        assert not path.with_name("buy.json.bak").exists()
        assert "leftOutCards" in caplog.text

    def test_a_file_that_is_no_json_object_is_still_moved_aside(self, tmp_path: Path) -> None:
        store, path = store_of(tmp_path, [1, 2])

        assert store.base_sellers() == 15
        assert path.with_name("buy.json.bak").exists()
