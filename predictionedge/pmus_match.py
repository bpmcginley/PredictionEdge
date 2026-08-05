"""Match an international-Polymarket market to its Polymarket US counterpart.

The whale signal comes from international Polymarket (slugs like
`fifwc-ecu-ger-2026-06-25-ger`); execution is on Polymarket US (slugs like
`atc-fwc-ecu-ger-2026-06-25-ger`). The sport prefix differs, but the meaningful
components - the team codes, the date, and the outcome token - line up. We build a
signature (team-code set, date, outcome) from each slug and match on it, which is far
safer than fuzzy title matching for picking the exact same contract.
"""

from __future__ import annotations

import re

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def slug_signature(slug: str) -> tuple[frozenset, str, str] | None:
    """(team-code set, date, outcome) for a dated game slug, or None."""
    if not slug:
        return None
    m = _DATE.search(slug)
    if not m:
        return None
    date = m.group(1)
    pre = [t for t in slug[:m.start()].strip("-").split("-") if t]
    post = [t for t in slug[m.end():].strip("-").split("-") if t]
    # Team codes = trailing 2-4 letter alpha tokens before the date (drops sport prefix
    # tokens like 'fifwc'/'atc'/'fwc' which are len 5 or part of the prefix run).
    teams = [t for t in pre if t.isalpha() and 2 <= len(t) <= 4][-2:]
    if len(teams) < 2:
        return None
    outcome = post[-1] if post else ""
    return frozenset(teams), date, outcome


class PMUSMatcher:
    """Index PM-US slugs by signature; look up the counterpart of an intl slug."""

    def __init__(self, pmus_slugs):
        self.index: dict = {}
        for slug in pmus_slugs:
            sig = slug_signature(slug)
            if sig is not None:
                self.index.setdefault(sig, slug)

    def match(self, intl_slug: str) -> str | None:
        sig = slug_signature(intl_slug)
        return self.index.get(sig) if sig else None
