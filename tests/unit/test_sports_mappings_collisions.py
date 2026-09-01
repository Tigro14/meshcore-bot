"""Guards against silently shadowed TEAM_MAPPINGS aliases.

``TEAM_MAPPINGS`` is one flat dict literal, so a repeated key is not an error —
the later entry just wins and the earlier team's alias disappears with no
warning. These tests pin the two things that matter: unique team nicknames must
not collide at all, and the set of genuinely ambiguous city/abbreviation aliases
must not grow without someone deciding which team should win.
"""

import ast
import collections
import pathlib

from modules.clients.sports_mappings import TEAM_MAPPINGS

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "modules" / "clients" / "sports_mappings.py"

# City names and league abbreviations that legitimately refer to different teams
# in different sports ("chicago" is Bears, Cubs, Bulls and Blackhawks). These
# still resolve last-wins; they are recorded here so a NEW collision — which is
# almost always an accident — fails this test instead of passing unnoticed.
KNOWN_AMBIGUOUS_ALIASES = frozenset({
    'atl', 'atlanta', 'bal', 'baltimore', 'bos', 'buf', 'buffalo', 'chi',
    'chicago', 'cin', 'cincinnati', 'cle', 'cleveland', 'col', 'colorado',
    'columbus', 'dal', 'dallas', 'det', 'detroit', 'hou', 'houston',
    'kansas city', 'kc', 'la', 'mia', 'miami', 'min', 'minnesota', 'montreal',
    'mtl', 'nashville', 'ne', 'new england', 'nsh', 'phi', 'philadelphia',
    'pit', 'pittsburgh', 'san diego', 'san jose', 'sd', 'seattle', 'sf', 'sj',
    'st louis', 'stl', 'tampa bay', 'tb', 'tor', 'toronto', 'van', 'vancouver',
    'washington', 'wsh',
})

# Unique nicknames that used to collide across leagues. Each resolves to the
# first definition in the file; the shadowed team keeps an unambiguous full-name
# alias (asserted below).
EXPECTED_NICKNAMES = {
    'hawks': ('football', 'nfl'),      # Seahawks, not Atlanta Hawks
    'giants': ('football', 'nfl'),     # NY Giants, not SF Giants
    'jets': ('football', 'nfl'),       # NY Jets, not Winnipeg Jets
    'rangers': ('baseball', 'mlb'),    # Texas Rangers, not NY Rangers
    'kings': ('basketball', 'nba'),    # Sacramento Kings, not LA Kings
    'blazers': ('basketball', 'nba'),  # Portland Trail Blazers, not Kamloops
    'rockets': ('basketball', 'nba'),  # Houston Rockets, not Kelowna
}

# The team each nickname no longer resolves to must stay reachable by full name.
SHADOWED_TEAM_ALIASES = {
    'atlanta hawks': ('basketball', 'nba'),
    'san francisco': ('baseball', 'mlb'),
    'winnipeg': ('hockey', 'nhl'),
    'new york rangers': ('hockey', 'nhl'),
    'los angeles': ('hockey', 'nhl'),
    'kamloops blazers': ('hockey', 'whl'),
    'kelowna rockets': ('hockey', 'whl'),
}


def _literal_keys() -> list[str]:
    """Every key written in the TEAM_MAPPINGS literal, duplicates included.

    Read from the AST rather than the dict, because building the dict is exactly
    what collapses duplicates.
    """
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "TEAM_MAPPINGS" for target in node.targets
        ):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError("TEAM_MAPPINGS literal not found in sports_mappings.py")


def test_no_unexpected_duplicate_aliases():
    counts = collections.Counter(_literal_keys())
    duplicates = {key for key, n in counts.items() if n > 1}
    unexpected = duplicates - KNOWN_AMBIGUOUS_ALIASES
    assert not unexpected, (
        f"New shadowed TEAM_MAPPINGS alias(es): {sorted(unexpected)}. "
        "A repeated key silently drops the earlier team. Rename one side, or add "
        "it to KNOWN_AMBIGUOUS_ALIASES if the collision is genuinely intended."
    )


def test_known_ambiguous_list_has_no_stale_entries():
    """A resolved collision should be removed from the allow-list."""
    counts = collections.Counter(_literal_keys())
    duplicates = {key for key, n in counts.items() if n > 1}
    stale = KNOWN_AMBIGUOUS_ALIASES - duplicates
    assert not stale, (
        f"KNOWN_AMBIGUOUS_ALIASES lists non-colliding key(s): {sorted(stale)}. "
        "Drop them from the allow-list."
    )


def test_unique_nicknames_do_not_collide():
    counts = collections.Counter(_literal_keys())
    for nickname in EXPECTED_NICKNAMES:
        assert counts[nickname] == 1, (
            f"{nickname!r} is defined {counts[nickname]} times; a unique team "
            "nickname must map to exactly one team."
        )


def test_unique_nicknames_resolve_to_expected_league():
    for nickname, (sport, league) in EXPECTED_NICKNAMES.items():
        entry = TEAM_MAPPINGS.get(nickname)
        assert entry is not None, f"{nickname!r} disappeared from TEAM_MAPPINGS"
        assert (entry["sport"], entry["league"]) == (sport, league), (
            f"{nickname!r} resolves to {entry['sport']}/{entry['league']}, "
            f"expected {sport}/{league}"
        )


def test_shadowed_teams_remain_reachable():
    """Removing a colliding nickname must not orphan the other team."""
    for alias, (sport, league) in SHADOWED_TEAM_ALIASES.items():
        entry = TEAM_MAPPINGS.get(alias)
        assert entry is not None, f"{alias!r} is the only alias left for its team"
        assert (entry["sport"], entry["league"]) == (sport, league)
