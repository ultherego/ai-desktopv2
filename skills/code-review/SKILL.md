---
name: code-review
description: Checks code or a diff for specific bugs and regressions without editing. Use for a request for review, an assessment of a fix or of security, or missing tests.
---

# Code correctness review

## Set the scope

Establish the change or component under review and the available state of the code. If you do not have the base version, review the current behavior and note that you are not assessing regression against an unknown diff. Do not extend the review to the whole project without a reason.

## Trace the behavior

1. Read the changed code together with its callers, data types and nearest tests. Trace the input through validation to the effect and the handling of the result.
2. Check the ordinary case and the relevant boundaries: empty and invalid data, permissions, partial write, interruption, dependency failure, concurrent change of state. Match the cases to the mechanism; do not produce hypothetical warnings unrelated to the code.
3. When you suspect a bug, name a reachable condition. Confirm it with a minimal example or an existing test if the available executor allows it. Without execution, mark the conclusion as code analysis; missing evidence does not confirm a defect.
4. Check whether the tests distinguish correct behavior from the old one. A syntax PASS or a mock does not prove the whole flow is correct or that it works on the target host.

## Classify the findings

- **P1:** reachable data loss, an access violation or a bug blocking the core scenario; describe the required conditions.
- **P2:** a functional bug or regression with limited effect.
- **P3:** a minor flaw; give style remarks only if they affect maintenance or a task requirement.

Merge duplicates with the same cause. Do not raise severity merely because a name sounds dangerous. Separate confirmed defects, hypotheses and missing evidence.

## Result

For each defect, give: priority, file and location, the condition under which it occurs, the effect, and evidence or a minimal reproduction. A recommendation must remove the cause; do not prepare edits in review mode.

If you found no bugs, say so together with the scope of the check and any relevant gap in verification. Do not issue a general security certificate. Do not run tests on a live system or widen access to finish the review.
