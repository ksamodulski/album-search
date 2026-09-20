from decimal import Decimal

import pytest

from groove_search.domain import Money
from groove_search.pricing import convert, looks_like_price, parse_money


@pytest.mark.parametrize(
    "text,amount,currency",
    [
        ("129,99 zł", "129.99", "PLN"),
        ("1 299,00 zł", "1299.00", "PLN"),
        ("1.299,00 zł", "1299.00", "PLN"),
        ("PLN 129.99", "129.99", "PLN"),
        ("od 24,99 EUR", "24.99", "EUR"),
        ("$21.98", "21.98", "USD"),
        ("£19.99", "19.99", "GBP"),
        ("1,299.50 USD", "1299.50", "USD"),
        ("149 zł", "149", "PLN"),
    ],
)
def test_parses_shop_price_formats(text, amount, currency):
    money = parse_money(text)
    assert money == Money(Decimal(amount), currency)


def test_picks_the_sale_price_when_old_price_is_shown():
    assert parse_money("159,99 zł 129,99 zł").amount == Decimal("129.99")


def test_returns_none_without_a_number():
    assert parse_money("cena na zapytanie") is None
    assert parse_money("") is None


def test_defaults_currency_when_page_omits_it():
    assert parse_money("129,99", default_currency="EUR").currency == "EUR"


def test_looks_like_price_requires_an_explicit_currency():
    assert looks_like_price("129,99 zł")
    assert not looks_like_price("129,99")
    assert not looks_like_price("Pet Fox")


def test_convert_uses_rate_table():
    rates = {"EUR": Decimal("4.30"), "USD": Decimal("4.00")}
    assert convert(Money(Decimal("10.00"), "EUR"), "PLN", rates) == Money(Decimal("43.00"), "PLN")


def test_convert_leaves_unknown_currency_alone():
    money = Money(Decimal("10.00"), "JPY")
    assert convert(money, "PLN", {}) == money
