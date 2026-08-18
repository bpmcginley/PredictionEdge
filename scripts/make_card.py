"""Draw the 1200x630 social card that `og:image` points at.

Run it, commit the PNG. It is a build step rather than a hand-made image so the card can
be regenerated when the headline changes, and so the colours come from one place - the
same tokens `docs/assets/app.css` uses - instead of being matched by eye in an editor.

WHY THERE ARE NO NUMBERS ON IT. The obvious card would print the trial's headline
figures, and they would be wrong within a day: the card is a committed file and the
figures move every fifteen minutes. A record whose whole claim is that it cannot be
quietly edited after the fact should not ship a picture of itself that goes stale and
stays flattering. The card states what the project IS; the page states what it found.

Fonts are Windows system faces, chosen as the nearest local stand-ins for the web fonts
the site loads: Arial Bold for Archivo (both grotesques), Georgia for Source Serif,
Consolas for JetBrains Mono. Pillow is a dev-only dependency and is deliberately not in
`requirements.txt` - nothing the bot runs needs it.

    python scripts/make_card.py
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "card.png"
FONTS = pathlib.Path("C:/Windows/Fonts")

W, H = 1200, 630
PAD = 76

# --- straight from :root in docs/assets/app.css ------------------------------
PAPER = (250, 250, 248)
INK = (10, 10, 10)
INK2 = (59, 59, 56)
INK3 = (120, 119, 111)
RULE = (212, 212, 208)
ACCENT = (180, 83, 9)

HEADLINE = "Does copying smart money on prediction markets actually pay?"
KICKER = "PREDICTIONEDGE"
BYLINE = "BRUCE MCGINLEY"
SUB = "A public, append-only paper trial of a copy-trading strategy."
URL = "bpmcginley.github.io/PredictionEdge"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def tracked(d: ImageDraw.ImageDraw, xy, text, f, fill, track: float) -> float:
    """Draw `text` with letter-spacing and return the width used.

    Pillow has no tracking, and the small uppercase labels on this site are defined by
    theirs - set solid they read as a different design. So they are drawn a glyph at a
    time.
    """
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) + track
    return x - xy[0] - track


def wrap(d: ImageDraw.ImageDraw, text: str, f, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=f) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit(d: ImageDraw.ImageDraw, text: str, width: int, max_lines: int):
    """Largest headline size that still wraps into `max_lines`. The card has one job and
    a fixed frame, so the type is sized to the frame rather than the frame padded to fit
    whatever a chosen size happened to produce."""
    for size in range(92, 39, -2):
        f = font("arialbd.ttf", size)
        lines = wrap(d, text, f, width)
        if len(lines) <= max_lines:
            return f, lines, size
    raise SystemExit("headline will not fit")


def main() -> None:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    inner = W - 2 * PAD

    # Kicker: accent mark, then the name, tracked like the site's nav.
    d.rectangle([PAD, PAD + 3, PAD + 11, PAD + 14], fill=ACCENT)
    tracked(d, (PAD + 26, PAD), KICKER, font("arialbd.ttf", 16), INK, 3.2)
    d.line([PAD, PAD + 46, W - PAD, PAD + 46], fill=INK, width=2)

    # Headline, optically tightened: Arial's default fit is loose at display sizes.
    f, lines, size = fit(d, HEADLINE, inner, 3)
    lead = int(size * 1.06)
    top = PAD + 108
    for i, line in enumerate(lines):
        d.text((PAD - 3, top + i * lead), line, font=f, fill=INK)

    # Footer rule sits below the block wherever it ended, so a shorter headline does not
    # leave the card looking bottom-heavy.
    rule_y = max(top + len(lines) * lead + 46, H - PAD - 96)
    d.line([PAD, rule_y, W - PAD, rule_y], fill=RULE, width=1)

    by_f = font("arialbd.ttf", 17)
    tracked(d, (PAD, rule_y + 30), BYLINE, by_f, INK, 2.6)

    sub_f = font("georgiai.ttf", 22)
    d.text((PAD, rule_y + 62), SUB, font=sub_f, fill=INK2)

    url_f = font("consola.ttf", 17)
    d.text((W - PAD - d.textlength(URL, font=url_f), rule_y + 30),
           URL, font=url_f, fill=INK3)

    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB, headline at {size}px, "
          f"{len(lines)} lines)")


if __name__ == "__main__":
    main()
