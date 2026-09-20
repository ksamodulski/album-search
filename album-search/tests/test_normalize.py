import pytest

from groove_search.domain import Format
from groove_search.normalize import normalize_lines, normalize_query


def test_splits_artist_and_title_on_dash():
    q = normalize_query("Pet Fox - A face in your life")
    assert q.artist == "Pet Fox"
    assert q.title == "A face in your life"
    assert q.format is Format.ANY
    assert not q.is_artist_only


def test_strips_list_marker_used_in_pasted_lists():
    q = normalize_query("-> Abase - Awakening")
    assert (q.artist, q.title) == ("Abase", "Awakening")


def test_artist_only_line_has_no_title():
    q = normalize_query("Whitest Boy Alive")
    assert q.is_artist_only
    assert q.artist == "Whitest Boy Alive"
    assert q.label == "Whitest Boy Alive"


def test_hyphenated_name_is_not_split():
    q = normalize_query("Jay-Z - The Blueprint")
    assert q.artist == "Jay-Z"
    assert q.title == "The Blueprint"


def test_title_by_artist_form():
    q = normalize_query("Awakening by Abase")
    assert (q.artist, q.title) == ("Abase", "Awakening")


def test_trailing_format_hint_is_extracted_not_searched():
    q = normalize_query("Abase - Awakening (vinyl)")
    assert q.format is Format.VINYL
    assert q.title == "Awakening"


def test_bare_trailing_format_hint():
    q = normalize_query("Pet Fox - A Face In Your Life LP")
    assert q.format is Format.VINYL
    assert q.title == "A Face In Your Life"


def test_format_word_inside_artist_name_is_kept():
    q = normalize_query("Vinyl Williams - Lemniscate")
    assert q.artist == "Vinyl Williams"
    assert q.format is Format.ANY


def test_search_terms_are_ordered_most_specific_first():
    q = normalize_query("Pet Fox - A face in your life")
    assert q.search_terms[0] == "pet fox face in your life"
    assert "pet fox" in q.search_terms[-1]


def test_diacritics_are_folded_into_tokens():
    q = normalize_query("Björk - Homogénic")
    assert "bjork" in q.artist_tokens
    assert "homogenic" in q.title_tokens


def test_normalize_lines_skips_blanks_and_duplicates():
    queries = normalize_lines("""
-> Pet Fox - A face in your life

-> Abase - Awakening
-> Pet Fox  -  A Face In Your Life
-> Whitest Boy Alive
""")
    assert [q.label for q in queries] == [
        "Pet Fox - A face in your life",
        "Abase - Awakening",
        "Whitest Boy Alive",
    ]


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Burial - Untrue EP", {"untrue"}),
        ("Aphex Twin - Windowlicker EP", {"windowlicker"}),
        ("The Beatles - Rubber Soul (Remastered)", {"rubber", "soul"}),
        ("Nat Birchall - Akhenaten 180g", {"akhenaten"}),
    ],
)
def test_a_query_folds_its_title_the_way_an_offer_will(line, expected):
    """Both sides of a comparison must fold alike or the scores are noise.

    `matching` scores against `significant()`, which drops edition noise, so
    a query that keeps "EP" asks for a token no offer can supply: the title
    scored 50% and was rejected as a different record.
    """
    assert set(normalize_query(line).title_tokens) == expected


def test_a_title_that_is_all_edition_noise_keeps_its_words():
    """An empty token set would match every record there is.

    A trailing "LP" is taken as a format request and leaves an artist-only
    query, but nothing consumes "Remastered Deluxe" - and folding that to
    nothing would make the query match every record in every shop.
    """
    assert set(normalize_query("Some Band - Remastered Deluxe").title_tokens) == {
        "remastered",
        "deluxe",
    }
