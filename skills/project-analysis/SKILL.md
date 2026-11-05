---
name: project-analysis
description: Reconstructs the architecture, data flows and working state of a shared project without changes. Use for getting to know a repository, assessing an implementation and comparing code with requirements.
---

# Project analysis

## Establish the question and coverage

Define the goal of the analysis: how the project works, whether it meets a requirement, where a problem arises, or what blocks completion. Establish the version, if available, and the limits of the shared files. Treat a list of names as a map for reading, not as evidence of behavior.

## Reconstruct the behavior

1. Start with the documentation of the goal, dependency manifests, entry points and configuration. Compare the declarations with the code; describe any discrepancy directly.
2. Choose the most important user scenario. Trace it from input through modules, data storage and dependencies to the result. Point out the permission boundaries and side effects.
3. For an external dependency, establish the interface, error handling and behavior when it is unavailable. For a write, establish the consent, the moment of durability and recovery after partial execution.
4. Read the tests of this flow. Separate a unit test, integration, a mock of a dependency and confirmation on the target host. Do not take the mere presence of a test as evidence that it passes.

## Choose the next read

- When you do not know how a component is called, read the entry point and its user instead of analyzing the file in isolation.
- When the documentation promises a feature, find the path that performs it and the denial conditions. If it is not in the available scope, mark missing evidence; do not automatically announce that the feature is absent.
- When a limit or policy blocks a read, conclude for the available part. Name the specific gap, without getting around the denial and without asking for the whole home directory.
- When the analysis concerns a later version, focus on the changed flow and its dependencies. Widen the scope only on finding a shared problem.

## Result

Deliver a short description of the goal, a map of module responsibilities, the course of the key scenario and an assessment of the requirements: confirmed in code, confirmed by execution, incomplete or unverifiable. Tie every significant claim to a file or a controller result.

Point out the main risks and one justified next step. Describe detailed defects by condition, effect and location; a full review of a fix belongs to `code-review`, if selected. Do not change files and do not present a planned test as performed. Do not claim a full analysis when part of the project remained unread.
