#!/usr/bin/env python3
"""
Validate MeshCore Bot config.ini section names.

Run standalone: python validate_config.py [--config config.ini]
Exits with 1 if any errors are found, 0 otherwise. Warnings and info are printed but do not affect exit code.

Use --strict to also fail on "Unknown section" info messages. This is intended for
CI checks against the shipped example configs (config.ini.example, etc.), which should
never contain a section outside the canonical list (see CANONICAL_NON_COMMAND_SECTIONS
in modules/config_validation.py). It is not recommended for validating end-user configs,
since those may legitimately contain sections for local/third-party plugins.
"""

import argparse
import sys

from modules.config_validation import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate MeshCore Bot config.ini section names"
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to configuration file (default: config.ini)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Also fail (exit 1) on 'Unknown section' info messages. Intended for "
            "CI validation of the shipped example configs; not recommended for "
            "end-user configs, which may have local/third-party plugin sections."
        ),
    )
    args = parser.parse_args()

    results = validate_config(args.config)
    has_error = False
    for severity, message in results:
        if severity == SEVERITY_ERROR:
            print(f"Error: {message}", file=sys.stderr)
            has_error = True
        elif severity == SEVERITY_WARNING:
            print(f"Warning: {message}", file=sys.stderr)
        else:
            print(f"Info: {message}", file=sys.stderr)
            if args.strict and "Unknown section" in message:
                has_error = True

    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
