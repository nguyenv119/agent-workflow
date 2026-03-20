# Shared Quality Standards

These standards are the single source of truth for test and code quality across the harness. Both implementers and reviewers reference this file — update here, not in individual skills.

> Examples use Python syntax for illustration; apply the same structure in your project's language.

## A. Test Structure (GIVEN / WHEN / THEN)

Every test body must have three visually distinct sections:

```
GIVEN  — setup: the world the test lives in (mocks, fixtures, state)
WHEN   — action: call the real production function
THEN   — assert: verify the outcome
```

Rules:

1. **GIVEN** sets up the world. Mocks, fixtures, and state live here. Keep it boring — if GIVEN is longer than THEN, you're testing the wrong layer.
2. **WHEN** calls the real, imported production function. Never call a mock here. If WHEN calls a mock, the test tests nothing.
3. **THEN** asserts on observable return values or state. Never dig into mock internals (mock call inspection APIs, argument capture). If you need to verify what was sent to a dependency, the production code should return or expose that information.
4. If you can't express the test as "GIVEN X, WHEN Y happens, THEN Z is true" in one sentence, you're testing the wrong thing or testing too many things.
5. **One behavior per test.** Each test name should complete: "it ______." If you need "and" in that sentence, split it into two tests.
6. **Mocks should be boring.** If mock setup is more complex than the assertion, you're testing infrastructure, not behavior — move up or down a layer.

Example:

```python
def test_expired_token_returns_401():
    """..."""
    # GIVEN — the world the test lives in
    expired_token = create_token(expires_at=datetime(2020, 1, 1))

    # WHEN — call the real production function
    response = authenticate(expired_token)

    # THEN — verify the outcome
    assert response.status_code == 401
    assert response.body["error"] == "token_expired"
```

## B. Docstrings

Every test must have a docstring (or language-equivalent block comment) that answers:

1. **What** behavioral contract or invariant is this test verifying?
2. **Why** does that contract matter to correctness — what real problem does it prevent?
3. **What breaks** if this contract is violated — what symptom would a user or caller observe?

Motivate the **why before the how**. Do not merely describe what the code does.

Example:

```python
def test_second_call_returns_cached_result_without_re_executing():
    """
    Verifies that repeated calls for the same key return the cached result
    rather than re-executing the underlying computation.

    This matters because re-executing can trigger side effects (network calls,
    DB writes) and degrade performance for hot paths.

    If this contract breaks, callers that rely on idempotency will observe
    duplicate side effects and unexpected latency spikes.
    """
```

## C. Mock Discipline

Prefer real over mock. Before writing any mock, work through this hierarchy:

1. **Real** — can you use the real dependency? A real database, a real HTTP server on localhost, a real in-process instance? If yes, use it.
2. **In-memory** — if the real thing is too slow or has external network requirements, use an in-memory alternative. Wire it through a factory or dependency-injection pattern so the same code path runs in tests and production.
3. **Mock** — acceptable only when the real dependency is genuinely unavailable: browser-only APIs in a headless environment, third-party SaaS with no test mode, hardware devices.

Rules:

- **Before writing any mock, ask: "Can I use the real thing?"** If you reach for a mock framework before asking this question, stop.
- **Don't unit-test trivial glue code.** A thin wrapper over an external call is not worth unit-testing in isolation — test it at the integration layer where the real call exercises the full path.
- **Use the factory pattern for routers and services** so tests can inject in-memory dependencies. Production wiring passes the real dependency; test wiring passes the in-memory one. Same code path.
- **Mocks require justification.** Any mock of a dependency with a real or in-memory alternative must include a comment explaining why the alternative wasn't used.

## D. Test Naming

Names must describe the **behavioral contract**, not the implementation:

```
# Bad — describes implementation
test_cache_hit

# Good — describes the contract
second_call_returns_cached_result_without_re_executing
```

The name should read as a specification of expected behavior: what scenario, what outcome.

## E. Core Dependency Flagging

If a test mocks a **core dependency** — anything central to correctness such as persistence layers, external service calls, or core state — add a language-appropriate review comment directly above the mock setup:

```
# REVIEW: mocking core dependency — test may not reflect real behavior
```

This flags that the test may provide false confidence and should be paired with an integration test exercising the real dependency.

## F. Refactor Cleanup Audit

If a change modifies existing functions (not just adds new ones), do a targeted scan for orphaned artifacts:

1. **Dead variables** — In every modified function, check that every declared variable is still read. Pay special attention to variables from the previous approach that survived the refactor.
2. **Stale comments** — In every touched block, verify that inline comments describe the current code, not the code that was there before.
3. **Unused imports** — Check the top of every modified file for imports no longer referenced.

Refactors that change approach (e.g., truncation to chunking, sync to async, single call to loop) reliably leave behind scaffolding from the old approach. The new logic is correct but the old declarations linger.
