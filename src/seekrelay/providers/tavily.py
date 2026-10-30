"""Tavily (https://tavily.com) - search and extraction API built for LLM agents.

API reference: https://docs.tavily.com/documentation/api-reference
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from typing import Any, NoReturn

import httpx

from ..errors import (
    ConfigurationError,
    InvalidRequestError,
    ProviderError,
    ProviderUnavailableError,
    QuotaExceededError,
    ResponseFormatError,
)
from ..models import (
    ExtractedPage,
    ExtractResponse,
    FailedExtraction,
    SearchQuery,
    SearchResponse,
    SearchResult,
    Topic,
    is_http_url,
    optional_str,
    validate_url,
)
from .base import SearchProvider

__all__ = ["TAVILY_BASE_URL", "TAVILY_COUNTRY_NAMES", "TavilyProvider"]

logger = logging.getLogger("seekrelay")

TAVILY_BASE_URL = "https://api.tavily.com"
EXTRACT_BATCH_SIZE = 20  # documented maximum number of URLs per /extract call

TAVILY_COUNTRY_NAMES: dict[str, str] = {
    # Tavily's `country` parameter takes lower-case English names, not ISO codes. This is
    # its full list (API reference, October 2026); other codes are skipped with a debug log.
    "ad": "andorra",
    "ae": "united arab emirates",
    "af": "afghanistan",
    "al": "albania",
    "am": "armenia",
    "ao": "angola",
    "ar": "argentina",
    "at": "austria",
    "au": "australia",
    "az": "azerbaijan",
    "ba": "bosnia and herzegovina",
    "bb": "barbados",
    "bd": "bangladesh",
    "be": "belgium",
    "bf": "burkina faso",
    "bg": "bulgaria",
    "bh": "bahrain",
    "bi": "burundi",
    "bj": "benin",
    "bn": "brunei",
    "bo": "bolivia",
    "br": "brazil",
    "bs": "bahamas",
    "bt": "bhutan",
    "bw": "botswana",
    "by": "belarus",
    "bz": "belize",
    "ca": "canada",
    "cf": "central african republic",
    "cg": "congo",
    "ch": "switzerland",
    "cl": "chile",
    "cm": "cameroon",
    "cn": "china",
    "co": "colombia",
    "cr": "costa rica",
    "cu": "cuba",
    "cv": "cape verde",
    "cy": "cyprus",
    "cz": "czech republic",
    "de": "germany",
    "dj": "djibouti",
    "dk": "denmark",
    "do": "dominican republic",
    "dz": "algeria",
    "ec": "ecuador",
    "ee": "estonia",
    "eg": "egypt",
    "er": "eritrea",
    "es": "spain",
    "et": "ethiopia",
    "fi": "finland",
    "fj": "fiji",
    "fr": "france",
    "ga": "gabon",
    "gb": "united kingdom",
    "ge": "georgia",
    "gh": "ghana",
    "gm": "gambia",
    "gn": "guinea",
    "gq": "equatorial guinea",
    "gr": "greece",
    "gt": "guatemala",
    "hn": "honduras",
    "hr": "croatia",
    "ht": "haiti",
    "hu": "hungary",
    "id": "indonesia",
    "ie": "ireland",
    "il": "israel",
    "in": "india",
    "iq": "iraq",
    "ir": "iran",
    "is": "iceland",
    "it": "italy",
    "jm": "jamaica",
    "jo": "jordan",
    "jp": "japan",
    "ke": "kenya",
    "kg": "kyrgyzstan",
    "kh": "cambodia",
    "km": "comoros",
    "kp": "north korea",
    "kr": "south korea",
    "kw": "kuwait",
    "kz": "kazakhstan",
    "lb": "lebanon",
    "li": "liechtenstein",
    "lk": "sri lanka",
    "lr": "liberia",
    "ls": "lesotho",
    "lt": "lithuania",
    "lu": "luxembourg",
    "lv": "latvia",
    "ly": "libya",
    "ma": "morocco",
    "mc": "monaco",
    "md": "moldova",
    "me": "montenegro",
    "mg": "madagascar",
    "mk": "north macedonia",
    "ml": "mali",
    "mm": "myanmar",
    "mn": "mongolia",
    "mr": "mauritania",
    "mt": "malta",
    "mu": "mauritius",
    "mv": "maldives",
    "mw": "malawi",
    "mx": "mexico",
    "my": "malaysia",
    "mz": "mozambique",
    "na": "namibia",
    "ne": "niger",
    "ng": "nigeria",
    "ni": "nicaragua",
    "nl": "netherlands",
    "no": "norway",
    "np": "nepal",
    "nz": "new zealand",
    "om": "oman",
    "pa": "panama",
    "pe": "peru",
    "pg": "papua new guinea",
    "ph": "philippines",
    "pk": "pakistan",
    "pl": "poland",
    "pt": "portugal",
    "py": "paraguay",
    "qa": "qatar",
    "ro": "romania",
    "rs": "serbia",
    "ru": "russia",
    "rw": "rwanda",
    "sa": "saudi arabia",
    "sd": "sudan",
    "se": "sweden",
    "sg": "singapore",
    "si": "slovenia",
    "sk": "slovakia",
    "sn": "senegal",
    "so": "somalia",
    "ss": "south sudan",
    "sv": "el salvador",
    "sy": "syria",
    "td": "chad",
    "tg": "togo",
    "th": "thailand",
    "tj": "tajikistan",
    "tm": "turkmenistan",
    "tn": "tunisia",
    "tr": "turkey",
    "tt": "trinidad and tobago",
    "tw": "taiwan",
    "tz": "tanzania",
    "ua": "ukraine",
    "ug": "uganda",
    "uk": "united kingdom",
    "us": "united states",
    "uy": "uruguay",
    "uz": "uzbekistan",
    "ve": "venezuela",
    "vn": "vietnam",
    "ye": "yemen",
    "za": "south africa",
    "zm": "zambia",
    "zw": "zimbabwe",
}


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer of 400 digits
        return None
    return number if math.isfinite(number) else None  # JSON has no NaN or Infinity


class TavilyProvider(SearchProvider):
    """Tavily search + extract.

    Args:
        api_key: ``tvly-...`` key from https://app.tavily.com.
        base_url: Override for proxies or the EU endpoint if you use one.
        include_answer: Ask Tavily for a short LLM-generated answer (``basic``).
        **kwargs: ``timeout``, ``client``, ``user_agent`` - see :class:`SearchProvider`.

    Credit cost (as documented in Oct 2026): basic search 1 credit, advanced 2;
    extract 1 credit per 5 successful URLs (advanced: 2).
    """

    name = "tavily"
    supports_extract = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TAVILY_BASE_URL,
        include_answer: bool = False,
        **kwargs: Any,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ConfigurationError("Tavily API key is empty")
        super().__init__(**kwargs)
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._include_answer = include_answer

    # ------------------------------------------------------------------ search

    def build_search_payload(self, query: SearchQuery) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query.text,
            "search_depth": query.depth.value,
            "max_results": query.max_results,
            "topic": query.topic.value,
            "include_answer": "basic" if self._include_answer else False,
            "include_raw_content": "markdown" if query.include_raw_content else False,
            # Off by default outside news, which would leave `published_at` empty.
            "include_published_date": True,
        }
        if query.time_range is not None:
            payload["time_range"] = query.time_range.value
        if query.include_domains:
            payload["include_domains"] = list(query.include_domains)
        if query.exclude_domains:
            payload["exclude_domains"] = list(query.exclude_domains)
        if query.language:
            payload["language"] = query.language
        if query.country:
            country_name = TAVILY_COUNTRY_NAMES.get(query.country)
            if country_name is None:
                logger.debug("tavily: no country name for %r, parameter skipped", query.country)
            elif query.topic is not Topic.GENERAL:
                logger.debug("tavily: country is only supported for topic=general, skipped")
            else:
                payload["country"] = country_name
        return payload

    async def search(self, query: SearchQuery) -> SearchResponse:
        started = time.perf_counter()
        response = await self._send(
            "POST",
            f"{self._base_url}/search",
            json=self.build_search_payload(query),
            headers=self._headers(),
        )
        data = self._json(response)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ResponseFormatError(self.name, "'results' list missing in search response")

        results: list[SearchResult] = []
        for item in data["results"]:
            if not isinstance(item, dict) or not is_http_url(str(item.get("url") or "")):
                continue
            # The filters are native here, but a filter must hold whatever the provider sends.
            if not query.allows_url(str(item["url"]).strip()):
                continue
            results.append(
                SearchResult(
                    title=optional_str(item.get("title")) or "",
                    url=str(item["url"]).strip(),
                    snippet=optional_str(item.get("content")) or "",
                    provider=self.name,
                    score=_as_float(item.get("score")),
                    published_at=optional_str(item.get("published_date")),
                    raw_content=(
                        optional_str(item.get("raw_content")) if query.include_raw_content else None
                    ),
                )
            )
        return SearchResponse(
            query=query,
            provider=self.name,
            results=results[: query.max_results],
            answer=optional_str(data.get("answer")),
            latency_ms=self._elapsed_ms(started),
            request_id=optional_str(data.get("request_id")),
        )

    # ------------------------------------------------------------------ extract

    async def extract(self, urls: Sequence[str], *, advanced: bool = False) -> ExtractResponse:
        try:
            unique = list(dict.fromkeys(validate_url(u) for u in urls))
        except ValueError as exc:
            raise InvalidRequestError(self.name, str(exc)) from exc
        if not unique:
            raise InvalidRequestError(self.name, "no URLs to extract")

        started = time.perf_counter()
        batches = [
            unique[i : i + EXTRACT_BATCH_SIZE] for i in range(0, len(unique), EXTRACT_BATCH_SIZE)
        ]
        outcomes = await asyncio.gather(
            *(self._extract_batch(b, advanced) for b in batches), return_exceptions=True
        )

        # If every batch failed, raise so the relay can retry or fall back. If some
        # succeeded, keep them (their credits are already spent) and report the URLs of
        # failed batches individually instead of failing the whole call, which would
        # make a retry pay for the successful batches again.
        errors = [o for o in outcomes if isinstance(o, BaseException)]
        if len(errors) == len(outcomes):
            raise errors[0]
        for error in errors:
            if not isinstance(error, ProviderError):
                raise error

        pages: list[ExtractedPage] = []
        failed: list[FailedExtraction] = []
        for batch, outcome in zip(batches, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                message = outcome.message if isinstance(outcome, ProviderError) else str(outcome)
                failed.extend(FailedExtraction(url=url, error=message) for url in batch)
                continue
            batch_pages, batch_failed = outcome
            pages.extend(batch_pages)
            failed.extend(batch_failed)
        return ExtractResponse(
            provider=self.name,
            pages=pages,
            failed=failed,
            latency_ms=self._elapsed_ms(started),
        )

    async def _extract_batch(
        self, urls: list[str], advanced: bool
    ) -> tuple[list[ExtractedPage], list[FailedExtraction]]:
        response = await self._send(
            "POST",
            f"{self._base_url}/extract",
            json={
                "urls": urls,
                "extract_depth": "advanced" if advanced else "basic",
                "format": "markdown",
            },
            headers=self._headers(),
        )
        data = self._json(response)
        if not isinstance(data, dict):
            raise ResponseFormatError(self.name, "unexpected extract response")
        pages = [
            ExtractedPage(
                url=str(item["url"]),
                content=optional_str(item.get("raw_content")) or "",
                provider=self.name,
            )
            for item in data.get("results") or []
            if isinstance(item, dict) and is_http_url(str(item.get("url") or ""))
        ]
        failed = [
            FailedExtraction(
                url=optional_str(item.get("url")) or "",
                error=optional_str(item.get("error")) or "unknown",
            )
            for item in data.get("failed_results") or []
            if isinstance(item, dict)
        ]
        return pages, failed

    # ------------------------------------------------------------------ internals

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _raise_for_status(self, response: httpx.Response) -> NoReturn:
        status = response.status_code
        if status in (432, 433):
            kind = "plan/key limit" if status == 432 else "pay-as-you-go limit"
            raise QuotaExceededError(
                self.name,
                f"HTTP {status}: {kind} exceeded - {self._error_message(response)}",
                status_code=status,
            )
        if status == 434:
            raise ProviderUnavailableError(
                self.name,
                "HTTP 434: live-fetch capacity temporarily exceeded - "
                + self._error_message(response),
                status_code=status,
            )
        super()._raise_for_status(response)
