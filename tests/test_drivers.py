# SPDX-License-Identifier: Apache-2.0
"""Provider-shaped transport and shared application conformance, no live accounts."""

import copy
import json
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlunsplit

import pytest

from creatidy_kernel.adapters.driver_http import object_value
from creatidy_kernel.adapters.project_drivers import GitHubSCM, JiraWorkManagement, binding_from_mapping
from creatidy_kernel.adapters.sqlite_store import IdempotencyConflict, OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.domain import CancelProgram
from creatidy_kernel.core.drivers import (
    DriverDelivery,
    DriverMutation,
    DriverSubject,
    EdgeObligation,
    driver_bytes,
    driver_digest,
)
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import EffectStatus, Presence, UnsupportedForge
from creatidy_kernel.core.ownership import WorkClaim
from creatidy_kernel.core.work_budget import BudgetRefused
from creatidy_kernel.ports.drivers import DriverApplication
from creatidy_kernel.ports.program_store import OperationRecord
from tools.check_drivers import (
    BASE,
    HEAD,
    Clock,
    SyntheticServices,
    accepted,
    binding,
    configured,
    conformance,
    settlement_recovery,
)


@pytest.fixture
def services() -> Iterator[SyntheticServices]:
    service = SyntheticServices()
    try:
        yield service
    finally:
        service.close()


def review_mutation(scm: GitHubSCM) -> DriverMutation:
    return DriverMutation(
        DriverSubject(scm.binding, "change", "7", HEAD),
        "append_review",
        (("actor", "synthetic-kernel"), ("body", "Exact candidate receipt")),
        "owner:synthetic-effect-policy",
    )


def delivery(mutation: DriverMutation, *, attempts: int = 1) -> DriverDelivery:
    return DriverDelivery(OperationKey("operation", "effect", driver_digest(mutation.request())), mutation, 1, attempts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("family", "knowledge"),
        ("implementation", "other"),
        ("origin", "https://github.example.test:9443"),
        ("tenant", "other-tenant"),
        ("namespace", "other/service"),
        ("api_version", "2022-11-28"),
        ("rights", "other-rights"),
    ],
)
def test_subject_identity_qualifies_every_binding_dimension(field: str, value: str) -> None:
    original = binding("scm", "github", "acme/service", "2026-03-10")
    left = DriverSubject(original, "issue", "ENG-42/uuid", "jira-native-updated")
    right = replace(left, binding=replace(original, **{field: value}))
    assert left != right and left.reference != right.reference
    assert left != replace(left, kind="commit") and left != replace(left, revision=HEAD)
    assert left.key == "ENG-42/uuid"
    assert DriverSubject.parse(left.reference) == left
    with pytest.raises(ValueError):
        DriverSubject.parse(left.reference.replace(":v1:", ":v2:"))
    with pytest.raises(ValueError):
        DriverSubject.parse("forgejo:acme/service#42")


@pytest.mark.parametrize(
    "change", [{"version": "2"}, {"grant": "owner"}, {"trust": "sandbox"}, {"mutation_token": "forbidden"}]
)
def test_configuration_unknown_versions_and_authority_fields_refuse(change: dict[str, str]) -> None:
    value = {**asdict(binding("work", "jira-cloud", "ENG", "3")), **change}
    with pytest.raises(ValueError):
        binding_from_mapping(value)


@pytest.mark.parametrize(
    "origin",
    [
        "http://host.test",
        urlunsplit(("https", "synthetic-user:synthetic-password@host.test", "", "", "")),
        "https://host.test/path",
        "https://host.test?x=y",
    ],
)
def test_binding_never_infers_credential_or_origin_authority(origin: str) -> None:
    with pytest.raises(ValueError):
        replace(binding("work", "jira-cloud", "ENG", "3"), origin=origin)


