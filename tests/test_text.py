from __future__ import annotations

import sys
import unicodedata

import pytest

from seekrelay._text import UNSAFE_CHARS_RE, one_line, strip_invisible


def test_ranges_cover_every_control_format_surrogate_and_private_character() -> None:
    # Runs on every Python in CI, so a new Unicode version cannot slip past the fixed ranges.
    missed = [
        f"U+{code:04X}"
        for code in range(sys.maxunicode + 1)
        if unicodedata.category(chr(code)) in ("Cc", "Cf", "Cs", "Co")
        and chr(code) not in "\t\n"
        and not UNSAFE_CHARS_RE.fullmatch(chr(code))
    ]
    assert missed == []


@pytest.mark.parametrize(
    "char",
    [
        pytest.param("\u034f", id="combining-grapheme-joiner"),
        pytest.param("\u115f", id="hangul-choseong-filler"),
        pytest.param("\u1160", id="hangul-jungseong-filler"),
        pytest.param("\u17b4", id="khmer-vowel-aq"),
        pytest.param("\u17b5", id="khmer-vowel-aa"),
        pytest.param("\u180b", id="mongolian-selector-one"),
        pytest.param("\u180f", id="mongolian-selector-four"),
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
        pytest.param("\u2065", id="reserved-format"),
        pytest.param("\u2800", id="braille-blank"),
        pytest.param("\u3164", id="hangul-filler"),
        pytest.param("\ufdd0", id="noncharacter-first"),
        pytest.param("\ufdef", id="noncharacter-last"),
        pytest.param("\ufe00", id="variation-selector-1"),
        pytest.param("\ufe0f", id="variation-selector-16"),
        pytest.param("\uffa0", id="halfwidth-hangul-filler"),
        pytest.param("\ufff0", id="reserved-specials"),
        pytest.param("\ufffe", id="noncharacter-fffe"),
        pytest.param("\uffff", id="noncharacter-ffff"),
        pytest.param("\U00016fe4", id="khitan-filler"),
        pytest.param("\U0001d159", id="musical-null-notehead"),
        pytest.param("\U000e0000", id="tag-block-start"),
        pytest.param("\U000e0041", id="tag-latin-capital-a"),
        pytest.param("\U000e0100", id="variation-selector-17"),
        pytest.param("\U000e0fff", id="tag-block-end"),
    ],
)
def test_invisible_characters_of_other_categories_are_unsafe(char: str) -> None:
    assert UNSAFE_CHARS_RE.fullmatch(char)


def test_every_noncharacter_is_unsafe() -> None:
    # Category Cn also holds unassigned characters, so the 66 of them are spelled out.
    noncharacters = [
        *range(0xFDD0, 0xFDF0),
        *(plane * 0x10000 + low for plane in range(17) for low in (0xFFFE, 0xFFFF)),
    ]
    assert len(noncharacters) == 66
    missed = [f"U+{code:04X}" for code in noncharacters if not UNSAFE_CHARS_RE.fullmatch(chr(code))]
    assert missed == []


@pytest.mark.parametrize(
    "char",
    [
        pytest.param("\t", id="tab"),
        pytest.param("\n", id="lf"),
        pytest.param(" ", id="space"),
        pytest.param("\xa0", id="no-break-space"),
        pytest.param("\xac", id="below-soft-hyphen"),
        pytest.param("\xae", id="above-soft-hyphen"),
        pytest.param("\u200a", id="below-zero-width"),
        pytest.param("\u2010", id="above-zero-width"),
        pytest.param("\u2027", id="below-line-separator"),
        pytest.param("\u202f", id="above-bidi"),
        pytest.param("\u205f", id="below-word-joiner"),
        pytest.param("\u2070", id="above-format"),
        pytest.param("\ufe10", id="above-variation-selectors"),
        pytest.param("\ufffc", id="object-replacement"),
        pytest.param("\ufffd", id="replacement-character"),
        pytest.param("\U000dfffd", id="below-tag-block"),
        pytest.param("\U000e1000", id="above-tag-block"),
        pytest.param("\U0001f600", id="emoji"),
        pytest.param("\u27ff", id="below-braille-blank"),
        pytest.param("\u2801", id="above-braille-blank"),
        pytest.param("\U00016fe3", id="below-khitan-filler"),
        pytest.param("\U00016fe5", id="above-khitan-filler"),
        pytest.param("\U0001d158", id="below-musical-null-notehead"),
        pytest.param("\U0001d15a", id="above-musical-null-notehead"),
        pytest.param("\ufdcf", id="below-noncharacters"),
        pytest.param("\ufdf0", id="above-noncharacters"),
        pytest.param("\U0001fffd", id="below-plane-end"),
        pytest.param("\U00020000", id="above-plane-end"),
    ],
)
def test_neighbouring_characters_are_safe(char: str) -> None:
    assert not UNSAFE_CHARS_RE.search(char)


