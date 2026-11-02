"""Reading lead spreadsheets for the CRM import (Phase 3B-1). No database here.

Only .xlsx and .csv are accepted. An .xlsx is a zip archive: its size, entry count and
unpacked size are checked before it is opened, macro-enabled workbooks are refused, and
formulas are never calculated: a formula cell is read from the value Excel saved with it,
and one without a saved value is reported. Rows become lead fields through the column
mapping and are checked by the same LeadCreate schema as leads entered by hand.

Nothing read from a file is logged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import uuid
import warnings
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.core.errors import BadRequestError, PayloadTooLargeError, UnsupportedMediaTypeError
from app.models.enums import BillingPeriod, LeadImportFileType, LeadSource, SpaceType
from app.schemas.crm import LeadCreate, NoteCreate
from app.services.crm import company_key, email_key, phone_key

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
MAX_COLUMNS = 50
# An .xlsx unpacks to many times its size; refuse archives that would unpack to more.
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 500

# The import template: (field key, column header), in template order.
FIELDS: list[tuple[str, str]] = [
    ("company_name", "Company"),
    ("contact_name", "Contact name"),
    ("contact_email", "Email"),
    ("contact_phone", "Phone"),
    ("property_id", "Preferred property"),
    ("space_type", "Space type"),
    ("capacity", "People"),
    ("area_sqft", "Area (sq ft)"),
    ("budget_amount", "Budget (₹)"),
    ("budget_period", "Budget per"),
    ("desired_start_date", "Desired start"),
    ("owner_id", "Owner email"),
    ("note", "Notes"),
]
FIELD_KEYS = [key for key, _ in FIELDS]
FIELD_LABEL = dict(FIELDS)

# The admin app's labels for these codes (features/property/options.ts, setup/pricing.ts).
SPACE_TYPE_LABEL = {
    SpaceType.DESK: "Desk",
    SpaceType.PRIVATE_OFFICE: "Private office",
    SpaceType.CABIN: "Cabin",
    SpaceType.MEETING_ROOM: "Meeting room",
    SpaceType.COWORKING: "Coworking space",
    SpaceType.FLOOR: "Whole floor",
    SpaceType.PORTION: "Portion",
    SpaceType.CONFERENCE_ROOM: "Conference room",
    SpaceType.TRAINING_ROOM: "Training room",
    SpaceType.DEDICATED_WORKSTATION: "Dedicated workstation",
    SpaceType.FLEXIBLE_WORKSTATION: "Flexible workstation",
    SpaceType.SEAT: "Seat",
    SpaceType.OTHER: "Other rentable space",
    SpaceType.NON_RENTABLE: "Non-rentable",
}
PERIOD_LABEL = {
    BillingPeriod.HOURLY: "Hourly",
    BillingPeriod.HALF_DAY: "Half-day",
    BillingPeriod.FULL_DAY: "Full-day",
    BillingPeriod.DAILY: "Daily",
    BillingPeriod.WEEKLY: "Weekly",
    BillingPeriod.MONTHLY: "Monthly",
    BillingPeriod.CUSTOM: "Custom",
}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_WHOLE = re.compile(r"^\d+$")
_NUMBER = re.compile(r"^\d+(\.\d+)?$")


def norm_header(text: object) -> str:
    """Headers match ignoring case and spacing: "contact  NAME" is "Contact name"."""
    return "".join(str(text or "").split()).lower()


_HEADER_FIELD = {norm_header(label): key for key, label in FIELDS}


def _choice_key(text: str) -> str:
    return " ".join(text.replace("_", " ").replace("-", " ").lower().split())


def _choices(labels: dict[Any, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for code, label in labels.items():
        out[_choice_key(code.value)] = code.value
        out[_choice_key(label)] = code.value
    return out


_SPACE_TYPES = _choices(SPACE_TYPE_LABEL)
_PERIODS = _choices(PERIOD_LABEL)


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_cell(value: object) -> str:
    """Text for a downloadable report that a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# --- reading -----------------------------------------------------------------------------


