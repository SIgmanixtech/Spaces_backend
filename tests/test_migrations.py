"""Alembic revisions, exercised on the disposable test database only.

Covers what a review of a migration needs to know: that it matches the models,
that rows which existed before it survive the upgrade and a clean downgrade
untouched, and that the downgrade refuses to throw away data created since.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from tests.conftest import BACKEND_ROOT

NEW_SPACE_COLUMNS = {
    "building_id",
    "floor_id",
    "parent_space_id",
    "depth",
    "parent_depth",
    "area_sqft",
    "is_rentable",
}
LEGACY_SPACE_TYPES = ["DESK", "PRIVATE_OFFICE", "CABIN", "MEETING_ROOM", "COWORKING"]


def alembic_config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


def current_revision() -> str:
    with get_engine().connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


@pytest.fixture
def migrations(migrated_database: None) -> Iterator[Config]:
    """Hand the test database back empty and at the latest revision, whatever the test did."""
    # Pooled connections may hold plans for a table shape that is about to change.
    dispose_engine()
    try:
        yield alembic_config()
    finally:
        dispose_engine()
        # Upgrade first: a test may leave an older revision, which lacks tables
        # added since (0004's allocations), and the truncate names every table.
        command.upgrade(alembic_config(), "head")
        dispose_engine()
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        with get_engine().begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        dispose_engine()


def insert_legacy_data(connection: Connection) -> None:
    """Rows shaped exactly as revision 0001 stores them: no floors, no hierarchy."""
    property_id = connection.execute(
        text(
            "INSERT INTO properties (name, property_type, category, address, city, state,"
            " postal_code) VALUES ('Legacy Tower', 'OFFICE', 'COMMERCIAL', '1 Old Road',"
            " 'Hyderabad', 'Telangana', '500001') RETURNING id"
        )
    ).scalar_one()
    layout_id = connection.execute(
        text("INSERT INTO layouts (property_id, name) VALUES (:p, 'Old plan') RETURNING id"),
        {"p": property_id},
    ).scalar_one()
    space_ids = [
        connection.execute(
            text(
                "INSERT INTO spaces (property_id, layout_id, name, code, space_type, capacity,"
                " price_amount, price_unit, x, y, width, height, attributes, archived_at)"
                " VALUES (:p, :l, :name, :code, :type, 4, 450.50, 'DAY', :n, 0, 1, 1,"
                " '{\"window\": true}', :archived) RETURNING id"
            ),
            {
                "p": property_id,
                "l": layout_id if number % 2 else None,
                "name": f"Legacy {space_type}",
                "code": f"L-{number}",
                "type": space_type,
                "n": number,
                "archived": "2026-01-01T00:00:00+00:00" if space_type == "COWORKING" else None,
            },
        ).scalar_one()
        for number, space_type in enumerate(LEGACY_SPACE_TYPES)
    ]
    connection.execute(
        text(
            "INSERT INTO bookings (property_id, space_id, customer_name, start_at, end_at, status)"
            " VALUES (:p, :s, 'Priya Nair', '2031-01-06T09:00:00+00:00',"
            " '2031-01-06T10:00:00+00:00', 'CONFIRMED')"
        ),
        {"p": property_id, "s": space_ids[0]},
    )
    tenant_id = connection.execute(
        text("INSERT INTO tenants (name) VALUES ('Acme Ltd') RETURNING id")
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO tenant_occupancies (tenant_id, property_id, space_id, start_date)"
            " VALUES (:t, :p, :s, '2026-01-01')"
        ),
        {"t": tenant_id, "p": property_id, "s": space_ids[1]},
    )


def snapshot(connection: Connection) -> dict[str, list[dict]]:
    """Every row of the tables that reference spaces, as JSON, in a stable order."""
    return {
        table: [
            row[0]
            for row in connection.execute(
                text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY t.id")  # noqa: S608
            )
        ]
        for table in (
            "properties",
            "layouts",
            "spaces",
            "bookings",
            "tenants",
            "tenant_occupancies",
        )
    }


def test_models_and_migrations_describe_the_same_schema(migrations: Config) -> None:
    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    assert differences == []


def test_upgrade_keeps_every_existing_row_and_a_clean_downgrade_restores_them(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0001")
    assert current_revision() == "0001"
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        before = snapshot(connection)
    assert len(before["spaces"]) == 5

    command.upgrade(migrations, "0002")
    assert current_revision() == "0002"
    with get_engine().connect() as connection:
        after = snapshot(connection)

    # Nothing but spaces changed shape, and nothing changed content.
    for table in before:
        if table != "spaces":
            assert after[table] == before[table], table
    for old, new in zip(before["spaces"], after["spaces"], strict=True):
        assert {key: value for key, value in new.items() if key not in NEW_SPACE_COLUMNS} == old
        # Every existing space is unassigned: the migration guesses no floor.
        assert {key: new[key] for key in NEW_SPACE_COLUMNS} == {
            "building_id": None,
            "floor_id": None,
            "parent_space_id": None,
            "depth": 0,
            "parent_depth": None,
            "area_sqft": None,
            "is_rentable": True,
        }
    with get_engine().connect() as connection:
        created = connection.execute(
            text("SELECT (SELECT count(*) FROM buildings), (SELECT count(*) FROM floors)")
        ).one()
    assert tuple(created) == (0, 0)  # no inventory was invented

    # Untouched since the upgrade, so the downgrade is allowed and loses nothing.
    command.downgrade(migrations, "0001")
    with get_engine().connect() as connection:
        assert snapshot(connection) == before


def test_downgrade_refuses_to_discard_hierarchy_data(migrations: Config) -> None:
    command.downgrade(migrations, "0002")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
        building_id = connection.execute(
            text("INSERT INTO buildings (property_id, name) VALUES (:p, 'Block A') RETURNING id"),
            {"p": property_id},
        ).scalar_one()
        floor_id = connection.execute(
            text(
                "INSERT INTO floors (building_id, name, level_number)"
                " VALUES (:b, 'Level 1', 1) RETURNING id"
            ),
            {"b": building_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO spaces (property_id, building_id, floor_id, name, space_type)"
                " VALUES (:p, :b, :f, 'Whole floor', 'FLOOR')"
            ),
            {"p": property_id, "b": building_id, "f": floor_id},
        )
        before = snapshot(connection)

    with pytest.raises(DBAPIError, match="Cannot downgrade past 0002: 1 space"):
        command.downgrade(migrations, "0001")

    # The refusal rolled the whole downgrade back: same revision, same rows.
    assert current_revision() == "0002"
    with get_engine().connect() as connection:
        assert snapshot(connection) == before


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("area_sqft = 1450", True),
        ("is_rentable = false", True),
        ("name = 'Renamed'", False),
    ],
)
def test_downgrade_guard_covers_each_new_column(
    migrations: Config, change: str, refused: bool
) -> None:
    command.downgrade(migrations, "0002")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        connection.execute(text(f"UPDATE spaces SET {change} WHERE code = 'L-0'"))  # noqa: S608
    if refused:
        with pytest.raises(DBAPIError, match="Cannot downgrade past 0002"):
            command.downgrade(migrations, "0001")
        assert current_revision() == "0002"
    else:
        command.downgrade(migrations, "0001")
        assert current_revision() == "0001"


def test_revision_0002_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``alembic upgrade 0001:0002 --sql`` is how the change is reviewed before it is applied."""
    command.upgrade(alembic_config(), "0001:0002", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0002:0001", sql=True)
    downgrade = capsys.readouterr().out

    assert "ALTER TABLE spaces ADD COLUMN floor_id UUID" in upgrade
    assert "CONSTRAINT fk_spaces_parent_space_id_spaces" in upgrade
    assert "DEFERRABLE INITIALLY DEFERRED" in upgrade
    assert "UPDATE alembic_version SET version_num='0002'" in upgrade
    # Purely additive: nothing is dropped except the type list it replaces, and no rows change.
    assert upgrade.count("DROP ") == 1 and "DROP CONSTRAINT ck_spaces_space_type_valid" in upgrade
    assert "INSERT INTO spaces" not in upgrade and "UPDATE spaces" not in upgrade
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade

    assert downgrade.index("Cannot downgrade past 0002") < downgrade.index("DROP COLUMN")


# --- revision 0003: file metadata ---------------------------------------------------

NEW_FILE_COLUMNS = {
    "asset_id",
    "width_px",
    "height_px",
    "caption",
    "description",
    "category",
    "sort_order",
    "is_primary",
}


def insert_files(connection: Connection) -> dict[str, str]:
    """Files shaped as 0002 stores them: two covers, a gallery picture and two plans."""
    property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
    layout_id = connection.execute(text("SELECT id FROM layouts")).scalar_one()
    made: dict[str, str] = {}
    for name, kind, layout, created in (
        ("old-cover", "MAIN", None, "2026-01-01"),
        ("new-cover", "MAIN", None, "2026-02-01"),
        ("gallery", "GALLERY", None, "2026-03-01"),
        ("old-plan", "FLOOR_PLAN", layout_id, "2026-01-01"),
        ("new-plan", "FLOOR_PLAN", layout_id, "2026-02-01"),
    ):
        made[name] = connection.execute(
            text(
                "INSERT INTO files (property_id, layout_id, kind, bucket, object_path,"
                " original_name, content_type, size_bytes, created_at)"
                " VALUES (:p, :l, :k, 'property-files', :path, :name, 'image/png', 10, :c)"
                " RETURNING id"
            ),
            {
                "p": property_id,
                "l": layout,
                "k": kind,
                "path": f"x/{name}.png",
                "name": name,
                "c": created,
            },
        ).scalar_one()
    return {name: str(file_id) for name, file_id in made.items()}


def files_snapshot(connection: Connection) -> list[dict]:
    return [
        row[0] for row in connection.execute(text("SELECT to_jsonb(f) FROM files f ORDER BY f.id"))
    ]


def test_0003_keeps_files_and_marks_the_covers_and_plans_already_in_use(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0002")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        made = insert_files(connection)
        before = files_snapshot(connection)

    command.upgrade(migrations, "0003")
    with get_engine().connect() as connection:
        after = files_snapshot(connection)
    for old, new in zip(before, after, strict=True):
        assert {key: value for key, value in new.items() if key not in NEW_FILE_COLUMNS} == old
        assert (new["caption"], new["sort_order"], new["asset_id"]) == (None, 0, None)
    primary = {row["original_name"] for row in after if row["is_primary"]}
    # The newest MAIN of the property, and the newest plan of the layout: what the app showed.
    assert primary == {"new-cover", "new-plan"}
    assert made  # ids exist

    command.downgrade(migrations, "0002")
    with get_engine().connect() as connection:
        assert files_snapshot(connection) == before


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("caption = 'Lobby'", True),
        ("category = 'Exterior'", True),
        ("description = 'North face'", True),
        ("sort_order = 2", True),
        ("is_primary = false", False),  # a cover choice is dropped, re-derived on upgrade
        ("width_px = 10, height_px = 10", False),
    ],
)
def test_0003_downgrade_refuses_to_discard_file_details(
    migrations: Config, change: str, refused: bool
) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        insert_files(connection)
        connection.execute(
            text(f"UPDATE files SET {change} WHERE original_name = 'gallery'")  # noqa: S608
        )
    if refused:
        with pytest.raises(DBAPIError, match="Cannot downgrade past 0003: 1 file"):
            command.downgrade(migrations, "0002")
        assert current_revision() == "0003"
    else:
        command.downgrade(migrations, "0002")
        assert current_revision() == "0002"


