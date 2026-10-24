"""Create a property with its buildings, floors, layouts, spaces, assets, amenities,
pricing rules and images, all or nothing.

Every record goes through the same service, and so the same rules, as when it is
created on its own; ``one_transaction`` turns their commits into flushes and this
module commits once at the end. Any refusal rolls the whole transaction back.

File bytes cannot join a database transaction. Each upload stores its bytes first;
if anything fails afterwards, the bytes stored by this request are deleted again
(compensation). Should that deletion itself fail, the bytes are unreferenced and
``FileService.purge_orphans`` removes them later; no record ever points at them.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy.orm import Session

from app.core.errors import AppError, BadRequestError
from app.models.enums import AmenityStatus, FileKind, SpaceType
from app.models.user import AppUser
from app.schemas.file import FileRead
from app.schemas.setup import (
    PROPERTY_REF,
    PropertySetup,
    PropertySetupResult,
    SetupCounts,
    SetupPricing,
    SetupSpace,
)
from app.services.amenities import AmenityService
from app.services.assets import AssetService
from app.services.base import commit, one_transaction
from app.services.billing import BillingProfileService, DerivationService
from app.services.buildings import BuildingService, FloorService
from app.services.files import FileService
from app.services.layouts import LayoutService
from app.services.pricing import PricingService
from app.services.properties import PropertyService
from app.services.spaces import SpaceService
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class UploadedFile:
    name: str | None
    data: bytes


@dataclass
class _State:
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    stored_paths: list[str] = field(default_factory=list)
    files: list[FileRead] = field(default_factory=list)
    # Floor ref -> its whole-floor space, so a floor can be priced as a whole.
    floor_roots: dict[str, uuid.UUID | None] = field(default_factory=dict)


def check_references(setup: PropertySetup, file_count: int) -> None:
    """Every ref unique and every reference pointing at a record of the right kind,
    before anything is written. All problems are reported together."""
    errors: list[dict[str, str]] = []
    kinds: dict[str, str] = {}

    def problem(where: str, message: str) -> None:
        errors.append({"field": where, "message": message})

    for collection in ("buildings", "floors", "layouts", "spaces", "assets"):
        for i, item in enumerate(getattr(setup, collection)):
            if item.ref == PROPERTY_REF:
                problem(f"{collection}.{i}.ref", f'"{PROPERTY_REF}" is reserved.')
            elif item.ref in kinds:
                problem(f"{collection}.{i}.ref", f'The ref "{item.ref}" is used twice.')
            else:
                kinds[item.ref] = collection

    def expect(where: str, ref: str | None, *allowed: str) -> None:
        if ref is None:
            return
        if kinds.get(ref) not in allowed:
            names = " or ".join(a.rstrip("s") for a in allowed)
            problem(where, f'"{ref}" is not a {names} in this request.')

    for i, floor in enumerate(setup.floors):
        expect(f"floors.{i}.building_ref", floor.building_ref, "buildings")
    for i, layout in enumerate(setup.layouts):
        expect(f"layouts.{i}.floor_ref", layout.floor_ref, "floors")
    spaces = {s.ref: s for s in setup.spaces}
    layouts = {layout.ref: layout for layout in setup.layouts}
    for i, space in enumerate(setup.spaces):
        expect(f"spaces.{i}.floor_ref", space.floor_ref, "floors")
        expect(f"spaces.{i}.parent_ref", space.parent_ref, "spaces")
        expect(f"spaces.{i}.layout_ref", space.layout_ref, "layouts")
        parent = spaces.get(space.parent_ref or "")
        if parent is not None and parent.floor_ref != space.floor_ref:
            problem(f"spaces.{i}.parent_ref", "The containing space is on a different floor.")
        layout = layouts.get(space.layout_ref or "")
        if layout is not None and layout.floor_ref != space.floor_ref:
            problem(f"spaces.{i}.layout_ref", "The layout belongs to a different floor.")
        if space.parent_ref == space.ref:
            problem(f"spaces.{i}.parent_ref", "A space cannot contain itself.")
    for i, space in enumerate(setup.spaces):
        seen = {space.ref}
        parent_ref = space.parent_ref
        while parent_ref in spaces:
            if parent_ref in seen:
                problem(f"spaces.{i}.parent_ref", "The spaces contain each other in a circle.")
                break
            seen.add(parent_ref)
            parent_ref = spaces[parent_ref].parent_ref
    for i, asset in enumerate(setup.assets):
        expect(f"assets.{i}.location_ref", asset.location_ref, "buildings", "floors", "spaces")
    for i, rule in enumerate(setup.pricing):
        if rule.target_ref != PROPERTY_REF:
            expect(f"pricing.{i}.target_ref", rule.target_ref, "spaces", "floors", "buildings")
        if rule.derive and setup.billing_profile is None:
            problem(
                f"pricing.{i}.derive",
                "Derived prices need the property's billing profile in this request.",
            )
    used: set[int] = set()
    for i, image in enumerate(setup.images):
        if image.owner_ref != PROPERTY_REF:
            expect(
                f"images.{i}.owner_ref", image.owner_ref, "buildings", "layouts", "spaces", "assets"
            )
        if image.file_index >= file_count:
            problem(f"images.{i}.file_index", f"There is no file number {image.file_index}.")
        elif image.file_index in used:
            problem(f"images.{i}.file_index", "Two images use the same file.")
        used.add(image.file_index)
    if file_count > len(used):
        problem("files", "Every uploaded file must be described by an entry in `images`.")
    if errors:
        raise BadRequestError(
            "The setup refers to records it does not contain. Nothing was saved.", errors=errors
        )


class PropertySetupService:
    def __init__(
        self,
        session: Session,
        actor: AppUser | None,
        storage_factory: Callable[[], ObjectStorage] | None = None,
        *,
        max_upload_bytes: int = 10 * 1024 * 1024,
        max_files_per_owner: int | None = None,
    ) -> None:
        self.session = session
        self.actor = actor
        self.storage_factory = storage_factory
        self.max_upload_bytes = max_upload_bytes
        self.max_files_per_owner = max_files_per_owner

    def create(
        self, setup: PropertySetup, files: Sequence[UploadedFile] = ()
    ) -> PropertySetupResult:
        check_references(setup, len(files))
        storage = self.storage_factory() if setup.images and self.storage_factory else None
        if setup.images and storage is None:
            raise BadRequestError("File storage is not available, so images cannot be saved.")
        state = _State()
        try:
            with one_transaction(self.session):
                prop = self._step(
                    "Property",
                    lambda: PropertyService(self.session, self.actor).create(
                        setup.property.model_dump()
                    ),
                )
                state.ids[PROPERTY_REF] = prop.id
                self._buildings(setup, prop.id, state)
                self._floors(setup, state)
                self._layouts(setup, prop.id, state)
                self._spaces(setup, prop.id, state)
                self._assets(setup, prop.id, state)
                amenities = self._amenities(setup, prop.id)
                derived = self._pricing(setup, prop.id, state)
                if storage is not None:
                    self._images(setup, files, prop.id, state, storage)
            commit(self.session)
        except BaseException:
            self.session.rollback()
            self._compensate(storage, state.stored_paths)
            raise

        service = PropertyService(self.session, self.actor)
        return PropertySetupResult(
            property=service.to_read([service.get(state.ids[PROPERTY_REF])])[0],
            ids={ref: id_ for ref, id_ in state.ids.items() if ref != PROPERTY_REF},
            created=SetupCounts(
                buildings=len(setup.buildings),
                floors=len(setup.floors),
                layouts=len(setup.layouts),
                spaces=len(setup.spaces) + len(state.added),
                assets=len(setup.assets),
                amenities=amenities,
                pricing_rules=len(setup.pricing),
                derived_rules=derived,
                files=len(state.files),
            ),
            added=state.added,
            files=state.files,
        )

    # --- steps -------------------------------------------------------------

    def _step(self, label: str, run: Callable[[], T], where: str | None = None) -> T:
        """Run one record's creation; a refusal names the record and says nothing was saved."""
        try:
            return run()
        except AppError as exc:
            errors = [{"field": where, "message": exc.detail}] if where else exc.errors
            raise type(exc)(
                f"{label}: {exc.detail} Nothing was saved.",
                errors=errors,
                conflicts=exc.conflicts,
            ) from None

    def _buildings(self, setup: PropertySetup, property_id: uuid.UUID, state: _State) -> None:
        service = BuildingService(self.session, self.actor)
        for i, b in enumerate(setup.buildings):
            record = self._step(
                f'Building "{b.name}"',
                lambda b=b: service.create(
                    {"property_id": property_id, "name": b.name, "description": b.description}
                ),
                f"buildings.{i}",
            )
            state.ids[b.ref] = record.id

    def _floors(self, setup: PropertySetup, state: _State) -> None:
        service = FloorService(self.session, self.actor)
        for i, f in enumerate(setup.floors):
            record = self._step(
                f'Floor "{f.name}"',
                lambda f=f: service.create(
                    {
                        "building_id": state.ids[f.building_ref],
                        "name": f.name,
                        "level_number": f.level_number,
                    }
                ),
                f"floors.{i}",
            )
            state.ids[f.ref] = record.id

    def _layouts(self, setup: PropertySetup, property_id: uuid.UUID, state: _State) -> None:
        service = LayoutService(self.session, self.actor)
        building_of = {f.ref: f.building_ref for f in setup.floors}
        for i, layout in enumerate(setup.layouts):
            values = layout.model_dump(exclude={"ref", "floor_ref"})
            values |= {
                "property_id": property_id,
                "floor_id": state.ids[layout.floor_ref],
                "building_id": state.ids[building_of[layout.floor_ref]],
            }
            record = self._step(
                f'Layout "{layout.name}"', lambda v=values: service.create(v), f"layouts.{i}"
            )
            state.ids[layout.ref] = record.id

    def _spaces(self, setup: PropertySetup, property_id: uuid.UUID, state: _State) -> None:
        """Floor by floor: the whole-floor space first (added when the request has none),
        then every space after the space containing it."""
        service = SpaceService(self.session, self.actor)
        index = {s.ref: i for i, s in enumerate(setup.spaces)}
        floor_names = {f.ref: f.name for f in setup.floors}
        priced_floors = {p.target_ref for p in setup.pricing}
        for floor in setup.floors:
            on_floor = [s for s in setup.spaces if s.floor_ref == floor.ref]
            if not on_floor and floor.ref not in priced_floors:
                continue
            roots = [s for s in on_floor if s.space_type == SpaceType.FLOOR]
            if len(roots) > 1:
                raise BadRequestError(
                    f'Floor "{floor.name}" has more than one whole-floor space. Nothing was saved.',
                    errors=[
                        {"field": f"spaces.{index[r.ref]}", "message": "Second whole-floor space."}
                        for r in roots[1:]
                    ],
                )
            if roots and roots[0].parent_ref is not None:
                i = index[roots[0].ref]
                raise BadRequestError(
                    "A whole-floor space cannot sit inside another space. Nothing was saved.",
                    errors=[{"field": f"spaces.{i}.parent_ref", "message": "Remove it."}],
                )
            if not roots:
                name = f"{floor_names[floor.ref]} (whole floor)"
                # A floor priced as a whole is let as a whole, so its root is rentable.
                rentable = floor.ref in priced_floors
                root = self._step(
                    f'Whole-floor space "{name}"',
                    lambda name=name, floor_id=state.ids[floor.ref], rentable=rentable: (
                        service.create(
                            {
                                "property_id": property_id,
                                "floor_id": floor_id,
                                "name": name,
                                "space_type": SpaceType.FLOOR,
                                "is_rentable": rentable,
                            }
                        )
                    ),
                )
                state.floor_roots[floor.ref] = root.id
                state.added.append(
                    f'Whole-floor space "{name}", which every floor needs'
                    + (" (rentable: the floor is priced as a whole)." if rentable else ".")
                )
            else:
                state.floor_roots[floor.ref] = None  # set once the explicit root is saved
            for space in _parents_first(on_floor):
                values = self._space_values(space, property_id, state)
                state.ids[space.ref] = self._step(
                    f'Space "{space.name}"',
                    lambda v=values: service.create(v),
                    f"spaces.{index[space.ref]}",
                ).id
                if space.space_type == SpaceType.FLOOR:
                    state.floor_roots[floor.ref] = state.ids[space.ref]

    def _space_values(
        self, space: SetupSpace, property_id: uuid.UUID, state: _State
    ) -> dict[str, Any]:
        values = space.model_dump(exclude={"ref", "floor_ref", "parent_ref", "layout_ref"})
        values["property_id"] = property_id
        values["layout_id"] = state.ids.get(space.layout_ref) if space.layout_ref else None
        if space.parent_ref:
            values["parent_space_id"] = state.ids[space.parent_ref]
            values["floor_id"] = None
        else:
            values["floor_id"] = state.ids[space.floor_ref]
            values["parent_space_id"] = None
        if values.get("area_sqft") is not None:
            values["area_sqft"] = _decimal(values["area_sqft"])
        return values

    def _assets(self, setup: PropertySetup, property_id: uuid.UUID, state: _State) -> None:
        service = AssetService(self.session, self.actor)
        floors = {f.ref: f for f in setup.floors}
        spaces = {s.ref: s for s in setup.spaces}
        for i, asset in enumerate(setup.assets):
            values = asset.model_dump(exclude={"ref", "location_ref"})
            values |= {"property_id": property_id, "building_id": None, "floor_id": None}
            values["space_id"] = None
            ref = asset.location_ref
            if ref in spaces:
                floor_ref = spaces[ref].floor_ref
                values["space_id"] = state.ids[ref]
                values["floor_id"] = state.ids[floor_ref]
                values["building_id"] = state.ids[floors[floor_ref].building_ref]
            elif ref in floors:
                values["floor_id"] = state.ids[ref]
                values["building_id"] = state.ids[floors[ref].building_ref]
            elif ref is not None:
                values["building_id"] = state.ids[ref]
            record = self._step(
                f'Asset "{asset.name}"', lambda v=values: service.create(v), f"assets.{i}"
            )
            state.ids[asset.ref] = record.id

    def _amenities(self, setup: PropertySetup, property_id: uuid.UUID) -> int:
        service = AmenityService(self.session, self.actor)
        seen: set[str] = set()
        count = 0
        for i, name in enumerate(setup.amenities):
            clean = " ".join(name.split())
            if not clean or clean.casefold() in seen:
                continue
            if len(clean) > 120:
                raise BadRequestError(
                    "An amenity name is longer than 120 characters. Nothing was saved.",
                    errors=[{"field": f"amenities.{i}", "message": "At most 120 characters."}],
                )
            seen.add(clean.casefold())
            self._step(
                f'Amenity "{clean}"',
                lambda clean=clean: service.create(
                    {"property_id": property_id, "name": clean, "status": AmenityStatus.ACTIVE}
                ),
                f"amenities.{i}",
            )
            count += 1
        return count

    def _pricing(self, setup: PropertySetup, property_id: uuid.UUID, state: _State) -> int:
        """Prices, then the derived periods the administrator approved. Returns how many
        derived rules were created."""
        service = PricingService(self.session, self.actor)
        if setup.billing_profile is not None:
            self._step(
                "Billing profile",
                lambda: BillingProfileService(self.session, self.actor).create(
                    {"property_id": property_id, **setup.billing_profile.model_dump()}
                ),
                "billing_profile",
            )
        names = {
            **{s.ref: s.name for s in setup.spaces},
            **{f.ref: f.name for f in setup.floors},
            **{b.ref: b.name for b in setup.buildings},
        }
        buildings = {b.ref for b in setup.buildings}
        floors = {f.ref for f in setup.floors}
        created: list[tuple[int, SetupPricing, uuid.UUID]] = []
        for i, rule in enumerate(setup.pricing):
            values = rule.model_dump(exclude={"ref", "target_ref", "derive", "derive_status"})
            values["property_id"] = property_id
            target = rule.target_ref
            values["space_id"] = None
            values["building_id"] = None
            if target in buildings:
                values["building_id"] = state.ids[target]
            elif target in floors:
                root = state.floor_roots.get(target)
                if root is None:
                    raise BadRequestError(
                        f'Floor "{names[target]}" has no whole-floor space to price. Nothing '
                        "was saved.",
                        errors=[{"field": f"pricing.{i}.target_ref", "message": "No floor root."}],
                    )
                values["space_id"] = root
            elif target != PROPERTY_REF:
                values["space_id"] = state.ids[target]
            label = (
                "Property default price"
                if target == PROPERTY_REF
                else f'Default price of building "{names[target]}"'
                if target in buildings
                else f'Price of "{names[target]}"'
            )
            record = self._step(label, lambda v=values: service.create(v), f"pricing.{i}")
            if rule.ref:
                state.ids[rule.ref] = record.id
            if rule.derive:
                created.append((i, rule, record.id))
        derived = 0
        explicit = {
            (p.target_ref, p.applies_to, p.rate_basis, p.billing_period) for p in setup.pricing
        }
        derivations = DerivationService(self.session, self.actor)
        for i, rule, rule_id in created:
            periods = [
                p
                for p in rule.derive
                if (rule.target_ref, rule.applies_to, rule.rate_basis, p) not in explicit
            ]
            if not periods:
                continue
            result = self._step(
                f"Derived prices of pricing {i + 1}",
                lambda rid=rule_id, ps=periods, st=rule.derive_status: derivations.approve(
                    rid, ps, st
                ),
                f"pricing.{i}.derive",
            )
            derived += len(result.created)
        return derived

    def _images(
        self,
        setup: PropertySetup,
        files: Sequence[UploadedFile],
        property_id: uuid.UUID,
        state: _State,
        storage: ObjectStorage,
    ) -> None:
        service = FileService(self.session, storage, self.actor)
        owner_column = {
            "buildings": "building_id",
            "layouts": "layout_id",
            "spaces": "space_id",
            "assets": "asset_id",
        }
        kinds = {
            **{b.ref: "buildings" for b in setup.buildings},
            **{lay.ref: "layouts" for lay in setup.layouts},
            **{s.ref: "spaces" for s in setup.spaces},
            **{a.ref: "assets" for a in setup.assets},
        }
        for i, image in enumerate(setup.images):
            upload = files[image.file_index]
            owner: dict[str, uuid.UUID] = {}
            if image.owner_ref != PROPERTY_REF:
                owner[owner_column[kinds[image.owner_ref]]] = state.ids[image.owner_ref]
            read = self._step(
                f'Image "{upload.name or "file"}"',
                lambda image=image, upload=upload, owner=owner: service.upload(
                    property_id=property_id,
                    kind=FileKind(image.kind),
                    data=upload.data,
                    original_name=upload.name,
                    max_bytes=self.max_upload_bytes,
                    max_per_owner=self.max_files_per_owner,
                    caption=image.caption,
                    description=image.description,
                    category=image.category,
                    sort_order=image.sort_order,
                    is_primary=image.is_primary,
                    on_stored=state.stored_paths.append,
                    **owner,
                ),
                f"images.{i}",
            )
            state.files.append(read)

    @staticmethod
    def _compensate(storage: ObjectStorage | None, paths: list[str]) -> None:
        if storage is None:
            return
        for path in paths:
            try:
                storage.delete(path)
            except Exception:  # noqa: BLE001
                # Unreferenced bytes only; purge_orphans removes them later.
                logger.warning("Could not delete stored bytes after a failed property setup")


def _parents_first(spaces: list[SetupSpace]) -> list[SetupSpace]:
    """Spaces ordered so each comes after the space containing it (the whole-floor
    space first). Request order is kept otherwise."""
    refs = {s.ref for s in spaces}
    done: set[str] = set()
    ordered: list[SetupSpace] = []
    pending = list(spaces)
    while pending:
        for i, space in enumerate(pending):
            parent = space.parent_ref
            if (
                space.space_type == SpaceType.FLOOR
                or parent is None
                or parent not in refs
                or (parent in done)
            ):
                ordered.append(pending.pop(i))
                done.add(space.ref)
                break
        else:  # a cycle; check_references reports these first
            ordered.extend(pending)
            break
    # The whole-floor space must exist before spaces placed directly on the floor.
    ordered.sort(key=lambda s: s.space_type != SpaceType.FLOOR)
    return ordered


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))
