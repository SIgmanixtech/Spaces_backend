from fastapi.testclient import TestClient

PROPERTIES = "/api/v1/properties"


def property_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Sigma Tower",
        "property_type": "OFFICE",
        "category": "COMMERCIAL",
        "address": "12 MG Road",
        "city": "Hyderabad",
        "state": "Telangana",
        "postal_code": "500001",
    }
    payload.update(overrides)
    return payload


def create_property(api: TestClient, **overrides: object) -> dict:
    response = api.post(PROPERTIES, json=property_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_empty_database_lists_nothing(api: TestClient) -> None:
    response = api.get(PROPERTIES)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_create_and_read_a_property(api: TestClient) -> None:
    created = create_property(api, latitude=17.385, longitude=78.4867, area_sqft=12500.5)

    assert created["name"] == "Sigma Tower"
    assert created["status"] == "ACTIVE"
    assert created["occupancy_status"] == "VACANT"
    assert created["occupancy_rate"] is None
    assert created["amenities"] == []
    assert created["latitude"] == 17.385
    assert created["area_sqft"] == 12500.5

    fetched = api.get(f"{PROPERTIES}/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_update_changes_only_sent_fields(api: TestClient) -> None:
    created = create_property(api, description="Original")

    response = api.patch(f"{PROPERTIES}/{created['id']}", json={"city": "Bengaluru"})

    assert response.status_code == 200
    body = response.json()
    assert body["city"] == "Bengaluru"
    assert body["description"] == "Original"
    assert body["name"] == "Sigma Tower"


def test_inactive_property_reports_unavailable(api: TestClient) -> None:
    created = create_property(api)

    response = api.patch(f"{PROPERTIES}/{created['id']}", json={"status": "UNDER_MAINTENANCE"})

    assert response.json()["occupancy_status"] == "UNAVAILABLE"


def test_archived_property_disappears_but_is_kept(api: TestClient) -> None:
    created = create_property(api)

    assert api.delete(f"{PROPERTIES}/{created['id']}").status_code == 204

    assert api.get(f"{PROPERTIES}/{created['id']}").status_code == 404
    assert api.get(PROPERTIES).json()["total"] == 0
    archived = api.get(PROPERTIES, params={"include_archived": True}).json()
    assert archived["total"] == 1
    assert archived["items"][0]["archived_at"] is not None


def test_missing_property_is_404(api: TestClient) -> None:
    response = api.get(f"{PROPERTIES}/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["detail"] == "Property not found."


def test_validation_errors_name_the_fields(api: TestClient) -> None:
    response = api.post(PROPERTIES, json={"name": "", "latitude": 200})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    fields = {error["field"] for error in body["errors"]}
    assert {"body.name", "body.latitude", "body.address"} <= fields


def test_read_only_and_unknown_fields_are_rejected(api: TestClient) -> None:
    response = api.post(PROPERTIES, json=property_payload(occupancy_status="OCCUPIED"))

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "body.occupancy_status"


def test_required_field_cannot_be_nulled(api: TestClient) -> None:
    created = create_property(api)

    response = api.patch(f"{PROPERTIES}/{created['id']}", json={"name": None})

    assert response.status_code == 422


def test_search_filter_sort_and_pagination(api: TestClient) -> None:
    create_property(api, name="Alpha Hub", city="Hyderabad")
    create_property(api, name="Beta Plaza", city="Hyderabad")
    create_property(api, name="Gamma Point", city="Chennai", status="INACTIVE")

    search = api.get(PROPERTIES, params={"search": "plaza"}).json()
    assert [item["name"] for item in search["items"]] == ["Beta Plaza"]

    by_city = api.get(PROPERTIES, params={"city": "Hyderabad", "sort": "-name"}).json()
    assert [item["name"] for item in by_city["items"]] == ["Beta Plaza", "Alpha Hub"]

    by_status = api.get(PROPERTIES, params={"status": "INACTIVE"}).json()
    assert [item["name"] for item in by_status["items"]] == ["Gamma Point"]

    page_two = api.get(PROPERTIES, params={"sort": "name", "page": 2, "page_size": 2}).json()
    assert page_two["total"] == 3
    assert page_two["page"] == 2
    assert [item["name"] for item in page_two["items"]] == ["Gamma Point"]


def test_search_treats_wildcards_literally(api: TestClient) -> None:
    create_property(api, name="Alpha Hub")

    assert api.get(PROPERTIES, params={"search": "%"}).json()["total"] == 0
    assert api.get(PROPERTIES, params={"search": "_lpha"}).json()["total"] == 0


def test_values_the_database_cannot_store_are_rejected(api: TestClient) -> None:
    nul = api.get(PROPERTIES, params={"search": "\x00"})
    too_many = api.post(PROPERTIES, json=property_payload(capacity=10**20))
    too_large = api.post(PROPERTIES, json=property_payload(area_sqft=1e30))

    for response in (nul, too_many, too_large):
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
    assert api.get(PROPERTIES).json()["total"] == 0


def test_list_parameter_limits(api: TestClient) -> None:
    assert api.get(PROPERTIES, params={"page_size": 101}).status_code == 422
    assert api.get(PROPERTIES, params={"page": 0}).status_code == 422

    unsortable = api.get(PROPERTIES, params={"sort": "created_by"})
    assert unsortable.status_code == 400
    assert "Allowed" in unsortable.json()["detail"]


def test_changes_are_recorded_in_the_activity_log(api: TestClient, db_session) -> None:
    from sqlalchemy import select

    from app.models import ActivityLog

    created = create_property(api)
    api.patch(f"{PROPERTIES}/{created['id']}", json={"city": "Pune"})

    actions = db_session.scalars(
        select(ActivityLog.action).order_by(ActivityLog.created_at, ActivityLog.action)
    ).all()
    assert sorted(actions) == ["created", "updated"]
