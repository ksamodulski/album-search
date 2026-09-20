"""The recalibration cooldown: when a broken shop is worth re-learning."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from groove_search.domain import Shop
from groove_search.recipes import SearchRecipe
from groove_search.registry import BROKEN_AFTER, RECALIBRATE_AFTER, Registry

SHOP = Shop(id="alfa", name="Alfa Records", base_url="https://alfa.pl")


def registry_with(calibrated_at: str, *, failures: int = BROKEN_AFTER) -> Registry:
    recipe = SearchRecipe(
        shop_id="alfa",
        search_url="https://alfa.pl/search?q={query}",
        item_selector="li.product",
        title_selector="a.pname",
        price_selector="span.price",
        link_selector="a.pname",
        calibrated_at=calibrated_at,
    )
    registry = Registry(shops=[SHOP], recipes={"alfa": recipe})
    health = registry.health_of("alfa")
    health.consecutive_failures = failures
    health.last_error = "HTTP 429"
    return registry


def ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="seconds")


def test_a_freshly_calibrated_shop_is_not_recalibrated_again():
    registry = registry_with(ago(days=3))
    assert registry.broken == [SHOP]  # still reported as broken...
    assert registry.repairable == []  # ...but not re-learned; assume it is down
    assert registry.repair_blocked("alfa")


def test_an_old_recipe_is_worth_re_learning():
    registry = registry_with(ago(days=RECALIBRATE_AFTER.days + 1))
    assert not registry.repair_blocked("alfa")
    assert registry.repairable == [SHOP]


def test_cooldown_boundary_is_the_configured_window():
    assert registry_with(ago(days=RECALIBRATE_AFTER.days, minutes=-1)).in_cooldown("alfa")
    assert not registry_with(ago(days=RECALIBRATE_AFTER.days, minutes=1)).in_cooldown("alfa")


def test_a_healthy_shop_is_never_repairable_even_when_old():
    registry = registry_with(ago(days=365), failures=0)
    assert registry.broken == []
    assert registry.repairable == []


def test_a_working_shop_can_always_be_re_learned_on_purpose():
    """The cooldown declines automatic repair, not a deliberate re-learn.

    A shop that redesigns its site still returns results, so it never goes
    broken; its Re-learn button must keep working the day after setup.
    """
    registry = registry_with(ago(minutes=5), failures=0)
    assert registry.in_cooldown("alfa")
    assert not registry.repair_blocked("alfa")


def test_a_merely_degraded_shop_is_not_blocked():
    registry = registry_with(ago(days=1), failures=BROKEN_AFTER - 1)
    assert registry.health_of("alfa").status == "degraded"
    assert not registry.repair_blocked("alfa")


@pytest.mark.parametrize("stamp", ["", "not-a-date"])
def test_an_unstamped_recipe_is_treated_as_old(stamp):
    registry = registry_with(stamp)
    assert not registry.repair_blocked("alfa")
    assert registry.repairable == [SHOP]


def test_a_shop_with_no_recipe_is_never_in_cooldown():
    registry = registry_with(ago(days=1))
    registry.recipes.pop("alfa")
    assert not registry.in_cooldown("alfa")
    assert not registry.repair_blocked("alfa")


def test_cooldown_survives_a_save_and_load(tmp_path):
    registry = registry_with(ago(days=2))
    registry.path = tmp_path / "registry.json"
    registry.save()
    assert Registry.load(tmp_path / "registry.json").repair_blocked("alfa")


def test_naive_timestamps_are_read_as_utc():
    naive = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
    assert registry_with(naive.isoformat(timespec="seconds")).in_cooldown("alfa")


def test_adopting_a_shop_restarts_the_cooldown():
    registry = registry_with(ago(days=365))
    assert registry.repairable == [SHOP]
    fresh = replace(registry.recipes["alfa"], calibrated_at=ago(minutes=1))
    registry.adopt(SHOP, fresh, coverage=0.5)
    assert registry.broken == []
    assert registry.in_cooldown("alfa")
