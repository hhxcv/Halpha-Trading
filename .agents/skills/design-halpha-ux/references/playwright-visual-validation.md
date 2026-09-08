# Browser Validation for Halpha UX

## Scope and Tool Choice

Use real rendering to verify executable visual and interaction changes under current `HALPHA-UX-001`/`HALPHA-UX-002`. This reference does not create product routes, states, commands or permission.

Use an available supported browser tool for direct inspection. Use the `playwright` skill when automation improves repeatability, exercises a multi-step flow or provides a valuable regression check; Playwright is not a prerequisite for every visual repair. Component tests and static inspection can supplement the browser, but cannot establish unseen rendering or interactions.

## Size the Check to the Change

| Change | Browser scope |
|---|---|
| Local copy, alignment, overflow or component state | Inspect the affected region at the width and state that can expose the defect; check keyboard or focus if affected. |
| Responsive layout, navigation or common workflow | Exercise the affected task at desktop and narrow widths; add intermediate widths when they expose a distinct layout risk. |
| Consequential command or async behavior | Cover affected normal, rejection/failure, stale/unknown and duplicate/interruption paths with isolated deterministic inputs. Verify that acknowledgement is distinct from authoritative result. |
| Static image or document only | Inspect at intended size and state that interaction was not tested. No special status marker or browser run is required. |

The detailed cases in `HALPHA-UX-002` are selected by changed behavior. Do not run every route or state for a local repair.

## Gather Evidence

Identify the implementation or prototype under review, the route, relevant data conditions and viewport. Use the actual code and task context for selectable test parameters; L4 supplies recorded choices where present, not a required inventory of browser settings. Do not invent missing runtime facts.

Run the intended surface with an isolated authorized fixture or profile. A mock can test presentation, but does not prove a live product or external action works. Never use real-account trading actions merely to obtain UI evidence. If the surface cannot run, complete independent static work and report exactly what remains unverified.

Observe the changed result and affected transitions, checking legibility, clipping, alignment, critical control visibility and applicable focus behavior. Review relevant browser errors or failed requests when they could explain the defect. Fix in-scope issues and repeat the affected check.

Keep only evidence needed to understand or reproduce the result: route, relevant width/state, outcome and material limits. Add inspected screenshots, traces or exact non-secret commands when they help a reviewer or reproduce a failure; do not generate an artifact for every checklist item. Follow `HALPHA-ENG-002` for privacy.

Finish with the lifecycle cleanup required by `AGENTS.md`: close only task-owned browsers/helpers and verify their cleanup. Do not claim browser validation from an image or command that was never inspected.
