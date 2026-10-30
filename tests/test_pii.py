from __future__ import annotations

import base64
import random
import sys
import time
import unicodedata
from collections.abc import Callable
from urllib.parse import quote

import pytest

from seekrelay import PIIGuard, QueryRejectedError
from seekrelay.pii import (
    _HIDDEN_MARKS,
    _cap_mark_runs,
    _is_real_value,
    _luhn_ok,
    _luhn_totals,
    _normalise,
)

EMAIL = "jane@corp.example"
CARD = "4111 1111 1111 1111"
PROSTIR_CARD = "9804 1234 5678 9014"  # fictional, Luhn-valid
IBAN = "UA21-3223-1300-0002-6007-2335-6600-1"
TAX_ID = "1234567899"  # fictional, valid control digit
TAX_ID_SUM_10 = "1000000000"  # fictional, the sum is -1
WRONG_TAX_ID = "1234567890"
UPPER_INN = "\u0406\u041d\u041d"  # the one spelling made of Latin look-alikes only
SNAIL = "\u0440\u0430\u0432\u043b\u0438\u043a"  # the Ukrainian name of the at sign
DOT_UK = "\u043a\u0440\u0430\u043f\u043a\u0430"
TAXPAYER_UK = "\u043f\u043b\u0430\u0442\u043d\u0438\u043a\u0430"
NUMBER_RU = "\u043d\u043e\u043c\u0435\u0440"
TIME_LIMIT = 2.0


def pem(label: str) -> str:
    return "-" * 5 + f"BEGIN {label}" + "-" * 5


# Built at runtime: GitHub push protection rejects anything that looks like a real token.
KEY_MATERIAL = "a1B2+/c3" * 2
SECRETS = {
    "pem-rsa": pem("RSA PRIVATE KEY") + "\n" + KEY_MATERIAL,
    "pem-bare": pem("PRIVATE KEY") + "\n" + KEY_MATERIAL,
    "pem-openssh": pem("OPENSSH PRIVATE KEY") + "\n" + KEY_MATERIAL,
    "pem-encrypted": pem("ENCRYPTED PRIVATE KEY") + "\n" + KEY_MATERIAL,
    "pem-pgp": pem("PGP PRIVATE KEY BLOCK") + "\n\n" + KEY_MATERIAL,
    "pem-no-line-break": pem("PRIVATE KEY") + KEY_MATERIAL,
    "pem-literal-backslash-n": pem("PRIVATE KEY") + "\\n" + KEY_MATERIAL,
    "pem-literal-backslash-r": pem("RSA PRIVATE KEY") + "\\r\\n" + KEY_MATERIAL,
    "pem-legacy-encrypted": pem("RSA PRIVATE KEY") + "\nProc-Type: 4,ENCRYPTED",
    "pem-pgp-version": pem("PGP PRIVATE KEY BLOCK") + "\nVersion: GnuPG v2",
    "pem-pgp-comment": pem("PGP PRIVATE KEY BLOCK") + "\nComment: made by hand",
    "github-ghp": "ghp_" + "a1B2" * 9,
    "github-gho": "gho_" + "a1B2" * 9,
    "github-ghu": "ghu_" + "a1B2" * 9,
    "github-ghs": "ghs_" + "a1B2" * 9,
    "github-ghr": "ghr_" + "a1B2" * 9,
    "github-pat": "github_pat_" + "a1B2_" * 5,
    "gitlab": "glpat-" + "a1B2-" * 4,
    "hugging-face": "hf_" + "a1B2" * 8,
    "npm": "npm_" + "a1B2" * 9,
    "tavily": "tvly-" + "a1_2-" * 4,
    "brave": "BSA" + "a1B2-" * 5,
    "sk-digit": "sk-" + "abcd1" * 4,
    "sk-upper": "sk-" + "abcdE" * 4,
    "stripe-sk-live": "sk_live_" + "a1B2" * 4,
    "stripe-sk-test": "sk_test_" + "a1B2" * 4,
    "stripe-rk-live": "rk_live_" + "a1B2" * 4,
    "stripe-rk-test": "rk_test_" + "a1B2" * 4,
    "aws-akia": "AKIA" + "A1B2" * 4,
    "aws-asia": "ASIA" + "C3D4" * 4,
    "google": "AIza" + "a1B-_" * 7,
    "slack-a": "xoxa-" + "1a2b3" * 2,
    "slack-b": "xoxb-" + "1a2b3" * 2,
    "slack-e": "xoxe-" + "1a2b3" * 2,
    "slack-o": "xoxo-" + "1a2b3" * 2,
    "slack-p": "xoxp-" + "1a2b3" * 2,
    "slack-r": "xoxr-" + "1a2b3" * 2,
    "slack-s": "xoxs-" + "1a2b3" * 2,
    "jwt": ".".join(("eyJ" + "a1B2" * 3, "eyJ" + "c3D4" * 3, "e5F6-_" * 2)),
    "jwt-shortest": ".".join(("eyJ" + "a" * 7, "eyJ" + "b" * 7, "c" * 10)),
    "telegram": "123456789:" + "a1B-_" * 7,
    "telegram-8-digits": "12345678:" + "a1B-_" * 7,
    "telegram-10-digits": "1234567890:" + "a1B-_" * 7,
    "telegram-url": "https://api.telegram.org/bot123456789:" + "a1B-_" * 7 + "/getMe",
    "aws-after-dash": "x-AKIA" + "A1B2" * 4,
    "aws-after-underscore": "x_ASIA" + "C3D4" * 4,
    "google-after-underscore": "key_AIza" + "a1B-_" * 7,
    "jwt-after-dash": "jwt-" + ".".join(("eyJ" + "a1B2" * 3, "eyJ" + "c3D4" * 3, "e5F6-_" * 2)),
    "jwt-after-underscore": "x_" + ".".join(("eyJ" + "a" * 7, "eyJ" + "b" * 7, "c" * 10)),
    "slack-webhook": "https://hooks.slack.com/services/T"
    + "a1B2" * 2
    + "/B"
    + "c3D4" * 2
    + "/"
    + "e5F6" * 6,
    "slack-app": "xapp-1-" + "A1b2C" * 2,
    "discord-webhook": "https://discord.com/api/webhooks/" + "123456" + "/" + "a1B-_" * 4,
    "discordapp-webhook": "https://discordapp.com/api/webhooks/" + "1234567" + "/" + "a1B-_" * 4,
    "google-oauth": "ya29." + "a1B-_" * 2,
    "google-oauth-long": "ya29." + "a1B-_" * 40,
    "google-client-secret": "GOCSPX-" + "a1B-_" * 4,
    "pypi": "pypi-AgE" + "a1B-_" * 4,
    "docker-hub": "dckr_pat_" + "a1B-_" * 4,
    "groq": "gsk_" + "a1B2" * 10,
    "sendgrid": "SG." + "a1B-" * 4 + "." + "c3D_" * 5,
    "sk-project-key": "sk-proj-" + "a1B2" * 5,
    "sk-anthropic-key": "sk-ant-api03-" + "a1B2" * 6,
    "bearer": "Bearer " + "a1B2" * 5,
    "bearer-lower": "bearer " + "abcde1" * 4,
    "bearer-colon": "Bearer: " + "a1B2" * 5,
    "bearer-colon-tight": "bearer:" + "a1B2" * 5,
    "bearer-equals": "bearer=" + "a1B2" * 5,
    "authorization-short-bearer": "Authorization: Bearer " + "abc123def456",
    "authorization-basic": "Authorization: Basic " + base64.b64encode(b"admin:Hunter22x9").decode(),
    "authorization-token": "Authorization: Token " + "a1B2" * 8,
    "authorization-apikey": "authorization: apikey " + "a1B2c3D4",
    "authorization-no-scheme": "Authorization: " + "a1B2c3D4",
    "authorization-quoted": '"Authorization": "Bearer ' + "abc123def456" + '"',
    "authorization-equals": "authorization=" + "a1B2c3D4",
    "curl-u": "curl -u admin:" + "Hunter22x9" + " https://api.internal/",
    "curl-u-tight": "curl -uadmin:" + "Hunter22x9",
    "curl-user": "curl --user admin:" + "Hunter22x9" + " https://api.internal/",
    "url-password": "postgres://admin:" + "hunter22" + "@localhost:5432/app",
    "url-password-no-user": "redis://:" + "hunter22" + "@localhost:6379/0",
    "url-password-symbol": "postgres://admin:" + "S3cr3t!" + "@10.0.0.5/prod",
    "url-password-my-prefix": "redis://:" + "myS3cret99" + "@cache:6379/0",
    "password": "password=hunter22",
    "password-spaces": "PASSWORD : hunter22",
    "password-quoted": 'password="hunter22"',
    "password-json": '"password": "hunter22"',
    "passwd": "passwd=hunter22",
    "pwd": "pwd=hunter22",
    "passphrase": "passphrase: hunter22",
    "parol-numeric": "пароль: 123456",
    "parol-upper": "ПАРОЛЬ=qwerty1",
    "secret": "secret=abc123def",
    "client-secret": "client_secret=abc123def",
    "api-key-underscore": "api_key=abc123def",
    "api-key-dash": "api-key: abc123def",
    "api-key-joined": "APIKEY=abc123def",
    "access-token": "access_token=abc123def",
    "auth-token": "auth_token=abc123def",
    "token": "?token=abc123def",
    "token-symbol": "token=abcdef!ghi",
    "password-final-symbol": "password=letmein!",
}


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
    assert guard.scan("https://a.com/?email=jane%40corp.example") == ["email"]
    assert guard.scan_url("https://a.com/?email=jane%40corp.example") == ["email"]
    assert guard.scan("https://a.com/?c=4111+1111+1111+1111") == ["payment_card"]
    assert guard.scan_url("https://a.com/?c=4111+1111+1111+1111") == ["payment_card"]
    with pytest.raises(QueryRejectedError):
        guard.check("https://a.com/?email=jane%40corp.example")
    with pytest.raises(QueryRejectedError):
        guard.check_url("https://a.com/?email=jane%40corp.example")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("jane%40corp.example", ["email"]),
        ("jane%2540corp.example", ["email"]),
        ("jane%252540corp.example", ["email"]),
        ("4111%201111%201111%201111", ["payment_card"]),
        ("4111+1111+1111+1111", ["payment_card"]),
        ("password%3DHunter22x9", ["secret"]),
        ("DE89%203704%200044%200532%200130%2000", ["iban"]),
        ("tax_id%3D1234567899", ["tax_id"]),
        ("jane%25252540corp.example", []),  # four rounds
        ("100%40 off", []),  # a literal %40 alone triggers nothing
        ("50%25 of c++ users", []),
        ("%ZZ", []),
    ],
)
def test_query_is_decoded_like_a_url(text: str, expected: list[str]) -> None:
    assert PIIGuard().scan(text) == expected
    assert PIIGuard().scan_url(text) == expected


