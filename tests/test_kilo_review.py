# SPDX-License-Identifier: Apache-2.0
"""Offline native-wire fixtures, not a claim of a real model's semantic review.

The production HTTP client and durable dispatcher execute unchanged against a
deliberately synthetic local service. Native API/source provenance is separate
from fixture receipt assertions and from future live-runtime acceptance.
"""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from test_product_review import configure_budget, request

from creatidy_kernel.adapters.kilo_review import KILO_VERSION, NativeKiloReview, review_permissions
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, FinishAttempt, PrepareAttempt, StartAttempt
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.intake import encode
from creatidy_kernel.ports.verification import ProductReviewStatus, dispatch_product_review, product_review_operation_id


class NativeService:
    def __init__(self) -> None:
        self.created = 0
        self.prompts: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []
        self.metadata: dict[str, object] = {}
        self.version = KILO_VERSION
        self.lose_reply = False
        self.changed_input = False
        self.inherited = False
        self.verdict = "approve"
        self.created_at = int(time.time())


@pytest.fixture
def native() -> Iterator[tuple[NativeService, str]]:
    state = NativeService()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, value: object) -> None:
            raw = encode(value)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/global/health":
                self.reply({"healthy": True, "version": state.version})
            elif path == "/session/ses_fixture/message":
                self.reply(state.messages)
            elif path == "/session/ses_fixture":
                self.reply(state.metadata)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"])
            body = cast(dict[str, object], json.loads(self.rfile.read(size)))
            if urlsplit(self.path).path == "/session":
                state.created += 1
                state.metadata = {**body, "id": "ses_fixture"}
                if state.inherited:
                    state.metadata["parentID"] = "ses_author"
                self.reply(state.metadata)
                return
            assert urlsplit(self.path).path == "/session/ses_fixture/message"
            state.prompts.append(body)
            prompt_text = cast(str, cast(list[dict[str, object]], body["parts"])[0]["text"])
            schema = cast(dict[str, object], json.loads(prompt_text.split("no markdown or prose:\n", 1)[1]))
            properties = cast(dict[str, object], schema["properties"])
            digest = cast(dict[str, object], properties["request_digest"])["const"]
            user: dict[str, object] = {
                "info": {
                    "id": "msg_prompt",
                    "sessionID": "ses_fixture",
                    "role": "user",
                    "agent": body["agent"],
                },
                "parts": [{"type": "text", "text": "poisoned"}] if state.changed_input else body["parts"],
            }
            result: dict[str, object] = {
                "info": {
                    "id": "msg_review",
                    "sessionID": "ses_fixture",
                    "parentID": "msg_prompt",
                    "role": "assistant",
                    "time": {"completed": state.created_at * 1000},
                },
                "parts": [
                    {
                        "type": "text",
                        "text": json.dumps({"request_digest": digest, "verdict": state.verdict, "findings": []}),
                    }
                ],
            }
            state.messages.extend([user, result])
            if state.lose_reply:
                self.close_connection = True
                self.connection.shutdown(2)
                self.connection.close()
            else:
                self.reply(result)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def ready(store: SQLiteProgramStore) -> None:
    program = store.create(reference_spec(), "native-review-create")
    configure_budget(store, program)
    program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
    attempt = AttemptSpec(
        "a1", program.program_id, "first", program.spec.revision, program.spec.digest, program.resolved_inputs("first")
    )
    store.admit_with_intent(
        program.program_id,
        "prepare",
        PrepareAttempt(program.revision, "worker", attempt),
        "runtime:a1",
        "runtime:a1",
        {"attempt": attempt.digest},
    )
    program = store.load(program.program_id)
    program = store.admit(program.program_id, "start", StartAttempt(program.revision, "worker", "a1"))
    store.admit(program.program_id, "finish", FinishAttempt(program.revision, "worker", "a1"))


