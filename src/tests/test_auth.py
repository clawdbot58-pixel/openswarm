"""Tests for JWT authentication (Phase 11)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import jwt

from kernel.auth import (
    AuthError,
    AuthMiddleware,
    Claims,
    RoleRequired,
    Roles,
    TokenExpired,
    TokenInvalid,
    make_middleware_from_config,
    role_at_least,
)


@pytest.fixture
def middleware() -> AuthMiddleware:
    return AuthMiddleware(
        enabled=True,
        secret="test-secret",
        algorithm="HS256",
        ttl_seconds=3600,
        default_user_id="local-dev",
        default_role=Roles.OPERATOR,
    )


class TestRoles:
    def test_role_constants_defined(self) -> None:
        assert Roles.OPERATOR == "operator"
        assert Roles.ADMIN == "admin"
        assert Roles.VIEWER == "viewer"
        assert Roles.AGENT == "agent"
        assert Roles.EXTERNAL == "external"


class TestRoleAtLeast:
    def test_exact_match(self) -> None:
        assert role_at_least("operator", "operator") is True

    def test_higher_satisfies_lower(self) -> None:
        assert role_at_least("admin", "viewer") is True
        assert role_at_least("operator", "viewer") is True

    def test_lower_fails_higher(self) -> None:
        assert role_at_least("viewer", "admin") is False

    def test_unknown_role_defaults_to_zero(self) -> None:
        assert role_at_least("unknown", "viewer") is False
        assert role_at_least("viewer", "unknown") is True


class TestClaims:
    def test_is_expired(self) -> None:
        future = Claims(
            sub="user1",
            role="operator",
            issued_at=int(time.time()) - 100,
            expires_at=int(time.time()) + 3600,
            raw={},
        )
        assert future.is_expired() is False

    def test_is_expired_true(self) -> None:
        past = Claims(
            sub="user1",
            role="operator",
            issued_at=int(time.time()) - 4000,
            expires_at=int(time.time()) - 100,
            raw={},
        )
        assert past.is_expired() is True

    def test_to_dict(self) -> None:
        claims = Claims(
            sub="user1",
            role="admin",
            issued_at=1000,
            expires_at=2000,
            raw={"foo": "bar"},
        )
        d = claims.to_dict()
        assert d["sub"] == "user1"
        assert d["role"] == "admin"
        assert d["iat"] == 1000
        assert d["exp"] == 2000


class TestAuthMiddleware:
    def test_issue_token(self, middleware: AuthMiddleware) -> None:
        token = middleware.issue_token("user1", Roles.OPERATOR)
        assert isinstance(token, str)

        # Decode without verification to check claims.
        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["sub"] == "user1"
        assert payload["role"] == "operator"

    def test_issue_token_with_extra(self, middleware: AuthMiddleware) -> None:
        token = middleware.issue_token(
            "agent1",
            Roles.AGENT,
            extra={"scopes": ["read", "write"]},
        )
        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert "scopes" in payload
        assert payload["scopes"] == ["read", "write"]

    def test_verify_valid_token(self, middleware: AuthMiddleware) -> None:
        token = middleware.issue_token("user1", Roles.OPERATOR)
        claims = middleware.verify_token(token)
        assert claims.sub == "user1"
        assert claims.role == "operator"

    def test_verify_missing_token(self, middleware: AuthMiddleware) -> None:
        with pytest.raises(TokenInvalid, match="missing"):
            middleware.verify_token(None)

    def test_verify_invalid_token(self, middleware: AuthMiddleware) -> None:
        with pytest.raises(TokenInvalid):
            middleware.verify_token("not-a-valid-jwt")

    def test_verify_expired_token(self, middleware: AuthMiddleware) -> None:
        # Create an already-expired token.
        now = int(time.time())
        payload = {
            "sub": "user1",
            "role": Roles.OPERATOR,
            "iat": now - 7200,
            "exp": now - 3600,
        }
        token = jwt.encode(payload, "test-secret", algorithm="HS256")

        with pytest.raises(TokenExpired):
            middleware.verify_token(token)

    def test_disabled_middleware_returns_synthetic(self, middleware: AuthMiddleware) -> None:
        middleware.enabled = False
        claims = middleware.verify_token("any-token")
        assert claims.sub == "local-dev"
        assert claims.role == Roles.OPERATOR

    def test_require_role_passes(self, middleware: AuthMiddleware) -> None:
        claims = Claims(
            sub="admin",
            role=Roles.ADMIN,
            issued_at=0,
            expires_at=0,
            raw={},
        )
        middleware.require_role(claims, Roles.OPERATOR)

    def test_require_role_fails(self, middleware: AuthMiddleware) -> None:
        claims = Claims(
            sub="viewer",
            role=Roles.VIEWER,
            issued_at=0,
            expires_at=0,
            raw={},
        )
        with pytest.raises(RoleRequired) as exc:
            middleware.require_role(claims, Roles.ADMIN)
        assert "insufficient" in exc.value.message.lower()

    def test_is_enabled(self, middleware: AuthMiddleware) -> None:
        assert middleware.is_enabled() is True
        middleware.enabled = False
        assert middleware.is_enabled() is False


class TestAuthMiddlewareHttpException:
    def test_token_invalid_maps_to_401(self, middleware: AuthMiddleware) -> None:
        exc = TokenInvalid("bad token")
        http_exc = middleware.http_exception(exc)
        assert http_exc.status_code == 401

    def test_token_expired_maps_to_401(self, middleware: AuthMiddleware) -> None:
        exc = TokenExpired("expired")
        http_exc = middleware.http_exception(exc)
        assert http_exc.status_code == 401

    def test_role_required_maps_to_403(self, middleware: AuthMiddleware) -> None:
        exc = RoleRequired("role required")
        http_exc = middleware.http_exception(exc)
        assert http_exc.status_code == 403


class TestWebSocketAuth:
    def test_extract_token_from_header(self, middleware: AuthMiddleware) -> None:
        token = middleware.issue_token("user1", Roles.OPERATOR)

        ws = MagicMock()
        ws.headers = {"authorization": f"Bearer {token}"}
        ws.query_params = {}

        extracted = middleware._extract_ws_token(ws)
        assert extracted == token

    def test_extract_token_from_query(self, middleware: AuthMiddleware) -> None:
        token = middleware.issue_token("user1", Roles.OPERATOR)

        ws = MagicMock()
        ws.headers = {}
        ws.query_params = {"token": token}

        extracted = middleware._extract_ws_token(ws)
        assert extracted == token


class TestMakeMiddlewareFromConfig:
    def test_builds_correctly(self) -> None:
        mw = make_middleware_from_config(
            enabled=True,
            secret="my-secret",
            algorithm="HS256",
            ttl_seconds=1800,
        )
        assert mw.enabled is True
        assert mw._secret == "my-secret"
        assert mw._algorithm == "HS256"
        assert mw._ttl == 1800


class TestAuthError:
    def test_error_attributes(self) -> None:
        exc = AuthError("test error", details={"foo": "bar"})
        assert exc.message == "test error"
        assert exc.details == {"foo": "bar"}
        assert exc.code == "auth_error"

    def test_token_invalid_code(self) -> None:
        assert TokenInvalid("").code == "token_invalid"

    def test_token_expired_code(self) -> None:
        assert TokenExpired("").code == "token_expired"

    def test_role_required_code(self) -> None:
        assert RoleRequired("").code == "role_required"