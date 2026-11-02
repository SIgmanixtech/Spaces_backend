"""Reading and checking lead spreadsheets (Phase 3B-1). No database needed."""

import io
import uuid
import zipfile
from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from app.core.errors import BadRequestError, PayloadTooLargeError, UnsupportedMediaTypeError
from app.models.enums import LeadImportFileType, LeadSource
from app.services import lead_import_files as files
from app.services.lead_import_files import (
    FIELDS,
    Lookups,
    auto_mapping,
    check_row,
    fingerprint,
    read_sheet,
    repeated_in_file,
    resolve_mapping,
    safe_cell,
    template,
)

HEADERS = [label for _, label in FIELDS]


def xlsx(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def checked(rows: list[list[object]], lookups: Lookups | None = None, mapping=None):
    sheet = read_sheet(xlsx(rows), "leads.xlsx")
    chosen = resolve_mapping(sheet.headers, mapping)
    return [check_row(n, c, sheet.headers, chosen, lookups or Lookups()) for n, c in sheet.rows]


# --- files ------------------------------------------------------------------------------


def test_csv_and_xlsx_read_the_same_rows() -> None:
    from_csv = read_sheet(
        csv_bytes("﻿Company,Email\r\nAcme Ltd,ops@acme.in\r\n,\r\nBeta,\r\n"), "LEADS.CSV"
    )
    from_xlsx = read_sheet(
        xlsx([["Company", "Email"], ["Acme Ltd", "ops@acme.in"], [], ["Beta"]]), "l.xlsx"
    )
    assert from_csv.file_type == LeadImportFileType.CSV
    assert from_xlsx.file_type == LeadImportFileType.XLSX
    for sheet in (from_csv, from_xlsx):
        assert sheet.headers == ["Company", "Email"]
        # Blank rows are skipped; row numbers are the spreadsheet's own.
        assert [n for n, _ in sheet.rows] == [2, 4]
        assert sheet.rows[0][1][0].value == "Acme Ltd"


@pytest.mark.parametrize(
    ("name", "error", "text"),
    [
        ("leads.xls", UnsupportedMediaTypeError, ".xls files are not accepted"),
        ("leads.xlsm", UnsupportedMediaTypeError, "Macro-enabled"),
        ("leads.pdf", UnsupportedMediaTypeError, ".xlsx) or a CSV"),
        ("leads", UnsupportedMediaTypeError, ".xlsx) or a CSV"),
    ],
)
def test_only_xlsx_and_csv_are_accepted(name: str, error: type, text: str) -> None:
    with pytest.raises(
        error, match=text.replace("(", r"\(").replace(")", r"\)").replace(".", r"\.")
    ):
        read_sheet(b"Company\nAcme\n", name)


def test_malformed_files_are_refused() -> None:
    with pytest.raises(BadRequestError, match="not a valid .xlsx"):
        read_sheet(b"Company,Email\nAcme,a@b.in\n", "renamed.xlsx")  # a CSV named .xlsx
    with pytest.raises(BadRequestError, match="not a valid .xlsx"):
        read_sheet(b"PK\x03\x04 not really a zip", "broken.xlsx")
    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as archive:
        archive.writestr("readme.txt", "hello")
    with pytest.raises(BadRequestError, match="not a valid .xlsx"):
        read_sheet(plain_zip.getvalue(), "zip.xlsx")
    with pytest.raises(BadRequestError, match="not a text CSV"):
        read_sheet(b"Company\x00\x01\x02", "binary.csv")
    with pytest.raises(BadRequestError, match="not UTF-8"):
        read_sheet("Company\nCafé\n".encode("cp1252"), "ansi.csv")
    with pytest.raises(BadRequestError, match="empty"):
        read_sheet(b"", "empty.csv")
    with pytest.raises(BadRequestError, match="no data rows"):
        read_sheet(b"Company,Email\n", "header-only.csv")
    with pytest.raises(BadRequestError, match="without a header"):
        read_sheet(b"Company\nAcme,extra\n", "wide.csv")


def test_macros_inside_an_xlsx_are_refused() -> None:
    data = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(xlsx([["Company"], ["Acme"]]))) as source,
        zipfile.ZipFile(data, "w") as target,
    ):
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr("xl/vbaProject.bin", b"macro")
    with pytest.raises(UnsupportedMediaTypeError, match="contains macros"):
        read_sheet(data.getvalue(), "sneaky.xlsx")


