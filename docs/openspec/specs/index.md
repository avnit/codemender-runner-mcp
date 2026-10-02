# OpenSpec Specifications Index

This directory contains the formal OpenSpec specification (`spec.md`) and design
(`design.md`) documents governing the CodeMender Orchestrator (`cm-runner`)
capabilities.

| Spec ID                                         | Name                                                               | Level     | Status      | Primary Focus                                                                       |
| :---------------------------------------------- | :----------------------------------------------------------------- | :-------- | :---------- | :---------------------------------------------------------------------------------- |
| [SPEC-FIND-DIFF](find-diff/spec.md)             | [find-diff](find-diff/design.md)                                   | Component | Implemented | Native Impact-Aware Differential Vulnerability Scanning (`cm-runner find-diff`)     |
| [SPEC-FIX](fix/spec.md)                         | [fix](fix/design.md)                                               | Component | Implemented | Patch One Finding and Print the Diff (`cm-runner fix`)                              |
| [SPEC-0003](runner-container-packaging/spec.md) | [runner-container-packaging](runner-container-packaging/design.md) | Component | Approved    | Lean Runner Base Container & Modular BYOI Packaging                                 |
| [SPEC-WIF](wif/spec.md)                         | [wif](wif/design.md)                                               | Component | Approved    | Composable Mode-Aware Workload Identity Federation Module (`terraform/wif`)         |
| [SPEC-DIST](distribute/spec.md)                 | [distribute](distribute/design.md)                                 | Component | Proposed    | Customer Repository Distribution Engine & Propagation Kit (`scripts/distribute.sh`) |
