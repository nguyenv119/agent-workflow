The codebase has three separate data-handling and synchronization bugs that need to be fixed:

1. Inconsistent Label/Body Validation for Question Issues
In the sync script for answered questions, there is inconsistent handling when an issue has a "question" label but the body lacks `**Kind:** question`.
- The single-issue path (when run with `--issue N`) currently fails with an exit code of 1. It should instead exit with status code 0 (since it's a skip, not a failure) and log a message suggesting remedies (e.g., adding `**Kind:** question` to the body or dropping the label).
- The cron sweep path silently skips these mismatches. It should instead collect them, print a warning to stderr for each mismatch found, and output a total mismatch count.

2. OKF Frontmatter Parser Data Corruption
The `extract_frontmatter` function (used for OKF exports) parses frontmatter using naive string manipulation (`line.strip().partition(":")`), which destroys YAML indentation. This causes several issues:
- Nested YAML blocks collapse onto the top level (e.g., a nested `source` field inside `provenance` overwrites the lesson's top-level `source`).
- YAML block sequences (lists like `- item`) are dropped completely.
- Escapes inside quoted values (e.g., `"grep -q \"Errno 98\""`) are mangled.
- Files containing a JSON block followed by YAML frontmatter fail to parse properly.
Refactor `extract_frontmatter` to correctly parse the frontmatter block, keeping nested objects intact, reading lists correctly, preserving escapes, and cleanly handling mixed JSON/YAML blocks.

3. Evidence Word Filter in the Worker
In the service worker script, the `isEvidenceResult(r)` function filters evidence intent queries by searching for literal words ("verified", "confirmed", "high") in fields that aren't present in default compact results.
Update it to use the corpus's actual evidence scale:
- Define a constant `EVIDENCE_RANK` mapping the levels to numbers: `{ E0: 0, E1: 1, E2: 2, E3: 3, E4: 4 }`.
- Define a constant `EVIDENCE_MIN_RANK = 2`.
- Update `isEvidenceResult(r)` to evaluate the `r.evidence_level` field and return true if its rank is greater than or equal to `EVIDENCE_MIN_RANK`.