# Halpha AI Work Entry Point

Applies to work within this repository. Keep global boundaries here and task-specific methods in the skills below.

## Authority and Current State

- Product and system semantics come from the Chinese files currently present in `docs/L0`–`docs/L4`; each document exists in one current copy and Git commits record its history.
- Use `docs/L4/HALPHA-PLAN-001-current-plan.yaml` for recorded current state. Read the relevant blocks; missing or stale facts remain unknown. A recorded focus does not veto a newly authorized task, and code or tests do not automatically update L4 or prove runtime availability.
- Halpha is a single-owner, self-funded, personally maintained project. Do not assume institution-grade governance, approval, high-availability, or compliance requirements, and do not describe account or capital scale decided by the owner outside the system as a Halpha capability or guarantee.

## Specialized Guidance Entry Points

- L0–L4 design, documentation and current-plan work: [`write-halpha-docs`](.agents/skills/write-halpha-docs/SKILL.md).
- Implementation, tests, dependencies, runtime validation, product-progress audits and design/code discrepancies: [`develop-halpha`](.agents/skills/develop-halpha/SKILL.md).
- Workbench scope, layout, interactions, visual review and browser evidence: [`design-halpha-ux`](.agents/skills/design-halpha-ux/SKILL.md).
- Independent research now lives in [hhxcv/Halpha-Research](https://github.com/hhxcv/Halpha-Research) and monitoring lives in [hhxcv/Halpha-Monitor](https://github.com/hhxcv/Halpha-Monitor). Do not recreate their workspaces, dependencies, caches or lifecycle inside this product repository. When the owner explicitly selects a research result for product consideration, treat the external artifact as read-only input and use [`develop-halpha`](.agents/skills/develop-halpha/SKILL.md) for the product-side implementation and qualification.
- Load only the skills and reference sections relevant to the task. Skills provide methods, not a parallel source of product semantics. Maintain document ownership and current-copy rules in `HALPHA-DOC-001`.

## Autonomy and Coordination

- Carry an authorized result through the necessary design, implementation, validation and fixes. Choose routine implementation details, reading depth and tools autonomously; use independently testable slices without requiring a new task or confirmation for each slice. Report the consolidated result at the natural outcome boundary.
- The user's explicit instructions take precedence over skill workflow preferences. Ask only when a missing decision materially changes the authorized result, product meaning or external effect and cannot be resolved from context. Do not ask merely because a document is in a higher layer. Complete independent authorized work and make any remaining decision concrete before asking.
- If an instruction blocks progress, cite the exact file and clause, the conflict and the decision needed; distinguish an explicit boundary from your interpretation. Preserve unresolved external facts as unknown.
- Subagents may handle bounded independent work when useful; counts and delegation are not mandatory. They inherit the parent task's scope and authority. Keep concurrent edits disjoint, and have the parent review the combined diff and checks. Separately writable top-level tasks use separate branches/worktrees only when their product, database, credential and external effects are independent, and integrate serially.

## Repository Work Baseline

- Change the semantic owner and necessary direct consumers. Preserve unrelated worktree changes; do not overwrite, revert or clean up the owner's work incidentally.
- The local-privacy and external-egress boundary is owned by `HALPHA-ENG-002`. Never place host identity, local configuration or credentials in Git, prompts, diagnostics or external payloads, and do not annotate a repository path with the private content it may contain or may have contained. Before commit or push, run `python .githooks/check_local_privacy.py --all`; do not bypass the repository hooks or publish a rejected revision.
- Engineering work does not by itself authorize commit, push, L4 fact changes, capital changes or real-account trading actions. Use existing explicit authorization when present; do not request it again.
- Finish mutations before observation that only waits for time or external evidence. Such observation remains read-only and serves only the decision that needs it.
- Base conclusions on actual reads, diffs, and validation results; do not claim to have run checks that were not run.
- Codex owns the complete lifecycle of every browser, automation daemon, helper process and temporary profile it starts. Close them on success, failure, cancellation and timeout (use guaranteed cleanup such as `finally` where applicable); before reporting browser-based work complete, verify that no Codex-started process remains and remove only its disposable temporary profiles. Never close a user's browser or remove a user profile; if ownership is uncertain, leave it intact and report the uncertainty.
- When Codex creates a Halpha strategy plan through the product UI or API within explicit user authorization, it must actively select `AI 创建` in the UI or submit `creator_kind: AI` through the API; it must not leave the human-creation default selected. The origin marker does not authorize fixing, activating, funding, or trading the plan.
