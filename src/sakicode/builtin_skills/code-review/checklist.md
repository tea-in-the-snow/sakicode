# Code review checklist

- Correctness: does each changed branch handle empty/None/error inputs?
- Regressions: do existing tests still describe the old behavior, and were any
  assertions weakened to make the change pass?
- Interfaces: did any signature change without updating all callers?
- Security: new path handling, shell commands, or deserialized data validated
  and confined? Secrets kept out of logs, traces, and checkpoints?
- Concurrency/state: can the new code leave partial state behind on failure?
- Tests: is the new behavior covered, including at least one failure case?
- Docs/comments: do comments still describe what the code actually does?
