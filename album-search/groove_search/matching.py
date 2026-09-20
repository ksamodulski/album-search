"""Decide whether a scraped product really is the album the user asked for.

Shop search engines are generous: ask for "Abase - Awakening" and you get
back t-shirts, other artists' records and a cassette. This module is the
gatekeeper, and it is deliberately strict - a wrong "best price" is worse
than a missing one.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from .domain import Availability, Format, RawOffer
from .text import fold, significant, tokenize

# A query token counts as present when some offer token scores at least this.
_TOKEN_HIT = 85.0
# Minimum overall confidence for an offer to be shown to the user.
ACCEPT = 0.70
# Gates applied before the weighted score, so a strong title cannot smuggle
# in the wrong artist and vice versa.
_MIN_ARTIST_COVERAGE = 0.5
_MIN_TITLE_COVERAGE = 0.6

def _folded(*phrases: str) -> tuple[str, ...]:
    """Fold phrase constants exactly like the text they are matched against."""
    return tuple(fold(p) for p in phrases)


_MERCH = _folded(
    "t-shirt", "tshirt", "koszulka", "longsleeve", "hoodie", "bluza",
    "poster", "plakat", "tote", "torba", "mug", "kubek", "patch", "naszywka",
    "pin", "przypinka", "magnes", "magnet", "sticker", "naklejka", "ticket",
    "bilet", "gift card", "karta podarunkowa", "slipmat", "koc", "socks",
)
_WRONG_CARRIER = _folded(
    # "mc" for musicassette is deliberately absent: it is a whole token in
    # MC Solaar, MC Ride and a hundred other names, and shops that actually
    # sell cassettes say "kaseta" or "cassette". Keeping it cost more real
    # records than the format confusion it prevented.
    "dvd", "blu ray", "bluray", "vhs", "kaseta", "cassette",
    "ksiazka", "book", "magazine", "czasopismo", "puzzle", "calendar", "kalendarz",
    # Searching the open web surfaces download and streaming pages constantly,
    # and they are always cheaper than the record - so without this gate a
    # digital album wins every comparison it enters.
    "digital album", "digital download", "mp3", "flac", "streaming", "download",
)
_VINYL_WORDS = ("vinyl", "winyl", "lp", "12", "10", "7", "wax", "plyta winylowa")
_CD_WORDS = ("cd", "compact disc", "digipak", "digipack", "jewel case")

_OUT_OF_STOCK = _folded(
    "niedostepny", "niedostepna", "brak", "wyprzedane", "wyprzedany",
    "out of stock", "sold out", "unavailable", "nakladu", "chwilowo",
    "powiadom", "notify me",
)
_PREORDER = ("preorder", "pre order", "przedsprzedaz", "zapowiedz", "premiera")
_IN_STOCK = _folded(
    "dostepny", "dostepna", "w magazynie", "na stanie", "in stock",
    "add to cart", "do koszyka", "kup teraz", "buy now", "available",
)


@dataclass(frozen=True, slots=True)
class MatchVerdict:
    """Why an offer was kept or dropped - the reason is shown in diagnostics."""

    matched: bool
    confidence: float
    format: Format
    reason: str


def score_offer(query, raw: RawOffer) -> MatchVerdict:
    """Judge one extracted product against the user's query.

    `query` is an `AlbumQuery`. Confidence is 0..1; `matched` already applies
    the accept threshold and every hard gate, so callers need only read it.
    """
    haystack = significant(raw.title_text)
    if not haystack:
        return MatchVerdict(False, 0.0, Format.ANY, "empty title")

    words = tuple(fold(raw.title_text).split())
    if _mentions(words, _MERCH):
        return MatchVerdict(False, 0.0, Format.ANY, "merchandise, not a record")

    fmt = detect_format(raw.title_text, raw.url)
    if fmt is Format.ANY and _mentions(words, _WRONG_CARRIER):
        return MatchVerdict(False, 0.0, fmt, "not a CD or vinyl")
    if query.format is not Format.ANY and fmt is not Format.ANY and fmt is not query.format:
        return MatchVerdict(False, 0.0, fmt, f"wrong format: {fmt} not {query.format}")

    offer_tokens = tokenize(haystack, keep_stopwords=True)
    artist_coverage = _coverage(query.artist_tokens, offer_tokens)
    title_coverage = _coverage(query.title_tokens, offer_tokens)

    if query.is_artist_only:
        confidence = artist_coverage
        if artist_coverage < 1.0:
            return MatchVerdict(False, confidence, fmt, "artist name incomplete")
    else:
        if artist_coverage < _MIN_ARTIST_COVERAGE:
            return MatchVerdict(False, artist_coverage, fmt, "different artist")
        if title_coverage < _MIN_TITLE_COVERAGE:
            return MatchVerdict(False, title_coverage, fmt, "different title")
        confidence = 0.45 * artist_coverage + 0.55 * title_coverage

    return MatchVerdict(
        matched=confidence >= ACCEPT,
        confidence=round(confidence, 3),
        format=fmt,
        reason="match" if confidence >= ACCEPT else "too dissimilar",
    )


def _mentions(words: tuple[str, ...], phrases: tuple[str, ...]) -> bool:
    """Does any phrase appear as whole words, in order?

    The substring test this replaces quietly deleted real records: "Bookends"
    contains "book", "Pink" contains "pin" and "Kind of Blue" nearly falls to
    the same trick - three albums rejected as a book, a badge and a cassette.
    Phrases are matched as a run of tokens because folding splits "t-shirt"
    into two.
    """
    for phrase in phrases:
        parts = phrase.split()
        span = len(parts)
        if not span:
            continue
        if any(list(words[i : i + span]) == parts for i in range(len(words) - span + 1)):
            return True
    return False


def detect_format(*texts: str | None) -> Format:
    """Guess the carrier from a product title and/or its URL."""
    for text in texts:
        tokens = fold(text or "").split()
        if any(t in _VINYL_WORDS for t in tokens) or "lp" in tokens:
            return Format.VINYL
        if any(t in _CD_WORDS for t in tokens):
            return Format.CD
    return Format.ANY


def read_availability(*texts: str | None) -> Availability:
    """Interpret a shop's stock wording, in Polish or English."""
    blob = fold(" ".join(t for t in texts if t))
    if not blob:
        return Availability.UNKNOWN
    if any(phrase in blob for phrase in _OUT_OF_STOCK):
        return Availability.OUT_OF_STOCK
    if any(phrase in blob for phrase in _PREORDER):
        return Availability.PREORDER
    if any(phrase in blob for phrase in _IN_STOCK):
        return Availability.IN_STOCK
    return Availability.UNKNOWN


def _coverage(needles: frozenset[str], haystack: frozenset[str]) -> float:
    """Share of query tokens that appear (fuzzily) among the offer's tokens."""
    if not needles:
        return 1.0
    if not haystack:
        return 0.0
    hits = sum(
        1
        for needle in needles
        if max(fuzz.ratio(needle, token) for token in haystack) >= _TOKEN_HIT
    )
    return hits / len(needles)
