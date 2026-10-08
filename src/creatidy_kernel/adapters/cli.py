# SPDX-License-Identifier: Apache-2.0
"""Thin local command interface to the bounded reference and task applications.

The public operator surface is self-contained: a user-local profile, deterministic
Codex discovery, a controller-owned source cache, and user-local state defaults
replace the former private operator wrappers. Secrets stay in the environment or
profile; they are never printed or serialized into evidence.
"""

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.operator_profile import (
    configuration_fields,
    default_profile_path,
    load_profile,
    profile_template,
    unknown_fields,
)
from creatidy_kernel.adapters.reference import export_reference, run_reference
from creatidy_kernel.adapters.task_execution import (
    TASKS,
    TaskRuntimeConfig,
    compose_task_live,
    default_state_directory,
    environment_with_discovered_codex,
    export_task,
    preflight_task,
    resolve_source,
    run_task,
    task_paths,
    task_status,
    validate_task_source,
)

_DOCTOR_HINTS: dict[str, str | dict[str | None, str]] = {
    "configuration_invalid": (
        "supply runtime configuration via the environment or --profile; inspect it with "
        "`creatidy-kernel config validate` and start from `creatidy-kernel config template`"
    ),
    "state_not_ready": (
        "select an empty directory on a supported native filesystem via --data-dir or CREATIDY_KERNEL_STATE_DIRECTORY"
    ),
    "task_baseline_invalid": (
        "the source at the exact base no longer matches the frozen task baseline; refresh the source or re-pin the base"
    ),
    "forge_not_ready": (
        "check read access for CREATIDY_KERNEL_FORGE_API, CREATIDY_KERNEL_FORGE_REMOTE and CREATIDY_KERNEL_FORGE_TOKEN"
    ),
    "source_not_ready": {
        "clone_failed": (
            "source acquisition from the canonical task repository failed; check remote "
            "reachability and credentials (an explicit --repo checkout remains an advanced override)"
        ),
        "fetch_failed": "refreshing the controller source cache failed; check remote reachability",
        "remote_mismatch": "the source origin must be exactly the task's canonical repository URL",
        "unsuitable_cache": (
            "preserve the refused cache; select a separate supported cache root or inspect ownership offline"
        ),
        "ownership_unproven": "preserve the entry and lock metadata; do not delete or relabel unknown data",
        "source_busy": (
            "another acquisition or object-copy lease owns this source; retry after that bounded operation settles"
        ),
        "checkout_unsuitable": "an explicit --repo override must be an ordinary Git checkout",
        "checkout_dirty": "commit or revert local changes in the explicit --repo override checkout",
        "base_unavailable": "the task base branch must be resolvable in the source",
        None: "source readiness failed; see the reported blocker category",
    },
    "codex_not_ready": {
        "not_found": (
            "install a supported `codex` on PATH, or set CREATIDY_KERNEL_CODEX_BIN and "
            "CREATIDY_KERNEL_CODEX_VERSION explicitly"
        ),
        "probe_failed": "the resolved Codex binary did not answer `--version`; check the installation",
        "unsupported_version": "the resolved Codex version is not a supported released version",
        None: "Codex readiness failed; see the reported blocker category",
    },
    "router_not_ready": {
        "endpoint_unreachable": "the Scarcity Router origin is unreachable; check CREATIDY_KERNEL_ROUTER_URL",
        "http_rejected": "the Router refused the request over HTTP; check the origin and bearer credential",
        "invalid_response": "the Router response was malformed, oversized or violated its schema",
        "no_eligible_selection": "the Router returned no eligible candidate; Kernel never falls back locally",
        "selection_incompatible": (
            "align CREATIDY_KERNEL_RUNTIME_BINDING with a Router-eligible provider/model[/effort]"
        ),
        "request_unsupported": "the controller allocation request is unsupported by the Router adapter",
        None: "Router readiness failed; see the reported blocker category",
    },
}


def _hint(reason: str, category: str | None) -> str:
    entry = _DOCTOR_HINTS.get(reason)
    if isinstance(entry, dict):
        return entry.get(category, entry[None])
    return entry or "remediate the reported blocker and retry"


