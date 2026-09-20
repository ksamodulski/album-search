"""The registry: which shops groove-search knows, and how to search them.

One JSON file holds the shops, their learned recipes, and a health record per
shop. It is the app's only persistent state, so a broken shop can be spotted,
re-learned and re-saved without touching code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .domain import Shop
from .recipes import SearchRecipe
from .shipping import ShippingRates

DEFAULT_PATH = Path("var/registry.json")

# Rough PLN value of one unit. Editable in registry.json; only used to compare
# offers priced in different currencies, never to display a converted price.
DEFAULT_RATES: dict[str, str] = {"PLN": "1.00", "EUR": "4.30", "USD": "3.95", "GBP": "5.05", "CZK": "0.17"}

# A shop is considered broken after this many consecutive failed searches.
BROKEN_AFTER = 3

# A recipe younger than this is not worth re-learning when it breaks. A recipe
# that worked a fortnight ago and fails today is rarely stale - the shop is
# refusing us (rate limit, bot block) - and re-probing it costs minutes and
# usually deepens the block. Treat such a shop as simply not working until its
# recipe is old enough that a redesign is the likelier explanation.
RECALIBRATE_AFTER = timedelta(days=30)


@dataclass
class ShopHealth:
    """Rolling record of whether a shop's recipe still works."""

    shop_id: str
    consecutive_failures: int = 0
    last_ok: str | None = None
    last_error: str | None = None
    last_offers: int = 0
    coverage: float = 0.0

    @property
    def status(self) -> str:
        if self.consecutive_failures >= BROKEN_AFTER:
            return "broken"
        if self.consecutive_failures > 0:
            return "degraded"
        return "healthy"

    @property
    def needs_recalibration(self) -> bool:
        return self.status == "broken"


@dataclass
class Registry:
    """Shops + recipes + health, loaded from and saved to one JSON file."""

    location: str = "PL"
    currency: str = "PLN"
    shops: list[Shop] = field(default_factory=list)
    recipes: dict[str, SearchRecipe] = field(default_factory=dict)
    health: dict[str, ShopHealth] = field(default_factory=dict)
    rates: dict[str, Decimal] = field(default_factory=lambda: {k: Decimal(v) for k, v in DEFAULT_RATES.items()})
    shipping: ShippingRates = field(default_factory=ShippingRates)
    updated_at: str = ""
    path: Path = field(default=DEFAULT_PATH, compare=False)

    # --- lifecycle -----------------------------------------------------
    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> Registry:
        """Read the registry, or return an empty one if setup has not run."""
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            location=data.get("location", "PL"),
            currency=data.get("currency", "PLN"),
            shops=[Shop(**s) for s in data.get("shops", [])],
            recipes={k: SearchRecipe.from_dict(v) for k, v in data.get("recipes", {}).items()},
            health={k: ShopHealth(**v) for k, v in data.get("health", {}).items()},
            rates={k: Decimal(str(v)) for k, v in data.get("rates", DEFAULT_RATES).items()},
            shipping=ShippingRates.from_dict(data.get("shipping")),
            updated_at=data.get("updated_at", ""),
            path=path,
        )

    def save(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        payload = {
            "location": self.location,
            "currency": self.currency,
            "updated_at": self.updated_at,
            "shops": [asdict(s) for s in self.shops],
            "recipes": {k: v.to_dict() for k, v in self.recipes.items()},
            "health": {k: asdict(v) for k, v in self.health.items()},
            "rates": {k: str(v) for k, v in self.rates.items()},
            "shipping": self.shipping.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # --- queries -------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.searchable)

    @property
    def searchable(self) -> list[Shop]:
        """Shops with a recipe - the ones a search can actually use."""
        return [s for s in self.shops if s.id in self.recipes]

    @property
    def broken(self) -> list[Shop]:
        return [s for s in self.shops if self.health_of(s.id).needs_recalibration]

    @property
    def repairable(self) -> list[Shop]:
        """Broken shops whose recipe is old enough to be worth re-learning."""
        return [s for s in self.broken if not self.repair_blocked(s.id)]

    def repair_blocked(self, shop_id: str, *, now: datetime | None = None) -> bool:
        """Is re-learning this shop a waste of time right now?

        Only a *repair* is blocked. Re-learning a working shop on purpose - the
        usual answer to a redesign - is always allowed; it is the automatic
        repair of a shop that broke within its own recipe's lifetime that we
        decline, because the recipe is not what went wrong.
        """
        return self.health_of(shop_id).needs_recalibration and self.in_cooldown(shop_id, now=now)

    def in_cooldown(self, shop_id: str, *, now: datetime | None = None) -> bool:
        """Is this shop's recipe too young to blame for the breakage?

        See `RECALIBRATE_AFTER`. A recipe with no timestamp is treated as old,
        so an unstamped recipe is always repairable.
        """
        recipe = self.recipes.get(shop_id)
        if recipe is None:
            return False
        age = _age_of(recipe.calibrated_at, now=now)
        return age is not None and age < RECALIBRATE_AFTER

    def calibrated_age(self, shop_id: str, *, now: datetime | None = None) -> timedelta | None:
        recipe = self.recipes.get(shop_id)
        return _age_of(recipe.calibrated_at, now=now) if recipe else None

    def shop(self, shop_id: str) -> Shop | None:
        return next((s for s in self.shops if s.id == shop_id), None)

    def health_of(self, shop_id: str) -> ShopHealth:
        return self.health.setdefault(shop_id, ShopHealth(shop_id=shop_id))

    # --- updates -------------------------------------------------------
    def adopt(self, shop: Shop, recipe: SearchRecipe, coverage: float = 0.0) -> None:
        """Add or replace a shop and the recipe learned for it."""
        self.shops = [s for s in self.shops if s.id != shop.id] + [shop]
        self.recipes[shop.id] = recipe
        health = self.health_of(shop.id)
        health.consecutive_failures = 0
        health.last_error = None
        health.coverage = coverage
        health.last_ok = datetime.now(UTC).isoformat(timespec="seconds")

    def drop(self, shop_id: str) -> None:
        self.shops = [s for s in self.shops if s.id != shop_id]
        self.recipes.pop(shop_id, None)
        self.health.pop(shop_id, None)

    def record_success(self, shop_id: str, offers: int) -> None:
        health = self.health_of(shop_id)
        health.consecutive_failures = 0
        health.last_error = None
        health.last_offers = offers
        health.last_ok = datetime.now(UTC).isoformat(timespec="seconds")

    def record_failure(self, shop_id: str, error: str) -> None:
        health = self.health_of(shop_id)
        health.consecutive_failures += 1
        health.last_error = error


def _age_of(stamp: str, *, now: datetime | None = None) -> timedelta | None:
    """How long ago an ISO timestamp was, or None if it is missing or unreadable."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - when