def test_0003_downgrade_refuses_while_assets_have_images(migrations: Config) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        insert_legacy_data(connection)
        property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
        asset_id = connection.execute(
            text(
                "INSERT INTO assets (property_id, name, category) VALUES (:p, 'Desk', 'FURNITURE')"
                " RETURNING id"
            ),
            {"p": property_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO files (property_id, asset_id, kind, bucket, object_path,"
                " original_name, content_type, size_bytes)"
                " VALUES (:p, :a, 'ASSET', 'b', 'x/desk.png', 'desk.png', 'image/png', 1)"
            ),
            {"p": property_id, "a": asset_id},
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0003"):
        command.downgrade(migrations, "0002")
    assert current_revision() == "0003"


def test_revision_0003_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0002:0003", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0003:0002", sql=True)
    downgrade = capsys.readouterr().out

    assert "ALTER TABLE files ADD COLUMN asset_id UUID" in upgrade
    assert "NULLS NOT DISTINCT" in upgrade and "WHERE is_primary" in upgrade
    assert "UPDATE alembic_version SET version_num='0003'" in upgrade
    # Additive: only the kind list is replaced; rows are only ever flagged, never removed.
    assert upgrade.count("DROP ") == 1 and "DROP CONSTRAINT ck_files_kind_valid" in upgrade
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade
    assert upgrade.index("UPDATE files SET is_primary") < upgrade.index(
        "CREATE UNIQUE INDEX uq_files_primary_per_owner"
    )
    assert downgrade.index("Cannot downgrade past 0003") < downgrade.index("DROP COLUMN")


# --- revision 0004: contracts and the allocation engine --------------------------------


def floor_with_portion(connection: Connection) -> dict[str, object]:
    """Floor 5 > Portion 5A > Desk, shaped as revision 0003 stores them."""
    ids: dict[str, object] = {}
    ids["property"] = connection.execute(
        text(
            "INSERT INTO properties (name, property_type, category, address, city, state,"
            " postal_code) VALUES ('Sigma Hub', 'COMMERCIAL', 'OFFICE', '1 Road', 'Hyderabad',"
            " 'Telangana', '500001') RETURNING id"
        )
    ).scalar_one()
    ids["building"] = connection.execute(
        text("INSERT INTO buildings (property_id, name) VALUES (:p, 'A') RETURNING id"),
        {"p": ids["property"]},
    ).scalar_one()
    ids["floor"] = connection.execute(
        text(
            "INSERT INTO floors (building_id, name, level_number) VALUES (:b, 'Fifth', 5)"
            " RETURNING id"
        ),
        {"b": ids["building"]},
    ).scalar_one()
    ids["root"] = connection.execute(
        text(
            "INSERT INTO spaces (property_id, building_id, floor_id, name, space_type, depth,"
            " is_rentable) VALUES (:p, :b, :f, 'Floor 5', 'FLOOR', 0, false) RETURNING id"
        ),
        {"p": ids["property"], "b": ids["building"], "f": ids["floor"]},
    ).scalar_one()
    ids["5A"] = connection.execute(
        text(
            "INSERT INTO spaces (property_id, building_id, floor_id, parent_space_id, depth,"
            " parent_depth, name, space_type) VALUES (:p, :b, :f, :r, 1, 0, '5A', 'PORTION')"
            " RETURNING id"
        ),
        {"p": ids["property"], "b": ids["building"], "f": ids["floor"], "r": ids["root"]},
    ).scalar_one()
    ids["desk"] = connection.execute(
        text(
            "INSERT INTO spaces (property_id, building_id, floor_id, parent_space_id, depth,"
            " parent_depth, name, space_type) VALUES (:p, :b, :f, :a, 2, 1, 'Desk', 'DESK')"
            " RETURNING id"
        ),
        {"p": ids["property"], "b": ids["building"], "f": ids["floor"], "a": ids["5A"]},
    ).scalar_one()
    ids["tenant"] = connection.execute(
        text("INSERT INTO tenants (name) VALUES ('Acme') RETURNING id")
    ).scalar_one()
    return ids


def legacy_booking(
    connection: Connection,
    ids: dict,
    space: str,
    start: str,
    end: str,
    status: str = "CONFIRMED",
) -> object:
    return connection.execute(
        text(
            "INSERT INTO bookings (property_id, space_id, customer_name, start_at, end_at, status)"
            " VALUES (:p, :s, 'Legacy', :a, :b, :st) RETURNING id"
        ),
        {"p": ids["property"], "s": ids[space], "a": start, "b": end, "st": status},
    ).scalar_one()


def test_0004_turns_occupancies_into_contracts_and_allocates_current_holds(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        future = legacy_booking(
            connection, ids, "desk", "2031-01-06 09:00+00", "2031-01-06 10:00+00"
        )
        legacy_booking(connection, ids, "desk", "2020-01-06 09:00+00", "2020-01-06 10:00+00")
        legacy_booking(
            connection, ids, "5A", "2031-01-06 09:00+00", "2031-01-06 10:00+00", "CANCELLED"
        )
        occupancy = connection.execute(
            text(
                "INSERT INTO tenant_occupancies (tenant_id, property_id, space_id, start_date)"
                " VALUES (:t, :p, :s, '2032-01-01') RETURNING id"
            ),
            {"t": ids["tenant"], "p": ids["property"], "s": ids["5A"]},
        ).scalar_one()

    command.upgrade(migrations, "0004")
    with get_engine().connect() as connection:
        contract = connection.execute(
            text("SELECT reference, status, activated_at IS NOT NULL FROM tenant_occupancies")
        ).one()
        assert contract[0].startswith("CT-") and tuple(contract[1:]) == ("ACTIVE", True)
        allocations = connection.execute(
            text("SELECT source_type, booking_id, contract_id FROM allocations ORDER BY 1")
        ).all()
        # The future booking and the contract; not the past or the cancelled booking.
        assert [(a[0], a[1] or a[2]) for a in allocations] == [
            ("BOOKING", future),
            ("CONTRACT", occupancy),
        ]
        chain = connection.execute(
            text(
                "SELECT s.name, c.claim_mode FROM allocation_claims c"
                " JOIN allocations a ON a.id = c.allocation_id JOIN spaces s ON s.id = c.space_id"
                " WHERE a.booking_id = :b ORDER BY c.claim_mode, s.name"
            ),
            {"b": future},
        ).all()
        assert [tuple(row) for row in chain] == [("Desk", 1), ("5A", 2), ("Floor 5", 2)]


def test_0004_refuses_to_upgrade_over_existing_hierarchy_conflicts(migrations: Config) -> None:
    command.downgrade(migrations, "0003")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        legacy_booking(connection, ids, "desk", "2031-01-06 09:00+00", "2031-01-06 10:00+00")
        legacy_booking(connection, ids, "5A", "2031-01-06 09:30+00", "2031-01-06 11:00+00")

    with pytest.raises(DBAPIError, match="Cannot upgrade to 0004: 1 pair"):
        command.upgrade(migrations, "0004")
    assert current_revision() == "0003"
    # Resolved deliberately, the upgrade goes through.
    with get_engine().begin() as connection:
        connection.execute(
            text("UPDATE bookings SET status = 'CANCELLED' WHERE space_id = :s"), {"s": ids["5A"]}
        )
    command.upgrade(migrations, "0004")
    assert current_revision() == "0004"


@pytest.mark.parametrize(
    ("change", "refused"),
    [
        ("status = 'PENDING'", True),
        ("status = 'CANCELLED', cancelled_at = now()", True),
        ("terms = 'Lock-in 12 months'", True),
        ("monthly_rent = 50000", True),
        ("notes = 'Plain note'", False),
    ],
)
def test_0004_downgrade_refuses_to_discard_contract_data(
    migrations: Config, change: str, refused: bool
) -> None:
    command.downgrade(migrations, "0004")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        connection.execute(
            text(
                "INSERT INTO tenant_occupancies (tenant_id, property_id, start_date)"
                " VALUES (:t, :p, '2032-01-01')"
            ),
            {"t": ids["tenant"], "p": ids["property"]},
        )
        connection.execute(text(f"UPDATE tenant_occupancies SET {change}"))  # noqa: S608
    if refused:
        with pytest.raises(DBAPIError, match="Cannot downgrade past 0004: 1 contract"):
            command.downgrade(migrations, "0003")
        assert current_revision() == "0004"
    else:
        command.downgrade(migrations, "0003")
        assert current_revision() == "0003"


def test_revision_0004_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0003:0004", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0004:0003", sql=True)
    downgrade = capsys.readouterr().out

    assert "CREATE TABLE allocations" in upgrade and "CREATE TABLE allocation_claims" in upgrade
    assert "ex_allocation_claims_mixed" in upgrade and "trg_spaces_guard" in upgrade
    assert "Cannot upgrade to 0004" in upgrade
    assert "UPDATE alembic_version SET version_num='0004'" in upgrade
    # Nothing is deleted; only the old one-occupant exclusion is replaced.
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade
    assert downgrade.index("Cannot downgrade past 0004") < downgrade.index("DROP TABLE")


def test_0005_keeps_existing_spaces_and_their_list_prices_and_creates_no_rule(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0004")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        connection.execute(
            text("UPDATE spaces SET price_amount = 999, price_unit = 'MONTH' WHERE id = :d"),
            {"d": ids["desk"]},
        )
        before = snapshot(connection)
    command.upgrade(migrations, "0005")
    with get_engine().connect() as connection:
        assert snapshot(connection) == before
        assert connection.execute(text("SELECT count(*) FROM pricing_rules")).scalar_one() == 0


@pytest.mark.parametrize("what", ["rule", "seat"])
def test_0005_downgrade_refuses_to_discard_prices_or_new_space_types(
    migrations: Config, what: str
) -> None:
    command.downgrade(migrations, "0005")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        if what == "rule":
            connection.execute(
                text(
                    "INSERT INTO pricing_rules (property_id, target_type, rate_basis,"
                    " billing_period, amount, effective_from)"
                    " VALUES (:p, 'PROPERTY', 'PER_SQFT', 'MONTHLY', 70, '2026-01-01')"
                ),
                {"p": ids["property"]},
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO spaces (property_id, building_id, floor_id, parent_space_id,"
                    " depth, parent_depth, name, space_type)"
                    " VALUES (:p, :b, :f, :a, 2, 1, 'Seat 1', 'SEAT')"
                ),
                {"p": ids["property"], "b": ids["building"], "f": ids["floor"], "a": ids["5A"]},
            )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0005"):
        command.downgrade(migrations, "0004")
    assert current_revision() == "0005"


def test_0005_refuses_overlapping_active_rules_in_the_database(migrations: Config) -> None:
    insert = text(
        "INSERT INTO pricing_rules (property_id, target_type, rate_basis, billing_period,"
        " amount, effective_from, effective_to, status)"
        " VALUES (:p, 'PROPERTY', 'PER_SQFT', 'MONTHLY', :a, :f, :t, :s)"
    )
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        row = {"p": ids["property"], "a": 70, "f": "2026-01-01", "t": "2026-12-31", "s": "ACTIVE"}
        connection.execute(insert, row)
        connection.execute(insert, {**row, "s": "DRAFT"})  # drafts never clash
        connection.execute(insert, {**row, "f": "2027-01-01", "t": None})  # later dates
    with (
        pytest.raises(DBAPIError, match="ex_pricing_rules_active_overlap"),
        get_engine().begin() as connection,
    ):
        connection.execute(insert, {**row, "f": "2026-06-01", "t": None})


def test_revision_0005_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0004:0005", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0005:0004", sql=True)
    downgrade = capsys.readouterr().out

    assert "CREATE TABLE pricing_rules" in upgrade
    assert "ex_pricing_rules_active_overlap" in upgrade and "'SEAT'" in upgrade
    assert "INSERT INTO pricing_rules" not in upgrade  # no example prices
    assert "DELETE FROM" not in upgrade and "TRUNCATE" not in upgrade
    assert downgrade.index("Cannot downgrade past 0005") < downgrade.index("DROP TABLE")