def test_read_only_driver_never_resolves_mutation_credentials(services: SyntheticServices) -> None:
    project, scm, _, transports = configured(services, Clock())

    def forbidden() -> str:
        raise AssertionError("write credential resolved by read")

    for transport in transports:
        transport.write_authorization = forbidden
    assert project.work.work("uuid-42").fact.subject.key == "ENG-42"
    assert scm.repository().presence is Presence.FOUND
    assert scm.ref("feature").subject.revision == HEAD
    assert scm.commit(HEAD).subject.revision == HEAD
    assert scm.change("7").related[-1].revision == BASE
    assert scm.reviews("7").complete
    review = scm.reviews("7").items[0]
    assert review.subject.revision is None and review.related == (DriverSubject(scm.binding, "commit", HEAD, HEAD),)
    assert len(review.observation_digest) == 64
    assert all(item[0] == "GET" and item[3] == "Bearer synthetic-read" for item in services.requests)
    transports[1].read_authorization = None
    assert scm.repository().presence is Presence.FOUND and services.requests[-1][3] is None


@pytest.mark.parametrize(
    "path",
    [
        "https://other.test/repos/acme/service",
        "//other.test/repos/acme/service",
        "/repos/acme/serviceevil",
        "/repos/acme/service/../secret",
        "/repos/acme/service/%2e%2e/secret",
        "/repos/acme/service/a\\secret",
        "/repos/acme/service#fragment",
        "/repos/acme/service/with space",
    ],
)
def test_namespace_and_url_refusals_precede_authentication(services: SyntheticServices, path: str) -> None:
    _, _, _, transports = configured(services, Clock())
    transport = transports[1]

    def forbidden() -> str:
        raise AssertionError("credential resolved for untrusted target")

    transport.read_authorization = forbidden
    with pytest.raises(ValueError):
        transport.request("GET", path)
    assert not services.requests


def test_redirect_is_not_followed_and_oversized_native_response_is_unknown(services: SyntheticServices) -> None:
    _, scm, _, transports = configured(services, Clock())
    services.filter = lambda method, path, body: (302, {})
    assert scm.repository().presence is Presence.UNKNOWN
    assert len(services.requests) == 1
    services.filter = lambda method, path, body: (200, {"full_name": "acme/service", "oversized": "x" * 300})
    transports[1].max_bytes = 100
    assert scm.repository().presence is Presence.UNKNOWN


def test_http_refuses_unsupported_total_deadline_thread_before_credentials(services: SyntheticServices) -> None:
    _, _, _, transports = configured(services, Clock())
    transports[1].read_authorization = lambda: pytest.fail("unsupported execution thread resolved credential")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(UnsupportedForge):
            executor.submit(transports[1].request, "GET", "/repos/acme/service").result()
    assert not services.requests


def test_jira_native_dependencies_are_not_a_complete_or_accepted_kernel_graph(services: SyntheticServices) -> None:
    project, _, _, _ = configured(services, Clock())
    observed = project.work.work("ENG-42")
    assert observed.fact.presence is Presence.FOUND and observed.fact.complete
    assert not observed.dependencies_complete
    edge = observed.dependencies[0]
    assert (edge.identity, edge.type_id, edge.native_type, edge.direction, edge.target.key) == (
        "link-9",
        "10000",
        "Blocks",
        "is blocked by",
        "ENG-41",
    )
    assert b"Closed" not in observed.fact.native  # It is attributed target data, not acceptance.
    obligation = EdgeObligation(
        observed.fact.subject,
        driver_digest([asdict(item) for item in observed.dependencies]),
        "owner:approved-explicit-edge-cut",
        130,
    )
    obligation.require(observed, 100)
    with pytest.raises(ValueError):
        replace(obligation, edges_digest="different").require(observed, 100)
    with pytest.raises(ValueError):
        obligation.require(observed, 131)