@dataclass
class Cell:
    value: object = None
    # A formula whose value Excel did not save: the import never calculates formulas.
    formula_without_value: bool = False
    # Excel's own error value, such as #DIV/0!.
    error: str | None = None

    @property
    def blank(self) -> bool:
        return (
            not self.formula_without_value
            and self.error is None
            and (self.value is None or (isinstance(self.value, str) and not self.value.strip()))
        )


@dataclass
class Sheet:
    file_type: LeadImportFileType
    headers: list[str]
    # (row number as the spreadsheet shows it, cells) for every row that is not blank.
    rows: list[tuple[int, list[Cell]]]


def file_type_of(file_name: str | None) -> LeadImportFileType:
    ext = os.path.splitext((file_name or "").strip().lower())[1]
    if ext == ".xls":
        raise UnsupportedMediaTypeError(
            "Old Excel .xls files are not accepted. Save it as an Excel Workbook (.xlsx) or as "
            "CSV UTF-8 and upload that."
        )
    if ext == ".xlsm":
        raise UnsupportedMediaTypeError(
            "Macro-enabled workbooks (.xlsm) are not accepted. Save it as an Excel Workbook "
            "(.xlsx) and upload that."
        )
    if ext == ".xlsx":
        return LeadImportFileType.XLSX
    if ext == ".csv":
        return LeadImportFileType.CSV
    raise UnsupportedMediaTypeError("Upload an Excel Workbook (.xlsx) or a CSV file (.csv).")


def read_sheet(data: bytes, file_name: str | None) -> Sheet:
    """The header row and the non-blank rows of the file's first sheet."""
    file_type = file_type_of(file_name)
    if len(data) > MAX_FILE_BYTES:
        raise PayloadTooLargeError("The file is larger than 5 MB.")
    if not data:
        raise BadRequestError("The file is empty.")
    raw = _read_xlsx(data) if file_type == LeadImportFileType.XLSX else _read_csv(data)
    header_at = next(
        (i for i, (_, cells) in enumerate(raw) if not all(c.blank for c in cells)), None
    )
    if header_at is None:
        raise BadRequestError("The file has no header row: the first row must name the columns.")
    _, header_cells = raw[header_at]
    if any(c.formula_without_value or c.error for c in header_cells):
        raise BadRequestError("The header row must be plain text, not formulas.")
    headers = [_text(c.value) or "" for c in header_cells]
    while headers and not headers[-1]:
        headers.pop()
    if len(headers) > MAX_COLUMNS:
        raise BadRequestError(f"The file has more than {MAX_COLUMNS} columns.")
    rows = []
    for number, cells in raw[header_at + 1 :]:
        cells = (cells + [Cell()] * len(headers))[: max(len(headers), len(cells))]
        if any(not c.blank for c in cells[len(headers) :]):
            raise BadRequestError(
                f"Row {number} has values in columns without a header. Name every column in "
                "the header row."
            )
        cells = cells[: len(headers)]
        if all(c.blank for c in cells):
            continue
        rows.append((number, cells))
    if not rows:
        raise BadRequestError("The file has a header row but no data rows.")
    return Sheet(file_type=file_type, headers=headers, rows=rows)


def _too_many_rows() -> BadRequestError:
    return BadRequestError(
        f"The file has more than {MAX_ROWS:,} data rows. Split it and import each part."
    )


