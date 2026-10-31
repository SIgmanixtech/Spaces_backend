"""Admin and client endpoints have separate prefixes; the old paths still answer."""

from fastapi.testclient import TestClient

from app.main import app


def test_the_docs_list_only_admin_client_and_health_paths() -> None:
    paths = app.openapi()["paths"]
    assert paths
    others = [
        p
        for p in paths
        if not p.startswith(("/api/v1/admin/", "/api/v1/client/", "/api/v1/health"))
    ]
    assert others == []
    for expected in (
        "/api/v1/admin/properties",
        "/api/v1/admin/contracts/{contract_id}/agreement",
        "/api/v1/admin/contract-templates",
        "/api/v1/admin/auth/login",
        "/api/v1/client/me",
        "/api/v1/client/phone-otp/request",
        "/api/v1/client/public/free-spaces",
        "/api/v1/client/auth/refresh",
    ):
        assert expected in paths, expected
    # Customer endpoints are not offered under /admin, nor admin ones under /client.
    assert not any(p.startswith("/api/v1/admin/public") for p in paths)
    assert not any(p.startswith("/api/v1/client/properties") for p in paths)


def test_new_and_old_paths_reach_the_same_endpoints() -> None:
    with TestClient(app) as client:
        for path in (
            "/api/v1/admin/properties",
            "/api/v1/properties",
            "/api/v1/client/me",
            "/api/v1/customer/me",
        ):
            assert client.get(path).status_code == 401, path  # exists, needs sign-in
        assert client.get("/api/v1/client/properties").status_code == 404
        assert client.get("/api/v1/admin/me").status_code == 404
