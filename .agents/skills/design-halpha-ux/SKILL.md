---
name: design-halpha-ux
description: Design and review Halpha's professional trading workbench against current L0–L4 semantics, actual L4 scope, personal-maintenance complexity, trader workflows, interaction safety, visual quality, and impact-appropriate browser evidence. Use for page scope, information architecture, navigation, wireframes, prototypes, interaction states, command feedback, risk confirmation, responsive behavior, accessibility, visual debugging, or material UI changes.
---

# Halpha UX Design

## Authority and Complexity

Use the current files in `docs/L0`–`docs/L4` and actual L4 scope. A route, mockup, benchmark or implementation convention does not create product behavior. Use `write-halpha-docs` when a user job, command, state, authority, failure result or support boundary must change; use `develop-halpha` for product implementation.

Load a reference only when its concern is in scope, then read it in full. Do not turn a copy or alignment repair into benchmarking, an artifact inventory or a browser campaign.

Default to the fewest justified surfaces, concepts and interaction paths that still expose decision-critical trading facts. Increase UX process only when a material workflow, consequential action, repeated usability failure or actual responsive/accessibility problem requires it.

## Workflow

### 1. Choose the Needed Depth

- **Local repair:** For copy, alignment, overflow, focus, contrast or an existing component state, read the owning clause and target implementation, then record the defect and completion check.
- **Material UX change:** For a new user job, route, navigation model, dense workbench layout or common workflow, read [Design Evidence and Scope](references/design-evidence-and-scope.md), the owning UX L2/L3 and only the directly affected domain owners.
- **Consequential action:** Also read [Feedback, Async State, and Risk Controls](references/feedback-async-and-risk-controls.md). Use only commands and results owned by current design.

A prototype stays separate from formal semantics and product implementation. Mark sample data and assumptions. Do not ask the owner to reconfirm a decision already fixed by design or the explicit task.

### 2. Start from the Trader's Job

Answer only what changes the layout or interaction:

1. What can change exposure, protection or machine execution now?
2. What must remain visible without another navigation step?
3. What is pending, stale or unknown, and what is the fact cutoff?
4. What is the next allowed action and its consequence?
5. When must the user use the venue's official interface?

Optimize for repeated expert use, fast comparison, numeric accuracy, keyboard access, stable positions and clear state—not marketing appearance or onboarding prose.

For financial facts, retain the economic sign while using the active K-line palette: profit and received funding use the up color with `+`; losses, stop-risk, projected loss, fees and paid funding use the down color with `−`. Zero and unknown remain neutral. Render default `Asia/Shanghai` times without a `UTC+8` suffix; label a time zone only when it differs from the default. Review changed pages for both sign and color rather than relying on either signal alone.

For ordinary selected choices, use the structural theme family specified by UX L2/L3 and the frontend theme source; do not repurpose semantic information, warning, success or error colors as a generic selected state. Use the exact theme structural fill and higher weight without a decorative selection outline. Direction controls are the exception: they retain no structural fill and use the active K-line red/green text and outline.

### 3. Choose Visual Direction Only When Needed

Read [Benchmarking Professional Trading UX](references/benchmarking-professional-trading-ux.md) only for a substantial redesign or unresolved interaction pattern. Compare the smallest relevant set of current first-party references; do not create a benchmark matrix when one direct precedent answers the question.

Read [Visual Density and Progressive Disclosure](references/visual-density-and-progressive-disclosure.md) when information architecture, density or disclosure changes. Keep critical environment, account, exposure, protection, open responsibility, unknown and available control visible; move secondary explanation and diagnosis deeper.

### 4. Specify Only Applicable States

For each changed interaction, cover immediate response, authoritative result and the applicable rejection, failure, stale or unknown outcome. Add stable request identity and persistent progress only when duplicate external effect is possible or work outlives the request.

A spinner, toast or HTTP success never proves a business effect. Risk-increasing activation uses the currently specified consequence preview and explicit user action; stop, exit and takeover remain directly reachable. Do not invent a second approval, receipt platform or generic risk gate.

### 5. Produce and Validate Proportionately

- Local repair: changed-state notes and before/after evidence only.
- Material structure: a small page/state sketch or prototype plus the key field and action mapping.
- Consequential flow: the affected normal, rejected/failed and unknown states, plus confirmation and external takeover where applicable.

Inspect executable visual or interaction changes in a real browser at the affected viewports. Use [Playwright Visual Validation](references/playwright-visual-validation.md) and the `playwright` skill when automation materially improves confidence; do not require it for a static artifact or unchanged interaction. For material or high-impact delivery, read [UX Review Checklist](references/ux-review-checklist.md) and apply only relevant items.

Hand off the result, assumptions, checks and unresolved semantic decisions. Do not describe a prototype as implemented or a tested UI as permission for real writing.