def test_phone_detection_ignores_versions_and_years() -> None:
    guard = PIIGuard(detect_phones=True)
    assert guard.scan("python 3.12 2024 2025") == []
    assert guard.scan("call (044) 123-45-67 now") == ["phone"]


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def percent_encoded(value: str, rounds: int) -> str:
    for _ in range(rounds):
        value = quote(value, safe="")
    return value


def fullwidth(text: str) -> str:
    return "".join(chr(ord(char) + 0xFEE0) for char in text)


def with_digits(text: str, zero: int) -> str:
    return "".join(chr(zero + int(char)) if char.isdigit() else char for char in text)


@pytest.mark.parametrize(
    "text",
    [
        "иван@пример.испытание",  # the Unicode spelling of xn--80akhbyknj4f
        "иван.петров@корпорация.испытание",
        "jane@corp.xn--80akhbyknj4f",
        "іванна@приклад.испытание",
        "张伟@例子.测试",
        "jane\uff20corp.example",
        "jane\ufe6bcorp.example",
        fullwidth("JANE@CORP.EXAMPLE"),
        "jane @ corp.example",
        "jane@ corp.example",
        "jane\t@\ncorp.example",
        "jane\u00a0@\u00a0corp.example",
        "mail jane@corp.example.",
        "jane@corp.example\u2014thanks",  # the em dash becomes "-" but still ends the address
        "jane@corp.example_x",
        "jane@163.example",  # a host that starts with digits is still a host
        "a" * 200 + "@corp.example",
        "a" + "." * 63 + "@corp.example",  # the last character of the 64 on the left
        "jane+@corp.example",
        "jane-@corp.example",
        "jane.@corp.example",
        "jane_@corp.example",
        "jane%@corp.example",
    ],
)
def test_email_variants(text: str) -> None:
    assert PIIGuard().scan(text) == ["email"]


@pytest.mark.parametrize(
    "at",
    ["[at]", "(at)", "{at}", "<at>", "[AT]", "(At)", "[ at ]", "[@]", "(@)", "{@}", "<@>", "[ @ ]"],
)
def test_bracketed_at(at: str) -> None:
    assert PIIGuard().scan(f"jane{at}corp.example") == ["email"]
    assert PIIGuard().scan(f"jane {at} corp.example") == ["email"]


@pytest.mark.parametrize("at", ["[собака]", "(собака)", "{собака}", "<собака>", "(СОБАКА)"])
def test_bracketed_at_in_russian(at: str) -> None:
    assert PIIGuard().scan(f"jane{at}corp.example") == ["email"]
    assert PIIGuard().scan(f"jane {at} corp.example") == ["email"]


@pytest.mark.parametrize(
    "at", [f"[{SNAIL}]", f"({SNAIL})", f"{{{SNAIL}}}", f"<{SNAIL}>", f"({SNAIL.upper()})"]
)
def test_bracketed_at_in_ukrainian(at: str) -> None:
    assert PIIGuard().scan(f"jane{at}corp.example") == ["email"]
    assert PIIGuard().scan(f"jane {at} corp.example") == ["email"]


@pytest.mark.parametrize(
    "at",
    [
        "&#64;",
        "&#064;",
        "&#0000064;",
        "&#x40;",
        "&#X40;",
        "&#x0040;",
        "&commat;",
        "\\" + "u0040",
        "\\" + "x40",
    ],
)
def test_text_escapes_of_the_at_sign(at: str) -> None:
    assert PIIGuard().scan(f"jane{at}corp.example") == ["email"]


def test_quoted_printable_at_sign_is_left_out() -> None:
    assert PIIGuard().scan("jane=40corp.example") == []


@pytest.mark.parametrize(
    "dot",
    [
        "[dot]",
        "(dot)",
        "{dot}",
        "<dot>",
        "(DOT)",
        "[.]",
        "(.)",
        "{.}",
        "<.>",
        "[ . ]",
        f"[{DOT_UK}]",
        f"({DOT_UK})",
        f"({DOT_UK.upper()})",
        "[точка]",
        "(точка)",
        "{точка}",
        "<точка>",
    ],
)
def test_bracketed_dot_in_the_domain(dot: str) -> None:
    assert PIIGuard().scan(f"jane[at]corp{dot}example") == ["email"]
    assert PIIGuard().scan(f"jane [at] corp {dot} example") == ["email"]
    assert PIIGuard().scan(f"jane@corp{dot}co{dot}uk") == ["email"]


@pytest.mark.parametrize(
    "text",
    [
        "use @app.route in flask",
        "@pytest.fixture scope",
        "npm install @types/node",
        "@babel/core vs @swc/core",
        "use @decorator in python",
        "use [at]app.route in flask",
        "look at the dot com bubble",
        "jane at corp dot example",
        "lodash@4.17.21",
        "https://cdn.jsdelivr.net/npm/lodash@4.17.21/lodash.min.js",
        "https://unpkg.com/@babel/core@7.24.0/lib/index.js",
        "https://a.example/@jane.doe/some-post",
        "pip install pkg==1.0 pkg@1.2.3-rc.1",
        "docker pull nginx@sha256:abcdef",
        "price @ 3.50 per unit",
        "user@localhost",
        "jane@corp.x",
        "jane@.example",
        "jane@corp..example",
        "jane@corp_x.example",
        "a" + "." * 64 + "@corp.example",  # the only letter is out of the 64 on the left
        "@ corp.example",
        "... @ corp.example",
    ],
)
def test_email_lookalikes_are_ignored(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "jane @ corp.example",
        "me @ corp.example",
        "jane.doe @ corp.example.",
        "write to jane @ corp.example, please",
        "(jane @ corp.example)",
        "\u0456\u0432\u0430\u043d @ \u043a\u043e\u0440\u043f.\u0443\u043a\u0440",
        "\u5f20\u4f1f @ \u4f8b\u5b50.\u6d4b\u8bd5",  # these scripts have no capitals
        "PS5 Pro bundle @ amazon.de cheapest",  # an accepted cost
        "buy laptops @ amazon.com",
    ],
)
def test_spaces_around_the_at_sign(text: str) -> None:
    assert PIIGuard().scan(text) == ["email"]


@pytest.mark.parametrize(
    "text",
    [
        "numpy A @ np.linalg.inv(B) singular matrix error",
        "python x @ self.weights shape mismatch pytorch",
        "y = X @ self.weights",
        "torch A @ B.transpose(0, 1) dimension",
        "scala case p @ Person.Adult(_) pattern",
        "numpy X @ y.T",
        "tickets Cubs @ St.Louis 2026 opening day",
        "iPhone 15 @ rozetka.com.ua",
        "2 @ 5.99",
        "dinner @ 7.30pm",
        "matrix @ np.linalg.inv(B)",  # a call right after the domain
        "jane @ Corp.Example",  # a capital in the last label
        "Cubs @ St.Louis.",
    ],
)
def test_spaced_at_sign_that_is_no_address(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "JANE (AT) CORP (DOT) EXAMPLE",
        "Jane [at] Corp.Example",
        "x [at] corp.example",
        "15 [at] corp.example",
        "Hello [at] world.com",
    ],
)
def test_brackets_around_the_sign_need_no_further_evidence(text: str) -> None:
    assert PIIGuard().scan(text) == ["email"]


@pytest.mark.parametrize(
    "text",
    [
        "4111.1111.1111.1111",
        "4111_1111_1111_1111",
        "4111/1111/1111/1111",
        "3782.822463.10005",
        "3782/822463/10005",
        "card 4111.1111.1111.1111 exp 12/26",
        "order 12.34 4111.1111.1111.1111",
        "4111\u20131111\u20131111\u20131111",
        "4111\u22121111\u22121111\u22121111",
        "4111\u00a01111\u00a01111\u00a01111",
        "4111  1111\t1111\n1111",
        "4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1",
        PROSTIR_CARD,
        "9804-1234-5678-9014",
        "9804.1234.5678.9014",
        "9804123456789014",
        "4111 1111-1111 1111",  # a space and a dash may mix, the other separators may not
        "4222222222222",  # 13 digits
        "378282246310005",  # 15 digits
        "4000000000000000006",  # 19 digits
        "7 378282246310005 7",  # windows start at odd and even offsets of the run
        "77 378282246310005 77",
        "1 4111 1111 1111 1111 1",
        "12 4111 1111 1111 1111 12",
        "123 4111 1111 1111 1111 123",
    ],
)
def test_card_formats(text: str) -> None:
    assert PIIGuard().scan(text) == ["payment_card"]


