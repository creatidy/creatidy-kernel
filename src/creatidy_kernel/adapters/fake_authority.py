# SPDX-License-Identifier: Apache-2.0
"""In-memory synthetic admission; not the durable Operation journal or isolation."""

from dataclasses import replace
from threading import RLock

from creatidy_kernel.core.authority import AuthorityDenied, AuthorityGrant, OperationIntent, Principal, check_scope
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.ports.authority import AuthorityBroker


class FakeAuthorityBroker(AuthorityBroker):
    def __init__(self) -> None:
        self._lock = RLock()
        self._grants: dict[str, AuthorityGrant] = {}
        self._intents: dict[str, tuple[str, OperationIntent]] = {}
        self._effects: dict[str, str] = {}
        self._consumed: dict[str, str] = {}

    def issue(self, principal: Principal, grant: AuthorityGrant) -> AuthorityGrant:
        if principal.role != "owner" or principal.actor_id != grant.issuer or grant.revoked:
            raise AuthorityDenied("only the issuing owner can grant authority")
        with self._lock:
            if grant.grant_id in self._grants:
                raise AuthorityDenied("grant identity already exists")
            self._grants[grant.grant_id] = grant
            return grant

    def revoke(self, principal: Principal, grant_id: str) -> AuthorityGrant:
        with self._lock:
            grant = self._grant(grant_id)
            if principal.role != "owner" or principal.actor_id != grant.issuer:
                raise AuthorityDenied("only the issuing owner can revoke authority")
            revoked = replace(grant, revoked=True)
            self._grants[grant_id] = revoked
            return revoked

    def check(
        self, principal: Principal, grant_id: str, attempt: AttemptSpec, intent: OperationIntent, now: int
    ) -> OperationIntent:
        with self._lock:
            grant = self._grant(grant_id)
            if principal.role != "worker" or principal.actor_id != attempt.attempt_id:
                raise AuthorityDenied("worker subject mismatch")
            # Even an exact replay must recheck revocation and expiry.
            check_scope(grant, attempt, intent, now)
            existing = self._intents.get(intent.operation_id)
            if existing is not None:
                if existing != (grant_id, intent):
                    raise AuthorityDenied("operation identity is bound to different input")
                return existing[1]
            if intent.effect_key in self._effects:
                raise AuthorityDenied("effect key already belongs to another operation")
            if grant.single_use and grant_id in self._consumed:
                raise AuthorityDenied("single-use grant consumed")
            self._intents[intent.operation_id] = (grant_id, intent)
            self._effects[intent.effect_key] = intent.operation_id
            if grant.single_use:
                self._consumed[grant_id] = intent.operation_id
            return intent

    def _grant(self, grant_id: str) -> AuthorityGrant:
        try:
            return self._grants[grant_id]
        except KeyError as exc:
            raise AuthorityDenied("unknown grant") from exc
