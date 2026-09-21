# ADR 0005: Authority, Verification And Human Attention

Status: accepted, 2026-09-21; target fixed before the Creatidy implementation audit.

## Decision

LLMs perform intelligent work. Code decides legal workflow transitions and authority changes.
Worker output, repository content, issues, comments and tool responses are untrusted inputs.
Workers may propose candidates and findings; they cannot grant authority, accept their own work,
mutate the control database, change trusted verification policy or operate with owner credentials.

An owner-approved ProgramSpec defines bounded authority and policy. An AuthorityGrant binds issuer,
subject, Program/spec digest, repository, paths, operations, network destinations, limits, expiry
and revocation state. Dangerous single-use decisions bind the exact effect/input digest and are
consumed atomically with its Operation intent. Redelivery of that same Operation does not consume
another grant or authorize different content. Changed scope/head/policy requires re-evaluation.
Use existing credential stores and scoped provider credentials; no custom secrets manager or DSL.

A grant record is not enforcement. A trusted broker and OS/container boundary enforce it; a broad
token must never be handed to a worker and then constrained only by a prompt. Same-user unsandboxed
workers cannot be treated as a hostile security boundary. Owner APIs and worker APIs must have
different authenticated capabilities, including when running locally.

Agent output is a CandidateResult bound to Attempt, spec and artifact/commit digests. Schema checks
only make it eligible for verification. Evidence is a separately stored observation with producer,
subject digest, tool/version, time and artifact reference; a verdict cites evidence and pinned policy.
The Kernel records an AcceptedResult only after all required deterministic and independent checks,
scope/authority checks and exact-subject binding pass. Missing evidence is not a pass. Tests themselves
are untrusted code; execute them without owner credentials. Trusted gate definitions cannot be
weakened by the candidate they judge.

For code changes, bind review/CI to repository, base/head revision and tested merge tree when relevant.
A cold reviewer gets the specification, code and evidence but no implementer's conversation or
claimed verdict. New remediation requires a new candidate and fresh independent review. Preserve
old findings; do not relabel their subjects. Acceptance of a patch is not a merge or deployment.
A later merge rechecks exact current inputs and uses an atomic conditional effect; observing a head
then performing an unconditional merge leaves a race and is not acceptable.

HumanGate means an owner decision or authority is required: product direction, architecture
authority, security/privacy, destructive production action, material scope/budget expansion,
requirements conflict, or genuinely unrecoverable authority/infrastructure. A failure, reviewer
defect, temporary provider error or silent-but-healthy worker is not by itself a HumanGate.

Remediation uses finding identity, persistence, progress and oscillation to assess convergence,
within finite preauthorized resource/time budgets. Five different defects may be healthy progress.
Repeating the same defect is not proof that an owner can solve it. Exhausted autonomous budgets
pause work with a recorded reason; request authority only if further work requires a new decision.
No unbounded retry loop and no generic `needs_human` error bucket.

## Alternatives

Self-attestation, prompt-only path restrictions, reviewer text as a protocol, round-count escalation,
and ambient owner tokens fail the observed requirements. A general enterprise governance system is
not required. The threat model and successor regression plan define future enforcement tests; A0
contains no worker execution and makes no claim that those controls have already been implemented.
