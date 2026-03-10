import pytest
from fastapi.testclient import TestClient


def get_client():
    from main import app
    return TestClient(app)


def test_health_check_returns_ok_status():
    """
    Verifies that GET /health returns HTTP 200 with a status field set to "ok".

    This matters because health check endpoints are used by orchestration systems
    (e.g., Kubernetes, load balancers) to determine if the service is ready to
    accept traffic. A missing or broken health endpoint causes the service to be
    removed from rotation even when it is functioning correctly.

    If this contract breaks, deployment systems will mark the service as unhealthy,
    causing outages and preventing rollouts from completing.
    """
    client = get_client()
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_get_items_returns_list_of_items():
    """
    Verifies that GET /items returns HTTP 200 with a non-empty list of item objects,
    each containing at least an 'id' and 'name' field.

    This matters because callers depend on a stable schema to render the item list.
    If the response shape changes silently, clients will crash or display empty data
    with no actionable error.

    If this contract breaks, API consumers will receive unexpected shapes and either
    raise KeyError/AttributeError or silently show blank UI to the user.
    """
    client = get_client()
    response = client.get("/items")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0
    for item in data:
        assert "id" in item
        assert "name" in item


def test_get_item_by_id_returns_correct_item():
    """
    Verifies that GET /items/{item_id} returns the item matching the requested id,
    not a different item or an empty response.

    This matters because users navigate to specific items by id; returning the wrong
    item or a 404 for a valid id violates the basic contract of a resource endpoint
    and can cause data integrity confusion.

    If this contract breaks, the user sees data for the wrong resource, which can
    lead to incorrect actions being taken on the wrong object.
    """
    client = get_client()
    response = client.get("/items/1")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 1


def test_get_item_by_nonexistent_id_returns_404():
    """
    Verifies that GET /items/{item_id} returns HTTP 404 when the requested item
    does not exist, rather than returning an empty body, a 200 with null, or a 500.

    This matters because callers distinguish between 'not found' and 'server error'
    to decide whether to retry, show a user-facing message, or escalate an alert.
    Returning the wrong status code causes callers to misclassify the failure.

    If this contract breaks, callers may silently swallow missing-resource errors
    or trigger unnecessary retries against a permanently absent resource.
    """
    client = get_client()
    response = client.get("/items/9999")
    assert response.status_code == 404