def test_0006_keeps_every_row_and_publishes_nothing(migrations: Config) -> None:
    command.downgrade(migrations, "0005")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        connection.execute(text("INSERT INTO tenants (name) VALUES ('Acme')"))
        connection.execute(
            text(
                "INSERT INTO bookings (property_id, space_id, customer_name, start_at, end_at)"
                " VALUES (:p, :d, 'Walk-in', '2031-01-01 09:00+00', '2031-01-01 10:00+00')"
            ),
            {"p": ids["property"], "d": ids["desk"]},
        )
        before = snapshot(connection)
    command.upgrade(migrations, "0006")
    with get_engine().connect() as connection:
        after = snapshot(connection)
        assert (
            connection.execute(
                text("SELECT count(*) FROM properties WHERE is_published")
            ).scalar_one()
            == 0
        )
        assert connection.execute(text("SELECT channel FROM bookings")).scalar_one() == "ADMIN"
    for table, rows in before.items():
        assert [
            {k: v for k, v in row.items() if k in rows_before}
            for row, rows_before in zip(after[table], rows, strict=True)
        ] == rows


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE properties SET is_published = true",
        "INSERT INTO app_users (id, email, role) VALUES (gen_random_uuid(), 'c@x.com', 'CUSTOMER')",
    ],
)
def test_0006_downgrade_refuses_to_discard_customer_data(migrations: Config, change: str) -> None:
    command.downgrade(migrations, "0006")
    with get_engine().begin() as connection:
        floor_with_portion(connection)
        connection.execute(text(change))
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0006"):
        command.downgrade(migrations, "0005")
    assert current_revision() == "0006"