def test_file_size_row_count_and_unpacked_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PayloadTooLargeError, match="5 MB"):
        read_sheet(b"a" * (files.MAX_FILE_BYTES + 1), "big.csv")
    exactly = "Company\n" + "".join(f"Lead {i}\n" for i in range(files.MAX_ROWS))
    assert len(read_sheet(csv_bytes(exactly), "max.csv").rows) == 2000
    with pytest.raises(BadRequestError, match="more than 2,000 data rows"):
        read_sheet(csv_bytes(exactly + "One more\n"), "over.csv")
    over = xlsx([["Company"]] + [[f"Lead {i}"] for i in range(files.MAX_ROWS + 1)])
    with pytest.raises(BadRequestError, match="more than 2,000 data rows"):
        read_sheet(over, "over.xlsx")
    # A small file that would unpack to more than the cap is refused before it is opened.
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (2 * 1024 * 1024))
    assert len(bomb.getvalue()) < 50_000
    monkeypatch.setattr(files, "MAX_UNPACKED_BYTES", 1024 * 1024)
    with pytest.raises(BadRequestError, match="unpacks to more than"):
        read_sheet(bomb.getvalue(), "bomb.xlsx")


def test_formulas_are_never_calculated() -> None:
    # openpyxl saves formulas without a calculated value, as some tools do.
    rows = checked([["Company", "People"], ["Acme", "=2+2"]])
    assert rows[0].lead is None
    assert rows[0].errors == [
        {
            "column": "People",
            "field": "capacity",
            "message": "This formula has no saved value. Open the file in Excel, let it "
            "calculate, save it, and upload it again.",
        }
    ]
    # A saved value is used as Excel showed it: rewrite the cell XML with a cached value.
    data = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(xlsx([["Company", "People"], ["Acme", "=2+2"]]))) as source,
        zipfile.ZipFile(data, "w") as target,
    ):
        for item in source.infolist():
            body = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                body = body.replace(b"<f>2+2</f><v></v>", b"<f>2+2</f><v>4</v>").replace(
                    b"<f>2+2</f><v />", b"<f>2+2</f><v>4</v>"
                )
                assert b"<v>4</v>" in body
            target.writestr(item, body)
    sheet = read_sheet(data.getvalue(), "cached.xlsx")
    row = check_row(2, sheet.rows[0][1], sheet.headers, auto_mapping(sheet.headers), Lookups())
    assert row.errors == [] and row.lead is not None and row.lead["capacity"] == 4


# --- mapping ----------------------------------------------------------------------------


def test_headers_match_ignoring_case_and_spacing_and_unknown_ones_are_left_out() -> None:
    headers = [
        "  COMPANY ",
        "contact   name",
        "e mail",
        "Email",
        "Email",
        "Budget(₹)",
        "Mystery",
        "",
    ]
    assert auto_mapping(headers) == [
        "company_name",
        "contact_name",
        "contact_email",  # spacing is ignored: "e mail" is "Email"
        None,  # a field already taken by an earlier column is not mapped twice
        None,
        "budget_amount",
        None,  # unknown header: reported, never guessed
        None,
    ]
    assert auto_mapping(HEADERS) == [key for key, _ in FIELDS]