def _read_csv(data: bytes) -> list[tuple[int, list[Cell]]]:
    if b"\x00" in data:
        raise BadRequestError("This is not a text CSV file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BadRequestError(
            "The CSV file is not UTF-8 text. In Excel, use Save As and choose "
            "CSV UTF-8 (Comma delimited)."
        ) from exc
    out: list[tuple[int, list[Cell]]] = []
    non_blank = 0
    try:
        for number, record in enumerate(csv.reader(io.StringIO(text, newline="")), start=1):
            cells = [Cell(value=v) for v in record]
            if not all(c.blank for c in cells):
                non_blank += 1
                if non_blank > MAX_ROWS + 1:  # the header row plus the data rows
                    raise _too_many_rows()
            out.append((number, cells))
    except csv.Error as exc:
        raise BadRequestError(
            "The CSV file could not be read: a quoted value is not closed."
        ) from exc
    return out


def _check_archive(data: bytes) -> None:
    if not data.startswith(b"PK\x03\x04"):
        raise BadRequestError("This is not a valid .xlsx file.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise BadRequestError("This .xlsx file has too many parts to be a spreadsheet.")
            if sum(e.file_size for e in entries) > MAX_UNPACKED_BYTES:
                raise BadRequestError(
                    "This .xlsx file unpacks to more than 50 MB. Remove unused sheets, images "
                    "or formatting, or save the rows as CSV UTF-8."
                )
            names = {e.filename for e in entries}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise BadRequestError("This is not a valid .xlsx file.")
            if any(n.lower().endswith("vbaproject.bin") for n in names) or (
                b"macroEnabled" in archive.read("[Content_Types].xml")
            ):
                raise UnsupportedMediaTypeError(
                    "This workbook contains macros. Save it as an Excel Workbook (.xlsx) "
                    "without macros and upload that."
                )
    except zipfile.BadZipFile as exc:
        raise BadRequestError("This is not a valid .xlsx file.") from exc


def _read_xlsx(data: bytes) -> list[tuple[int, list[Cell]]]:
    _check_archive(data)
    from openpyxl import load_workbook  # only needed for .xlsx

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # Values as Excel saved them, and the same sheet again to see which are formulas.
            saved = load_workbook(
                io.BytesIO(data), read_only=True, data_only=True, keep_links=False
            )
            formulas = load_workbook(
                io.BytesIO(data), read_only=True, data_only=False, keep_links=False
            )
            try:
                if not saved.worksheets:
                    raise BadRequestError("The workbook has no sheets.")
                out: list[tuple[int, list[Cell]]] = []
                non_blank = 0
                for number, (values, kinds) in enumerate(
                    zip(
                        saved.worksheets[0].iter_rows(),
                        formulas.worksheets[0].iter_rows(),
                        strict=False,
                    ),
                    start=1,
                ):
                    cells = []
                    for v, k in zip(values, kinds, strict=False):
                        number = getattr(v, "row", None) or number
                        kind = getattr(k, "data_type", None)
                        value = getattr(v, "value", None)
                        if getattr(v, "data_type", None) == "e":
                            cells.append(Cell(error=str(value)))
                        elif kind == "f" and value is None:
                            cells.append(Cell(formula_without_value=True))
                        else:
                            cells.append(Cell(value=value))
                        if len(cells) > MAX_COLUMNS * 4:
                            break
                    if not all(c.blank for c in cells):
                        non_blank += 1
                        if non_blank > MAX_ROWS + 1:
                            raise _too_many_rows()
                    out.append((number, cells))
                return out
            finally:
                saved.close()
                formulas.close()
    except (BadRequestError, UnsupportedMediaTypeError):
        raise
    except Exception as exc:  # untrusted input: any parser failure means "cannot read"
        raise BadRequestError(
            "The workbook could not be read. Open it in Excel, save it as an Excel Workbook "
            "(.xlsx) and upload it again."
        ) from exc


# --- mapping -----------------------------------------------------------------------------


def auto_mapping(headers: list[str]) -> list[str | None]:
    """Each column's field from its header; a field already taken by an earlier column, or
    an unknown header, stays unmapped (and is reported)."""
    taken: set[str] = set()
    out: list[str | None] = []
    for header in headers:
        key = _HEADER_FIELD.get(norm_header(header))
        if key in taken:
            key = None
        if key:
            taken.add(key)
        out.append(key)
    return out


def resolve_mapping(headers: list[str], chosen: list[str | None] | None) -> list[str | None]:
    """The admin's mapping (one field or null per column), or the automatic one."""
    if chosen is None:
        return auto_mapping(headers)
    if len(chosen) != len(headers):
        raise BadRequestError(
            f"The mapping names {len(chosen)} column(s) but the file has {len(headers)}. "
            "Preview the file again."
        )
    seen: set[str] = set()
    for key in chosen:
        if key is None:
            continue
        if key not in FIELD_LABEL:
            raise BadRequestError(f"Unknown import field: {key}.")
        if key in seen:
            raise BadRequestError(
                f"Two columns are mapped to {FIELD_LABEL[key]}. Map each field once."
            )
        seen.add(key)
    return list(chosen)


# --- rows --------------------------------------------------------------------------------


@dataclass
class Lookups:
    """Live (not archived) properties by lower-case name, and active admins by email."""

    properties: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    owners: dict[str, uuid.UUID] = field(default_factory=dict)


@dataclass
class RowCheck:
    row: int
    values: dict[str, str]  # field -> the text as given, for display
    errors: list[dict[str, Any]]
    lead: dict[str, Any] | None  # validated LeadCreate values, when the row has no errors
    note: str | None
    keys: dict[str, str | None]  # normalised email/phone/company, for duplicate matching


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return (
            value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


class _Bad(ValueError):
    pass


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if m := _ISO_DATE.match(text):
                return date(int(m[1]), int(m[2]), int(m[3]))
            if m := _DMY_DATE.match(text):
                return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError as exc:
            raise _Bad("Not a real date.") from exc
    raise _Bad("Use a date as DD/MM/YYYY or YYYY-MM-DD.")


def _whole(value: object) -> int:
    if isinstance(value, bool):
        raise _Bad("Enter a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _WHOLE.match(value.strip()):
        return int(value.strip())
    raise _Bad("Enter a whole number.")


def _decimal(value: object, *, money: bool) -> Decimal:
    if isinstance(value, bool):
        raise _Bad("Enter a number.")
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    text = str(value).strip()
    if money:
        # Approved normalisation: drop the rupee sign, thousands separators and spaces.
        text = "".join(text.replace("₹", "").replace(",", "").split())
    if _NUMBER.match(text):
        try:
            return Decimal(text)
        except InvalidOperation:
            pass
    raise _Bad("Enter an amount such as 50000, 50,000 or ₹50,000." if money else "Enter a number.")


def _choice(value: object, choices: dict[str, str], labels: dict[Any, str]) -> str:
    found = choices.get(_choice_key(str(value)))
    if found is None:
        raise _Bad("Use one of: " + ", ".join(labels.values()) + ".")
    return found


def check_row(
    number: int,
    cells: list[Cell],
    headers: list[str],
    mapping: list[str | None],
    lookups: Lookups,
    source: LeadSource = LeadSource.EXCEL,
) -> RowCheck:
    """Turn one row into lead values, checked like a lead entered by hand. Also used for
    Meta Ads leads (source META_ADS), whose answers arrive as one row."""
    errors: list[dict[str, Any]] = []
    values: dict[str, str] = {}
    data: dict[str, Any] = {"source": source}
    note: str | None = None
    column_of: dict[str, str] = {}

    def problem(key: str | None, message: str, column: str | None = None) -> None:
        errors.append(
            {
                "column": column or (column_of.get(key, FIELD_LABEL.get(key, "")) if key else ""),
                "field": key,
                "message": message,
            }
        )

    for index, key in enumerate(mapping):
        if key is None:
            continue
        cell = cells[index] if index < len(cells) else Cell()
        column_of[key] = headers[index] or FIELD_LABEL[key]
        if cell.formula_without_value:
            values[key] = "(formula)"
            problem(
                key,
                "This formula has no saved value. Open the file in Excel, let it calculate, "
                "save it, and upload it again.",
            )
            continue
        if cell.error is not None:
            values[key] = cell.error
            problem(key, f"This cell shows an error ({cell.error}).")
            continue
        shown = _text(cell.value)
        if shown is None:
            continue  # blank: not provided
        values[key] = shown
        try:
            if key == "property_id":
                found = lookups.properties.get(shown.lower(), [])
                if not found:
                    raise _Bad("No live property has this name.")
                if len(found) > 1:
                    raise _Bad("More than one live property has this name.")
                data[key] = found[0]
            elif key == "owner_id":
                owner = lookups.owners.get(shown.lower())
                if owner is None:
                    raise _Bad("No active administrator has this email.")
                data[key] = owner
            elif key == "space_type":
                data[key] = _choice(shown, _SPACE_TYPES, SPACE_TYPE_LABEL)
            elif key == "budget_period":
                data[key] = _choice(shown, _PERIODS, PERIOD_LABEL)
            elif key == "capacity":
                data[key] = _whole(cell.value)
            elif key == "area_sqft":
                data[key] = _decimal(cell.value, money=False)
            elif key == "budget_amount":
                data[key] = _decimal(cell.value, money=True)
            elif key == "desired_start_date":
                data[key] = _date(cell.value)
            elif key == "note":
                note = NoteCreate(body=shown).body
            else:  # names, email and phone: text as given
                data[key] = shown
        except _Bad as exc:
            problem(key, str(exc))
        except ValidationError as exc:
            problem(key, _message(exc.errors()[0]))

    keys = {
        "email": email_key(values.get("contact_email")),
        "phone": phone_key(values.get("contact_phone")),
        "company": company_key(values.get("company_name")),
    }
    lead: dict[str, Any] | None = None
    converted_ok = not errors
    try:
        validated = LeadCreate.model_validate(data).model_dump()
        lead = validated if converted_ok else None
    except ValidationError as exc:
        for err in exc.errors():
            loc = err.get("loc") or ()
            key = str(loc[0]) if loc and str(loc[0]) in FIELD_LABEL else None
            message = _message(err)
            if key is None and not converted_ok:
                continue  # a whole-row rule about a cell already reported above
            if key is None and "company name" in message:
                names = [k for k in ("company_name", "contact_name") if k in column_of]
                column = " / ".join(column_of[k] for k in names) or "Company / Contact name"
                problem("company_name", message, column)
            elif key is None and "budget" in message:
                column = " / ".join(
                    column_of.get(k, FIELD_LABEL[k]) for k in ("budget_amount", "budget_period")
                )
                problem("budget_amount", message, column)
            else:
                problem(key, message)
    return RowCheck(row=number, values=values, errors=errors, lead=lead, note=note, keys=keys)


def _message(err: Any) -> str:
    message = str(err.get("msg", "Invalid value."))
    return message.removeprefix("Value error, ")


def repeated_in_file(checks: list[RowCheck]) -> dict[int, list[int]]:
    """For each row, the other rows of the same file sharing its email, phone or company."""
    by_key: dict[tuple[str, str], list[int]] = {}
    for check in checks:
        for kind, value in check.keys.items():
            if value:
                by_key.setdefault((kind, value), []).append(check.row)
    out: dict[int, set[int]] = {}
    for rows in by_key.values():
        if len(rows) > 1:
            for row in rows:
                out.setdefault(row, set()).update(r for r in rows if r != row)
    return {row: sorted(others) for row, others in out.items()}


# --- template ----------------------------------------------------------------------------


def template(file_type: LeadImportFileType) -> bytes:
    headers = [label for _, label in FIELDS]
    if file_type == LeadImportFileType.CSV:
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\r\n").writerow(headers)
        return buffer.getvalue().encode("utf-8-sig")
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    sheet = book.create_sheet("Leads")
    sheet.append(headers)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