def test_real_http_adapter_receives_native_structured_review_without_author_conversation(
    tmp_path: Path, native: tuple[NativeService, str]
) -> None:
    service, origin = native
    with SQLiteProgramStore(tmp_path / "state.db") as store:
        ready(store)
        review = request()
        dispatcher = NativeKiloReview(
            store,
            origin,
            tmp_path,
            "product-review",
            lambda _: "Exact immutable diff and AC.",
            lambda: int(time.time()),
            freshness_seconds=60,
        )
        result = dispatch_product_review(store, review, dispatcher, now=int(time.time()))
        assert result.status is ProductReviewStatus.APPROVED
        assert result.receipt is not None and result.receipt.reviewer_session == "ses_fixture"
        assert service.created == 1 and len(service.prompts) == 1
        assert service.metadata["permission"] == review_permissions()
        assert "parentID" not in service.metadata
        assert store.find_artifact(result.operation.operation_id, "kilo-review-input") is not None
        repeated = dispatch_product_review(store, review, dispatcher, now=int(time.time()))
        assert repeated.receipt == result.receipt
        assert service.created == 1 and len(service.prompts) == 1


@pytest.mark.parametrize("mode", ["version", "inherited", "poisoned", "revoked", "lease"])
def test_unsupported_or_lost_authority_never_forges_independence_or_sends_an_unadmitted_prompt(
    tmp_path: Path, native: tuple[NativeService, str], mode: str
) -> None:
    service, origin = native
    service.version = "unsupported" if mode == "version" else KILO_VERSION
    service.inherited = mode == "inherited"
    service.changed_input = mode == "poisoned"
    with SQLiteProgramStore(tmp_path / "state.db") as store:
        ready(store)

        def credential() -> str:
            if mode == "revoked":
                store.revoke_work_budget("reference", "owner-revoked-before-send")
            if mode == "lease":
                operation = store.operation(product_review_operation_id(request()))
                # A reentrant trusted credential resolver can invalidate the
                # transport's once-current fence. It cannot authorize another send.
                store.observe(operation.operation_id, operation.fence, "uncertain-before-send", "unknown")
            return ""

        dispatcher = NativeKiloReview(
            store,
            origin,
            tmp_path,
            "product-review",
            lambda _: "Exact diff and AC.",
            lambda: int(time.time()),
            freshness_seconds=60,
            authorization=credential,
        )
        result = dispatch_product_review(store, request(), dispatcher, now=int(time.time()))
        assert result.status is ProductReviewStatus.UNKNOWN
        if mode in {"version", "revoked", "lease"}:
            assert service.created == 0 and not service.prompts
        if mode == "inherited":
            assert service.created == 1 and not service.prompts


def test_lost_native_reply_recovers_exact_retained_session_without_second_prompt(
    tmp_path: Path, native: tuple[NativeService, str]
) -> None:
    service, origin = native
    service.lose_reply = True
    with SQLiteProgramStore(tmp_path / "state.db") as store:
        ready(store)
        dispatcher = NativeKiloReview(
            store,
            origin,
            tmp_path,
            "product-review",
            lambda _: "Exact diff and AC.",
            lambda: int(time.time()),
            freshness_seconds=60,
        )
        result = dispatch_product_review(store, request(), dispatcher, now=int(time.time()), lease_seconds=60)
        assert result.status is ProductReviewStatus.UNKNOWN
        operation = result.operation
        # Read-only reconciliation does not require an elapsed wall-time sleep.
        lookup = dispatcher.reconcile(
            OperationKey(operation.operation_id, operation.effect_key, operation.request_digest),
            operation.request_digest,
        )
        assert lookup.receipt is not None and lookup.receipt.request_digest == request().input_digest
        assert service.created == 1 and len(service.prompts) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "https://service.invalid",
        "http://localhost:1234",
        "http://127.0.0.1:1/path",
        "http://user:secret@127.0.0.1:1",  # pragma: allowlist secret - synthetic rejection fixture
        "http://127.0.0.1:1?x=1",
    ],
)
def test_native_client_does_not_accept_ambient_or_remote_runtime_endpoints(tmp_path: Path, origin: str) -> None:
    with SQLiteProgramStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            NativeKiloReview(
                store, origin, tmp_path, "product-review", lambda _: "source", lambda: 100, freshness_seconds=60
            )
