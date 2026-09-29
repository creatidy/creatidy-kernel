# SPDX-License-Identifier: Apache-2.0
"""Thin local command interface to the bounded reference and dogfood applications."""

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
from creatidy_kernel.adapters.dogfood import (
    DOGFOOD_TASKS,
    compose_dogfood_live,
    dogfood_status,
    export_dogfood,
    run_dogfood,
)
from creatidy_kernel.adapters.reference import export_reference, run_reference


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="creatidy-kernel",
        description=(
            "Run or inspect the offline two-node reference scenario, or run the experimental "
            "owner-approved dogfood task. Never merges or deploys."
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
    dogfood = commands.add_parser(
        "dogfood",
        help=(
            "Experimental owner-approved trusted-development dogfood path for one frozen task; "
            "not a general issue runner"
        ),
    )
    dogfood_commands = dogfood.add_subparsers(dest="dogfood_command", required=True)
    dogfood_run = dogfood_commands.add_parser("run", help="Run or recover the frozen dogfood task once")
    dogfood_run.add_argument("--data-dir", type=Path, required=True, help="Local durable control directory")
    dogfood_run.add_argument(
        "--repo", type=Path, required=True, help="Owner-controlled checkout of the target repository (read-only)"
    )
    dogfood_run.add_argument(
        "--task", choices=sorted(DOGFOOD_TASKS), default="143", help="Frozen controller-owned task contract"
    )
    dogfood_run.add_argument(
        "--expected-base",
        type=str,
        default=None,
        help="Pin an exact authorized base SHA; default freezes the then-current base branch on first admission",
    )
    dogfood_run.add_argument(
        "--approve", action="store_true", help="Explicit owner approval of this frozen dogfood task"
    )
    dogfood_run.add_argument(
        "--trusted-development",
        action="store_true",
        help="Acknowledge trusted-development isolation (Codex workspace-write; not hostile-worker isolation)",
    )
    dogfood_run.add_argument(
        "--deadline",
        type=int,
        default=None,
        help="Owner deadline as a UNIX epoch second; default is 55 minutes ahead",
    )
    dogfood_run.add_argument("--json", action="store_true", help="Print full structured evidence instead of a summary")
    dogfood_status_cmd = dogfood_commands.add_parser("status", help="Show concise durable dogfood status")
    dogfood_status_cmd.add_argument("--data-dir", type=Path, required=True)
    dogfood_export = dogfood_commands.add_parser("export", help="Export durable dogfood evidence as JSON")
    dogfood_export.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "reference":
            result = run_reference(args.data_dir, owner_approved=args.approve, fault=args.fault)
        elif args.command == "export":
            result = export_reference(args.data_dir)
        elif args.command == "dogfood":
            return _dogfood(args)
        else:  # pragma: no cover - argparse rejects unknown commands first.
            raise ValueError("unknown command")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"creatidy-kernel: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


def _dogfood(args: argparse.Namespace) -> int:
    if args.command != "dogfood":  # pragma: no cover - argparse routes first.
        raise ValueError("dogfood subcommand required")
    if args.dogfood_command == "status":
        print(json.dumps(dogfood_status(args.data_dir), sort_keys=True, indent=2, allow_nan=False))
        return 0
    if args.dogfood_command == "export":
        print(json.dumps(export_dogfood(args.data_dir), sort_keys=True, indent=2, allow_nan=False))
        return 0
    task = DOGFOOD_TASKS[args.task](expected_base_sha=args.expected_base)
    if not args.approve or not args.trusted_development:
        raise ValueError("dogfood run requires explicit --approve and --trusted-development acknowledgment")
    deadline = args.deadline if args.deadline is not None else int(time.time()) + 3300
    components = compose_dogfood_live(os.environ, task)
    connection = components.connection_factory()
    try:
        result = run_dogfood(
            args.data_dir,
            task=task,
            source_repository=args.repo,
            owner_approved=args.approve,
            trusted_development_acknowledged=args.trusted_development,
            connection=connection,
            version=connection.version,
            deadline=deadline,
            allocator=components.allocator,
            forge_factory=components.forge_factory,
            supported_efforts=components.supported_efforts,
        )
    finally:
        if isinstance(connection, CodexStdio):
            connection.close()
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    for line in cast("list[str]", result.get("lifecycle", [])):
        print(f"dogfood: {line}")
    print(f"dogfood: condition={result.get('condition')}")
    print(f"dogfood: no merge / no deploy; evidence: creatidy-kernel dogfood export --data-dir {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