@pytest.mark.parametrize("mode", ["permission404", "missinglinks", "moving", "missingpage", "capped"])
def test_jira_incomplete_changed_or_hidden_edges_are_not_absence(services: SyntheticServices, mode: str) -> None:
    project, _, _, transports = configured(services, Clock())
    reads = 0

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        nonlocal reads
        if "/issue/ENG-42?" not in path:
            if mode == "missingpage" and "/issue/uuid-41?" in path:
                return 404, {}
            return None
        if mode == "permission404":
            return 404, {}
        status, raw = services.route(method, path, body)
        data = copy.deepcopy(object_value(raw))
        fields = object_value(data["fields"])
        if mode == "missinglinks":
            fields.pop("issuelinks")
        if mode == "moving":
            reads += 1
            fields["updated"] = f"native-revision-{reads}"
        if mode == "capped":
            fields["issuelinks"] = [*cast_list(fields["issuelinks"]), *cast_list(fields["issuelinks"])]
        return status, data

    services.filter = changed
    work = JiraWorkManagement(transports[0], Clock().wall, max_edges=1) if mode == "capped" else project.work
    result = work.work("ENG-42")
    assert result.fact.presence is not Presence.FOUND and not result.dependencies_complete
    assert not result.fact.complete


@pytest.mark.parametrize("mode", ["cap", "page404", "duplicate", "movinghead", "permission", "movingreview"])
def test_github_pages_and_repeated_moving_scans_do_not_prove_absence(services: SyntheticServices, mode: str) -> None:
    _, scm, _, transports = configured(services, Clock())
    if mode == "cap":
        scm = GitHubSCM(transports[1], Clock().wall, max_pages=1)
    count = 0

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        nonlocal count
        if "/reviews?" not in path:
            return None
        count += 1
        if mode == "permission":
            return 403, {}
        if "page=2" in path and mode == "page404":
            return 404, {}
        if "page=2" in path and mode == "duplicate":
            return 200, services.reviews
        if mode == "movinghead":
            services.head = "c" * 40
        if mode == "movingreview" and count >= 3:
            return 200, [{**services.reviews[0], "state": "DISMISSED"}] if "page=1" in path else []
        return None

    services.filter = changed
    scan = scm.reviews("7")
    assert not scan.complete and not scan.stable


def test_github_append_is_exact_commit_creation_not_claimed_cas(services: SyntheticServices) -> None:
    _, scm, _, _ = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    before: list[str] = []
    result = scm.apply(intent, lambda: before.append("fresh-authority"))
    assert result.status is EffectStatus.ACCEPTED and before == ["fresh-authority"]
    native = object_value(json.loads(next(item[2] for item in services.requests if item[0] == "POST")))
    assert native["commit_id"] == HEAD and native["event"] == "COMMENT" and "forge-effect:" in str(native["body"])
    assert "not-current-head-CAS" in result.reason
    calls = sum(item[0] == "POST" for item in services.requests)
    services.head = "c" * 40
    historical = scm.apply(replace(intent, attempts=2), lambda: pytest.fail("duplicate mutation"))
    assert historical.status is EffectStatus.ACCEPTED
    assert sum(item[0] == "POST" for item in services.requests) == calls


def test_github_append_head_race_still_targets_only_explicit_approved_commit(services: SyntheticServices) -> None:
    _, scm, _, transports = configured(services, Clock())

    def credential() -> str:
        services.head = "c" * 40
        return "Bearer synthetic-write"

    transports[1].write_authorization = credential
    receipt = scm.apply(delivery(review_mutation(scm)), lambda: None)
    assert receipt.status is EffectStatus.ACCEPTED and services.head != HEAD
    assert services.reviews[-1]["commit_id"] == HEAD  # Append receipt does NOT assert unchanged current PR head.


@pytest.mark.parametrize("action", ["create_pr", "update_pr", "merge", "append_review_if_head_unchanged"])
def test_unsupported_github_conditional_mutation_refuses_before_write_credentials(
    services: SyntheticServices, action: str
) -> None:
    _, scm, _, transports = configured(services, Clock())
    transports[1].write_authorization = lambda: pytest.fail("unsupported mutation resolved credentials")
    with pytest.raises(UnsupportedForge):
        scm.apply(delivery(replace(review_mutation(scm), action=action)), lambda: None)
    assert not services.requests


