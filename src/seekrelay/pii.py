"""A small, conservative guard that stops personal data and secrets leaving the process.

Search queries and URLs are sent to third parties. Agents sometimes build queries out
of whatever is in their context, including e-mail addresses, payment details or API
keys. The guard rejects such requests *before* any network call.

Design choices:

* High precision over recall. Card numbers must pass the Luhn check, IBANs the
  ISO 13616 mod-97 check and tax numbers their control digit, next to a keyword, so
  ordinary numbers in queries ("python 3.12 2024") do not trigger it. Secrets need a
  known prefix, or a keyword and a value that reads neither as prose nor as code nor as
  a placeholder. Phone detection is off by default because it is noisy.
* Digit runs are checked in every contiguous window of groups, so a card number is
  still found when an expiry date, CVV or order number sits next to it. Dots, underscores
  and slashes also split dates, versions and identifiers, so a window using them needs one
  separator throughout, at least 3 groups and at least 4 digits in every group. A window needs
  the layout of a card, as lists of numbers and date ranges have many windows, and only 16-digit
  numbers may start with any prefix.
* Every detector sees normalised text: invisible characters removed, NFKC applied, Unicode
  dashes and digits turned into ASCII, the ideographic full stop turned into a dot, combining
  marks removed, whitespace collapsed. Runs of marks are capped first, because NFKC sorts them.
* Encoded data is decoded first, within limits. Both scan methods undo up to three rounds of
  percent-encoding. Base64 is decoded up to two levels deep; only results that are valid UTF-8
  count. Decoded base64 is read with its runs apart and glued, and percent-decoded if it holds
  a percent sign. Text escapes of the at sign are read as the sign.
* The work is linear in the size of the input, because it runs on the event loop. Patterns
  are flat or bounded, and e-mail addresses are found from the at sign outwards.
* The guard reports *kinds* of findings, never the values, so its errors are safe
  to log.
* It is a seatbelt, not a DLP system. Names, postal addresses and secrets with no known
  shape are not detected. Neither are these, on purpose:

  - addresses spelled with plain words ("jane at corp dot example"), or with a space before
    the at sign only, and a spaced sign after one character or a number, before a last label
    with capitals or before a call ("A @ np.linalg.inv(B)", "Cubs @ St.Louis");
  - card groups joined by anything but one kind of space, dash, dot, underscore or slash, in
    an unusual layout, or numbers glued to other digits or padded with zeros;
  - tax numbers split into groups, and keywords written in look-alike letters;
  - secret values with spaces in them, a word and digits after a bare "password", a PEM header
    or body alone, AWS principal ids, Docker ``auth`` entries and ``mysql -p``, and placeholders
    that fit a known prefix;
  - base64 that does not start at the beginning of a run, UTF-16 with a BOM, hex, rot13,
    quoted-printable (``=40``) and a fourth round of percent-encoding;
  - digits drawn as symbols (dingbats) and look-alike letters in keywords.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from itertools import accumulate
from urllib.parse import unquote_plus

from ._text import strip_invisible
from .errors import QueryRejectedError

__all__ = ["PIIGuard"]

# A decimal digit outside ASCII: not a non-digit and not 0-9.
_NON_ASCII_DIGIT_RE = re.compile(r"[^\D0-9]")
_MARK_RUN_LIMIT = 30
# NFKC turns these into marks, although combining() says 0 for them: three starters that
# decompose into marks, and the halfwidth Katakana voicing marks.
_HIDDEN_MARKS = frozenset("\u0f73\u0f75\u0f81\uff9e\uff9f")

# Bracketed spellings of "@" and "." in English, Russian and Ukrainian. The plain words at and
# dot are ordinary English.
_EMAIL_BRACKETED_AT = r"[\[({<] ?(?:at|@|собака|\u0440\u0430\u0432\u043b\u0438\u043a) ?[\])}>]"
# Brackets are no operator, so the spaces around them go too and the rule for a bare sign with
# spaces does not apply. The rest are escapes of the sign that need no decoding pass.
_EMAIL_AT_RE = re.compile(
    rf" {_EMAIL_BRACKETED_AT} |{_EMAIL_BRACKETED_AT}"
    r"|&#0*64;|&#x0*40;|&commat;|\\u0040|\\x40",
    re.IGNORECASE,
)
_EMAIL_DOT_RE = re.compile(
    r" ?[\[({<] ?(?:dot|\.|точка|\u043a\u0440\u0430\u043f\u043a\u0430) ?[\])}>] ?", re.IGNORECASE
)
_EMAIL_LOCAL_LIMIT = 64
_EMAIL_LOCAL_SYMBOLS = "._%+-"
_EMAIL_DOMAIN_RE = re.compile(r"[\w.-]{1,255}")
# With the sign in them, but no address: logo@2x.png, wg-quick@wg0.service, python@3.12.json,
# git@github.com:, this@MainActivity.start(). Each of these last labels is no TLD.
_NOT_A_TLD_RE = re.compile(
    r"png|jpe?g|gif|webp|svg|avif|ico|bmp|tiff|service|socket|target|timer|mount|slice"
    r"|json|rb|tgz|gz|bz2|xz|tar|js|mjs|css|map|data",
    re.IGNORECASE,
)
_NOT_A_PERSON_RE = re.compile(r"(?<![\w.%+-])(?:git|this|super)\Z")
# Digits and the separators of card groups ("4111 1111 1111 1111 123").
_DIGIT_RUN_RE = re.compile(r"\d[\d ._/-]*")
_DIGIT_SEPARATOR_RE = re.compile(r"([ ._/-])")
# What does not join two groups ends a run: a separator before anything but a digit, and a dot,
# underscore or slash without 4 digits on both sides, as those also split dates and versions.
_DIGIT_RUN_BREAK_RE = re.compile(r"[ -](?!\d)|[._/](?:(?<!\d{4}[._/])|(?!\d{4}))")
# Major networks start with 2-6 (Mir/Mastercard 2/5, Amex/JCB 3, Visa 4, Discover 6),
# PROSTIR, the Ukrainian national scheme, with 9804.
_CARD_PREFIXES = ("2", "3", "4", "5", "6", "9804")
# Only 16 digits may start with any of them. Other lengths belong to a few networks, which keeps
# out IMEI numbers, French company numbers and 14-digit timestamps.
_MAESTRO = ("50", "56", "57", "58", "6")
_PREFIXES_BY_LENGTH = {
    13: ("4", *_MAESTRO),
    14: ("30", "36", "38", *_MAESTRO),
    15: ("34", "37", "2131", *_MAESTRO),
    16: _CARD_PREFIXES,
    17: _MAESTRO,
    18: _MAESTRO,
    19: ("22", "35", "4", "9804", *_MAESTRO),
}
_AMEX_AND_DINERS_LAYOUTS = ([4, 6, 5], [4, 6, 4])
# On upper-case text: country code and check digits, then a separator or the rest of a compact
# number. Other first groups are UUIDs or paths: "AB12CD34-...", "en/2024/10/24/...". The group
# captures up to 45 characters, the longest IBAN in groups of three with the country code split
# off. They are walked in Python, as optional separators in a repeated group would backtrack.
_IBAN_WINDOW_RE = re.compile(
    r"(?=[A-Z]{2}(?:[ ._/-]?\d{2}[ ._/-]|\d{2}[A-Z0-9]{11}))(?<![A-Z0-9])"
    r"(?=([A-Z0-9 ._/-]{15,45}))"
)
_IBAN_SEPARATOR_RE = re.compile(r"[ ._/-]")
# Only a space joins groups of more than 4 characters: the rest also split UUIDs, paths and file
# names, so there they end a number.
_IBAN_BREAK_RE = re.compile(r"[._/-](?:(?<=[A-Z0-9]{5}[._/-])|(?=[A-Z0-9]{5}))")
_IBAN_SHAPE_RE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
# The mod-97 check reads a letter as two digits, A being 10.
_IBAN_LETTER_VALUES = {code: str(code - 55) for code in range(ord("A"), ord("Z") + 1)}
_IBAN_MIN_DIGITS = 10
# One unbounded class. A start fails only if the rest of its run is separators then digits, so
# a run has a handful of failing starts and the scan stays linear. Bounding the class would be
# worse: the tail of a long list of years would match on its own.
_PHONE_RE = re.compile(r"(?<![\w+.])\+?\d[\d ()-]{7,}\d(?![\w.])")

_TAX_ID_WEIGHTS = (-1, 5, 7, 9, 4, 6, 10, 5, 7)
_TEN_DIGITS_RE = re.compile(r"(?<!\d)\d{10}(?!\d)")
# The Ukrainian keywords exist with a Latin i too, which a Russian keyboard layout produces.
_TAX_KEYWORD = (
    r"(?<![^\W_])(?:рнокпп|rnokpp|рнукпн|ипн|[\U00000456i]пн|инн|[\U00000456i]нн|ipn|drfo|дрфо|tin"
    # "Inn" is a hotel more often than not.
    r"|(?-i:inn|INN)"
    r"|tax(?:payer)?[ _-]?id(?:entification)?|tax[ _-]?(?:no|number|code)"
    r"|код платника податків"
    r"|(?:податков|налогов|ідентифікаційн|идентификационн)\w{1,4} (?:номер|код)\w{0,3})"
    r"(?: (?:number|номер|код|платника(?: податків)?|налогоплательщика))?(?![^\W_])"
)
# NFKC turns the numero sign into "No", which is the only word allowed between the two.
_TAX_KEYWORD_BEFORE_RE = re.compile(_TAX_KEYWORD + r"\W{0,5}(?:No\W{0,3})?\Z", re.IGNORECASE)
_TAX_KEYWORD_AFTER_RE = re.compile(r"\W{0,5}" + _TAX_KEYWORD, re.IGNORECASE)
_TAX_KEYWORD_WINDOW = 64

# Credentials with a fixed shape, each anchored on a literal prefix, none with a nested
# quantifier. The lookbehinds keep short prefixes out of longer words. In the JWT branch, the only
# one that looks past an unbounded run, the lookbehind also stops a restart inside a run: without
# it "eyJ" * n is quadratic. A JWT after a dash or underscore can be such a restart, so there its
# first part is bounded.
_SECRET_RE = re.compile(
    # Every branch starts with one of these. This lookahead, like the first one in the patterns
    # below, spares the text that NFKC makes longest.
    r"(?=[A-Za-z0-9-])(?:"
    # A header alone is not a key. A service-account JSON file writes its line breaks as "\n",
    # and PEM and PGP put their own headers before the key material.
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
    r"(?:\s|\\[nr]){0,4}(?:[A-Za-z0-9+/=]{16}|Proc-Type:|Version:|Comment:)"
    r"|gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|tvly-[A-Za-z0-9_-]{16,}"
    # Slack tokens go on with digits, "xoxb-your-bot-token" does not.
    r"|xox[abeoprs]-\d[A-Za-z0-9-]{9,}"
    r"|hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{10,}"
    r"|discord(?:app)?\.com/api/webhooks/\d{6,}/[A-Za-z0-9_-]{20,}"
    r"|pypi-AgE[A-Za-z0-9_-]{20,}"
    r"|dckr_pat_[A-Za-z0-9_-]{20,}"
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"
    # Every Telegram API URL has "bot" right before the token.
    r"|bot\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
    r"|eyJ(?:(?<![A-Za-z0-9_-]eyJ)[A-Za-z0-9_-]{7,}|(?<=[_-]eyJ)[A-Za-z0-9_-]{7,128})"
    r"\.eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]{10,}"
    r"|(?<![A-Za-z0-9])(?:"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
    r"|AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
    r")"
    r"|(?<![A-Za-z0-9_-])(?:"
    r"hf_[A-Za-z0-9]{30,}"
    r"|npm_[A-Za-z0-9]{36,}"
    r"|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|gsk_[A-Za-z0-9]{40,}"
    r"|xapp-\d-[A-Za-z0-9-]{10,}"
    r"|ya29\.[A-Za-z0-9_-]{10,}"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{20,}"
    # The first character keeps out phrases such as "BSA-compliance-for-small-banks".
    r"|BSA[A-Za-z0-9][A-Za-z0-9_-]{24,}"
    r"|\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
    r"))"
)
# Shapes that also occur in ordinary text, so the captured value needs a check in Python.
_SK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])sk-([A-Za-z0-9_-]{20,})")
_BEARER_RE = re.compile(r"\bbearer(?: ?[:=] ?| )([A-Za-z0-9._~+/=-]{20,})", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"""\bauthorization(?:\\?["'])? ?[:=] ?(?:\\?["'])?(?:(?:basic|bearer|token|apikey) )?"""
    r"([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_CURL_USER_RE = re.compile(r"""(?<![\w-])(?:-[A-Za-z]{0,4}u|--user) ?[^\s:]{1,64}:([^\s"']{4,})""")
_URL_PASSWORD_RE = re.compile(r"://[^\s/?#:@]{0,128}:([^\s/?#@]{1,256})@[^\s/?#@]")
# After a name: PASSWORD1, DB_PASSWORD_PROD, SECRET_KEY_BASE. Other words make another name, such
# as PASSWORD_RESET_TIMEOUT.
_NAME_SUFFIX = (
    r"(?:\d+|_(?:\d+|v\d+|prod|production|dev|development|staging|stage|test|qa|uat|ci"
    r"|primary|secondary|backup|old|new|base))?"
)
# Password words need no boundary on the left, for PGPASSWORD and dbpassword. Between the word
# and the value goes whatever the formats of config files, code, Markdown and prose put there,
# and at least something: "password_hint" is another name.
_PASSWORD_RE = re.compile(
    r"(?=[p\u043f])(?:pass(?:word|wd|phrase)|(?<![^\W_])(?:pwd|пароль|p(?:ass|w)(?= ?[:=])))"
    + _NAME_SUFFIX
    + r"""(?P<gap>(?:\\?["'])?(?:\*\*)? ?"""
    r"(?P<operator>(?:is|\u0446\u0435|-)(?= )|:=|==|=>|->|[:=>,])?"
    r"""(?:\*\*)? ?(?:\\?["'])?)(?<!\w)"""
    r"""(?P<value>[^\s"'\\&<>]{6,})""",
    re.IGNORECASE,
)
# A word boundary or a camelCase hump, which has to be case-sensitive, before an ASCII letter.
_NAME_START = r"(?=[a-z])(?:(?<![^\W_])|(?-i:(?<=[a-z])(?=[A-Z])))"
_TOKEN_WORD_RE = re.compile(
    _NAME_START + r"(?P<word>client[_-]?secret|secret(?:[_-]?access)?(?:[ ._-]?key)?"
    r"|api[ ._-]?(?:key|secret|token)|access[ ._-]?(?:key|token)"
    r"|(?:auth|refresh|session)[_-]?token|token)"
    + _NAME_SUFFIX
    + r"""(?:\\?["'])? ?(?::=|==|=>|[:=]) ?(?:\\?["'])?(?P<value>[^\s"'\\&<>;]{6,})""",
    re.IGNORECASE,
)
_SECRET_FLAG_RE = re.compile(
    r"(?<![\w-])--?(?:[a-z0-9]+-)*(?P<word>password|passwd|passphrase|token|secret|api-?key)"
    r"""[ =]["']?(?P<value>[^\s"'&]{6,})""",
    re.IGNORECASE,
)
# What makes a token name no credential: the word before it, if that says a position in a list,
# or the OAuth state parameter, which holds an anti-forgery value such as security_token=...
_NOT_A_CREDENTIAL_RE = re.compile(
    "(?:" + _NAME_START + r"(?:page|next|prev|previous|cursor|continuation|sync|resume|start"
    r"|after|max|min|pagination|resumption|since|before|until|stream|scroll|offset|end|delta"
    r"|skip|idempotency)[_-]?|\bstate=[\w-]*)\Z",
    re.IGNORECASE,
)
_NOT_A_CREDENTIAL_WINDOW = 20
# A value that is a default, a placeholder or a variable as a whole, as in shell, PHP, YAML, SQL
# and Windows: $PASSWORD, $password, $request->password, !vault, @password, %DB_PASSWORD%. Parts
# do not count: "Mykola2005", "hunter22xxx" and "$uperS3cret" are passwords.
_PLACEHOLDER_RE = re.compile(
    r"password|passwd|pass|pwd|secret|token|root|admin|guest|test|postgres|user|username|string"
    r"|changeit|none|null|nil|true|false|undefined|required|optional|empty|default|hidden|redacted"
    r"|(?:your|my)[-_].*|(?:your|my)(?:pass|secret|token|key|api)\w*"
    r"|<.*>|\[.*\]|\{.*\}|\$\{.*\}|(?-i:\$[A-Z0-9_]+|\$[a-z_]+-?|![a-z_]+)"
    r"|(?-i:\$[a-z]+(?:[A-Z][a-z]+)+|@[a-z_]+(?:[A-Z][a-z]+)*|%[A-Z0-9_]+%)|\$env:\w+|%\{.*"
    r"|(?:example|placeholder|changeme|x{3,})\d*|[*x.#\u2022]{3,}"
    # Rule strings, options and algorithms, which code and docs put after these words.
    r"|hashed|confirmed|(?:required|nullable|sometimes|string|confirmed)\|.*"
    r"|(?:id|src|source|target)=.*|(?:env|file|fd):.*"
    r"|aws4-hmac-sha256|(?:sha|md|argon|pbkdf)\d+|base64",
    re.IGNORECASE,
)
# "os.environ" is code. A digit makes two parts a value, "Hunter.x9", unless the root or a third
# part shows code: "var.db2_password", "data.cluster.eks1.token".
_DOTTED_NAME_RE = re.compile(
    r"[A-Za-z_$]+(?:\??\.[A-Za-z_$]+)+!?"
    r"|(?:var|local|module|data|self|this|cfg|config|settings|env|args|opts)"
    r"(?:\.[A-Za-z_$][\w$]*)+"
    r"|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){2,}"
)
# After a bare "password": a product, not a value. "password Windows10", "password OAuth2".
_PRODUCT_WORD_RE = re.compile(r"[A-Za-z]+\d+(?:\.\d+)?[a-z]{0,2}")
# Words of an identifier, not a random key: cancellationToken, HTTPAuthorizationCredentials.
_CAMEL_WORDS_RE = re.compile(r"[a-z]*(?:[A-Z]+\d?[a-z]{2,}\d?)+[A-Z]*")
_NUMBER_RE = re.compile(r"[\d.,]+")
# Punctuation around a word is prose: "token: authentication, authorization". A final "!"
# stays, because passwords end in it more often than sentences after a colon do.
_PROSE_PUNCTUATION = ".,;:?()[]"
_MIN_VALUE_LENGTH = 6
_MIN_MIXED_CASE_LENGTH = 16
_ONLY_A_CLOSING_QUOTE = ("'", '"', "\\'", '\\"')

_URL_DECODE_ROUNDS = 3
_BASE64_DEPTH = 2
# The standard alphabet, then one without the slash and with the URL-safe characters, because
# a token can be one path segment between slashes.
_BASE64_RUN_RES = (re.compile(r"[A-Za-z0-9+/]{8,}"), re.compile(r"[A-Za-z0-9+_-]{8,}"))


def _cap_mark_runs(text: str) -> str:
    """Drop combining marks past the 30th in a row, the most UAX #15 allows.

    NFKC sorts each run of marks. The sort is quadratic before the fix for CVE-2026-3276, and
    interpreters without that fix are supported, such as the last python.org installers for
    3.10 and 3.11.
    """
    kept = []
    run = 0
    for char in text:
        run = run + 1 if unicodedata.combining(char) or char in _HIDDEN_MARKS else 0
        if run <= _MARK_RUN_LIMIT:
            kept.append(char)
    return "".join(kept)


def _hiding_characters(text: str) -> dict[int, str | None]:
    """Return a table that removes the marks of ``text`` and turns its dashes into ASCII."""
    table: dict[int, str | None] = {}
    for char in set(text):
        category = unicodedata.category(char)
        if category[0] == "M":
            table[ord(char)] = None
        elif category == "Pd" or char == "\U00002212":  # the minus sign is no Pd
            table[ord(char)] = "-"
    return table


def _normalise(text: str) -> str:
    """Remove the formatting that would hide data from the detectors."""
    text = strip_invisible(text)
    if not text.isascii():
        # Whole Arabic phrases stored as one character. NFKC makes them 18 and 8 characters
        # long, no other character passes 6, and a hostile URL would pay for it four times.
        text = text.replace("\ufdfa", "").replace("\ufdfb", "")
        text = unicodedata.normalize("NFKC", _cap_mark_runs(text))
        text = text.replace("\u3002", ".")
        text = _NON_ASCII_DIGIT_RE.sub(lambda match: str(int(match.group())), text)
        # Marks go after NFKC, which keeps composed letters. This shows addresses and digits in
        # Hindi, Bengali, Tamil and Thai to the detectors. The text is only scanned, never sent.
        text = text.translate(_hiding_characters(text))
    return " ".join(text.split())


def _has_local_part(text: str, end: int) -> bool:
    for index in range(end - 1, max(end - _EMAIL_LOCAL_LIMIT, 0) - 1, -1):
        char = text[index]
        if char.isalnum():
            return True
        if char not in _EMAIL_LOCAL_SYMBOLS:
            return False
    return False


def _has_domain(text: str, start: int) -> bool:
    match = _EMAIL_DOMAIN_RE.match(text, start)
    if match is None:
        return False
    # An underscore is a word character but not part of a host name.
    labels = match.group().partition("_")[0].split(".")
    for index, label in enumerate(labels):
        if not label:
            return False
        # Two letters are enough for a TLD: that covers "xn--80akhbyknj4f", and a dash that
        # came from an em dash after the address.
        if index and len(label) >= 2 and label[:2].isalpha():
            return True
    return False


def _is_not_an_address(text: str, local_end: int, domain_start: int) -> bool:
    domain = _EMAIL_DOMAIN_RE.match(text, domain_start)
    if domain is None:
        return False
    last_label = domain.group().partition("_")[0].rstrip(".-").rpartition(".")[2]
    return (
        _NOT_A_TLD_RE.fullmatch(last_label) is not None
        or text[domain.end() : domain.end() + 1] == "("
        or _NOT_A_PERSON_RE.search(text, max(local_end - 6, 0), local_end) is not None
    )


def _spaced_sign_ok(text: str, local_end: int, domain_start: int) -> bool:
    # With spaces on both sides the sign can be an operator or mean "at a place" as well:
    # "x @ self.weights", "Cubs @ St.Louis", "iPhone 15 @ rozetka.ua".
    left = local_end
    limit = max(local_end - _EMAIL_LOCAL_LIMIT, 0)
    while left > limit and (text[left - 1].isalnum() or text[left - 1] in _EMAIL_LOCAL_SYMBOLS):
        left -= 1
    token = text[left:local_end]
    domain = _EMAIL_DOMAIN_RE.match(text, domain_start)
    if domain is None or len(token) < 2 or token.isdigit():
        return False
    last_label = domain.group().partition("_")[0].rstrip(".-").rpartition(".")[2]
    return last_label == last_label.lower()


def _contains_email(text: str) -> bool:
    text = _EMAIL_DOT_RE.sub(".", _EMAIL_AT_RE.sub("@", text))
    at = text.find("@")
    while at != -1:
        local_end = at - 1 if text[at - 1 : at] == " " else at
        domain_start = at + 2 if text[at + 1 : at + 2] == " " else at + 1
        # A space before the sign and none after it is a decorator, an npm scope or a handle.
        spacing_ok = local_end == at or domain_start != at + 1
        if (
            spacing_ok
            and _has_local_part(text, local_end)
            and _has_domain(text, domain_start)
            and not _is_not_an_address(text, local_end, domain_start)
            and (local_end == at or _spaced_sign_ok(text, local_end, domain_start))
        ):
            return True
        at = text.find("@", at + 1)
    return False


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _looks_like_card(digits: str) -> bool:
    # Repeated digits ("0000 0000 ...") pass Luhn but are never real card numbers.
    return (
        13 <= len(digits) <= 19
        and digits.startswith(_PREFIXES_BY_LENGTH[len(digits)])
        and len(set(digits)) > 1
        and _luhn_ok(digits)
    )


def _separators_ok(separators: list[str], start: int, end: int) -> bool:
    # Dots, underscores and slashes join two numbers all the time: a DOI, YYYYMMDD_HHMMSS.
    used = set(separators[start:end])
    return used <= {" ", "-"} or (len(used) == 1 and end - start >= 2)


def _layout_ok(sizes: list[int]) -> bool:
    # A card has groups of one size and a last group of any size, or an Amex or Diners layout.
    # Every other window of a list of numbers or a date range would pass Luhn one time in ten.
    return len(set(sizes[:-1])) <= 1 or sizes in _AMEX_AND_DINERS_LAYOUTS


def _luhn_totals(digits: str) -> tuple[list[int], list[int]]:
    """Return running Luhn totals for windows of ``digits`` that end at even and odd offsets.

    Luhn doubles every second digit from the right, so the weights depend only on where a
    window ends, and ``totals[last] - totals[first]`` is the total of ``digits[first:last]``.
    A run of n digits has up to 7n windows: with this each of them costs one subtraction.
    """
    plain = [int(char) for char in digits]
    doubled = [value * 2 - 9 if value > 4 else value * 2 for value in plain]
    ends_even, ends_odd = plain.copy(), plain.copy()
    ends_even[0::2] = doubled[0::2]
    ends_odd[1::2] = doubled[1::2]
    return [0, *accumulate(ends_even)], [0, *accumulate(ends_odd)]


def _contains_card(text: str) -> bool:
    for match in _DIGIT_RUN_RE.finditer(_DIGIT_RUN_BREAK_RE.sub(",", text)):
        if match.end() - match.start() < 13:
            continue
        parts = _DIGIT_SEPARATOR_RE.split(match.group())
        groups, separators = parts[0::2], parts[1::2]
        digits = "".join(groups)
        offsets = [0, *accumulate(map(len, groups))]
        group_ending_at = {offset: index for index, offset in enumerate(offsets[1:])}
        # The totals only filter: _looks_like_card decides, so an error there cannot flag text.
        totals = _luhn_totals(digits)
        for start, first in enumerate(offsets[:-1]):
            if not digits.startswith(_CARD_PREFIXES, first):
                continue
            for last in range(first + 13, first + 20):
                end = group_ending_at.get(last)
                if (
                    end is not None
                    and (totals[last % 2][last] - totals[last % 2][first]) % 10 == 0
                    and _looks_like_card(digits[first:last])
                    and _separators_ok(separators, start, end)
                    and _layout_ok([len(group) for group in groups[start : end + 1]])
                ):
                    return True
    return False


def _iban_ok(candidate: str, numeric: str) -> bool:
    # In ``numeric`` the letters are digits, so the country code and check digits take six.
    # The minimum of digits keeps out prose that passes by chance, such as "py 37 ...".
    return (
        _IBAN_SHAPE_RE.fullmatch(candidate) is not None
        and int(numeric[6:] + numeric[:6]) % 97 == 1
        and sum(map(str.isdigit, candidate)) >= _IBAN_MIN_DIGITS
    )


def _contains_iban(text: str) -> bool:
    for match in _IBAN_WINDOW_RE.finditer(_IBAN_BREAK_RE.sub(",", text.upper())):
        # The end is unknown: a word after the number can look like one more group. Groups are
        # turned into digits once, as "AB12 " * n has a start in every group.
        tokens = _IBAN_SEPARATOR_RE.split(match[1])
        numerics = [token.translate(_IBAN_LETTER_VALUES) for token in tokens]
        if any(map(_iban_ok, accumulate(tokens), accumulate(numerics))):
            return True
    return False


def _contains_phone(text: str) -> bool:
    for match in _PHONE_RE.finditer(text):
        if 9 <= len(re.sub(r"\D", "", match.group())) <= 15:
            return True
    return False


def _tax_id_ok(digits: str) -> bool:
    total = sum(w * int(d) for w, d in zip(_TAX_ID_WEIGHTS, digits[:9], strict=True))
    return total % 11 % 10 == int(digits[9])


def _contains_tax_id(text: str) -> bool:
    # A bare 10-digit number is not enough: phone numbers are 10 digits too.
    for match in _TEN_DIGITS_RE.finditer(text):
        start, end = match.span()
        if _tax_id_ok(match.group()) and (
            _TAX_KEYWORD_BEFORE_RE.search(text, max(start - _TAX_KEYWORD_WINDOW, 0), start)
            or _TAX_KEYWORD_AFTER_RE.match(text, end, end + _TAX_KEYWORD_WINDOW)
        ):
            return True
    return False


def _has_digit(token: str) -> bool:
    return any(char.isdigit() for char in token)


def _has_digit_or_upper(token: str) -> bool:
    return any(char.isdigit() or char.isupper() for char in token)


def _has_symbol(token: str) -> bool:
    return any(not char.isalpha() and char not in "-_" for char in token)


def _has_both_cases(token: str) -> bool:
    return any(char.islower() for char in token) and any(char.isupper() for char in token)


def _is_real_value(value: str) -> bool:
    return _PLACEHOLDER_RE.fullmatch(value) is None


def _looks_like_code(value: str) -> bool:
    return (
        "(" in value
        or "[" in value
        or value.startswith(("/", "./", "../", "~/"))
        or _DOTTED_NAME_RE.fullmatch(value) is not None
    )


def _is_assigned_value(value: str) -> bool:
    return len(value) >= _MIN_VALUE_LENGTH and _is_real_value(value) and not _looks_like_code(value)


def _is_sk_token(match: re.Match[str]) -> bool:
    # A slug such as sk-hynix-q3-profit-hits-record-high-2024 is short words.
    return any(len(part) >= 20 and _has_digit_or_upper(part) for part in match[1].split("-"))


def _is_authorization(match: re.Match[str]) -> bool:
    return _has_digit(match[1]) and _is_real_value(match[1])


def _is_curl_password(match: re.Match[str]) -> bool:
    value = match[1]
    return (
        any(char.isalpha() for char in value)
        and _has_digit(value)
        and _is_real_value(value)
        and not value.startswith("/")  # "sqlmap -u https://...", where -u is a URL
    )


def _is_password_assignment(match: re.Match[str]) -> bool:
    value, gap, operator = match["value"].strip(_PROSE_PUNCTUATION), match["gap"], match["operator"]
    if len(value) < _MIN_VALUE_LENGTH or not _is_real_value(value):
        return False
    # An opening quote, so whatever follows is a literal, not code. The keyword's own closing quote
    # proves nothing: get("password").unwrap(). After a comma it may be the next item of a list.
    if gap.endswith(("'", '"')) and gap not in _ONLY_A_CLOSING_QUOTE:
        return operator != "," or (_has_digit(value) and "password" not in value.lower())
    if _looks_like_code(value) or _looks_like_code(match["value"]):
        return False
    if gap == "=":
        # Tight, as in .env files and URLs. An underscore makes it an identifier.
        return "_" not in value or _has_symbol(value)
    if operator == "==":
        return False  # A comparison.
    if operator == ">" and value.startswith("@"):
        return False  # A template: "<user>:<password>@host".
    if operator in (":", ":=", "=", ">", "=>", "->"):
        # GraphQL writes "password: String!".
        return _has_symbol(value.removesuffix("!"))
    if operator in ("is", "\U00000446\U00000435"):
        return _has_digit(value)
    # Only prose is left: "password sha256", "password argon2id", "password Windows10". After a
    # closing quote it is the next attribute: type="password" minLength={8}.
    return (
        not any(quote in gap for quote in "'\"")
        and _has_digit(value)
        and _has_both_cases(value)
        and _PRODUCT_WORD_RE.fullmatch(value) is None
    )


def _is_token_assignment(match: re.Match[str]) -> bool:
    value = match["value"].strip(_PROSE_PUNCTUATION)
    word = match.start("word")
    return (
        _is_assigned_value(value)
        and not _looks_like_code(match["value"])
        and _CAMEL_WORDS_RE.fullmatch(value) is None
        and not _NUMBER_RE.fullmatch(value)
        and (
            any(not char.isalpha() and char not in "-_." for char in value)
            or (len(value) >= _MIN_MIXED_CASE_LENGTH and _has_both_cases(value))
        )
        and not _NOT_A_CREDENTIAL_RE.search(
            match.string, max(word - _NOT_A_CREDENTIAL_WINDOW, 0), word
        )
    )


_CHECKED_SECRETS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], bool]], ...] = (
    (_SK_TOKEN_RE, _is_sk_token),
    (_BEARER_RE, lambda match: _has_digit(match[1])),
    (_AUTHORIZATION_RE, _is_authorization),
    (_CURL_USER_RE, _is_curl_password),
    (_URL_PASSWORD_RE, lambda match: _is_real_value(match[1])),
    (_PASSWORD_RE, _is_password_assignment),
    (_TOKEN_WORD_RE, _is_token_assignment),
    (_SECRET_FLAG_RE, _is_token_assignment),
)


