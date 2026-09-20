"""Landed cost: what the record actually costs delivered.

The whole point of these numbers is ordering, so most of what is asserted here
is which offer wins - not the arithmetic for its own sake.
"""

from decimal import Decimal

import pytest

from groove_search.domain import Format, Money
from groove_search.shipping import Zone, landed_cost, zone_between

FX = {"EUR": Decimal("4.30"), "USD": Decimal("3.95"), "GBP": Decimal("5.05")}


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("PL", Zone.DOMESTIC),
        ("DE", Zone.EU),
        ("EU", Zone.EU),
        ("GB", Zone.WORLD),
        ("US", Zone.WORLD),
        (None, Zone.WORLD),
    ],
)
def test_zones_from_poland(origin, expected):
    assert zone_between(origin, "PL") is expected


def test_an_unknown_country_is_costed_as_the_worst_case():
    """Guessing cheap would let an unidentified foreign shop win on a
    domestic postage estimate - the exact error this module exists to stop."""
    known = landed_cost(Money(Decimal("100"), "PLN"), origin="PL", fmt=Format.VINYL, fx=FX)
    unknown = landed_cost(Money(Decimal("100"), "PLN"), origin=None, fmt=Format.VINYL, fx=FX)
    assert unknown.total.amount > known.total.amount
    assert any("unknown" in note for note in unknown.notes)


def test_import_vat_is_charged_on_parcels_from_outside_the_eu():
    cost = landed_cost(Money(Decimal("20"), "USD"), origin="US", fmt=Format.VINYL, fx=FX)
    assert cost.import_tax is not None
    # 79 PLN of record + 90 postage, +23% VAT and a clearance fee.
    assert cost.total.amount == Decimal("217.87")
    assert any("VAT" in note for note in cost.notes)


def test_no_import_tax_inside_the_eu():
    cost = landed_cost(Money(Decimal("30"), "EUR"), origin="DE", fmt=Format.VINYL, fx=FX)
    assert cost.import_tax is None


def test_a_cheap_foreign_record_loses_to_a_dearer_domestic_one():
    """The finding that motivated this module: a $20 record in Boston is not
    cheaper than a 90 PLN copy in Warsaw once it has been shipped."""
    boston = landed_cost(Money(Decimal("20"), "USD"), origin="US", fmt=Format.VINYL, fx=FX)
    warsaw = landed_cost(Money(Decimal("90"), "PLN"), origin="PL", fmt=Format.VINYL, fx=FX)
    assert boston.item.amount < warsaw.item.amount  # listed price says Boston
    assert boston.total.amount > warsaw.total.amount  # delivered says Warsaw


def test_cds_post_cheaper_than_vinyl():
    cd = landed_cost(Money(Decimal("50"), "PLN"), origin="PL", fmt=Format.CD, fx=FX)
    lp = landed_cost(Money(Decimal("50"), "PLN"), origin="PL", fmt=Format.VINYL, fx=FX)
    assert cd.total.amount < lp.total.amount


def test_free_domestic_delivery_over_the_threshold():
    cost = landed_cost(Money(Decimal("250"), "PLN"), origin="PL", fmt=Format.VINYL, fx=FX)
    assert cost.shipping.amount == Decimal("0")
    assert cost.total.amount == Decimal("250")
    assert any("free delivery" in note for note in cost.notes)


def test_everything_is_reported_in_one_currency():
    cost = landed_cost(Money(Decimal("22.49"), "GBP"), origin="GB", fmt=Format.VINYL, fx=FX)
    assert cost.item.currency == cost.shipping.currency == cost.total.currency == "PLN"
    assert cost.estimated is True
