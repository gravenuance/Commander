from datetime import UTC, datetime, timedelta, timezone

from commander.peerschedule import PEER_FRESH_FOR, fresh_for, full_search_due, stable_offset

NOON = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
# Cache item names of fictional commanders at bracket 5, with the refresh day sha256 gives each (0, 1 or 2).
DAY_0 = "archidekt-fictional-commander-07-5-5e68d219"
DAY_1 = "archidekt-fictional-commander-01-5-a743fcc7"
DAY_2 = "archidekt-fictional-commander-04-5-c94e37fa"
# The same for the full-search day (0 to 6).
FULL_DAY_0 = "archidekt-fictional-commander-08-5-a9687be4"
FULL_DAY_5 = "archidekt-fictional-commander-12-5-8852c946"


def test_a_groups_day_is_fixed_by_its_name() -> None:
    assert [stable_offset(item, "refresh", 3) for item in (DAY_0, DAY_1, DAY_2)] == [0, 1, 2]


def test_an_answer_is_fresh_back_to_the_start_of_its_groups_latest_refresh_day() -> None:
    # 15 October 2026 is a day 2 of the cycle: the day-2 group's day is today, the day-0 group's was the 13th.
    assert PEER_FRESH_FOR.days == 3
    assert fresh_for(DAY_0, NOON) == timedelta(days=2, hours=12)
    assert fresh_for(DAY_1, NOON) == timedelta(days=1, hours=12)


def test_nothing_under_a_day_old_is_refreshed_even_on_the_groups_day() -> None:
    assert fresh_for(DAY_2, NOON) == timedelta(hours=24)


def test_a_groups_full_search_day_is_fixed_by_its_name() -> None:
    assert [stable_offset(item, "full", 7) for item in (FULL_DAY_0, FULL_DAY_5)] == [0, 5]


def test_a_full_search_is_due_once_the_groups_latest_weekly_day_has_begun_since_the_last_one() -> None:
    # 15 October 2026 is a day 4 of the seven: the day-0 group's day began on the 11th, the day-5 group's on
    # the 9th.
    assert not full_search_due(FULL_DAY_0, datetime(2026, 10, 11, tzinfo=UTC), NOON)
    assert full_search_due(FULL_DAY_0, datetime(2026, 10, 10, 23, 59, tzinfo=UTC), NOON)
    assert not full_search_due(FULL_DAY_5, datetime(2026, 10, 9, 8, 0, tzinfo=UTC), NOON)
    assert full_search_due(FULL_DAY_5, datetime(2026, 10, 8, 23, 0, tzinfo=UTC), NOON)


def test_a_search_made_today_is_not_due_whatever_the_day() -> None:
    for item in (DAY_0, DAY_1, DAY_2, FULL_DAY_0, FULL_DAY_5):
        assert not full_search_due(item, NOON, NOON)


def test_the_day_is_the_utc_day() -> None:
    # 05:00 on the 16th at UTC+10 is 19:00 on the 15th in UTC.
    local = datetime(2026, 10, 16, 5, 0, tzinfo=timezone(timedelta(hours=10)))
    assert fresh_for(DAY_0, local) == timedelta(days=2, hours=19)