def test_revision_0006_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0005:0006", sql=True)
    upgrade = capsys.readouterr().out
    command.downgrade(alembic_config(), "0006:0005", sql=True)
    downgrade = capsys.readouterr().out
    assert "ADD COLUMN is_published BOOLEAN DEFAULT false NOT NULL" in upgrade
    assert "uq_tenants_user_id" in upgrade
    assert "DELETE FROM" not in upgrade and "UPDATE properties" not in upgrade
    assert downgrade.index("Cannot downgrade past 0006") < downgrade.index("DROP COLUMN")


def test_0007_adds_an_empty_unique_property_code_and_guards_its_downgrade(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0006")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
    command.upgrade(migrations, "0007")
    with get_engine().begin() as connection:
        assert (
            connection.execute(
                text("SELECT code FROM properties WHERE id = :p"), {"p": ids["property"]}
            ).scalar_one()
            is None
        )
        connection.execute(text("UPDATE properties SET code = 'SH'"))
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0007"):
        command.downgrade(migrations, "0006")
    assert current_revision() == "0007"


def test_revision_0007_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0006:0007", sql=True)
    upgrade = capsys.readouterr().out
    assert "ALTER TABLE properties ADD COLUMN code VARCHAR(50)" in upgrade
    assert "uq_properties_code" in upgrade
    assert "UPDATE properties" not in upgrade and "DELETE FROM" not in upgrade


def test_0008_keeps_prices_configured_and_guards_its_downgrade(migrations: Config) -> None:
    command.downgrade(migrations, "0007")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        connection.execute(
            text(
                "INSERT INTO pricing_rules (property_id, target_type, rate_basis, billing_period,"
                " amount, effective_from, status)"
                " VALUES (:p, 'PROPERTY', 'PER_SQFT', 'MONTHLY', 70, '2026-01-01', 'ACTIVE')"
            ),
            {"p": ids["property"]},
        )
    command.upgrade(migrations, "0008")
    with get_engine().begin() as connection:
        assert connection.execute(text("SELECT source FROM pricing_rules")).scalar_one() == (
            "CONFIGURED"
        )
        assert connection.execute(text("SELECT count(*) FROM billing_profiles")).scalar_one() == 0
        connection.execute(
            text(
                "INSERT INTO billing_profiles (property_id, hours_per_day, days_per_week,"
                " days_per_month, weekly_discount_pct, monthly_discount_pct, rounding)"
                " VALUES (:p, 8, 6, 26, 0, 0, 'PAISA')"
            ),
            {"p": ids["property"]},
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0008"):
        command.downgrade(migrations, "0007")
    assert current_revision() == "0008"


def test_revision_0008_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0007:0008", sql=True)
    upgrade = capsys.readouterr().out
    assert "CREATE TABLE billing_profiles" in upgrade
    assert "coalesce(building_id" in upgrade
    assert "INSERT INTO billing_profiles" not in upgrade and "UPDATE pricing_rules" not in upgrade


def test_0009_is_additive_and_guards_its_downgrade(migrations: Config) -> None:
    command.downgrade(migrations, "0008")
    with get_engine().begin() as connection:
        ids = floor_with_portion(connection)
        connection.execute(
            text(
                "INSERT INTO app_users (id, email, role)"
                " VALUES (gen_random_uuid(), 'a@x.io', 'ADMIN')"
            )
        )
    command.upgrade(migrations, "0009")
    with get_engine().begin() as connection:
        # Existing rows are untouched: no type set, no details, every user keeps an email.
        assert (
            connection.execute(
                text("SELECT count(*) FROM spaces WHERE contract_type IS NOT NULL")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM app_users WHERE email IS NULL")
            ).scalar_one()
            == 0
        )
        # A user needs an email or a phone; phones are unique.
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                text("INSERT INTO app_users (id, role) VALUES (gen_random_uuid(), 'CUSTOMER')")
            )
        connection.execute(
            text(
                "INSERT INTO app_users (id, phone, role)"
                " VALUES (gen_random_uuid(), '+919876543210', 'CUSTOMER')"
            )
        )
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO app_users (id, phone, role)"
                    " VALUES (gen_random_uuid(), '+919876543210', 'CUSTOMER')"
                )
            )
        # Only the two contract types are accepted.
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(
                text("UPDATE spaces SET contract_type = 'RENTAL' WHERE id = :s"),
                {"s": ids["5A"]},
            )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0009"):
        command.downgrade(migrations, "0008")
    assert current_revision() == "0009"
    with get_engine().begin() as connection:
        connection.execute(text("DELETE FROM app_users WHERE email IS NULL"))
    command.downgrade(migrations, "0008")
    assert current_revision() == "0008"
    command.upgrade(migrations, "head")


