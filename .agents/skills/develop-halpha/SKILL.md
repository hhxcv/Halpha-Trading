---
name: develop-halpha
description: Implement, test, refactor and validate Halpha code, dependencies and runtime configuration. Also use for test-quality or product-progress audits and design/code discrepancies.
---

# Halpha Development

## Scope and Context

Use `AGENTS.md` for authorization, privacy, worktree and lifecycle boundaries, and `HALPHA-ENG-001` for engineering tradeoffs. Complete the authorized result with the least process that preserves correctness.

Start with the target implementation, its consumers and the owning design. Read the relevant L4 blocks when current choices or facts affect the task. Search to locate context, then read enough of the contract to understand its meaning; do not load the whole document hierarchy for a local change.

| Task needs | Read |
|---|---|
| Owner or affected boundary is unclear | [Design Navigation](references/design-navigation.md) |
| Implementation evidence contradicts a stable contract | [Design Inconsistency](references/design-inconsistency.md) |
| Product progress, readiness, next step or mechanism value | [Product Closure Audit](references/product-closure-audit.md) |
| Formal design or current-plan changes | `write-halpha-docs` |
| Rendered appearance, interaction or page scope | `design-halpha-ux` and the affected UX/domain contract |

Missing current facts remain unknown. An old L4 focus does not override the user's newly authorized task. Product capability requires evidence from its actual consumer; code, a fixture or a passing utility alone does not establish runtime availability or closure.

## Implementation Judgment

- Identify the observable result and the behavior that could regress. Use the task context when it already supplies the objective and boundaries; add a short plan only when sequencing or tradeoffs need explanation.
- Change the owner and direct consumers. Keep edits reviewable, preserve unrelated work, and complete directly related fixes within scope.
- Prefer existing implementations and mature components for complex foundations. A bounded business function need not trigger dependency research. When adding or replacing a dependency, verify the relevant first-party contract and compare lifecycle costs as required by `HALPHA-ENG-001`.
- Preserve environment isolation, durable action identity, the unique exchange-changing execution boundary, reconciliation, protection, stop and takeover semantics. Process simplification cannot weaken these product invariants.
- Resolve ordinary implementation choices autonomously. When a correction changes documented behavior, update the owner through `write-halpha-docs` within existing authorization. Use the inconsistency reference for material ambiguity; layer numbers alone do not require another approval.
- For an owner-selected research candidate, read its framework-neutral handoff and current `HALPHA-ALP-002`/`HALPHA-ALP-003`. Keep the research workspace, Notebook, VectorBT and caches outside the product runtime; fixtures remain evidence inputs.

## Verification and Completion

Choose checks from the changed behavior and likely failure, not from the size of the repository:

| Change | Evidence needed |
|---|---|
| Copy, formatting or other low-impact presentation | Inspect the changed result; use a relevant mechanical check. No new test that merely repeats the wording or implementation. |
| Business logic or an interface | Targeted behavior tests; include direct consumers when the contract crosses a module, persistence or process boundary. |
| Rendered layout or interaction | Inspect the affected surface and states in a real browser; choose viewports and automation through `design-halpha-ux`. |
| Trading decisions, durable state, protection, stop or recovery | Affected normal path and critical counterexamples, including applicable duplicate/retry, unknown, stop and restart behavior in an isolated authorized environment. |
| Shared contracts, core state, dependencies, builds or uncertain impact | Expand to the relevant complete suites and integration checks; include browser or qualification suites when their paths can be affected. |
| Documentation or L4 | The checks selected by `write-halpha-docs`; validation does not authorize a fact change. |

For a selected strategy handoff, compare product decisions with the handoff trace on identical normalized inputs and cutoffs, then use the product's NautilusTrader path to validate affected event, order, fill, funding, margin and online/offline behavior. Resolve unexplained decision drift and execution-model differences before enabling the strategy.

Test meaningful outcomes: authority, persisted identity, transitions, reasons and user-visible behavior. Reuse existing coverage for the same risk; avoid coupling to private call topology or exact prose unless that representation is the contract. If a check fails, distinguish a product defect from fixture/interface drift or a stale expectation before changing production behavior. Do not preserve test-only production branches to accommodate invalid fixtures.

Keep fixtures isolated, bounded and deterministic, with cleanup on success and failure. Do not use real credentials, exchange-changing effects or an active product database to make a test pass.

At completion, review the diff and affected context for scope, correctness and unused additions. Once impact-appropriate checks pass, stop testing unless a new change, failure or unresolved concern justifies more. Report actual evidence and material limits; do not claim unrun checks, runtime closure or permission from test success. Elapsed observation is useful only for a stated question that direct checks cannot answer and remains read-only.
