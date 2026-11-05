"""Indian Rupee amounts in words, with Indian numbering (thousand, lakh, crore).

    rupees_in_words(Decimal("115500.50"))
    -> "Rupees One Lakh Fifteen Thousand Five Hundred and Fifty Paise Only"

Rules (Phase 4B):
- The amount is rounded to whole paise, half up (1.005 -> 1.01), as on an invoice.
- Paise are written as a number of paise: ".50" is "Fifty Paise", ".05" is "Five Paise".
- No paise: "Rupees ... Only". Only paise: "Rupees Zero and Fifty Paise Only".
  Zero: "Rupees Zero Only".
- Above 99 crore the crore part is itself written in Indian numbering
  (e.g. "One Hundred Crore", "One Thousand Crore").
- Negative amounts are refused; contracts never hold them.
"""

from decimal import ROUND_HALF_UP, Decimal

_ONES = [
    "Zero",
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
    "Thirteen",
    "Fourteen",
    "Fifteen",
    "Sixteen",
    "Seventeen",
    "Eighteen",
    "Nineteen",
]
_TENS = ["_", "_", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} Hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def indian_words(n: int) -> str:
    """A whole number in words with Indian numbering. 0 is "Zero"."""
    if n < 0:
        raise ValueError("Negative amounts are not written in words.")
    if n == 0:
        return "Zero"
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    parts = []
    if crore:
        parts.append(f"{indian_words(crore)} Crore")
    if lakh:
        parts.append(f"{_below_hundred(lakh)} Lakh")
    if thousand:
        parts.append(f"{_below_hundred(thousand)} Thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def rupees_in_words(amount: Decimal | int | float | str) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if value < 0:
        raise ValueError("Negative amounts are not written in words.")
    rupees = int(value)
    paise = int((value - rupees) * 100)
    words = f"Rupees {indian_words(rupees)}"
    if paise:
        words += f" and {_below_hundred(paise)} Paise"
    return words + " Only"