def test_the_admin_can_correct_the_mapping() -> None:
    headers = ["Firm", "Mail"]
    assert resolve_mapping(headers, None) == [None, None]
    assert resolve_mapping(headers, ["company_name", "contact_email"]) == [
        "company_name",
        "contact_email",
    ]
    with pytest.raises(BadRequestError, match="Map each field once"):
        resolve_mapping(headers, ["company_name", "company_name"])
    with pytest.raises(BadRequestError, match="Unknown import field"):
        resolve_mapping(headers, ["company_name", "source"])
    with pytest.raises(BadRequestError, match="file has 2"):
        resolve_mapping(headers, ["company_name"])
    rows = checked(
        [["Firm", "Mail"], ["Acme", "ops@acme.in"]], mapping=["company_name", "contact_email"]
    )
    assert rows[0].lead is not None
    assert (rows[0].lead["company_name"], rows[0].lead["contact_email"]) == ("Acme", "ops@acme.in")


# --- values -----------------------------------------------------------------------------


def test_dates_as_day_month_year_iso_or_excel_dates() -> None:
    rows = checked(
        [
            ["Company", "Desired start"],
            ["A", "15/01/2027"],
            ["B", "5/3/2027"],
            ["C", "2027-01-15"],
            ["D", datetime(2027, 1, 15)],
            ["E", "31/02/2027"],
            ["F", "01-15-2027"],
            ["G", 46000],
        ]
    )
    assert [r.lead["desired_start_date"] if r.lead else None for r in rows] == [
        date(2027, 1, 15),
        date(2027, 3, 5),
        date(2027, 1, 15),
        date(2027, 1, 15),
        None,
        None,
        None,
    ]
    assert rows[4].errors[0]["message"] == "Not a real date."
    assert rows[5].errors[0]["message"] == "Use a date as DD/MM/YYYY or YYYY-MM-DD."
    assert rows[6].errors[0]["message"] == "Use a date as DD/MM/YYYY or YYYY-MM-DD."


def test_budget_is_normalised_and_needs_its_period() -> None:
    rows = checked(
        [
            ["Company", "Budget (₹)", "Budget per"],
            ["A", "₹50,000", "Monthly"],
            ["B", " 1,20,000.50 ", "half-day"],
            ["C", 75000, "MONTHLY"],
            ["D", "$500", "Monthly"],
            ["E", "50000", ""],
            ["F", "-5", "Monthly"],
            ["G", "50000", "Fortnightly"],
        ]
    )
    amounts = [(r.lead["budget_amount"], r.lead["budget_period"]) if r.lead else None for r in rows]
    assert amounts[:3] == [
        (Decimal("50000"), "MONTHLY"),
        (Decimal("120000.50"), "HALF_DAY"),
        (Decimal("75000"), "MONTHLY"),
    ]
    assert amounts[3:] == [None, None, None, None]
    assert "Enter an amount" in rows[3].errors[0]["message"]  # other currencies are not rupees
    assert rows[4].errors == [
        {
            "column": "Budget (₹) / Budget per",
            "field": "budget_amount",
            "message": "Give the budget amount together with its billing period.",
        }
    ]
    assert "Enter an amount" in rows[5].errors[0]["message"]
    assert rows[6].errors[0]["message"].startswith("Use one of: Hourly, Half-day")


def test_space_types_by_code_or_label_and_numbers() -> None:
    rows = checked(
        [
            ["Company", "Space type", "People", "Area (sq ft)", "Phone"],
            ["A", "Private office", 4, 1200.5, 9876543210],
            ["B", "PRIVATE_OFFICE", "4", "1200", "+91 98765 43210"],
            ["C", "coworking space", 4.0, "1,200", None],
            ["D", "Penthouse", 2.5, "-3", None],
            ["E", "Desk", 0, None, None],
        ]
    )
    assert rows[0].lead is not None and rows[0].lead["space_type"] == "PRIVATE_OFFICE"
    assert rows[0].lead["contact_phone"] == "9876543210"  # a number cell, not 9876543210.0
    assert rows[1].lead is not None and rows[1].lead["capacity"] == 4
    assert rows[2].errors == [
        {"column": "Area (sq ft)", "field": "area_sqft", "message": "Enter a number."}
    ]
    assert {e["field"] for e in rows[3].errors} == {"space_type", "capacity", "area_sqft"}
    assert rows[4].errors[0]["field"] == "capacity"  # 0 people: the lead schema's rule


