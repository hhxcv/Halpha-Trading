"""Fail-closed Binance Live venue-account type qualification.

The configured account type is an operator declaration, not venue evidence.
Before a LIVE_WRITE executor may increase risk, this module verifies the exact
credential against Binance's signed Copy Trading status endpoint. Copy-lead
accounts additionally have to expose every configured instrument in the lead
trading symbol whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from time import monotonic, time
from threading import Lock
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from pydantic import SecretStr

from halpha.configuration import VenueAccountType
from halpha.venue_integration.binance_rate_limits import (
    binance_retry_after_seconds,
)


_COPY_TRADING_API_ORIGIN = "https://api.binance.com"
_LEAD_STATUS_PATH = "/sapi/v1/copyTrading/futures/userStatus"
_LEAD_SYMBOL_PATH = "/sapi/v1/copyTrading/futures/leadSymbol"
_SUCCESS_CODE = "000000"
_RECV_WINDOW_MILLISECONDS = 5_000
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_SERVER_CLOCK_SKEW_MILLISECONDS = 60_000


class VenueAccountQualificationError(RuntimeError):
    """Sanitized rejection of an unproven or mismatched venue account type."""

    def __init__(
        self,
        reason_code: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class VenueAccountFacts:
    venue_account_type: VenueAccountType
    is_lead_trader: bool
    lead_symbols: tuple[str, ...]
    server_time_ms: int


def require_live_venue_symbols(
    facts: VenueAccountFacts,
    required_symbols: tuple[str, ...],
) -> VenueAccountFacts:
    """Validate an activation's symbols against already-cached account facts."""

    normalized_symbols = tuple(sorted(set(required_symbols)))
    if not normalized_symbols or any(
        not symbol or symbol != symbol.upper() for symbol in normalized_symbols
    ):
        raise VenueAccountQualificationError(
            "VENUE_ACCOUNT_REQUIRED_SYMBOLS_INVALID"
        )
    if facts.venue_account_type is VenueAccountType.USDM_COPY_LEAD:
        if set(normalized_symbols) - set(facts.lead_symbols):
            raise VenueAccountQualificationError(
                "VENUE_ACCOUNT_COPY_LEAD_SYMBOL_NOT_ALLOWED"
            )
    return facts


@dataclass(frozen=True)
class _HttpResponse:
    status_code: int
    final_url: str
    body: bytes


