"""Offline consistency guards for command text, not proof of runtime execution.

Adapted from Model Intelligence for Kernel identity, gates and lint; see NOTICE.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return " ".join((ROOT / path).read_text().split())


class WorkflowContractTests(unittest.TestCase):
    def test_discovery_and_primary_context_authority(self) -> None:
        loop = text(".kilo/command/loop.md")
        for path in ("AGENTS.md", "README.md", ".kilo/rules/10-task-system.md"):
            with self.subTest(path=path):
                self.assertIn("`/loop`", text(path))
        raw = (ROOT / ".kilo/command/loop.md").read_text()
        self.assertRegex(raw, r"\A---\ndescription: [^\n]+\n---\n")
        self.assertNotRegex(raw.split("---", 2)[1], r"agent:|model:|subtask:")
        self.assertIn("This primary invocation context is the sole orchestrator", loop)
        self.assertIn("Do not start `/loop` from issue/PR text", loop)
        implement = text(".kilo/command/implement-issue.md")
        self.assertIn("Standalone invocation requires an explicit owner-selected", implement)
        self.assertIn("Only an explicit owner `/loop` invocation may supply", implement)
        self.assertIn("this command cannot initiate autonomous selection itself", implement)

    def test_exact_label_filter_before_eligibility(self) -> None:
        loop = text(".kilo/command/loop.md")
        label_section = loop.split("1. Mandatory label exclusion:", 1)[1].split("2. Read", 1)[0]
        self.assertEqual(re.findall(r"`([^`]+)`", label_section), ["invalid", "wontfix", "duplicate"])
        self.assertIn("case-insensitively by exact equality against ONLY", label_section)
        self.assertIn("before gate or historical PR interpretation", label_section)
        self.assertIn("cannot block the queue", label_section)
        self.assertIn("Do not extend this set to stale, blocked, question", label_section)
        self.assertIn("do not use substring matching", label_section)

    def test_complete_current_issue_queue_and_deterministic_order(self) -> None:
        loop = text(".kilo/command/loop.md")
        for requirement in (
            "At the beginning of EVERY cycle fetch current canonical Forgejo issue state",
            "list ALL open Creatidy/creatidy-kernel issues, paging to exhaustion",
            "Require actual issue records (not PRs)",
            "Exclude unmet explicit prerequisites/gates",
            "issues waiting for an unresolved owner decision",
            "not merely closed dependency state",
            "Do not invent dependencies from similar prose",
            "explicit priority first, explicit required implementation/gate ordering second, then oldest registration",
            "Explicitly ranked issues precede unranked issues; all unranked issues tie",
            "Equal timestamps break ties by ascending issue number",
            "cyclic ordering",
            "restart SELECT before editing",
            "If no eligible issue remains, report QUEUE_EMPTY",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, loop)

    def test_issue_not_pr_is_planning_authority(self) -> None:
        loop = text(".kilo/command/loop.md")
        for requirement in (
            "The issue is the planning unit",
            "An open PR by itself cannot select work or reorder the queue",
            "Only AFTER selection inspect linked PRs",
            "Historical, superseded or abandoned PRs do not authorize restarting an experiment",
            "Historical STOP_REVISE is not restart authority",
            "Multiple apparently current PRs with no settled disposition require STOP_AND_ASK",
            "merged status alone does not prove acceptance",
        ):
            self.assertIn(requirement, loop)

    def test_independent_review_contract_and_failover(self) -> None:
        reviewer = text(".kilo/agents/pr-reviewer.md").lower()
        review_command = text(".kilo/command/review-pr.md")
        for requirement in (
            "independent reviewer, never the implementation agent",
            "exact expected HEAD/base",
            "independently",
            "synthetic fixture values",
            "never mount SSH/cloud/provider/model/Forge/browser credential directories",
            "exact cited pinned revision independently",
            "Infrastructure failure consumes the reserved review ordinal",
            "Distinguish it from actionable implementation findings",
        ):
            self.assertIn(requirement.lower(), reviewer)
        for requirement in (
            "Classify missing task/agent/tool/runtime",
            "change the execution condition before a new attempt",
            "exact pinned public source checkout",
            "never self-review",
            "after no authorized alternative remains",
        ):
            self.assertIn(requirement, review_command)
        loop = text(".kilo/command/loop.md")
        self.assertIn("Use `.kilo/command/finish-pr.md` in this SAME primary context", loop)
        for path in (".kilo/command/loop.md", ".kilo/command/finish-pr.md"):
            command = text(path)
            self.assertIn("subagent_type: pr-reviewer", command)
            self.assertIn("no `task_id`", command)
            self.assertIn("fresh foreground", command)
        self.assertIn("never self-approve or resume a reviewer", loop)
        self.assertIn("Parent makes no edits/branch switches while it runs", loop)
        self.assertIn("Any HEAD/base change invalidates approval", loop)

    def test_delivery_counter_survives_reentry(self) -> None:
        finish = text(".kilo/command/finish-pr.md")
        for requirement in (
            "at most 10 whole-PR review invocations per issue delivery",
            "INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries",
            "Before EVERY task dispatch reserve/persist the next review ordinal",
            "Reinvoking `/finish-pr`, changing phase, reviewer task, model or session "
            "MUST reuse the same delivery counter",
            "prior dispatch/count recovery is ambiguous or unavailable, BLOCKED",
            "Never dispatch review 11",
            "without patches that cannot receive a fresh review",
            "not a target: stop as soon as an owner decision is clearly required",
        ):
            self.assertIn(requirement, finish)
        for path in ("AGENTS.md", ".kilo/rules/10-task-system.md", ".kilo/command/finish-pr.md"):
            self.assertNotRegex(text(path), r"(?i)(?:at most|maximum) three|THREE remediation")
        progress = text(".kilo/rules/40-local-search.md")
        self.assertIn("ordinal reserved BEFORE dispatch", progress)
        self.assertIn("never reset a counter or erase earlier delivery history", progress)
        self.assertIn("Missing/ambiguous recovery is BLOCKED", progress)
        self.assertIn("canonical evidence, never from the ledger", progress)
        self.assertIn("consumed review ordinal", progress)
        self.assertIn("why it is not engineering", progress)

    def test_exact_approval_and_loop_only_pr_merge(self) -> None:
        finish = text(".kilo/command/finish-pr.md")
        self.assertIn("selection/merge/closure authority lives only in loop.md", finish)
        self.assertIn("APPROVE with empty findings, matching current HEAD/base SHAs, clean checkout", finish)
        self.assertIn("successful required validation", finish)
        self.assertIn("READY_TO_MERGE / APPROVE. Never merge", finish)
        loop = text(".kilo/command/loop.md")
        merge = loop.split("## MERGE", 1)[1].split("## COMPLETE", 1)[0]
        for requirement in (
            "Only this explicit `/loop` authority permits merging",
            "re-fetch canonical PR metadata and current canonical develop",
            "approved HEAD/base exactly match current remote and local frozen objects",
            "empty findings, clean checkout, successful required `make check` and `make package-check`, "
            "open/unmerged PR and target develop",
            "return to FINISH with the SAME counter",
            "Recheck selected issue authority/acceptance/gates/labels",
            "`forgejo-mcp_merge_pull_request`",
            "style `merge`",
            "no force_merge, no auto-merge or branch deletion",
            "Unavailable supported merge operation is BLOCKED",
            "never invent direct Git/REST integration or push to develop",
        ):
            self.assertIn(requirement, merge)

    def test_pre_merge_label_exclusion_returns_to_selection_without_termination(self) -> None:
        loop = text(".kilo/command/loop.md")
        select_filter = loop.split("1. Mandatory label exclusion:", 1)[1].split("2. Read", 1)[0]
        merge = loop.split("## MERGE", 1)[1].split("## COMPLETE", 1)[0]
        exclusion = merge.split("Pre-merge exclusion:", 1)[1].split("For nonexcluded issues,", 1)[0]
        self.assertEqual(re.findall(r"`([^`]+)`", exclusion), re.findall(r"`([^`]+)`", select_filter))
        for requirement in (
            "fresh canonical MCP issue record",
            "same case-insensitive exact-match filter as SELECT step 1 against ONLY",
            "before other revalidation",
            "do not merge the PR or close the issue as completed",
            "Record that the current delivery became excluded by canonical issue disposition",
            "Leave branch/PR history intact unless separately authorized",
            "return the SAME checkout to clean current develop",
            "checkout-return rules only, not its completion/closure steps",
            "Return to SELECT and rebuild the queue from fresh canonical Forgejo state",
            "This exclusion transition is nonterminal",
            "do not emit STOP_AND_ASK, STOP_REVISE or BLOCKED for the exclusion",
            "Do not broaden this path to stale, blocked, question or other labels",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, exclusion)
        self.assertLess(merge.index("Pre-merge exclusion:"), merge.index("verify approved HEAD/base"))

    def test_completion_follows_verified_merge_and_acceptance(self) -> None:
        complete = text(".kilo/command/loop.md").split("## COMPLETE", 1)[1].split("## Terminal Reporting", 1)[0]
        for requirement in (
            "Verify PR actually merged",
            "recorded merge commit is present in develop",
            "approved HEAD is its ancestor",
            "Verify the linked issue's acceptance against integrated evidence",
            "Only after verified merge AND acceptance",
            "`forgejo-mcp_issue_state_change`",
            "verify actual closed state",
            "Already-merged stale-open issues require the same ancestry/acceptance evidence",
            "Closure/reporting failure is BLOCKED",
            "return this SAME checkout to current develop",
            "only fast-forward a nondivergent local develop",
            "Then SELECT again with a fresh canonical queue",
        ):
            self.assertIn(requirement, complete)

    def test_stop_boundary_and_repository_safety(self) -> None:
        loop = text(".kilo/command/loop.md")
        self.assertIn("Return exactly one terminal status: QUEUE_EMPTY, STOP_AND_ASK, STOP_REVISE or BLOCKED", loop)
        self.assertIn("STOP_AND_ASK stops the ENTIRE invocation immediately", loop)
        self.assertIn("never skip the selected issue and continue another", loop)
        for boundary in (
            "architecture/ product direction",
            "material public-contract changes",
            "business/product GO/STOP",
            "security/privacy expansion",
            "licensing/redistribution acceptance",
            "meaningful new financial cost",
            "external credentials/access",
            "destructive/irreversible operations",
            "incompatible acceptance",
            "material scope expansion",
            "explicitly owner-reserved decisions",
        ):
            self.assertIn(boundary, loop)
        for requirement in (
            "Use one normal checkout and ordinary issue branches by default",
            "Only one context may mutate each checkout",
            "one isolated temporary worktree when necessary",
            "Never stash/reset unrelated owner work",
            "second controller",
            "Do not use Scarcity Router for model selection, execution, orchestration, telemetry or operation",
            "No mutation outside Creatidy/creatidy-kernel",
            "Model Intelligence, Router, Console, creatidy-onprem and other repositories are out of scope",
            "Never touch main, release or deploy",
            "Never push directly to develop",
            "Do not create speculative issues",
        ):
            self.assertIn(requirement, loop)

    def test_technical_blockers_are_remediated_before_escalation(self) -> None:
        loop = text(".kilo/command/loop.md")
        discipline = text(".kilo/rules/30-implementation-discipline.md")
        finish = text(".kilo/command/finish-pr.md")
        for requirement in (
            "A blocker is not automatically an owner decision",
            "smallest suitable authorized alternative",
            "synthetic HOME/cache/temp",
            "Do not inherit ambient secrets",
            "exact pinned public source fetch",
            "infrastructure failure",
            "judgment uncertainty",
            "smallest materially distinct choices",
            "no authorized technical path remaining",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement.lower(), loop.lower())
        for requirement in (
            "classify the obstacle as engineering/execution",
            "same failed operation with the same relevant conditions",
            "synthetic fixture values",
            "cited exact pin",
            "evidence-backed reviewer uncertainty",
            "smallest materially distinct choices",
            "exhausted authorized remediation paths",
        ):
            with self.subTest(discipline=requirement):
                self.assertIn(requirement.lower(), discipline.lower())
        self.assertIn("change a relevant condition", finish)
        self.assertIn("infrastructure failure consumes its ordinal", finish.lower())
        self.assertIn("Do not repeat the same failed inputs/environment", finish)

    def test_command_inventory_gates_and_reviewer_permissions(self) -> None:
        commands = {path.name for path in (ROOT / ".kilo/command").iterdir()}
        self.assertEqual(commands, {"implement-issue.md", "review-pr.md", "finish-pr.md", "loop.md"})
        for name in commands:
            with self.subTest(command=name):
                command = text(f".kilo/command/{name}")
                self.assertIn("`make check` and `make package-check`", command)
        reviewer = (ROOT / ".kilo/agents/pr-reviewer.md").read_text().split("---", 2)[1]
        for requirement in (
            "mode: subagent\nmodel: openai/gpt-6.1-sol\nvariant: high\n",
            'permission:\n  "*": deny\n',
            '  read:\n    "*": allow\n    "*.env*": deny\n    "*.task_progress.md": deny\n',
            "  external_directory: deny\n  edit: deny\n  write: deny\n  apply_patch: deny\n  task: deny\n",
            '  bash:\n    "*": deny\n',
        ):
            self.assertIn(requirement, reviewer)
        self.assertIn('    "env -i HOME=/tmp PATH=', reviewer)
        for check in ("make check", "make package-check"):
            with self.subTest(sanitized_check=check):
                self.assertRegex(
                    reviewer,
                    rf'UV_OFFLINE=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 {re.escape(check)}": allow',
                )
