from fastapi.testclient import TestClient

from tests.conftest import TEST_DATABASE_URL, TEST_ORIGIN, requires_database


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "sigmanix-spaces-api"
    assert body["environment"] == "test"
    assert response.headers["X-Request-ID"]


def test_readiness_is_503_when_database_is_unreachable(client: TestClient) -> None:
    if TEST_DATABASE_URL:
        import pytest

        pytest.skip("a reachable test database is configured")

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "service_unavailable"
    assert body["detail"] == "The database is not reachable."
    assert body["request_id"] == response.headers["X-Request-ID"]
    # No connection details may leak into the response.
    assert "127.0.0.1" not in response.text
    assert "postgres" not in response.text


@requires_database
def test_readiness_is_ok_with_database(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_unknown_route_uses_standard_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert isinstance(body["detail"], str)


def test_cors_allows_configured_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health",
        headers={"Origin": TEST_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TEST_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_openapi_schema_is_served(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/health/ready" in paths
