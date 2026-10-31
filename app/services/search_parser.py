"""Turns a workspace request written as a sentence into search filters, without an LLM.

It recognises the things the inventory actually records: the kind of space, people,
area, date and time, how long, budget, place and amenities. Places and amenities count
only when they match what is listed; anything else is reported as unknown, never guessed.
Nothing missing is filled with a default: no date means availability is not checked.
"""

from __future__ import annotations

import re
from datetime import date, time, timedelta

from app.models.enums import BillingPeriod, SpaceType
from app.schemas.search import SearchIntent

WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "single": 1,
    "two": 2,
    "couple": 2,
    "pair": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "hundred": 100,
}
NUMBER = r"(\d+|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"

# Longest phrases first; each match is removed so "meeting room" is not also "room".
TYPE_PHRASES: list[tuple[str, list[SpaceType]]] = [
    (r"conference (?:room|hall)s?|board ?rooms?", [SpaceType.CONFERENCE_ROOM]),
    (r"training (?:room|hall)s?|class ?rooms?", [SpaceType.TRAINING_ROOM]),
    (r"meeting (?:room|space)s?", [SpaceType.MEETING_ROOM]),
    (r"(?:whole|entire|full) floors?", [SpaceType.FLOOR]),
    (r"(?:office )?portions?|zones?", [SpaceType.PORTION]),
    (r"dedicated (?:workstation|desk)s?|fixed desks?", [SpaceType.DEDICATED_WORKSTATION]),
    (
        r"(?:hot|flexi(?:ble)?|shared|co-?working) desks?|hot ?desking",
        [SpaceType.FLEXIBLE_WORKSTATION, SpaceType.DESK],
    ),
    (r"workstations?", [SpaceType.DEDICATED_WORKSTATION, SpaceType.FLEXIBLE_WORKSTATION]),
    (r"(?:private )?cabins?", [SpaceType.CABIN]),
    (r"(?:small |private |team )?offices?(?: space)?", [SpaceType.PRIVATE_OFFICE, SpaceType.CABIN]),
    (r"co-?working(?: space| area)?", [SpaceType.COWORKING, SpaceType.FLEXIBLE_WORKSTATION]),
    (r"desks?", [SpaceType.DESK, SpaceType.FLEXIBLE_WORKSTATION, SpaceType.DEDICATED_WORKSTATION]),
    (r"seats?", [SpaceType.SEAT]),
]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}
MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
PERIOD_WORDS = {
    "hour": BillingPeriod.HOURLY,
    "hr": BillingPeriod.HOURLY,
    "day": BillingPeriod.DAILY,
    "week": BillingPeriod.WEEKLY,
    "month": BillingPeriod.MONTHLY,
    "mo": BillingPeriod.MONTHLY,
}
UNIT_TO_PERIOD = {
    "HOUR": BillingPeriod.HOURLY,
    "HALF_DAY": BillingPeriod.HALF_DAY,
    "DAY": BillingPeriod.DAILY,
    "WEEK": BillingPeriod.WEEKLY,
    "MONTH": BillingPeriod.MONTHLY,
}
STOP = re.compile(
    r"\b(for|with|from|between|on|at|under|below|within|next|this|tomorrow|today|available|"
    r"and|that|which|by|starting|budget|per|around|about)\b"
)


def _number(token: str) -> int:
    return int(token) if token.isdigit() else WORDS[token]


def _amount(text: str, scale: str | None) -> float:
    value = float(text.replace(",", ""))
    return value * {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lac": 100_000}.get(
        scale or "", 1
    )


def _clock(hour: str, minute: str | None, meridian: str | None) -> time | None:
    h, m = int(hour), int(minute or 0)
    if meridian:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if meridian.startswith("p") else 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def _next_weekday(today: date, index: int, *, skip_this_week: bool) -> date:
    ahead = (index - today.weekday()) % 7
    if ahead == 0 or skip_this_week:
        ahead = ahead or 7
    return today + timedelta(days=ahead)


