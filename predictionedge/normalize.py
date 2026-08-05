"""Team-name normalisation for cross-venue matching.

Sportsbooks say "Los Angeles Lakers", Kalshi might say "Lakers" or "LAL". We reduce
names to a token set (city words + nickname, aliases expanded) and score two names
by the **overlap coefficient** - so "Lakers" scores 1.0 against "Los Angeles Lakers"
(subset), while "Yankees" scores 0 against "Mets". Single-team scores are only used
inside a dual-team event match (both teams must line up), which makes a stray
partial overlap (e.g. two New York teams sharing "new york") harmless.
"""

from __future__ import annotations

import re

_STOPWORDS = {"the", "fc", "afc", "sc", "cf", "club"}

# Code/variant -> canonical nickname token. Extended once the sports-data research
# lands; this covers the common cases the tests exercise.
_ALIASES = {
    "lal": "lakers", "bos": "celtics", "gsw": "warriors", "lac": "clippers",
    "nyy": "yankees", "nym": "mets", "phi": "76ers", "sixers": "76ers",
    "sf": "49ers", "ne": "patriots", "gb": "packers", "kc": "chiefs",
}


def normalize(name: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


def tokens(name: str) -> frozenset[str]:
    """Significant tokens of a team name, with aliases expanded and stopwords dropped."""
    out: set[str] = set()
    for tok in normalize(name).split():
        if tok in _STOPWORDS:
            continue
        out.add(_ALIASES.get(tok, tok))
    return frozenset(out)


def team_score(a: str, b: str) -> float:
    """Overlap coefficient of two team names: |A∩B| / min(|A|, |B|), in [0, 1]."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))