@pytest.mark.parametrize(
    "text",
    [
        "25.10.2024 26.10.2024",
        "2024/10/30 2024/10/31",
        "30/10/2024 31/10/2024",
        "1.2.3.4",
        "python 3.12.1 numpy 1.26.4 pandas 2.2.0",
        "192.168.100.200",
        "192.168.100.200.172.16.254.100",
        "4111.1111 1111.1111",  # no single separator throughout the window
        "4111.1111/1111.1111",
        "41.11.11.11.11.11.11.11",  # groups shorter than 4 digits
        "411.111.111.111.1111",
        "4.111.111.111.111.111",
        "41.1111.1111.1111.11111",  # only the first group is short
        "4222.2222.2222.2",  # only the last group is short
        "411.1111.1111.1111.11",
        "4000.0000.0000.0000.006",
        "41111111.11111111",  # two groups: only 3 or more make a window with these separators
        "41111111/11111111",
        "41111111_11111111",
        "411111.1111111111",
        "4111..1111..1111..1111",  # two separators in a row
        "9780134685991",
        "9790134685990",
        "978-0-13-468599-1",
        "979-8-88645-174-0",
        "9804 1234 5678 9015",  # wrong check digit
        "9805 1234 5678 9013",  # Luhn-valid, but not the PROSTIR range
        "4" * 17,  # Luhn-valid, but repeated digits
        "4111--1111--1111--1111",  # two separators in a row
        "4111 - 1111 - 1111 - 1111",
    ],
)
def test_card_lookalikes_are_ignored(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "10.1145/3292500.3330701 Optuna",
        "https://dl.acm.org/doi/10.1145/2959100.2959190",
        "https://stackoverflow.com/a/56789012/9401096",
        "IMG_20160911_052452.jpg exif data",
        "https://cdn.example.com/photos/IMG_20241030_143008.jpg",
        "backup_20241030_001800.sql",
        "https://www.example.com/page?_ga=2.880333222.413198897.1767045617-413198897.1767045617",
        "https://a.example/LC08_L1TP_101035_20200610_20200824_01_T1/index.html",
    ],
)
def test_two_numbers_joined_once_are_no_card(text: str) -> None:
    assert PIIGuard().scan(text) == []


def test_luhn_totals_agree_with_the_luhn_check() -> None:
    rng = random.Random(0)
    digits = "".join(rng.choices("0123456789", k=400))
    totals = _luhn_totals(digits)
    for first in range(len(digits) - 19):
        for last in range(first + 13, first + 20):
            filtered = (totals[last % 2][last] - totals[last % 2][first]) % 10 == 0
            assert filtered == _luhn_ok(digits[first:last])


@pytest.mark.parametrize(
    "text",
    [
        IBAN,
        "UA213223130000026007233566001",
        "UA21 3223 1300 0002 6007 2335 6600 1",
        "DE89-3704-0044-0532-0130-00",
        "de89-3704-0044-0532-0130-00",
        "DE89\u20133704\u20130044\u20130532\u20130130\u201300",
        "DE89-3704 0044-0532 0130-00",
        "iban DE89-3704-0044-0532-0130-00 for rent",
        "DE89370400440532013000.",
        "DE89370400440532013000,",
        "DE89370400440532013000;",
        "(DE89370400440532013000)",
        '"DE89370400440532013000"',
        "DE89 3704 0044 0532 0130 00.",
        "(DE89 3704 0044 0532 0130 00)",
        "DE89-3704-0044-0532-0130-00.",
        "https://de89370400440532013000.a.example/",
        "https://a.example/de89370400440532013000/details",
        "de89370400440532013000_x",
    ],
)
def test_iban_formats(text: str) -> None:
    assert PIIGuard().scan(text) == ["iban"]


# Each of these holds a window of groups that passes the mod-97 check.
IBAN_SHAPED_IDENTIFIERS = [
    "7cec3272-b3b1-ab03-4d9a-4a0867974bc2",
    "https://example.com/uk/2024/11/22/2502098/some-story-title-here",
    "/us/34/88384037/7996/story",
    "LC08_L1TP_960839_20201170_20201209_01_T1",
    "US 2003/2510018 A1",
    "in 2024 8765330 units sold",
    "AB12CD34 9555 8430 8997",
]


def iban_with_check_digits(country: str, body: str) -> str:
    numeric = "".join(str(int(char, 36)) for char in f"{body}{country}00")
    return f"{country}{98 - int(numeric) % 97:02d}{body}"


@pytest.mark.parametrize(
    "text",
    [
        "UA21 322313 0000026007233566001",
        "UA 21 3223 1300 0002 6007 2335 6600 1",
        "UA 21 322 313 000 002 600 723 356 600 1",
        "UA21 322 313 000 002 600 723 356 600 1",
        "DE 89 3704 0044 0532 0130 00",
        "DE-89-3704-0044-0532-0130-00",
        "DE89 37040044 0532013000",
        "DE89.3704.0044.0532.0130.00",
        "DE89/3704/0044/0532/0130/00",
        "DE89_3704_0044_0532_0130_00",
        "UA21_3223_1300_0002_6007_2335_6600_1",
        "iban_DE89370400440532013000",
        "iban-DE89370400440532013000",
        "IBAN:DE89370400440532013000",
        "https://a.example/DE89/3704/0044/0532/0130/00",
        "https://a.example/?i=UA21_3223_1300_0002_6007_2335_6600_1",
        "AB12 DE89 3704 0044 0532 0130 00",
        "FY24 UA21 3223 1300 0002 6007 2335 6600 1",
        "AB12 DE89370400440532013000",
        "AB12 CD34 EF56 GH78 DE89 3704 0044 0532 0130 00",
        "DE00 DE89 3704 0044 0532 0130 00",  # a mistyped number first
    ],
)
def test_iban_in_any_grouping(text: str) -> None:
    assert PIIGuard().scan(text) == ["iban"]


def test_longest_iban_in_groups_of_three_with_the_country_code_split_off() -> None:
    iban = iban_with_check_digits("MT", "A12" * 10)
    assert len(iban) == 34
    body = iban[4:]
    groups = [body[index : index + 3] for index in range(0, 30, 3)]
    assert PIIGuard().scan(" ".join((iban[:2], iban[2:4], *groups))) == ["iban"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("THISISPLAINPROSEHERE", [], id="only-the-check-digits"),
        pytest.param("PLAINPROSE1234567", [], id="9-digits"),
        pytest.param("PLAINPROSE12345678", ["iban"], id="10-digits"),
    ],
)
def test_iban_needs_ten_digits(body: str, expected: list[str]) -> None:
    assert PIIGuard().scan(iban_with_check_digits("PY", body)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "DE89370400440532013000abc",  # glued to a word: a limit
        "DE00 3704 0044 0532 0130 00",
        "AB12 CD34 EF56 GH78 IJ90 KL12",
        "AB12-CD34-EF56-GH78",
        "xDE89 3704 0044 0532 0130 00",  # the start of the number is inside a word
        "1DE89370400440532013000",
        *IBAN_SHAPED_IDENTIFIERS,
    ],
)
def test_iban_lookalikes(text: str) -> None:
    assert PIIGuard().scan(text) == []


def test_iban_with_dashes_and_a_wrong_checksum_is_ignored() -> None:
    assert PIIGuard().scan("DE00-3704-0044-0532-0130-00") == []


@pytest.mark.parametrize(
    "keyword",
    [
        "РНОКПП",
        "рнокпп",
        "ІПН",
        "іпн",
        "ИНН",
        "инн",
        UPPER_INN,
        "інн",
        "податковий номер",
        "податковий код",
        "податкового номера",
        "податковому коду",
        "ідентифікаційний код",
        "ідентифікаційний номер",
        "ідентифікаційного номера",
        "ідентифікаційним кодом",
        "идентификационный код",
        "идентификационный номер",
        "идентификационного номера",
        "tax id",
        "Tax ID",
        "tax_id",
        "taxpayer id",
        "TIN",
        "tin",
        "ipn",
        "IPN",
        "inn",
        "INN",
        "rnokpp",
        "RNOKPP",
        "drfo",
        "DRFO",
        "\u0434\u0440\u0444\u043e",
        "\u0414\u0420\u0424\u041e",
        "i\u043f\u043d",  # with a Latin i
        "I\u041f\u041d",
        "i\u043d\u043d",
        f"\u0406\u041f\u041d {TAXPAYER_UK}",
        f"i\u043f\u043d {TAXPAYER_UK}",
        "tax number",
        "Tax Number",
        "tax_number",
        "tax no",
        "tax-no",
        "tax code",
        "tax identification number",
        "Tax Identification Number",
        f"\u043d\u0430\u043b\u043e\u0433\u043e\u0432\u044b\u0439 {NUMBER_RU}",
        "\u043d\u0430\u043b\u043e\u0433\u043e\u0432\u044b\u0439 \u043a\u043e\u0434",
        f"\u043d\u0430\u043b\u043e\u0433\u043e\u0432\u043e\u0433\u043e {NUMBER_RU}",
        f"\u043a\u043e\u0434 {TAXPAYER_UK} \u043f\u043e\u0434\u0430\u0442\u043a\u0456\u0432",
    ],
)
def test_tax_id_next_to_a_keyword(keyword: str) -> None:
    guard = PIIGuard()
    assert guard.scan(f"{keyword} {TAX_ID}") == ["tax_id"]
    assert guard.scan(f"{keyword}: {TAX_ID}") == ["tax_id"]
    assert guard.scan(f"{TAX_ID} {keyword}") == ["tax_id"]
    assert guard.scan(f"{TAX_ID} ({keyword})") == ["tax_id"]


@pytest.mark.parametrize(
    "text",
    [
        f"РНОКПП \u2116 {TAX_ID}",  # NFKC turns the numero sign into "No"
        f"РНОКПП \u2116{TAX_ID}",
        f"ІПН{TAX_ID}",
        f"?tax_id={TAX_ID}&x=1",
        f"ІПН {TAX_ID_SUM_10}",  # the sum modulo 11 is 10, so the control digit is 0
        f"customer_tax_id={TAX_ID}",
        f"USER_TAX_ID={TAX_ID}",
        f"https://a.example/?ipn={TAX_ID}",
        f"https://a.example/?rnokpp={TAX_ID}",
        f"https://a.example/?inn={TAX_ID}",
        f"tax no. {TAX_ID}",
        "\u0418\u041d\u041d 7740000076",  # a public Russian company number: an accepted cost
    ],
)
def test_tax_id_forms(text: str) -> None:
    assert PIIGuard().scan(text) == ["tax_id"]


