"""Core vocabulary of groove-search.

Every module in the package speaks in these types. They are plain, frozen data:
no I/O, no behaviour beyond arithmetic and formatting, so they are cheap to
construct in tests and safe to pass across every seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Format(StrEnum):
    """Physical carrier the buyer wants."""

    VINYL = "vinyl"
    CD = "cd"
    ANY = "any"


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    PREORDER = "preorder"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An amount in a single currency.

    Comparison is only meaningful within one currency; callers that mix
    currencies must convert first (see `pricing.to_common_currency`).
    """

    amount: Decimal
    currency: str = "PLN"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):  # pragma: no cover - guard
            object.__setattr__(self, "amount", Decimal(str(self.amount)))

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"

    def percent_above(self, other: Money) -> Decimal:
        """How much more expensive `self` is than `other`, in percent."""
        if other.amount <= 0:
            return Decimal(0)
        return (self.amount - other.amount) / other.amount * Decimal(100)


@dataclass(frozen=True, slots=True)
class AlbumQuery:
    """A user's album line, normalized into something shops can be asked about.

    `search_terms` is ordered most-specific first: a runner tries them in turn
    and stops at the first that yields a confident match.
    """

    raw: str
    artist: str | None
    title: str | None
    format: Format = Format.ANY
    search_terms: tuple[str, ...] = ()
    artist_tokens: frozenset[str] = frozenset()
    title_tokens: frozenset[str] = frozenset()

    @property
    def is_artist_only(self) -> bool:
        """True for lines like "Whitest Boy Alive" - no album named."""
        return self.title is None

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.artist or self.title or self.raw


@dataclass(frozen=True, slots=True)
class SavedAlbum:
    """An album somebody added to their streaming library.

    An input, not a result: the point of reading a library is to save the user
    retyping a want-list they have already built somewhere else. It turns into
    an `AlbumQuery` through exactly the same `normalize` path as a typed line,
    so nothing downstream can tell the two apart - and nothing downstream has
    to learn what a streaming service is.
    """

    source: str
    id: str
    artist: str
    title: str
    # ISO 8601, as the service gave it. Empty when the service does not say
    # when the album was added, which is the only honest answer then.
    added_at: str = ""
    url: str = ""
    image_url: str | None = None

    @property
    def query_line(self) -> str:
        """The album as a line the user could have typed themselves.

        Edition noise ("(Deluxe Edition)", "- Remastered 2011") is deliberately
        left in: `text.significant()` strips it from *both* sides of a
        comparison later, and a title cleaned only on this side would fold
        differently from the shop's - the exact fault that made "Burial -
        Untrue EP" unmatchable.
        """
        return f"{self.artist} - {self.title}" if self.artist and self.title else (self.artist or self.title)

    @property
    def added_on(self) -> str:
        """Just the date, for a UI that has one line per album."""
        return self.added_at[:10]


@dataclass(frozen=True, slots=True)
class RawOffer:
    """One product block as extracted from a shop's search page.

    Strings are exactly as the page presented them; nothing is parsed or
    trusted yet. Turning this into an `Offer` is `matching`'s job.
    """

    shop_id: str
    title_text: str
    price_text: str
    url: str
    availability_text: str = ""
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class LandedCost:
    """A listed price turned into what the buyer actually pays.

    Lives here rather than in `shipping` so an `Offer` can carry its own
    delivered cost without the domain vocabulary importing a calculator.
    """

    item: Money
    shipping: Money | None
    import_tax: Money | None
    total: Money
    estimated: bool = True
    notes: tuple[str, ...] = ()

    @property
    def has_extras(self) -> bool:
        return bool((self.shipping and self.shipping.amount) or (self.import_tax and self.import_tax.amount))


@dataclass(frozen=True, slots=True)
class Offer:
    """A priced, format-resolved product that matched an `AlbumQuery`."""

    shop_id: str
    shop_name: str
    title: str
    price: Money
    url: str
    format: Format
    availability: Availability = Availability.UNKNOWN
    confidence: float = 0.0
    image_url: str | None = None
    # Where the shop ships from, and what the record costs delivered. Both are
    # None for an offer nobody has costed yet.
    country: str | None = None
    landed: LandedCost | None = None
    # Which source found this: a calibrated shop recipe, or the open web. The
    # two answer different questions - a shop missing a record it should stock
    # is a broken recipe, the open web missing one is engine coverage - so the
    # reader cannot diagnose a surprising result without being told which.
    from_open_web: bool = False
    # Which engine put this page in front of us, for an open-web offer. Engines
    # disagree about what exists far more than they disagree about ranking, so
    # "only Qwant ever finds this shop" is a fact worth being able to read off
    # a result rather than infer from two runs.
    found_via: str = ""

    @property
    def source_label(self) -> str:
        return "web search" if self.from_open_web else "known shop"

    @property
    def engine_label(self) -> str:
        """The engine that found this, for a UI to show beside the source.

        Deliberately separate from `source_label`: which *kind* of source
        found an offer and which *engine* did are two different questions,
        and only the first one has an answer for every offer.
        """
        return self.found_via if self.from_open_web else ""

    @property
    def comparable_amount(self) -> Decimal:
        """What ranking should use: delivered cost when known, else the price.

        Ranking on the listed price sends a buyer to a $20 record in Boston
        over a 90 PLN one in Warsaw, so the landed total wins whenever it has
        been worked out.
        """
        return self.landed.total.amount if self.landed else self.price.amount

    @property
    def is_buyable(self) -> bool:
        return self.availability in (Availability.IN_STOCK, Availability.PREORDER, Availability.UNKNOWN)