def _contains_secret(text: str) -> bool:
    if _SECRET_RE.search(text):
        return True
    return any(
        check(match) for pattern, check in _CHECKED_SECRETS for match in pattern.finditer(text)
    )


def _decode_base64_runs(text: str) -> str:
    """Return the UTF-8 text hidden in the base64 runs of ``text``.

    The runs are joined by spaces and joined with nothing, because base64 tools wrap at 76
    columns and a value can be cut at a line end. Text with a percent sign is added in its
    decoded form, because base64 of a URL is how redirect and tracking links carry an address.
    """
    runs = dict.fromkeys(
        match.group() for regex in _BASE64_RUN_RES for match in regex.finditer(text)
    )
    decoded = []
    for run in runs:
        # In a run of 4n+1 characters the last one holds no full byte.
        body = run[:-1] if len(run) % 4 == 1 else run
        try:
            decoded.append(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        except ValueError:
            continue
    spaced, glued = " ".join(decoded), "".join(decoded)
    result = spaced if len(decoded) < 2 else f"{spaced} {glued}"
    return f"{result} {unquote_plus(glued)}" if "%" in glued else result


@dataclass(frozen=True, slots=True)
class PIIGuard:
    """Detect personal data and secrets in outgoing text.

    Attributes:
        detect_emails: E-mail addresses, with Unicode letters, spaces around the at sign,
            bracketed spellings such as ``[at]``, ``(@)``, ``(dot)`` and ``[.]``, and the
            escapes ``&#64;``, ``&#x40;``, ``&commat;``, a literal ``\\u0040`` and ``\\x40``.
            With spaces on both sides of a bare sign the left token needs 2 characters and
            not only digits, the last label of the domain no capitals, and a "(" must not
            follow the domain.
        detect_cards: Payment card numbers (13-19 digits, Luhn-valid, a prefix that fits the
            length, 16 digits with prefix 2-6 or 9804, the layout of a card).
        detect_ibans: IBANs (mod-97 valid, at least 10 digits), compact or in groups: of any
            size split by spaces, of up to 4 characters split by dashes, dots, underscores or
            slashes. A first group other than the country code and check digits must be a
            compact number of at least 15 characters.
        detect_phones: Sequences of 9-15 digits with phone separators. Off by default
            because version numbers and years can look like phone numbers.
        detect_tax_ids: Ukrainian tax numbers: 10 digits with a valid control digit, next
            to a keyword such as ``tax id``, ``ipn`` or ``rnokpp``. The keyword is shared with
            Russia, so some public company numbers count too.
        detect_secrets: API keys and tokens with a known prefix, private keys with their key
            material, JWTs, webhook URLs, ``Authorization`` headers, ``curl -u`` passwords,
            passwords in URLs, assignments such as ``password=...`` and ``API_KEY: ...``, and
            flags such as ``--token ...``. Defaults, placeholders, pagination tokens and
            values that read as code are skipped.
    """

    detect_emails: bool = True
    detect_cards: bool = True
    detect_ibans: bool = True
    detect_phones: bool = False
    detect_tax_ids: bool = True
    detect_secrets: bool = True

    def scan(self, text: str) -> list[str]:
        """Return the sorted kinds of personal data and secrets found in ``text``.

        ``?email=jane%40corp.example`` or ``?c=4111+1111+1111+1111`` only reveal the
        data after decoding, and ``%2540`` only after decoding it twice, so the text is
        decoded up to three times and every form is scanned, base64 runs included. This
        happens for plain text as well, because pasting a URL into a search is ordinary
        agent behaviour. A literal ``%40`` alone triggers nothing.
        """
        findings = self._scan(text, _BASE64_DEPTH)
        form = text
        for _ in range(_URL_DECODE_ROUNDS):
            decoded = unquote_plus(form)
            if decoded == form:
                break
            findings |= self._scan(decoded, _BASE64_DEPTH)
            form = decoded
        return sorted(findings)

    def scan_url(self, url: str) -> list[str]:
        """Return the sorted kinds of personal data and secrets found in ``url``.

        A URL needs nothing beyond :meth:`scan`, which decodes percent-encoding.
        """
        return self.scan(url)

    def check(self, text: str, *, what: str = "query") -> None:
        """Raise :class:`QueryRejectedError` if ``text`` contains personal data or secrets."""
        self._raise_if(self.scan(text), what)

    def check_url(self, url: str) -> None:
        """Raise :class:`QueryRejectedError` if ``url`` contains personal data or secrets.

        The URL is checked both as it is and decoded.
        """
        self._raise_if(self.scan_url(url), "URL")

    def _scan(self, text: str, depth: int) -> set[str]:
        text = _normalise(text)
        findings: set[str] = set()
        if self.detect_emails and _contains_email(text):
            findings.add("email")
        if self.detect_cards and _contains_card(text):
            findings.add("payment_card")
        if self.detect_ibans and _contains_iban(text):
            findings.add("iban")
        if self.detect_phones and _contains_phone(text):
            findings.add("phone")
        if self.detect_tax_ids and _contains_tax_id(text):
            findings.add("tax_id")
        if self.detect_secrets and _contains_secret(text):
            findings.add("secret")
        if depth:
            findings |= self._scan(_decode_base64_runs(text), depth - 1)
        return findings

    @staticmethod
    def _raise_if(findings: list[str], what: str) -> None:
        if findings:
            raise QueryRejectedError(
                f"The {what} was not sent: it appears to contain personal data or secrets "
                f"({', '.join(findings)}). Remove it or disable the PII guard.",
                findings=findings,
            )
