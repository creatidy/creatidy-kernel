# SPDX-License-Identifier: Apache-2.0
"""Thin local command interface to the bounded reference and task applications."""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.reference import export_reference, run_reference
from creatidy_kernel.adapters.task_execution import (
    TASKS,
    TaskRuntimeConfig,
    compose_task_live,
    export_task,
    preflight_task,
    run_task,
    task_paths,
    task_status,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="creatidy-kernel",
        description=(
            "Run or inspect the offline two-node reference scenario, or preflight/run a bounded "
            "owner-approved task. Never merges or deploys."
        ),
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
    task = commands.add_parser("task", help="Bounded frozen task execution; not a general issue runner")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    preflight = task_commands.add_parser(
        "preflight", help="Quota-free read-only readiness; no model turn or Forge write"
    )
    task_run = task_commands.add_parser("run", help="Run or recover the frozen task once")
    for command in (preflight, task_run):
        command.add_argument(
            "--data-dir", type=Path, help="Absolute control directory; overrides configured state path"
        )
        command.add_argument(
            "--repo", type=Path, help="Absolute read-only source checkout; overrides configured source path"
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
        help="Owner deadline as a UNIX epoch second; default is 55 minutes ahead",
    )
    status = task_commands.add_parser("status", help="Show concise durable task status")
    status.add_argument("--data-dir", type=Path, required=True)
    task_export = task_commands.add_parser("export", help="Export durable task evidence as JSON")
    task_export.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "reference":
            result = run_reference(args.data_dir, owner_approved=args.approve, fault=args.fault)
        elif args.command == "export":
            result = export_reference(args.data_dir)
        elif args.command == "task":
            return _task(args)
        else:  # pragma: no cover - argparse rejects unknown commands first.
            raise ValueError("unknown command")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"creatidy-kernel: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


def _task(args: argparse.Namespace) -> int:
    if args.task_command == "status":
        print(json.dumps(task_status(args.data_dir), sort_keys=True, indent=2, allow_nan=False))
        return 0
    if args.task_command == "export":
        print(json.dumps(export_task(args.data_dir), sort_keys=True, indent=2, allow_nan=False))
        return 0
    task = TASKS[args.task](expected_base_sha=args.expected_base)
    if args.task_command == "preflight":
        readiness = preflight_task(os.environ, task=task, directory=args.data_dir, source_repository=args.repo)
        payload = readiness.payload()
        if args.json:
            print(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False))
        else:
            print(f"task preflight: {payload['status']}")
            if readiness.exact_base_sha is not None:
                print(f"task preflight: exact base {readiness.exact_base_sha} ({readiness.base_ref})")
            for blocker in readiness.blockers:
                suffix = ": " + ", ".join(blocker.fields) if blocker.fields else ""
                print(f"task preflight: {blocker.reason.value}{suffix}")
            print("task preflight: readiness is not run approval; no model turn / no Forge writes")
        return 0 if readiness.ready else 1
    if not args.approve or not args.trusted_development:
        raise ValueError("task run requires explicit --approve and --trusted-development acknowledgment")
    deadline = args.deadline if args.deadline is not None else int(time.time()) + 3300
    config = TaskRuntimeConfig.parse(os.environ)
    directory, source = task_paths(config, args.data_dir, args.repo)
    components = compose_task_live(config, task)
    connection = components.connection_factory()
    try:
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
        )
    finally:
        if isinstance(connection, CodexStdio):
            connection.close()
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