def _print_hints(payload: dict[str, object]) -> None:
    for blocker in cast(list[dict[str, object]], payload["blockers"]):
        reason = cast(str, blocker["reason"])
        category = cast("str | None", blocker.get("category"))
        suffix = f" ({category})" if category else ""
        print(f"doctor: hint for {reason}{suffix}: {_hint(reason, category)}")


def _effective_environment(args: argparse.Namespace) -> dict[str, str]:
    """Explicit operator profile values override the ambient process environment."""
    environment = dict(os.environ)
    if args.profile is not None:
        environment.update(load_profile(args.profile, sops=args.sops))
    return environment


def _config(args: argparse.Namespace) -> int:
    if args.config_command == "template":
        print(profile_template())
        return 0
    if args.config_profile is not None:
        values = load_profile(args.config_profile, sops=args.config_sops)
        origin = str(args.config_profile)
    else:
        values = dict(os.environ)
        origin = "process environment"
    enriched, discovery_failure = environment_with_discovered_codex(values)
    fields = configuration_fields(enriched)
    unknown = unknown_fields(values)
    valid = not fields and not unknown
    if args.json:
        codex = (
            {"status": discovery_failure.category.value} if discovery_failure is not None else {"status": "configured"}
        )
        print(
            json.dumps(
                {
                    "schema": 1,
                    "source": origin,
                    "valid": valid,
                    "fields": list(fields),
                    "unknown_fields": list(unknown),
                    "codex": codex,
                },
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
        )
    else:
        print(f"kernel configuration: {origin}")
        print("kernel configuration: valid" if valid else "kernel configuration: needs attention")
        if fields:
            print("kernel configuration: missing or invalid fields: " + ", ".join(fields))
        if unknown:
            print("kernel configuration: unknown fields: " + ", ".join(unknown))
        if discovery_failure is not None:
            print(f"kernel configuration: codex: {discovery_failure.category.value}")
        else:
            print("kernel configuration: codex: configured")
        print(f"kernel configuration: default profile location: {default_profile_path(values)}")
    return 0 if valid else 1


def _doctor(args: argparse.Namespace, environment: dict[str, str]) -> int:
    enriched, _discovery_failure = environment_with_discovered_codex(environment)
    task = TASKS[args.task]()
    readiness = preflight_task(enriched, task=task, directory=args.data_dir, source_repository=args.repo)
    payload = readiness.payload()
    if args.json:
        print(
            json.dumps(
                {"schema": 1, "readiness": payload, "doctor": task.task_id},
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
        )
    else:
        print(f"doctor: task {task.task_id}")
        evidence = cast("dict[str, object]", payload["evidence"])
        source_evidence = cast("dict[str, object] | None", evidence.get("source"))
        if source_evidence is not None:
            mode = "acquired cache" if source_evidence.get("acquired") else "explicit checkout"
            print(f"doctor: source: {source_evidence.get('directory')} ({mode}, read-only)")
        codex_evidence = cast("dict[str, object] | None", evidence.get("codex"))
        if codex_evidence is not None:
            print(f"doctor: codex: {codex_evidence.get('version')} (schema ok, authentication not attested)")
        for blocker in readiness.blockers:
            suffix = f" ({blocker.category})" if blocker.category else ""
            fields = ": " + ", ".join(blocker.fields) if blocker.fields else ""
            print(f"doctor: blocked {blocker.reason.value}{suffix}{fields}")
        print(f"doctor: {payload['status']}")
        _print_hints(payload)
        print("doctor: readiness performed no inference and no Forge writes; it is not run approval")
    return 0 if readiness.ready else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="creatidy-kernel",
        description=(
            "Run or inspect the offline two-node reference scenario, or validate and preflight a bounded "
            "owner-approved task. Never merges or deploys."
        ),
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Operator profile dotenv file; its values override the process environment (secrets never printed)",
    )
    parser.add_argument(
        "--sops",
        action="store_true",
        help="Decrypt --profile with the user's own SOPS environment (optional generic adapter)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    reference = commands.add_parser("reference", help="Run or resume the approved offline scenario")
    reference.add_argument("--data-dir", type=Path, required=True, help="Local durable state directory")
    reference.add_argument(
        "--approve", action="store_true", help="Approve this finite synthetic two-node scenario (no live provider use)"
    )
    reference.add_argument(
        "--fault",
        choices=("commit", "send", "receipt", "lost-context", "pr-commit", "pr-send", "pr-receipt"),
        help="Inject a synthetic recovery fault; never applies to live providers",
    )
    export = commands.add_parser("export", help="Export existing outcome and evidence records without running work")
    export.add_argument("--data-dir", type=Path, required=True, help="Existing local durable state directory")
    configuration = commands.add_parser("config", help="Operator profile and runtime configuration helpers")
    config_commands = configuration.add_subparsers(dest="config_command", required=True)
    config_validate = config_commands.add_parser(
        "validate", help="Validate the operator profile or process environment; prints field names only"
    )
    config_validate.add_argument(
        "--profile", dest="config_profile", type=Path, default=None, help="Profile file to validate"
    )
    config_validate.add_argument(
        "--sops", dest="config_sops", action="store_true", help="Decrypt the profile with SOPS first"
    )
    config_validate.add_argument("--json", action="store_true", help="Print structured validation evidence")
    config_commands.add_parser("template", help="Print a generic operator profile template")
    doctor = commands.add_parser("doctor", help="Safe readiness diagnostics for one frozen task; no thread or turn")
    doctor.add_argument("--data-dir", type=Path, default=None, help="Control directory; default is user-local state")
    doctor.add_argument("--repo", type=Path, default=None, help="Advanced explicit read-only source checkout override")
    doctor.add_argument("--task", choices=sorted(TASKS), default="143", help="Frozen controller-owned task contract")
    doctor.add_argument("--json", action="store_true", help="Print structured diagnostics")
    task = commands.add_parser("task", help="Bounded frozen task execution; not a general issue runner")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    preflight = task_commands.add_parser(
        "preflight", help="Quota-free read-only readiness; no model turn or Forge write"
    )
    task_run = task_commands.add_parser("run", help="Run or recover the frozen task once")
    for command in (preflight, task_run):
        command.add_argument("--data-dir", type=Path, help="Absolute control directory; default is user-local state")
        command.add_argument(
            "--repo", type=Path, help="Advanced explicit read-only source checkout; default is the acquired cache"
        )
        command.add_argument(
            "--task", choices=sorted(TASKS), default="143", help="Frozen controller-owned task contract"
        )
        command.add_argument(
            "--expected-base",
            type=str,
            default=None,
            help="Pin an exact base SHA; default resolves the base branch or recovers the durable frozen base",
        )
        command.add_argument("--json", action="store_true", help="Print structured evidence instead of a summary")
    task_run.add_argument("--approve", action="store_true", help="Explicit owner approval of this frozen task")
    task_run.add_argument(
        "--trusted-development",
        action="store_true",
        help="Acknowledge trusted-development isolation (Codex workspace-write; not hostile-worker isolation)",
    )
    task_run.add_argument(
        "--deadline",
        type=int,
        default=None,
        help="Owner deadline as a UNIX epoch second; recover original deadline or default to 55 minutes ahead",
    )
    status = task_commands.add_parser("status", help="Show concise durable task status")
    status.add_argument("--data-dir", type=Path, default=None, help="Control directory; default is user-local state")
    task_export = task_commands.add_parser("export", help="Export durable task evidence as JSON")
    task_export.add_argument(
        "--data-dir", type=Path, default=None, help="Control directory; default is user-local state"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            return _config(args)
        environment = _effective_environment(args)
        if args.command == "reference":
            result = run_reference(args.data_dir, owner_approved=args.approve, fault=args.fault)
        elif args.command == "export":
            result = export_reference(args.data_dir)
        elif args.command == "doctor":
            return _doctor(args, environment)
        elif args.command == "task":
            return _task(args, environment)
        else:  # pragma: no cover - argparse rejects unknown commands first.
            raise ValueError("unknown command")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"creatidy-kernel: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


def _task(args: argparse.Namespace, environment: dict[str, str]) -> int:
    if args.task_command == "status":
        directory = args.data_dir if args.data_dir is not None else default_state_directory(environment)
        print(json.dumps(task_status(directory), sort_keys=True, indent=2, allow_nan=False))
        return 0
    if args.task_command == "export":
        directory = args.data_dir if args.data_dir is not None else default_state_directory(environment)
        print(json.dumps(export_task(directory), sort_keys=True, indent=2, allow_nan=False))
        return 0
    task = TASKS[args.task](expected_base_sha=args.expected_base)
    if args.task_command == "preflight":
        readiness = preflight_task(environment, task=task, directory=args.data_dir, source_repository=args.repo)
        payload = readiness.payload()
        if args.json:
            print(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False))
        else:
            print(f"task preflight: {payload['status']}")
            if readiness.exact_base_sha is not None:
                print(f"task preflight: exact base {readiness.exact_base_sha} ({readiness.base_ref})")
            for blocker in readiness.blockers:
                category = f" ({blocker.category})" if blocker.category else ""
                suffix = ": " + ", ".join(blocker.fields) if blocker.fields else ""
                print(f"task preflight: {blocker.reason.value}{category}{suffix}")
            print("task preflight: readiness is not run approval; no model turn / no Forge writes")
        return 0 if readiness.ready else 1
    if not args.approve or not args.trusted_development:
        raise ValueError("task run requires explicit --approve and --trusted-development acknowledgment")
    deadline = args.deadline
    config = TaskRuntimeConfig.parse(environment_with_discovered_codex(environment)[0])
    directory, _source = task_paths(config, args.data_dir, args.repo, environ=environment)
    source = resolve_source(config, task, args.repo, environment=environment, directory=directory)
    # Existing envelopes may be cancelled before any snapshot existed. The
    # backend validates every actual new start, not read-only/stopped recovery.
    if not (directory / "kernel.sqlite3").exists():
        validate_task_source(source, task, dict(config.child_environment))
    components = compose_task_live(config, task)
    connection = components.connection_factory()
    lifecycle: list[str] = []
    cancellation_signal = False

    def request_cancellation(_signum: int, _frame: object) -> None:
        nonlocal cancellation_signal
        cancellation_signal = True

    # Do not interrupt a framed RPC and destroy its transport before journaling.
    # Requests already have finite timeouts; the controller checks this flag at
    # dispatch and acceptance boundaries and then cancels the original receipt.
    previous_sigint = (
        signal.signal(signal.SIGINT, request_cancellation)
        if threading.current_thread() is threading.main_thread()
        else None
    )

    def advance(*, cancel_requested: bool = False) -> dict[str, object]:
        result = run_task(
            directory,
            task=task,
            source_repository=source,
            owner_approved=args.approve,
            trusted_development_acknowledged=args.trusted_development,
            connection=connection,
            version=connection.version,
            deadline=deadline,
            allocator=components.allocator,
            forge_factory=components.forge_factory,
            supported_efforts=components.supported_efforts,
            command_environment=dict(config.child_environment),
            cancel_requested=cancel_requested,
            cancellation_requested=lambda: cancellation_signal,
        )
        lifecycle.extend(line for line in cast("list[str]", result.get("lifecycle", [])) if line not in lifecycle)
        result["lifecycle"] = list(lifecycle)
        return result

    try:
        while True:
            result = advance()
            if cancellation_signal and result.get("condition") not in {
                "cancelled",
                "cancel_uncertain",
                "cancelled_no_dispatch",
            }:
                result = advance(cancel_requested=True)
            if result.get("condition") not in {"running", "waiting"}:
                break
            deadline = cast(int, result["deadline"])
            time.sleep(min(1, max(0, deadline - time.time())))
    except KeyboardInterrupt:
        result = advance(cancel_requested=True)
    finally:
        try:
            if isinstance(connection, CodexStdio):
                connection.close()
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    for line in cast("list[str]", result.get("lifecycle", [])):
        print(f"task: {line}")
    print(f"task: condition={result.get('condition')}")
    print(f"task: no merge / no deploy; evidence: creatidy-kernel task export --data-dir {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
