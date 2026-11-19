# Issue 60: Qualified Export Recovery And Offline Acceptance

## Subject And Authority

This is the distinct owner-authorized recovery of [#60](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60),
2026-11-16. The owner authorized the bounded exporter correction, selective reuse of the rejected
candidate, a fresh PR from current develop and **at most four** independent whole-PR recovery reviews.
This is not review 11 or a reset: original #60 reviews remain permanently **10/10 consumed**.

[PR #135](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/pulls/135) is CLOSED/UNMERGED. Its branch
`issue-60-offline-acceptance`, commit `b2c61bff190575977403e4cda28c054e6912bab6`, 34-cell report,
20 added tests, review9 COMMENT and review10 REQUEST_CHANGES are preserved. The authoritative
[rejection/corrected disposition](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-22102)
is not superseded by retroactive approval. This report replaces the invalid completeness claim for
the new recovery only; it does not import the rejected verdict as approval.

- Canonical repository: `https://forgejo.creatidy.com/Creatidy/creatidy-kernel`.
- Fetched recovery base: `5e6f42906980fd42293e0dab1be3d7942532ff81`.
- Recovery branch: `issue-60-qualified-export-recovery`.
- Product/test/report subject: exact commit containing this report, bound by the recovery PR,
  canonical CI actual checkout and independent review. A commit cannot embed its own SHA.
- Package: `creatidy_kernel 0.0.1.dev0`, Python >=3.12, no new runtime dependency or schema migration.
- Tested final wheel SHA-256: `62fa458d23ce0bc4f91dd31c6a47db3a008d59f193bf23b612e543d1093c2eb8`.
  All 77 installed package files equal that wheel and this candidate's `src/` bytes.
- Baseline wheel SHA-256: `f3652660751f67dade5d960c6293fa38993dfc53cfb45c53dea8f29e197a045b`,
  reproduced from the fetched base, matching the original rejected delivery's product.
- Disposable installed execution root: `/tmp/kilo/ck60-recovery`, outside the checkout; baseline and
  final wheels are independently installed under `baseline-venv` and `final-venv`.

The offline candidate below is complete for independent review, not self-approved. `OFFLINE_ACCEPTED`
requires exact successful current canonical CI, fresh independent approval, verified integration and
the corresponding canonical #60 receipt. It does not imply live acceptance or Product GO. #60 remains
OPEN while binding live AC6 is unmet. No release, deployment, `main` promotion or live effect is authorized.

## Binding Criteria And Prerequisites

Use the current issue and [November 8 reconciliation](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-21185),
not the superseded strict AC4 or old #105 deferral. ADR [0011](../adr/0011-task-level-authority-and-trusted-development.md)
and [0012](../adr/0012-unified-work-coordination-and-drivers.md) retain pull-first task authority,
profile-specific settlement, independent external identities and one lifecycle.

| AC | Binding scope |
| --- | --- |
| 1 | Actual installed ordinary handlers outside checkout: owner approval, ordered obligations, claim/handoff, verification, review/remediation/acceptance and separately authorized synchronization. Pull-first; supported managed push uses the same core, no mandatory harness. |
| 2 | Historical Forgejo behavior and independently qualified Jira/GitHub/Jenkins/Confluence bindings, including partial/refusal/migration. Synthetic peers do not establish real-account compatibility. |
| 3 | Changed/stale source, policy or dependencies; incomplete reads/outages/capabilities; lost/duplicate/ambiguous effects; claims/expiry/collisions/manual edits; pull STOP versus managed settlement; reconnect/gaps and privacy. |
| 4 replacement | Unsupported strict route/pin/admission/no-hidden-call/isolation guarantees remain unsupported/unknown; requests refuse. #52/#53 are not reopened and exact-version negative experiments are not universal harness claims. |
| 5 | #56 costs/unknowns/corrections, #58 private export/provenance, exact review/remediation, #57 served authorization/events, #59 ownership and #62 finite installed compatibility/migration. Qualified composition must actually export; legacy success alone is insufficient. |
| 6 | Separate owner-controlled live authorization for exact artifact/environment/accounts/network/effects/privacy and finite numeric time/attempt/money exposure, STOP/unknown and cleanup. Not granted by offline tests. |

Fresh native dependency enumeration has the same eight predecessors below. Their closed state alone is
not proof: accepted result/PR/source/test evidence and merge ancestry are required. All listed merges
are ancestors of the fetched base. No dependency is invented from prose and no unrelated #55 work is selected.

| Predecessor | Integrated source / durable receipt |
| --- | --- |
| #105 | Merge `20de425eb01e2467151b0e1994ef02eed493626b`, [PR111](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/pulls/111), reviewed head `1e9f0f97c65ccb540d488be1c9733b605e1e4dc3`, CI317. Issue21706 says unmerged and is stale; actual merged metadata/tree/ancestry and installed conformance establish integration, not that comment. |
| #106 | Merge `fce1c0d4d63fdf4ca0673512c9b9d2512f0ac023`, [receipt20200](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/106#issuecomment-20200). Ordinary Forgejo pull/managed/mixed, not live inference. |
| #56 | Merge `b11aae420517b14bbb4d85c84f9a0d82c14446c6`, [receipt19417](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56#issuecomment-19417). Finite atomic whole-work capacity and sourced unknowns/corrections, not hidden billing enforcement. |
| #57 | Merge `da9c736fe61ba33c3d1889f15949948928eb4dea`, [receipt20654](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57#issuecomment-20654). Local owner-served state/commands/events, not deployed Console or remote IAM. |
| #58 | Merge `97fedb7e3c67026c5987a2806a419cef914fdad7`, [receipt20902](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58#issuecomment-20902). Legacy private export/provenance; its missing qualified composition is corrected here, not assumed. |
| #59 | Merge `b1c9ffca16f06596e5c3afdca3b9893cf7422d8d`, [receipt19620](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/59#issuecomment-19620). Claims/retained conflicts and qualified settlement, not escaped-process containment. |
| #62 | Merge `4cb500a6d58d983854ad795a56db0af3c6d902f8`, [receipt20960](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/62#issuecomment-20960). Finite reader/profile/platform matrix, not all platforms or actual owner-key migration. |
| #133 | Merge/base `5e6f42906980fd42293e0dab1be3d7942532ff81`, [receipt22062](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/133#issuecomment-22062). Qualified ordinary DAG/schema7, not full #60 or semantic model quality. |

## Defect And Correction

Before any product edit, rebuild/install the unchanged base and run the existing full qualified fixture
with actual handlers. It reaches COMPLETED with two satisfactions, closes/reopens SQLite, then production
`export_private_outcome` raises `ValueError: original ordinary proposal belongs to another Program`.
History/Operations remain unchanged. The first attempt to reuse old `/tmp` artifacts failed because
the disposable path no longer existed; fresh offline locked builds reproduced the same hash and failure.

The former exporter accepted only proposal v1 and legacy numeric drafts. The correction shares the
actual strict `proposal_from` decoder with ordinary recovery, including canonical byte equality and
version-specific fields: v1 legacy drafts/dependency evidence; v2 additionally qualified dependency cuts
for the otherwise legacy WorkUnits; v3 additionally actual qualified drafts. Unsupported versions,
unknown fields, malformed/noncanonical bodies and inconsistent producer facts still refuse.

Export binds the stored proposal digest and each original meaning input to its exact Attempt. Qualified
work and independent SCM subjects retain their opaque identity, driver/binding/version/API/instance/
tenant/namespace/revision, source/cut/baseline provenance and dependency correlation through HMACs.
There is no fabricated numeric conversion. Legacy fields remain legacy, not reinterpreted as Jira.

Qualified effects use the existing native-effects projection, not another exporter/model. Original
ownership-publication evidence is read through the existing store evidence port. Validate the original
claim/Attempt/generation, reservation, immutable Operation/effect/request digest and accepted satisfaction;
validate any receipt's exact operation tuple/status/reference. An absent receipt remains absent/unknown,
never a current remote read or inferred success. No effect or authority is created during export.

All new identifying/free-form strings are HMAC-pseudonymized under the existing owner-local
`private-metadata-v1` policy. Only closed codecs/provider families/statuses, counters and observed sourced
quantities are public metadata. No raw keys, provider endpoints, mutation bodies/authority, prompts,
findings text, credentials, source content or workspace paths are exported. Costs remain unitful and
attributed; corrections do not rewrite original observations and estimates/unknowns never become totals.

## Versioned Profiles

All adapters are the exact source/wheel above, not independently released adapter packages.

| Profile / producer | Version and assurance |
| --- | --- |
| Ordinary pull | Closed basic `kilo-pull`, `trusted_development`, cooperative task-scope quiescence, not control of the user's process. |
| Managed push | Synthetic Codex-shaped `0.99.1` app-server with actual retained local owned process groups. Not native Codex conformance, detached supervision or hostile isolation. |
| Product review | Real `NativeKiloReview` receipt/verification/remediation flow with fresh read-scoped synthetic Kilo peers, exact candidate and programmed negative then corrected verdict. Not model-review quality or native Kilo7.8.25 conformance. |
| Work | `jira-cloud` implementation1 / REST3, `ENG-41` and `ENG-42`; explicitly visible-scope dependencies plus owner obligation, not exhaustive native enumeration. |
| SCM | `github` implementation1 / REST `2026-03-10`, bounded stable scans and exact local Git objects, not atomic remote snapshots or unsupported conditional writes. |
| CI | `jenkins` implementation1 / `jenkins-2.479.3;git-5.7.0;git-client-6.1.0`; exact build/source/recipe mapping from trusted host, not SUCCESS-as-acceptance. |
| Context/sync | `confluence-cloud` implementation1 / REST2, exact actor/page/version and explicit possible native draft-effect grant; no native universal draft CAS or cross-driver transaction. |
| Retained Forgejo | Existing version1 facade/numeric subject and synthetic HTTP/AGit; not live provider receipt. |
| State/presentation | SQLite schema7/history codec1, owner-served local wire1; exact finite historical readers via `check_schema_upgrade.py`. No new database migration. |
| Native migration | Existing SOPS3.12.2/age1.1.1, generated synthetic keys/profile and installed CLI; no owner keys, actual customer data or recipient migration. |

## Execution Evidence

**I, new installed regressions:** initial corrected wheel
`d9bbf15c11410b275404bd9a6f97161980590341924208fb93f076c8d826fe50`,
`tests/test_drivers.py` and `tests/test_outcome_export.py`: **215 passed, 0 failed/skipped**,
138.673 seconds in the JUnit receipt. Four xdist workers use disjoint
per-test native-local SQLite directories/workspaces and loopback ports. This is focused exporter and
adapter validation, not a redundant complete local CI run. `tests/conftest.py` enforces the actual
installed interpreter/package origin before and after the test session.

After tightening original owner/claim/publication evidence and preserving the exact legacy v1 subject
shape (no empty qualified-dependency field), a distinct final wheel was built once: the final hash above.
Focused final-artifact reception: **6 passed, 0 failed/skipped**, 47.301 seconds, covering actual v1
effect/export, actual v2 proposal/Attempt, all three full v3 export/reopen/unknown variants including new
publication-corruption refusals, and native SOPS/age/installed CLI. This repeats only the scenarios needed
to verify those changed conditions and final artifact; the complete local gate is not repeated.

The enhanced existing `test_qualified_work_dag_owner_approval_and_reopen` composes genuine work/candidate/
verification/review/remediation/acceptance and authorized synchronization. `completion` receives export
after final SQLite reopen; `sync-reopen` additionally reopens during lost-reply reconciliation with
failing fresh-authorization sentinels; `export-unknown` completes accepted work while retaining an
independently unknown publication. All export after final reopen, with two distinct qualified WorkUnits,
three Attempts, full findings union, accepted lineage, one effect dispatch, stable repeated private export,
and known2 -> corrected4 / estimated7 / unknown USD facts. Subtotals remain separated and totals unknown.
Export changes neither SQLite change token, Program history, Operations nor budget accounting.

Historical v2 is produced/approved by `test_qualified_cut_owner_approval_and_recovery`, then a real
PrepareAttempt command binds the approved inputs/context and exports its legacy subject plus qualified
dependency evidence. It is partial compatibility evidence, not fabricated accepted work. Actual v1 full
Forgejo/export journeys are exercised in `test_outcome_export.py`; the shared decoder also round-trips
the actual v1 producer. Malformed/unsupported/noncanonical proposals, foreign claim/Attempt metadata and
wrong receipt Operation refuse, without durable mutation. Existing legacy privacy/refusal tests also pass.

All 20 valid negatives from rejected `b2c61bff` were selectively reused in the existing fixtures, with
provenance retained. No invalid acceptance conclusion was reused. During development, representation
checks were corrected to compare actual in-memory claim tuples with `asdict` (not JSON lists), and a new
test assertion was corrected to require the full verification-plus-review findings union rather than
review-only findings. These were caught before installed reception, not weakened product assertions.

**N, new exact-wheel native migration:**
`test_operator_profile.py::test_real_sops_age_profile_is_read_only_and_missing_key_refuses`, **1 passed**,
real existing SOPS/age and installed CLI, synthetic key/profile, ciphertext preservation, missing key,
atomic migration and interrupted staging/fsync rollback. This distinct current-artifact proof replaces
reliance on the earlier local native receipt where CI skips the binaries.

Runs use `env -i`, synthetic HOME/TMPDIR, minimum PATH, C.UTF-8, disabled Git owner config/plugin autoload,
isolated wheel interpreter `-I`, and explicit installed prefix/CLI. Existing locked pytest dependencies
are appended without editable-site initialization. No owner credentials/profile are inherited. JUnit
scratch paths are `installed-recovery.xml`, `native-migration.xml` and `final-installed.xml` under the disposable root above;
committed test definitions and canonical CI are durable/reproducible evidence, not reliance on `/tmp`.

**H, selectively reused installed receipts:** [CI332](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/actions/runs/332)
on base `5e6f4290` (2695 passed/1 skipped/106 subtests installed) and
[CI333](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/actions/runs/333) on rejected test-only
`b2c61bff` (2715 passed/1 skipped/106 subtests installed; serial17 passed/97 skipped). These establish
unchanged bounded STOP/budget/event/migration components, not the formerly missing qualified export.
The current recovery still requires its own exact-head full canonical `make check`, `make package-check`
and `make audit` before review/integration; the #60 integration receipt must identify actual checkout,
gate outcomes, package/test subject and independent recovery verdict. Old CI never substitutes for it.

## Complete Offline Matrix

`Q[mode]` abbreviates `test_drivers.py::test_qualified_work_dag_owner_approval_and_reopen[mode]`.
**C** is composed actual application lifecycle; **F** is focused contract evidence. I/H use installed
packages and synthetic peers/state, including native filesystem/process behavior where stated. N uses
native SOPS/age with synthetic data. PASS is exact bounded evidence, not live/universal compatibility.

| Cell / required behavior | Exact test / receipt / strength | Result |
| --- | --- | --- |
| 1. Two ordered qualified WorkUnits through review/fix/accept/sync | Q[completion/sync-reopen/export-unknown]: genuine handlers, three Attempts/two satisfactions; I/C. | PASS |
| 2. Forgejo pull/push/mixed same core | `test_ordinary_installed_handlers_with_synthetic_external_peers` and retained `ordinary_flow_fixture` variants; H/C, current full package CI required. | PASS |
| 3. Independent identities/owner-only approval | Q[owner-mismatch], per-provider separate read/write credential refusals, `test_qualified_dependency_cut_distinct_instances`; I/C+F. | PASS |
| 4. Exact proposal/material policy | `test_host_bound_approval_uses_exact_proposal_and_expected_revision`, Q[admission-policy]; H+I/C. | PASS |
| 5. No worker/source/reviewer authority | `test_worker_capability_cannot_issue_owner_commands`, intake body-authority refusals and `test_remediation_refuses_review_receipt_with_different_subject`; H/F. | PASS |
| 6. Changed source/base/dependencies | Q[admission-source/admission-dependency] actual request refuses with no Attempt/workspace/CI; original wrong-base/missing-predecessor checks; I/C. | PASS |
| 7. Stale approval/evidence never renewed | Q[admission-expired/sync-reopen], `test_qualified_readiness_uses_current_time_without_renewing_evidence`; I/C+F. | PASS |
| 8. Incomplete observations/pages not absence | Q[incomplete-jira/incomplete-inventory], `test_qualified_readiness_rejects_incomplete_moving_or_equivalent_inventory`, Jira hidden-edge tests; I/C+F. | PASS |
| 9. Outage/version/unsupported capabilities | Q[baseline-unavailable/admission-outage/unsupported-version] and adapter capability refusals; I/C. | PASS |
| 10. Lost/duplicate execution/report/review receipts | Ordinary `test_lost_start_receipt_returns_unknown_without_redispatch`, `test_lost_report_receipt_is_unknown_without_new_review`, `test_completed_review_receipt_recovers_without_second_prompt`; H/C. | PASS |
| 11. Original effect through restart | Q[sync-reopen]: digest/fence/one dispatch, retained exact receipt, no fresh grant; final reopened export preserves original Operation/satisfaction; I/C. | PASS |
| 12. Unknown/duplicate markers never blind replay | Q[sync-ambiguous-reopen/export-unknown], duplicate-native-marker refusal tests; unknown export has no receipt/current-remote success or second PUT; I/C+F. | PASS |
| 13. Separately authorized native effects | Q[completion] exact Confluence effect/draft grants and unsupported GitHub conditional-write refusal before credential; I/C+F. | PASS |
| 14. Expiry/heartbeat/concurrent ownership | `test_conflicting_resources_remain_reserved_after_expiry_and_reopen`, `test_renewal_and_reports_never_reanchor_original_wall_boot_horizon`, `test_second_process_cannot_bypass_single_writer_and_retained_claim`, `check_work_ownership.py`; H/F with native processes. | PASS |
| 15. Revocation/final preparation race | Q[stop/preparation-race], `test_historical_claim_without_authority_cannot_begin_preparation`, `test_revocation_during_preparation_stops_before_next_git_mutation`; I+H/C. | PASS |
| 16. Manual edits/retained resources | `test_changed_workspace_is_retained_unattributed_and_needs_exact_owner_reconcile`, cleanup/retirement race tests and installed ownership conformance; H/F native filesystem. | PASS |
| 17. Cooperative pull STOP | Q[stop], `test_ordinary_stop_revokes_authority_without_accepting_or_publishing`: retained workspace, no acceptance/publication, settled=False; I+H/C, not process retirement. | PASS |
| 18. Managed exact-target STOP/missing witness | Q[managed-uncertain], `test_owner_channel_stops_only_its_original_managed_target`, `test_owner_stop_with_unstarted_managed_binding_remains_unknown`, reopened managed outcomes; I+H/C/native groups. | PASS |
| 19. Descendants/escape/controller-loss uncertainty | `test_real_retained_native_owner_supplies_matching_start_and_settlement_not_cold_adoption`, `test_escaped_scope_quarantine_survives_reopen_and_never_promotes_old_witness`, `test_abrupt_process_exit_retains_original_handoff_and_missing_settlement`; H/F native process/synthetic witness faults, not universal escape detection. | PASS |
| 20. Deadline STOP/verification horizon | `test_false_cancellation_poll_crossing_deadline_cannot_authorize_late_effect`, `test_long_verification_cannot_extend_original_deadline`, `test_refusal_inside_admission_uses_durable_stop_and_qualified_cleanup`; H/F, not strict syscall expiry. | PASS |
| 21. Restart never replacement execution | Q[sync-reopen], `test_original_host_command_receipt_survives_producer_process_death`, `test_terminal_receipt_repairs_exact_settlement_after_crash_reopen`; I+H/C+F. | PASS |
| 22. Whole-work finite atomic budget | `check_work_budget.py`, `test_project_capacity_atomic_across_programs_and_reopen`, `test_concurrent_store_owner_cannot_oversubscribe`; H/F/native ownership. | PASS |
| 23. Original clocks/unknown headroom/strict refusal | `test_strict_profile_refused_and_observed_ceiling_cannot_erase_unknowns`, budget amendment/refusal and ownership original-clock tests; H/F. | PASS |
| 24. Qualified known/estimate/unknown/corrected usage | Q[completion/sync-reopen/export-unknown] records real usage against original effect/Attempt, immutable known2->corrected4, estimate7 and unknown, separate sourced subtotals/no totals; I/C. Replaces previous qualified UNVERIFIED. | PASS |
| 25. Qualified private export and immutable lineage | Same full qualified journeys plus final SQLite reopen, production exporter, two distinct work subjects/one SCM source, exact review/findings/remediation/acceptance/effect/receipt relationships, stable HMAC; I/C. Replaces previous FAIL with actual composed evidence, not legacy inference. | PASS |
| 26. Qualified privacy/refusal/custody | Same journeys reject raw opaque keys/revisions/origins/credentials/content/paths; corrupt proposal/foreign claim/wrong receipt refuse without history/operation/budget/change-token mutation. Existing `test_outcome_export.py` private policy/0600/no-overwrite/link/fsync/rights negatives also pass; I/C+F. | PASS |
| 27. Owner-served authorization/disconnect | `check_served.py`, `test_payload_cannot_claim_owner_identity_or_submit_command`, `test_revoke_active_launched_clients_without_revoking_work`; H/C+F, actual local client/socket. UI closure is not task settlement. | PASS |
| 28. Event reconnect/gap/snapshot recovery | `test_replay_is_scoped_bounded_and_read_only`, `test_restart_scope_and_future_position_require_resync`, `test_owner_process_death_and_fresh_process_require_resync`, installed chunked snapshots/stale cursor; H/F/native process. | PASS |
| 29. Served privacy/lossy hints | `test_invalid_event_is_not_appended`, `test_snapshot_exposes_only_scoped_command_receipt_state`, `test_claim_projection_isolates_programs`, installed notification/dedupe/drop; H/F, not global durable stream. | PASS |
| 30. Actual historical readers/interrupted upgrade | `check_schema_upgrade.py` builds exact pinned old wheels; `test_actual_old_reader_refuses_new_schema_without_dispatch_or_record_mutation`, `test_migration_interruption_rolls_back_then_reuses_atomic_original_backup`, failed-backup refusal; H/F actual old/current installed packages, current CI repeats required gate. | PASS |
| 31. Native profile/platform/uninstall | N exact current installed SOPS/age+CLI; H storage topology/schema999/unversioned refusal and `check_uninstall.py` byte/mode/mtime preservation. Linux native storage only; unsupported topology/platform stays refused, not received. | PASS |
| 32. Explicit unsupported strict demands | `test_requested_unsupported_strict_profile_refuses_before_recording`, all ten reused closed-shape demands: routing/pin/admission/hidden-call/quota/physical pull settlement/keys/egress/reads/expiry/escaped containment; I/F. Unknown fields are deliberately refused, not new selectors or semantic prose classification. | PASS |
| 33. Optional native/hostile-isolation profiles | Real Kilo7.8.25/Codex and bubblewrap/OCI experiments outside chosen basic synthetic profile; skipped tests not passes. #53 CLOSED/NOT IMPLEMENTED and PR85 CLOSED/UNMERGED retained. | NOT APPLICABLE |
| 34. Separately controlled live AC6 | No real account execution/credential/effect grant or accepted live receipt. No paid inference/Console deployment/release/GO. Owner proposal below only. | UNVERIFIED (LIVE) |

The discovered defect invalidated the older export-composition justification, now replaced by actual
qualified end-to-end assertions in cells24-26 and effect restart assertions in cells11-12/21. Other
cells retain bounded evidence, not a claim that distinct terminal negatives ran in one physical journey.
An expired/stopped/incomplete journey cannot simultaneously be the successful two-obligation journey.
Budget/served/migration tests independently exercise the same installed core/producer contracts; this
does not excuse a newer-format consumer from its own composed integration proof.

Current canonical exact-head gates and independent review are still required to conclude acceptance.
No offline FAIL/UNVERIFIED is concealed as live-only or optional. A newly discovered applicable failure
must replace the relevant PASS and stop integration until resolved within the four-review recovery budget.

## Remaining Live Authorization

The earlier live envelope is **proposal only**, not executable authorization: exact independently
approved integrated artifact/hash/version, disposable owner-controlled Linux/native-local host and
one Jira project/GitHub repo/Jenkins job/Confluence space with synthetic noncustomer data. Separate
least-privilege credentials and exact origins/scopes are required; no production/owner data or keys.
Proposed maximum45min original horizon,3 Attempts,3 product reviews,one external write,100 reads,
USD0 incremental paid inference. Owner must confirm disposable service capacity has no incremental
charge. No model call/account authorization is implied; any native model reception needs its own grant.

STOP revokes new Kernel work; cooperative clients retain unknown resources without killing shared user
sessions. Lost replies reconcile the original operation read-only within the original limits, never
resend, replacement or expiry renewal. Any managed mode needs its own original target settlement.
Retain uncertain targets/evidence; destructive external cleanup requires separate owner approval.
No Git push/PR merge, CI trigger, Jira update, other tenant, public upload or deployment is allowed.

Live pass observations must include actual service/plugin versions and credential refusals, exact
source/proposal/material policy, ordered verification/review/remediation and acceptance, original effect
identity/conditional conflict/unknown handling, STOP, private export/cost unknowns and reconnect/gap/
snapshot. Changed artifact/service/profile requires a revised decision, not synthetic extrapolation.
Offline acceptance, live acceptance and Product GO remain three different conclusions.

## Provider-Composable Offline Reception (2026-11-19)

This is the distinct [scope decision](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-22223)
and [four-review delivery](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-22466),
not a revision of the historical report above. PR #135 remains rejected/unmerged (10/10 consumed);
PR #136 remains accepted for its own bounded export recovery (1/4 consumed). Neither is approval of
this candidate. This delivery changes fixtures and evidence only; no package source, runtime
dependency, ADR, storage schema or workflow changed.

- Source base: `959167ddc2dbd9bb48c620932e41207442844eab`, exact fetched canonical develop.
- Installed wheel: `creatidy_kernel-0.0.1.dev0-py3-none-any.whl`, SHA-256
  `2264eeb0abbb96863f5699fa27ecaf42fd101f30aac5dd5d9fd810db1d635086`. Installed package files
  match the unchanged `src/creatidy_kernel` files byte for byte (ignoring generated bytecode).
- Environment: CPython 3.12.0, Linux x86-64 WSL2 (native Linux filesystem), isolated `-I` test
  interpreter, synthetic HOME/TMPDIR under `/tmp/kilo`, closed environment, offline locked `uv`
  build and test dependencies. Wheel install, test cwd and state are outside the checkout.
  The installed-prefix guard in `tests/conftest.py` checks actual package origin. Each parallel
  case uses its own SQLite store, Git repositories, workspaces, loopback ports and review peer.
- Canonical CI must independently build/install and verify the exact test/report PR revision, in
  addition to `make check` and audit. The candidate commit cannot embed its own ID; the PR and
  eventual canonical issue receipt bind the frozen HEAD/base, CI checkout, artifact and verdict.

The eight component receipts in the historical table remain reachable. Four newer native
predecessors were verified against closed canonical issue records, acceptance receipts and Git
ancestry, not merely closure: [#137/PR140](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/137#issuecomment-22289)
(`313cff74`, configured roles), [#138/PR142](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/138#issuecomment-22344)
(`ea508e34`, reusable Forgejo roles), [#139/PR143](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/139#issuecomment-22402)
(`ee9467dc`, GitHub work, Actions and context roles), and
[#146/PR147](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/146#issuecomment-22742)
(`959167dd`, distinct exact native execution evidence for each Attempt). These are prerequisites,
not substitutes for the executions below. The default owner-selected offline profile does not
require external Router, MI or Console reception.

### Installed Composition Matrix

`Q[case]` denotes `tests/test_drivers.py::test_qualified_work_dag_owner_approval_and_reopen[case]`.
This is the existing `OrdinaryProgram` flow using real production ports, drivers, controller,
SQLite, workspaces, owned verification commands and `NativeKiloReview` with a deterministic read-scoped
review peer. Provider HTTP responses are synthetic and deterministic; no real provider account or
model is accessed. The test configures role bindings and passes them to the same production
application. It does not implement alternate Program transitions or inject satisfactions.

| Cell | Work / SCM / CI / context | Exact installed evidence |
| --- | --- | --- |
| 1 | Forgejo Issues / Forgejo SCM / Forgejo Actions / repository files | `Q[forgejo-forgejo-actions-git]`: native Forgejo issue/dependency and SCM scans, qualified Actions and exact local Git blob. Read-only role configuration refuses an unconfigured synchronization effect before authorization. Legacy Forgejo AGit is separately retained, not a qualified cross-driver CAS. |
| 2 | Forgejo Issues / GitHub SCM / GitHub Actions / repository files | `Q[forgejo-github-actions-git]`: Forgejo issue identity and its native dependency cut remain independent of GitHub Git objects, native run/attempt and exact documentation blob. Exact-commit GitHub COMMENT effect only after separate authorization. |
| 3 | GitHub Issues / GitHub SCM / GitHub Actions / repository files | `Q[github-github-actions-git]`: separate planning repository/reader and source repository, owner-qualified visible `blocked_by` evidence; configured Actions binding, exact Git blob and separately authorized GitHub COMMENT. |
| 4 | Jira / GitHub SCM / Jenkins / Confluence | `Q[completion]` and `Q[sync-reopen]` on this wheel: visible Jira links plus owner obligation; exact Jenkins build/source/recipe; Confluence version/draft authorization, one send, lost reply and original Operation recovery. Also receives original trusted-local command evidence without reinterpreting it as external CI. |
| 5 | GitHub Issues / GitHub SCM / GitHub Actions / independent local documents | `Q[github-github-actions-local]`: substitutes only knowledge relative to cell 3, using `LocalDocumentKnowledge`, authorized exact key and SHA-256 snapshot, not a repository/Confluence fallback. Same obligations, Attempts and authorized GitHub COMMENT. |
| 6 | Missing, incompatible, unsupported or unverified required capabilities | The four new `Q` cells refuse a missing or changed CI profile, changed knowledge binding, incomplete dependency/inventory reads, wrong evidence and stale approval without replacing existing authority. They verify unsupported GitHub change mutation or absent Forgejo effect configuration separately. Historical matrix cells 8, 9, 13, 30-32 retain additional bounded version, optional/native capability and strict profile refusals. |

Cells 1-3 and 5 passed together (4 passed, 75.19 seconds). In the earlier focused run on the same
wheel, the two cell-4 cases, `Q[stop]`, `Q[managed-uncertain]`, and
`test_operator_profile.py::test_real_sops_age_profile_is_read_only_and_missing_key_refuses` passed.
That earlier run was not all-green: new Actions test assertions still assumed an empty pre-existing
GitHub review list and an endpoint serving only the current candidate, not the original retained
candidate after reopen. The final run verifies the corrected fixture assertions, not a change to
production behavior. Local Ruff lint/format and basedpyright were scoped to the changed test.

### Actions Attempt And Failure Evidence

Each of the four Actions rows approves a two-WorkUnit DAG over the actual source revision and pinned
context, releases only the first WorkUnit, and obtains a real cooperative claim and candidate A.
Exact A verification permits the fresh product review peer to return `request_changes`, not
acceptance. The owner settles the original claim; an authorized linked remediation Attempt
collects changed candidate B, obtains newly observed native CI under the same approved profile,
and receives fresh review before acceptance. WorkUnit 2 then starts on accepted B, verifies and
accepts its own candidate. All three Attempts and both satisfactions survive store reopen. The
native fixture ties workflow bytes to the actual local Git objects for the baseline and candidates.

GitHub CI identity retains native run and run-attempt; Forgejo retains run and per-job attempts,
handles and task IDs. Fixture native run IDs are 11 (baseline), 12 (A), 13 (B), 14 (dependent
candidate) and are verified as distinct within each isolated composition. The Forgejo proof is
not a same-run job rerun or a synthetic shared run-attempt number. CI facts alone do not establish
actual remote runner worktree or external-action resolution; Forgejo's absent structured step
evidence is not inferred. Provider pins and remaining limits are in
[the driver contract](../../README.md#independent-project-drivers).

Exact subject/recipe refusals include foreign Attempt, wrong commit/repository/profile, wrong
workflow or workflow bytes, fork, reused native execution and old receipt, pending/ambiguous,
missing or changing jobs, and stale result. The faulted check changes neither history nor
Operations and grants no satisfaction. Interrupted native read and interrupted receipt publication
are distinguished. Duplicate check and reconstructed workspace read-back retain the original
receipt time and original Attempt/candidate binding; expired authority is not renewed. Historical
draft v1 still decodes with its slot semantics; v2 profile selection refuses ambiguous/mismatched
versions or a simultaneous slot list.

The fresh Actions rows additionally track synthetic sourced `2 -> 4 USD` correction and unknown
usage on the original verification Operation. Private export after reopen retains both distinct
work identities, the dependency, accepted lineage and the presence or absence of a native effect;
it exposes no raw source/candidate revisions, fixture origins, context text, paths or credentials.
No aggregate cost is inferred. The GitHub COMMENT is an authorized synthetic native effect, not
product review, conditional PR update, merge or write authority for the other providers.

### Evidence Reuse And Live Cut

The historical 34-cell matrix remains the reference for unchanged cross-cutting contracts, not for
the new provider composition claims. Newly executed `Q[stop]` and `Q[managed-uncertain]` on this
artifact receive cooperative STOP and unsupported/missing exact managed settlement, respectively.
Retain historical bounded ownership/expiry/collision/manual edit and process-loss receipts (cells
14-21), budget expiry and estimates (22-24), owner disconnect/reconnect/gap and scoped privacy
(27-29), installed historical readers and interrupted migration (30), and strict refusal (32). These
are component/shared-path evidence, not claims that every terminal negative ran in each successful
configuration. Current canonical package CI runs the installed reader and application corpus; the
separate native SOPS 3.12.2/age 1.1.1 proof uses synthetic keys only, no owner/customer data. The
new Actions qualified-format lineage has its own composed runtime/export checks rather than borrowing
the historical Jenkins/Confluence export result.

Expanded offline acceptance requires exact canonical CI, fresh independent approval and verified
integration. [#60](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60) remains open for
AC6. The current live decision is [Forgejo Work Management + GitHub SCM read-only](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60#issuecomment-22183),
not the earlier proposed Jira/Jenkins/Confluence envelope above. An independently verified exact
integrated artifact and executable live manifest are still needed: real disposable resource and
instance/version/identity pins, approved scoped read credentials, effects/network/privacy and
cleanup boundaries, finite time/read/Attempt/review/money limits including STOP and uncertainty,
and separate execution authorization. An actual live receipt within those limits is still unreceived.
No Product GO, actual service mutation, paid inference, release or deployment is claimed.
