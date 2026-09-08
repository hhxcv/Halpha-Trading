---
name: design-halpha-ux
description: Design, review and validate Halpha workbench scope, layouts and interactions, including prototypes, feedback, risk controls, accessibility and browser-visible defects.
---

# Halpha UX Design

## Scope and Context

Use the current UX/domain documents for product meaning and `AGENTS.md` for task authorization and operating boundaries. A benchmark, prototype or browser result does not create business authority. Use `write-halpha-docs` for semantic changes and `develop-halpha` for implementation.

Read only the relevant references and sections:

| Concern | Reference |
|---|---|
| New user job, route or major workbench structure | [Design Evidence and Scope](references/design-evidence-and-scope.md) |
| Substantial redesign or an unresolved interaction pattern | [Benchmarking Professional Trading UX](references/benchmarking-professional-trading-ux.md) |
| Density, hierarchy or disclosure | [Visual Density and Progressive Disclosure](references/visual-density-and-progressive-disclosure.md) |
| Consequential actions or asynchronous results | [Feedback, Async State and Risk Controls](references/feedback-async-and-risk-controls.md) |
| Executable visual or interaction changes | [Browser Validation](references/playwright-visual-validation.md) |
| Material or high-impact review | Applicable sections of [UX Review Checklist](references/ux-review-checklist.md) |

For a local copy, alignment, overflow, focus or contrast repair, start with the target implementation and owning clause. For new workflows, also read the affected UX L2/L3 and domain contracts. Read L4 when current scope or configuration matters; missing entries do not forbid a newly authorized design, but its implementation and runtime status remain unproven.

## Design Judgment

Start from the trader's decision: what changes exposure or protection, what is pending or unknown, which facts must stay visible, and which action is currently allowed. Prefer stable positions, comparable numbers, keyboard reachability and concise feedback.

Use current `HALPHA-UX-001`/`HALPHA-UX-002` and the theme source for financial signs, colors, time, component states and visual direction; do not copy these detailed contracts into this skill. Choose layout and carriers within those semantics. A recurring task or critical recovery path can justify a dedicated surface; decorative symmetry cannot.

A submission response is distinct from an authoritative business result. Cover the changed interaction's applicable pending, rejected, failed, stale and unknown states. Add identity and persistent progress when duplicate effects or work surviving navigation require them. Use the designed consequence preview and explicit action; keep stop, exit and takeover directly reachable.

## Produce and Verify

Choose the smallest artifact that resolves the design question. An existing implementation and a focused diff may suffice; use sketches, field/action mappings or a prototype when they clarify a material structure or unresolved choice. Mark sample data and assumptions.

Inspect executable visual or interaction changes in a real browser at the affected states and widths. Use automation when reproducibility, repeated flows or regression value justify it; a static artifact needs visual inspection, not browser certification. Apply only the review checklist items that the change can affect.

Complete authorized design choices without reconfirming settled decisions. If an unresolved product meaning or external effect needs the owner, prepare the alternatives and continue independent work. Hand off the result, actual checks and material limits; prototype quality and UI test success do not establish product availability or trading permission.
