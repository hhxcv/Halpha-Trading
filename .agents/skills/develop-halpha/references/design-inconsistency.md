# Resolving Design and Implementation Discrepancies

Use this reference when current evidence and an owning contract imply different behavior, or implementation would have to invent an unresolved product rule. `HALPHA-DOC-001` owns document placement and `HALPHA-ENG-001` owns engineering autonomy.

## Establish the Discrepancy

Keep enough evidence to distinguish expected and actual behavior: the current owning clause, a reproduction or failing check, the affected result, and relevant component/configuration facts. Unknown evidence does not prove a design defect.

Check likely causes at the affected boundary: a misread owner, omitted dependency, wrong implementation, outdated fixture, stale expectation, environment mismatch or unsupported component use. An explicit stop, unknown or unsupported result may already satisfy the contract. Do not change product semantics just to satisfy an invalid test.

Contain only the disputed behavior and unsafe external effects. Independent work continues. Do not hide a discrepancy behind silent fallback, a second authority, a disabled meaningful check or wider permission.

## Choose the Smallest Authorized Correction

| Correction | Resolution |
|---|---|
| Implementation, fixture or configuration error | Correct the actual owner and verify affected behavior. A local correction does not automatically require design or L4 edits. |
| Current record is wrong or stale (L4) | Update through `write-halpha-docs` only when authorized and supported by current evidence. Missing external facts remain unknown. |
| Durable module, interface, state, failure or component-use contract changes (L3) | Update the owning document and direct consumers through `write-halpha-docs` within the authorized result. |
| Domain responsibility, product direction, overall architecture or highest boundary changes (L2–L0) | Check whether the user's instruction already settles that meaning. Carry out an explicit authorized revision; otherwise prepare the decision before implementing new semantics. |

Classify by the highest meaning changed, not by the file where the failure appeared. A current configuration correction is not an L4-only change if it alters a durable contract.

Ask for a decision only when a material product, authority, capital or external-effect choice is still unresolved and cannot be derived from context. Provide the conflicting clauses, consequence, viable options and recommendation; include migration or rollback when the choice affects existing data or behavior. Routine implementation details and previously authorized decisions do not require reconfirmation.

For core, cross-domain or high-failure-cost changes, re-derive the normal and critical failure paths. Use an independent read-only view when it can resolve meaningful uncertainty or repeated review escapes; no fixed reviewer count or new approval state is required.

## Resume and Report

Update the current semantic owner before implementation relies on the new rule. Verify affected consumers and critical counterexamples, and report the correction, evidence and remaining limits. A document change does not establish a runtime fact, and test success does not grant trading or publication authority.