def test_revision_0009_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0008:0009", sql=True)
    upgrade = capsys.readouterr().out
    assert "CREATE TABLE contract_templates" in upgrade
    assert "CREATE TABLE contract_documents" in upgrade
    # No data is written, apart from Alembic's own version marker.
    statements = [line for line in upgrade.splitlines() if line.startswith(("INSERT", "UPDATE"))]
    assert all("alembic_version" in line for line in statements)


def test_0011_and_0012_are_additive_and_0012_seeds_the_expense_types(migrations: Config) -> None:
    command.downgrade(migrations, "0010")
    with get_engine().begin() as connection:
        tables = set(
            connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            ).scalars()
        )
        assert "reimbursement_expense_types" not in tables
    command.upgrade(migrations, "0012")
    with get_engine().begin() as connection:
        names = (
            connection.execute(text("SELECT name FROM reimbursement_expense_types ORDER BY name"))
            .scalars()
            .all()
        )
        assert names == [
            "Diesel generator / power backup",
            "Electricity",
            "Internet",
            "Other shared operational expenses",
            "Waste management",
            "Water",
        ]
        tenant_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'tenants'"
                )
            ).scalars()
        )
        assert {"client_invited_at", "client_activated_at"} <= tenant_columns
    command.downgrade(migrations, "0011")
    with get_engine().begin() as connection:
        assert (
            connection.execute(
                text("SELECT to_regclass('public.reimbursement_terms')")
            ).scalar_one()
            is None
        )
    command.upgrade(migrations, "head")