def test_native_head_drift_refuses_send_and_lost_post_is_not_retried(services: SyntheticServices) -> None:
    _, scm, _, _ = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    services.head = "c" * 40
    assert scm.apply(intent, lambda: None).status is EffectStatus.STALE
    assert all(item[0] == "GET" for item in services.requests)
    services.head = HEAD
    services.lost.add("POST")
    assert scm.apply(intent, lambda: None).status is EffectStatus.UNKNOWN
    assert (
        scm.apply(replace(intent, attempts=2), lambda: pytest.fail("lost reply retried")).status
        is EffectStatus.ACCEPTED
    )
    assert sum(item[0] == "POST" for item in services.requests) == 1


def test_empty_review_search_and_duplicate_marker_are_unknown_not_retry_permission(services: SyntheticServices) -> None:
    _, scm, _, _ = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    assert scm.reconcile(intent).status is EffectStatus.UNKNOWN
    assert scm.apply(intent, lambda: None).status is EffectStatus.ACCEPTED
    services.reviews.append({**services.reviews[-1], "id": 99})
    assert scm.reconcile(intent).status is EffectStatus.UNKNOWN


def test_native_marker_from_another_principal_is_not_kernel_receipt(services: SyntheticServices) -> None:
    _, scm, knowledge, transports = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    assert scm.apply(intent, lambda: None).status is EffectStatus.ACCEPTED
    services.reviews[-1]["user"] = {"login": "untrusted-reviewer"}
    assert scm.reconcile(intent).status is EffectStatus.UNKNOWN
    transports[1].write_authorization = None
    services.reviews[-1]["user"] = {"login": "synthetic-kernel"}
    assert scm.reconcile(intent).status is EffectStatus.ACCEPTED  # Reconciliation needs no write secret.
    mutation = DriverMutation(
        DriverSubject(knowledge.binding, "page", "900", "19"),
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
        "owner:policy",
    )
    page = delivery(mutation)
    assert knowledge.apply(page, lambda: None).status is EffectStatus.ACCEPTED
    object_value(services.pages[20]["version"])["authorId"] = "untrusted-user"
    assert knowledge.reconcile(page).status is EffectStatus.UNKNOWN


@pytest.mark.parametrize("mode", ["wrongsha", "wrongremote", "duplicate", "parameters", "unknownresult"])
def test_jenkins_requires_actual_checkout_evidence_not_parameters_or_job_name(
    services: SyntheticServices, mode: str
) -> None:
    project, scm, _, _ = configured(services, Clock())

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        if "/api/json?" not in path:
            return None
        status, raw = services.route(method, path, body)
        data = copy.deepcopy(object_value(raw))
        actions = [object_value(value) for value in cast_list(data["actions"])]
        if mode == "wrongsha":
            actions[0]["lastBuiltRevision"] = {"SHA1": "c" * 40}
        if mode == "wrongremote":
            actions[0]["remoteUrls"] = ["https://other.test/acme/service.git"]
        if mode == "duplicate":
            actions.append(copy.deepcopy(actions[0]))
        if mode == "parameters":
            actions = [
                {"_class": "hudson.model.ParametersAction", "parameters": [{"name": "GIT_COMMIT", "value": HEAD}]}
            ]
        if mode == "unknownresult":
            data["result"] = "CUSTOM_PASS"
        data["actions"] = actions
        return status, data

    services.filter = changed
    source = DriverSubject(scm.binding, "commit", HEAD, HEAD)
    assert project.ci.build("12", source).presence is not Presence.FOUND
    with pytest.raises(ValueError):
        project.ci.build("12", replace(source, binding=replace(scm.binding, tenant="other")))


def cast_list(value: object) -> list[object]:
    from creatidy_kernel.adapters.driver_http import list_value

    return list_value(value)


def test_confluence_pinned_content_and_server_version_conflict(services: SyntheticServices) -> None:
    _, _, knowledge, _ = configured(services, Clock())
    before = knowledge.content("900", "19")
    assert before.presence is Presence.FOUND and before.subject.revision == "19"
    assert any("version=19" in item[1] for item in services.requests)
    mutation = DriverMutation(
        before.subject,
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
        "owner:policy",
    )
    intent = delivery(mutation)
    assert knowledge.apply(intent, lambda: None).status is EffectStatus.ACCEPTED
    other = delivery(
        replace(
            mutation, fields=(("actor", "synthetic-confluence-writer"), ("body", "<p>other</p>"), ("title", "Guide"))
        )
    )
    assert knowledge.apply(other, lambda: None).status is EffectStatus.STALE
    assert services.version == 20
    assert knowledge.reconcile(intent).status is EffectStatus.ACCEPTED
    assert knowledge.content("900", "19").native == before.native


