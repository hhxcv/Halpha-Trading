---
name: develop-halpha
description: Guide Halpha implementation, test design and suite audits, refactoring, dependency checks, builds, runtime configuration, direct operational validation, product-closure audits, and design-to-code traceability. Use when developing or modifying Halpha code, migrations, builds, configuration, tests, fixtures, or runtime checks; reviewing test value, coverage, critical paths, over-testing, or suite reliability; auditing actual business progress, runtime reachability, mechanism or dependency complexity, and the next smallest useful slice; or handling implementation evidence that exposes a suspected L0–L4 design error or conflict.
---

# Halpha Development

## Authority

Use the current files in `docs/L0`–`docs/L4` for product semantics and the current L4 plan for current focus, component versions, configuration and results. Git commits record document history. Development uses continuous small iterations rather than packages or stage completion. The user's requested outcome defines scope; real-account trading actions, capital changes, commit, push and L4 fact changes still require their own authority.

Read [Design Navigation](references/design-navigation.md) in full for every development task. If implementation evidence conflicts with design, also read [Design Inconsistency Protocol](references/design-inconsistency.md). Use `write-halpha-docs` for formal document changes.

For progress, readiness, next-step, mechanism-complexity or test-value audits, read [Product Closure Audit](references/product-closure-audit.md) in full. Do not count documentation, a class, a passing unit test or a qualification utility as a product capability until its actual runtime consumer and user-visible result are established.

Use the least process that preserves correctness. Early, local and reversible work defaults to ordinary Git, direct tests and a short handoff. Add process only for current irreversible effects, real-account trading actions, meaningful concurrency, repeated failures or a decision that actually consumes extra evidence.

## Workflow

### 1. Bound the Outcome

1. Read `AGENTS.md`, inspect the worktree and preserve unrelated changes.
2. 按 HALPHA-ENG-001 的影响原则判断本次改动的最高实际影响。
3. Read only the L4 blocks that identify the current objective, applicable environment, exact choices, constraints and known results. Read the full plan only when changing it or when the needed state cannot otherwise be found.
4. Keep work inside the user-authorized outcome. Several directly related, independently testable slices may be completed together. Split only for a new external effect, a material scope expansion, a path conflict or evidence that must arrive later.
5. Independent research may proceed in its own workspace when it does not touch product runtime, product databases, credentials or exchange-changing endpoints.

Missing state remains unknown. Code or passing tests never imply product availability, permission to perform a real-account trading action or an L4 fact change.

### 2. Read by Impact

| Impact | Minimum design set |
|---|---|
| Light | Target files/tests and the directly relevant L4 fact; read an owner only when behavior is encoded. |
| General | Owning L3, relevant L2 boundary, direct dependencies and consumers. |
| Core or unclear | General set plus only the L1/L0 clauses and cross-domain contracts that can change the result. |

Read target code and direct consumers before editing. Search locates owners but does not replace them. Implementation uses the current owning documents; do not substitute an older Git revision or a separate process copy.

### 3. State a Small Contract

Record the problem, expected result, excluded scope and validation. Add semantic owner, key failure behavior and rollback only when relevant. For core trading changes also cover the applicable normal path, duplicate/retry, unknown, stop, protection, takeover and restart cases.

Do not turn the contract into a new status model, approval chain or evidence object. If an unresolved choice changes behavior, report the design gap instead of inventing a default.

### 4. Reuse Before Building

Before designing or implementing a new capability, apply the dependency-selection order owned by HALPHA-ENG-001: use the current pinned components directly, then assess another mature component, then use supported composition, extension, bounded customization or a thin adapter, and choose a complete Halpha implementation only after those options cannot meet the current result. Check a component only when adding, replacing or materially changing it. Verify the current pinned choice against first-party contracts and the target machine to the depth needed by the decision.

Exhaust supported capabilities in existing dependencies before adding one. Do not introduce a large component for a small capability when its transitive, configuration, testing, runtime, upgrade, recovery and exit costs exceed a bounded Halpha implementation. For high-stability, high-performance or complex foundations, especially the quantitative trading core, understand and reuse the mature framework's relevant design and extension points instead of rebuilding its foundation. Keep one implementation and one source of truth; explicitly leave a capability unsupported when its maintenance cost exceeds current value.

### 5. Implement the Authorized Result

