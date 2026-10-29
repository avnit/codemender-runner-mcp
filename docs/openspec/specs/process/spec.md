<!--
sdd_spec_id: docs/openspec/specs/process/spec.md
sdd_legacy_id: SPEC-0000
sdd_level: PROCESS
sdd_implements: NONE
title: "SDD Process Governance and Configuration"
sdd_config:
  spec_root: docs/openspec/specs
  adr_root: docs/adr
  domains:
    - name: journeys
      levels:
        - [JOURNEY]
      prefixes: [JOURNEY]
    - name: tech
      levels:
        - [SYSTEM]
        - [COMPONENT]
      prefixes: [SPEC]
    - name: process
      levels:
        - [PROCESS]
      prefixes: [SPEC, ADR]
  transitions:
    implements:
      - tech -> journeys
-->

# SDD Process Governance and Configuration

This document defines the governance rules, specification domains, hierarchy
levels, and traceability conventions of the Specification-Driven Development
(SDD) process for `codemender-runner`.

## Meta-Domain

This specification is the **process meta-domain**: it configures the OpenSpec
SDD process for this repository and is intentionally isolated from the Journeys
and Technical domains. It does not implement, extend, or reference specifications
in those domains, and is not a place for product or runtime operational
specifications.

## SDD Configuration

The top-of-file HTML-comment YAML metadata block of this document defines the
active domains and validation rules for specification identifiers:

- **Canonical Repository-Relative Path Spec IDs**: Every specification identifies
  itself via its repository-root-relative path (`sdd_spec_id:
  docs/openspec/specs/<domain>/.../spec.md`) and exposes stable requirement
  anchors (`docs/openspec/specs/<domain>/.../spec.md#req-xxxx`) for code
  `# Governing:` comments and cross-spec links.
- **Legacy Sequential Spec IDs**: Optional `sdd_legacy_id` identifiers
  (`SPEC-XXXX`, `JOURNEY-XXXX`) are preserved for backward compatibility during
  migrations.

### Domains

- **Journeys Domain (`docs/openspec/specs/journeys/`)**: Documents end-to-end
  user and operator workflows (`sdd_level: JOURNEY`, legacy prefix: `JOURNEY`).
- **Tech Domain (`docs/openspec/specs/tech/`)**: Maps technical systems
  (`docs/openspec/specs/tech/systems/<name>/spec.md`, `sdd_level: SYSTEM`) and
  software/infrastructure components
  (`docs/openspec/specs/tech/components/<name>/spec.md`, `sdd_level: COMPONENT`,
  legacy prefix: `SPEC`).
- **Process Domain (`docs/openspec/specs/process/`)**: Configures the OpenSpec
  SDD process itself (domains, levels, prefixes, allowed transitions, writing
  standard, review instructions, and document templates). Isolated from the
  Journeys and Tech domains (`sdd_level: PROCESS`, legacy prefix: `SPEC` or
  `ADR`).

## Related Documents

- [SDD Writing Standard](writing_standard.md)
- [Domain Review Instructions](domain-review-instructions.md)
- [Journey Spec Template](templates/journey/spec_template.md)
- [Journey Design Template](templates/journey/design_template.md)
- [Technical Spec Template](templates/tech/spec_template.md)
- [Technical Design Template](templates/tech/design_template.md)
