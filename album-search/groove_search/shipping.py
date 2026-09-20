"""What an offer actually costs to get to your door.

A record listed at £22.49 in the UK is not cheaper than one at 120 PLN in
Poland. Cross-border postage, and outside the EU import VAT, routinely add
more than the price gap being compared - so ranking on the listed price alone
is not merely incomplete, it is wrong often enough to send the buyer to the
wrong shop. Once the open web is a source, most candidates are foreign and
this stops being a refinement.

Every number here is an estimate and says so. The aim is not to predict a
courier's invoice to the grosz; it is to stop a $20 record in Boston
outranking a 90 PLN copy in Warsaw when it will cost 230 PLN delivered.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .domain import Format, LandedCost, Money
from .pricing import convert

# Countries whose parcels reach an EU destination without customs. Not the
# full membership list's business - only what changes the arithmetic.
# "EU" itself is accepted as an origin: a shop that prices in euros but
# hides its country is still inside the customs union, which is the only
# thing the arithmetic below needs to know.
EU: frozenset[str] = frozenset(
    "EU AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)

# Import VAT charged by the destination when goods arrive from outside the EU,
# levied on price + shipping, plus the courier's flat clearance fee.
IMPORT_VAT: dict[str, Decimal] = {"PL": Decimal("0.23"), "DE": Decimal("0.19"), "CZ": Decimal("0.21")}
CLEARANCE_FEE: Decimal = Decimal("10")


class Zone(StrEnum):
    """How far a parcel has to travel, which is all postage really keys on."""

    DOMESTIC = "domestic"
    EU = "eu"
    WORLD = "world"


@dataclass(frozen=True, slots=True)
class ShippingRates:
    """Postage per zone and carrier, in the registry's own currency.

    Held as plain data so it can live in `registry.json` and be corrected by a
    user who knows better than these defaults, without touching code.
    """

    currency: str = "PLN"
    vinyl: dict[str, Decimal] = None  # type: ignore[assignment]
    cd: dict[str, Decimal] = None  # type: ignore[assignment]
    free_over_domestic: Decimal | None = Decimal("200")

    def __post_init__(self) -> None:
        # A vinyl LP posts at roughly twice a CD's weight and three times its
        # bulk, which is why the two carriers are priced apart rather than
        # averaged into one "media" rate.
        if self.vinyl is None:
            object.__setattr__(
                self, "vinyl", {Zone.DOMESTIC: Decimal("15"), Zone.EU: Decimal("50"), Zone.WORLD: Decimal("90")}
            )
        if self.cd is None:
            object.__setattr__(
                self, "cd", {Zone.DOMESTIC: Decimal("12"), Zone.EU: Decimal("35"), Zone.WORLD: Decimal("55")}
            )

    def cost(self, zone: Zone, fmt: Format) -> Decimal:
        table = self.cd if fmt is Format.CD else self.vinyl
        return table.get(zone, table[Zone.WORLD])

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "vinyl": {k: str(v) for k, v in self.vinyl.items()},
            "cd": {k: str(v) for k, v in self.cd.items()},
            "free_over_domestic": str(self.free_over_domestic) if self.free_over_domestic is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ShippingRates:
        if not data:
            return cls()
        free = data.get("free_over_domestic")
        return cls(
            currency=data.get("currency", "PLN"),
            vinyl={k: Decimal(str(v)) for k, v in (data.get("vinyl") or {}).items()} or None,
            cd={k: Decimal(str(v)) for k, v in (data.get("cd") or {}).items()} or None,
            free_over_domestic=Decimal(str(free)) if free is not None else None,
        )


def zone_between(origin: str | None, destination: str) -> Zone:
    """Which postage zone a parcel from `origin` falls into.

    An unknown origin is treated as WORLD: guessing the cheap end would let an
    unidentified foreign shop win on a domestic postage estimate, which is the
    exact error this module exists to prevent.
    """
    if not origin:
        return Zone.WORLD
    origin, destination = origin.upper(), destination.upper()
    if origin == destination:
        return Zone.DOMESTIC
    if origin in EU and destination in EU:
        return Zone.EU
    return Zone.WORLD


def landed_cost(
    price: Money,
    *,
    origin: str | None,
    destination: str = "PL",
    fmt: Format = Format.ANY,
    rates: ShippingRates | None = None,
    fx: dict[str, Decimal] | None = None,
    target_currency: str = "PLN",
) -> LandedCost:
    """Price + postage + any import tax, all converted to `target_currency`."""
    rates = rates or ShippingRates(currency=target_currency)
    fx = fx or {}
    zone = zone_between(origin, destination)
    item = convert(price, target_currency, fx)
    notes: list[str] = []

    postage = Money(rates.cost(zone, fmt), rates.currency)
    if zone is Zone.DOMESTIC and rates.free_over_domestic is not None and item.amount >= rates.free_over_domestic:
        postage = Money(Decimal("0"), rates.currency)
        notes.append(f"free delivery over {rates.free_over_domestic:.0f} {rates.currency}")
    postage = convert(postage, target_currency, fx)

    tax: Money | None = None
    if zone is Zone.WORLD and destination.upper() in EU:
        vat = IMPORT_VAT.get(destination.upper())
        if vat is not None:
            due = (item.amount + postage.amount) * vat + CLEARANCE_FEE
            tax = Money(due.quantize(Decimal("0.01")), target_currency)
            notes.append(f"import VAT {vat:.0%} + {CLEARANCE_FEE:.0f} {target_currency} clearance")
    if origin is None:
        notes.append("shop country unknown - priced as worst case")

    total = Money(
        (item.amount + postage.amount + (tax.amount if tax else Decimal("0"))).quantize(Decimal("0.01")),
        target_currency,
    )
    return LandedCost(
        item=item, shipping=postage, import_tax=tax, total=total, estimated=True, notes=tuple(notes)
    )