def test_revision_0012_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0011:0012", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE reimbursement_expense_types" in sql
    assert "Electricity" in sql


def insert_0011_data(connection: Connection) -> None:
    """A property, a price with a deposit and an additional charge, a tenant and a contract,
    as revision 0011 stores them."""
    property_id = connection.execute(
        text(
            "INSERT INTO properties (name, property_type, category, address, city, state,"
            " postal_code) VALUES ('Fee Tower', 'COMMERCIAL', 'OFFICE', '2 New Road',"
            " 'Hyderabad', 'Telangana', '500001') RETURNING id"
        )
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO pricing_rules (property_id, target_type, rate_basis, billing_period,"
            " amount, effective_from, status, additional_charges, deposit_rule, deposit_value)"
            " VALUES (:p, 'PROPERTY', 'PER_SQFT', 'MONTHLY', 70, '2026-01-01', 'ACTIVE',"
            ' \'[{"label": "Electricity", "amount": 500}]\', \'MONTHS_OF_RENT\', 2)'
        ),
        {"p": property_id},
    )
    tenant_id = connection.execute(
        text("INSERT INTO tenants (name) VALUES ('Acme Ltd') RETURNING id")
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO tenant_occupancies (tenant_id, property_id, start_date, security_deposit)"
            " VALUES (:t, :p, '2026-01-01', 50000)"
        ),
        {"t": tenant_id, "p": property_id},
    )


