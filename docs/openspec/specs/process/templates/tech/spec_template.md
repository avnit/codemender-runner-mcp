<!--
sdd_spec_id: docs/openspec/specs/tech/{systems|components}/{name}/spec.md
sdd_legacy_id: SPEC-XXXX
sdd_implements: docs/openspec/specs/{journeys|tech/systems}/{parent-name}/spec.md
sdd_extends: NONE
sdd_level: COMPONENT
-->

# {Technical Capability Title}

**Implements Spec:** [{Parent Journey}](../../../journeys/{journey-name}/spec.md) or [{Parent System}](../../systems/{system-name}/spec.md) (or None)

**Extends Spec:** [{Base Title}](../{base-name}/spec.md) (or None)

**Governing Journeys:**
- **{Persona Name} (`{persona-id}`)**: As a {persona}, I want {capability} so that {outcome}.

## Overview

{Brief description of the technical capability, what system or package it
belongs to, and its purpose.}

## Requirements

### REQ-XXXX: {Descriptive Name} {#req-xxxx}

{Description using RFC 2119 keywords. Every normative statement MUST use SHALL,
MUST, MUST NOT, SHOULD, SHOULD NOT, MAY, REQUIRED, RECOMMENDED, or OPTIONAL per
RFC 2119. State observable behavior that callers or operators rely on, such as
CLI/API contracts, security boundaries, and error semantics; leave design
choices to design.md.}

#### Scenario: {System Scenario Name}

- **WHEN** {system state changes, CLI/API is invoked, or event is received}
- **THEN** {observable outcome: response, exit code, error, or downstream effect}
