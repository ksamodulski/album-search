import pytest

from groove_search.domain import Availability, Format, RawOffer
from groove_search.matching import detect_format, read_availability, score_offer
from groove_search.normalize import normalize_query


def offer(title, url="https://shop.pl/p/1"):
    return RawOffer(shop_id="shop", title_text=title, price_text="99 zł", url=url)


PET_FOX = normalize_query("Pet Fox - A face in your life")
ABASE = normalize_query("Abase - Awakening")
WBA = normalize_query("Whitest Boy Alive")


@pytest.mark.parametrize(
    "title",
    [
        "Pet Fox - A Face In Your Life",
        "PET FOX A FACE IN YOUR LIFE LP",
        "Pet Fox – A Face In Your Life (Limited Coloured Vinyl)",
        "A Face In Your Life - Pet Fox [CD]",
    ],
)
def test_accepts_the_right_record_however_the_shop_writes_it(title):
    verdict = score_offer(PET_FOX, offer(title))
    assert verdict.matched, verdict.reason


def test_rejects_a_different_artist_with_the_same_title():
    verdict = score_offer(PET_FOX, offer("Various Artists - A Face In Your Life"))
    assert not verdict.matched
    assert verdict.reason == "different artist"


def test_rejects_a_similar_artist_name():
    verdict = score_offer(ABASE, offer("Abasement - Awakening"))
    assert not verdict.matched


def test_rejects_a_different_album_by_the_right_artist():
    verdict = score_offer(PET_FOX, offer("Pet Fox - Rowsboat"))
    assert not verdict.matched
    assert verdict.reason == "different title"


def test_rejects_merchandise():
    verdict = score_offer(PET_FOX, offer("Pet Fox - A Face In Your Life T-Shirt"))
    assert not verdict.matched
    assert "merch" in verdict.reason


def test_rejects_cassette_and_dvd():
    assert not score_offer(ABASE, offer("Abase - Awakening (kaseta)")).matched
    assert not score_offer(ABASE, offer("Abase - Awakening DVD")).matched


def test_artist_only_query_accepts_any_album_by_that_artist():
    assert score_offer(WBA, offer("The Whitest Boy Alive - Dreams LP")).matched
    assert score_offer(WBA, offer("Whitest Boy Alive - Rules")).matched


def test_artist_only_query_rejects_a_partial_name_collision():
    assert not score_offer(WBA, offer("Boy Harsher - Careful")).matched


def test_format_request_filters_the_other_carrier():
    vinyl_only = normalize_query("Abase - Awakening (vinyl)")
    assert score_offer(vinyl_only, offer("Abase - Awakening LP")).matched
    rejected = score_offer(vinyl_only, offer("Abase - Awakening CD"))
    assert not rejected.matched
    assert "wrong format" in rejected.reason


def test_detect_format_reads_title_then_url():
    assert detect_format("Abase - Awakening LP") is Format.VINYL
    assert detect_format("Abase - Awakening", "https://shop.pl/cd/abase") is Format.CD
    assert detect_format("Abase - Awakening") is Format.ANY


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Dostępny", Availability.IN_STOCK),
        ("do koszyka", Availability.IN_STOCK),
        ("Chwilowo niedostępny", Availability.OUT_OF_STOCK),
        ("sold out", Availability.OUT_OF_STOCK),
        ("Przedsprzedaż - premiera 12.10", Availability.PREORDER),
        ("", Availability.UNKNOWN),
    ],
)
def test_reads_stock_wording_in_both_languages(text, expected):
    assert read_availability(text) is expected


@pytest.mark.parametrize(
    "title",
    [
        # Each of these was rejected outright by the substring gates that
        # preceded `_mentions`: "book" inside Bookends, "pin" inside Pink,
        # "mc" as a whole word in an artist's name.
        "Simon & Garfunkel - Bookends",
        "The Books - The Lemon of Pink",
        "MC Solaar - Qui Seme Le Vent Recolte Le Tempo",
        "Bookworms - Xeno",
        "Cassetteboy - The Parker Tapes",
    ],
)
def test_a_record_is_not_merchandise_because_a_word_hides_inside_its_title(title):
    """Words must match on token boundaries, not as substrings."""
    verdict = score_offer(
        normalize_query(title),
        RawOffer(shop_id="s", title_text=title, price_text="100 PLN", url="https://shop.pl/p/x"),
    )

    assert verdict.matched, verdict.reason


@pytest.mark.parametrize(
    "offer_title,reason",
    [
        ("Pet Fox T-Shirt", "merchandise, not a record"),
        ("Pet Fox koszulka", "merchandise, not a record"),
        ("Pet Fox - A Face In Your Life Cassette", "not a CD or vinyl"),
        ("Pet Fox - A Face In Your Life kaseta", "not a CD or vinyl"),
        # A download is always cheaper than the record, so it wins every
        # comparison it is allowed to enter.
        ("Pet Fox - A Face In Your Life Digital Album", "not a CD or vinyl"),
    ],
)
def test_the_gates_still_reject_what_they_are_for(offer_title, reason):
    verdict = score_offer(
        normalize_query("Pet Fox - A Face In Your Life"),
        RawOffer(shop_id="s", title_text=offer_title, price_text="100 PLN", url="https://shop.pl/p/x"),
    )

    assert not verdict.matched
    assert verdict.reason == reason
