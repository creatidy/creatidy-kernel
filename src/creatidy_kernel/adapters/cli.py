# SPDX-License-Identifier: Apache-2.0
"""Thin local command interface to the bounded reference application."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from creatidy_kernel.adapters.reference import export_reference, run_reference


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="creatidy-kernel",
        description="Run or inspect the offline two-node reference scenario. Never merges or deploys.",
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
    args = parser.parse_args(argv)
    try:
        if args.command == "reference":
            result = run_reference(args.data_dir, owner_approved=args.approve, fault=args.fault)
        else:
            result = export_reference(args.data_dir)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"creatidy-kernel: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
