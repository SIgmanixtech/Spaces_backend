"""Company profiles (Phase 4B): the legal entity that owns properties and signs their
agreements. Generating an agreement copies the profile's values into the document, so an
edit here never changes a generated or signed agreement. History names changed fields,
never their values."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, NotFoundError
from app.models.company import CompanyProfile
from app.models.property import Property
from app.models.user import AppUser
from app.schemas.company import CompanyProfileRead, ProfileProperty
from app.services.base import commit, flush, log_activity

FIELD_NAMES = {
    "legal_name": "legal name",
    "registered_address": "registered address",
    "signatory_name": "signatory name",
    "signatory_designation": "signatory designation",
    "signatory_address": "signatory address",
    "gstin": "GSTIN",
    "pan": "PAN",
    "cin": "CIN",
    "notes": "notes",
}


class CompanyProfileService:
    def __init__(self, session: Session, actor: AppUser) -> None:
        self.session = session
        self.actor = actor

    def _log(self, action: str, profile: CompanyProfile, message: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="company_profiles",
            entity_id=profile.id,
            property_id=None,
            message=message,
        )

    def _get(self, profile_id: uuid.UUID) -> CompanyProfile:
        profile = self.session.get(CompanyProfile, profile_id)
        if profile is None:
            raise NotFoundError("Company profile not found.")
        return profile

    def _reads(self, profiles: list[CompanyProfile]) -> list[CompanyProfileRead]:
        owned: dict[uuid.UUID, list[ProfileProperty]] = {p.id: [] for p in profiles}
        if profiles:
            for prop in self.session.scalars(
                select(Property)
                .where(Property.company_profile_id.in_(owned))
                .order_by(Property.name, Property.id)
            ):
                owned[prop.company_profile_id].append(  # type: ignore[index]
                    ProfileProperty(id=prop.id, name=prop.name, city=prop.city)
                )
        return [
            CompanyProfileRead(
                **{k: getattr(p, k) for k in (*FIELD_NAMES, "id", "created_at", "updated_at")},
                properties=owned[p.id],
            )
            for p in profiles
        ]

    def list(self) -> list[CompanyProfileRead]:
        return self._reads(
            list(
                self.session.scalars(
                    select(CompanyProfile).order_by(CompanyProfile.legal_name, CompanyProfile.id)
                )
            )
        )

    def get(self, profile_id: uuid.UUID) -> CompanyProfileRead:
        return self._reads([self._get(profile_id)])[0]

    def create(self, values: dict[str, Any]) -> CompanyProfileRead:
        profile = CompanyProfile(**values, created_by=self.actor.id, updated_by=self.actor.id)
        self.session.add(profile)
        flush(self.session)
        self._log("created", profile, f'Company profile "{profile.legal_name}" was created.')
        commit(self.session)
        return self.get(profile.id)

    def update(self, profile_id: uuid.UUID, values: dict[str, Any]) -> CompanyProfileRead:
        profile = self._get(profile_id)
        changed = [k for k, v in values.items() if getattr(profile, k) != v]
        for key in changed:
            setattr(profile, key, values[key])
        if changed:
            profile.updated_by = self.actor.id
            self._log(
                "updated",
                profile,
                f'Company profile "{profile.legal_name}" was updated: '
                f"{', '.join(FIELD_NAMES[k] for k in changed)}. Agreements already generated "
                "keep the values they were made with.",
            )
        commit(self.session)
        return self.get(profile.id)

    def set_properties(
        self, profile_id: uuid.UUID, property_ids: list[uuid.UUID]
    ) -> CompanyProfileRead:
        profile = self._get(profile_id)
        wanted = set(property_ids)
        found = {
            p.id: p
            for p in self.session.scalars(
                select(Property).where(Property.id.in_(wanted)).with_for_update()
            )
        }
        unknown = wanted - set(found)
        if unknown:
            raise BadRequestError(f"{len(unknown)} property id(s) do not exist.")
        current = list(
            self.session.scalars(
                select(Property).where(Property.company_profile_id == profile.id).with_for_update()
            )
        )
        removed = [p for p in current if p.id not in wanted]
        added = [p for p in found.values() if p.company_profile_id != profile.id]
        for prop in removed:
            prop.company_profile_id = None
        for prop in added:
            moved_from = prop.company_profile_id
            prop.company_profile_id = profile.id
            log_activity(
                self.session,
                self.actor,
                action="company_profile_set",
                entity_type="properties",
                entity_id=prop.id,
                property_id=prop.id,
                message=f'Property "{prop.name}" now belongs to company profile '
                f'"{profile.legal_name}"'
                + (" (moved from another profile)." if moved_from else "."),
            )
        for prop in removed:
            log_activity(
                self.session,
                self.actor,
                action="company_profile_unset",
                entity_type="properties",
                entity_id=prop.id,
                property_id=prop.id,
                message=f'Property "{prop.name}" no longer belongs to company profile '
                f'"{profile.legal_name}".',
            )
        if added or removed:
            self._log(
                "properties_changed",
                profile,
                f'Company profile "{profile.legal_name}": {len(added)} property(ies) added, '
                f"{len(removed)} removed.",
            )
        commit(self.session)
        return self.get(profile.id)
