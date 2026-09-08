from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr
import pytest

from halpha.configuration import VenueAccountType
from halpha.venue_account_qualification import (
    LiveVenueAccountQualifier,
    VenueAccountQualificationError,
    _HttpResponse,
    qualify_live_venue_account,
    require_live_venue_symbols,
)


API_KEY = SecretStr("account-key")
API_SECRET = SecretStr("account-secret")
SERVER_TIME_MS = 1_717_382_310_843


def _transport(
    *,
    is_lead_trader: bool,
    symbols: tuple[str, ...] = ("BTCUSDT",),
    requests: list[object] | None = None,
):
    def send(request, timeout_seconds: float, proxy_url: str | None) -> _HttpResponse:
        if requests is not None:
            requests.append((request, timeout_seconds, proxy_url))
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        unsigned_query = (
            f"timestamp={SERVER_TIME_MS}&recvWindow=5000"
        )
        expected_signature = hmac.new(
            API_SECRET.get_secret_value().encode("utf-8"),
            unsigned_query.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.binance.com"
        assert query == {
            "timestamp": [str(SERVER_TIME_MS)],
            "recvWindow": ["5000"],
            "signature": [expected_signature],
        }
        assert request.get_header("X-mbx-apikey") == API_KEY.get_secret_value()
        if parsed.path.endswith("/userStatus"):
            payload = {
                "code": "000000",
                "message": "success",
                "data": {
                    "isLeadTrader": is_lead_trader,
                    "time": SERVER_TIME_MS,
                },
                "success": True,
            }
        else:
            assert parsed.path.endswith("/leadSymbol")
            payload = {
                "code": "000000",
                "message": "success",
                "data": [
                    {
                        "symbol": symbol,
                        "baseAsset": symbol.removesuffix("USDT"),
                        "quoteAsset": "USDT",
                    }
                    for symbol in symbols
                ],
            }
        return _HttpResponse(
            status_code=200,
            final_url=request.full_url,
            body=json.dumps(payload).encode("utf-8"),
        )

    return send


def test_copy_lead_account_requires_status_and_symbol_whitelist() -> None:
    requests: list[object] = []

    facts = qualify_live_venue_account(
        VenueAccountType.USDM_COPY_LEAD,
        api_key=API_KEY,
        api_secret=API_SECRET,
        required_symbols=("ETHUSDT", "BTCUSDT"),
        proxy_url="http://127.0.0.1:7890",
        clock_ms=lambda: SERVER_TIME_MS,
        transport=_transport(
            is_lead_trader=True,
            symbols=("ETHUSDT", "BTCUSDT"),
            requests=requests,
        ),
    )

    assert facts.venue_account_type is VenueAccountType.USDM_COPY_LEAD
    assert facts.is_lead_trader is True
    assert facts.lead_symbols == ("BTCUSDT", "ETHUSDT")
    assert facts.server_time_ms == SERVER_TIME_MS
    assert len(requests) == 2
    assert all(item[1:] == (3.0, "http://127.0.0.1:7890") for item in requests)

    assert require_live_venue_symbols(facts, ("ETHUSDT",)) is facts
    with pytest.raises(
        VenueAccountQualificationError,
        match="VENUE_ACCOUNT_COPY_LEAD_SYMBOL_NOT_ALLOWED",
    ):
        require_live_venue_symbols(facts, ("SOLUSDT",))


def test_copy_lead_startup_can_cache_whitelist_before_any_activation_exists() -> None:
    facts = qualify_live_venue_account(
        VenueAccountType.USDM_COPY_LEAD,
        api_key=API_KEY,
        api_secret=API_SECRET,
        required_symbols=(),
        clock_ms=lambda: SERVER_TIME_MS,
        transport=_transport(
            is_lead_trader=True,
            symbols=("ETHUSDT", "SOLUSDT"),
        ),
    )

    assert facts.lead_symbols == ("ETHUSDT", "SOLUSDT")
    assert require_live_venue_symbols(facts, ("SOLUSDT",)) is facts


def test_personal_account_requires_non_lead_status_without_whitelist_request() -> None:
    requests: list[object] = []

    facts = qualify_live_venue_account(
        VenueAccountType.USDM_PERSONAL,
        api_key=API_KEY,
        api_secret=API_SECRET,
        required_symbols=("BTCUSDT",),
        clock_ms=lambda: SERVER_TIME_MS,
        transport=_transport(is_lead_trader=False, requests=requests),
    )

    assert facts.venue_account_type is VenueAccountType.USDM_PERSONAL
    assert facts.is_lead_trader is False
    assert facts.lead_symbols == ()
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("account_type", "is_lead_trader", "reason"),
    (
        (
            VenueAccountType.USDM_COPY_LEAD,
            False,
            "VENUE_ACCOUNT_COPY_LEAD_STATUS_REQUIRED",
        ),
        (
            VenueAccountType.USDM_PERSONAL,
            True,
            "VENUE_ACCOUNT_PERSONAL_NON_LEAD_REQUIRED",
        ),
    ),
)
def test_account_type_mismatch_fails_closed(
    account_type: VenueAccountType,
    is_lead_trader: bool,
    reason: str,
) -> None:
    with pytest.raises(VenueAccountQualificationError, match=reason):
        qualify_live_venue_account(
            account_type,
            api_key=API_KEY,
            api_secret=API_SECRET,
            required_symbols=("BTCUSDT",),
            clock_ms=lambda: SERVER_TIME_MS,
            transport=_transport(is_lead_trader=is_lead_trader),
        )