def test_confluence_native_condition_handles_race_after_local_preflight(services: SyntheticServices) -> None:
    _, _, knowledge, transports = configured(services, Clock())
    mutation = DriverMutation(
        DriverSubject(knowledge.binding, "page", "900", "19"),
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>ours</p>"), ("title", "Guide")),
        "owner:policy",
    )

    def credential() -> str:
        services.version = 20
        services.pages[20] = services.page(20, "Another owner", "<p>theirs</p>", "independent")
        return "Bearer synthetic-write"

    transports[3].write_authorization = credential
    assert knowledge.apply(delivery(mutation), lambda: None).status is EffectStatus.STALE
    assert services.version == 20 and services.pages[20]["title"] == "Another owner"


def test_confluence_valid_response_without_version_visibility_is_unknown(services: SyntheticServices) -> None:
    _, _, knowledge, _ = configured(services, Clock())
    mutation = DriverMutation(
        DriverSubject(knowledge.binding, "page", "900", "19"),
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
        "owner:policy",
    )

    def hidden(method: str, path: str, body: object) -> tuple[int, object] | None:
        return (404, {}) if "version=20" in path else None

    services.filter = hidden
    intent = delivery(mutation)
    assert knowledge.apply(intent, lambda: None).status is EffectStatus.UNKNOWN and services.version == 20
    assert (
        knowledge.apply(replace(intent, attempts=2), lambda: pytest.fail("PUT retried")).status is EffectStatus.UNKNOWN
    )
    assert sum(item[0] == "PUT" for item in services.requests) == 1


@pytest.mark.parametrize("mode", ["withdrawn", "space", "version", "body", "moving"])
def test_confluence_permission_version_and_freshness_refusals(services: SyntheticServices, mode: str) -> None:
    _, _, knowledge, _ = configured(services, Clock())
    reads = 0

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        nonlocal reads
        if not path.startswith("/wiki/"):
            return None
        reads += 1
        status, raw = services.route(method, path, body)
        data = copy.deepcopy(object_value(raw))
        if mode == "withdrawn":
            data["status"] = "trashed"
        if mode == "space":
            data["spaceId"] = "other"
        if mode == "version" and "version=" in path:
            data["version"] = {"number": 99}
        if mode == "body":
            data.pop("body")
        if mode == "moving" and reads >= 3:
            data["title"] = "changed"
        return status, data

    services.filter = changed
    assert knowledge.content("900", "19").presence is not Presence.FOUND


def state_directory() -> tempfile.TemporaryDirectory[str]:
    import os

    return tempfile.TemporaryDirectory(dir=os.environ.get("CREATIDY_TEST_STORAGE_DIR"))


@pytest.mark.parametrize(
    "cut", ["credential-stop", "read-stop", "credential-expiry", "credential-lease", "policy-revoke"]
)
def test_application_rechecks_real_claim_authority_after_reads_and_credentials(
    services: SyntheticServices, cut: str
) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        policy = [True]
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, lambda original, mutation: policy[0]
        )
        mutation = review_mutation(scm)

        def revoke() -> str:
            if cut in {"credential-stop", "read-stop"}:
                program = store.load(claim.attempt.program_id)
                store.admit(program.program_id, "cancel", CancelProgram(program.revision, "owner"))
            elif cut == "credential-expiry":
                clock.now = 1001
            elif cut == "credential-lease":
                clock.now = 161
            else:
                policy[0] = False
            return "Bearer synthetic-write"

        if cut == "read-stop":

            def after(method: str, path: str) -> None:
                if path.endswith("/pulls/7"):
                    services.client_after = None
                    revoke()

            services.client_after = after
        else:
            transports[1].write_authorization = revoke
        with pytest.raises((ValueError, BudgetRefused, OperationConflict)):
            app.synchronize(claim, "review", "effect", mutation)
        assert not any(item[0] == "POST" for item in services.requests)
        assert store.operation("review").status == "unknown"