@pytest.mark.parametrize(
    "text",
    [
        TAX_ID,  # a valid number, but no keyword
        f"call {TAX_ID} now",
        "0671234567",
        f"ІПН {WRONG_TAX_ID}",
        f"tax id: {WRONG_TAX_ID}",
        f"{WRONG_TAX_ID} РНОКПП",
        f"ІПН {TAX_ID}0",  # 11 digits
        "ІПН 123456789",  # 9 digits
        f"tin can {TAX_ID}",  # a word between them
        f"martin {TAX_ID}",
        f"{TAX_ID} tinder",
        f"tax {TAX_ID}",
        f"taxi no {TAX_ID}",
        f"innovation {TAX_ID}",
        f"{TAX_ID} innings",
        f"\u0406\u041f\u041d {TAX_ID[:4]} {TAX_ID[4:]}",  # split into groups: a limit
        f"IHH {TAX_ID}",  # Latin look-alikes: a limit
    ],
)
def test_tax_id_needs_keyword_and_control_digit(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(("name", "secret"), SECRETS.items(), ids=SECRETS.keys())
def test_secret_found(name: str, secret: str) -> None:
    assert PIIGuard().scan(secret) == ["secret"], name
    assert PIIGuard().scan(f"look up {secret} please") == ["secret"], name


@pytest.mark.parametrize("rounds", [1, 2, 3])
@pytest.mark.parametrize(("name", "secret"), SECRETS.items(), ids=SECRETS.keys())
def test_secret_found_in_url(name: str, secret: str, rounds: int) -> None:
    url = f"https://a.example/search?q={percent_encoded(secret, rounds)}"
    assert PIIGuard().scan_url(url) == ["secret"], name


@pytest.mark.parametrize(
    "text",
    [
        "https://jane:" + "s3cret" + "@corp.example/",  # an address as well
        "ftp://jane:" + "s3cret" + "@files.corp.example/report",
    ],
)
def test_password_in_url_with_a_dotted_host(text: str) -> None:
    assert PIIGuard().scan(text) == ["email", "secret"]


@pytest.mark.parametrize(
    "text",
    [
        "sk-learn-compose-pipeline-tutorial",
        "risk-management-framework-2024-edition",
        "task-1234567890-abcdefghij",
        "password: reset link not working",
        "how to reset password: step by step",
        "password: forgotten?",
        "password: (optional)",
        "token: authentication in fastapi",
        "token: authentication, authorization",
        "api_key=YOUR_API_KEY",
        "api_key=your-api-key-1",
        "password=my_password1",
        "password=<password1>",
        "password=$PASSWORD1",
        "password={password1}",
        "password=example123",
        "password=placeholder1",
        "password=changeme1",
        "password=xxx12345",
        "password=abc",  # too short
        "?token=abcdef&page=1",  # the ampersand ends the value
        "api_key=YOUR_KEY_1",
        "password=CHANGEME1",
        "password = letters-and_dashes",
        "page_token=abc123def",
        "next_token=abc123def",
        "pagination token 123456",
        "bearer token authentication in fastapi",
        "Bearer authentication-and-authorization-explained",
        "BSA-compliance-for-small-and-midsize-banks",
        "BSA_compliance_for_small_and_midsize_banks",
        "postgres://user:<password>@localhost/db",
        "postgres://user:${DB_PASSWORD}@localhost/db",
        "xoxb-your-bot-token invalid_auth slack bolt",
        "SLACK_BOT_TOKEN=xoxb-your-token not_authed",
        "benchmark_test_ReadWriteLatencyP99 flaky",
        "task_test_CeleryBeatScheduleUpdate",
        "framework_test_HandlerRegistration",
        "disk_live_MigrationConsistencyChecks",
        "AGPA" + "A1B2" * 4,  # an AWS principal id is no secret
        "MIIE" + "a1B2" * 14,  # a PEM body alone
        "foxapp-1-release-notes-2024",
        "MSG." + "a1B-" * 4 + "." + "c3D_" * 5,
        "ya29." + "a1B-",
        "gsk_" + "a1B2" * 9,
        "pypi-AgE" + "a1B-",
        "dckr_pat_" + "a1B-",
        "GOCSPX-" + "a1B-",
        "https://hooks.slack.com/services/",
        "https://discord.com/api/webhooks/123456/short",
        "https://www.reuters.com/technology/sk-hynix-q3-profit-hits-record-high-ai-chip-demand-2024-10-24/",
        "https://www.cnbc.com/2024/10/24/sk-hynix-q3-earnings-report-2024.html",
        "https://towardsdatascience.com/sk-learn-pipelines-explained-with-3-examples-1b2c3d4e5f60",
        "https://a.example/pl-sk-cz-hu-roaming-prices-2024",
        "http://localhost:8080/path@home",
        "https://a.example:8443/?x=1:2@host",
        "ghp_" + "a1B2" * 8,  # 32 characters, too short
        "AKIA" + "A1B2" * 3,  # too short
        "AKIA" + "A1B2" * 5,  # too long
        "AIza" + "a1B-_" * 6,  # too short
        "AIza" + "a1B-_" * 8,  # too long
        "123456789:" + "a1B-_" * 6,  # too short
        "123456789:" + "a1B-_" * 7 + "a",  # too long
        "12345678901:" + "a1B-_" * 7,  # 11 digits
        "eyJ" + "a1B2" * 3,  # a JWT has three parts
        "-" * 5 + "BEGIN PUBLIC KEY" + "-" * 5,
        "-" * 5 + "BEGIN CERTIFICATE" + "-" * 5,
        pem("RSA PRIVATE KEY"),
        pem("PRIVATE KEY") + "\n" + "a1B2",  # too little key material
        f"difference between {pem('RSA PRIVATE KEY')} and {pem('PRIVATE KEY')}",
        pem("PRIVATE KEY") + " is the header of a key in " + "PKCS8" + " format",
    ],
)
def test_secret_lookalikes_are_ignored(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "'password' => $password",
        "'password' => $request->password",
        "PGPASSWORD=$password psql",
        "token=$token",
        "ansible password: !vault | decrypt error",
    ],
)
def test_variables_are_not_secrets(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        'data["password"].encode("utf-8")',
        'std::env::var("DB_PASSWORD").unwrap()',
        'config.get("password").toString()',
        "protected $hidden = ['password', 'remember_token'];",
        "fields = ['username', 'password', 'first_name', 'last_name']",
        '"required": ["username", "password", "customer_id"]',
        "how to reset password Windows10",
        "password OAuth2 grant type",
        "store password Argon2 python",
        "пароль Windows10 скинути",
        "SELECT * FROM users WHERE username = @username AND password = @password",
        "User.new(email: email, password: @password)",
        "'password' => $hashedPassword,",
        "'password' => 'hashed',",
        "'password' => 'required|string|min:8|confirmed',",
        "-Password $env:DB_PASSWORD",
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY! });",
        "token: session?.accessToken,",
        "password = var.db2_password",
        "token = data.aws_eks_cluster_auth.eks1.token",
        'let api_key = env::var("API_KEY")?;',
        "const token = req.headers.authorization?.split(' ')[1];",
        "await client.GetAsync(url, cancellationToken: cancellationToken);",
        "def verify(token: HTTPAuthorizationCredentials = Depends(security)):",
        "https://api.twitter.com/2/users/12/tweets?pagination_token=b26v89c19zqg8o3fo7gesq314",
        "https://export.arxiv.org/oai2?verb=ListRecords&resumptionToken=6960524|1001",
        "https://graph.microsoft.com/v1.0/users/delta?$deltaToken=abc123def456",
        'sqlmap -u "http://testphp.vulnweb.com/listproducts.php?cat=1" --dbs',
        "nuclei -u https://example.com:8443 -t cves/",
        "docker build --secret id=npmrc,src=$HOME/.npmrc .",
        "openssl pkcs12 -in c.p12 -nodes -password env:PFX_PASS",
        "Authorization: AWS4-HMAC-SHA256 SignatureDoesNotMatch",
        "hash password: sha256 vs bcrypt",
        "PASSWORD_RESET_TIMEOUT=259200",
        '<input type="password" minLength={8} maxLength={64} />',
    ],
)
def test_code_and_configuration_are_not_secrets(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "git@github.com: Permission denied (publickey).",
        "https://cdn.example.com/assets/logo@2x.png",
        "systemctl status wg-quick@wg0.service",
        "this@MainActivity.startActivity(intent)",
        "A@np.linalg.inv(B)",
        "pbmc@meta.data$percent.mt",
        "https://formulae.brew.sh/api/formula/python@3.12.json",
    ],
)
def test_at_signs_that_are_no_address(text: str) -> None:
    assert PIIGuard().scan(text) == []


def test_git_inside_a_name_is_still_an_address() -> None:
    assert PIIGuard().scan("git.user@corp.example") == ["email"]


@pytest.mark.parametrize(
    "text",
    [
        "prime numbers 2 3 5 7 11 13 17 19 23 29 31 37 41 43",
        "date range 2024-01-01 2024-12-31",
        "http status codes 200 201 204 301 302 304 400 401 403 404 500 502 503",
        "2+3+5+7+11+13+17+19+23+29+31",
        "check imei 490154203237518 stolen",
        "SIRET 44306184100047 Google France",
        "https://web.archive.org/web/20241030143008/https://example.com/",
        "https://example.com/db/migrate/20241030143008_create_users.rb",
    ],
)
def test_lists_and_identifiers_are_not_cards(text: str) -> None:
    assert PIIGuard().scan(text) == []


def _with_check_digit(digits: str) -> str:
    return next(digits + str(digit) for digit in range(10) if _luhn_ok(digits + str(digit)))


@pytest.mark.parametrize(
    ("prefix", "length"),
    [
        ("4", 13),
        ("30", 14),
        ("36", 14),
        ("38", 14),
        ("50", 14),
        ("34", 15),
        ("37", 15),
        ("2131", 15),
        ("6", 15),
        ("2", 16),
        ("9804", 16),
        ("58", 17),
        ("6", 18),
        ("22", 19),
        ("35", 19),
        ("4", 19),
        ("9804", 19),
    ],
)
def test_card_lengths_with_their_prefixes(prefix: str, length: int) -> None:
    digits = _with_check_digit(prefix + "1" * (length - len(prefix) - 1))
    assert PIIGuard().scan(f"card {digits}") == ["payment_card"]


