"""Does a quote actually support the value claimed from it? Pure, conservative checks.

The quote guard proves a quote is on the page. These checks prove the quote carries the
value: a real sentence about 21 days cannot back a claim of 45. Each rule is deliberately
simple and errs towards "not supported", which sends the fact through the resolution ladder.

List keys use the simpler rule: every item must appear verbatim (whitespace-normalised)
in the one quote. An empty list needs an explicit statement of absence for that question.
An unrelated negation ("do not include originals") cannot waive the required items.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date

from parkingbreaker.json_types import JsonValue
from parkingbreaker.mail_timing import DeadlinePeriod, PeriodUnit, parse_deadline_period
from parkingbreaker.research.questions import LIST_KEYS, RULE_KEYS, FactKey

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
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
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_WORDS = frozenset({*_UNITS, *_TENS, "hundred"})
_TOKEN = re.compile(r"[a-z]+|\d[\d,]*(?:\.\d+)?")
_NEGATION = re.compile(r"\b(?:no|not|none|without|never|cannot|can.t)\b", re.IGNORECASE)

# Timing is date-only. The calculator supports calendar-day periods, explicit business or
# working-day periods, and one stated same-day clock cutoff (arrival by the end of the
# previous business day); it does no time-zone conversion. A plain integer DEADLINE_DAYS value
# means calendar days and keeps the strict date-only reading. The unit and the cutoff are
# supported only by an object value (mail_timing.parse_deadline_period) whose explicit words
# are in the quote. Unsupported wording must remain unresolved rather than being silently
# converted into a date; a unit is never inferred from its absence.
_DAY_UNIT = re.compile(
    r"\s*[\])}]?\s*(?:(?:st|nd|rd|th))?\s*[-\u2010-\u2015]?\s*"
    r"(?:(calendar|business|working)\s+)?days?\b",
    re.IGNORECASE,
)
_UNSUPPORTED_DEADLINE_TIME = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b"
    r"|\b(?:midnight|noon)\b"
    r"|\b(?:pst|pdt|mst|mdt|cst|cdt|est|edt|utc|gmt)\b"
    r"|\b(?:time\s*zone|timezone)\b"
    r"|\b(?:pacific|mountain|central|eastern)(?: standard| daylight)? time\b",
    re.IGNORECASE,
)

_CLOCK_TIME = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|\bnoon\b",
    re.IGNORECASE,
)
_MIDNIGHT = re.compile(r"\bmidnight\b", re.IGNORECASE)
_DEADLINE_CUE_CUTOFF = re.compile(
    r"\b(?:by|before|prior to|on or before|no later than|not later than)\b", re.IGNORECASE
)
_BUSINESS_UNIT = frozenset({"business", "working"})
_BUSINESS_WORD = re.compile(r"\b(?:business|working)\b", re.IGNORECASE)
_PAID_WORD = re.compile(r"\bpaid\b", re.IGNORECASE)
_CALENDAR_WORD = re.compile(r"\bcalendar\b", re.IGNORECASE)
_COUNTING_QUALIFIER = re.compile(
    r"\b(?:excluding|exclusive of|not counting|except|weekends?|holidays?|saturdays?|sundays?)\b",
    re.IGNORECASE,
)

# One shared vocabulary for "this is about contesting the citation". Agencies say contest,
# appeal, protest, dispute, review or adjudication for the same first-level step.
_CONTEST = (
    r"(?:contest(?:s|ed|ing)?|appeal(?:s|ed|ing)?|protest(?:s|ed|ing)?|dispute(?:s|d)?"
    r"|adjudication|hearings?|(?:initial|administrative|citation) reviews?)"
)
_CONTEST_TERM = re.compile(rf"\b{_CONTEST}\b", re.IGNORECASE)
# Destinations for other purposes. A quote naming any of them cannot establish a contest
# destination, even when it also mentions contesting: one page often lists a payment or
# prepayment-waiver box beside the appeal box, with the same recipient.
_OTHER_PURPOSE = re.compile(
    r"\b(?:(?:pre-?)?pay(?:s|ing|ments?|able)?|waivers?|refunds?|correspondence)\b",
    re.IGNORECASE,
)
_OTHER_DEADLINE_PURPOSE = re.compile(
    r"\b(?:(?:pre-?)?pay(?:s|ing|ments?|able)?|waivers?|refunds?|correspondence"
    r"|late fees?|late penalties?|penalties?|interest charges?)\b",
    re.IGNORECASE,
)

_DEADLINE_CUE_BEFORE = re.compile(
    r"\b(?:within|no later than|not later than|deadline(?: is| of| for)?|"
    r"time limit(?: is| of| for)?|due in|period (?:is|of)|window of|limit of|have|has)\b",
    re.IGNORECASE,
)
_DEADLINE_CUE_AFTER = re.compile(
    r"\b(?:window|period|deadline|time limit|to (?:appeal|contest|protest|dispute)|"
    r"for (?:appeal|contest|protest|dispute))\b",
    re.IGNORECASE,
)
_ISSUE_TRIGGER = re.compile(
    r"\b(?:of|from|after|following|starting|beginning on)\s+(?:the\s+)?"
    r"(?:(?:parking|citation|ticket)\s+)?issue(?:\s+date)?\b"
    r"|\b(?:of|from|after|following)\s+(?:the\s+)?(?:citation|ticket) date\b"
    r"|\b(?:citation|ticket)\s+(?:was|is)\s+issued\b",
    re.IGNORECASE,
)
_NOTICE_TRIGGER = re.compile(
    r"\b(?:of|from|after|following|starting|beginning on)\s+(?:the\s+)?"
    r"(?:(?:first|courtesy|delinquency)\s+)*(?:notice(?:\s+of\s+violation)?"
    r"(?:\s+mailing)?(?:\s+date)?)\b",
    re.IGNORECASE,
)
_POSTMARK_NEGATED = re.compile(
    r"\b(?:must|shall|should|will|may|can) not\b[^.!?;]{0,35}\bpostmarked\b"
    r"|\bpostmarks?\b[^.!?;]{0,60}\b(?:not accepted|not valid|not timely|"
    r"not permitted|rejected|will not be accepted)\b"
    r"|\b(?:do|does|did) not accept\b[^.!?;]{0,40}\bpostmarks?\b",
    re.IGNORECASE,
)
_POSTMARK_ACCEPTED = re.compile(
    r"\b(?:must|shall|has to|have to)\b[^.!?;]{0,45}\bpostmarked\b"
    r"[^.!?;]{0,35}\b(?:by|before|no later than|on or before)\b"
    r"|\bpostmarks?\s+dated\b[^.!?;]{0,45}\b(?:accepted|valid|timely)\b"
    r"|\bpostmarked\b[^.!?;]{0,35}\b(?:accepted|valid|timely)\b",
    re.IGNORECASE,
)
_RECEIPT_ACCEPTED = re.compile(
    r"\b(?:must|shall|has to|have to)\b[^.!?;]{0,45}"
    r"\b(?:received|delivered)\b[^.!?;]{0,45}"
    r"\b(?:by|before|no later than|on or before|within)\b",
    re.IGNORECASE,
)


def _words_value(words: list[str]) -> int:
    """Value of a run of English number words ("three hundred sixty five" -> 365)."""
    total = 0
    for word in words:
        if word == "hundred":
            total = max(total, 1) * 100
        else:
            total += _UNITS.get(word, 0) + _TENS.get(word, 0)
    return total


def numbers_in(text: str) -> Iterator[float]:
    """Every number stated in `text`, as digits (commas allowed) or English words.

    Words are read in runs: "twenty-one" and "twenty one" are 21, never 20 and 1, and
    "and" inside a run after "hundred" is skipped ("one hundred and five" is 105).
    """
    tokens = _TOKEN.findall(text.lower())
    run: list[str] = []
    for i, token in enumerate(tokens):
        if token in _NUMBER_WORDS:
            run.append(token)
            continue
        if token == "and" and run and run[-1] == "hundred":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt in _NUMBER_WORDS and nxt != "hundred":
                continue
        if run:
            yield float(_words_value(run))
            run = []
        if token[0].isdigit():
            yield float(token.replace(",", ""))
    if run:
        yield float(_words_value(run))


def _number_spans(text: str) -> tuple[tuple[float, int, int], ...]:
    """Numbers and their character spans, using the same word-number rules as numbers_in."""
    tokens = tuple(_TOKEN.finditer(text.lower()))
    run: list[re.Match[str]] = []
    result: list[tuple[float, int, int]] = []

    def flush() -> None:
        if run:
            result.append(
                (_words_value([item.group() for item in run]), run[0].start(), run[-1].end())
            )
            run.clear()

    for index, token in enumerate(tokens):
        value = token.group()
        if value in _NUMBER_WORDS:
            run.append(token)
            continue
        if value == "and" and run and run[-1].group() == "hundred":
            nxt = tokens[index + 1].group() if index + 1 < len(tokens) else ""
            if nxt in _NUMBER_WORDS and nxt != "hundred":
                continue
        flush()
        if value[0].isdigit():
            result.append((float(value.replace(",", "")), token.start(), token.end()))
    flush()
    return tuple(result)


def _deadline_clauses(quote: str) -> tuple[str, ...]:
    """Separate procedural statements so a later fee or unrelated notice cannot lend meaning."""
    # "5 p.m." must not end a statement: fold the periods before splitting.
    folded = re.sub(r"\b([ap])\.m\b\.?", r"\1m", quote, flags=re.IGNORECASE)
    return tuple(part.strip() for part in re.split(r"[.!?;\n]+", folded) if part.strip())


def _nearest_purpose_is_other(clause: str, start: int, end: int) -> bool:
    center = (start + end) / 2
    purposes = [
        (match.start(), match.end(), "contest") for match in _CONTEST_TERM.finditer(clause)
    ] + [
        (match.start(), match.end(), "other") for match in _OTHER_DEADLINE_PURPOSE.finditer(clause)
    ]
    return (
        bool(purposes)
        and min(purposes, key=lambda item: abs((item[0] + item[1]) / 2 - center))[2] == "other"
    )


def _one_explicit_cutoff(clause: str) -> bool:
    """Exactly one stated clock time (a.m./p.m./noon), no midnight; a zone name may accompany."""
    return len(_CLOCK_TIME.findall(clause)) == 1 and not _MIDNIGHT.search(clause)


def _deadline_number_in_clause(
    value: int | float,
    clause: str,
    period: DeadlinePeriod | None = None,
    *,
    number_span: tuple[int, int] | None = None,
) -> bool:
    """`period` None is the legacy reading: calendar days, no clock time anywhere.

    With a period, the unit must be stated by the words after the number (CALENDAR: unqualified
    or "calendar"; BUSINESS: "business"/"working"), the clause must not mix in another unit,
    and a clock time is accepted only when `period.time_cutoff` and it is the single stated
    cutoff of that clause.
    """
    lowered = clause.lower()
    cutoff = period is not None and period.time_cutoff
    if cutoff:
        if not _one_explicit_cutoff(clause):
            return False
    elif _UNSUPPORTED_DEADLINE_TIME.search(clause):
        return False
    spans = _number_spans(clause)
    units = [_DAY_UNIT.match(clause[end:]) for _, _, end in spans]
    if period is not None:
        if period.unit is PeriodUnit.BUSINESS:
            # Every stated day-period must be business/working; "calendar" anywhere is mixed.
            if _CALENDAR_WORD.search(clause) or any(
                (m.group(1) or "").lower() not in _BUSINESS_UNIT for m in units if m is not None
            ):
                return False
        elif _BUSINESS_WORD.search(clause) or _COUNTING_QUALIFIER.search(clause):
            # A calendar reading cannot sit beside business-day or weekend/holiday counting.
            return False
    for (number, start, end), unit in zip(spans, units, strict=True):
        if number != value:
            continue
        if number_span is not None and (start, end) != number_span:
            continue
        # Do not repair malformed split numbers such as "2 1 days" into 1 day.
        if any(
            previous != number and 0 <= start - previous_end <= 4
            for previous, _, previous_end in spans
        ):
            continue
        if unit is None:
            continue
        word = (unit.group(1) or "").lower()
        if period is not None and period.unit is PeriodUnit.BUSINESS:
            if word not in _BUSINESS_UNIT:
                continue
        elif word in _BUSINESS_UNIT:
            continue
        before = lowered[max(0, start - 70) : start]
        after = lowered[end : min(len(lowered), end + 45)]
        cue = _DEADLINE_CUE_BEFORE.search(before) or (
            cutoff and _DEADLINE_CUE_CUTOFF.search(before)
        )
        if not (cue or _DEADLINE_CUE_AFTER.search(after)):
            continue
        if _nearest_purpose_is_other(clause, start, end):
            continue
        # The broader "by/before" cue needs the clause itself to be about contesting.
        if cutoff and (not _CONTEST_TERM.search(clause) or _PAID_WORD.search(clause)):
            continue
        return True
    return False


def _clock_trigger_supported(trigger: re.Pattern[str], quote: str) -> bool:
    """Tie a clock to its deadline phrase, not an unrelated notice/issue mention."""
    clauses = _deadline_clauses(quote)
    for index, clause in enumerate(clauses):
        if re.search(r"\b(?:do|does|must|shall|should|may|can)\s+not\b", clause, re.I):
            continue
        for clock in trigger.finditer(clause):
            # A complete separate clock statement may accompany a duration elsewhere.
            if (
                re.fullmatch(
                    r"\s*(?:the\s+)?(?:(?:appeal|contest|review)\s+)?"
                    r"(?:deadline|period|window|time limit|clock)\s+"
                    r"(?:is (?:calculated|measured)|runs|starts|begins)\s*",
                    clause[: clock.start()],
                    re.IGNORECASE,
                )
                and not clause[clock.end() :].strip()
                and (
                    index == 0
                    or (
                        _CONTEST_TERM.search(clauses[index - 1])
                        and _deadline_period_present(clauses[index - 1])
                        and not _OTHER_DEADLINE_PURPOSE.search(clauses[index - 1])
                        and not (
                            (
                                _NOTICE_TRIGGER if trigger is _ISSUE_TRIGGER else _ISSUE_TRIGGER
                            ).search(clauses[index - 1])
                        )
                    )
                )
            ):
                return True
            for number, start, end in _number_spans(clause):
                unit = _DAY_UNIT.match(clause[end:])
                if unit is None or int(number) != number:
                    continue
                period_end = end + unit.end()
                if period_end <= clock.start():
                    linked = re.fullmatch(
                        rf"\s*,?\s*(?:(?:to|for)\s+{_CONTEST}\s*)?"
                        r"(?:(?:of|from|after|following)\s+(?:the\s+)?)?",
                        clause[period_end : clock.start()],
                        re.IGNORECASE,
                    )
                elif clock.end() <= start:
                    if clause[: clock.start()].strip():
                        continue
                    linked = re.fullmatch(
                        r"\s*[,:—-]?\s*(?:you\s+have|within)\s+",
                        clause[clock.end() : start],
                        re.IGNORECASE,
                    )
                else:
                    continue
                if linked is None:
                    continue
                if not (
                    _CONTEST_TERM.search(clause)
                    or re.search(r"\b(?:deadline|time limit|period|window)\b", clause, re.I)
                    or re.match(r"\s*(?:within|no later than|not later than)\b", clause, re.I)
                ):
                    continue
                period = (
                    DeadlinePeriod(int(number), PeriodUnit.BUSINESS)
                    if (unit.group(1) or "").lower() in _BUSINESS_UNIT
                    else None
                )
                if _deadline_number_in_clause(number, clause, period, number_span=(start, end)):
                    return True
    return False


def clock_quote_supported_by_source(value: JsonValue, quote: str, text: str) -> bool:
    """Check the quoted trigger in each complete saved clause that contains it.

    Exact short quotes are retained, but cannot strip a recordkeeping instruction's
    meaning. Repeated identical procedure clauses are harmless; ambiguous uses refuse.
    """
    normal_text = " ".join(text.split())
    normal_quote = " ".join(quote.split())
    trigger = (
        _ISSUE_TRIGGER
        if value == "ISSUE_DATE"
        else _NOTICE_TRIGGER
        if value == "NOTICE_DATE"
        else None
    )
    if not normal_quote or trigger is None or not trigger.search(normal_quote):
        return False
    occurrences = tuple(re.finditer(re.escape(normal_quote), normal_text))
    if not occurrences:
        return False
    boundaries = tuple(re.finditer(r"[.!?;]+(?:\s+|$)", normal_text))
    contexts: set[str] = set()
    for found in occurrences:
        start = max((b.end() for b in boundaries if b.end() <= found.start()), default=0)
        end = min((b.end() for b in boundaries if b.end() >= found.end()), default=len(normal_text))
        # Include an immediately preceding statement for generic standalone clocks:
        # a late-fee period cannot lend its clock to the appeal.
        previous = max((b.end() for b in boundaries if b.end() < start), default=0)
        context = normal_text[start:end]
        if trigger.fullmatch(context.strip().rstrip(".!?;")) or re.match(
            r"\s*(?:the\s+)?(?:deadline|period|window|time limit|clock)\b", context, re.I
        ):
            context = normal_text[previous:end]
        contexts.add(context)
    return all(value_supported_by_quote(FactKey.DEADLINE_CLOCK_START, value, c) for c in contexts)


def _deadline_period_value_supported(period: DeadlinePeriod, quote: str) -> bool:
    clauses = _deadline_clauses(quote)
    if period.time_cutoff:
        # The one stated cutoff must sit in the deadline clause; a time anywhere else in the
        # quote is unrelated or ambiguous wording and keeps the veto.
        timed = [c for c in clauses if _UNSUPPORTED_DEADLINE_TIME.search(c)]
        return any(
            _deadline_number_in_clause(period.days, c, period) and all(t is c for t in timed)
            for c in clauses
        )
    if _UNSUPPORTED_DEADLINE_TIME.search(quote):
        return False
    return any(_deadline_number_in_clause(period.days, c, period) for c in clauses)


def _deadline_number_supported(value: int | float, quote: str) -> bool:
    return any(_deadline_number_in_clause(value, clause) for clause in _deadline_clauses(quote))


def _deadline_period_present(quote: str) -> bool:
    return any(
        _deadline_number_in_clause(number, clause)
        for clause in _deadline_clauses(quote)
        for number, _, _ in _number_spans(clause)
        if 1 <= number <= 365
    )


def _states_number(value: float, quote: str) -> bool:
    return value in set(numbers_in(quote))


def _contains(needle: str, haystack: str) -> bool:
    return " ".join(needle.split()) in " ".join(haystack.split())


def address_text(text: str) -> str:
    """Case and punctuation may differ; letters and complete numeric tokens may not."""
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def address_matches(value: JsonValue, quote: str) -> bool:
    """Whether a quote carries the complete delivery point, recipient and attention."""
    if not isinstance(value, dict):
        return False
    for field in ("recipient", "line1", "city", "state", "zip_code"):
        item = value.get(field)
        if not isinstance(item, str) or not address_text(item):
            return False
    for field in ("attention", "line2"):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or not address_text(item)):
            return False
    # Keep the delivery point together. Matching isolated numbers permits a fabricated
    # street, or an address spliced from separate payment and contest destinations.
    delivery = " ".join(
        address_text(item)
        for field in ("line1", "line2", "city", "state", "zip_code")
        if isinstance(item := value.get(field), str)
    )
    quoted = f" {address_text(quote)} "
    if f" {delivery} " not in quoted:
        return False
    return all(
        f" {address_text(item)} " in quoted
        for field in ("recipient", "attention")
        if isinstance(item := value.get(field), str)
    )


def _address_span(value: JsonValue, quote: str) -> tuple[int, int] | None:
    """Locate the quoted delivery-point tokens so purpose is read in their own clause."""
    if not isinstance(value, dict):
        return None
    delivery = " ".join(
        address_text(item)
        for field in ("line1", "line2", "city", "state", "zip_code")
        if isinstance(item := value.get(field), str)
    )
    expected = address_text(delivery).split()
    tokens = tuple(re.finditer(r"[^\W_]+", quote.casefold()))
    for index in range(len(tokens) - len(expected) + 1):
        found = tokens[index : index + len(expected)]
        if [token.group() for token in found] == expected:
            return found[0].start(), found[-1].end()
    return None


_ABSENCE_PATTERNS: dict[FactKey, str] = {
    FactKey.REQUIRED_ITEMS: (
        r"(?:no (?:items|documents) are (?:required|needed)|nothing is (?:required|needed))"
    ),
    FactKey.REQUIRED_DECLARATIONS: (
        r"(?:no (?:signed )?(?:declaration|statement) is (?:required|needed)"
        r"|no (?:signed )?(?:declarations|statements) are (?:required|needed)"
        r"|a (?:signed )?(?:declaration|sworn statement) is not (?:required|needed))"
    ),
    FactKey.CONTEST_ELIGIBILITY: r"(?:no standing restrictions apply|anyone may contest)",
    FactKey.MANDATORY_FORMS: r"no (?:agency )?forms? (?:is|are) (?:required|needed)",
    FactKey.SIGNATURE_REQUIREMENT: r"no signatures? (?:is|are) (?:required|needed)",
    FactKey.PRIOR_STEP_REQUIREMENT: r"no prior steps? (?:is|are) (?:required|needed)",
    FactKey.TICKET_SPECIFIC_REQUIREMENTS: (
        r"no (?:other |ticket-specific )?(?:requirements|conditions) (?:apply|are required)"
    ),
    FactKey.CODE_EXCEPTIONS: (
        r"(?:there are no exceptions|no exceptions (?:apply|are (?:provided|permitted)))"
    ),
}


def _states_absence(key: FactKey, quote: str) -> bool:
    # Accept one explicit statement, not a negation embedded in a list of requirements.
    # Unrecognized wording remains unresolved and goes through the ladder.
    normal = " ".join(quote.lower().split())
    return bool(
        re.fullmatch(_ABSENCE_PATTERNS[key] + r"(?: for a first-level review)?[.]?", normal)
    )


def _purpose_problem(value: JsonValue, quote: str) -> str | None:
    span = _address_span(value, quote)
    if span is None:
        return "address purpose cannot be tied to the quoted delivery point"
    separators = tuple(re.finditer(r"[!?;]+|(?<![PO])\.(?=\s+[A-Z0-9])", quote, re.IGNORECASE))
    start = max((match.end() for match in separators if match.end() <= span[0]), default=0)
    end = min(
        (match.start() for match in separators if match.start() >= span[1]), default=len(quote)
    )
    clause = quote[start:end]
    other = _OTHER_PURPOSE.search(clause)
    if other:
        return f"address quote names another purpose ({other.group(0).lower()})"
    if not _CONTEST_TERM.search(clause):
        nearby_contests = tuple(
            match
            for match in _CONTEST_TERM.finditer(quote[: span[0]])
            if span[0] - match.end() <= 160
        )
        linked_destination = bool(
            nearby_contests
            and not _OTHER_PURPOSE.search(quote[nearby_contests[-1].end() : span[0]])
            and re.search(r"\b(?:mail|send|letter|form|address|destination)\b", clause, re.I)
        )
        if not linked_destination:
            return "address quote does not say the address is for contesting"
    return None


def _address_supported(value: JsonValue, quote: str) -> bool:
    # The bytes of an address prove nothing about what it is for. The same quote must tie
    # it to contesting (lead-in, recipient or attention line) and name no other purpose.
    return address_matches(value, quote) and _purpose_problem(value, quote) is None


def address_purpose_problem(value: JsonValue, quote: str) -> str | None:
    """The purpose failure when `quote` carries the address `value` but cannot establish it
    as a contest destination; None when the address is missing or its purpose is clear."""
    return _purpose_problem(value, quote) if address_matches(value, quote) else None


_DIRECT_DECLARATION = r"I declare that the foregoing is true and correct[.:]?"


def direct_declaration(text: str) -> str | None:
    """A complete printed declaration line; never truncate a longer statement."""
    match = re.search(rf"^{_DIRECT_DECLARATION}$", text, re.M)
    return None if match is None else match.group()


def signed_declaration_block(text: str) -> str | None:
    """Exact declaration followed by the form's signature and date fields."""
    match = re.search(
        rf"^{_DIRECT_DECLARATION}\n+(?i:SIGNATURE)(?:[ \t]+|\n+)(?i:DATE)$",
        text,
        re.M,
    )
    return None if match is None else match.group()