def parse(
    query: str,
    *,
    today: date,
    known_places: list[str],
    known_amenities: list[str],
) -> tuple[SearchIntent, list[str]]:
    """Returns the filters and any problems found while reading the sentence."""
    text = " " + re.sub(r"\s+", " ", query.lower().replace("₹", " rs ")).strip() + " "
    problems: list[str] = []
    intent = SearchIntent()

    # Amenities: "with parking and meeting-room access". Read first, then removed, so
    # the words inside (like "meeting room") are not taken as the kind of space.
    clause = re.search(
        r"\bwith ([a-z0-9 ,&/-]+?)(?= (?:in|near|at|for|from|on|under|below)\b| ?$)", text
    )
    if clause:
        wanted = [
            w.strip(" -") for w in re.split(r",| and | & |/", clause.group(1)) if w.strip(" -")
        ]
        for item in wanted:
            match = next(
                (
                    a
                    for a in known_amenities
                    if a.lower() == item
                    or a.lower() in item
                    or item in a.lower()
                    or a.lower().replace(" ", "-") in item
                ),
                None,
            )
            if match and match not in intent.amenities:
                intent.amenities.append(match)
            elif not match and len(item) <= 60:
                intent.unknown_amenities.append(item)
        text = text.replace(clause.group(0), " ")

    # People.
    people = re.search(
        rf"\b(?:for|team of|group of)?\s*{NUMBER}\s*(?:people|persons|pax|members|employees|"
        r"colleagues|team members|seater|guests)\b",
        text,
    ) or re.search(rf"\bteam of {NUMBER}\b", text)
    if not people:
        # A bare "for 4", unless the number is a duration, a time, a date or an amount.
        bare = re.search(
            rf"\bfor {NUMBER}\b(?!\s*(?:[:.]\d|am\b|pm\b|o'?clock|h\b|hrs?\b|hours?\b|days?\b|"
            r"weeks?\b|months?\b|years?\b|nights?\b|sq|k\b|lakh|rs\b|inr\b|rupees|/|-|st\b|nd\b|"
            r"rd\b|th\b|full\b|whole\b|half\b|entire\b|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))",
            text,
        )
        if bare and 0 < _number(bare.group(1)) <= 500:
            people = bare
    if people:
        intent.capacity_min = _number(people.group(1))
        text = text.replace(people.group(0), " ")
    seats = re.search(rf"\b{NUMBER}\s+seats\b", text)
    if seats and intent.capacity_min is None:
        intent.capacity_min = _number(seats.group(1))
        text = text.replace(seats.group(0), " seats ")

    # Area.
    area = re.search(
        r"\b(around|about|approx(?:imately)?|at least|minimum|min|under|below|up to|upto|max)?"
        r"\s*(\d[\d,]*(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sft|square feet|sq feet)\b",
        text,
    )
    if area:
        size = float(area.group(2).replace(",", ""))
        qualifier = area.group(1) or "around"
        if qualifier in ("at least", "minimum", "min"):
            intent.area_min_sqft = size
        elif qualifier in ("under", "below", "up to", "upto", "max"):
            intent.area_max_sqft = size
        else:
            intent.area_min_sqft, intent.area_max_sqft = round(size * 0.85), round(size * 1.15)
        text = text.replace(area.group(0), " ")

    # Budget.
    budget = re.search(
        r"\b(?:under|below|less than|within|up to|upto|max(?:imum)?|not more than|"
        r"budget(?: of| is)?)"
        r"\s*(?:rs\.?|inr|rupees)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?\b"
        r"(?:\s*(?:rs|inr|rupees))?(?:\s*(?:per|a|an|/|each)\s*(hour|hr|day|week|month|mo)\b)?",
        text,
    )
    if budget:
        intent.budget_max = _amount(budget.group(1), budget.group(2))
        if budget.group(3):
            intent.budget_period = PERIOD_WORDS[budget.group(3)]
        text = text.replace(budget.group(0), " ")
    elif re.search(r"\b(?:within|in) (?:my|our) budget\b", text):
        problems.append("What is your budget, and is it per hour, day or month?")

    # Duration.
    if re.search(r"\bhalf[ -]day\b", text):
        intent.duration_unit, intent.duration_value = "HALF_DAY", 1
    elif re.search(r"\b(?:one |a |1 )?(?:full|whole|entire) day\b", text):
        intent.duration_unit, intent.duration_value = "DAY", 1
    else:
        span = re.search(rf"\b(?:for )?{NUMBER}\s*(hours?|hrs?|days?|weeks?|months?)\b", text)
        if span:
            unit = span.group(2).rstrip("s")
            intent.duration_unit = {
                "hour": "HOUR",
                "hr": "HOUR",
                "day": "DAY",
                "week": "WEEK",
                "month": "MONTH",
            }[unit]
            intent.duration_value = _number(span.group(1))
            text = text.replace(span.group(0), " ")
        elif re.search(r"\b(?:monthly|per month|month[- ]to[- ]month|monthly rental)\b", text):
            intent.duration_unit = "MONTH"
        elif re.search(r"\b(?:weekly|per week)\b", text):
            intent.duration_unit = "WEEK"
        elif re.search(r"\b(?:hourly|per hour)\b", text):
            intent.duration_unit = "HOUR"
    if intent.budget_max and intent.budget_period is None and intent.duration_unit:
        intent.budget_period = UNIT_TO_PERIOD[intent.duration_unit]
    if intent.budget_max and intent.budget_period is None:
        problems.append("Is your budget per hour, per day or per month?")

    # Time of day.
    meridian = r"\s*(am|pm|a\.m\.|p\.m\.)?"
    # Dates are masked so their digits ("2026-11-16") are not read as a time range.
    clock_text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b", " ", text)
    window = next(
        (
            w
            for w in re.finditer(
                rf"\b(?:between|from)?\s*(\d{{1,2}})(?::(\d{{2}}))?{meridian}\s*"
                rf"(?:and|to|-|–|till|until)\s*(\d{{1,2}})(?::(\d{{2}}))?{meridian}",
                clock_text,
            )
            if w.group(3) or w.group(6) or w.group(2) or w.group(5)
        ),
        None,
    )
    if window:
        end_meridian = window.group(6)
        start_meridian = window.group(3) or end_meridian
        start = _clock(window.group(1), window.group(2), start_meridian)
        end = _clock(window.group(4), window.group(5), end_meridian or start_meridian)
        if start and end and end > start:
            intent.start_time, intent.end_time = start, end
        else:
            problems.append("The end time must be after the start time.")
        text = text.replace(window.group(0), " ")
    else:
        at = re.search(rf"\b(?:at|from)\s*(\d{{1,2}})(?::(\d{{2}}))?{meridian}\b", clock_text)
        if at and (at.group(3) or at.group(2)):
            start = _clock(at.group(1), at.group(2), at.group(3))
            if start:
                intent.start_time = start
            text = text.replace(at.group(0), " ")
    if intent.start_time and not intent.end_time and intent.duration_unit == "HOUR":
        end_hour = intent.start_time.hour + (intent.duration_value or 1)
        if end_hour <= 23:
            intent.end_time = time(end_hour, intent.start_time.minute)

    # Date.
    day: date | None = None
    if re.search(r"\bday after tomorrow\b", text):
        day = today + timedelta(days=2)
    elif re.search(r"\btomorrow\b", text):
        day = today + timedelta(days=1)
    elif re.search(r"\btoday\b|\btonight\b", text):
        day = today
    elif re.search(r"\bnext week\b", text):
        day = _next_weekday(today, 0, skip_this_week=True)
    elif re.search(r"\bnext month\b", text):
        day = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    else:
        weekday = re.search(r"\b(next |this |on |coming )?(" + "|".join(WEEKDAYS) + r")\b", text)
        iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        numeric = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b", text)
        named = re.search(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_NAMES})\b", text
        ) or (re.search(rf"\b({MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text))
        try:
            if iso:
                day = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            elif numeric:  # Indian order: day/month/year
                year = int(numeric.group(3))
                day = date(
                    year + 2000 if year < 100 else year,
                    int(numeric.group(2)),
                    int(numeric.group(1)),
                )
            elif named:
                first, second = named.group(1), named.group(2)
                d, m = (first, second) if first.isdigit() else (second, first)
                day = date(today.year, MONTHS[m], int(d))
                if day < today:
                    day = day.replace(year=today.year + 1)
            elif weekday:
                day = _next_weekday(
                    today,
                    WEEKDAYS.index(weekday.group(2)),
                    skip_this_week=(weekday.group(1) or "").strip() == "next",
                )
        except ValueError:
            problems.append("That date does not exist; check the day and month.")
    if day is not None:
        if day < today:
            problems.append("That date has passed; choose today or a later date.")
        else:
            intent.date = day

    # Kinds of space.
    for pattern, types in TYPE_PHRASES:
        found = re.search(rf"\b(?:{pattern})\b", text)
        if found:
            for t in types:
                if t not in intent.resource_types:
                    intent.resource_types.append(t)
            text = text.replace(found.group(0), " ")

    # Places: known names anywhere, and the words after "in / near / at / around".
    places = sorted({p for p in known_places if p}, key=len, reverse=True)
    for place in places:
        if re.search(rf"\b{re.escape(place.lower())}\b", text):
            if place not in intent.locations:
                intent.locations.append(place)
            text = re.sub(rf"\b{re.escape(place.lower())}\b", " ", text)
    for match in re.finditer(r"\b(?:in|near|at|around|close to)\s+([a-z][a-z .'-]{1,40})", text):
        phrase = STOP.split(match.group(1))[0].strip(" .,'-")
        plausible = len(phrase) > 2 and not re.search(r"\d|\b(?:the|my|a|an|am|pm)\b", phrase)
        known = any(phrase in p.lower() or p.lower() in phrase for p in intent.locations)
        if plausible and not known:
            intent.unknown_locations.append(phrase)
    return intent, problems