def test_actual_outbox_fence_is_not_a_worker_supplied_integer(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        request = {
            **mutation.request(),
            "attempt": claim.attempt.digest,
            "claim": claim.command_key,
            "generation": claim.generation,
        }
        owner.authorize_publication(claim, "review", "effect", request)
        with pytest.raises(OperationConflict):
            store.check_driver_delivery("review", 1, "forged")
        assert not services.requests
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, lambda original, value: value == mutation
        )
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.ACCEPTED
        with pytest.raises((ValueError, OperationConflict, IdempotencyConflict)):
            app.synchronize(claim, "review", "effect", replace(mutation, authority_reference="worker:forged"))


def test_exact_intent_and_actual_delivery_claim_are_durable_before_native_send(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        observed: list[int] = []

        def credential() -> str:
            operation = store.operation("review")
            request = object_value(object_value(json.loads(operation.request_json))["request"])
            assert operation.status == "dispatched" and operation.attempts == 1 and operation.fence > 0
            assert request["schema"] == "kernel-driver-effect-v1" and request["target"] == asdict(mutation.target)
            assert request["attempt"] == claim.attempt.digest and request["generation"] == claim.generation
            assert request["authority_reference"] == mutation.authority_reference
            assert not any(item[0] == "POST" for item in services.requests)
            observed.append(operation.fence)
            return "Bearer synthetic-write"

        transports[1].write_authorization = credential
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, lambda original, value: value == mutation
        )
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.ACCEPTED
        assert observed == [store.operation("review").fence]


def test_original_receipt_replay_is_history_not_fresh_authority(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, lambda original, value: value == mutation
        )
        receipt = app.synchronize(claim, "review", "effect", mutation)
        calls = len(services.requests)
        clock.now = 1001
        for transport in transports:
            transport.read_authorization = transport.write_authorization = lambda: pytest.fail(
                "history resolved a credential"
            )
        assert app.synchronize(claim, "review", "effect", mutation) == receipt
        assert len(services.requests) == calls and store.operation("review").attempts == 1
        with pytest.raises((BudgetRefused, ValueError)):
            app.synchronize(claim, "new:review", "new:effect", mutation)


def test_installed_recipe_runs_same_real_transports_and_shared_handlers() -> None:
    with state_directory() as directory:
        conformance(Path(directory))


@pytest.mark.parametrize("provider", ["github", "confluence"])
@pytest.mark.parametrize("cut", ["owner-capture", "store-cut"])
def test_effect_grant_is_rechecked_after_final_workspace_capture(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    cut: str,
) -> None:
    from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace

    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = (
            review_mutation(scm)
            if provider == "github"
            else DriverMutation(
                DriverSubject(knowledge.binding, "page", "900", "19"),
                "replace_page",
                (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
                "owner:policy",
            )
        )
        policy = [True]
        captures: list[int] = []
        observations = 0
        observing_final = False
        capture = LocalOwnedWorkspace.capture
        observe = owner.observe
        delivery_cut = store.check_driver_delivery

        def observed(original: WorkClaim) -> str:
            nonlocal observations, observing_final
            observations += 1
            observing_final = observations == 3
            try:
                return observe(original)
            finally:
                observing_final = False

        def captured(workspace: LocalOwnedWorkspace) -> tuple[str, bytes]:
            result = capture(workspace)
            if observing_final and cut == "owner-capture":
                captures.append(observations)
                policy[0] = False
            return result

        def checked(operation_id: str, fence: int, request_digest: str) -> None:
            delivery_cut(operation_id, fence, request_digest)
            if cut == "store-cut":
                policy[0] = False

        monkeypatch.setattr(owner, "observe", observed)
        monkeypatch.setattr(LocalOwnedWorkspace, "capture", captured)
        monkeypatch.setattr(store, "check_driver_delivery", checked)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, value: policy[0])
        with pytest.raises(ValueError, match="authority revoked"):
            app.synchronize(claim, "review", "effect", mutation)
        assert observations == 3 and (captures or cut == "store-cut")
        assert not any(method in {"POST", "PUT"} for method, _, _, _ in services.requests)
        original = store.operation("review")
        assert original.status == "unknown" and original.attempts == 1
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.UNKNOWN
        assert store.operation("review") == original