@pytest.mark.parametrize(
    ("prefix", "length"),
    [("2", 13), ("3", 13), ("4", 14), ("35", 15), ("4", 15), ("4", 17), ("2", 18), ("2", 19)],
)
def test_prefixes_that_do_not_fit_the_length_are_no_cards(prefix: str, length: int) -> None:
    digits = _with_check_digit(prefix + "1" * (length - len(prefix) - 1))
    assert PIIGuard().scan(f"card {digits}") == []


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1111 123",
        "1234 4111 1111 1111 1111",
        "3782 822463 10005",
        "3056 930902 5904",
        "4111 111111111111",
    ],
)
def test_card_layouts_are_found(text: str) -> None:
    assert PIIGuard().scan(text) == ["payment_card"]


@pytest.mark.parametrize(
    "text",
    [
        "my password is hunter22",
        "пароль це qwerty123",
        "DB_PASSWORD_PROD=Hunter22x9",
        "PASSWORD1=Hunter22x9",
        "SECRET_KEY_BASE=abc123def456",
        "API_KEY_PROD=abc123def456",
        "TOKEN_PROD=abc123def456",
        "API key: abc123def456",
        "secret key: abc123def456",
        "api.key=abc123def456",
        "DB_PASS=Hunter22x9",
        "SMTP_PASS=Hunter22x9",
        "pass: Hunter22x9",
        '"password": "aB3(dE5!xY"',
        'password="xK9[mP2#qR"',
        "next_token=abc;access_token=abc123def456",
        "curl -su admin:Hunter22x9 https://a.example/",
        "curl -vu admin:Hunter22x9 https://a.example/",
        '{\\"Authorization\\": \\"Bearer abc123def456\\"}',
        "define('DB_PASSWORD', 'hunter22')",
    ],
)
def test_secrets_in_ordinary_formats_are_found(text: str) -> None:
    assert PIIGuard().scan(text) == ["secret"]


@pytest.mark.parametrize(
    "text",
    [
        "ИПН 1234567899",
        "РНУКПН 1234567899",
        "tax ID number 1234567899",
        "taxpayer identification number 1234567899",
        "TIN number: 1234567899",
        "ідентифікаційний номер платника податків 1234567899",
        "ИНН налогоплательщика 1234567899",
        "INN 1234567899",
        "https://a.example/?inn=1234567899",
        "tax identification number ::: No.. 1234567899",
        "ІПН ::: 1234567899",
        "1234567899 --- ІПН",
    ],
)
def test_tax_keywords_and_their_distance(text: str) -> None:
    assert PIIGuard().scan(text) == ["tax_id"]


def test_inn_as_a_hotel_is_no_tax_keyword() -> None:
    assert PIIGuard().scan("Holiday Inn 1234567899") == []


@pytest.mark.parametrize("dash", ["\U0000058a", "\U00002e3a", "\U00002212", "\U00002011"])
def test_every_unicode_dash_counts(dash: str) -> None:
    assert PIIGuard().scan(dash.join(["4111", "1111", "1111", "1111"])) == ["payment_card"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "postgres://admin:Hunter22x9Hunter22x9@db/app", ["secret"], id="long-password"
        ),
        pytest.param("jane@" + "a" * 50 + ".example", ["email"], id="long-domain"),
        pytest.param("a1234567890 @ corp.example", ["email"], id="long-token-before-a-spaced-sign"),
        pytest.param("jane @ corp.example_X", ["email"], id="underscore-after-the-domain"),
        pytest.param("api_key=aBcDeFgHiJkLmN", [], id="short-mixed-case"),
        pytest.param("api_key=aBcDeFgHiJkLmNoPqRsT", ["secret"], id="long-mixed-case"),
        pytest.param("sk-internationalization-guide", [], id="sk-long-word"),
        pytest.param("411.1111.1111.1116", [], id="three-digits-before-a-dot"),
        pytest.param("DE89-37040-04405-32013-000", [], id="dashed-groups-of-five"),
        pytest.param("MT48A12A12A12A12A12A12A12A12A12A12A12A12", [], id="iban-too-long"),
        pytest.param("PY08 1234 5678 90", [], id="iban-too-short"),
    ],
)
def test_limits_of_the_rules(text: str, expected: list[str]) -> None:
    assert PIIGuard().scan(text) == expected


def test_runs_of_marks_are_capped_before_nfkc(monkeypatch: pytest.MonkeyPatch) -> None:
    # The sort is quadratic only on interpreters CI does not run, so timing cannot show this.
    seen: list[str] = []
    normalize = unicodedata.normalize

    def spy(form: str, text: str) -> str:
        seen.append(text)
        return normalize(form, text)  # type: ignore[arg-type]

    monkeypatch.setattr("seekrelay.pii.unicodedata.normalize", spy)
    PIIGuard().scan("a" + "\U00000301" * 1000)
    assert seen
    assert max(map(len, seen)) == 31


@pytest.mark.parametrize(
    "text",
    ["password=$uperS3cret", "password=Hunter.x9", "token=abc123.def456", "password: $3cret-Value"],
)
def test_values_that_only_resemble_variables_are_secrets(text: str) -> None:
    assert PIIGuard().scan(text) == ["secret"]


@pytest.mark.parametrize(
    "text",
    [
        "-" * 5 + "BEGIN  RSA  PRIVATE  KEY" + "-" * 5 + "\n" + KEY_MATERIAL,
        "-" * 5 + "BEGIN RSA PRIVATE\nKEY" + "-" * 5 + "\n" + KEY_MATERIAL,
        '{"private_key": "' + pem("PRIVATE KEY") + "\\n" + KEY_MATERIAL + '"}',
        quote(SECRETS["pem-bare"]),
        b64(SECRETS["pem-bare"]),
        f"https://a.example/?k={quote(SECRETS['pem-bare'])}",
    ],
)
def test_private_key_in_context(text: str) -> None:
    assert PIIGuard().scan(text) == ["secret"]


@pytest.mark.parametrize(
    "text",
    [
        "PGPASSWORD=Hunter22x9",
        "export DB_PASSWORD=Hunter22x9",
        "dbpassword=Hunter22x9",
        "dbPassword=Hunter22x9",
        "db_passwd=Hunter22x9",
        "my_passphrase=Hunter22x9",
        "user_pwd=Hunter22x9",
        "x-pwd=Hunter22x9",
        "https://a.example/?db_password=Hunter22x9",
        # A quote opens the value.
        'password="correct horse"',
        "password='correct horse'",
        '"password": "Hunter22x9"',
        '{\\"password\\": \\"Hunter22x9\\"}',
        "ALTER USER app WITH PASSWORD 'Hunter22x9';",
        "define('DB_PASSWORD', 'Hunter22x9');",
        'password := "Hunter22x9"',
        'if password == "Hunter22x9":',
        "password => 'Hunter22x9'",
        'password -> "Hunter22x9"',
        'password: "abcdef"',
        'the password "sunshine"',
        # Tight, as in .env files and URLs.
        "password=sunshine",
        "EMAIL_HOST_PASSWORD=abcdefghijklmnop",
        "password=correct-horse-battery-staple",
        "password=Hunter.22",
        "password=MyP@ssw0rd!",
        "password=Mykola2005",
        "password=$uperS3cret",
        "password=hunter22xxx",
        "password=new_password1",
        "--password=Hunter22x9",
        "old_password_or_new_password=Hunter22x9",  # the first word does not swallow the second
        # A colon, ">" or a spaced "=" needs a symbol or digit.
        "password: S3cret",
        "password: hunter22!",
        "**Password:** Hunter22x9",
        "**Password**: Hunter22x9",
        "<password>Hunter22x9</password>",
        "password := Hunter22",
        "password => Hunter22",
        "password -> Hunter22",
        "password = hunter22",
        "Password> hunter22",
        # Prose needs a letter, a digit and both cases.
        "the password is Hunter22x9",
        "machine api.example.com login me password Hunter22x9",
        "\u043f\u0430\u0440\u043e\u043b\u044c \u0446\u0435 Hunter22x9",
        "password Hunter22x9",
        "password - Hunter22x9",
        "password, Hunter22x9",
        "**Password** Hunter22x9",
    ],
)
def test_password_assignment(text: str) -> None:
    assert PIIGuard().scan(text) == ["secret"]


@pytest.mark.parametrize(
    "text",
    [
        "password=new_password",
        "password=letters_and_underscores",
        "password==confirm_password flask form",
        "password == hunter22x9",
        "password == Hunter22x9",  # no quote: a comparison
        "password: reset link not working",
        "password: String!",
        "password: SecretStr",
        "password = letters-and_dashes",
        "password sha256",
        "password argon2id",
        "password reset",
        "the password is required",
        "the password is wrong",
        "password hashing with bcrypt12",
        "password Manager review",
        "passwordless login 2024",
        "password-protected zip file 2024",
        "Password-Reset-Guide-2024.html",
        "PASSWORD_MIN_LENGTH=8",
        "password123456",
        'flask "password" argument missing',
        '<input type="password" placeholder="Password">',
        '<input type="password" value="" />',
        "mongodb+srv://<username>:<password>@cluster0.mongodb.net connection timeout",
        "OLDPWD=/home/user/project",
        "cd $PWD && make",
        "password: command not found",
        "pwd: command not found",
        "\u043f\u0430\u0440\u043e\u043b\u044c: \u0437\u0430\u0431\u0443\u043b\u0438",
    ],
)
def test_password_words_that_hold_no_password(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "AWS_SECRET_ACCESS_KEY=" + "a1B2c3D4" * 5,
        "aws_secret_access_key = " + "a1B2c3D4" * 5,
        "TAVILY_API_KEY=abc123def456",
        "OPENAI_API_KEY=abc123def456",
        "API_KEY=abc123def456",
        "DJANGO_SECRET_KEY=abc123def456",
        "SECRET_KEY=abc123def456",
        "JWT_SECRET=abc123def456",
        "GITHUB_TOKEN=abc123def456",
        "authToken: abc123def456",
        "myAuthToken: abc123def456",
        "clientSecret: abc123def456",
        "client-secret=abc123def456",
        "X-API-Key: abc123def456",
        "x-api-secret: abc123def456",
        "api_token=abc123def456",
        "apiToken=abc123def456",
        "access_key=abc123def456",
        "accessKey=abc123def456",
        "refresh_token=abc123def456",
        "refreshToken=abc123def456",
        "session_token=abc123def456",
        "sessionToken=abc123def456",
        "session-token=abc123def456",
        "X-Amz-Security-Token=abc123def456",
        "X-Amz-Security-Token: abc123def456",
        "adminToken=abc123def456",  # ends in "min", but that is a different word
        '"token": "abc123def456"',
        '{\\"token\\": \\"abc123def456\\"}',
        "token := abc123def456",
        "token => abc123def456",
        "token == abc123def456",
        "api_key=abc123xxxdef",
        "api_key=aBcDeFgHiJkLmNoPqRsTuVwXyZ",  # letters only, but long and in both cases
        "token=abc-def_ghi.jkl1",
    ],
)
def test_token_assignment(text: str) -> None:
    assert PIIGuard().scan(text) == ["secret"]


