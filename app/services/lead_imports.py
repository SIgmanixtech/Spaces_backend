"""Lead spreadsheet import (Phase 3B-1, BR-006, BR-008).

Preview reads and checks every row and saves nothing. Import reads the same file again
(the fingerprint must match the preview), checks every row again, and creates the valid
rows the admin did not skip in one transaction: every lead, its first note, its history
entry and the import record, or none of them. Leads get source EXCEL and start at Lead
Created. Possible duplicates (existing leads and tenants, and rows repeated in the file)
are warned about and imported unless the admin skips them; nothing is merged. The file is
not kept: only its name, fingerprint, the mapping and each row's outcome. Imports are
never undone.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.crm import LeadImportBatch, LeadNote
from app.models.enums import UserRole
from app.models.property import Property
from app.models.user import AppUser
from app.repositories.base import paginate_rows
from app.schemas.common import ListQuery
from app.schemas.crm import DuplicateMatch
from app.schemas.lead_import import (
    ImportBatchRead,
    ImportBatchSummary,
    ImportColumn,
    ImportField,
    ImportPreview,
    ImportResultRow,
    PreviewRow,
    PreviousImport,
)
from app.services.base import commit, flush, log_activity
from app.services.crm import CrmService
from app.services.lead_import_files import (
    FIELD_LABEL,
    FIELDS,
    Lookups,
    RowCheck,
    Sheet,
    auto_mapping,
    check_row,
    fingerprint,
    read_sheet,
    repeated_in_file,
    resolve_mapping,
    safe_cell,
)


@dataclass
class Analysis:
    file_name: str
    sheet: Sheet
    mapping: list[str | None]
    fingerprint: str
    checks: list[RowCheck]
    duplicates: list[list[DuplicateMatch]]
    repeated: dict[int, list[int]]

    def columns(self) -> list[ImportColumn]:
        recognised = auto_mapping(self.sheet.headers)
        return [
            ImportColumn(
                index=i, header=h, field=self.mapping[i], recognised=recognised[i] is not None
            )
            for i, h in enumerate(self.sheet.headers)
        ]


def _label(check: RowCheck) -> str | None:
    return (
        " · ".join(v for k in ("company_name", "contact_name") if (v := check.values.get(k)))
        or None
    )


class LeadImportService:
    def __init__(self, session: Session, actor: AppUser) -> None:
        self.session = session
        self.actor = actor
        self.crm = CrmService(session, actor)

    # --- reading and checking -----------------------------------------------------------

    def _lookups(self) -> Lookups:
        lookups = Lookups()
        for pid, name in self.session.execute(
            select(Property.id, Property.name).where(Property.archived_at.is_(None))
        ):
            lookups.properties.setdefault(name.strip().lower(), []).append(pid)
        for uid, email in self.session.execute(
            select(AppUser.id, AppUser.email).where(
                AppUser.role == UserRole.ADMIN, AppUser.is_active.is_(True)
            )
        ):
            if email:
                lookups.owners[email.strip().lower()] = uid
        return lookups

    def analyse(
        self, data: bytes, file_name: str | None, mapping: list[str | None] | None
    ) -> Analysis:
        sheet = read_sheet(data, file_name)
        chosen = resolve_mapping(sheet.headers, mapping)
        lookups = self._lookups()
        checks = [check_row(n, cells, sheet.headers, chosen, lookups) for n, cells in sheet.rows]
        duplicates = self.crm.match_many([c.keys for c in checks])
        return Analysis(
            file_name=(file_name or "import").strip()[-255:] or "import",
            sheet=sheet,
            mapping=chosen,
            fingerprint=fingerprint(data),
            checks=checks,
            duplicates=duplicates,
            repeated=repeated_in_file(checks),
        )

    def _previous(self, digest: str) -> list[PreviousImport]:
        rows = self.session.execute(
            select(LeadImportBatch, AppUser.email)
            .outerjoin(AppUser, AppUser.id == LeadImportBatch.created_by)
            .where(LeadImportBatch.file_sha256 == digest)
            .order_by(LeadImportBatch.created_at.desc())
            .limit(10)
        )
        return [
            PreviousImport(
                id=b.id,
                file_name=b.file_name,
                created_at=b.created_at,
                imported_count=b.imported_count,
                created_by_email=email,
            )
            for b, email in rows
        ]

    def preview(
        self, data: bytes, file_name: str | None, mapping: list[str | None] | None
    ) -> ImportPreview:
        a = self.analyse(data, file_name, mapping)
        rows = []
        for check, dupes in zip(a.checks, a.duplicates, strict=True):
            repeated = a.repeated.get(check.row, [])
            status = "ERROR" if check.errors else "DUPLICATE" if dupes or repeated else "READY"
            rows.append(
                PreviewRow(
                    row=check.row,
                    status=status,  # type: ignore[arg-type]
                    values=check.values,
                    errors=check.errors,  # type: ignore[arg-type]
                    duplicates=dupes,
                    repeated_in_file=repeated,
                )
            )
        columns = a.columns()
        return ImportPreview(
            file_name=a.file_name,
            file_type=a.sheet.file_type,
            fingerprint=a.fingerprint,
            columns=columns,
            fields=[ImportField(key=k, label=label) for k, label in FIELDS],
            unknown_columns=[
                c.header or f"Column {c.index + 1} (no header)" for c in columns if not c.recognised
            ],
            total_rows=len(rows),
            ready_count=sum(r.status == "READY" for r in rows),
            error_count=sum(r.status == "ERROR" for r in rows),
            duplicate_count=sum(r.status == "DUPLICATE" for r in rows),
            rows=rows,
            previous_imports=self._previous(a.fingerprint),
        )

    # --- importing ----------------------------------------------------------------------

    def run(
        self,
        data: bytes,
        file_name: str | None,
        mapping: list[str | None] | None,
        skip_rows: set[int],
        expected_fingerprint: str,
    ) -> ImportBatchRead:
        a = self.analyse(data, file_name, mapping)
        if a.fingerprint != expected_fingerprint:
            raise ConflictError(
                "This is not the file that was previewed. Preview it again before importing."
            )
        results: list[dict[str, Any]] = []
        chosen: list[tuple[RowCheck, dict[str, Any]]] = []
        for check, dupes in zip(a.checks, a.duplicates, strict=True):
            entry: dict[str, Any] = {
                "row": check.row,
                "label": _label(check),
                "errors": check.errors,
                "duplicates": [
                    {
                        "kind": d.kind,
                        "id": str(d.id),
                        "reference": d.reference,
                        "name": d.name,
                        "matched_on": list(d.matched_on),
                    }
                    for d in dupes
                ],
                "repeated_in_file": a.repeated.get(check.row, []),
            }
            if check.errors or check.lead is None:
                entry |= {"outcome": "ERROR", "values": check.values}
            elif check.row in skip_rows:
                entry |= {"outcome": "SKIPPED", "values": check.values}
            else:
                entry |= {"outcome": "IMPORTED"}
                chosen.append((check, entry))
            results.append(entry)

        counts = {
            "total_rows": len(results),
            "imported_count": len(chosen),
            "skipped_count": sum(r["outcome"] == "SKIPPED" for r in results),
            "error_count": sum(r["outcome"] == "ERROR" for r in results),
            "duplicate_count": sum(
                r["outcome"] != "ERROR" and bool(r["duplicates"] or r["repeated_in_file"])
                for r in results
            ),
        }
        batch = LeadImportBatch(
            id=uuid.uuid4(),
            file_name=a.file_name,
            file_sha256=a.fingerprint,
            file_type=a.sheet.file_type,
            mapping={str(c.index): {"header": c.header, "field": c.field} for c in a.columns()},
            results=[],
            created_by=self.actor.id,
            **counts,
        )
        try:
            self.session.add(batch)
            flush(self.session)
            for check, entry in chosen:
                lead = self.crm.insert(
                    dict(check.lead or {}),
                    import_batch_id=batch.id,
                    how=f" by import from {a.file_name} (row {check.row})",
                )
                if check.note:
                    self.session.add(
                        LeadNote(
                            lead_id=lead.id,
                            body=check.note,
                            created_by=self.actor.id,
                            updated_by=self.actor.id,
                        )
                    )
                    self.crm._log("note_added", lead, f"Lead {lead.reference}: a note was added.")
                entry["lead_id"] = str(lead.id)
                entry["lead_reference"] = lead.reference
            batch.results = results
            log_activity(
                self.session,
                self.actor,
                action="imported",
                entity_type="lead_imports",
                entity_id=batch.id,
                property_id=None,  # an import is not tied to one property
                message=(
                    f"{counts['imported_count']} lead(s) imported from {a.file_name}: "
                    f"{counts['skipped_count']} skipped, {counts['error_count']} with errors."
                )[:500],
            )
            commit(self.session)
        except Exception:
            # One transaction: nothing of this import is kept. Not retried automatically.
            self.session.rollback()
            raise
        return self.get(batch.id)

    # --- history ------------------------------------------------------------------------

    def _summary(self, batch: LeadImportBatch, user: AppUser | None) -> dict[str, Any]:
        return {
            "id": batch.id,
            "file_name": batch.file_name,
            "file_type": batch.file_type,
            "file_sha256": batch.file_sha256,
            "total_rows": batch.total_rows,
            "imported_count": batch.imported_count,
            "skipped_count": batch.skipped_count,
            "error_count": batch.error_count,
            "duplicate_count": batch.duplicate_count,
            "created_by_email": user.email if user else None,
            "created_by_name": user.full_name if user else None,
            "created_at": batch.created_at,
        }

    def batches(self, query: ListQuery) -> tuple[list[ImportBatchSummary], int]:
        statement = (
            select(LeadImportBatch, AppUser)
            .outerjoin(AppUser, AppUser.id == LeadImportBatch.created_by)
            .order_by(LeadImportBatch.created_at.desc(), LeadImportBatch.id)
        )
        if query.search:
            statement = statement.where(
                func.lower(LeadImportBatch.file_name).contains(query.search.strip().lower())
            )
        rows, total = paginate_rows(self.session, statement, query)
        return [ImportBatchSummary(**self._summary(b, u)) for b, u in rows], total

    def get(self, batch_id: uuid.UUID) -> ImportBatchRead:
        found = self.session.execute(
            select(LeadImportBatch, AppUser)
            .outerjoin(AppUser, AppUser.id == LeadImportBatch.created_by)
            .where(LeadImportBatch.id == batch_id)
        ).first()
        if found is None:
            raise NotFoundError("Import not found.")
        batch, user = found
        columns = [
            ImportColumn(
                index=int(i),
                header=c.get("header") or "",
                field=c.get("field"),
                recognised=c.get("field") is not None,
            )
            for i, c in sorted(batch.mapping.items(), key=lambda kv: int(kv[0]))
        ]
        return ImportBatchRead(
            **self._summary(batch, user),
            columns=columns,
            rows=[ImportResultRow.model_validate(r) for r in batch.results],
        )

    def report_csv(self, batch_id: uuid.UUID) -> tuple[str, bytes]:
        """Each row's outcome as CSV for Excel, with formula-like text neutralised."""
        batch = self.get(batch_id)
        fields = [c.field for c in batch.columns if c.field]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            ["Row", "Outcome", "Lead", "Problems", "Possible duplicates"]
            + [FIELD_LABEL[f] for f in fields]
        )
        for r in batch.rows:
            problems = "; ".join(f"{p.column}: {p.message}" for p in r.errors)
            warnings_ = [f"{d.kind.title()} {d.reference or d.name}" for d in r.duplicates]
            if r.repeated_in_file:
                warnings_.append("repeated in rows " + ", ".join(map(str, r.repeated_in_file)))
            writer.writerow(
                [
                    safe_cell(v)
                    for v in [
                        r.row,
                        r.outcome.title(),
                        r.lead_reference or "",
                        problems,
                        "; ".join(warnings_),
                    ]
                ]
                + [safe_cell(r.values.get(f, "")) for f in fields]
            )
        stem = batch.file_name.rsplit(".", 1)[0][:80] or "import"
        return f"{stem}-results.csv", buffer.getvalue().encode("utf-8-sig")
