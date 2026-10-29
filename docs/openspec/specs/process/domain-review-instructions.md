# Domain Review Instructions

This document provides targeted review instructions for each Specification-Driven
Development (SDD) domain (`Journeys`, `Technical`, and `Process`) so reviewers
apply the right abstraction boundary and verification criteria to each document.

## Journey Domain (`docs/openspec/specs/journeys/`)

- **Focus:** Step-by-step user or operator workflows describing *what* happens
  from the external actor's perspective without prescribing *how* internal
  packages implement it.
- **Review Criteria:** Is the end-to-end journey complete? Are actor triggers,
  inputs, and observable outcomes clear? Are failure and recovery paths
  addressed from the user or operator perspective?
- **Implementation Requirements:** Journey specifications **do not directly
  govern source files**. They serve as the upstream functional contracts
  implemented by `SYSTEM` and `COMPONENT` specifications in the Technical
  domain (`tech -> journeys`).

## Technical Domain (`docs/openspec/specs/tech/`)

- **Focus:** System and component contracts (`spec.md`), architecture,
  trust boundaries, deployment models, and cross-package wiring (`design.md`).
- **Abstraction Boundary (`spec.md` vs. `design.md` vs. Code):**
  - **`spec.md` (Configuration & Input -> Observable Behavior Contract):** MUST
    specify what the user, operator, or upstream caller can rely on given
    different configuration settings and inputs (observable outcomes, invariants,
    security boundaries, completion conditions, and failure behavior). It MUST
    NOT descend into low-level implementation details that are better read
    directly from source code—specifically: private internal data structures,
    private function or method names and return values, source file line
    citations (e.g., `foo.py:123`), private state-machine enums, or internal
    helper formulas. While reading source code is essential to verify what the
    system does, `spec.md` MUST state the resulting behavioral contract without
    citing private code internals.
  - **`design.md` (Architecture & Realization):** Houses component boundaries,
    data-flow diagrams, and architectural trade-offs that realize `spec.md`,
    while still deferring line-by-line mechanics to the source code itself.
- **Review Criteria:** Does `spec.md` clearly define what a caller or operator
  should expect across configuration combinations without leaking internal
  implementation details? Is the architecture in `design.md` sound? Are material
  risks and trade-offs explicitly addressed? Does it fulfill the requirements of
  any governing Journey or System specification?
- **Implementation Requirements:** Technical specifications **require
  implementation and automated test coverage**. Source and test files must
  include `# Governing: docs/openspec/specs/tech/.../spec.md#req-xxxx` comments
  tracing back to the requirements they realize.

## Process Domain (`docs/openspec/specs/process/`)

- **Focus:** The meta-domain that configures the OpenSpec SDD process (domains,
  levels, ID prefixes, allowed transitions, writing standard, review criteria,
  and templates). It is isolated from the Journey and Technical domains and is
  not for product or runtime operational processes.
- **Review Criteria:** Are the governance rules clear, concise, and vendor-neutral?
  Do they improve specification quality and developer productivity without adding
  unnecessary ceremony?
- **Implementation Requirements:** No runtime feature implementation required.
  Modifications may require corresponding updates to process templates and
  governance documentation.
