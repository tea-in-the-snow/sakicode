---
name: code-review
description: Review a diff or a set of changed files for correctness, regressions, and security issues before committing.
---

# Code review

Use this skill when the user asks for a review of uncommitted changes, a diff,
or recently edited files.

1. Get the change surface first: `git status` and `git diff` (staged and
   unstaged). Review the diff, not your memory of the conversation.
2. Read the surrounding code for every hunk you are unsure about; a diff
   without context hides broken callers and stale comments.
3. Report findings ordered by severity: correctness bugs, regressions of
   existing behavior, security issues, then style. Each finding cites
   `path:line` and explains the failure mode in one sentence.
4. Do not rewrite the code unless asked; a review ends with findings and a
   verdict (approve / approve with nits / changes requested).

Load the bundled `checklist.md` resource for the full review checklist.