def value_supported_by_quote(key: FactKey, value: JsonValue, quote: str) -> bool:
    """True when `quote` states `value` for `key` by the rules in this module's docstring."""
    lowered = quote.lower()
    period = parse_deadline_period(value) if key is FactKey.DEADLINE_DAYS else None
    cutoff_period = period is not None and isinstance(value, dict) and period.time_cutoff
    if (
        key
        in {
            FactKey.DEADLINE_DAYS,
            FactKey.DEADLINE_CLOCK_START,
            FactKey.DEADLINE_RULE,
            FactKey.DEADLINE_ABSOLUTE,
        }
        and not cutoff_period
        and _UNSUPPORTED_DEADLINE_TIME.search(quote)
    ):
        return False
    match key:
        case FactKey.MAIL_IN_ACCEPTED:
            # A mention of a mailing address or mailed payments is not a contest route.
            # Unsupported wording stays UNKNOWN; never infer permission from the registry.
            if value is not True or _NEGATION.search(quote) or _OTHER_PURPOSE.search(quote):
                return False
            return bool(
                re.fullmatch(
                    rf"{_CONTEST} (?:sent|submitted) by mail must be "
                    r"(?:received|postmarked)[^.!?;]*[.]?"
                    r"|you (?:may|can)(?: also)? (?:(?:submit|send|file|request) "
                    rf"(?:your |an? |the )?)?{_CONTEST}\b[^.!?;]*\bby mail[.]?"
                    rf"|you (?:may|can)(?: also)? mail (?:your |an? |the )?{_CONTEST}"
                    r"(?: letter| request| form)?\b[^.!?;]*[.]?"
                    r"|to (?:submit|send|file|request) (?:your |an? |the )?"
                    rf"{_CONTEST} by mail\b[^.!?;]*[.:]?"
                    rf"|to submit (?:your |an? |the ){_CONTEST} via mail, you must include:"
                    rf"|(?:we accept|the agency accepts) (?:mailed {_CONTEST}"
                    rf"|{_CONTEST} by mail)[.]?",
                    " ".join(lowered.split()),
                )
            )
        case FactKey.DEADLINE_DAYS:
            if isinstance(value, dict):
                return period is not None and _deadline_period_value_supported(period, quote)
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            return _deadline_number_supported(value, quote)
        case FactKey.FINE_AMOUNT:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            return _states_number(value, quote)
        case FactKey.DEADLINE_RULE:
            if value == "POSTMARK":
                clauses = _deadline_clauses(quote)
                return not any(_POSTMARK_NEGATED.search(clause) for clause in clauses) and any(
                    _POSTMARK_ACCEPTED.search(clause) for clause in clauses
                )
            if value == "RECEIPT":
                clauses = _deadline_clauses(quote)
                return not any(_POSTMARK_ACCEPTED.search(clause) for clause in clauses) and any(
                    _RECEIPT_ACCEPTED.search(clause) for clause in clauses
                )
            return False
        case FactKey.DEADLINE_CLOCK_START:
            if value == "ISSUE_DATE":
                return _clock_trigger_supported(_ISSUE_TRIGGER, quote)
            if value == "NOTICE_DATE":
                return _clock_trigger_supported(_NOTICE_TRIGGER, quote)
            return False
        case FactKey.MAILING_ADDRESS:
            return _address_supported(value, quote)
        case FactKey.GOVERNING_CODE_SECTION:
            return isinstance(value, str) and bool(value.strip()) and _contains(value, quote)
        case FactKey.DEADLINE_ABSOLUTE:
            if not isinstance(value, dict):
                return False
            if value.get("applicable") is False and value.get("date") is None:
                return _deadline_period_present(quote) and not _NEGATION.search(quote)
            raw = value.get("date")
            if value.get("applicable") is not True or not isinstance(raw, str):
                return False
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                return False
            dates = (raw, day.strftime("%B %d, %Y"), f"{day.strftime('%B')} {day.day}, {day.year}")
            return (
                any(d in quote for d in dates)
                and bool(re.search(r"\b(?:by|before|deadline)\b", lowered))
                and bool(_CONTEST_TERM.search(quote))
            )
        case key if key in LIST_KEYS:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return False
            if not value:
                return _states_absence(key, quote)
            supported = all(isinstance(v, str) and _contains(v, quote) for v in value)
            if key is FactKey.SIGNATURE_REQUIREMENT:
                block = signed_declaration_block(quote)
                if block is not None and " ".join(quote.split()) == " ".join(block.split()):
                    return supported
            if key in RULE_KEYS:
                # A noun mentioned on a page does not establish a compulsory condition.
                topics = {
                    FactKey.CONTEST_ELIGIBILITY: (
                        r"\b(?:owner|driver|standing|eligible|eligibility|"
                        r"representative|renter|anyone)\b"
                    ),
                    FactKey.MANDATORY_FORMS: r"\bforms?\b",
                    FactKey.SIGNATURE_REQUIREMENT: r"\b(?:signatures?|signed|sign)\b",
                    FactKey.PRIOR_STEP_REQUIREMENT: r"\b(?:prior|before|first|previous|initial)\b",
                    FactKey.TICKET_SPECIFIC_REQUIREMENTS: r".",
                }
                return (
                    supported
                    and bool(
                        re.search(
                            r"\b(?:must|required|only|shall|eligible)\b"
                            r"|^(?:include|complete|enclose|attach|submit|provide|sign|return)\b"
                            r"|\bmail (?:your |an? |the )?(?:appeal|contest|dispute)"
                            r"(?: letter| request)? with\b",
                            lowered,
                        )
                    )
                    and bool(re.search(topics[key], lowered))
                )
            return supported

    return False
