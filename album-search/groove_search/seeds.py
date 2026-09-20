"""Candidate shops per location, read from the bundled YAML seed files."""

from __future__ import annotations

from pathlib import Path

import yaml

from .domain import Shop

SEED_DIR = Path(__file__).parent / "data" / "seeds"


def available_locations() -> list[str]:
    return sorted(p.stem.upper() for p in SEED_DIR.glob("*.yaml"))


def load_seeds(location: str = "PL") -> tuple[list[Shop], str]:
    """Candidate shops for a location, plus that location's currency."""
    path = SEED_DIR / f"{location.lower()}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"no seed list for {location!r}; available: {', '.join(available_locations())}"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    currency = data.get("currency", "PLN")
    shops = [
        Shop(
            id=entry["id"],
            name=entry["name"],
            base_url=entry["base_url"].rstrip("/"),
            country=entry.get("country", data.get("location", location)),
            currency=entry.get("currency", currency),
            needs_browser=entry.get("needs_browser", False),
            note=entry.get("note", ""),
            search_hint=entry.get("search_hint", ""),
        )
        for entry in data.get("shops", [])
    ]
    return shops, currency