Transport = Callable[[Request, float, str | None], _HttpResponse]
ClockMilliseconds = Callable[[], int]
MonotonicClock = Callable[[], float]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_transport(
    request: Request,
    timeout_seconds: float,
    proxy_url: str | None,
) -> _HttpResponse:
    proxy = ProxyHandler({"https": proxy_url} if proxy_url is not None else {})
    opener = build_opener(proxy, _RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise VenueAccountQualificationError(
                    "VENUE_ACCOUNT_RESPONSE_TOO_LARGE"
                )
            return _HttpResponse(
                status_code=int(response.status),
                final_url=str(response.geturl()),
                body=body,
            )
    except VenueAccountQualificationError:
        raise
    except HTTPError as exc:
        raise VenueAccountQualificationError(
            f"VENUE_ACCOUNT_HTTP_REJECTED_{exc.code}",
            retry_after_seconds=binance_retry_after_seconds(exc),
        ) from None
    except (OSError, URLError) as exc:
        raise VenueAccountQualificationError(
            f"VENUE_ACCOUNT_HTTP_UNAVAILABLE_{type(exc).__name__.upper()}"
        ) from None


def _signed_get(
    path: str,
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    proxy_url: str | None,
    timeout_seconds: float,
    clock_ms: ClockMilliseconds,
    transport: Transport,
) -> dict[str, object]:
    timestamp = clock_ms()
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
        raise VenueAccountQualificationError("VENUE_ACCOUNT_CLOCK_INVALID")
    unsigned_query = urlencode(
        (
            ("timestamp", str(timestamp)),
            ("recvWindow", str(_RECV_WINDOW_MILLISECONDS)),
        )
    )
    signature = hmac.new(
        api_secret.get_secret_value().encode("utf-8"),
        unsigned_query.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    request_url = (
        f"{_COPY_TRADING_API_ORIGIN}{path}?{unsigned_query}&signature={signature}"
    )
    request = Request(
        request_url,
        headers={"X-MBX-APIKEY": api_key.get_secret_value()},
        method="GET",
    )
    try:
        response = transport(request, timeout_seconds, proxy_url)
    except VenueAccountQualificationError:
        raise
    except Exception as exc:
        raise VenueAccountQualificationError(
            f"VENUE_ACCOUNT_HTTP_UNAVAILABLE_{type(exc).__name__.upper()}"
        ) from None
    if response.status_code != 200:
        raise VenueAccountQualificationError(
            f"VENUE_ACCOUNT_HTTP_REJECTED_{response.status_code}"
        )
    if response.final_url != request_url:
        raise VenueAccountQualificationError("VENUE_ACCOUNT_REDIRECT_FORBIDDEN")
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VenueAccountQualificationError(
            "VENUE_ACCOUNT_RESPONSE_INVALID_JSON"
        ) from None
    if not isinstance(payload, dict):
        raise VenueAccountQualificationError("VENUE_ACCOUNT_RESPONSE_INVALID")
    if payload.get("code") != _SUCCESS_CODE:
        raise VenueAccountQualificationError("VENUE_ACCOUNT_RESPONSE_REJECTED")
    return payload


def _lead_status(
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    proxy_url: str | None,
    timeout_seconds: float,
    clock_ms: ClockMilliseconds,
    transport: Transport,
) -> tuple[bool, int]:
    payload = _signed_get(
        _LEAD_STATUS_PATH,
        api_key=api_key,
        api_secret=api_secret,
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
        clock_ms=clock_ms,
        transport=transport,
    )
    if payload.get("success") is not True:
        raise VenueAccountQualificationError("VENUE_ACCOUNT_STATUS_UNSUCCESSFUL")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise VenueAccountQualificationError("VENUE_ACCOUNT_STATUS_INVALID")
    is_lead_trader = data.get("isLeadTrader")
    server_time_ms = data.get("time")
    if not isinstance(is_lead_trader, bool) or (
        not isinstance(server_time_ms, int) or isinstance(server_time_ms, bool)
    ):
        raise VenueAccountQualificationError("VENUE_ACCOUNT_STATUS_INVALID")
    if abs(clock_ms() - server_time_ms) > _MAX_SERVER_CLOCK_SKEW_MILLISECONDS:
        raise VenueAccountQualificationError("VENUE_ACCOUNT_STATUS_STALE")
    return is_lead_trader, server_time_ms


def _lead_symbols(
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    proxy_url: str | None,
    timeout_seconds: float,
    clock_ms: ClockMilliseconds,
    transport: Transport,
) -> tuple[str, ...]:
    payload = _signed_get(
        _LEAD_SYMBOL_PATH,
        api_key=api_key,
        api_secret=api_secret,
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
        clock_ms=clock_ms,
        transport=transport,
    )
    data = payload.get("data")
    if not isinstance(data, list):
        raise VenueAccountQualificationError("VENUE_ACCOUNT_SYMBOLS_INVALID")
    symbols: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise VenueAccountQualificationError("VENUE_ACCOUNT_SYMBOLS_INVALID")
        symbol = item.get("symbol")
        base_asset = item.get("baseAsset")
        quote_asset = item.get("quoteAsset")
        if not all(
            isinstance(value, str) and value and value == value.upper()
            for value in (symbol, base_asset, quote_asset)
        ):
            raise VenueAccountQualificationError("VENUE_ACCOUNT_SYMBOLS_INVALID")
        symbols.add(symbol)
    return tuple(sorted(symbols))


def qualify_live_venue_account(
    account_type: VenueAccountType,
    *,
    api_key: SecretStr,
    api_secret: SecretStr,
    required_symbols: tuple[str, ...],
    proxy_url: str | None = None,
    timeout_seconds: float = 3.0,
    clock_ms: ClockMilliseconds | None = None,
    transport: Transport = _default_transport,
) -> VenueAccountFacts:
    """Prove that one credential matches its declared fixed Live account type."""

    if account_type not in {
        VenueAccountType.USDM_COPY_LEAD,
        VenueAccountType.USDM_PERSONAL,
    }:
        raise VenueAccountQualificationError("VENUE_ACCOUNT_LIVE_TYPE_REQUIRED")
    normalized_symbols = tuple(sorted(set(required_symbols)))
    if any(
        not symbol or symbol != symbol.upper() for symbol in normalized_symbols
    ):
        raise VenueAccountQualificationError("VENUE_ACCOUNT_REQUIRED_SYMBOLS_INVALID")
    observed_clock = clock_ms or (lambda: int(time() * 1_000))
    is_lead_trader, server_time_ms = _lead_status(
        api_key=api_key,
        api_secret=api_secret,
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
        clock_ms=observed_clock,
        transport=transport,
    )
    if account_type is VenueAccountType.USDM_COPY_LEAD:
        if not is_lead_trader:
            raise VenueAccountQualificationError(
                "VENUE_ACCOUNT_COPY_LEAD_STATUS_REQUIRED"
            )
        lead_symbols = _lead_symbols(
            api_key=api_key,
            api_secret=api_secret,
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
            clock_ms=observed_clock,
            transport=transport,
        )
        missing = set(normalized_symbols) - set(lead_symbols)
        if missing:
            raise VenueAccountQualificationError(
                "VENUE_ACCOUNT_COPY_LEAD_SYMBOL_NOT_ALLOWED"
            )
    else:
        if is_lead_trader:
            raise VenueAccountQualificationError(
                "VENUE_ACCOUNT_PERSONAL_NON_LEAD_REQUIRED"
            )
        lead_symbols = ()
    return VenueAccountFacts(
        venue_account_type=account_type,
        is_lead_trader=is_lead_trader,
        lead_symbols=lead_symbols,
        server_time_ms=server_time_ms,
    )


class LiveVenueAccountQualifier:
    """Cache current account evidence briefly and refresh it before new risk."""

    def __init__(
        self,
        account_type: VenueAccountType,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        required_symbols: tuple[str, ...],
        proxy_url: str | None = None,
        max_age_seconds: float = 60.0,
        timeout_seconds: float = 3.0,
        clock_ms: ClockMilliseconds | None = None,
        monotonic_clock: MonotonicClock = monotonic,
        transport: Transport = _default_transport,
    ) -> None:
        if max_age_seconds <= 0 or timeout_seconds <= 0:
            raise VenueAccountQualificationError(
                "VENUE_ACCOUNT_QUALIFIER_WINDOW_INVALID"
            )
        self._account_type = account_type
        self._api_key = api_key
        self._api_secret = api_secret
        self._required_symbols = required_symbols
        self._proxy_url = proxy_url
        self._max_age_seconds = max_age_seconds
        self._timeout_seconds = timeout_seconds
        self._clock_ms = clock_ms
        self._monotonic_clock = monotonic_clock
        self._transport = transport
        self._facts: VenueAccountFacts | None = None
        self._checked_at: float | None = None
        self._retry_not_before: float | None = None
        self._state_lock = Lock()
        self._refresh_lock = Lock()

    def _require_retry_ready(self, now: float) -> None:
        with self._state_lock:
            if self._retry_not_before is not None and now < self._retry_not_before:
                raise VenueAccountQualificationError(
                    "VENUE_ACCOUNT_RATE_LIMIT_BACKOFF",
                    retry_after_seconds=self._retry_not_before - now,
                )

    def refresh(self) -> VenueAccountFacts:
        """Perform one explicit network refresh, intended for a worker thread."""

        with self._refresh_lock:
            now = self._monotonic_clock()
            self._require_retry_ready(now)
            try:
                facts = qualify_live_venue_account(
                    self._account_type,
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    required_symbols=self._required_symbols,
                    proxy_url=self._proxy_url,
                    timeout_seconds=self._timeout_seconds,
                    clock_ms=self._clock_ms,
                    transport=self._transport,
                )
            except VenueAccountQualificationError as exc:
                if exc.retry_after_seconds is not None:
                    with self._state_lock:
                        self._retry_not_before = max(
                            self._retry_not_before or now,
                            now + exc.retry_after_seconds,
                        )
                raise
            with self._state_lock:
                self._facts = facts
                self._checked_at = self._monotonic_clock()
                self._retry_not_before = None
            return facts

    def require_current(self) -> VenueAccountFacts:
        """Return fresh evidence, refreshing synchronously only at startup."""

        now = self._monotonic_clock()
        self._require_retry_ready(now)
        with self._state_lock:
            if (
                self._facts is not None
                and self._checked_at is not None
                and 0 <= now - self._checked_at < self._max_age_seconds
            ):
                return self._facts
        return self.refresh()

    def require_cached_current(self) -> VenueAccountFacts:
        """Validate cached evidence without performing network I/O."""

        now = self._monotonic_clock()
        self._require_retry_ready(now)
        with self._state_lock:
            if (
                self._facts is None
                or self._checked_at is None
                or not 0 <= now - self._checked_at < self._max_age_seconds
            ):
                raise VenueAccountQualificationError(
                    "VENUE_ACCOUNT_QUALIFICATION_STALE"
                )
            return self._facts
