# Installed Offline Acceptance: Issue 60

## Subject And Verdict

This is the bounded offline candidate for [#60](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60),
prepared 2026-11-13. It receives the actual packaged implementation against authorized synthetic peers,
not live providers or a release. All applicable offline cells below pass. Independent acceptance and
integration must be recorded against the exact report/test commit in the canonical #60 receipt before
using `OFFLINE_ACCEPTED`. This document alone is not that independent verdict.

- Canonical source/base: `5e6f42906980fd42293e0dab1be3d7942532ff81`, fetched from
  `https://forgejo.creatidy.com/Creatidy/creatidy-kernel.git`.
- Product: `creatidy_kernel`, version `0.0.1.dev0`, Python >=3.12, no runtime dependencies.
- Tested wheel SHA-256: `f3652660751f67dade5d960c6293fa38993dfc53cfb45c53dea8f29e197a045b`.
  All 77 package files were compared byte-for-byte with the installed wheel and current `src/`.
  This delivery changes tests and documentation only, not those product bytes.
- Local installed prefix: `/tmp/kilo/acceptance-60-venv`; execution directory:
  `/tmp/kilo/acceptance-60-offline`, outside the checkout. These are disposable execution locations,
  not required product paths or durable receipt storage.
- The report/test revision is the commit containing this file, bound to its exact base/HEAD by the
  delivery PR, canonical CI checkout and independent review result. It is distinct from the product
  source revision above; a commit cannot embed its own SHA.
- Historical #60 reviews consumed: 8/10, including the bounded documentation approval in PR #119.
  #133 review is not #60 review. Reserve 9/10 only after this complete candidate and exact CI are ready.

`Offline installed acceptance` proves the bounded packaged behavior below. `Live provider acceptance`
requires separately authorized real accounts/services. `Product GO` is a separate owner release/readiness
decision. Neither green CI nor this report implies the latter two. #60 stays OPEN while live AC6 is unmet.

## Binding Criteria

The current issue body and [owner reconciliation of November 8](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-21185)
are the acceptance authority, together with ADR [0011](../adr/0011-task-level-authority-and-trusted-development.md)
and [0012](../adr/0012-unified-work-coordination-and-drivers.md):

| Criterion | Binding interpretation |
| --- | --- |
| AC1 | Built installed handlers outside the checkout: approve ordinary work, order dependencies, claim/handoff/execute/submit/review/fix/accept/synchronize through one core. Pull-first; optional supported managed push must retain that same truth and acceptance. No mandatory particular harness. |
| AC2 | Retain Forgejo compatibility and execute independently qualified Jira/GitHub/Jenkins/Confluence bindings, including native partial/refusal and migration semantics. Synthetic services do not prove live compatibility. |
| AC3 | Lost/duplicate receipts; changed source/base/policy; incomplete pages; unsupported capability; outage; missed heartbeat; claim collision/expiry; manual edits; uncertain writes; cooperative STOP versus managed settlement; disconnect; event gap/reconnect; privacy refusal. |
| AC4 replacement | State unproved strict route, exact pin/admission, no-hidden-call and related guarantees as unsupported/unknown; refuse requested unsupported profiles. #52 strict execution is not required or implemented; preserve exact-version negatives, not a universal harness verdict. |
| AC5 | #56/#58 attributable costs, unknowns and corrections; #54 exact review lineage; #57 authorization/events; #59 ownership; #62 supported-platform/state/profile compatibility and migration, with versioned evidence and limits. |
| AC6 | Separately approved exact artifact, disposable resources, accounts/effects/network/privacy and finite numeric time/attempt/money envelope, STOP/unknown rules. Unreceived live is not an offline failure or product GO. |

The older superseded strict AC4/#52 edge and deferred-#105 wording are history. #105 was reactivated and
integrated; #133 supplies the qualified ordinary composition absent in the November 11 inventory.
No optional #55 implementation, external-repository mutation, #52/#53 restart, new controller, routing
framework, release or deployment is part of this delivery.

## Prerequisite Evidence

Fresh MCP dependency enumeration returned exactly #105/#106/#56/#57/#58/#59/#62/#133, all closed.
Closure was not used as acceptance evidence: read the receipts/PRs and verify merged ancestry instead.
All following merges are ancestors of the product source above; their trees equal their reviewed heads.

| Issue | Integrated merge | Evidence and boundary |
| --- | --- | --- |
| #105 | `20de425eb01e2467151b0e1994ef02eed493626b` | [PR #111](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/pulls/111), head `1e9f0f97c65ccb540d488be1c9733b605e1e4dc3`, base `c24cd3718c66a7516ff5a60208de14a5880394a2`; independent 10/10 APPROVE and exact CI run 317. Issue comment 21706 still says unmerged, so it is not a postmerge receipt. Current merged metadata, Git ancestry and installed source/CI supply the integrated evidence. Trusted Jenkins mapping and Confluence draft-effect grants are not universal native atomicity. |
| #106 | `fce1c0d4d63fdf4ca0673512c9b9d2512f0ac023` | [Receipt 20200](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/106#issuecomment-20200): installed Forgejo pull/managed/mixed ordinary lifecycle, not live execution. |
| #56 | `b11aae420517b14bbb4d85c84f9a0d82c14446c6` | [Receipt 19417](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56#issuecomment-19417): finite work/clock/correction accounting, not hidden inference enforcement. |
| #57 | `da9c736fe61ba33c3d1889f15949948928eb4dea` | [Receipt 20654](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57#issuecomment-20654): owner-thread local descriptor/client, not remote authentication or Console deployment. |
| #58 | `97fedb7e3c67026c5987a2806a419cef914fdad7` | [Receipt 20902](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58#issuecomment-20902): private producer lineage, not sharing/billing authority. |
| #59 | `b1c9ffca16f06596e5c3afdca3b9893cf7422d8d` | [Receipt 19620](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/59#issuecomment-19620): exact claims, retained conflicts and profile settlement, not escaped-process containment. |
| #62 | `4cb500a6d58d983854ad795a56db0af3c6d902f8` | [Receipt 20960](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/62#issuecomment-20960): finite installed upgrade/profile matrix, not all platforms or owner data. |
| #133 | `5e6f42906980fd42293e0dab1be3d7942532ff81` | [Receipt 22062](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/133#issuecomment-22062): qualified ordinary DAG, schema 7 and four synthetic providers, not #60 completion or live review quality. |

## Profiles And Versions

All shipped adapters below are the implementation in the exact product source, not separately released
packages. External subjects retain family, implementation major, origin including port, tenant,
namespace, native key/revision and provenance. Fixtures use distinct synthetic read/write credentials
per provider and reject wrong-provider credentials. No real credentials or private records are used.

| Installed profile | Version-qualified producer and evidence strength |
| --- | --- |
| Ordinary cooperative pull | Basic `kilo-pull`, `trusted_development`; cooperative task quiescence, not ownership of the user's Kilo process. Provider/model/effort may remain unavailable. |
| Optional managed push | Basic `codex`/mixed fixture, `CodexStdio` synthetic app-server version `0.99.1`, actual local owned process groups and retained exact witnesses. This is not native Codex version conformance, detached supervision or hostile isolation. |
| Product review | `NativeKiloReview` uses fresh read-scoped synthetic Kilo-shaped sessions, exact candidate input/receipt, programmed negative verdict then substantive remediation. Native Kilo 7.8.25 is separately opt-in, not executed by these synthetic receipts; no semantic model-quality claim. |
| Work | `jira-cloud`, implementation `1`, REST API `3`, opaque `ENG-41`/`ENG-42`, visible-scope obligation, not complete native dependency enumeration. |
| SCM/review | `github`, implementation `1`, REST `2026-03-10`; stable bounded all-open scans and exact local Git objects. Two scans are not an atomic remote snapshot. Unsupported conditional mutations refuse. |
| CI | `jenkins`, implementation `1`, API profile `jenkins-2.479.3;git-5.7.0;git-client-6.1.0`; exact build/remote/revision and host-owned build-to-recipe mapping, not SUCCESS-as-acceptance. |
| Context/sync | `confluence-cloud`, implementation `1`, REST `2`, explicit page/version/actor; replacement requires exact effect and separately authorized possible native draft effects. Not a cross-service transaction or draft CAS guarantee. |
| Forgejo compatibility | Retained version-1 facade/reference and original numeric intake; synthetic Forgejo HTTP/AGit, no live remote receipt. |
| Durable state | SQLite schema `7`, history codec `1`, typed versioned ordinary/qualified/ownership records; backup/refusal instead of downgrading authority. |
| Presentation/export | Owner-served local wire `1`, producer-scoped positions; private outcome policy `private-metadata-v1`, local-only pseudonymized metadata, not a sharing policy. |
| Native profile migration | SOPS `3.12.2`, age-keygen `1.1.1`, generated synthetic key/profile, installed CLI; reused exact-wheel receipt below. No actual owner key/profile or recipient migration. |

## Execution Receipts

**R1, reused exact-source canonical CI:** [run 332 / database ID 3677](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/actions/runs/332),
actual expanded checkout log confirms the full product SHA. `make check`, `make package-check`, and
`make audit` succeeded. Installed wheel suite: **2695 passed, 1 skipped, 106 subtests**, 438.31 seconds;
built-package phase 9m12s, total run 9m53s. The separate serial/native gate reported 17 passed and 97
opt-in skips. Missing native/SOPS binaries do not become passes.

`tools/check_package.py` builds wheel/sdist, compares package bytes after sdist rebuild, installs the
rebuilt wheel outside the checkout, verifies import provenance and bytes, and runs the entire
`not serial_native` test suite there. Thus the tests cited below are installed tests, not merely
source-tree mocks. The dependency-free installed smoke scripts and actual historical-reader wheel
checks also ran. The new report/test commit must separately pass these same canonical gates before
review/integration; R1 is not a substitute for that candidate's CI.

**R2, reused local native-tool receipt:** [comment 22066](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-22066)
records the exact wheel hash above and installed
`tests/test_operator_profile.py::test_real_sops_age_profile_is_read_only_and_missing_key_refuses`:
**1 passed**, SOPS 3.12.2 / age 1.1.1, synthetic keys, closed environment, installed CLI. Includes source
ciphertext preservation, missing key, new-file migration and injected staging/fsync failures. The
same product/test bytes are retained; this scenario is not repeated merely because CI skipped it.

**L1, newly executed installed acceptance:** selected only the new strict-refusal and qualified DAG
fault modes in `tests/test_drivers.py`, using the preserved verified wheel. Initial run: **18 passed,
2 failed** because the new restart assertion compared the current Program with the initial approval
snapshot after real remediation. This was a test defect, not a production defect. Corrected comparison
retains the exact proposal/deadline and requires the recovered Program to equal current stored history.
Focused rerun of only the two affected restart modes: **2 passed**. All 20 new cells therefore have
passing local execution, with final whole-suite canonical candidate CI still required. No product fix.
An additional distinct two-case run replaced effect/draft authorization callbacks with failing sentinels:
**2 passed**, proving restart reconciliation/replay does not acquire fresh grants.

Commands used an `env -i` closed environment: synthetic HOME/TMPDIR, `PATH=/usr/bin:/bin`, C.UTF-8,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, plugin autoload disabled and explicit installed
prefix. The wheel interpreter used `-I`; locked `.venv` pytest/xdist dependencies were appended to its
module path, not site-initialized, so the editable product was not loaded. `tests/conftest.py` checks
installed interpreter/package provenance before and after tests. Four xdist workers used separate
per-test databases, workspaces and ephemeral loopback ports; the corrected two-node rerun used two.
Native-local storage validation remained enabled. JUnit files `focused.xml` and `sync-corrected.xml`
were produced in the disposable execution directory; durable scenario definitions are committed here
and reexecuted by the canonical package gate.

The local selection was:

```text
tests/test_drivers.py -k 'requested_unsupported_strict or (qualified_work_dag and (incomplete or unsupported or admission or sync))'
tests/test_drivers.py -k 'qualified_work_dag and sync'  # only after correcting the new test assertion
```

Focused Ruff format/lint, basedpyright and `git diff --check` are delivery checks, not replacements for
canonical CI. No redundant complete local CI run was performed.

## Acceptance Matrix

Evidence levels: **C** = composed actual application lifecycle; **F** = focused contract/adapter test.
Every R1/L1 row is **installed + synthetic peers/state** unless explicitly marked native process or
native tool. R2 is **installed + native tools + synthetic data**. PASS is scoped to the exact versions
above, not universal compatibility. Test files are under `tests/`; helper scripts are under `tools/`.
`Q[mode]` means `test_drivers.py::test_qualified_work_dag_owner_approval_and_reopen[mode]`.

| Cell / requirement | Exact evidence and expected outcome | Level / receipt | Result |
| --- | --- | --- | --- |
| 1. Approve, execute, fix, accept two dependent qualified obligations | Q[completion]: approved original source, first ready alone, rejected exact candidate, new remediation Attempt/findings, corrected exact Jenkins/review subject accepted, second consumes accepted bytes, completed with two satisfactions. No direct synthetic satisfaction injection in this ordinary journey. | C / R1 | PASS |
| 2. Forgejo pull/push same core | `test_ordinary_program.py::test_ordinary_installed_handlers_with_synthetic_external_peers`, pull/managed/mixed x normal/reopen/remediation/lost-reply: two satisfactions, four native sends, same Program/Attempt handlers. | C / R1 | PASS |
| 3. Independent identities and authority | Q[owner-mismatch/completion], wrong/missing per-provider read/write credentials; `test_qualified_dependency_cut_distinct_instances`; wrong Jira instance cannot satisfy edge. No Program/workspace/CI from nonowner approval. | C+F / R1 | PASS |
| 4. Exact owner proposal/material policy/revision | `test_ordinary_program.py::test_host_bound_approval_uses_exact_proposal_and_expected_revision`: missing/tampered scope/digest, stale revision, changed permission/budget/verification/execution and final-guard changes refuse. Q[admission-policy] additionally attempts an actual claim with a changed approved effect policy, preserving history/operations and zero Attempts. | C / R1+L1 | PASS |
| 5. No worker/source/reviewer authority | `test_worker_capability_cannot_issue_owner_commands`; `test_intake.py::test_body_authority_claims_rejected`; `test_remediation_refuses_review_receipt_with_different_subject`. Self-proclaimed owner/approval or different review subject cannot authorize acceptance. | C+F / R1 | PASS |
| 6. Changed source/base/dependencies | Q wrong-base/missing-predecessor/collision checks; Q[admission-source/admission-dependency]: actual request refuses before workspace objects, candidate tooling, CI or review; prior approval/history unchanged. `test_domain.py` transitive applicability tests retain/invalidate exact predecessors, not ticket state. | C+F / R1+L1 | PASS |
| 7. Original freshness, no renewed evidence | `test_qualified_readiness_uses_current_time_without_renewing_evidence`; Q reopen retains earliest baseline age; Q[admission-expired] refuses actual request at original expiry with no Attempt. | C+F / R1+L1 | PASS |
| 8. Incomplete observations/pages are not absence | Q[incomplete-jira/incomplete-inventory] refuses preparation without Program/operations/workspace. `test_qualified_readiness_rejects_incomplete_moving_or_equivalent_inventory` and `test_jira_incomplete_changed_or_hidden_edges_are_not_absence` cover capped/moving/denied/missing/duplicate/contradictory reads. Visible Jira cut remains explicitly incomplete and owner-obligated. | C+F / R1+L1 | PASS |
| 9. Required provider outage/version | Q[baseline-unavailable] refuses unavailable Jenkins baseline; Q[admission-outage] refuses actual claim after approval when Jira is 503. Q[unsupported-version] refuses before Program on unsupported GitHub API version. No fallback or inferred success. | C / R1+L1 | PASS |
| 10. Lost/duplicate start/report/review receipts | `test_lost_start_receipt_returns_unknown_without_redispatch`, `test_lost_report_receipt_is_unknown_without_new_review`, `test_completed_review_receipt_recovers_without_second_prompt`; ordinary fixture original command replay, unchanged sends/review prompt counts. | C / R1 | PASS |
| 11. Exact original external operation after restart | Q[sync-reopen]: lose authorized Confluence PUT reply after first real acceptance, reopen store, recover exact proposal/deadline/current Program and original request digest/fence/one attempt; reconcile original Operation, replay same receipt, exactly one authorized PUT. | C / L1 | PASS |
| 12. Ambiguity never becomes resend/success | Q[sync-ambiguous-reopen]: foreign operation marker keeps original synchronization UNKNOWN after reopen and repeat; no receipt fabricated, no extra PUT or satisfaction, original operation identity/count unchanged. `test_empty_review_search_and_duplicate_marker_are_unknown_not_retry_permission` covers duplicate native markers. | C+F / R1+L1 | PASS |
| 13. Conditional effects and separate authorization | Q[completion] separately authorizes synchronization and Confluence draft effect; driver tests refuse unsupported GitHub conditional mutation before write credentials and changed/revoked/expired grants before final send. An uncertain sync does not erase prior acceptance or imply a transaction with successor readiness. | C+F / R1 | PASS |
| 14. Claim collision, expiry, missed heartbeat | `test_work_ownership.py::test_conflicting_resources_remain_reserved_after_expiry_and_reopen`, `test_renewal_and_reports_never_reanchor_original_wall_boot_horizon`, `test_second_process_cannot_bypass_single_writer_and_retained_claim`; installed `check_work_ownership.py` also permits independent nonconflicting actual-handler work. Expiry is not settlement. | F, native processes / R1 | PASS |
| 15. Revocation and final race | Q[stop/preparation-race]; `test_historical_claim_without_authority_cannot_begin_preparation` (expired, reopened, revoked, generation/revision); `test_revocation_during_preparation_stops_before_next_git_mutation`. No fresh work from historical grants. | C / R1 | PASS |
| 16. Manual edit and workspace ownership | `test_changed_workspace_is_retained_unattributed_and_needs_exact_owner_reconcile`, `test_cleanup_race_preserves_changed_identity_and_nonempty_data`, `test_concurrent_leaf_write_during_retirement_is_retained`; installed ownership fresh-subject and alias conformance rejects tooling edits and conflicting reuse in both modes, preserves bytes. | C+F, native filesystem / R1 | PASS |
| 17. Pull STOP | Q[stop] and `test_ordinary_stop_revokes_authority_without_accepting_or_publishing`: workspace retained, reports/context denied, zero acceptance/publication; `settled=False`. Cooperative quiescence is not managed physical settlement. | C / R1 | PASS |
| 18. Managed exact-target STOP/unknown | Q[managed-uncertain], `test_owner_channel_stops_only_its_original_managed_target`, `test_owner_stop_with_unstarted_managed_binding_remains_unknown`, both reopened managed outcome tests. Original retained target only; missing witness is `cancel_uncertain`, no acceptance or replacement adoption. | C, synthetic app-server/native process / R1 | PASS |
| 19. Descendant, missing witness and controller loss | `test_work_ownership.py::test_real_retained_native_owner_supplies_matching_start_and_settlement_not_cold_adoption`, `test_escaped_scope_quarantine_survives_reopen_and_never_promotes_old_witness`, `test_abrupt_process_exit_retains_original_handoff_and_missing_settlement`; `test_task_execution.py::test_unobservable_owned_group_is_uncertain_without_retry`, `test_partial_owned_cleanup_retains_workspace_and_unproven_stop`. Qualified group retirement versus retained uncertainty, not universal escaped-descendant termination. | F, native processes plus synthetic witness faults / R1 | PASS |
| 20. Deadline STOP uses settlement gate | `test_task_execution.py::test_cancellation_after_terminal_verification_is_durable`, `test_false_cancellation_poll_crossing_deadline_cannot_authorize_late_effect`, `test_long_verification_cannot_extend_original_deadline`; `test_reference_live_expiry.py::test_refusal_inside_admission_uses_durable_stop_and_qualified_cleanup` (offline synthetic runtime). Terminality/interrupt alone never supplies witness. Existing clock/STOP receipts do not prove strict syscall expiry. | C+F / R1 | PASS |
| 21. Restart/reconciliation never new execution | Ordinary reopen modes, `test_original_host_command_receipt_survives_producer_process_death`, `test_drivers.py::test_terminal_receipt_repairs_exact_settlement_after_crash_reopen`, Q[sync-reopen]. Original IDs/handles/fences/unknowns retained; no numeric-PID adoption or new uncertain send. | C+F, native process death / R1+L1 | PASS |
| 22. Finite whole-work budget/capacity | Installed `check_work_budget.py`; `test_work_budget.py::test_project_capacity_atomic_across_programs_and_reopen`, `test_concurrent_store_owner_cannot_oversubscribe`; atomic reservation with Attempt/outbox, bounded attempts/review/verification/retry/auxiliary/output, no partial oversubscription. | C+F / R1 | PASS |
| 23. Clock/unknown headroom/strict inference | `test_strict_profile_refused_and_observed_ceiling_cannot_erase_unknowns`; ownership clock tests and budget acceptance initial/amendment strict refusals. Boot mismatch/rollback/unavailable original projection/expiry refuse; estimates/unknowns never release unproved resource headroom. | F / R1 | PASS |
| 24. Known/estimated/unknown costs and corrections | `test_work_budget.py::test_unitful_usage_dedupe_corrections_and_unknowns_survive_reopen`, `test_outcome_export.py::test_resource_provenance_and_units_are_never_conflated`; installed `check_outcome.py`: USD 2 corrected to 4, subtotal 4, total unknown; estimates/provider/owner provenance and currencies/units stay separate. No inferred billing or zero hidden work. | F / R1 | PASS |
| 25. Exact private outcome/review lineage | `test_private_outcome_export_uses_actual_shared_producers`, `test_substantive_review_findings_reach_remediation_author`, `test_partial_outcome_evidence_survives_reopen`; actual ordinary pull/managed producers retain candidate/Attempt/check/review/remediation/satisfaction/effect and immutable corrected-usage lineage. | C / R1 | PASS |
| 26. Privacy/export custody/refusal | Installed `check_outcome.py`, `test_outcome_export.py`: private identifiers/content excluded, unavailable source rights do not grant sharing, explicit policy required, 0600 no-overwrite publication, link/fsync uncertainty retained; reading/export does not issue effects. No automatic upload. | F, native filesystem / R1 | PASS |
| 27. Owner-served commands and disconnect | Installed `check_served.py`, `test_served_local.py::test_payload_cannot_claim_owner_identity_or_submit_command`; `test_served_host.py::test_revoke_active_launched_clients_without_revoking_work`; exact-revision owner/worker/observer capabilities, no second DB reader. Descriptor/UI closure revokes communication, not execution or settlement. | C+F, installed client/native socket/process / R1 | PASS |
| 28. Events/reconnect/gap/snapshot | `test_served_progress.py::test_replay_is_scoped_bounded_and_read_only`, `test_restart_scope_and_future_position_require_resync`; `test_served_host.py::test_owner_process_death_and_fresh_process_require_resync`; installed served paged/chunked snapshots and stale-cursor refusal. Wrong epoch/scope, overflow/state change or restart requires snapshot; reads never dispatch/renew. | F, installed client/native process / R1 | PASS |
| 29. Served privacy and notifications | `test_invalid_event_is_not_appended`, `test_snapshot_exposes_only_scoped_command_receipt_state`, `test_claim_projection_isolates_programs`; installed local notification/dedupe/drop behavior. No arbitrary private payload or other-Program claims; sampled history/lossy hints never claim durable global completeness. | F / R1 | PASS |
| 30. Actual historical readers and interrupted state migration | `check_schema_upgrade.py` builds pinned real old wheels; `test_sqlite_schema_upgrade.py::test_migration_interruption_rolls_back_then_reuses_atomic_original_backup`, `test_actual_old_reader_refuses_new_schema_without_dispatch_or_record_mutation`, `test_failed_backup_never_installs_the_new_authority_contract`; original rows/bytes/handles and complete artifact inventories preserved; schema 6 reader refuses 7, backups reused after rollback. | F, installed old/current wheels/synthetic state / R1 | PASS |
| 31. Platform/profile migration and refusal | R2 real SOPS/age test; R1 storage tests, schema 999/unversioned-populated refusal, `check_uninstall.py` byte/mode/mtime retention. Native local Linux only; DrvFS/9p/overlay/split sidecars/non-Linux refuse. Unsupported actual platforms/recipient changes stay unreceived, not destructive repair. | F, synthetic topology/native tools / R1+R2 | PASS |
| 32. Explicit requested unsupported strict profiles | `test_drivers.py::test_requested_unsupported_strict_profile_refuses_before_recording`, ten demands: route, exact pin/admission, no hidden calls, hard inference quota, physical settlement on pull, provider-key secrecy, shell egress, outside-workspace reads, strict native expiry, escaped containment. Each unknown authority-bearing field is refused by the shipped closed basic-profile constructor before evidence/Program/operation creation. These are deliberately unsupported inputs, not newly defined profile selectors or proof that arbitrary prose can be semantically classified. Existing strict budget/isolation/version tests add typed boundary refusals. | F / R1+L1 | PASS |
| 33. Optional native/strong isolation profiles | Native Kilo/real Codex, verification-only bubblewrap opt-ins and historical OCI experiments are not the selected basic synthetic acceptance profile. Their skipped tests do not pass, and ADR 0008 cannot become whole-worker/reviewer isolation. #53 remains CLOSED/NOT IMPLEMENTED, PR #85 CLOSED/UNMERGED. | Explicitly outside selected profile | NOT APPLICABLE |
| 34. Live AC6 / product GO | No authorized real account, artifact-scoped network/effect grant or numeric envelope. No live task/inference/provider mutation/Console deployment/release was attempted. Proposed bounded gate below; owner retains authorization. | Live evidence missing by design | UNVERIFIED |

No applicable offline cell remains FAIL or UNVERIFIED. Row 34 is a binding outstanding **live** gate,
not silently reclassified as N/A or an offline pass. Row 33 does not remove any selected managed-profile
STOP/unknown obligation in rows 18-21.

## Why Separate Receipts Suffice

The heterogeneous success journey must accept a candidate and enable its dependent; a STOP/expired/
incomplete journey must refuse those very transitions. These cannot be one physical execution with
one terminal outcome. The same installed Program/WorkUnit/Attempt, claim, operation, review and outcome
handlers are exercised under independently qualified fixtures. No second lifecycle or acceptance
application is introduced. All new missing composed cases extend the existing qualified DAG fixture.

Costs, private export, owner-served events and migrations are independently configured orthogonal
contracts already received on the installed ordinary/shared producers. Reexecuting every fixture in a
single Jira journey would neither prove stronger native semantics nor improve identity provenance.
Receipt recovery cannot renew approval; acceptance and synchronization remain separate. In particular,
an accepted first WorkUnit can remain accepted/enable a dependency while its independently authorized
Confluence publication is unknown; that unknown must not be reported as publication success.

Native process tests use real local process lifetime behavior with synthetic executables/evidence,
not a live model. Tests that inject an escaped/unobservable witness establish quarantine/refusal, not
that every actual escape can be detected. Local effects during managed termination remain possible.
There is no universal atomicity, physical settlement, hostile isolation, observed model identity,
hard inference/cost cap, live compatibility or rollback claim.

## Historical Reader Pins

The installed compatibility gate retains actual producers/readers, not rewritten synthetic validators:

| Boundary | Exact historical source |
| --- | --- |
| Schema 2 | `e2348656238a2ff9a3244868e2beb543d86f15a5` |
| Schema 3 | `23a28e31c17018acb9900efeed2da1b9b03330cf` |
| Schema 4 | `b11aae420517b14bbb4d85c84f9a0d82c14446c6` |
| Initial ownership | `b9aae79d36e1a1ece046dd565a07be028c8f865a` |
| Prequalification | `3aa2b8f0232892ddfc61545bff7c646176253628` |
| Accepted history | `88d59fcd594cbf7fd529971a12139628bef9b6a5` |
| Prepublication authorization | `3ebd84a28c6c3bfcf6ed7f4b1937fc425ae05b13` |
| Pathname-only ownership | `397ee0cb6a11f0638b9b19f48076f34d1d1c69b1` |
| Preordinary/schema 5 | `b1c9ffca16f06596e5c3afdca3b9893cf7422d8d` |
| Schema 6 | `1bcb052e527933b7b2f419ac76017f2d20e88a1e` |

These finite pins do not promise arbitrary old schema/profile compatibility. Recovery uses the current
reader on a new offline restore path, never downgrade permission or automatic real-owner migration.

## Proposed Live Authorization

**Proposal only, not executed or authorized.** After independent offline acceptance/integration, bind
the authorization to the exact integrated report/test commit, unchanged product source and wheel hash
above, plus exact deployed native service/plugin versions discovered read-only. If any differs, stop
and obtain a revised compatibility decision; do not extrapolate the synthetic profile.

| Authorization dimension | Proposed bounded choice and observations |
| --- | --- |
| Environment/resources | One disposable owner-controlled Linux/native-ext4 host/account and one disposable Jira project, private GitHub repository/branch, Jenkins job and Confluence space/two pages. Synthetic noncustomer source/task/context only. No existing owner's IDE session or production workspace. |
| Accounts/credentials | Owner-provisioned separate least-privilege service identities: Jira issue/link read; GitHub exact repo/ref/PR read and review-comment write only if explicitly selected; Jenkins exact job/build read; Confluence exact context read and disposable output-page versioned replace plus explicit possible draft merge/overwrite grant. No admin, merge, release or deploy scope. Credentials remain trusted-host callbacks, never records/prompts/worker environment. |
| Execution choice | Pull-first existing trusted local client, manually produce the two small candidate edits and one substantive remediation. Fresh separately scoped product-review peer. No paid/live inference in this first proposed provider gate: zero model calls and model credentials. Any later native model reception needs another explicit exact harness/model/account grant; do not call deterministic review proof semantic model acceptance. |
| Network/effects | HTTPS only to the four exact configured service origins, plus exact Git source read if needed. One original Program, two obligations, at most three Attempts and three product-review requests; one authorized Confluence replacement and read-only reconciliation. No Git push/PR creation/merge, CI trigger, Jira update, other tenant/repository, public upload or automatic cleanup. |
| Numeric envelope | At most 45 minutes original task horizon, 3 Attempts, 3 reviews, 1 state-changing provider request, 100 bounded read requests, 1 MiB retained output and USD 0 incremental paid inference/service spend. Owner must confirm existing disposable service capacity has no incremental charge; otherwise this proposal cannot run. Expiry does not renew on restart. |
| Privacy/trust | Only generated fixture content; no private owner records/customer data/production credentials. Explicit trusted same-principal profile, not hostile isolation or provider-key secrecy. Private local export only, 0600; share only a separately approved redacted receipt. |
| STOP/unknown | Revoke Kernel admissions immediately, retain cooperative task resources and unknown effects, do not kill shared clients. If managed execution is separately selected, require exact original owned-target settlement. Lost provider replies reconcile the original operation read-only within the same read/time budget; no resend, replacement, inferred absence or deadline extension. Exhaustion retains unknown and stops. |
| Cleanup | Retain evidence and unknown targets. Owner inspects disposable resource IDs and separately authorizes deletion after reconciliation; this proposal grants no destructive cleanup. Local empty owned-workspace retirement only with exact settled/reconciled evidence. |
| Pass observations | Exact provider identities/versions/scopes and independent credential refusals; original approved source/proposal/policy; ordered two-obligation execution; negative then fresh corrected candidate review/CI; retained operation identity and one provider write; native conditional conflict/ambiguous outcome behavior; original horizon/STOP; private export/cost unknowns; owner reconnect/gap/snapshot. Live receipt must name actual results and failed/unavailable cells, not replace them with offline receipts. |

Until a separate owner decision supplies and accepts this exact scope and the remaining live evidence,
#60 stays open. There is no Product GO, `main` promotion, release or deployment request in this report.
