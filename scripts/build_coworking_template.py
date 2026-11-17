"""Build the placeholder Coworking License Agreement template from the approved agreement.

    python scripts/build_coworking_template.py <approved.docx> <template.docx>

Owner decisions of 17 Nov 2026. The approved agreement (Nanda Kishore, v1.2) is read and
never written; a separate copy is produced in which only member- and deal-specific text is
replaced by placeholders. Clauses, headings, tables, page layout, the letterhead and the
signature blocks are kept. Text is replaced across Word runs, keeping the formatting of
the first run of each match (bold names, dates and amounts stay bold).

Replaced:
- the member definition (name, without "S/O"; PANs, company and CIN when held; address);
- the licensor's name and address in the definitions; the premises address;
- the start date and the fee due day; the deposit, its words, lock-in and notice periods;
- the old payment transactions (removed) -> the licensor's bank and UPI details;
- the GST rate in the payment terms; the membership table (one row per line, totals);
- the signatories, company and dates of both signature blocks.
Every replacement must match exactly once, or nothing is written.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import docx
from docx.text.paragraph import Paragraph


def replace(paragraph: Paragraph, old: str, new: str, *, first_of: int | None = None) -> None:
    """Replace `old` (which may span runs) by `new`, in the first run of the match. `old`
    must occur exactly once, or exactly `first_of` times (then the first is replaced)."""
    runs = paragraph.runs
    text = "".join(r.text for r in runs)
    if text.count(old) != (first_of or 1):
        raise SystemExit(f"Expected {old!r} {first_of or 1} time(s) in {text!r}")
    start = text.index(old)
    end = start + len(old)
    pos = 0
    first = True
    for run in runs:
        r_start, r_end = pos, pos + len(run.text)
        pos = r_end
        if r_end <= start or r_start >= end:
            continue
        keep_head = run.text[: max(0, start - r_start)]
        keep_tail = run.text[max(0, end - r_start) :] if r_end > end else ""
        if first:
            run.text = keep_head + new + keep_tail
            first = False
        else:
            run.text = keep_tail


def set_cell(cell, value: str) -> None:  # noqa: ANN001
    paragraph = cell.paragraphs[0]
    runs = paragraph.runs
    if runs:
        runs[0].text = value
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(value)
    for extra in cell.paragraphs[1:]:
        for run in extra.runs:
            run.text = ""


def find(document: docx.Document, starts: str) -> Paragraph:
    found = [p for p in document.paragraphs if p.text.strip().startswith(starts)]
    if len(found) != 1:
        raise SystemExit(f"Expected one paragraph starting {starts!r}, found {len(found)}")
    return found[0]


def build(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        raise SystemExit("Refusing to overwrite the approved agreement.")
    d = docx.Document(str(source))

    # Definitions.
    p = find(d, "'You' means")
    replace(p, "K NandaKishore", "{{ member_name }}")
    replace(
        p,
        "' S/O 'Ramulu, 13-176, VidyaNagar Colony, Kothakota, Mahabubnagar, "
        "Andhra Pradesh - 509381’.",
        "'{% if member_pan %} (PAN {{ member_pan }}){% endif %}"
        "{% if company_name %}, representing '{{ company_name }}'"
        "{% if company_cin %} (CIN {{ company_cin }}{% if company_pan %}, PAN {{ company_pan }}"
        "{% endif %}){% elif company_pan %} (PAN {{ company_pan }}){% endif %}{% endif %}"
        ", {{ registered_address }}.",
    )
    sigma = (
        "5B - Sigma Hub Building, Plot No.8A, Survey No.184P, Sai Prithvi Enclave, "
        "Masidbanda, Kondapur, Hyderabad 500084"
    )
    p = find(d, "'Us' means")
    replace(
        p,
        "Sigmanix Ventures Private Limited located at " + sigma,
        "{{ provider_company_name }} located at {{ provider_address }}",
    )
    p = find(d, "'Sigmanix Spaces' means the building")
    replace(p, sigma, "{{ property_name }}, {{ property_address }}")

    # Exclusive terms: dates, deposit, payments.
    p = find(d, "The membership is commencing")
    replace(p, "29th May 2026", "{{ agreement_start_date }}")
    replace(p, "5th day", "{{ payment_due_date }} day")
    p = find(d, "There is a 2 (Two) months security deposit")
    # Piece by piece, so each value keeps its own formatting (amounts and periods bold).
    replace(p, "a 2 (Two) months security", "a security")
    replace(
        p,
        "Rs. 40,000 (Rupees Forty Thousand Only)",
        "Rs. {{ security_deposit }} ({{ security_deposit_words }})",
    )
    replace(p, "2 (Two) months", "{{ lock_in_period }}", first_of=2)
    replace(p, "2 (Two) months", "{{ notice_period }}")
    replace(p, " The details of the transaction are as below:", "")
    # The old payment transactions are removed (decision 4); the licensor's account follows.
    p = find(d, "INR 20,000.00 is credited")
    replace(
        p,
        p.text.strip(),
        "Payments are to be made to {{ bank_account_name }}, {{ bank_name }}, Account No. "
        "{{ bank_account_number }}, IFSC {{ bank_ifsc }}, {{ bank_branch }} branch.",
    )
    p = find(d, "An amount of INR 30,000.00")
    replace(p, p.text.strip(), "UPI ID: {{ upi_id }}")

    # Terms of payment: the GST rate comes from the pricing rule.
    p = find(d, "Membership Plan Prices does not include GST")
    replace(p, "additional 18%", "additional {{ gst_percentage }}%")

    # The membership table: header rates, one row per line, totals.
    table = d.tables[0]
    header = table.rows[0].cells
    set_cell(header[4], "GST {{ gst_percentage }}%")
    set_cell(header[6], "TDS Hold {{ tds_percentage }}%")
    body = table.rows[1]
    for cell, value in zip(
        body.cells,
        [
            "{{ item.number }}",
            "{{ item.membership }}",
            "{{ item.quantity }}",
            "₹ {{ item.nt_amount }}",
            "₹ {{ item.gst_amount }}",
            "₹ {{ item.invoice_amount }}",
            "₹ {{ item.tds_amount }}",
            "₹ {{ item.net_amount }}",
        ],
        strict=True,
    ):
        set_cell(cell, value)
    # Loop markers around the row (docxtpl removes these rows when filling).
    for before in (True, False):  # the markers' text is set below
        marker = copy.deepcopy(body._tr)
        if before:
            body._tr.addprevious(marker)
        else:
            body._tr.addnext(marker)
    rows = table.rows
    set_cell(rows[1].cells[0], "{%tr for item in membership_items %}")
    set_cell(rows[3].cells[0], "{%tr endfor %}")
    for marker in (rows[1], rows[3]):
        for cell in marker.cells[1:]:
            set_cell(cell, "")
    # The old second line (Virtual Address) goes: extra services are rows of the loop.
    old = table.rows[4]
    old._tr.getparent().remove(old._tr)
    totals = table.rows[4].cells
    for cell, value in zip(
        totals,
        [
            "",
            "Total",
            "",
            "₹ {{ subtotal_amount }}",
            "₹ {{ gst_amount }}",
            "₹ {{ invoice_amount }}",
            "₹ {{ tds_amount }}",
            "₹ {{ net_payable_amount }}",
        ],
        strict=True,
    ):
        set_cell(cell, value)

    # Signature blocks.
    p = find(d, "Name of the Sigmanix Spaces Representative")
    replace(p, "Juluri Sri Rama Siva Prasada Rao", "{{ provider_signatory_name }}")
    p = find(d, "Designation: Founder")
    replace(p, "Founder& Managing Director", "{{ provider_signatory_designation }}")
    p = find(d, "Company Name: Sigmanix Ventures Private Limited")
    replace(p, "Sigmanix Ventures Private Limited", "{{ provider_company_name }}")
    for p in [x for x in d.paragraphs if x.text.strip() == "Date: 29th May 2026"]:
        replace(p, "29th May 2026", "{{ agreement_generation_date }}")
    p = find(d, "Name of the Member Representative: K Nandakishore")
    replace(p, "K Nandakishore", "{{ representative_name }}")
    p = find(d, "Name of the Member Representative and Designation:")
    p.add_run(
        " {{ representative_name }}{% if representative_designation %}, "
        "{{ representative_designation }}{% endif %}"
    )
    p = find(d, "Member Company/Individual")
    replace(
        p,
        "Name: ",
        "Name: {% if company_name %}{{ company_name }}{% else %}{{ member_name }}{% endif %}",
    )

    # Nothing of the old member may remain.
    text = "\n".join(p.text for p in d.paragraphs) + "\n".join(
        c.text for r in d.tables[0].rows for c in r.cells
    )
    for leftover in ("Nanda", "Ramulu", "40,000", "29th May", "30,000", "20,000", "XX0051"):
        if leftover.lower() in text.lower():
            raise SystemExit(f"Old member detail still present: {leftover}")
    target.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(target))
    print(f"Template written to {target}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    build(Path(sys.argv[1]), Path(sys.argv[2]))
