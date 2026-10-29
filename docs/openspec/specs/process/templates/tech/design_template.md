<!--
sdd_spec_id: docs/openspec/specs/tech/{systems|components}/{name}/spec.md
sdd_implements: docs/openspec/specs/{journeys|tech/systems}/{parent-name}/spec.md
sdd_extends: NONE
sdd_level: COMPONENT
-->

# Design: {Technical Capability Title}

## Context

{Background, current system state, technical constraints, and dependencies.
Reference the companion `spec.md` and any related ADRs under `docs/adr/`.}

## Goals / Non-Goals

### Goals

{What technical outcomes are we aiming for? (e.g., isolation guarantees,
bounded concurrency, deterministic output)}

### Non-Goals

{What technical features or optimizations are out of scope?}

## Decisions

### {Technical Decision 1 Title}

**Choice**: {what technology, algorithm, or architecture was decided}

**Rationale**: {why this choice, citing technical merits like security,
simplicity, or maintainability}

**Alternatives considered** (omit if none is worth listing):

- {an option a competent engineer could choose}: {main reason it lost}

## Architecture

{High-level description of components, data flow, and package boundaries.
Include a Mermaid or Graphviz DOT diagram if it clarifies how components
interact.}

```mermaid
flowchart LR
    A["Caller"] --> B["Component"]
```

## Risks / Trade-offs

- **{Material Technical Risk}** -> {Mitigation (e.g., timeouts, fallbacks)}

## Deployment & Rollback

{Steps to roll out this technical component and the strategy to roll back in
case of failure.}

## Open Questions