1. Change the semantic owner's implementation and necessary direct consumers.
2. Use small reversible edits; do not require a new user round-trip for each slice already included in the requested result.
3. Add only tests, migrations, configuration and diagnostics that the changed behavior needs.
4. Apply the local-privacy and external-egress boundary owned by `HALPHA-ENG-002`: keep credentials and host-private values out of code, documents, ordinary logs, browsers, test artifacts and external diagnostic payloads; do not add telemetry or uploads without explicit authorization for that external effect. Never cause a real account or capital change without explicit authorization.
5. Do not bypass environment isolation, durable action identity, the unique exchange-changing execution role, fact reconciliation, protection, stop or takeover behavior.
6. For an owner-selected research candidate, read its framework-neutral handoff and current `HALPHA-ALP-002`/`HALPHA-ALP-003`. Implement or review the fixed decision logic in the product path without importing VectorBT, a Notebook, the research workspace or its cache; preserve the research fixture as evidence input, not a product runtime dependency.

### 6. Validate Proportionately

- **Every change:** run the smallest targeted check that directly exercises the change and likely failure.
- **Shared boundary or state change:** add relevant integration and direct-consumer checks.
- **Executable UX:** inspect the affected route, states and viewports in a real browser when visual or interaction behavior changed.
- **Financial and time displays:** when a changed page shows economic outcomes, verify profit/received funding uses the active K-line up color with `+`, while losses, stop-risk, projected loss, fees and paid funding use the active K-line down color with `−`; verify zero and unknown stay neutral, and default `Asia/Shanghai` times omit the redundant `UTC+8` suffix.
- **L4 or real-account-action-state change:** run the general documentation validator and the small governance validator.
- **Core trading change:** exercise normal behavior, the critical counterexample, duplicate/retry and stop or rollback in the closest authorized environment.
- **Selected strategy handoff:** first compare the product decision with the research handoff trace on identical normalized inputs and cutoffs, then use NautilusTrader to validate event, order, fill, funding, margin and online/offline behavior. Separate unexplained decision drift from expected execution-model differences; do not enable the strategy while either remains unresolved.

Treat targeted checks as iteration feedback, not final suite evidence. At the natural outcome boundary, run the complete relevant repository suites, including separately configured browser or qualification suites when they can exercise the affected path, and report skips and checks not run.

#### Keep Tests Valuable

- Start from a current user result, authority boundary or failure mode. Use the lowest-cost layer that proves it, and add a direct-consumer or integration check when behavior crosses a process, persistence, framework or browser boundary. Test count and blanket coverage are not goals.
- Before adding or retaining a case, find the production consumer and existing coverage. Extend an existing scenario when it proves the same risk; delete a test with a mechanism that has no current consumer instead of preserving self-validating code.
- Assert durable semantics such as state transitions, reason codes, persisted identities, authoritative facts and accessible roles. Avoid exact source text, private call topology, file-membership lists and whole UI sentences unless that representation is itself the contract.
- Classify a failure from direct evidence as a product regression, fixture or interface drift, or a stale expectation before changing code or relaxing an assertion.
- Keep mutating fixtures on isolated environments, databases and ports. Match current interfaces, replace external dependencies with deterministic bounded providers, exclude real credentials and exchange-changing effects, clean up on success and failure, and never point them at an active Demo or live product instance.
- Treat fixture, tool and qualification success as evidence for only the path they exercise; do not use it to claim product closure or replace the closest authorized runtime check.

Use elapsed observation only when a current release, deployment or real-capital decision needs behavior that direct tests cannot establish. Waiting remains read-only and creates no permanent role or project state. When evidence arrives, verify its source, inputs, time and scope before using it.

### 7. Resolve Design Conflicts

Pause only the affected slice and preserve raw evidence. Use [Design Inconsistency Protocol](references/design-inconsistency.md) to distinguish implementation error, ordinary L4 correction and stable-design change. Independent unaffected work continues.

A project-owner decision is required when the correction changes product direction, domain responsibility, authority, capital meaning or another L2-or-higher rule. An independent second view is required only for core, cross-domain, repeated-escape or high-cost L3 changes. Use `write-halpha-docs` to update the current owning documents; never let code silently establish new semantics.

### 8. Integrate and Deliver

1. Re-read every modified file and the actual diff; remove accidental scope and unused concepts.
2. Run relevant checks, including `python .githooks/check_local_privacy.py --all` before any Git publication, and state what was and was not run.
3. Report the outcome, changed scope, design basis, validation, rollback or stop path and remaining unknowns.

Do not infer commit, push, L4 advancement, real credentials or real external writes. Do not keep a mutation-capable task open merely to wait for time or outside evidence.
