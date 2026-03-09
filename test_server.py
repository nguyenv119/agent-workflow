"""
Tests for the asyncio bank server (server.py).

All tests use aiohttp's TestClient via pytest-aiohttp fixtures so they run
against a real in-process HTTP server without network overhead.
"""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(aiohttp_client):
    """
    Provides a fresh TestClient for each test, importing the app factory from
    server.py.  A fresh import resets the in-memory balance to 0.0 so tests
    are fully isolated.
    """
    # Re-import and reset balance for each test
    import server
    server.balance = 0.0
    app = server.create_app()
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# GET /balance
# ---------------------------------------------------------------------------

async def test_initial_balance_is_zero(client):
    """
    Verifies that the server starts with a balance of 0.0.

    This matters because any non-zero starting balance would mean users
    inherit phantom funds or debts from a previous session, violating the
    invariant that a fresh server has no outstanding state.

    If this contract breaks, a caller that has never performed a transaction
    would observe a non-zero balance, making account auditing impossible.
    """
    resp = await client.get("/balance")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"balance": 0.0}


async def test_balance_endpoint_returns_json_content_type(client):
    """
    Verifies that GET /balance responds with Content-Type: application/json.

    This matters because API clients that parse the response body as JSON will
    fail silently or raise decode errors if the server returns a different
    content type (e.g., text/plain).

    If this contract breaks, any client library that inspects Content-Type
    before deserialising will reject the response or raise an exception.
    """
    resp = await client.get("/balance")
    assert resp.content_type == "application/json"


# ---------------------------------------------------------------------------
# POST /deposit
# ---------------------------------------------------------------------------

async def test_deposit_adds_amount_to_balance_and_returns_new_balance(client):
    """
    Verifies that a valid deposit increases the balance by exactly the
    deposited amount and returns the updated balance in the response body.

    This is the fundamental contract of a deposit operation: money placed into
    the account must be reflected in the balance immediately and accurately.

    If this contract breaks, users would deposit funds and see an incorrect
    balance, causing trust failure and potential financial discrepancy.
    """
    resp = await client.post("/deposit", json={"amount": 100.0})
    assert resp.status == 200
    data = await resp.json()
    assert data == {"balance": 100.0}


async def test_multiple_deposits_accumulate_correctly(client):
    """
    Verifies that successive deposits are additive: each deposit increases the
    running balance, and the final balance equals the sum of all deposits.

    This matters because a bug that resets the balance on each deposit (rather
    than accumulating) would silently destroy previously deposited funds.

    If this contract breaks, a user making two deposits of 50.0 would see a
    balance of 50.0 instead of 100.0, losing half their funds with no error.
    """
    await client.post("/deposit", json={"amount": 50.0})
    resp = await client.post("/deposit", json={"amount": 25.0})
    assert resp.status == 200
    data = await resp.json()
    assert data == {"balance": 75.0}


async def test_deposit_of_negative_amount_returns_400_with_error(client):
    """
    Verifies that depositing a negative amount is rejected with HTTP 400 and
    a descriptive error message, leaving the balance unchanged.

    Accepting negative deposits would be semantically equivalent to an
    uncontrolled withdrawal and would allow balance manipulation through the
    deposit endpoint, bypassing withdrawal safeguards such as the
    insufficient-funds check.

    If this contract breaks, a caller sending {"amount": -50.0} to /deposit
    could reduce the balance without the server returning an error.
    """
    resp = await client.post("/deposit", json={"amount": -50.0})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "amount must be positive"}

    # Balance must not have changed
    bal_resp = await client.get("/balance")
    bal_data = await bal_resp.json()
    assert bal_data == {"balance": 0.0}


async def test_deposit_of_zero_returns_400_with_error(client):
    """
    Verifies that depositing exactly zero is rejected with HTTP 400.

    A zero-amount deposit has no economic effect but would still be processed
    as a valid transaction, creating noise in audit logs and potentially
    masking bugs where callers forget to set the amount field.

    If this contract breaks, callers that accidentally omit an amount (defaulting
    to 0) would receive a 200 response, hiding the programming error.
    """
    resp = await client.post("/deposit", json={"amount": 0.0})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "amount must be positive"}


async def test_deposit_with_missing_amount_returns_400_with_error(client):
    """
    Verifies that a deposit request body missing the 'amount' field is
    rejected with HTTP 400 and an 'invalid amount' error.

    Clients that send malformed requests should receive a clear error rather
    than a server-side exception or a default-value transaction, which could
    deposit 0 or crash with a 500.

    If this contract breaks, a misconfigured client that omits 'amount' would
    receive a 500 Internal Server Error or silently deposit the wrong value,
    making it very hard to diagnose the client-side bug.
    """
    resp = await client.post("/deposit", json={})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "invalid amount"}


async def test_deposit_with_non_numeric_amount_returns_400_with_error(client):
    """
    Verifies that a deposit request body with a non-numeric 'amount' value
    is rejected with HTTP 400 and an 'invalid amount' error.

    If the server attempts arithmetic on a string it would raise a TypeError
    and return 500, which is an implementation detail leaking to the client.
    The correct contract is to validate input and return 400 with a clear message.

    If this contract breaks, callers sending {"amount": "lots"} receive a
    500 Internal Server Error instead of a helpful 400 validation error.
    """
    resp = await client.post("/deposit", json={"amount": "lots"})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "invalid amount"}