def snapshot_0011(connection: Connection) -> dict[str, list[dict]]:
    return {
        table: [
            row[0]
            for row in connection.execute(
                text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY t.id")  # noqa: S608
            )
        ]
        for table in ("properties", "pricing_rules", "tenants", "tenant_occupancies")
    }


def test_0012_keeps_existing_rows_and_its_seed_is_never_duplicated(migrations: Config) -> None:
    from sqlalchemy.orm import Session

    from app.services.reimbursements import seed_expense_types

    command.downgrade(migrations, "0011")
    with get_engine().begin() as connection:
        insert_0011_data(connection)
        before = snapshot_0011(connection)
    command.upgrade(migrations, "0012")
    with get_engine().connect() as connection:
        # Prices, their additional charges and deposits, and contracts are untouched.
        assert snapshot_0011(connection) == before
    # Re-running the test/e2e seed after the migration adds nothing.
    with Session(get_engine()) as session:
        assert seed_expense_types(session) == 0
        assert seed_expense_types(session) == 0
        session.commit()
    with get_engine().connect() as connection:
        assert connection.execute(
            text("SELECT count(*), count(DISTINCT lower(name)) FROM reimbursement_expense_types")
        ).one() == (6, 6)
    # Nothing entered since, so the downgrade is allowed and loses nothing.
    command.downgrade(migrations, "0011")
    with get_engine().connect() as connection:
        assert snapshot_0011(connection) == before