@pytest.mark.parametrize(
    "name",
    [
        "continuation-token",
        "continuation_token",
        "next-token",
        "next_token",
        "nextToken",
        "NEXT_TOKEN",
        "page-token",
        "page_token",
        "pageToken",
        "nextPageToken",
        "next_page_token",
        "prev-token",
        "prevToken",
        "previous_token",
        "previousPageToken",
        "cursor_token",
        "cursorToken",
        "sync-token",
        "syncToken",
        "resume_token",
        "resumeToken",
        "start-token",
        "startToken",
        "after_token",
        "afterToken",
        "max_token",
        "maxToken",
        "min-token",
        "minToken",
    ],
)
def test_pagination_tokens_are_no_secrets(name: str) -> None:
    assert PIIGuard().scan(f"{name}=abc123def456") == []
    assert PIIGuard().scan(f"https://a.example/?{name}=abc123def456") == []


@pytest.mark.parametrize(
    "text",
    [
        "gpt-4o context window token: 128000",
        "token: 1,000,000 price gemini",
        "price per token: 0.0001 usd",
        "token: authentication in fastapi",
        "api_key=abcdefgh",
        "token=abc-def_ghi.jkl",
        "token: aaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # long, but in one case
        "max_tokens=4096",
        "token_limit=128000",
        "https://a.example/o/oauth2/v2/auth?state=security_token%3D138r5719ru3e1%26url%3Dhttps",
    ],
)
def test_token_words_that_hold_no_token(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "--password hunter22x9",
        "--password=hunter22x9",
        "--passwd hunter22x9",
        "--passphrase hunter22x9",
        "-password hunter22x9",
        '--password "hunter22x9"',
        "--token abc123def",
        "--api-key abc123def",
        "--apikey abc123def",
        "--secret abc123def",
        "--db-password hunter22x9",
        "--github-token=abc123def",
    ],
)
def test_secret_flag(text: str) -> None:
    assert PIIGuard().scan(text) == ["secret"]


@pytest.mark.parametrize(
    "text",
    [
        "--token-limit 4096",
        "--password-stdin",
        "--password-file /run/secrets/db",
        "--token 128000",
        "--token $TOKEN",
        "--password abcde",
        "--next-token abc123def",
        "--page-token abc123def",
        "x--token abc123def",
    ],
)
def test_flags_that_hold_no_secret(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "docker run -u 1000:1000 nginx",
        "docker run -u www-data:www-data nginx",
        "docker run -u $(id -u):$(id -g) nginx",
        "curl -u user:pass https://a.example/",
        "curl -u admin:abcdefgh https://a.example/",
        "curl -u admin:$PASSWORD https://a.example/",
        "curl -u admin:$PASSWORD1 https://a.example/",
        "curl -u user:changeme1 https://a.example/",
        "curl -u admin:12345678 https://a.example/",
        "x-u admin:Hunter22x9",
        "authorization header 401",
        "Authorization: Bearer YOUR_TOKEN",
        "Authorization: Bearer your-api-key-1",
        "Authorization: Bearer <token>",
        "Authorization: Bearer $TOKEN",
        "Authorization: Basic",
        "Authorization: abcdefgh",  # no digit
        "authorization: required",
        "Bearer: short1",
    ],
)
def test_commands_and_headers_that_hold_no_secret(text: str) -> None:
    assert PIIGuard().scan(text) == []


@pytest.mark.parametrize(
    "value",
    [
        *("password", "passwd", "pass", "pwd", "secret", "token", "root", "admin", "guest"),
        *("test", "postgres", "user", "username", "string", "changeit", "changeme", "none"),
        *("null", "nil", "true", "false", "undefined", "required", "optional", "empty"),
        *("default", "hidden", "redacted", "PASSWORD", "Password", "None", "True"),
        *("your-password", "your_password1", "YOUR_API_KEY", "my-secret", "my_key", "MY_TOKEN"),
        *("yourpassword", "yourSecret1", "YOURTOKEN", "mykey123", "myapikey", "mypass1"),
        *("<password>", "<>", "[password]", "[YOUR-PASSWORD]", "{password}", "{{ password }}"),
        *("${DB_PASSWORD}", "${password}", "$DB_PASSWORD", "$PASSWORD1", "$_POST"),
        *("example", "example123", "EXAMPLE", "placeholder", "placeholder1", "changeme1"),
        *("xxx", "xxxxxxxx", "XXXX", "xxx12345", "***", "*******", "...", "#####", "x.x.x"),
        *("\u2022\u2022\u2022", "**x**", "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"),
    ],
)
def test_defaults_and_placeholders_are_no_real_values(value: str) -> None:
    assert not _is_real_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "Mykola2005",
        "MyP@ssw0rd!",
        "myS3cret99",
        "hunter22xxx",
        "abc123xxxdef",
        "$uperS3cret",
        "passwords",
        "password1",
        "admin123",
        "tokens",
        "exampled",
        "my",
        "<password",
        "[password",
        "{password",
    ],
)
def test_values_that_only_contain_a_placeholder_are_real(value: str) -> None:
    assert _is_real_value(value)


@pytest.mark.parametrize(
    "text",
    [
        'client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))',
        'openai.api_key = os.getenv("OPENAI_API_KEY")',
        'api_key = os.environ["OPENAI_API_KEY"] KeyError',
        'password = os.getenv("DB_PASSWORD") returns None docker',
        "const token = localStorage.getItem('token') returns null after refresh",
        "const password = req.body.password undefined express",
        "token = request.headers.get('Authorization') flask None",
        "api_key=settings.OPENAI_API_KEY pydantic BaseSettings",
        "python getpass password = getpass.getpass() pycharm echo",
        "secret = secrets.token_hex(32) flask secret key",
        "tokenizer padding token = tokenizer.eos_token warning huggingface",
        "const apiKey = process.env.API_KEY undefined nextjs client component",
        "password: z.string().min(8) zod custom error message",
        "token: Optional[str] = None",
        "api_key: Optional[str] = None",
        "token: dict[str, Any]",
        "token = jwt.encode(payload, key)",
        "password=request.form",
        "password=args.password",
        "password = bcrypt.hashpw(pw, salt)",
        "token=self.token",
        "secret=config.secret",
        "SyntaxError: unexpected token: import.meta",
        "PWD=/usr/local/bin",
        "PWD=./bin",
        "PWD=../bin",
        "PWD=~/bin",
        "pwd = os.getcwd()",
        "pwd: /home/maksim",
        "postgres://user:password@localhost:5432/mydb connection refused docker compose",
        "redis://:password@localhost:6379/0 NOAUTH authentication required",
        "mysql://root:root@127.0.0.1:3306/test sqlalchemy operational error",
        "amqp://guest:guest@localhost:5672 celery broker connection refused",
        "DATABASE_URL=postgresql://postgres:postgres@db:5432/app prisma migrate docker",
        "mongodb://admin:admin@mongo:27017/?authSource=admin authentication failed",
        "postgres://USER:PASSWORD@HOST:PORT/DATABASE heroku format",
        "postgres://username:password@hostname:5432/dbname",
        "postgresql://postgres:[YOUR-PASSWORD]@db.abcdefghijklmnop.supabase.co:5432/postgres",
        "HTTP_PROXY=http://user:pass@proxy:3128",
    ],
)
def test_code_and_templates_hold_no_secret(text: str) -> None:
    assert PIIGuard().scan(text) == []
    assert PIIGuard().scan_url(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "how to reset my password",
        "what is the difference between a token and a coin",
        "best laptop under 1000 usd in 2025",
        "how to fix error 0x80070057 on windows 11",
        "what time is it in kyiv right now",
        "як скинути пароль на роутері",
        "податковий кодекс україни 2024",
        "що таке ідентифікаційний код",
        "курс долара на сьогодні 41.5",
        "рецепт борщу на 5 літрів",
        "как сбросить пароль на роутере",
        "что такое инн и зачем он нужен",
        "идентификационный номер налогоплательщика в 2024 году",
        "собака породы такса купить",
        "погода в киеве на 10 дней",
        "https://en.wikipedia.org/wiki/Internationalization_and_localization",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
    ],
)
def test_ordinary_queries_stay_clean(text: str) -> None:
    assert PIIGuard().scan(text) == []
    assert PIIGuard().scan_url(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("jane\u200b@corp\u200c.exa\u200dmple", ["email"]),
        ("ja\u2060ne@corp.example", ["email"]),
        ("jane\u00ad@corp.example", ["email"]),
        ("\U000e0041jane@corp.example", ["email"]),
        ("4111\u200b 1111\ufeff 11\u200b11 1111", ["payment_card"]),
        ("4\u200b111111111111111", ["payment_card"]),
        ("DE89 37\u200b04 0044 0532 0130 00", ["iban"]),
        (f"ІПН {TAX_ID[:4]}\u200b{TAX_ID[4:]}", ["tax_id"]),
        ("gh\u200bp_" + "a1B2" * 9, ["secret"]),
        ("sk-" + "abcd1" * 2 + "\u200b" + "abcd1" * 2, ["secret"]),
        ("pass\u200bword=hunter22", ["secret"]),
        ("4111\ufdfa 1111 1111 1111", ["payment_card"]),
        ("4111\ufdfb 1111 1111 1111", ["payment_card"]),
    ],
)
def test_invisible_characters_do_not_hide_data(text: str, expected: list[str]) -> None:
    assert PIIGuard().scan(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\u0301".join("4111111111111111"), ["payment_card"]),
        ("".join(digit + "\u20e3" for digit in "4111111111111111"), ["payment_card"]),
        ("".join(digit + "\u0323" for digit in "4111 1111 1111 1111"), ["payment_card"]),
        ("D\u0301E89 3704 0044 0532 0130 00", ["iban"]),
        (f"\u0406\u041f\u041d {TAX_ID[:4]}\u0301{TAX_ID[4:]}", ["tax_id"]),
        ("gh\u0301p_" + "a1B2" * 9, ["secret"]),
        ("pas\u20e3sword=hunter22", ["secret"]),
        (with_digits(CARD, 0x0966).replace(" ", "\u093e "), ["payment_card"]),
    ],
)
def test_combining_marks_do_not_hide_data(text: str, expected: list[str]) -> None:
    assert PIIGuard().scan(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "\u0930\u093e\u091c@\u0909\u0926\u093e\u0939\u0930\u0923.\u092d\u093e\u0930\u0924",
        "\u0938\u0940\u0924\u093e@corp.example",
        "raj@\u0921\u093e\u091f\u093e\u092e\u0947\u0932.\u092d\u093e\u0930\u0924",
        "\u0e2a\u0e21\u0e0a\u0e32\u0e22@\u0e15\u0e31\u0e27\u0e2d\u0e22\u0e48\u0e32\u0e07.\u0e44\u0e17\u0e22",
        "somchai@\u0e15\u0e31\u0e27\u0e2d\u0e22\u0e48\u0e32\u0e07.\u0e44\u0e17\u0e22",
        "\u0b95\u0bc1\u0bae\u0bbe\u0bb0\u0bcd@\u0b89\u0ba4\u0bbe\u0bb0\u0ba3\u0bae\u0bcd.\u0b87\u0ba8\u0bcd\u0ba4\u0bbf\u0baf\u0bbe",
        "\u09b0\u09be\u099c@\u0989\u09a6\u09be\u09b9\u09b0\u09a3.\u09ad\u09be\u09b0\u09a4",
    ],
)
def test_email_in_scripts_with_combining_vowels(text: str) -> None:
    assert PIIGuard().scan(text) == ["email"]


