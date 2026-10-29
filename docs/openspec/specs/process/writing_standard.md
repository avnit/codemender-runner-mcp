# SDD Writing Standard

This standard keeps OpenSpecs (`spec.md` and `design.md`) and Architecture
Decision Records (`docs/adr/`) concise enough to review and to keep accurate. It
applies to everyone who writes or reviews them: human authors, coding agents,
and automated review agents.

It overrides default template instructions where they ask for unnecessary detail
or boilerplate coverage. Project rules (`.agents/rules/sdd_rules.md`), domain
templates under `docs/openspec/specs/process/templates/`, and explicit requests
from a user, human reviewer, or repository maintainer take precedence over it.

## `spec.md`: the contract

`spec.md` states what callers, users, and operators can rely on.

- Add a requirement only if both are true:
  - violating it produces behavior that a caller, user, or operator can observe
    as incorrect; and
  - a competent engineer could plausibly get it wrong without it.

  For example, "MUST reject a tenant's requests with `RESOURCE_EXHAUSTED` once
  it exceeds its per-minute quota" is a requirement, whereas "MUST return a
  response for every request" is not.

- Leave out:
  - obvious or implied behavior, including behavior the parent spec already
    implies;
  - restatements of parent specs, language or ecosystem standards, or tool
    defaults;
  - design and implementation choices. For example, "the CLI MUST exit with a
    non-zero status code when inputs are invalid" is a requirement, whereas "an
    internal helper function validates the CLI flags" is a design or
    implementation choice that belongs in `design.md` or code.

- Apply the refactor test: if a behavior-preserving refactor that no caller can
  observe would make a sentence wrong, the sentence belongs in `design.md` or in
  code.

- Write scenarios only for critical behavior: boundaries, failure and escalation
  paths, and contracts between components. Skip scenarios that merely restate
  their requirement, and skip minor edge cases. Scenarios define the minimum
  acceptance test set, not the exhaustive one; automated unit and integration
  tests cover the rest.

- Code for behavior that has no requirement of its own anchors its
  `# Governing: docs/openspec/specs/.../spec.md#req-xxxx` comment to the
  requirement whose contract it serves.

## `design.md`: the rationale

`design.md` records the high-level design choices and why they were made.

- `Context` and `Decisions` are required, along with any section the domain
  template requires. Add optional sections—such as `Goals / Non-Goals`, an
  `Architecture` diagram, `Risks / Trade-offs`, or `Deployment & Rollback`—only
  when they tell the reader something they need to know.
- Record a decision only when it shapes the design and isn't obvious: state both
  the choice and the reason.
- List an alternative only if a competent engineer could reasonably have chosen
  it, and give the main reason it lost. Leave out strawmen: options that are
  obviously worse or that the context already rules out. If no alternative is
  worth listing, list none.
- Stay at the component level. Do not list private functions, internal flags,
  helper classes, or line-by-line control flow unless they define a boundary
  interface between components. Detail that restates the code goes stale; link
  to the code instead.

## ADRs (`docs/adr/`): optional beyond `ADR-0001`

- `ADR-0001` (`docs/adr/ADR-0001.md`), the project's foundational architectural
  blueprint, is always required, and the rest of this section does not apply to
  it.
- Write any other ADR only for a system- or architecture-level decision: one
  that spans multiple specifications or packages, is costly to reverse, or sets
  a precedent that other components must follow.
- An ADR requires at least two genuinely viable options: the chosen option, and
  at least one alternative that passes the `design.md` alternatives test above.
  If only one option is viable, do not write a standalone ADR; record the
  decision in `design.md` (for a decision that spans components, use the
  `design.md` of the lowest common parent or `SYSTEM`-level spec under
  `docs/openspec/specs/tech/systems/`).
- Record all component-local design decisions in `design.md`.
- Do not write ADRs merely to cover a topic category (such as logging or
  storage) when there is no open architectural decision.
- Keep pros and cons focused on the specific trade-offs that decided the
  outcome.
- Add an architecture diagram only when it clarifies a non-obvious topology or
  trust boundary.

## Reviews

These rules apply to human reviewers and review agents alike.

- Flag content to remove—such as obvious requirements, redundant scenarios,
  implementation detail, strawman alternatives, or unnecessary ADRs—as
  non-blocking suggestions. The exception is implementation detail in `spec.md`
  that fails the refactor test: flag it as blocking.
- In a change to an existing document, flag only what the change adds or edits.
  A requirement, scenario, decision, or ADR section that the change adds or
  edits counts as a whole. Leave untouched sections for a separate cleanup.
- Before adding a requirement, scenario, decision, alternative, or ADR suggested
  during review, apply the tests above. Decline additions that fail them and
  explain why.
