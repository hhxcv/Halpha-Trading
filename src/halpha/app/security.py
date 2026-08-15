"""Local-request web security primitives for the App process."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from asgi_csrf import asgi_csrf
from pydantic import SecretStr
from starlette.responses import JSONResponse


class ASGIApp(Protocol):
    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None: ...


PROGRAMMATIC_CALLER_HEADER = "X-Halpha-Caller"
PROGRAMMATIC_MONITOR_CALLER = "MONITOR"
PROGRAMMATIC_CALLER_SCOPE_KEY = "halpha.programmatic_caller"
_PROGRAMMATIC_PLAN_MUTATION_ROUTES = (
    ("POST", re.compile(r"^/api/v1/account-position-operations/preview$")),
    ("POST", re.compile(r"^/api/v1/order-schedules/preview$")),
    ("POST", re.compile(r"^/api/v1/plans$")),
    ("PUT", re.compile(r"^/api/v1/plans/[^/]+$")),
    ("DELETE", re.compile(r"^/api/v1/plans/[^/]+$")),
    ("POST", re.compile(r"^/api/v1/plans/[^/]+/submit-and-start$")),
    ("POST", re.compile(r"^/api/v1/plans/[^/]+/ai-review$")),
    (
        "POST",
        re.compile(r"^/api/v1/plan-versions/[^/]+/activation-preview$"),
    ),
    ("POST", re.compile(r"^/api/v1/activations$")),
)


def programmatic_plan_mutation_allowed(method: str, path: str) -> bool:
    """Return whether the native Monitor channel may mutate this API path."""

    normalized_method = method.upper()
    return any(
        normalized_method == allowed_method and pattern.fullmatch(path) is not None
        for allowed_method, pattern in _PROGRAMMATIC_PLAN_MUTATION_ROUTES
    )


def programmatic_caller(scope: dict[str, Any]) -> str | None:
    value = scope.get(PROGRAMMATIC_CALLER_SCOPE_KEY)
    return value if isinstance(value, str) else None


def add_programmatic_plan_openapi_contract(
    document: dict[str, Any],
) -> dict[str, Any]:
    """Describe the shared Monitor transport header on the actual domain routes."""

    components = document.setdefault("components", {})
    parameters = components.setdefault("parameters", {})
    parameters["HalphaProgrammaticCaller"] = {
        "name": PROGRAMMATIC_CALLER_HEADER,
        "in": "header",
        "required": False,
        "schema": {
            "type": "string",
            "enum": [PROGRAMMATIC_MONITOR_CALLER],
        },
        "description": (
            "Required with value MONITOR for a native loopback Monitor mutation; "
            "browser workbench requests use their local Origin and CSRF token instead."
        ),
    }
    reference = {"$ref": "#/components/parameters/HalphaProgrammaticCaller"}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or not programmatic_plan_mutation_allowed(
                method,
                path,
            ):
                continue
            operation_parameters = operation.setdefault("parameters", [])
            if reference not in operation_parameters:
                operation_parameters.append(reference)
            operation["x-halpha-programmatic-caller"] = PROGRAMMATIC_MONITOR_CALLER
    return document


def allowed_local_origin(value: str, port: int) -> bool:
    """Return whether an HTTP or WebSocket Origin is the local workbench."""

    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed_port == port
        and not parsed.username
        and not parsed.password
    )


class CsrfMiddleware:
    """Thin Starlette middleware adapter around the selected ASGI component."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        signing_secret: SecretStr,
        cookie_name: str,
    ) -> None:
        self._app = asgi_csrf(
            app,
            cookie_name=cookie_name,
            http_header="x-csrftoken",
            signing_secret=signing_secret.get_secret_value(),
            always_protect=[],
            always_set_cookie=True,
            skip_if_scope=lambda scope: (
                programmatic_caller(scope) == PROGRAMMATIC_MONITOR_CALLER
            ),
            cookie_path="/",
            cookie_domain=None,
            cookie_secure=False,
            cookie_samesite="Strict",
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._app(scope, receive, send)


def csrf_cookie_name(port: int) -> str:
    """Keep local workbench CSRF state isolated across environment origins."""

    return f"halpha_csrf_{port}"


class LocalRequestBoundaryMiddleware:
    """Separate local browser requests from the scoped native Monitor channel."""

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self._app = app
        self._port = port

    def _allowed_origin(self, value: str) -> bool:
        return allowed_local_origin(value, self._port)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        if "authorization" in headers:
            response = JSONResponse(
                {"detail": {"code": "AUTHORIZATION_HEADER_FORBIDDEN"}},
                status_code=400,
            )
            await response(scope, receive, send)
            return

        caller = headers.get(PROGRAMMATIC_CALLER_HEADER.lower())
        origin = headers.get("origin")
        referer = headers.get("referer")
        if caller is not None:
            if caller != PROGRAMMATIC_MONITOR_CALLER:
                response = JSONResponse(
                    {"detail": {"code": "PROGRAMMATIC_CALLER_INVALID"}},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
            if origin is not None or referer is not None:
                response = JSONResponse(
                    {"detail": {"code": "PROGRAMMATIC_BROWSER_ORIGIN_FORBIDDEN"}},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
            if (
                scope.get("method") not in self._SAFE_METHODS
                and not programmatic_plan_mutation_allowed(
                    str(scope.get("method", "")),
                    str(scope.get("path", "")),
                )
            ):
                response = JSONResponse(
                    {"detail": {"code": "PROGRAMMATIC_API_SCOPE_FORBIDDEN"}},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
            scope = {
                **scope,
                PROGRAMMATIC_CALLER_SCOPE_KEY: PROGRAMMATIC_MONITOR_CALLER,
            }

        if scope.get("method") not in self._SAFE_METHODS and caller is None:
            if not (
                (origin is not None and self._allowed_origin(origin))
                or (origin is None and referer is not None and self._allowed_origin(referer))
            ):
                response = JSONResponse(
                    {"detail": {"code": "LOCAL_ORIGIN_REQUIRED"}},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"content-security-policy", b"default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                        (b"referrer-policy", b"same-origin"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                    ]
                )
                message = {**message, "headers": response_headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)