def test_0012_downgrade_refuses_to_discard_reimbursement_data(migrations: Config) -> None:
    from sqlalchemy.orm import Session

    from app.services.reimbursements import seed_expense_types

    # Start at 0012 itself, whatever revisions came after it.
    command.downgrade(migrations, "0012")
    dispose_engine()
    # The fixture empties every table between tests, the seeded catalogue included.
    with Session(get_engine()) as session:
        seed_expense_types(session)
        session.commit()
    with get_engine().begin() as connection:
        insert_0011_data(connection)
        property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
        electricity = connection.execute(
            text("SELECT id FROM reimbursement_expense_types WHERE name = 'Electricity'")
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO reimbursement_terms (property_id, expense_type_id, method, amount,"
                " recurrence, billing_period) VALUES (:p, :x, 'FIXED', 1000, 'RECURRING',"
                " 'MONTHLY')"
            ),
            {"p": property_id, "x": electricity},
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0012: 1 reimbursement term"):
        command.downgrade(migrations, "0011")
    dispose_engine()
    assert current_revision() == "0012"
    with get_engine().begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM reimbursement_terms")).scalar() == 1
        connection.execute(text("DELETE FROM reimbursement_terms"))
        # An expense type an admin added is kept too.
        connection.execute(
            text("INSERT INTO reimbursement_expense_types (name) VALUES ('Pest control')")
        )
    with pytest.raises(DBAPIError, match=r"0 expense\(s\) and 1 added or renamed"):
        command.downgrade(migrations, "0011")
    dispose_engine()
    with get_engine().begin() as connection:
        connection.execute(
            text("DELETE FROM reimbursement_expense_types WHERE name = 'Pest control'")
        )
    command.downgrade(migrations, "0011")
    assert current_revision() == "0011"


def test_revision_0012_downgrade_renders_its_guard_as_sql(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(alembic_config(), "0012:0011", sql=True)
    sql = capsys.readouterr().out
    assert "Cannot downgrade past 0012" in sql
    assert "DROP TABLE reimbursement_expense_types" in sql


AMENITY_LEVEL_COLUMNS = {"building_id", "floor_id", "space_id"}


def test_0013_keeps_property_amenities_and_its_downgrade_guards_placed_ones(
    migrations: Config,
) -> None:
    command.downgrade(migrations, "0012")
    dispose_engine()
    with get_engine().begin() as connection:
        insert_0011_data(connection)
        property_id = connection.execute(text("SELECT id FROM properties")).scalar_one()
        for name, status in (("Wi-Fi", "ACTIVE"), ("Parking", "INACTIVE")):
            amenity = connection.execute(
                text("INSERT INTO amenities (name) VALUES (:n) RETURNING id"), {"n": name}
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO property_amenities (property_id, amenity_id, status, notes,"
                    " opening_time) VALUES (:p, :a, :s, 'kept', '09:00')"
                ),
                {"p": property_id, "a": amenity, "s": status},
            )
        before = [
            row[0]
            for row in connection.execute(
                text("SELECT to_jsonb(t) FROM property_amenities t ORDER BY t.id")
            )
        ]
    command.upgrade(migrations, "0013")
    dispose_engine()
    with get_engine().begin() as connection:
        after = [
            row[0]
            for row in connection.execute(
                text("SELECT to_jsonb(t) FROM property_amenities t ORDER BY t.id")
            )
        ]
        # Every existing row is a property-level assignment, otherwise unchanged.
        assert [{k: v for k, v in r.items() if k not in AMENITY_LEVEL_COLUMNS} for r in after] == (
            before
        )
        assert all(r[k] is None for r in after for k in AMENITY_LEVEL_COLUMNS)
        building = connection.execute(
            text("INSERT INTO buildings (property_id, name) VALUES (:p, 'A') RETURNING id"),
            {"p": property_id},
        ).scalar_one()
        wifi = connection.execute(text("SELECT id FROM amenities WHERE name = 'Wi-Fi'")).scalar()
        # The same amenity may now also sit at a building of the property.
        connection.execute(
            text(
                "INSERT INTO property_amenities (property_id, amenity_id, building_id)"
                " VALUES (:p, :a, :b)"
            ),
            {"p": property_id, "a": wifi, "b": building},
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade past 0013: 1 building, floor or space"):
        command.downgrade(migrations, "0012")
    dispose_engine()
    assert current_revision() == "0013"
    with get_engine().begin() as connection:
        connection.execute(text("DELETE FROM property_amenities WHERE building_id IS NOT NULL"))
    command.downgrade(migrations, "0012")
    dispose_engine()
    assert current_revision() == "0012"
    with get_engine().connect() as connection:
        assert [
            row[0]
            for row in connection.execute(
                text("SELECT to_jsonb(t) FROM property_amenities t ORDER BY t.id")
            )
        ] == before
        kind = connection.execute(
            text("SELECT contype FROM pg_constraint WHERE conname = 'uq_property_amenities_pair'")
        ).scalar_one()
        assert kind == "u"  # the original (property, amenity) constraint is back


def test_revision_0013_renders_as_sql_without_a_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "0012:0013", sql=True)
    upgrade = capsys.readouterr().out
    assert "ADD COLUMN building_id" in upgrade
    assert "CREATE UNIQUE INDEX uq_property_amenities_floor" in upgrade
    statements = [line for line in upgrade.splitlines() if line.startswith(("INSERT", "UPDATE"))]
    assert all("alembic_version" in line for line in statements)
    command.downgrade(alembic_config(), "0013:0012", sql=True)
    assert "Cannot downgrade past 0013" in capsys.readouterr().out