def test_property_and_owner_are_matched_exactly() -> None:
    hub, twin1, twin2, admin = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lookups = Lookups(
        properties={"sigma hub": [hub], "twin tower": [twin1, twin2]},
        owners={"asha@sigmanix.in": admin},
    )
    rows = checked(
        [
            ["Company", "Preferred property", "Owner email"],
            ["A", "SIGMA HUB", "Asha@Sigmanix.in"],
            ["B", "Sigma", ""],
            ["C", "Twin Tower", "nobody@sigmanix.in"],
            ["D", "", ""],
        ],
        lookups,
    )
    assert rows[0].lead is not None
    assert (rows[0].lead["property_id"], rows[0].lead["owner_id"]) == (hub, admin)
    assert rows[1].errors == [
        {
            "column": "Preferred property",
            "field": "property_id",
            "message": "No live property has this name.",
        }
    ]
    assert [e["message"] for e in rows[2].errors] == [
        "More than one live property has this name.",
        "No active administrator has this email.",
    ]
    assert rows[3].lead is not None and rows[3].lead["owner_id"] is None  # blank: unassigned


def test_rows_follow_the_lead_rules_and_report_every_problem() -> None:
    rows = checked(
        [
            ["Company", "Contact name", "Email", "People", "Notes"],
            ["", "", "ops@acme.in", "", ""],
            ["Acme", "Priya", "not-an-email", "many", "x" * 5001],
            ["Acme", "Priya", "ops@acme.in", "4", "Call after 3"],
        ]
    )
    assert rows[0].errors == [
        {
            "column": "Company / Contact name",
            "field": "company_name",
            "message": "Give a company name or a contact name.",
        }
    ]
    assert {e["field"] for e in rows[1].errors} == {"contact_email", "capacity", "note"}
    good = rows[2]
    assert good.errors == [] and good.note == "Call after 3"
    assert good.lead is not None
    assert (good.lead["source"], good.lead["currency"]) == (LeadSource.EXCEL, "INR")
    assert "stage" not in good.lead  # the stage is the model's default: Lead Created


def test_rows_repeated_within_the_file() -> None:
    rows = checked(
        [
            ["Company", "Email", "Phone"],
            ["Acme Ltd", "ops@acme.in", ""],
            ["ACME  Ltd.", "", ""],
            ["Beta", "OPS@ACME.IN", ""],
            ["Gamma", "", "098765 43210"],
            ["Delta", "", "+91 98765-43210"],
            ["Acme Holdings", "", ""],  # a similar name alone is not a match
        ]
    )
    assert repeated_in_file(rows) == {2: [3, 4], 3: [2], 4: [2], 5: [6], 6: [5]}


# --- reports, fingerprint, template -----------------------------------------------------


def test_report_cells_never_start_a_formula() -> None:
    assert [
        safe_cell(v) for v in ("=HYPERLINK(1)", "+91 9", "-5", "@SUM", "\tx", "Acme", None, 7)
    ] == [
        "'=HYPERLINK(1)",
        "'+91 9",
        "'-5",
        "'@SUM",
        "'\tx",
        "Acme",
        "",
        "7",
    ]


def test_fingerprint_is_the_files_sha256() -> None:
    assert fingerprint(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert fingerprint(b"abc") != fingerprint(b"abd")


def test_the_blank_template_has_every_column() -> None:
    book = load_workbook(io.BytesIO(template(LeadImportFileType.XLSX)), read_only=True)
    assert [c.value for c in next(book.worksheets[0].iter_rows())] == HEADERS
    assert template(LeadImportFileType.CSV).decode("utf-8-sig").strip() == ",".join(HEADERS)
    assert auto_mapping(HEADERS) == [k for k, _ in FIELDS]
