# Spec-Driven Development (SDD) Project Rules

This project enforces Spec-Driven Development (SDD) using OpenSpec (`spec.md` +
`design.md`) and Markdown Architecture Decision Records (`docs/adr/`).

1. **Specification First:** New user-observable capabilities, architectural
   changes, and cross-component contracts SHOULD have an approved OpenSpec
   (`spec.md` + `design.md` pair) under `docs/openspec/specs/` before
   implementation. Minor changes, bug fixes, internal refactorings, and work
   already covered by an existing specification do not require a new or updated
   spec.
2. **RFC 2119 & Anchor Stability Mandate:** All normative requirements in
   `spec.md` MUST use uppercase RFC 2119 keywords (`MUST`, `SHALL`, `SHOULD`,
   `MAY`, `MUST NOT`, `SHALL NOT`, `SHOULD NOT`, `REQUIRED`, `RECOMMENDED`,
   `OPTIONAL`) and explicit Given/When/Then `#### Scenario:` blocks. Once a
   requirement heading and anchor (`### REQ-XXXX: <Title> {#req-xxxx}`) are
   published, the requirement identifier and `#req-xxxx` anchor MUST NOT be
   renumbered when other requirements are inserted or deleted.
3. **Refactor Invariance (`spec.md` vs. `design.md`):** `spec.md` defines
   observable behavioral contracts and MUST NOT cite private function or method
   names, internal helper constants, or source line numbers (see
   `docs/openspec/specs/process/writing_standard.md`). Architectural realization,
   component boundaries, and trade-offs belong in `design.md`.
4. **Governing Traceability:** All implementation code and test files MUST
   include governing comments pointing to their specification using the
   repository-root-relative file path and `#anchor` format:
   `# Governing: docs/openspec/specs/tech/components/<name>/spec.md#req-xxxx`.
5. **Relative Markdown Pathing & Spec Graph Identifiers:**
   - All Markdown links inside `docs/` MUST use relative paths so they resolve
     natively on GitHub.
   - All `spec.md` and `design.md` files MUST begin on Line 1 with a `<!-- ... -->`
     HTML-comment YAML metadata block (`sdd_spec_id`, `sdd_implements`,
     `sdd_extends`, `sdd_level`), and all graph identifiers MUST use
     repository-root-relative paths (`docs/openspec/specs/.../spec.md`) without
     `..` parent traversal segments.
6. **Public Repository Hygiene:** This is a public repository. Never commit
   internal-only URLs, corporate shortlinks, internal issue tracker IDs,
   corporate email addresses or handles, local workstation paths
   (`/usr/local/...`), or internal document-review HTML comment metadata.