def test_copy_lead_missing_required_symbol_fails_closed() -> None:
    with pytest.raises(
        VenueAccountQualificationError,
        match="VENUE_ACCOUNT_COPY_LEAD_SYMBOL_NOT_ALLOWED",
    ):
        qualify_live_venue_account(
            VenueAccountType.USDM_COPY_LEAD,
            api_key=API_KEY,
            api_secret=API_SECRET,
            required_symbols=("ETHUSDT",),
            clock_ms=lambda: SERVER_TIME_MS,
            transport=_transport(is_lead_trader=True, symbols=("BTCUSDT",)),
        )


def test_stale_status_and_redirect_are_rejected_without_response_content() -> None:
    def stale(request, _timeout_seconds, _proxy_url):
        payload = {
            "code": "000000",
            "data": {"isLeadTrader": False, "time": SERVER_TIME_MS - 60_001},
            "success": True,
        }
        return _HttpResponse(200, request.full_url, json.dumps(payload).encode())

    with pytest.raises(VenueAccountQualificationError, match="STATUS_STALE"):
        qualify_live_venue_account(
            VenueAccountType.USDM_PERSONAL,
            api_key=API_KEY,
            api_secret=API_SECRET,
            required_symbols=("BTCUSDT",),
            clock_ms=lambda: SERVER_TIME_MS,
            transport=stale,
        )

    def redirected(request, _timeout_seconds, _proxy_url):
        return _HttpResponse(
            200,
            "https://example.invalid/leak",
            b'{"code":"000000","secret":"must-not-escape"}',
        )

    with pytest.raises(VenueAccountQualificationError) as captured:
        qualify_live_venue_account(
            VenueAccountType.USDM_PERSONAL,
            api_key=API_KEY,
            api_secret=API_SECRET,
            required_symbols=("BTCUSDT",),
            clock_ms=lambda: SERVER_TIME_MS,
            transport=redirected,
        )
    assert str(captured.value) == "VENUE_ACCOUNT_REDIRECT_FORBIDDEN"
    assert "must-not-escape" not in str(captured.value)


def test_qualifier_caches_briefly_and_refreshes_at_expiry() -> None:
    requests: list[object] = []
    observed_monotonic = [10.0]
    qualifier = LiveVenueAccountQualifier(
        VenueAccountType.USDM_COPY_LEAD,
        api_key=API_KEY,
        api_secret=API_SECRET,
        required_symbols=("BTCUSDT",),
        max_age_seconds=60.0,
        clock_ms=lambda: SERVER_TIME_MS,
        monotonic_clock=lambda: observed_monotonic[0],
        transport=_transport(is_lead_trader=True, requests=requests),
    )

    first = qualifier.require_current()
    observed_monotonic[0] = 69.999
    assert qualifier.require_current() is first
    assert len(requests) == 2

    observed_monotonic[0] = 70.0
    refreshed = qualifier.require_current()
    assert refreshed == first
    assert len(requests) == 4


def test_cached_qualification_check_never_performs_network_io() -> None:
    requests: list[object] = []
    observed_monotonic = [10.0]
    qualifier = LiveVenueAccountQualifier(
        VenueAccountType.USDM_PERSONAL,
        api_key=API_KEY,
        api_secret=API_SECRET,
        required_symbols=("BTCUSDT",),
        max_age_seconds=60.0,
        clock_ms=lambda: SERVER_TIME_MS,
        monotonic_clock=lambda: observed_monotonic[0],
        transport=_transport(is_lead_trader=False, requests=requests),
    )

    first = qualifier.require_current()
    observed_monotonic[0] = 69.999
    assert qualifier.require_cached_current() is first
    assert len(requests) == 1

    observed_monotonic[0] = 70.0
    with pytest.raises(
        VenueAccountQualificationError,
        match="VENUE_ACCOUNT_QUALIFICATION_STALE",
    ):
        qualifier.require_cached_current()
    assert len(requests) == 1


def test_qualifier_does_not_poll_again_during_binance_retry_after() -> None:
    requests = [0]
    observed_monotonic = [10.0]

    def rate_limited(_request, _timeout_seconds, _proxy_url):
        requests[0] += 1
        raise VenueAccountQualificationError(
            "VENUE_ACCOUNT_HTTP_REJECTED_429",
            retry_after_seconds=90.0,
        )

    qualifier = LiveVenueAccountQualifier(
        VenueAccountType.USDM_PERSONAL,
        api_key=API_KEY,
        api_secret=API_SECRET,
        required_symbols=("BTCUSDT",),
        clock_ms=lambda: SERVER_TIME_MS,
        monotonic_clock=lambda: observed_monotonic[0],
        transport=rate_limited,
    )

    with pytest.raises(
        VenueAccountQualificationError,
        match="VENUE_ACCOUNT_HTTP_REJECTED_429",
    ):
        qualifier.require_current()
    observed_monotonic[0] = 50.0
    with pytest.raises(
        VenueAccountQualificationError,
        match="VENUE_ACCOUNT_RATE_LIMIT_BACKOFF",
    ) as captured:
        qualifier.require_current()

    assert captured.value.retry_after_seconds == 50.0
    assert requests == [1]
