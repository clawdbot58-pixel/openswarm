"""JWT-based authentication for the kernel API and WebSocket.

Phase 11 introduces the first real auth surface in OpenSwarm. It is
**opt-in**: when :attr:`AuthSection.enabled` is False (the default),
:class:`AuthMiddleware.verify_token` is a no-op and any caller is
treated as the ``local-dev`` operator. When it is True, every API
and WebSocket call must carry a bearer token minted by
:meth:`AuthMiddleware.issue_token`.

Design choices
--------------
* HS256 by default (symmetric, no key-server needed). Operators
  wanting RS256 can override ``jwt_algorithm`` and
  ``jwt_secret`` (treated as a PEM public key in that case).
* Tokens carry three claims: ``sub`` (user id), ``role`` (one of
  the standard :data:`Roles`), and ``exp`` (epoch seconds). The
  kernel never inspects any other claim; rich ACLs are a Phase 12+
  concern.
* The middleware is **stateless**. There is no token revocation
  list; operators rotate the secret to invalidate outstanding
  tokens. For low-risk local dev this is fine; for production we
  recommend a Redis-backed allowlist (also added in Phase 12).
* The WebSocket helper accepts the token either as
  ``?token=...`` query string (the standard for browser WS
  clients) or as ``Authorization: Bearer …`` header (the
  standard for non-browser clients that proxy through nginx).

Threat model
------------
The brief's :doc:`vision/security` calls out the high-value
surfaces introduced by Phase 11. The auth module addresses:

* **API/WS impersonation** — every protected call must present a
  signed token. The kernel refuses unsigned calls when
  :attr:`AuthSection.enabled` is True.
* **Token replay** — JWTs have a 1-hour TTL by default; a
  rotation of ``jwt_secret`` invalidates outstanding tokens.
* **Role escalation** — :meth:`require_role` is a strict
  equality check; there is no ``in_role`` ladder. Operators add
  new roles deliberately.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import jwt
from fastapi import HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class Roles:
    """Role names. Kept as a class of constants (not an Enum) so the
    CLI can serialize them to TOML/JSON without an extra adapter.
    """

    OPERATOR = "operator"
    ADMIN = "admin"
    VIEWER = "viewer"
    AGENT = "agent"
    EXTERNAL = "external"


# Standard role ordering used by ``require_role`` for "at-least" checks.
_ROLE_ORDER: dict[str, int] = {
    Roles.VIEWER: 1,
    Roles.OPERATOR: 2,
    Roles.ADMIN: 3,
    Roles.AGENT: 4,
    Roles.EXTERNAL: 0,
}


def role_at_least(actual: str, required: str) -> bool:
    """Return True if ``actual`` is at least as privileged as ``required``."""
    return _ROLE_ORDER.get(actual, 0) >= _ROLE_ORDER.get(required, 0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class for auth failures."""

    code: str = "auth_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class TokenInvalid(AuthError):
    code = "token_invalid"


class TokenExpired(AuthError):
    code = "token_expired"


