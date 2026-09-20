"""Turn whatever the user typed into an `AlbumQuery` shops can be asked about.

Interface: `normalize_query` for one line, `normalize_lines` for a pasted list.
Everything else - separator guessing, format hints, term ordering - is private.
"""

from __future__ import annotations

import re

from .domain import AlbumQuery, Format
from .text import fold, significant, tokenize

# Leading list decoration users paste in: "-> ", "* ", "1. ", "- ".
_LIST_MARKER = re.compile(r"^\s*(?:[-*•>]+>?|\d+[.)])\s+")

# Artist/title separators. Dashes must be whitespace-flanked so "Jay-Z" survives.
_SEPARATORS = (
    re.compile(r"\s+[-–—]\s+"),
    re.compile(r"\s+[/|:]\s+"),
)
_BY = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", re.IGNORECASE)

_FORMAT_HINTS: tuple[tuple[re.Pattern[str], Format], ...] = (
    (re.compile(r"\b(?:\d\s*x\s*)?(?:lp|vinyl|winyl|wax)\b|\b12\"|\b7\"", re.IGNORECASE), Format.VINYL),
    (re.compile(r"\bcds?\b|\bcompact\s+disc\b", re.IGNORECASE), Format.CD),
)
# A trailing "(vinyl)" / "[CD]" is a format request, not part of the title.
_TRAILING_HINT = re.compile(r"[\(\[\{]\s*[^\)\]\}]{1,24}\s*[\)\]\}]\s*$")


def normalize_query(raw: str) -> AlbumQuery:
    """Parse one user line into a query.

    Handles "Artist - Title", "Title by Artist", a bare artist name, and a
    trailing format hint in brackets. Never raises: an unparseable line
    becomes an artist-only query on the whole string.
    """
    line = _LIST_MARKER.sub("", raw).strip()
    body, fmt = _extract_format(line)
    artist, title = _split_artist_title(body)
    return AlbumQuery(
        raw=raw.strip(),
        artist=artist,
        title=title,
        format=fmt,
        search_terms=_search_terms(artist, title),
        artist_tokens=tokenize(artist) if artist else frozenset(),
        # Folded exactly as an offer's title will be. `matching` scores against
        # `significant()`, which drops edition noise, so a query that keeps it
        # is asking for a token no offer can ever supply: "Burial - Untrue EP"
        # scored 50% on a two-word title and was rejected as a different
        # record, while "Burial - Untrue" matched. Both sides fold alike or
        # the comparison is noise.
        # A title that is *entirely* edition noise ("Various - LP") would fold
        # to nothing, and an empty token set matches every record there is -
        # so keep the raw words there and let it match nothing instead, which
        # is the safe direction to be wrong in.
        title_tokens=tokenize(significant(title) or title) if title else frozenset(),
    )


def normalize_lines(text: str) -> list[AlbumQuery]:
    """Parse a pasted block, one album per line, ignoring blanks and dupes."""
    seen: set[str] = set()
    queries: list[AlbumQuery] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        query = normalize_query(line)
        key = fold(query.label)
        if key and key not in seen:
            seen.add(key)
            queries.append(query)
    return queries


def _extract_format(line: str) -> tuple[str, Format]:
    """Pull a format request out of the line, returning the line without it."""
    trailing = _TRAILING_HINT.search(line)
    if trailing:
        for pattern, fmt in _FORMAT_HINTS:
            if pattern.search(trailing.group(0)):
                return line[: trailing.start()].strip(), fmt
    for pattern, fmt in _FORMAT_HINTS:
        match = pattern.search(line)
        # Only treat a bare hint as a request when it sits at the very end,
        # so "Vinyl Williams - Lemniscate" keeps its artist.
        if match and match.end() >= len(line.rstrip(" )]}.")) - 1 and match.start() > 0:
            return (line[: match.start()] + line[match.end():]).strip(" -–—([{)]}"), fmt
    return line, Format.ANY


def _split_artist_title(body: str) -> tuple[str | None, str | None]:
    for separator in _SEPARATORS:
        parts = separator.split(body, maxsplit=1)
        if len(parts) == 2 and all(p.strip() for p in parts):
            return parts[0].strip(), parts[1].strip()
    by_match = _BY.match(body)
    if by_match:
        return by_match.group("artist").strip(), by_match.group("title").strip()
    return (body.strip() or None), None


def _search_terms(artist: str | None, title: str | None) -> tuple[str, ...]:
    """Query strings to try, most specific first."""
    candidates: list[str] = []
    if artist and title:
        candidates.append(f"{significant(artist)} {significant(title)}")
        candidates.append(f"{artist} {title}")
        candidates.append(significant(title))
        candidates.append(significant(artist))
    elif artist:
        candidates.append(significant(artist))
        candidates.append(artist)
    elif title:
        candidates.append(significant(title))

    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = " ".join(candidate.split())
        key = fold(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            ordered.append(cleaned)
    return tuple(ordered)
