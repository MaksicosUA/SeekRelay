"""Characters that must not reach a terminal or a model as they are.

A web page can hide instructions from a human reader with Unicode tag characters and
zero-width or bidi controls, or rewrite a terminal with control sequences. ``html.escape``
leaves all of these intact.

The ranges are explicit code points rather than ``unicodedata`` categories, so the result
is the same on every supported Python and importing this module stays cheap.
"""

from __future__ import annotations

import re

__all__ = ["UNSAFE_CHARS_RE", "one_line", "strip_invisible"]

UNSAFE_CHARS_RE: re.Pattern[str] = re.compile(
    "["
    # Controls: C0 except TAB and LF, DEL, C1 (with NEL, CSI and OSC).
    r"\x00-\x08\x0b-\x1f\x7f-\x9f"
    # Format characters (category Cf). U+2065 and U+FFF0-U+FFF8 are unassigned, but reserved
    # as default ignorable: they render as nothing and may become Cf in a later Unicode.
    r"\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\ufff0-\ufffb"
    r"\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    # Line and paragraph separators, surrogates, private use.
    r"\u2028\u2029\ud800-\udfff\ue000-\uf8ff\U000f0000-\U0010ffff"
    # Tags and variation selectors supplement: the block behind ASCII smuggling.
    r"\U000e0000-\U000e0fff"
    # Invisible marks and letters outside Cf: variation selectors, combining grapheme joiner,
    # Hangul fillers, Khmer inherent vowels, Mongolian variation selectors, and the Braille,
    # musical and Khitan blanks, which fonts draw as nothing.
    r"\ufe00-\ufe0f\u034f\u115f\u1160\u3164\uffa0\u17b4\u17b5\u180b-\u180f"
    r"\u2800\U00016fe4\U0001d159"
    # Noncharacters.
    r"\ufdd0-\ufdef\ufffe\uffff"
    r"\U0001fffe\U0001ffff\U0002fffe\U0002ffff\U0003fffe\U0003ffff\U0004fffe\U0004ffff"
    r"\U0005fffe\U0005ffff\U0006fffe\U0006ffff\U0007fffe\U0007ffff\U0008fffe\U0008ffff"
    r"\U0009fffe\U0009ffff\U000afffe\U000affff\U000bfffe\U000bffff\U000cfffe\U000cffff"
    r"\U000dfffe\U000dffff\U000efffe\U000effff"
    "]"
)
"""Matches one character that must not reach a terminal or a model as it is."""

_LINE_BREAKS_RE = re.compile(r"\r\n?|[\x0b\x0c\x85\u2028\u2029]")


def strip_invisible(text: str) -> str:
    """Remove everything :data:`UNSAFE_CHARS_RE` matches and keep the line structure.

    CR LF, lone CR, VT, FF, NEL, U+2028 and U+2029 become LF, so a page cannot overwrite a
    terminal line. TAB and LF stay.
    """
    if UNSAFE_CHARS_RE.search(text) is None:
        return text
    return UNSAFE_CHARS_RE.sub("", _LINE_BREAKS_RE.sub("\n", text))


def one_line(text: str) -> str:
    """Clean ``text`` and put it on one line, so that it cannot pass for a line of our own."""
    return " ".join(strip_invisible(text).split())
