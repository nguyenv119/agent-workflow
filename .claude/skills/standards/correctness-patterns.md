# Correctness Patterns

Known bug patterns learned from production incidents and external code review. Each pattern describes a class of bug that is easy to introduce and hard to spot in review.

These patterns are language-agnostic — they describe structural problems, not syntax.

---

## Async & Orchestration

### Race/Select Orphaned Failures

**What to look for:** When a race or select construct picks a winner (e.g., first response, fastest goroutine, `Promise.race`), check whether the losing branch can fail *after* the winner settles.

**Why this matters:** The winner returns success. The loser throws an error a moment later — but nobody is listening. The error is silently swallowed, or worse, it corrupts shared state. The caller sees success while a side effect failed.

**Real pattern:** A function races a timeout against an API call. The API call loses the race and its error is never caught. The timeout "succeeds" but the API call's partial write is never rolled back.

---

### Unbounded Input Accumulation

**What to look for:** Loops or iterators that collect input into an in-memory structure (array, list, map, string builder) without a size cap or backpressure mechanism.

**Why this matters:** In normal operation the collection is small. Under adversarial or unexpected input (large file, malformed stream, pagination that never ends), memory grows without bound until the process is killed.

**Real pattern:** A function reads paginated API results into an array. The API returns 10,000 pages due to a filter bug. The array grows until OOM kills the worker.

---

### Multi-Step Orchestration

**What to look for:** A function that makes multiple sequential async calls where each can fail independently. Check whether ALL step failures contribute to the return value, not just the last one.

**Why this matters:** The pattern: intermediate failure is logged as a warning, final step succeeds, function returns success. The caller thinks everything worked, but actionable data was lost. Any function that orchestrates N steps and only checks step N is a bug.

**Real pattern:** A Slack notification function posts a summary, then posts mismatch details. The details post fails but the summary succeeds. The function returns success. The recipient sees stats with no context.

---

### Retry Scope Overshoot

**What to look for:** A retry wrapper that encloses more than just the retryable operation. The retry boundary should wrap only the call that can transiently fail — not unrelated I/O, state mutations, or side effects that preceded it.

**Why this matters:** When the retry fires, it re-executes everything inside the wrapper. Idempotent reads are safe to retry, but non-idempotent writes get duplicated. The wider the retry scope, the more unrelated work gets re-executed on each attempt.

**Real pattern:** A function wraps "read config + call API + write result" in a retry loop. The API call is the flaky part, but on retry the config is re-read (stale) and the write is duplicated.

---

## Type Safety

### Type Narrowing Loss

**What to look for:** An explicit type annotation on a variable that the compiler/interpreter had already inferred to a narrower type. The annotation widens the type back to a less specific one.

**Why this matters:** The compiler inferred `"active" | "inactive"` but the developer annotated `string`. Downstream code that pattern-matches on the narrow type now compiles but fails at runtime because the type system lost the constraint.

**Real pattern:** A function returns a union type. The caller stores it in a variable annotated with the base type. A later switch statement is "exhaustive" against the base type but misses a variant, and the compiler doesn't warn because the annotation erased the narrow type.

---

## Data Flow

### Automated Expansion / Derived Data Collisions

**What to look for:** A derived set (computed from a source set) that can overlap with its source set in the same output collection. When both are added to the same result, duplicates or conflicts arise.

**Why this matters:** The derived set was computed to *extend* the source, but nothing prevents the extension from including items already in the source. The output has duplicates, or worse, conflicting versions of the same item.

**Real pattern:** A function computes "related items" from a set of "matched items" and adds both to the response. A related item can also be a matched item. The response contains the same item twice with different metadata.

---

### Dual Code Paths After Refactoring

**What to look for:** A function that was split during refactoring into two call sites, each independently performing a subset of the original steps. Check whether both paths still execute all required steps, or whether some steps were accidentally left in only one path.

**Why this matters:** Before the refactor, there was one code path that did steps A → B → C. After the split, path 1 does A → B and path 2 does A → C. Step B is missing from path 2 and step C is missing from path 1. Both paths "work" in isolation but produce incomplete results.

**Real pattern:** A notification function is split into "urgent" and "normal" paths. The urgent path sends the message but skips the audit log. The normal path logs but uses a stale template. Neither path does everything the original did.
