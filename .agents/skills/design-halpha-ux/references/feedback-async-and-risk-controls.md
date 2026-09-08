# Feedback, Async State, and Risk Controls

## Feedback for the Changed Action

Use the following layers when they apply to a changed stateful action. A local selection need not acquire request identity, persistent progress or a new lifecycle.

1. **Local response:** within perceptual immediacy, show pressed state, focus, field validation, or button-local activity and prevent accidental duplicate submission.
2. **Request acknowledgement:** show that the request reached the responsible boundary; include a stable identity only when it is needed for retry, refresh or external-effect tracking.
3. **Ongoing work:** when work outlives the request, show the supported waiting, retrying, reconciling or blocked result. Display determinate progress only when truthful; otherwise show the latest meaningful step.
4. **Authoritative result:** show effective, rejected, failed, cancelled, abandoned, timed out, stale, or unknown as supported by current semantics, with evidence cutoff and next action.

Submission acknowledgement is not business effect. `PROCESSING`, a spinner, an HTTP response, or a toast must never masquerade as order placement, cancellation, exit, protection, or capital release.

## Feedback Placement

Use the least intrusive surface that preserves the consequence:

- button-local feedback for immediate submission;
- inline field messages for validation;
- a nonblocking toast or status message for transient acknowledgement;
- persistent row state, activity item, banner, or Task for ongoing work, failure, unknown, or required follow-up;
- a dialog only when the user must decide before proceeding;
- a detail drawer or route for evidence and diagnosis.

Success messages may disappear after the durable state is visible. Failure, rejection, unknown, and required follow-up must persist until resolved, superseded, or explicitly acknowledged according to current semantics.

## No Silent Failure

For every failed or abnormal path, display:

- what failed in user language;
- the affected object, scope, and environment;
- whether the requested effect occurred, did not occur, or is unknown;
- a stable reason or correlation identity when available;
- fact cutoff and last confirmed state;
- safe retry conditions, alternative action, or external venue handoff;
- whether the system will continue retrying or has stopped.

Do not expose secrets, raw stack traces, or provider errors as the primary message. Preserve technical detail in a protected diagnostic layer.

## Long-Running Work

For work that may outlive the current view and cannot be represented by the owning domain result alone:

- give it a stable visible identity when refresh or retry must find the same responsibility;
- keep status available after navigation or refresh;
- show the latest meaningful change;
- show determinate progress only when backed by real units;
- allow cancellation only when semantically safe;
- explain consequences before cancelling work that has partial effects;
- show completion or failure on the owning result surface and use the currently configured delivery channel only when required.

Avoid indefinite unlabeled spinners. If progress stalls, transition to a visible waiting, delayed, failed, or unknown state rather than silently continuing animation.

## Review Actions by Consequence

Review changed actions by consequence, not button color. The following are review triggers, not a required inventory or proof that Halpha owns the action:

- deletion or irreversible evidence loss;
- configuration changes that affect runtime, data, permissions, notifications, or recovery;
- increasing capital, notional, loss, or machine authority;
- activating or resuming risk;
- modifying or removing protection;
- manual trade behavior with material exposure consequence;
- stopping a strategy, executor, application, or protective process;
- exiting, taking over, releasing capital, or changing recovery authority.

Include directly coupled actions when their irreversibility, uncertainty, privilege or exposure can change. Locate the current owner and required consequence preview; do not create a separate record when the design and diff already make them clear. A new business action needs an owning contract through `write-halpha-docs`, not a generic UI command invented in its place.

## Consequence Preview and Explicit Action

Use the confirmation required by current design. For risk-increasing activation, show current target, scope, limits and consequence, then require one explicit user action. This is the product action itself, not a second authorization object or approval workflow. Add stronger friction only when accidental activation cost or current design requires it.

The confirmation shows, as applicable:

- exact action name, never generic “确定” as the primary label;
- target object, environment, account, instrument, and scope;
- current facts and cutoff;
- immediate and downstream consequences;
- what the action does not do;
- uncertainty, partial-effect, and external-system behavior;
- reversibility and recovery path;
- expected version or expiry only when the owning contract uses it;
- a safe cancel path and keyboard focus behavior.

Do not repeat warnings so often that the owner learns to dismiss them automatically.

Stop, exit and takeover are time sensitive. Keep the first legitimate request directly reachable; do not add arbitrary approvals, long wizards, generic rate limits or dependencies on another interface. A visual warning does not authorize an action absent from current design.

## Control-State Requirements

For each changed consequential control, define the applicable items:

- enabled conditions;
- disabled reason shown adjacent or on focus/hover;
- in-flight duplicate prevention;
- stale-preview invalidation;
- version-conflict behavior;
- navigation-away behavior;
- retry idempotency;
- result reconciliation;
- accessibility name and focus return after dialog close.

Color must never be the only risk or result channel. Pair color with text, icon, position, and state language.
