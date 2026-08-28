"""Who is acting, established from a credential rather than claimed in a header.

The header the demo used, `X-BioIntake-Actor: pi-kwame-osei`, is not authentication: it is a
request to be believed. Anyone who could reach the API could approve a temperature exception as the
principal investigator, and every audit line naming that actor would be worthless as evidence, which
matters here more than usual, because the audit trail is the product.

A lab user holds a bearer token. The server stores only its SHA-256, compares in constant time, and
cannot show anyone their token after it is issued. Tokens are minted out of band (see
`bootstrap_users`) and never appear in this repository.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from ..clock import Clock
from ..domain.enums import ActorRole
from ..domain.models import ActorContext, LabUser
from ..repositories.interfaces import Repository

TOKEN_BYTES = 32
TOKEN_PREFIX = "bit_"  # so a leaked token is recognisable in a log or a paste

# Long enough that a coordinator is not signing in every week, short enough that a credential
# nobody noticed was leaked stops working on its own.
DEFAULT_LIFETIME = timedelta(days=30)


class AuthenticationError(Exception):
    """No usable credential. Deliberately says nothing about which part failed."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


class AuthService:
    def __init__(self, repo: Repository, clock: Clock, lifetime: timedelta | None = None) -> None:
        self._repo = repo
        self._clock = clock
        self._lifetime = DEFAULT_LIFETIME if lifetime is None else lifetime

    def issue(
        self,
        user_id: str,
        display_name: str,
        role: ActorRole,
        token: str | None = None,
        lifetime: timedelta | None = None,
    ) -> str:
        """Create or re-key a user. Returns the token, which is not recoverable afterwards.

        Re-keying restarts the clock, which is the point: rotating a credential should give you a
        fresh one, not one that expires on the old schedule.
        """
        token = token or mint_token()
        existing = self._repo.get_user(user_id)
        lifetime = self._lifetime if lifetime is None else lifetime
        self._repo.save_user(
            LabUser(
                user_id=user_id,
                display_name=display_name,
                role=role,
                token_sha256=hash_token(token),
                active=True,
                created_at=existing.created_at if existing else self._clock(),
                expires_at=(self._clock() + lifetime) if lifetime else None,
            )
        )
        return token

    def revoke(self, user_id: str) -> None:
        user = self._repo.get_user(user_id)
        if user is not None:
            self._repo.save_user(user.model_copy(update={"active": False}))

    def authenticate(self, authorization: str | None) -> ActorContext:
        """Resolve an `Authorization: Bearer <token>` header to the actor it belongs to."""
        if not authorization:
            raise AuthenticationError("no credential supplied")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("credential is not a bearer token")
        digest = hash_token(token.strip())
        # A lab has staff, not users at internet scale, so a linear walk is honest and keeps the
        # token out of any index. Every candidate is compared in constant time regardless of match.
        found: LabUser | None = None
        for user in self._repo.list_users():
            if secrets.compare_digest(user.token_sha256, digest):
                found = user
        if found is None:
            raise AuthenticationError("credential is not recognised")
        if not found.active:
            raise AuthenticationError("credential has been revoked")
        if found.is_expired(self._clock()):
            raise AuthenticationError("credential has expired")
        return found.context()