# The open web reports itself alongside the shops, but it is a source rather
# than a shop - it lives here so `AlbumResult` can tell the two apart without
# importing the module that produces it.
OPEN_WEB_SOURCE_ID = "openweb"


@dataclass(frozen=True, slots=True)
class Lead:
    """A page that is this record, but whose price we were not allowed to read.

    Some of the biggest sellers refuse us outright: Allegro answers 403 to
    plain HTTP and to a headless browser alike, and its API grants offer
    search only to partner accounts. Discogs and Boomkat do the same through
    Cloudflare. Dropping those on the floor tells a Polish buyer "not
    available" about a record the country's dominant marketplace is selling.

    A lead is deliberately *not* an `Offer`: it has no price, it is never
    ranked, and it never competes for "best". It is a link that says where
    else to look, which is the most that can honestly be said about a page
    nobody let us open.
    """

    host: str
    url: str
    title: str
    reason: str
    format: Format = Format.ANY


@dataclass(frozen=True, slots=True)
class EngineReport:
    """What one search engine contributed to one album's open-web search.

    A merged search asks several engines and shows their union, which hides
    exactly the thing a user needs when a result surprises them: whether an
    engine found nothing or was never really asked. A captcha, a throttle and
    an honest empty answer all look identical in the union, so each engine
    states its own case here.
    """

    name: str
    hits: int = 0
    # Why this engine contributed nothing, in its own words. None means it
    # answered normally - including answering "no results", which is a real
    # answer and not a fault.
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.note is None

    @property
    def summary(self) -> str:
        if self.note:
            return f"{self.name}: {self.note}"
        return f"{self.name}: {self.hits} hit{'' if self.hits == 1 else 's'}"


@dataclass(frozen=True, slots=True)
class ShopReport:
    """What one shop contributed to one album's search - including failure."""

    shop_id: str
    shop_name: str
    offers_found: int = 0
    error: str | None = None
    # Only the open web fills this in: one entry per engine it asked.
    engines: tuple[EngineReport, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def engines_used(self) -> str:
        """One line naming every engine asked and what each one gave back."""
        return "; ".join(e.summary for e in self.engines)


@dataclass(frozen=True, slots=True)
class AlbumResult:
    """The answer for a single input line: a winner, its rivals, or nothing."""

    query: AlbumQuery
    best: Offer | None = None
    alternatives: tuple[Offer, ...] = ()
    reports: tuple[ShopReport, ...] = ()
    # Sellers that certainly have this record but would not let us read a
    # price. Shown beside the offers, never among them.
    leads: tuple[Lead, ...] = ()

    @property
    def available(self) -> bool:
        return self.best is not None

    @property
    def offers(self) -> tuple[Offer, ...]:
        """Every offer shown, winner first."""
        return ((self.best,) if self.best else ()) + self.alternatives

    @property
    def shop_reports(self) -> tuple[ShopReport, ...]:
        """What the calibrated shops did - the open web is not one of them."""
        return tuple(r for r in self.reports if r.shop_id != OPEN_WEB_SOURCE_ID)

    @property
    def web_report(self) -> ShopReport | None:
        """What the open web did, or None when it was not searched at all."""
        return next((r for r in self.reports if r.shop_id == OPEN_WEB_SOURCE_ID), None)

    @property
    def shops_searched(self) -> int:
        """Shops, counted as shops: the open web is a source, not a shop."""
        return sum(1 for r in self.shop_reports if r.ok)

    @property
    def shops_failed(self) -> tuple[ShopReport, ...]:
        return tuple(r for r in self.reports if not r.ok)

    def savings_vs_worst(self) -> Decimal | None:
        """How much buying the best offer saves against the priciest, in percent.

        Expressed as a share of the priciest offer, so it is always 0-100 -
        the markup the other way round can exceed 100 and reads as nonsense.
        """
        if not self.best or not self.alternatives:
            return None
        rivals = self.alternatives
        if self.best.landed and all(a.landed for a in rivals):
            # Delivered totals are already in one currency, so every offer is
            # comparable - including the foreign ones a price-only comparison
            # had to discard.
            best_amount = self.best.comparable_amount
            worst_amount = max(a.comparable_amount for a in rivals)
        else:
            same_currency = [a for a in rivals if a.price.currency == self.best.price.currency]
            if not same_currency:
                return None
            best_amount = self.best.price.amount
            worst_amount = max(a.price.amount for a in same_currency)
        if worst_amount <= 0:
            return None
        return (worst_amount - best_amount) / worst_amount * Decimal(100)


@dataclass(frozen=True, slots=True)
class Shop:
    """An online record shop, independent of how we search it."""

    id: str
    name: str
    base_url: str
    country: str = "PL"
    currency: str = "PLN"
    needs_browser: bool = False
    note: str = ""
    # Optional known search URL template, tried before anything is guessed.
    # Lets a user teach the app an endpoint without touching code.
    search_hint: str = ""
