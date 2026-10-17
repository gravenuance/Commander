"""A casual deck's budget from the prices of what it already runs (ADR 0025)."""

from commander.budget import Budget, average_cents, deck_budget


def test_the_working_budget_is_the_average_card_and_the_ceiling_a_quarter_over_the_dearest() -> None:
    # Two copies at 1.00 and one at 4.00 average 2.00; an unpriced card counts for nothing.
    assert average_cents([(2, 100), (1, 400), (1, None)]) == 200
    assert average_cents([(1, None)]) is None
    assert deck_budget(3, [(2, 100), (1, 400)], None) == Budget(200, 500)


def test_the_profiles_price_limit_caps_both_and_bracket_5_has_no_budget_of_its_own() -> None:
    assert deck_budget(3, [(2, 100), (1, 400)], 300) == Budget(200, 300)
    assert deck_budget(3, [(2, 100), (1, 400)], 150) == Budget(150, 150)
    assert deck_budget(5, [(2, 100), (1, 400)], None) == Budget(None, None)
    assert deck_budget(5, [(2, 100), (1, 400)], 300) == Budget(None, 300)


def test_within_and_allows_treat_unpriced_cards_as_unknown() -> None:
    budget = Budget(200, 500)
    # Within budget runs to 25% over the average card: 2.00 x 1.25 = 2.50.
    assert (budget.within(250), budget.within(251), budget.within(None)) == (True, False, False)
    assert (budget.allows(500), budget.allows(501), budget.allows(None)) == (True, False, False)
    assert Budget(None, None).within(None)


def test_the_within_line_never_passes_the_ceiling() -> None:
    assert Budget(200, 500).line == 250
    assert Budget(200, 220).line == 220
    assert Budget(None, 500).line is None