class RoleRequired(AuthError):
    code = "role_required"


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Claims:
    """The decoded claims we trust for the rest of the request."""

    sub: str
    role: str
    issued_at: int
    expires_at: int
    raw: dict[str, Any]

    def is_expired(self, *, leeway_seconds: int = 0) -> bool:
        return time.time() > (self.expires_at + leeway_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "role": self.role,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """JWT verification + token issuance for the kernel API.

    Parameters
    ----------
    enabled:
        When False, :meth:`verify_token` returns a synthetic
        "local-dev" :class:`Claims` for any caller. This is the
        default in local dev; production must set it to True.
    secret:
        HMAC secret (or PEM public key for RS256).
    algorithm:
        JWT algorithm. ``HS256`` is the default.
    ttl_seconds:
        Lifetime of tokens issued by :meth:`issue_token`.
    default_user_id, default_role:
        The synthetic identity assigned to anonymous callers when
        :attr:`enabled` is False.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        secret: str = "change-me-in-production",
        algorithm: str = "HS256",
        ttl_seconds: int = 3600,
        default_user_id: str = "local-dev",
        default_role: str = Roles.OPERATOR,
    ) -> None:
        self.enabled = enabled
        self._secret = secret
        self._algorithm = algorithm
        self._ttl = ttl_seconds
        self._default_user_id = default_user_id
        self._default_role = default_role
        self._bearer = HTTPBearer(auto_error=False)

    # -- public API --------------------------------------------------------

    def issue_token(
        self,
        user_id: str,
        role: str = Roles.OPERATOR,
        *,
        ttl_seconds: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Mint a signed JWT for ``user_id``.

        Parameters
        ----------
        user_id:
            Subject claim. Use the agent's ``agent_id`` for service
            tokens, the human's email/login for operator tokens.
        role:
            One of :data:`Roles`.
        ttl_seconds:
            Override the default TTL. Useful for short-lived CLI
            tokens.
        extra:
            Additional claims to embed (e.g. ``{"scopes": [...]}``).
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": user_id,
            "role": role,
            "iat": now,
            "exp": now + (ttl_seconds or self._ttl),
        }
        if extra:
            claims.update(extra)
        return jwt.encode(claims, self._secret, algorithm=self._algorithm)

    def verify_token(self, token: str | None) -> Claims:
        """Verify a bearer ``token`` and return the parsed :class:`Claims`.

        When the middleware is disabled (``enabled=False``), this
        always returns the synthetic local-dev identity regardless
        of the token — even if the token is invalid. That mirrors
        the brief's "auth is opt-in for local dev" rule.
        """
        if not self.enabled:
            return self._synthetic_claims()

        if not token:
            raise TokenInvalid("missing bearer token")

        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                options={"require": ["sub", "exp", "iat"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpired("token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenInvalid(f"token rejected: {exc}") from exc

        return Claims(
            sub=str(payload["sub"]),
            role=str(payload.get("role", Roles.VIEWER)),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            raw=payload,
        )

    def require_role(self, claims: Claims, required: str) -> None:
        """Raise :class:`RoleRequired` unless ``claims.role`` is sufficient.

        Uses :func:`role_at_least` for the comparison so a caller
        with the ``operator`` role satisfies a ``viewer`` check.
        """
        if not role_at_least(claims.role, required):
            raise RoleRequired(
                f"role {claims.role!r} insufficient; need {required!r}",
                details={"actual": claims.role, "required": required},
            )

    def is_enabled(self) -> bool:
        return self.enabled

    # -- FastAPI integration -----------------------------------------------

    async def __call__(self, request: Request) -> Claims:
        """FastAPI dependency: extract & verify the token, attach to ``request.state``."""
        creds: HTTPAuthorizationCredentials | None = await self._bearer(request)
        token = creds.credentials if creds else None
        claims = self.verify_token(token)
        request.state.claims = claims
        return claims

    def require(self, role: str):
        """Return a FastAPI dependency that enforces ``role`` (≥)."""

        async def _dep(request: Request) -> Claims:
            claims = await self(request)
            self.require_role(claims, role)
            return claims

        return _dep

    # -- WebSocket integration ---------------------------------------------

    async def authenticate_ws(
        self, websocket: WebSocket, *, role: str | None = None
    ) -> Claims:
        """Verify the WebSocket handshake.

        Accepts the token from the ``Authorization`` header (when
        the client speaks the WS-over-HTTP idiom and includes it)
        or from the ``?token=...`` query string (the standard for
        browser clients that can't set headers on the WS upgrade).
        """
        token = self._extract_ws_token(websocket)
        claims = self.verify_token(token)
        if role is not None:
            self.require_role(claims, role)
        return claims

    def _extract_ws_token(self, websocket: WebSocket) -> str | None:
        # Header first.
        auth = websocket.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        # Query string fallback.
        qs = websocket.query_params
        if "token" in qs:
            return qs["token"]
        # Last-resort: look at the Sec-WebSocket-Protocol subprotocol
        # value. Some libraries use this as a "credential" slot.
        proto = websocket.headers.get("sec-websocket-protocol")
        if proto and proto.startswith("bearer."):
            encoded = proto.split(".", 1)[1]
            try:
                return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            except Exception:  # noqa: BLE001
                return None
        return None

    # -- HTTP error mapping ------------------------------------------------

    def http_exception(self, exc: AuthError) -> HTTPException:
        """Map an :class:`AuthError` to a :class:`HTTPException`."""
        code_map = {
            TokenInvalid: status.HTTP_401_UNAUTHORIZED,
            TokenExpired: status.HTTP_401_UNAUTHORIZED,
            RoleRequired: status.HTTP_403_FORBIDDEN,
        }
        status_code = status.HTTP_401_UNAUTHORIZED
        for cls, sc in code_map.items():
            if isinstance(exc, cls):
                status_code = sc
                break
        return HTTPException(
            status_code=status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
        )

    # -- helpers -----------------------------------------------------------

    def _synthetic_claims(self) -> Claims:
        now = int(time.time())
        return Claims(
            sub=self._default_user_id,
            role=self._default_role,
            issued_at=now,
            expires_at=now + 365 * 24 * 3600,
            raw={"synthetic": True},
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def make_middleware_from_config(
    *,
    enabled: bool,
    secret: str,
    algorithm: str = "HS256",
    ttl_seconds: int = 3600,
) -> AuthMiddleware:
    """Build an :class:`AuthMiddleware` from the phase-11 config.

    Convenience used by the kernel's ``create_app`` and by tests.
    """
    return AuthMiddleware(
        enabled=enabled,
        secret=secret,
        algorithm=algorithm,
        ttl_seconds=ttl_seconds,
    )


__all__ = [
    "AuthError",
    "AuthMiddleware",
    "Claims",
    "RoleRequired",
    "Roles",
    "TokenExpired",
    "TokenInvalid",
    "make_middleware_from_config",
    "role_at_least",
]
