"""Admin endpoints live at /admin/api/v1/..., client ones at /client/api/v1/... (chosen 3 Nov
2026); the earlier /api/v1/admin/..., /api/v1/client/... and unprefixed paths still answer."""

from fastapi.testclient import TestClient

from app.main import app


def test_the_docs_list_only_admin_client_and_health_paths() -> None:
    paths = app.openapi()["paths"]
    assert paths
    others = [
        p
        for p in paths
        if not p.startswith(("/admin/api/v1/", "/client/api/v1/", "/api/v1/health"))
    ]
    assert others == []
    for expected in (
        "/admin/api/v1/properties",
        "/admin/api/v1/contracts/{contract_id}/agreement",
        "/admin/api/v1/contract-templates",
        "/admin/api/v1/auth/login",
        "/admin/api/v1/crm/leads",
        "/admin/api/v1/accounts",
        "/client/api/v1/me",
        "/client/api/v1/phone-otp/request",
        "/client/api/v1/public/free-spaces",
        "/client/api/v1/auth/refresh",
    ):
        assert expected in paths, expected
    # The earlier shapes still work but are not documented twice.
    assert not any(p.startswith(("/api/v1/admin/", "/api/v1/client/")) for p in paths)
    # Customer endpoints are not offered under /admin, nor admin ones under /client.
    assert not any(p.startswith("/admin/api/v1/public") for p in paths)
    assert not any(p.startswith("/client/api/v1/properties") for p in paths)


def test_new_and_old_paths_reach_the_same_endpoints() -> None:
    with TestClient(app) as client:
        for path in (
            "/admin/api/v1/properties",
            "/admin/api/v1/crm/leads",
            "/api/v1/admin/properties",
            "/api/v1/properties",
            "/client/api/v1/me",
            "/api/v1/client/me",
            "/api/v1/customer/me",
        ):
            assert client.get(path).status_code == 401, path  # exists, needs sign-in
        for path in (
            "/client/api/v1/properties",
            "/admin/api/v1/me",
            "/api/v1/client/properties",
            "/api/v1/admin/me",
        ):
            assert client.get(path).status_code == 404, path
