#!/usr/bin/env python3
"""Verify the base wheel behaves safely without optional geocoding packages."""

from __future__ import annotations

import importlib.util


def main() -> None:
    unexpectedly_installed = [
        name for name in ("pycountry", "us") if importlib.util.find_spec(name) is not None
    ]
    if unexpectedly_installed:
        raise SystemExit(
            "base wheel unexpectedly installed geo extras: "
            + ", ".join(unexpectedly_installed)
        )

    from modules.utils import (
        PYCOUNTRY_AVAILABLE,
        US_AVAILABLE,
        normalize_country_name,
        normalize_us_state,
    )

    if PYCOUNTRY_AVAILABLE or US_AVAILABLE:
        raise SystemExit("geo availability flags do not match the clean environment")
    if normalize_country_name("United States") != (None, None):
        raise SystemExit("country normalization did not use the no-geo fallback")
    if normalize_us_state("Washington") != (None, None):
        raise SystemExit("state normalization did not use the no-geo fallback")

    print("base-wheel no-geo fallback passed")


if __name__ == "__main__":
    main()
