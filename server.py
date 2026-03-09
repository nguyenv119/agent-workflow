"""
Asyncio bank server using aiohttp.

In-memory balance starts at 0.0. Supports deposit, withdraw, and balance
query over HTTP/JSON.
"""
from aiohttp import web

# In-memory balance state.  Reset by tests between runs.
balance: float = 0.0


def _parse_amount(body: dict) -> float:
    """
    Extract and validate the 'amount' field from a request body dict.

    Raises web.HTTPBadRequest with the appropriate JSON error payload if the
    field is missing, non-numeric, or not strictly positive.
    """
    raw = body.get("amount")

    if raw is None or not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise _BadRequest("invalid amount")

    amount = float(raw)

    if amount <= 0:
        raise _BadRequest("amount must be positive")

    return amount


class _BadRequest(Exception):
    """Carries a JSON error message to be returned as a 400 response."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _json_error(message: str) -> web.Response:
    """Return a 400 JSON error response without raising an HTTP exception."""
    return web.json_response({"error": message}, status=400)


async def handle_balance(request: web.Request) -> web.Response:
    """Return the current balance."""
    return web.json_response({"balance": balance})


async def handle_deposit(request: web.Request) -> web.Response:
    """Add the requested amount to the balance."""
    global balance
    body = await request.json()
    try:
        amount = _parse_amount(body)
    except _BadRequest as exc:
        return _json_error(exc.message)
    balance += amount
    return web.json_response({"balance": balance})


async def handle_withdraw(request: web.Request) -> web.Response:
    """Subtract the requested amount from the balance."""
    global balance
    body = await request.json()
    try:
        amount = _parse_amount(body)
    except _BadRequest as exc:
        return _json_error(exc.message)

    if amount > balance:
        return _json_error("insufficient funds")

    balance -= amount
    return web.json_response({"balance": balance})


def create_app() -> web.Application:
    """Create and return the aiohttp Application."""
    app = web.Application()
    app.router.add_get("/balance", handle_balance)
    app.router.add_post("/deposit", handle_deposit)
    app.router.add_post("/withdraw", handle_withdraw)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080)
