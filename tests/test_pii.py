from __future__ import annotations

import pytest

from search_gateway import PIIGuard, QueryRejectedError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("contact john.doe+jobs@example.co.uk about the vacancy", ["email"]),
        ("card 4111 1111 1111 1111 expired", ["payment_card"]),
        ("pay 4111-1111-1111-1111 now", ["payment_card"]),
        ("iban DE89 3704 0044 0532 0130 00 please", ["iban"]),
        ("iban GB82WEST12345698765432", ["iban"]),
        ("send to a@b.io and 4111111111111111", ["email", "payment_card"]),
    ],
)
def test_detects_personal_data(text: str, expected: list[str]) -> None:
    assert PIIGuard().scan(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "python 3.12 release notes 2024 2025",
        "order 1234567890123456 tracking",  # 16 digits but not Luhn-valid
        "iban DE00 0000 0000 0000 0000 00",  # wrong checksum
        "serial 0000 0000 0000 0000",  # Luhn-valid but not a card
        "isbn 9780134685991 and ean 4006381333931",  # 13 digits, not cards
        "use @decorator in python",
        "call +380 67 123 45 67",  # phones are off by default
        "fastapi vs django performance benchmark",
    ],
)
def test_ignores_ordinary_queries(text: str) -> None:
    assert PIIGuard().scan(text) == []


def test_phone_detection_is_opt_in() -> None:
    assert PIIGuard(detect_phones=True).scan("call +380 67 123 45 67") == ["phone"]


def test_check_raises_without_leaking_values() -> None:
    with pytest.raises(QueryRejectedError) as info:
        PIIGuard().check("who is jane@corp.example")
    assert info.value.findings == ("email",)
    assert "jane@corp.example" not in str(info.value)


def test_can_disable_individual_detectors() -> None:
    guard = PIIGuard(detect_emails=False)
    assert guard.scan("jane@corp.example") == []


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1111 12/26",  # expiry date after the number
        "card 4111 1111 1111 1111 123",  # CVV after the number
        "order 12 4111 1111 1111 1111",  # unrelated digits before it
        "5500-0000-0000-0004 exp 01-28",
    ],
)
def test_card_found_next_to_other_digits(text: str) -> None:
    assert PIIGuard().scan(text) == ["payment_card"]


@pytest.mark.parametrize(
    "text",
    [
        "PL61 1090 1014 0000 0712 1981 2874 from my account",
        "ES91 2100 0418 4502 0005 1332 for rent",
        "pay to de89370400440532013000 today",
    ],
)
def test_iban_found_before_short_words(text: str) -> None:
    assert PIIGuard().scan(text) == ["iban"]


def test_url_scan_decodes_percent_and_plus() -> None:
    guard = PIIGuard()
    assert guard.scan("https://a.com/?email=jane%40corp.example") == []
    assert guard.scan_url("https://a.com/?email=jane%40corp.example") == ["email"]
    assert guard.scan_url("https://a.com/?c=4111+1111+1111+1111") == ["payment_card"]
    with pytest.raises(QueryRejectedError):
        guard.check_url("https://a.com/?email=jane%40corp.example")


def test_phone_detection_ignores_versions_and_years() -> None:
    guard = PIIGuard(detect_phones=True)
    assert guard.scan("python 3.12 2024 2025") == []
    assert guard.scan("call (044) 123-45-67 now") == ["phone"]
