# SPDX-License-Identifier: Apache-2.0
"""Installed local read client for an already-passed, host-bound descriptor."""

import argparse
import json
import socket
import sys
from typing import cast

from creatidy_kernel.adapters.served_local import (
    MAX_REQUEST,
    MAX_RESPONSE,
    LocalChannelRefused,
    read_frame,
    write_frame,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read a host-bound local Kernel projection; no listener is opened.")
    parser.add_argument("--fd", type=int, required=True, help="preconnected descriptor supplied by the trusted host")
    parser.add_argument(
        "action",
        choices=(
            "snapshot",
            "record",
            "events",
            "claim",
            "context",
            "start",
            "quiesce",
            "renew",
            "submit",
            "report",
            "stop",
            "proposal",
            "proposal-chunk",
            "approve",
            "reconcile",
            "start-managed",
        ),
    )
    parser.add_argument("--producer")
    parser.add_argument("--scope")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--limit", type=int, default=256)
    parser.add_argument("--page", type=int)
    parser.add_argument("--collection")
    parser.add_argument("--index", type=int)
    parser.add_argument("--offset", type=int)
    parser.add_argument("--command-id")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--lease-seconds", type=int)
    parser.add_argument("--remediation-of")
    parser.add_argument("--claim-id")
    parser.add_argument("--receipt")
    parser.add_argument("--reason")
    parser.add_argument("--decision-id")
    parser.add_argument("--proposal-digest")
    parser.add_argument("--expires-at", type=int)
    parser.add_argument("--subject")
    parser.add_argument("--evidence")
    args = parser.parse_args(argv)
    if args.fd < 3:
        parser.error("a preconnected descriptor >= 3 is required")
    request: dict[str, object] = {"version": 1, "action": args.action}
    if args.action == "snapshot" and args.page is not None:
        request["page"] = args.page
    elif args.action != "snapshot" and args.page is not None:
        parser.error("--page is only valid for snapshot")
    if args.action == "record":
        if any(
            value is None
            for value in (args.collection, args.index, args.offset, args.producer, args.scope, args.sequence)
        ):
            parser.error("record requires --collection, --index, --offset, --producer, --scope and --sequence")
        request.update(
            {
                "collection": args.collection,
                "index": args.index,
                "offset": args.offset,
                "after": {"producer": args.producer, "scope": args.scope, "sequence": args.sequence},
            }
        )
    elif args.action == "proposal-chunk":
        if args.offset is None or args.collection is not None or args.index is not None:
            parser.error("proposal-chunk requires --offset and does not accept --collection or --index")
        if args.offset != 0 and args.proposal_digest is None:
            parser.error("proposal-chunk continuation requires --proposal-digest")
        request.update({"offset": args.offset, "proposal_digest": args.proposal_digest})
    elif any(value is not None for value in (args.collection, args.index, args.offset)):
        parser.error("--collection, --index and --offset are only valid for record")
    if args.action == "events":
        if args.producer is None or args.scope is None or args.sequence is None:
            parser.error("events requires --producer, --scope and --sequence")
        request["after"] = {"producer": args.producer, "scope": args.scope, "sequence": args.sequence}
        request["limit"] = args.limit
    elif args.action != "record" and (args.producer is not None or args.scope is not None or args.sequence is not None):
        parser.error("snapshot does not accept a position")
    if args.action != "approve" and (args.decision_id is not None or args.expires_at is not None):
        parser.error("only approve accepts --decision-id and --expires-at")
    if args.action not in {"approve", "proposal-chunk"} and args.proposal_digest is not None:
        parser.error("only approve or proposal-chunk accepts --proposal-digest")
    if args.action != "reconcile" and (args.subject is not None or args.evidence is not None):
        parser.error("only reconcile accepts --subject and --evidence")
    if args.action == "claim":
        if args.command_id is None or args.expected_revision is None or args.lease_seconds is None:
            parser.error("claim requires --command-id, --expected-revision and --lease-seconds")
        if args.claim_id is not None or args.receipt is not None or args.reason is not None:
            parser.error("claim does not accept --claim-id, --receipt or --reason")
        request.update(
            {
                "command_id": args.command_id,
                "expected_revision": args.expected_revision,
                "lease_seconds": args.lease_seconds,
                "remediation_of": args.remediation_of,
            }
        )
    elif args.action == "context":
        if args.claim_id is None:
            parser.error("context requires --claim-id")
        if any(
            value is not None
            for value in (
                args.command_id,
                args.expected_revision,
                args.lease_seconds,
                args.remediation_of,
                args.receipt,
                args.reason,
            )
        ):
            parser.error("context accepts only --claim-id")
        request["claim_id"] = args.claim_id
    elif args.action in {"start", "quiesce", "renew", "submit", "report"}:
        if args.command_id is None or args.expected_revision is None or args.claim_id is None:
            parser.error("action requires --command-id, --expected-revision and --claim-id")
        if args.remediation_of is not None or (args.action != "renew" and args.lease_seconds is not None):
            parser.error("this action does not accept claim parameters")
        if args.action == "renew" and args.lease_seconds is None:
            parser.error("renew requires --lease-seconds")
        if args.reason is not None:
            parser.error("this action does not accept --reason")
        if (args.action == "quiesce") != (args.receipt is not None):
            parser.error("quiesce requires --receipt; start does not accept it")
        request.update(
            {"command_id": args.command_id, "expected_revision": args.expected_revision, "claim_id": args.claim_id}
        )
        if args.action == "quiesce":
            request["receipt"] = args.receipt
        if args.action == "renew":
            request["lease_seconds"] = args.lease_seconds
    elif args.action == "stop":
        if args.command_id is None or args.expected_revision is None or args.reason is None:
            parser.error("stop requires --command-id, --expected-revision and --reason")
        if any(value is not None for value in (args.lease_seconds, args.remediation_of, args.claim_id, args.receipt)):
            parser.error("stop does not accept worker parameters")
        request.update(
            {"command_id": args.command_id, "expected_revision": args.expected_revision, "reason": args.reason}
        )
    elif args.action == "start-managed":
        if args.command_id is None or args.expected_revision is None:
            parser.error("start-managed requires --command-id and --expected-revision")
        if any(
            value is not None
            for value in (args.lease_seconds, args.remediation_of, args.claim_id, args.receipt, args.reason)
        ):
            parser.error("start-managed does not accept worker or STOP parameters")
        request.update({"command_id": args.command_id, "expected_revision": args.expected_revision})
    elif args.action == "approve":
        if (
            args.command_id is None
            or args.expected_revision is None
            or args.decision_id is None
            or args.expires_at is None
            or args.proposal_digest is None
        ):
            parser.error(
                "approve requires --command-id, --expected-revision, --decision-id, --expires-at and --proposal-digest"
            )
        if any(
            value is not None
            for value in (args.lease_seconds, args.remediation_of, args.claim_id, args.receipt, args.reason)
        ):
            parser.error("approve does not accept worker or STOP parameters")
        request.update(
            {
                "command_id": args.command_id,
                "expected_revision": args.expected_revision,
                "decision_id": args.decision_id,
                "expires_at": args.expires_at,
                "proposal_digest": args.proposal_digest,
            }
        )
    elif args.action == "reconcile":
        if args.command_id is None or args.expected_revision is None or args.subject is None or args.evidence is None:
            parser.error("reconcile requires --command-id, --expected-revision, --subject and --evidence")
        if any(
            value is not None
            for value in (args.lease_seconds, args.remediation_of, args.claim_id, args.receipt, args.reason)
        ):
            parser.error("reconcile does not accept worker or STOP parameters")
        request.update(
            {
                "command_id": args.command_id,
                "expected_revision": args.expected_revision,
                "subject": args.subject,
                "evidence": args.evidence,
            }
        )
    elif any(
        value is not None
        for value in (
            args.command_id,
            args.expected_revision,
            args.lease_seconds,
            args.remediation_of,
            args.claim_id,
            args.receipt,
            args.reason,
        )
    ):
        parser.error("read-only actions do not accept work command arguments")
    try:
        with socket.socket(fileno=args.fd) as channel:
            write_frame(channel, json.dumps(request, sort_keys=True).encode(), bound=MAX_REQUEST)
            response = read_frame(channel, bound=MAX_RESPONSE, timeout=300.0)
            value: object = json.loads(response)
            if type(value) is not dict:
                raise LocalChannelRefused("invalid owner response")
            fields = cast(dict[str, object], value)
            if fields.get("version") != 1 or fields.get("ok") is not True:
                raise LocalChannelRefused("invalid owner response")
            sys.stdout.buffer.write(response + b"\n")
    except (OSError, ValueError, TypeError) as error:
        print("local projection request refused", file=sys.stderr)
        raise SystemExit(1) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
