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
    # The last two alpha tokens before the date are the participants; anything earlier is
    # the league/tour prefix, which is exactly what differs across the two venues
    # (`fifwc-ecu-ger` vs `atc-fwc-ecu-ger`). Position alone drops it.
    #
    # There used to be a `2 <= len(t) <= 4` filter here as well, on the theory that
    # participants are country codes. They are for the World Cup and for MLB
    # (`mlb-bal-tex`), and for nothing else this project actually publishes. Real slugs
    # use surname fragments: `wta-annli-rybakin`, `atp-grieksp-arnaldi`. Those tokens are
    # 5-7 characters, so the filter deleted both participants, left only the tour code,
    # and the function returned None - every tennis market on the board was unroutable to
    # a tradeable venue, silently, as a "no counterpart" rather than an error. Position
    # already excludes the prefix, so the length rule bought nothing and cost the
    # majority of the flow.
    teams = [t for t in pre if t.isalpha()][-2:]
    if len(teams) < 2:
        return None
    outcome = post[-1] if post else ""
    return frozenset(teams), date, outcome


class PMUSMatcher:
    """Index PM-US slugs by signature; look up the counterpart of an intl slug."""

    def __init__(self, pmus_slugs):
        self.index: dict = {}
        collisions: set = set()
        for slug in pmus_slugs:
            sig = slug_signature(slug)
            if sig is None:
                continue
            if sig in self.index and self.index[sig] != slug:
                # Two PM-US markets with one signature. This is not hypothetical: a
                # single game slug like `mlb-bal-tex-2026-08-07` carries the moneyline,
                # the run line and the total, and the board only ever gives us the event
                # URL - so the pre-date tokens are identical for all three. The old
                # `setdefault` kept whichever arrived first, which would route a copied
                # Over 8.5 onto a moneyline contract and book the fill as if it were the
                # signal. Refusing to match is a missed ticket; matching the wrong
                # contract is a wrong position that then reports as a correct one.
                collisions.add(sig)
                continue
            self.index[sig] = slug
        for sig in collisions:
            self.index.pop(sig, None)
        self.ambiguous = collisions

    def match(self, intl_slug: str) -> str | None:
        sig = slug_signature(intl_slug)
        return self.index.get(sig) if sig else None
