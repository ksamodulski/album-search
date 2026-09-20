"""Text folding shared by query normalization and offer matching.

Both sides of a comparison must be folded the same way or scores are noise,
so the folding lives here once rather than in each caller.
"""

from __future__ import annotations

import re
import unicodedata

# Characters that survive NFKD decomposition and still need folding.
_CHAR_FOLD = str.maketrans(
    {
        "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
        "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "þ": "th",
        "'": "'", "`": "'", "’": "'", "‘": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "―": "-", "&": " and ",
    }
)

# Words that carry no identity and would otherwise inflate fuzzy scores.
STOPWORDS = frozenset(
    {"the", "a", "an", "of", "and", "de", "la", "le", "el", "i", "w", "z", "na"}
)

# Edition / pressing noise that shops append to titles.
_EDITION_NOISE = re.compile(
    r"\b("
    r"remaster(ed)?|reissue|re-?issue|deluxe|expanded|anniversary|edition|edycja|"
    r"limited|limitowana|special|bonus|digipak|digipack|gatefold|coloured|colored|"
    r"black|white|clear|marbled|splatter|translucent|opaque|heavyweight|"
    r"\d{2,3}\s?gram|\d{2,3}g|180\s?g|hq|hi-?fi|mp3|download|"
    r"nowa|nowy|folia|nowe|pl|eu|us|uk|import|version|wersja|"
    r"vinyl|winyl|winylowa|winylowy|lp|ep|cd|plyta|album|record|records|"
    r"\d+\s?x?\s?(lp|cd|vinyl|winyl)|box\s?set|boxset"
    r")\b",
    re.IGNORECASE,
)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def fold(value: str) -> str:
    """Casefold, strip diacritics and punctuation, collapse whitespace.

    "Björk – Homogénic (2LP)" -> "bjork homogenic 2lp"
    """
    if not value:
        return ""
    folded = value.translate(_CHAR_FOLD)
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    no_punct = _PUNCT.sub(" ", stripped)
    return _WS.sub(" ", no_punct).strip().casefold()


def strip_edition_noise(value: str) -> str:
    """Remove pressing/edition words so "Awakening (LP, 180g)" == "Awakening"."""
    return _WS.sub(" ", _EDITION_NOISE.sub(" ", value)).strip()


def tokenize(value: str, *, keep_stopwords: bool = False) -> frozenset[str]:
    """Fold to a set of identity-bearing tokens."""
    words = fold(value).split()
    if keep_stopwords:
        return frozenset(words)
    kept = [w for w in words if w not in STOPWORDS]
    return frozenset(kept or words)


def significant(value: str) -> str:
    """Fold, drop edition noise and stopwords, keep word order."""
    words = fold(strip_edition_noise(value)).split()
    kept = [w for w in words if w not in STOPWORDS]
    return " ".join(kept or words)