def test_marks_go_after_nfkc_so_that_composed_letters_stay() -> None:
    assert _normalise("\u0438\u0306") == "\u0439"
    keyword = unicodedata.normalize(
        "NFD", "\u043f\u043e\u0434\u0430\u0442\u043a\u043e\u0432\u0438\u0439 \u043a\u043e\u0434"
    )
    assert PIIGuard().scan(f"{keyword} {TAX_ID}") == ["tax_id"]


@pytest.mark.parametrize("dot", ["\u3002", "\uff61", "\ufe12"])
def test_ideographic_full_stop_is_a_dot(dot: str) -> None:
    assert PIIGuard().scan(f"jane@163{dot}com") == ["email"]


def test_mark_runs_are_capped_at_thirty() -> None:
    assert _cap_mark_runs("a" + "\u0301" * 30 + "b") == "a" + "\u0301" * 30 + "b"
    assert _cap_mark_runs("a" + "\u0301" * 31 + "b") == "a" + "\u0301" * 30 + "b"
    assert _cap_mark_runs(("a" + "\u0301" * 40) * 2) == ("a" + "\u0301" * 30) * 2


@pytest.mark.parametrize("mark", ["\u0f73", "\u0f75", "\u0f81", "\uff9e", "\uff9f"])
def test_characters_that_decompose_into_marks_count_as_marks(mark: str) -> None:
    assert unicodedata.combining(mark) == 0
    assert _cap_mark_runs("a" + mark * 40) == "a" + mark * 30
    assert _cap_mark_runs("a" + ("\u0301" + mark) * 40) == "a" + ("\u0301" + mark) * 15


def test_every_character_that_starts_with_a_mark_after_decomposition_is_counted() -> None:
    # NFKC decomposes with the compatibility mappings, so NFD would miss U+FF9E and U+FF9F.
    hidden = set()
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        first = unicodedata.normalize("NFKD", char)[0]
        if unicodedata.combining(first) and not unicodedata.combining(char):
            hidden.add(char)
    assert hidden == _HIDDEN_MARKS


def test_data_after_a_long_run_of_marks_is_still_found() -> None:
    assert PIIGuard().scan("4" + "\u0301" * 100 + "111 1111 1111 1111") == ["payment_card"]
    assert PIIGuard().scan("\u0f73" * 100 + EMAIL) == ["email"]


@pytest.mark.parametrize(
    "zero",
    [
        pytest.param(0xFF10, id="fullwidth"),
        pytest.param(0x0660, id="arabic-indic"),
        pytest.param(0x06F0, id="extended-arabic-indic"),
        pytest.param(0x0966, id="devanagari"),
        pytest.param(0x1D7CE, id="mathematical-bold"),
    ],
)
def test_unicode_digits(zero: int) -> None:
    guard = PIIGuard()
    assert guard.scan(with_digits(CARD, zero)) == ["payment_card"]
    assert guard.scan(with_digits(PROSTIR_CARD, zero)) == ["payment_card"]
    assert guard.scan(with_digits("DE89 3704 0044 0532 0130 00", zero)) == ["iban"]
    assert guard.scan("ІПН " + with_digits(TAX_ID, zero)) == ["tax_id"]
    assert guard.scan("пароль: " + with_digits("123456", zero)) == ["secret"]


def test_phone_with_unicode_dashes_and_digits() -> None:
    guard = PIIGuard(detect_phones=True)
    assert guard.scan("call (044) 123\u201345\u201367 now") == ["phone"]
    assert guard.scan(with_digits("call 044 123 45 67 now", 0x0660)) == ["phone"]


def test_can_disable_tax_ids_and_secrets() -> None:
    assert PIIGuard(detect_tax_ids=False).scan(f"ІПН {TAX_ID}") == []
    assert PIIGuard(detect_secrets=False).scan("password=hunter22") == []
    assert PIIGuard(detect_secrets=False).scan(SECRETS["github-ghp"]) == []
    assert PIIGuard(detect_secrets=False).scan_url("https://a.example/?token=abc123def") == []


def test_disabled_kinds_stay_off_in_decoded_data() -> None:
    assert PIIGuard(detect_emails=False).scan(b64(EMAIL)) == []
    assert PIIGuard(detect_secrets=False).scan(b64("password=hunter22")) == []
    assert PIIGuard(detect_tax_ids=False).scan_url(f"https://a.example/?t=ІПН%20{TAX_ID}") == []


@pytest.mark.parametrize("rounds", [1, 2, 3])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (EMAIL, ["email"]),
        ("jane @ corp.example", ["email"]),
        ("jane[at]corp.example", ["email"]),
        ("jane (собака) corp (точка) example", ["email"]),
        ("иван@пример.испытание", ["email"]),
        (CARD, ["payment_card"]),
        ("4111.1111.1111.1111", ["payment_card"]),
        ("4111/1111/1111/1111", ["payment_card"]),
        (PROSTIR_CARD, ["payment_card"]),
        (IBAN, ["iban"]),
        (f"ІПН {TAX_ID}", ["tax_id"]),
        (f"tax id: {TAX_ID}", ["tax_id"]),
        (SECRETS["pem-rsa"], ["secret"]),
        (SECRETS["bearer"], ["secret"]),
        ("password=hunter22", ["secret"]),
        ("пароль: 123456", ["secret"]),
    ],
)
def test_url_percent_encoded_up_to_three_times(
    value: str, expected: list[str], rounds: int
) -> None:
    url = f"https://a.example/search?q={percent_encoded(value, rounds)}"
    assert PIIGuard().scan_url(url) == expected


def test_url_scan_decodes_nested_percent_signs() -> None:
    guard = PIIGuard()
    assert guard.scan_url("https://a.example/?e=jane%2540corp.example") == ["email"]
    assert guard.scan_url("https://a.example/?e=jane%252540corp.example") == ["email"]


def test_url_scan_stops_after_three_rounds() -> None:
    assert PIIGuard().scan_url("https://a.example/?e=jane%25252540corp.example") == []


@pytest.mark.parametrize(
    ("value", "symbol", "expected"),
    [
        pytest.param("password=ab?cd123", "/", ["secret"], id="slash-inside-the-value"),
        pytest.param("password=ab~cd123", "+", ["secret"], id="plus-inside-the-value"),
        pytest.param("ab>jane@corp.example", "+", ["email"], id="plus-before-the-address"),
    ],
)
def test_base64_with_standard_symbols(value: str, symbol: str, expected: list[str]) -> None:
    token = b64(value)
    assert symbol in token
    guard = PIIGuard()
    assert guard.scan(f"decode {token} please") == expected
    assert guard.scan_url(f"https://a.example/?d={token}") == expected


def test_base64_is_decoded_after_percent_decoding() -> None:
    # The escaped slash splits the token, so only the decoded URL shows it whole.
    token = quote(b64("ab?jane@corp.example"), safe="")
    assert "%2F" in token
    guard = PIIGuard()
    assert guard.scan(f"https://a.example/?d={token}") == ["email"]
    assert guard.scan_url(f"https://a.example/?d={token}") == ["email"]