def test_pattern_matches_one_character_at_a_time() -> None:
    assert UNSAFE_CHARS_RE.findall("a\x00\x01b") == ["\x00", "\x01"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("a\r\nb", "a\nb", id="crlf"),
        pytest.param("a\rb", "a\nb", id="lone-cr"),
        pytest.param("a\x0bb", "a\nb", id="vt"),
        pytest.param("a\x0cb", "a\nb", id="ff"),
        pytest.param("a\x85b", "a\nb", id="nel"),
        pytest.param("a\u2028b", "a\nb", id="line-separator"),
        pytest.param("a\u2029b", "a\nb", id="paragraph-separator"),
        pytest.param("a\n\rb", "a\n\nb", id="lf-cr"),
        pytest.param("a\r\r\nb", "a\n\nb", id="cr-crlf"),
        pytest.param("a\r\u200bb", "a\nb", id="cr-with-format-character"),
        pytest.param("a\nb\tc", "a\nb\tc", id="lf-and-tab-stay"),
    ],
)
def test_line_breaks_become_lf(raw: str, expected: str) -> None:
    assert strip_invisible(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("\x00a\x07b\x08c\x7fd", "abcd", id="c0-and-del"),
        pytest.param("\x1b[2Jtext", "[2Jtext", id="esc"),
        pytest.param("\x9b2Jtext", "2Jtext", id="c1-csi"),
        pytest.param("\x9d0;title\x07text", "0;titletext", id="c1-osc"),
        pytest.param("a\u200bb\u200cc\u200dd\ufeffe", "abcde", id="zero-width"),
        pytest.param("\u202ea\u2066b\u2069c\u200f", "abc", id="bidi"),
        pytest.param("a\ud800b\ue000c\U0010fffd", "abc", id="surrogate-and-private-use"),
        pytest.param("a\u2800b\U00016fe4c\U0001d159d", "abcd", id="drawn-as-nothing"),
        pytest.param("a\U0001fffeb\U000effffc", "abc", id="noncharacters"),
    ],
)
def test_unsafe_characters_are_removed(raw: str, expected: str) -> None:
    assert strip_invisible(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("The quick brown fox: 100% <tested> & (done)!", id="english"),
        pytest.param("Реве та стогне Дніпр широкий, ґанок та їжак", id="ukrainian"),
        pytest.param("Съешь же ещё этих мягких французских булок", id="russian"),
        pytest.param("中文搜索结果《标题》「引用」。", id="chinese"),
        pytest.param("مرحبا بالعالم، كيف حالك؟", id="arabic"),
        pytest.param("\U0001f600 \U0001f680 \u2764 \U0001f1fa\U0001f1e6", id="emoji"),
        pytest.param("\u2801\u2803\u28ff", id="braille-patterns"),
    ],
)
def test_ordinary_text_is_unchanged(text: str) -> None:
    assert strip_invisible(text) == text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("\u2764\ufe0f", "\u2764", id="emoji-presentation-selector"),
        pytest.param("\U0001f468\u200d\U0001f469", "\U0001f468\U0001f469", id="emoji-joiner"),
        pytest.param("\U0001f3f4\U000e0067\U000e0062\U000e007f", "\U0001f3f4", id="tag-flag"),
    ],
)
def test_emoji_lose_only_the_invisible_parts(raw: str, expected: str) -> None:
    assert strip_invisible(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("plain text", "plain text", id="plain"),
        pytest.param("", "", id="empty"),
        pytest.param("\n \t\r\n", "", id="only-blanks"),
        pytest.param("  a  b  ", "a b", id="runs-of-spaces"),
        pytest.param("a\nb", "a b", id="lf"),
        pytest.param("a\r\nb\rc", "a b c", id="cr"),
        pytest.param("a\x0bb\x0cc\x85d", "a b c d", id="vt-ff-nel"),
        pytest.param("a\u2028b\u2029c", "a b c", id="unicode-separators"),
        pytest.param("a\tb\u3000c\u00a0d", "a b c d", id="tab-and-unicode-spaces"),
        pytest.param("a\u200bb\x1b[2Jc", "ab[2Jc", id="invisible"),
        pytest.param("fake\r\n\x1b[2Jerror: boom", "fake [2Jerror: boom", id="forged-line"),
    ],
)
def test_one_line(raw: str, expected: str) -> None:
    assert one_line(raw) == expected


def test_cleaning_leaves_nothing_unsafe_and_is_stable() -> None:
    every_character = "".join(chr(code) for code in range(sys.maxunicode + 1))
    once = strip_invisible(every_character)
    assert not UNSAFE_CHARS_RE.search(once)
    assert strip_invisible(once) == once


@pytest.mark.parametrize("unit", ["\r", "\r\n", "\u200b", "\x1b[", "a\u2028"])
def test_long_hostile_input(unit: str) -> None:
    # A slower-than-linear pattern would hang here instead of failing an assertion.
    cleaned = strip_invisible(unit * 200_000)
    assert not UNSAFE_CHARS_RE.search(cleaned)
