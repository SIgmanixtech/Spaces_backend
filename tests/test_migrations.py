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
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        with get_engine().begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        command.upgrade(alembic_config(), "head")
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
