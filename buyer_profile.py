#!/usr/bin/env python3
"""Personal buyer profile — WHO is buying, WHAT, and WHERE.

This repo was built around one household (an SC buyer, investment-first, the
his/hers mandates in weekly.py). A fork used by a different buyer needs those
facts to be data, not code. This module reads them from a git-ignored
``profile.local.json`` next to this file, so:

  * a fork can carry its own buyer without editing tracked code, and upstream
    merges stay clean;
  * when NO profile is present, ``active()`` returns None and every caller
    keeps the owner's original behaviour byte-for-byte.

Deliberately NOT config.py: config.py is sha-hashed into ``score_version``, so
a buyer fact living there would restamp the vintage of the whole scored book.
Nothing in this file changes how a listing is SCORED — it changes who the
costs are computed for (ABSD), which listings a search keeps, and which ECs
the household may legally buy.

Lookup order:
  1. ``PF_PROFILE`` env var — a path, or one of none/off/0/false to disable
     (the test suite sets ``PF_PROFILE=none`` so owner-shaped tests stay valid);
  2. ``profile.local.json`` in the repo root, when it exists.

    python buyer_profile.py            # show the active profile
    python buyer_profile.py --check    # validate it, exit 1 on error
    python buyer_profile.py --init     # copy profile.example.json -> profile.local.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE_ENV = "PF_PROFILE"
DEFAULT_FILE = os.path.join(BASE, "profile.local.json")
EXAMPLE_FILE = os.path.join(BASE, "profile.example.json")
_DISABLED = {"", "none", "off", "0", "false"}

VALID_PURPOSES = ("own_stay", "investment")
VALID_BUYER_TYPES = ("SC", "PR", "Foreigner")
# Household citizenship decides EC eligibility: a NEW EC needs at least one
# Singapore Citizen applicant ("SC"); a PR-only household ("PR") may only buy
# an EC on the resale market after its MOP; foreigners only once privatised.
VALID_CITIZENSHIP = ("SC", "PR", "Foreigner")
VALID_PROPERTY_KINDS = ("condo", "ec")


class ProfileError(ValueError):
    """The profile file exists but is not usable — fail loudly, never guess."""


def property_kind(property_type: str | None) -> str | None:
    """Map a listing's scraped property_type to a profile kind ('condo'/'ec').

    Landed / cluster houses map to None: no mandate here is written for them.
    """
    t = (property_type or "").strip().lower()
    if not t:
        return None
    if t == "executive condominium" or t == "ec":
        return "ec"
    if t in ("condominium", "apartment", "walk-up apartment", "condo"):
        return "condo"
    return None


def _district_num(district) -> int | None:
    digits = "".join(c for c in str(district or "") if c.isdigit())
    return int(digits) if digits else None


@dataclass(frozen=True)
class Mandate:
    label: str
    max_price: int
    beds: tuple[int, ...]
    property_types: tuple[str, ...] = ("condo",)
    who: str = ""

    def as_tuple(self) -> tuple:
        """The (label, max_price, beds, who) shape weekly.MANDATES uses."""
        return (self.label, self.max_price, self.beds, self.who)

    def accepts(self, price, beds, property_type: str | None = None) -> bool:
        if price is None or beds is None or price > self.max_price or beds not in self.beds:
            return False
        if property_type is None:
            return True
        return property_kind(property_type) in self.property_types


@dataclass(frozen=True)
class Anchors:
    workplace_postal: str | None = None
    workplace_label: str | None = None
    commute_depart: str = "08:30"
    school_radii_m: tuple[int, ...] = (1000, 2000)

    @property
    def has_workplace(self) -> bool:
        return bool(self.workplace_postal)


@dataclass(frozen=True)
class Profile:
    name: str
    purpose: str
    buyer_type: str
    property_count: int
    household_citizenship: str
    mandates: tuple[Mandate, ...]
    districts: tuple[int, ...]
    anchors: Anchors = field(default_factory=Anchors)
    hold_years: tuple[int, ...] | None = None
    path: str | None = None

    def in_region(self, district) -> bool:
        n = _district_num(district)
        return n is not None and n in self.districts

    def mandate_for(self, price, beds, district=None, property_type=None) -> str | None:
        """Cheapest-fitting mandate label, or None (same rule as weekly.mandate_for)."""
        if district is not None and not self.in_region(district):
            return None
        for m in sorted(self.mandates, key=lambda m: m.max_price):
            if m.accepts(price, beds, property_type):
                return m.label
        return None

    @property
    def wants_ec(self) -> bool:
        return any("ec" in m.property_types for m in self.mandates)

    def summary(self) -> str:
        ms = "; ".join(
            f"{m.label}: {'/'.join(map(str, m.beds))}BR <= ${m.max_price / 1e6:.2f}M "
            f"[{'+'.join(m.property_types)}]" for m in self.mandates)
        d = ",".join(f"D{x:02d}" for x in self.districts)
        return (f"{self.name} — purpose={self.purpose}, buyer={self.buyer_type} "
                f"(property #{self.property_count + 1}), household={self.household_citizenship}; "
                f"{ms}; districts {d}")


def _as_int_tuple(v, what: str) -> tuple[int, ...]:
    if not isinstance(v, (list, tuple)) or not v:
        raise ProfileError(f"{what} must be a non-empty list")
    try:
        return tuple(int(x) for x in v)
    except (TypeError, ValueError):
        raise ProfileError(f"{what} must be a list of integers") from None


def parse(data: dict, path: str | None = None) -> Profile:
    """Validate a profile dict. Raises ProfileError with a precise message."""
    if not isinstance(data, dict):
        raise ProfileError("profile must be a JSON object")
    purpose = data.get("purpose", "investment")
    if purpose not in VALID_PURPOSES:
        raise ProfileError(f"purpose must be one of {VALID_PURPOSES}, got {purpose!r}")
    buyer_type = data.get("buyer_type", "SC")
    if buyer_type not in VALID_BUYER_TYPES:
        raise ProfileError(f"buyer_type must be one of {VALID_BUYER_TYPES}, got {buyer_type!r}")
    try:
        property_count = int(data.get("property_count", 0))
    except (TypeError, ValueError):
        raise ProfileError("property_count must be an integer") from None
    if property_count < 0:
        raise ProfileError("property_count must be >= 0")
    citizenship = data.get("household_citizenship", buyer_type)
    if citizenship not in VALID_CITIZENSHIP:
        raise ProfileError(
            f"household_citizenship must be one of {VALID_CITIZENSHIP}, got {citizenship!r}")

    raw_mandates = data.get("mandates")
    if not isinstance(raw_mandates, list) or not raw_mandates:
        raise ProfileError("mandates must be a non-empty list")
    mandates, labels = [], set()
    for i, m in enumerate(raw_mandates):
        if not isinstance(m, dict):
            raise ProfileError(f"mandates[{i}] must be an object")
        label = str(m.get("label") or "").strip()
        if not label or label in labels:
            raise ProfileError(f"mandates[{i}].label must be a unique non-empty string")
        labels.add(label)
        try:
            max_price = int(m["max_price"])
        except (KeyError, TypeError, ValueError):
            raise ProfileError(f"mandates[{i}].max_price must be an integer (SGD)") from None
        if max_price <= 0:
            raise ProfileError(f"mandates[{i}].max_price must be positive")
        beds = _as_int_tuple(m.get("beds"), f"mandates[{i}].beds")
        kinds = tuple(m.get("property_types") or ("condo",))
        bad = [k for k in kinds if k not in VALID_PROPERTY_KINDS]
        if bad:
            raise ProfileError(
                f"mandates[{i}].property_types has {bad}; allowed {VALID_PROPERTY_KINDS}")
        mandates.append(Mandate(label, max_price, beds, kinds, str(m.get("who") or "")))

    districts = _as_int_tuple(data.get("districts"), "districts")
    if any(d < 1 or d > 28 for d in districts):
        raise ProfileError("districts must be postal districts 1-28")

    a = data.get("anchors") or {}
    if not isinstance(a, dict):
        raise ProfileError("anchors must be an object")
    postal = a.get("workplace_postal")
    if postal is not None:
        postal = str(postal).strip()
        if postal and (len(postal) != 6 or not postal.isdigit()):
            raise ProfileError("anchors.workplace_postal must be a 6-digit postal code")
        postal = postal or None
    radii = tuple(a.get("school_radii_m") or (1000, 2000))
    anchors = Anchors(
        workplace_postal=postal,
        workplace_label=a.get("workplace_label"),
        commute_depart=str(a.get("commute_depart") or "08:30"),
        school_radii_m=tuple(int(r) for r in radii),
    )
    hold = data.get("hold_years")
    hold_years = _as_int_tuple(hold, "hold_years") if hold else None

    return Profile(
        name=str(data.get("name") or "my profile"),
        purpose=purpose,
        buyer_type=buyer_type,
        property_count=property_count,
        household_citizenship=citizenship,
        mandates=tuple(mandates),
        districts=tuple(sorted(set(districts))),
        anchors=anchors,
        hold_years=hold_years,
        path=path,
    )


def load(path: str) -> Profile:
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ProfileError(f"{path}: invalid JSON ({e})") from None
    return parse(data, path)


_UNSET = object()
_cache: object = _UNSET


def _resolve_path() -> str | None:
    env = os.environ.get(PROFILE_ENV)
    if env is not None:
        if env.strip().lower() in _DISABLED:
            return None
        return os.path.expanduser(env)
    return DEFAULT_FILE if os.path.exists(DEFAULT_FILE) else None


def active() -> Profile | None:
    """The active profile, or None (= owner defaults everywhere). Cached."""
    global _cache
    if _cache is _UNSET:
        path = _resolve_path()
        _cache = load(path) if path else None
    return _cache  # type: ignore[return-value]


def reset_cache() -> None:
    global _cache
    _cache = _UNSET


def buyer_defaults() -> tuple[str, int]:
    """(buyer_type, property_count) for cost/ABSD maths. ('SC', 0) without a profile."""
    p = active()
    return (p.buyer_type, p.property_count) if p else ("SC", 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="validate; exit 1 on error")
    ap.add_argument("--init", action="store_true",
                    help="copy profile.example.json to profile.local.json (won't overwrite)")
    args = ap.parse_args()
    if args.init:
        if os.path.exists(DEFAULT_FILE):
            print(f"{DEFAULT_FILE} already exists — edit it instead")
            return 1
        shutil.copy(EXAMPLE_FILE, DEFAULT_FILE)
        print(f"created {DEFAULT_FILE} — edit it, then run: python buyer_profile.py --check")
        return 0
    try:
        p = active()
    except (ProfileError, OSError) as e:
        print(f"profile error: {e}", file=sys.stderr)
        return 1
    if p is None:
        print("no profile active — owner defaults apply (see profile.example.json)")
        return 0
    print(p.summary())
    print(f"  file: {p.path}")
    if not p.anchors.has_workplace:
        print("  note: anchors.workplace_postal not set — commute is skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
