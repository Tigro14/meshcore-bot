# Solar conditions rewrite provenance

`modules/solar_conditions.py` was replaced on 2026-07-28 with an independent
implementation. The rewrite did not inspect the former function bodies or the
upstream GPL source. The author had received a provenance report that named the
affected public functions and included one short comparison excerpt.

The replacement behavior was derived from:

- MeshCore Bot command call sites, configuration, and documented output fields.
- Black-box observations of the former module's public return values.
- HamQSL's public `solarxml.php` response schema.
- NOAA SWPC's public `drap_global_frequencies.txt` response schema.
- N2YO's REST API v1 documentation.
- PyEphem's public observer, rise/set, lunar phase, and date documentation.

The replacement retains the module's public function signatures and short
radio-oriented return formats. Regression tests use synthetic HamQSL, NOAA, and
N2YO responses and do not contain upstream implementation code.

The public constant `ERROR_FETCHING_DATA = "Error fetching data"` intentionally
retains the same generic name and value as the upstream module. It is part of
the established failure-return contract used by command-facing helpers, so it
is documented here rather than changed along with their callers.

This record documents engineering provenance; it is not a legal conclusion
about the repository's licensing status.
