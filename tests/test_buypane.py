import pytest

from commander.buypane import PaneArea, parse_pane_area, screen_rect
from commander.buysettings import BuyError


class TestParsePaneArea:
    def test_numbers_become_an_area(self) -> None:
        assert parse_pane_area(10, 20.5, 300, 200, 1.5) == PaneArea(10.0, 20.5, 300.0, 200.0, 1.5)

    def test_a_negative_offset_is_kept(self) -> None:
        assert parse_pane_area(-4, -2, 300, 200, 1) == PaneArea(-4.0, -2.0, 300.0, 200.0, 1.0)

    @pytest.mark.parametrize(
        "numbers",
        [
            (0, 0, 0, 0, 0),
            (10, 20, 0, 200, 1),
            (10, 20, 300, 0, 1),
            (10, 20, 300, 200, 0),
            (10, 20, -1, 5, 1),
        ],
    )
    def test_an_empty_area_or_scale_is_no_area(self, numbers: tuple[int, int, int, int, int]) -> None:
        assert parse_pane_area(*numbers) is None

    @pytest.mark.parametrize(
        "bad",
        [True, "10", None, float("nan"), float("inf"), [1]],
    )
    @pytest.mark.parametrize("position", range(5))
    def test_a_value_that_is_not_a_finite_number_is_refused(self, bad: object, position: int) -> None:
        values: list[object] = [10, 20, 300, 200, 1]
        values[position] = bad

        with pytest.raises(BuyError, match="not valid"):
            parse_pane_area(*values)


class TestScreenRect:
    def test_the_area_is_scaled_and_moved_by_the_window_origin(self) -> None:
        area = PaneArea(10.5, 20.25, 300.0, 200.0, 1.5)

        # x 100 + 15.75, y 50 + 30.375, 300 * 1.5 and 200 * 1.5
        assert screen_rect((100, 50), area) == (116, 80, 450, 300)

    def test_at_full_size_the_area_is_the_origin_plus_the_css_pixels(self) -> None:
        assert screen_rect((8, 31), PaneArea(24, 120, 800, 420, 1.0)) == (32, 151, 800, 420)

    def test_a_window_on_a_second_screen_left_of_the_main_one_has_a_negative_origin(self) -> None:
        assert screen_rect((-1920, 40), PaneArea(0, 0, 100, 50, 2.0)) == (-1920, 40, 200, 100)