@pytest.mark.parametrize("rejected", [False, True], ids=["github-accepted", "confluence-409-rejected"])
def test_terminal_receipt_repairs_exact_settlement_after_crash_reopen(rejected: bool) -> None:
    with state_directory() as directory:
        settlement_recovery(Path(directory), rejected=rejected)


@pytest.mark.parametrize(
    "mismatch", ["unknown", "stale", "rejected", "key", "reference", "noncanonical", "extra-field", "cancelled"]
)
def test_cached_terminal_receipt_must_match_exact_operation_and_canonical_outcome(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, value: True)
        receipt = app.synchronize(claim, "review", "effect", mutation)
        value = asdict(receipt)
        if mismatch in {"unknown", "stale", "rejected"}:
            value["status"] = mismatch
        elif mismatch == "key":
            value["operation"] = {**asdict(receipt.operation), "request_digest": "different-exact-input"}
        elif mismatch == "reference":
            value["reference"] = "different-native-review"
        elif mismatch == "extra-field":
            value["grant"] = "not-a-receipt-field"
        cached = json.dumps(value, indent=2).encode() if mismatch == "noncanonical" else driver_bytes(value)
        calls = len(services.requests)
        operation = store.operation("review")
        native_operation = store.operation

        if mismatch == "cancelled":

            def cancelled_operation(operation_id: str) -> OperationRecord:
                return replace(native_operation(operation_id), status="cancelled")

            monkeypatch.setattr(store, "operation", cancelled_operation)

        def cached_artifact(operation_id: str, name: str) -> bytes:
            return cached

        def no_settlement(operation_id: str, evidence: str, *, expected_fence: int | None = None) -> None:
            pytest.fail("mismatched receipt settled")

        monkeypatch.setattr(store, "find_artifact", cached_artifact)
        monkeypatch.setattr(store, "record_work_settlement", no_settlement)
        if mismatch == "cancelled":
            assert app.synchronize(claim, "review", "effect", mutation) == receipt
        else:
            with pytest.raises(ValueError, match="receipt"):
                app.synchronize(claim, "review", "effect", mutation)
        assert len(services.requests) == calls and native_operation("review") == operation


def test_nonterminal_cached_receipt_still_finishes_original_outcome_without_resend(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, value: True)

        def crash(*args: object, **kwargs: object) -> None:
            raise RuntimeError("crash after artifact before terminal observation")

        with monkeypatch.context() as patch:
            patch.setattr(store, "observe", crash)
            with pytest.raises(RuntimeError, match="before terminal observation"):
                app.synchronize(claim, "review", "effect", mutation)
        operation = store.operation("review")
        cached = store.find_artifact("review", "driver-receipt")
        assert operation.status == "dispatched" and cached is not None
        calls = len(services.requests)
        clock.now = 1001
        for transport in transports:
            transport.read_authorization = transport.write_authorization = lambda: pytest.fail(
                "cache replay contacted provider"
            )

        def no_new_grant(original: WorkClaim, value: DriverMutation) -> bool:
            pytest.fail("cache replay requested new grant")

        app.authorize = no_new_grant
        receipt = app.synchronize(claim, "review", "effect", mutation)
        assert receipt.status is EffectStatus.ACCEPTED and driver_bytes(asdict(receipt)) == cached
        recovered = store.operation("review")
        assert recovered.status == "accepted" and (recovered.fence, recovered.attempts) == (
            operation.fence,
            operation.attempts,
        )
        owner.release(claim, evidence="original-nonterminal-receipt-recovery")
        assert len(services.requests) == calls
