# Visual Density and Progressive Disclosure

## Visual Character

Design a modern professional trading workbench, not a SaaS landing page, OA portal, generic admin dashboard, or decorative crypto product.

Follow HALPHA-UX-001: professionalism comes from accurate semantics, comparison, and traceable context, not from density, jargon, or action count. Compactness is a means only when it improves the current task outcome.

Use Apple-like component discipline rather than consumer-product imitation:

- precise alignment and optical balance;
- restrained radii, borders, shadows, and semantic color;
- consistent pressed, hover, focus, selected, disabled, loading, and error states;
- immediate micro-feedback without ornamental motion;
- clear typography and calm surfaces;
- one primary emphasis per decision region.

Do not add gradients, glass effects, large decorative illustrations, oversized cards, badge clouds, marketing copy, excessive whitespace or multiple themes without a current user need.

## Dense Without Crowded

Increase value per viewport through structure, not by shrinking everything indiscriminately:

- place comparable facts in rows and aligned columns;
- right-align numeric values and use tabular numerals;
- keep units, signs, precision, environment, account, instrument, direction, and fact cutoff unambiguous;
- use compact row heights and short labels while preserving target size and legibility;
- group by the trader's scan sequence: exposure and protection, live responsibilities, next decision, recent state change, evidence;
- keep action controls near the state they affect;
- allow expert keyboard traversal and preserve spatial positions across refreshes;
- reserve whitespace for separating decisions and risk domains.

Measure density with useful facts, comparisons, and reachable actions per viewport. A visually sparse screen that forces drawers for position, order, protection, or current command state is under-dense. A screen that presents many unrelated facts without alignment or priority is crowded.

## Current Information Layers

Use the information layers described by HALPHA-UX-001 to choose components when hierarchy or disclosure is in scope; no separate per-item mapping artifact is required:

| Current layer | Default presentation |
|---|---|
| Conclusion | Show the current state, worthwhile or mandatory action, and time limit in the primary scan path. |
| Decision | Show the change, capital consequence, strongest support and counterevidence, decision-changing unknowns, and available options when deciding. |
| Evidence | Reveal supporting facts, plans, risks, actions, and versions progressively while preserving traceability. |
| Diagnosis | Put raw sources, complete timelines, technical state, logs, and replay material in a deep evidence surface. |

Quick viewing normally stays in conclusion and decision. Critical counterevidence, capital limits, unknown state, pending external result, and external responsibility must never be permanently buried in evidence or diagnosis.

## Visual Carrier Annotation

Choose carriers according to decision value and task continuity. The options below guide layout; they do not require annotations, another information taxonomy or persisted state.

### Always Visible

Keep decision-critical trading context visible without hover or navigation:

- environment and account;
- instrument and direction when scoped;
- current position, open order, and protection state when exposure exists;
- plan activation, machine-execution scope, or takeover state;
- new-risk, exit, or stop state;
- pending, rejected, failed, stale, or unknown outcome;
- fact cutoff and next time-sensitive judgment;
- primary allowed action and its current availability.

Critical risk or failure information must never exist only in a tooltip.

### Local Secondary Detail

Use compact secondary text, disclosure rows, hover/focus popovers, or expandable table rows for definitions, unit help, short reasons, and one-object detail. Any hover content must also be keyboard reachable.

### Task Context

Use a drawer, sheet, dialog, or local tab for consequence previews, field explanations, current evidence summaries, parameter groups, task history, and selected-object detail that the user needs without losing workspace context.

### Deep Evidence

Use a dedicated route for full timelines, raw facts, review evidence, immutable plan versions, or diagnosis that needs stable identity, deep linking, or substantial space.

## Explanation Budget

On the primary surface:

- prefer a precise label, state, value, and next action over a paragraph;
- add concise context when the title and state do not carry the meaning; let decision value determine its length;
- move rationale, definitions, and detailed consequences into the appropriate disclosure layer;
- show blocking reasons adjacent to disabled actions;
- reveal full technical evidence on demand.

Do not hide essential semantics merely to achieve a clean screenshot. Progressive disclosure removes secondary noise, not operational truth.

## Component and Layout Heuristics

Use these as prototype starting points, not acceptance requirements or replacements for current L4 theme variables:

- compact desktop controls and rows around 32-40 px where accessibility remains intact;
- 8-16 px internal spacing for dense regions and larger separation only between task domains;
- 12-14 px operational text with stronger numeric and state hierarchy;
- hairline separators and subtle surface contrast instead of one card per concept;
- stable top or workspace context strip; avoid tall global chrome;
- drawers sized for detail, not used as a dumping ground for all explanations;
- dialogs limited to decisions that require interruption;
- transient messages placed consistently and never covering critical controls.

Validate at actual viewport size. A reduced overview image can make a dense design appear clean while the real viewport has clipping, tiny hit targets, or excessive scrolling.
