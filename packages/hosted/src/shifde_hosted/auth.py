from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt

from shifde_hosted.errors import ServiceError


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    subject: str
    scopes: frozenset[str]


class JWTVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        keys: dict[str, Any] | None = None,
        jwks_url: str | None = None,
    ):
        if not issuer.startswith("https://"):
            raise ValueError("JWT issuer must use HTTPS")
        if bool(keys) == bool(jwks_url):
            raise ValueError("configure exactly one trusted key source")
        if jwks_url and not jwks_url.startswith("https://"):
            raise ValueError("JWKS must use HTTPS")
        self.issuer, self.audience, self.keys = issuer, audience, keys
        self.client = jwt.PyJWKClient(jwks_url, timeout=5) if jwks_url else None

    def verify(self, token: str) -> tuple[Principal, dict]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("invalid JWT header")
            if self.keys:
                key = self.keys[header["kid"]]
            else:
                assert self.client is not None
                key = self.client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["sub", "iss", "aud", "exp", "iat", "tenant_id"]},
            )
            subject, scope = claims["sub"], claims.get("scope", "")
            if (
                not isinstance(subject, str)
                or not subject
                or len(subject) > 200
                or not isinstance(scope, str)
            ):
                raise ValueError("invalid principal")
            principal = Principal(UUID(claims["tenant_id"]), subject, frozenset(scope.split()))
            return principal, claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise ServiceError("UNAUTHENTICATED", 401) from None
