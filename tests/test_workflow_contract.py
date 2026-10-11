"""Offline consistency guards for command text, not proof of runtime execution.

Adapted from Model Intelligence for Kernel identity, gates and lint; see NOTICE.
"""

import re
import unittest
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]


class DependencyEvidence(TypedDict, total=False):
    state: str
    evidence: str
    contract: str
    dependencies: list[int]


def text(path: str) -> str:
    return " ".join((ROOT / path).read_text().split())


def eligibility_transitions() -> dict[str, tuple[str, str, str]]:
    """Decode command classification, not a Kernel product/runtime controller."""
    section = (
        (ROOT / ".kilo/command/loop.md")
        .read_text()
        .split("## Post-Selection Eligibility Revalidation", 1)[1]
        .split("## IMPLEMENT", 1)[0]
    )
    rows: dict[str, tuple[str, str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("| "):
            continue
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if cells[0] in ("Evidence Class", "---"):
            continue
        if len(cells) != 4 or cells[0] in rows:
            raise AssertionError("classification rows must be unique four-column rules")
        rows[cells[0]] = (cells[1], cells[2], cells[3])
    return rows


def trace_dependency(issue: int, records: Mapping[int, DependencyEvidence]) -> tuple[str, tuple[int, ...]]:
    """Return the evidence result and all executable leaves in verified dependency branches."""
    record = records.get(issue)
    if record is None or record.get("evidence") != "current":
        return "UNVERIFIED", ()
    contract = record.get("contract")
    if contract == "unknown":
        return "UNVERIFIED", ()
    if contract == "gated":
        return "GATED", ()
    if record.get("state") == "closed":
        return ("SATISFIED", ()) if contract == "ready" else ("UNVERIFIED", ())
    if record.get("state") != "open":
        return "UNVERIFIED", ()
    predecessors: list[int] = []
    gated = False
    for dependency in record.get("dependencies", []):
        state, dependency_predecessors = trace_dependency(dependency, records)
        if state == "UNVERIFIED":
            return state, ()
        if state in ("GATED", "ELIGIBLE"):
            gated = True
            predecessors.extend(dependency_predecessors)
    if gated:
        return "GATED", tuple(dict.fromkeys(predecessors))
    return "ELIGIBLE", (issue,)


def select_confirmed_eligible(
    repository_issues: tuple[int, ...], records: Mapping[int, DependencyEvidence]
) -> tuple[int | None, str | None]:
    """Use the loop's already-ordered queue; unknown evidence blocks empty claims."""
    unverified = False
    for issue in repository_issues:
        state, _ = trace_dependency(issue, records)
        if state == "ELIGIBLE":
            return issue, None
        unverified |= state == "UNVERIFIED"
    if unverified:
        return None, "UNVERIFIED"
    return None, "QUEUE_EMPTY"


def mcp_checkout_evidence_is_current(evidence: dict[str, object]) -> bool:
    """Model the review admission contract for MCP refs and prepared Git objects."""
    expected_head = evidence.get("expected_head")
    expected_base = evidence.get("expected_base")
    if not isinstance(expected_head, str) or not isinstance(expected_base, str):
        return False
    return (
        evidence.get("canonical_repository") == "Creatidy/creatidy-kernel"
        and evidence.get("canonical_repository_url") == "https://forgejo.creatidy.com/Creatidy/creatidy-kernel"
        and isinstance(evidence.get("repository_is_private"), bool)
        and evidence.get("pr_base_repository") == "Creatidy/creatidy-kernel"
        and evidence.get("pr_base_repository_url") == evidence.get("canonical_repository_url")
        and evidence.get("pr_state") == "open"
        and evidence.get("pr_merged") is False
        and evidence.get("pr_base_ref") == "develop"
        and evidence.get("base_ref_repository") == evidence.get("pr_base_repository")
        and evidence.get("base_ref_name") == evidence.get("pr_base_ref")
        and evidence.get("pr_base_sha") == expected_base
        and evidence.get("base_ref_sha") == expected_base
        and evidence.get("pr_head_repository") == evidence.get("head_ref_repository")
        and evidence.get("pr_head_repository_url") == evidence.get("head_ref_repository_url")
        and evidence.get("pr_head_ref") == evidence.get("head_ref_name")
        and evidence.get("pr_head_sha") == expected_head
        and evidence.get("head_ref_sha") == expected_head
        and evidence.get("local_head_sha") == expected_head
        and evidence.get("local_head_object") == expected_head
        and evidence.get("local_base_object") == expected_base
        and evidence.get("checkout_clean") is True
    )


class WorkflowContractTests(unittest.TestCase):
    def test_dependency_chain_selects_earliest_executable_predecessor(self) -> None:
        records: dict[int, DependencyEvidence] = {
            86: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": []},
            87: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": [86]},
            88: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": [87]},
        }
        self.assertEqual(trace_dependency(88, records), ("GATED", (86,)))
        self.assertEqual(select_confirmed_eligible((88, 87, 86), records), (86, None))
        loop = text(".kilo/command/loop.md")
        for requirement in (
            "Walk each explicit dependency chain backward",
            "earliest open issue that is itself eligible to execute",
            "Select that executable predecessor before its dependent",
            "Do not infer an edge where Forgejo has none",
        ):
            self.assertIn(requirement, loop)

    def test_branching_dependencies_use_canonical_order_not_edge_order(self) -> None:
        records: dict[int, DependencyEvidence] = {
            83: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": []},
            84: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": []},
            88: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": [84, 83]},
        }
        self.assertEqual(trace_dependency(88, records), ("GATED", (84, 83)))
        self.assertEqual(select_confirmed_eligible((88, 83, 84), records), (83, None))

    def test_issue_local_gate_does_not_block_unrelated_eligible_work(self) -> None:
        records: dict[int, DependencyEvidence] = {
            87: {"state": "open", "evidence": "current", "contract": "gated", "dependencies": []},
            88: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": [87]},
            90: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": []},
        }
        self.assertEqual(select_confirmed_eligible((88, 90), records), (90, None))
        self.assertEqual(trace_dependency(88, records), ("GATED", ()))
        loop = text(".kilo/command/loop.md")
        self.assertIn("it does not gate unrelated eligible Kernel issues", loop)
        self.assertIn("issue-local gates from repository-wide inactivity", loop)

    def test_changed_or_missing_dependency_evidence_is_unverified_not_ready_or_gated(self) -> None:
        records: dict[int, DependencyEvidence] = {
            88: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": [87]},
            87: {"state": "open", "evidence": "changed", "contract": "ready", "dependencies": []},
        }
        self.assertEqual(trace_dependency(88, records), ("UNVERIFIED", ()))
        self.assertEqual(trace_dependency(88, {88: records[88]}), ("UNVERIFIED", ()))
        unknown_contract: dict[int, DependencyEvidence] = {
            88: {"state": "open", "evidence": "current", "contract": "unknown", "dependencies": []}
        }
        self.assertEqual(trace_dependency(88, unknown_contract), ("UNVERIFIED", ()))
        self.assertEqual(select_confirmed_eligible((88,), records), (None, "UNVERIFIED"))
        mixed_edges: dict[int, DependencyEvidence] = {
            86: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": []},
            87: {"state": "open", "evidence": "changed", "contract": "ready", "dependencies": []},
            88: {"state": "open", "evidence": "current", "contract": "ready", "dependencies": [86, 87]},
        }
        self.assertEqual(trace_dependency(88, mixed_edges), ("UNVERIFIED", ()))
        loop = text(".kilo/command/loop.md")
        self.assertIn("`UNVERIFIED`, not ready and not a verified gate", loop)
        self.assertIn("Do not report `QUEUE_EMPTY` while any in-scope candidate", loop)
        terminal_refresh = loop.split("Before `WAIT`/inactivity", 1)[1].split("Before any terminal status", 1)[0]
        for requirement in (
            "`BLOCKED`, `STOP_AND_ASK` or `QUEUE_EMPTY`",
            "refresh the current canonical issue/dependency evidence",
            "every required producer/contract observation",
            "Record changed or unavailable facts as `UNVERIFIED`",
        ):
            self.assertIn(requirement, terminal_refresh)

    def test_queue_exhaustion_is_limited_to_verified_kernel_issue_evidence(self) -> None:
        kernel_records: dict[int, DependencyEvidence] = {
            88: {"state": "open", "evidence": "current", "contract": "gated", "dependencies": []},
        }
        self.assertEqual(select_confirmed_eligible((88,), kernel_records), (None, "QUEUE_EMPTY"))
        changed_records: dict[int, DependencyEvidence] = {88: {"state": "open", "evidence": "changed"}}
        self.assertEqual(select_confirmed_eligible((88,), changed_records), (None, "UNVERIFIED"))
        loop = text(".kilo/command/loop.md")
        self.assertIn("only the refreshed canonical open Kernel issue queue", loop)
        self.assertIn("it is not a claim that upstream repositories have no work", loop)

    def test_recovery_progress_is_nonterminal_and_same_invocation_continues(self) -> None:
        loop = text(".kilo/command/loop.md")
        for requirement in (
            "Progress/status narration",
            "is nonterminal",
            "continue the same authorized invocation",
            "until an existing terminal condition is actually met",
            "narration does not create a new state or extend authority",
        ):
            self.assertIn(requirement, loop)
        discipline = text(".kilo/rules/10-task-system.md")
        self.assertIn("Technical-recovery progress/status narration is nonterminal", discipline)

    def test_pre_delivery_gates_refresh_queue_without_terminating_or_resetting_history(self) -> None:
        transitions = eligibility_transitions()
        gates = ("unmet_prerequisite", "missing_producer", "missing_contract_artifact", "known_dependency_gate")
        for gate in gates:
            with self.subTest(gate=gate):
                before, after, action = transitions[gate]
                self.assertEqual((before, after, action), ("SELECT", "PRESERVE_DELIVERY", "retain"))
                fresh_queue = (49, 52)
                gated_this_cycle = {49}
                eligible = tuple(issue for issue in fresh_queue if issue not in gated_this_cycle)
                self.assertEqual(eligible, (52,))
                self.assertNotIn(before, ("BLOCKED", "STOP_REVISE", "STOP_AND_ASK"))
                gated_this_cycle.add(52)
                eligible = tuple(issue for issue in fresh_queue if issue not in gated_this_cycle)
                outcome = eligible[0] if eligible else transitions["all_ineligible_queue"][0]
                self.assertEqual(outcome, "QUEUE_EMPTY")
        section = (
            text(".kilo/command/loop.md")
            .split("## Post-Selection Eligibility Revalidation", 1)[1]
            .split("## IMPLEMENT", 1)[0]
        )
        for invariant in (
            "BEFORE the first substantive implementation mutation",
            "no implementation commit, no current authorized implementation PR",
            "including documentation/contract work",
            "Prior delivery in another checkout/session still counts",
            "Make no speculative implementation, invented producer semantics or workaround",
            "Keep the issue open and unchanged",
            "clean current develop",
            "rebuild the FULL canonical issue queue, paging to exhaustion",
            "not a permanent exclusion or authority from progress memory",
            "revalidate gates against current canonical/upstream evidence each cycle",
            "If all remaining issues are verified ineligible, QUEUE_EMPTY",
            "a gated candidate alone never emits BLOCKED",
        ):
            self.assertIn(invariant, section)

    def test_started_delivery_and_real_stops_cannot_be_evaded_by_a_gate(self) -> None:
        transitions = eligibility_transitions()
        for has_commit, has_pr, has_changes in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(commit=has_commit, pr=has_pr, changes=has_changes):
                phase = 1 if has_commit or has_pr or has_changes else 0
                self.assertEqual(transitions["missing_producer"][phase], "PRESERVE_DELIVERY")
        for condition, expected in (
            ("genuine_owner_decision", "STOP_AND_ASK"),
            ("eligible_execution_problem", "RECOVER"),
            ("unverified_required_evidence", "RECOVER"),
            ("exhausted_machinery_failure", "BLOCKED"),
        ):
            self.assertEqual(transitions[condition], (expected, expected, "retain"))
        self.assertEqual(transitions["review_findings_at_bound"], ("NOT_APPLICABLE", "STOP_REVISE", "retain"))
        self.assertTrue(all(rule[2] == "retain" for rule in transitions.values()))
        for path in (".kilo/rules/10-task-system.md", ".kilo/rules/30-implementation-discipline.md"):
            self.assertIn("post-selection eligibility revalidation", text(path))

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
            "never read the contents of `.env*` files or mount",
            "SSH/cloud/provider/model/Forge/browser credential directories",
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

    def test_review_admits_matching_private_mcp_refs_and_prepared_objects(self) -> None:
        evidence: dict[str, object] = {
            "canonical_repository": "Creatidy/creatidy-kernel",
            "canonical_repository_url": "https://forgejo.creatidy.com/Creatidy/creatidy-kernel",
            "repository_is_private": True,
            "pr_base_repository": "Creatidy/creatidy-kernel",
            "pr_base_repository_url": "https://forgejo.creatidy.com/Creatidy/creatidy-kernel",
            "pr_state": "open",
            "pr_merged": False,
            "pr_base_ref": "develop",
            "base_ref_repository": "Creatidy/creatidy-kernel",
            "base_ref_name": "develop",
            "expected_base": "base-sha",
            "pr_base_sha": "base-sha",
            "base_ref_sha": "base-sha",
            "pr_head_repository": "Creatidy/creatidy-kernel",
            "pr_head_repository_url": "https://forgejo.creatidy.com/Creatidy/creatidy-kernel",
            "head_ref_repository": "Creatidy/creatidy-kernel",
            "head_ref_repository_url": "https://forgejo.creatidy.com/Creatidy/creatidy-kernel",
            "pr_head_ref": "issue-86-review-mcp",
            "head_ref_name": "issue-86-review-mcp",
            "expected_head": "head-sha",
            "pr_head_sha": "head-sha",
            "head_ref_sha": "head-sha",
            "local_head_sha": "head-sha",
            "local_head_object": "head-sha",
            "local_base_object": "base-sha",
            "checkout_clean": True,
            "anonymous_git_available": False,
        }
        self.assertTrue(mcp_checkout_evidence_is_current(evidence))

        for field in (
            "canonical_repository",
            "canonical_repository_url",
            "repository_is_private",
            "pr_base_repository",
            "pr_base_repository_url",
            "pr_state",
            "pr_merged",
            "pr_base_ref",
            "base_ref_repository",
            "base_ref_name",
            "pr_base_sha",
            "base_ref_sha",
            "pr_head_repository",
            "pr_head_repository_url",
            "head_ref_repository",
            "head_ref_repository_url",
            "pr_head_ref",
            "head_ref_name",
            "pr_head_sha",
            "head_ref_sha",
            "expected_head",
            "expected_base",
            "local_head_sha",
            "local_head_object",
            "local_base_object",
            "checkout_clean",
        ):
            with self.subTest(mismatched_or_missing=field):
                mismatched = {**evidence, field: None}
                self.assertFalse(mcp_checkout_evidence_is_current(mismatched))

        reviewer = text(".kilo/agents/pr-reviewer.md")
        for requirement in (
            '"forgejo-mcp_get_*": allow',
            '"forgejo-mcp_list_*": allow',
            "reported base and head repositories' branches through MCP",
            "exact base/head ref names and commit IDs",
            "matching both the PR metadata and supplied expected SHAs",
            "Private repositories are valid when MCP returns matching current metadata",
            "public visibility and anonymous Git access are not prerequisites",
            "exact expected head/base commit objects",
            "complete merge-base-to-HEAD diff",
            "Recheck PR and ref metadata through MCP",
        ):
            self.assertIn(requirement, reviewer)
        self.assertIn("read-only Forgejo MCP", text(".kilo/command/review-pr.md"))
        self.assertIn("does not need anonymous Git transport", text(".kilo/command/finish-pr.md"))

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
            "mode: subagent\n",
            'permission:\n  "*": deny\n',
            '  read:\n    "*": allow\n    "*.env*": deny\n    "*.task_progress.md": deny\n',
            '  external_directory:\n    "/home/adrian/workspace/creatidy/creatidy-kernel/**": allow\n'
            '    "/home/adrian/workspace/creatidy/creatidy-kernel-*/**": allow\n    "*": deny\n',
            "  edit: deny\n  write: deny\n  apply_patch: deny\n  task: deny\n",
            '  bash:\n    "*": deny\n',
        ):
            self.assertIn(requirement, reviewer)
        # The reviewer binding is intentionally unpinned: enforce the durable
        # inheritance/affinity policy instead of a specific model configuration.
        self.assertNotRegex(reviewer, r"(?m)^model:|^variant:")
        binding = text(".kilo/agents/pr-reviewer.md")
        for requirement in (
            "## Reviewer binding",
            "intentionally does not pin a provider, model, model version or reasoning/thinking variant",
            "inherits the implementation session's effective provider family,"
            " model and reasoning/thinking configuration",
            "an OpenAI implementation is reviewed by an OpenAI reviewer",
            "a z.ai implementation is reviewed by a z.ai reviewer",
            "Never silently substitute a provider, model, version or reasoning/thinking configuration",
        ):
            self.assertIn(requirement, binding)
        self.assertIn('    "env -i HOME=/tmp PATH=', reviewer)
        self.assertIn('"git -C /home/adrian/workspace/creatidy/creatidy-kernel-* status --short": allow', reviewer)
        self.assertIn("Never read the contents of `.env*` files", text(".kilo/agents/pr-reviewer.md"))
        prefix = "UV_OFFLINE=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1"
        for check in ("make check", "make package-check"):
            with self.subTest(sanitized_check=check):
                command = check.removeprefix("make ")
                permission = f'{prefix} make -C /home/adrian/workspace/creatidy/creatidy-kernel-* {command}": allow'
                self.assertIn(permission, reviewer)

    def test_reviewer_read_only_allowances_cover_supported_checkout_forms(self) -> None:
        reviewer = (ROOT / ".kilo/agents/pr-reviewer.md").read_text().split("---", 2)[1]
        bash = reviewer.split("  bash:\n", 1)[1]
        allowed = re.findall(r'^    "([^"\n]+)": allow$', bash, flags=re.MULTILINE)
        reviewer = (ROOT / ".kilo/agents/pr-reviewer.md").read_text()
        for forbidden_tool in ("forgejo-mcp_create_", "forgejo-mcp_update_", "forgejo-mcp_delete_"):
            self.assertNotIn(forbidden_tool, reviewer)
        check_environment = (
            "env -i HOME=/tmp PATH=/home/adrian/.local/bin:/usr/local/bin:/usr/bin:/bin "
            "TMPDIR=/tmp LANG=C.UTF-8 UV_CACHE_DIR=/home/adrian/.cache/uv UV_OFFLINE=1 "
            "GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "
        )
        for checkout, path_pattern in (
            ("/home/adrian/workspace/creatidy/creatidy-kernel", "/home/adrian/workspace/creatidy/creatidy-kernel"),
            (
                "/home/adrian/workspace/creatidy/creatidy-kernel-owner-approved-isolation",
                "/home/adrian/workspace/creatidy/creatidy-kernel-*",
            ),
        ):
            commands = [
                f"git -C {checkout} {operation}"
                for operation in (
                    "remote -v",
                    "status --short",
                    "rev-parse HEAD",
                    "merge-base HEAD origin/develop",
                    "log -1",
                    "show HEAD",
                    "diff BASE HEAD",
                    "ls-tree HEAD",
                )
            ]
            commands += [f"{check_environment}make -C {checkout} {gate}" for gate in ("check", "package-check")]
            for command in commands:
                with self.subTest(command=command):
                    self.assertTrue(any(fnmatchcase(command, pattern) for pattern in allowed))
            self.assertIn(f'"{path_pattern}/**": allow', reviewer)
            for operation in (
                "fetch origin",
                "checkout develop",
                "commit -m change",
                "push origin develop",
                "ls-remote https://forgejo.creatidy.com/Creatidy/creatidy-kernel",
            ):
                self.assertFalse(any(fnmatchcase(f"git -C {checkout} {operation}", pattern) for pattern in allowed))
        self.assertFalse(
            any(
                fnmatchcase("git -C /home/adrian/workspace/creatidy/model-intelligence status --short", pattern)
                for pattern in allowed
            )
        )
        for denial in (
            '"*--output*": deny',
            '"*--ext-diff*": deny',
            '"*--textconv*": deny',
            '"*>*": deny',
            '"*|*": deny',
            '"*;*": deny',
            '"*&*": deny',
        ):
            self.assertIn(denial, bash)

    def test_shared_commands_use_inherited_reviewer_binding(self) -> None:
        reviewer = (ROOT / ".kilo/agents/pr-reviewer.md").read_text().split("---", 2)[1]
        self.assertNotRegex(reviewer, r"(?m)^model:|^variant:")
        review = text(".kilo/command/review-pr.md")
        self.assertIn("inheritance and explicit owner override policy", review)
        for path in (ROOT / ".kilo/command").glob("*.md"):
            self.assertNotRegex(path.read_text(), r"GPT-6\.1 Sol High selection")