@pytest.mark.parametrize(
    "encode",
    [pytest.param(b64, id="base64"), pytest.param(b64url, id="base64url")],
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(EMAIL, ["email"], id="email"),
        pytest.param(CARD, ["payment_card"], id="card"),
        pytest.param(SECRETS["github-ghp"], ["secret"], id="token"),
        pytest.param("password=hunter22", ["secret"], id="assignment"),
        pytest.param(f"ІПН {TAX_ID}", ["tax_id"], id="tax-id"),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        pytest.param("https://a.example/?data={}&x=1", id="query-value"),
        pytest.param("https://a.example/v1/{}/details", id="path-segment"),
        pytest.param("https://a.example/{}", id="last-segment"),
        pytest.param("decode {} please", id="plain-text"),
    ],
)
def test_base64_is_decoded_by_both_scans(
    encode: Callable[[str], str], value: str, expected: list[str], template: str
) -> None:
    text = template.format(encode(value))
    assert PIIGuard().scan(text) == expected
    assert PIIGuard().scan_url(text) == expected


def test_wrapped_base64_is_read_across_line_ends() -> None:
    token = SECRETS["github-ghp"]
    for offset in range(57):  # base64 tools put 57 bytes on a line
        wrapped = base64.encodebytes(f"{'x' * offset} {token} and some more text".encode())
        assert PIIGuard().scan(wrapped.decode()) == ["secret"], offset


@pytest.mark.parametrize(
    "text", ["amFuZUBj\nb3JwLmV4YW1wbGU=", "amFuZUBj b3JwLmV4YW1wbGU=", "amFuZUBjb3Jw.LmV4YW1wbGU="]
)
def test_base64_of_one_value_in_two_runs(text: str) -> None:
    assert PIIGuard().scan(text) == ["email"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jane%40corp.example", ["email"]),
        ("https://x.example/?e=jane%40corp.example", ["email"]),
        ("https://x.example/?c=4111%201111%201111%201111", ["payment_card"]),
        ("100%", []),
    ],
)
def test_percent_encoding_inside_base64(value: str, expected: list[str]) -> None:
    assert PIIGuard().scan(b64(value)) == expected
    assert PIIGuard().scan_url(f"https://a.example/?d={b64(value)}") == expected


def test_decoded_base64_stays_a_limit_in_these_forms() -> None:
    token = b64(EMAIL).rstrip("=")
    assert PIIGuard().scan("x" + token) == []  # misaligned
    assert PIIGuard().scan(base64.b64encode(EMAIL.encode("utf-16")).decode()) == []  # a BOM


def test_base64_without_padding() -> None:
    assert PIIGuard().scan(b64(EMAIL).rstrip("=")) == ["email"]


def test_base64_shortest_token() -> None:
    token = b64("a@b.cc")
    assert len(token) == 8
    assert PIIGuard().scan(token) == ["email"]


def test_base64_run_with_one_character_too_many() -> None:
    token = b64("jane@corp.exampl")  # 16 bytes, so 22 characters and two padding signs
    assert PIIGuard().scan(token.rstrip("=") + "AAA") == ["email"]


def test_base64_two_levels_deep() -> None:
    assert PIIGuard().scan(b64(b64(EMAIL))) == ["email"]
    assert PIIGuard().scan_url(f"https://a.example/{b64url(b64url(EMAIL))}/x") == ["email"]


def test_base64_three_levels_deep_is_beyond_the_limit() -> None:
    assert PIIGuard().scan(b64(b64(b64(EMAIL)))) == []


def test_every_base64_token_counts() -> None:
    padding = " ".join(b64(f"harmless text {number}") for number in range(500))
    assert PIIGuard().scan(f"{padding} {b64(EMAIL)}") == ["email"]
    assert PIIGuard().scan(f"{b64(EMAIL)} {padding}") == ["email"]
    assert PIIGuard().scan(f"{padding} {b64(EMAIL)} {padding}") == ["email"]


def test_base64_must_decode_to_valid_utf8() -> None:
    token = base64.b64encode(b"\xff" + EMAIL.encode()).decode()
    assert PIIGuard().scan(token) == []


@pytest.mark.parametrize(
    "text",
    [
        "internationalization",
        "counterrevolutionaries",
        "characteristically internationalization",
        "https://a.example/internationalization/characteristically",
        "AAAAAAAAAAAAAAAA",
        "abcdefgh",
        b64("just some harmless text"),
    ],
)
def test_ordinary_words_are_not_decoded_into_findings(text: str) -> None:
    assert PIIGuard().scan(text) == []


def test_other_encodings_are_out_of_scope() -> None:
    assert PIIGuard().scan(EMAIL.encode().hex()) == []


@pytest.mark.parametrize(
    ("check", "text", "kind"),
    [
        pytest.param("check", b64(EMAIL), "email", id="base64-query"),
        pytest.param("check", b64(SECRETS["github-ghp"]), "secret", id="base64-secret"),
        pytest.param("check_url", f"https://a.example/{b64url(CARD)}", "payment_card", id="url"),
        pytest.param("check_url", f"https://a.example/?e={quote(EMAIL)}", "email", id="percent"),
    ],
)
def test_decoded_finding_is_a_kind_and_never_the_value(check: str, text: str, kind: str) -> None:
    with pytest.raises(QueryRejectedError) as info:
        getattr(PIIGuard(), check)(text)
    assert info.value.findings == (kind,)
    message = str(info.value)
    assert kind in message
    for value in (EMAIL, "jane", CARD, "4111", SECRETS["github-ghp"], "ghp_", text):
        assert value not in message


def test_rejection_message_covers_secrets() -> None:
    with pytest.raises(QueryRejectedError, match="personal data or secrets") as info:
        PIIGuard().check("password=hunter22")
    assert info.value.findings == ("secret",)
    assert "secret" in str(info.value)
    assert "hunter22" not in str(info.value)


def test_several_kinds_are_reported_sorted() -> None:
    text = f"{EMAIL} {CARD} DE89370400440532013000 ІПН {TAX_ID} password=hunter22"
    assert PIIGuard().scan(text) == ["email", "iban", "payment_card", "secret", "tax_id"]


HOSTILE_INPUTS = {
    "password-suffix": "password_prod " * 5000,
    "api-key-words": "api key: " * 5000,
    "camel-value": "token=" + "aBcd" * 10000 + "!",
    "file-after-the-sign": "a@b.png " * 5000,
    "combined-flags": "-su a:" * 8000,
    "mixed-group-sizes": "1 22 333 4444 " * 3000,
    "percent": "%" * 80000,
    "single-digit-groups": "1 " * 40000,
    "card-start-groups": "4 1 " * 20000,
    "a-at": "a@" * 40000,
    "at": "@" * 80000,
    "spaces": " " * 80000,
    "bracket-at": "[at]" * 20000,
    "local-part": "a." * 40000 + "@",
    "letters": "A" * 80000,
    "digits": "1" * 80000,
    "assignments": "password=" * 10000,
    "pem-headers": "-----BEGIN " * 8000,
    "base64-text": "QUJD" * 20000,
    "nested-percent": "%25" * 2600 + "40",
    "jwt-prefixes": "eyJ" * 26000,
    "bearer": "bearer " * 11000,
    # Not in any report, but each one aims at one more detector.
    "dashed-jwt-prefixes": "-eyJ" * 20000,
    "dotted-jwt-parts": ".eyJaaaaaaa" * 8000,
    "sk-prefixes": "sk-" * 26000,
    "url-schemes": "://a:b" * 13000,
    "bracket-dot": " (dot) " * 11000,
    "iban-groups": "AB12 " * 16000,
    "iban-dashes": "AB12-" * 16000,
    "tax-numbers": f"{TAX_ID} " * 7000,
    "phone-groups": "(1) " * 20000,
    "separators": "1" + "-" * 80000,
    "arabic-ligature": "\ufdfa" * 40000,
    "combining-marks": "a" + "\u0301\u0323" * 40000,
    "arabic-digits": "\u0664 \u0661 " * 20000,
    "passwords": "password " * 9000,
    "password-gaps": "password is " * 7000,
    "password-values": "password=" + "x" * 80000,
    "password-flags": "--password " * 7000,
    "long-flag-names": "--" + "a-" * 40000,
    "authorization-headers": "authorization: " * 5000,
    "curl-users": "-u a:" * 16000,
    "curl-users-tight": "-ua:" * 20000,
    "iban-short-groups": "AB 12 " * 13000,
    "iban-country-codes": "AB-" * 26000,
    "iban-in-one-run": "AB12" * 20000,
    "slack-webhooks": "hooks.slack.com/services/T" * 3000,
    "sendgrid-prefixes": "SG." * 26000,
    "sendgrid-parts": "SG.aaaaaaaaaaaaaaaa." * 4000,
    "spaced-at": "a @ " * 20000,
    "spaced-at-with-domains": "aa @ b.cc( " * 7000,
    "html-escapes": "&#000" * 16000,
    "html-escape-zeros": "&#" + "0" * 80000,
    "token-names": "x_token=" * 10000,
    "token-names-with-values": "x_token= " * 9000,
    "camel-case-humps": "aB" * 40000,
    "camel-case-token-names": "aToken" * 13000,
    "pagination-names": "nextPageToken=abc123 " * 4000,
    "dotted-values": "password=" + "a." * 40000 + "!",
    "bracket-values": "password=[" + "]" * 80000 + "x",
    "tibetan-marks": "x" + "\u0f73" * 20000,
    "halfwidth-voicing-marks": "a" + "\u0301\uff9e" * 40000,
    "base64-chunks": "QUJDRA== " * 9000,
    "base64-chunks-with-percent": (b64("%41 text") + " ") * 6000,
}


@pytest.mark.parametrize(("name", "text"), HOSTILE_INPUTS.items(), ids=HOSTILE_INPUTS.keys())
@pytest.mark.parametrize("method", ["scan", "scan_url"])
def test_hostile_input_stays_fast(name: str, text: str, method: str) -> None:
    scan = getattr(PIIGuard(detect_phones=True), method)
    start = time.perf_counter()
    scan(text)
    assert time.perf_counter() - start < TIME_LIMIT, name
