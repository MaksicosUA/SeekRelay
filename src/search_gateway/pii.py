"""A small, conservative guard that stops personal data from leaving the process.

Search queries and URLs are sent to third parties. Agents sometimes build queries out
of whatever is in their context, including e-mail addresses or payment details. The
guard rejects such requests *before* any network call.

Design choices:

* High precision over recall. Card numbers must pass the Luhn check and IBANs the
  ISO 13616 mod-97 check, so ordinary numbers in queries ("python 3.12 2024") do not
  trigger it. Phone detection is off by default because it is noisy.
* Digit runs are checked in every contiguous window of groups, so a card number is
  still found when an expiry date, CVV or order number sits next to it.
* The guard reports *kinds* of findings, never the values, so its errors are safe
  to log.
* It is a seatbelt, not a DLP system. Names, addresses and free-text secrets are not
  detected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

from .errors import QueryRejectedError

__all__ = ["PIIGuard"]

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
# Digit groups separated by single spaces or dashes ("4111 1111 1111 1111 123").
_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d+(?:[ -]\d+)*(?!\d)")
# Country code + check digits, then compact or space-grouped characters.
_IBAN_CANDIDATE_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{1,4}){3,9})\b")
_PHONE_RE = re.compile(r"(?<![\w+.])\+?\d[\d ()-]{7,}\d(?![\w.])")


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
    # Major networks start with 2-6 (Mir/Mastercard 2/5, Amex/JCB 3, Visa 4, Discover 6).
    # Repeated digits ("0000 0000 ...") pass Luhn but are never real card numbers.
    return (
        13 <= len(digits) <= 19
        and digits[0] in "23456"
        and len(set(digits)) > 1
        and _luhn_ok(digits)
    )


def _contains_card(text: str) -> bool:
    for match in _DIGIT_RUN_RE.finditer(text):
        groups = re.split(r"[ -]", match.group())
        for start in range(len(groups)):
            digits = ""
            for group in groups[start:]:
                digits += group
                if len(digits) > 19:
                    break
                if _looks_like_card(digits):
                    return True
    return False


def _iban_ok(candidate: str) -> bool:
    iban = candidate.replace(" ", "")
    if not 15 <= len(iban) <= 34 or not iban.isalnum():
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def _contains_iban(text: str) -> bool:
    for match in _IBAN_CANDIDATE_RE.finditer(text.upper()):
        tokens = match.group().split(" ")
        # A following short word ("... 2874 FOR") can be swallowed as a last group,
        # so retry with trailing groups dropped.
        ends = range(len(tokens), 1, -1) if len(tokens) > 1 else [1]
        if any(_iban_ok(" ".join(tokens[:end])) for end in ends):
            return True
    return False


def _contains_phone(text: str) -> bool:
    for match in _PHONE_RE.finditer(text):
        if 9 <= len(re.sub(r"\D", "", match.group())) <= 15:
            return True
    return False


@dataclass(frozen=True, slots=True)
class PIIGuard:
    """Detect personal data in outgoing text.

    Attributes:
        detect_emails: E-mail addresses.
        detect_cards: Payment card numbers (13-19 digits, prefix 2-6, Luhn-valid).
        detect_ibans: IBANs (mod-97 valid), compact or in groups of four.
        detect_phones: Sequences of 9-15 digits with phone separators. Off by default
            because version numbers and years can look like phone numbers.
    """

    detect_emails: bool = True
    detect_cards: bool = True
    detect_ibans: bool = True
    detect_phones: bool = False

    def scan(self, text: str) -> list[str]:
        """Return the sorted kinds of personal data found in ``text``."""
        findings: set[str] = set()
        if self.detect_emails and _EMAIL_RE.search(text):
            findings.add("email")
        if self.detect_cards and _contains_card(text):
            findings.add("payment_card")
        if self.detect_ibans and _contains_iban(text):
            findings.add("iban")
        if self.detect_phones and _contains_phone(text):
            findings.add("phone")
        return sorted(findings)

    def scan_url(self, url: str) -> list[str]:
        """Like :meth:`scan`, but also looks at the percent-decoded URL.

        ``?email=jane%40corp.example`` or ``?c=4111+1111+1111+1111`` only reveal the
        data after decoding.
        """
        return self.scan(f"{url} {unquote_plus(url)}")

    def check(self, text: str, *, what: str = "query") -> None:
        """Raise :class:`QueryRejectedError` if ``text`` contains personal data."""
        self._raise_if(self.scan(text), what)

    def check_url(self, url: str) -> None:
        """Raise :class:`QueryRejectedError` if ``url`` (decoded or not) contains personal data."""
        self._raise_if(self.scan_url(url), "URL")

    @staticmethod
    def _raise_if(findings: list[str], what: str) -> None:
        if findings:
            raise QueryRejectedError(
                f"The {what} was not sent: it appears to contain personal data "
                f"({', '.join(findings)}). Remove it or disable the PII guard.",
                findings=findings,
            )
