# SPDX-License-Identifier: Apache-2.0
"""Trusted-channel authority broker interface; principals are supplied by the caller boundary."""

from typing import Protocol

from creatidy_kernel.core.authority import AuthorityGrant, OperationIntent, Principal
from creatidy_kernel.core.domain import AttemptSpec


class AuthorityBroker(Protocol):
    """Real adapters must consume grants durably with Operation intent; the fake does not."""

    def issue(self, principal: Principal, grant: AuthorityGrant) -> AuthorityGrant: ...

    def revoke(self, principal: Principal, grant_id: str) -> AuthorityGrant: ...

    def check(
        self, principal: Principal, grant_id: str, attempt: AttemptSpec, intent: OperationIntent, now: int
    ) -> OperationIntent: ...
