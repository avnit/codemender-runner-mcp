# Technical Systems

This directory contains specifications defining the high-level technical systems
(`sdd_level: SYSTEM`) of `codemender-runner`.

## Active Systems

| System | Specification | Design | Primary Focus |
| :--- | :--- | :--- | :--- |
| **CodeMender Runner** | [codemender-runner/spec.md](codemender-runner/spec.md) | [codemender-runner/design.md](codemender-runner/design.md) | Top-level multi-package architecture, scenario- and host-agnostic workflow seams, ephemeral-versus-durable storage separation, progressive completion-order streaming, and split-stream CLI contracts across every customer deployment scenario. |
