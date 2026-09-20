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

    @property
    def source_label(self) -> str:
        return "web search" if self.from_open_web else "known shop"

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
class ShopReport:
    """What one shop contributed to one album's search - including failure."""

    shop_id: str
    shop_name: str
    offers_found: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class AlbumResult:
    """The answer for a single input line: a winner, its rivals, or nothing."""

    query: AlbumQuery
    best: Offer | None = None
    alternatives: tuple[Offer, ...] = ()
    reports: tuple[ShopReport, ...] = ()

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