# ---------------------------------------------------------------------------
# POST /withdraw
# ---------------------------------------------------------------------------

async def test_withdraw_subtracts_amount_from_balance_and_returns_new_balance(client):
    """
    Verifies that a valid withdrawal decreases the balance by exactly the
    withdrawn amount and returns the updated balance.

    This is the fundamental contract of a withdrawal: funds removed from the
    account must be deducted from the balance accurately.

    If this contract breaks, users could withdraw funds without the balance
    decreasing, effectively allowing infinite withdrawals.
    """
    await client.post("/deposit", json={"amount": 200.0})
    resp = await client.post("/withdraw", json={"amount": 75.0})
    assert resp.status == 200
    data = await resp.json()
    assert data == {"balance": 125.0}


async def test_withdrawal_of_exact_balance_brings_balance_to_zero(client):
    """
    Verifies that withdrawing exactly the current balance succeeds and leaves
    the balance at exactly 0.0 (not negative or epsilon-off).

    This boundary condition matters because floating-point arithmetic can
    introduce small errors, and a subtraction that overshoots zero would
    leave a negative balance that should be impossible given the
    insufficient-funds guard.

    If this contract breaks, withdrawing the full balance would either fail
    with 'insufficient funds' (denying a valid operation) or result in a
    tiny negative balance.
    """
    await client.post("/deposit", json={"amount": 50.0})
    resp = await client.post("/withdraw", json={"amount": 50.0})
    assert resp.status == 200
    data = await resp.json()
    assert data == {"balance": 0.0}


async def test_withdrawal_exceeding_balance_returns_400_and_does_not_modify_balance(client):
    """
    Verifies that attempting to withdraw more than the current balance is
    rejected with HTTP 400 and an 'insufficient funds' error, and that the
    balance remains unchanged after the rejection.

    Allowing overdrafts would violate the bank's core invariant that the
    balance never goes negative. The balance-unchanged requirement ensures the
    rejection is atomic: no partial deduction occurs before the error is raised.

    If this contract breaks, a user with a balance of 50.0 could withdraw
    100.0 and end up with a balance of -50.0, which is an invalid state for
    this server.
    """
    await client.post("/deposit", json={"amount": 50.0})
    resp = await client.post("/withdraw", json={"amount": 100.0})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "insufficient funds"}

    # Balance must be unchanged
    bal_resp = await client.get("/balance")
    bal_data = await bal_resp.json()
    assert bal_data == {"balance": 50.0}


async def test_withdrawal_from_zero_balance_returns_400_insufficient_funds(client):
    """
    Verifies that attempting to withdraw from a zero balance returns 400
    'insufficient funds' (not a different error or a 500).

    Zero balance is a common edge-case state — reached after emptying an
    account or at server start. The server must handle it cleanly rather than
    dividing by zero or choosing the wrong error branch.

    If this contract breaks, a withdrawal on an empty account might return
    a generic 500 error or 'invalid amount', confusing the caller about the
    actual cause of the rejection.
    """
    resp = await client.post("/withdraw", json={"amount": 10.0})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "insufficient funds"}


async def test_withdraw_of_negative_amount_returns_400_with_error(client):
    """
    Verifies that withdrawing a negative amount is rejected with HTTP 400 and
    an 'amount must be positive' error.

    A negative withdrawal is semantically equivalent to a deposit and would
    allow balance inflation through the withdrawal endpoint. All amount
    validation must be symmetric between deposit and withdraw.

    If this contract breaks, sending {"amount": -50.0} to /withdraw would
    increase the balance without going through the /deposit endpoint,
    bypassing any deposit-specific business rules added in the future.
    """
    resp = await client.post("/withdraw", json={"amount": -50.0})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "amount must be positive"}


async def test_withdraw_of_zero_returns_400_with_error(client):
    """
    Verifies that withdrawing exactly zero is rejected with HTTP 400.

    Zero-amount transactions have no economic effect but represent a
    programming error on the caller's side. Rejecting them ensures the server
    enforces a consistent positive-amount invariant on all transactions.

    If this contract breaks, clients that accidentally send amount=0 to
    /withdraw receive a 200 success, hiding their bug.
    """
    resp = await client.post("/withdraw", json={"amount": 0.0})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "amount must be positive"}


async def test_withdraw_with_missing_amount_returns_400_with_error(client):
    """
    Verifies that a withdrawal request with no 'amount' field returns 400
    with 'invalid amount'.

    Same rationale as the deposit equivalent: a missing field must produce a
    client-facing validation error, not a server crash.

    If this contract breaks, a client that omits 'amount' from a withdrawal
    request receives a 500 error that leaks implementation details.
    """
    resp = await client.post("/withdraw", json={})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "invalid amount"}


async def test_withdraw_with_non_numeric_amount_returns_400_with_error(client):
    """
    Verifies that a withdrawal request with a non-numeric 'amount' value
    returns 400 with 'invalid amount'.

    Non-numeric amounts cannot be compared or subtracted numerically; the
    server must validate and reject them rather than propagating a TypeError.

    If this contract breaks, a client sending {"amount": "all"} to /withdraw
    receives a 500 Internal Server Error instead of a 400 validation error.
    """
    resp = await client.post("/withdraw", json={"amount": "all"})
    assert resp.status == 400
    data = await resp.json()
    assert data == {"error": "invalid amount"}
