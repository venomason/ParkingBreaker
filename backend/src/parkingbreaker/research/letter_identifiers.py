"""Deterministic verification of vehicle plate and registration state in frozen packets.

`PacketPlan.customer_fields` is the sole authoritative source of expected vehicle
identifiers. Unanchored substrings in prose, postal City/State/ZIP address lines,
uploaded-photo caption pages, and AcroForm widget pages never satisfy a written-letter
identifier requirement.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from parkingbreaker.research.questions import PacketPlan

STATE_NAMES_TO_CODE: Final[dict[str, str]] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

# US territories and Canadian provinces/territories also appear on confirmed plates.
US_AND_CA_JURISDICTION_CODES = frozenset(STATE_NAMES_TO_CODE.values()) | frozenset(
    (
        "PR",
        "GU",
        "VI",
        "AS",
        "MP",
        "AB",
        "BC",
        "MB",
        "NB",
        "NL",
        "NS",
        "NT",
        "NU",
        "ON",
        "PE",
        "QC",
        "SK",
        "YT",
    )
)

_DASH_TRANSLATION: Final = str.maketrans("\u2010\u2011\u2012\u2013\u2014\u2212", "------")


class IdentifierKind(StrEnum):
    PLATE_NUMBER = "plate_number"
    PLATE_STATE = "plate_state"
    PLATE_AND_STATE = "plate_and_state"


@dataclass(frozen=True)
class ExpectedVehicleIdentifiers:
    plate: str | None = None
    plate_state: str | None = None


@dataclass(frozen=True)
class LabeledPlateSpan:
    plate_token: str | None
    state_code: str | None
    malformed_plate: bool = False
    malformed_state: bool = False


_DOCUMENT_OR_COPY_NOISE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:cop(?:y|ies)|photo(?:graph)?s?|images?|pictures?|documents?|cards?|"
    r"certificates?|proof|attach(?:ed|ment)?|enclos(?:e|ed|ure)|original|forms?)\b",
    re.I,
)

_TRAILING_CLAUSE: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:is\s+required|are\s+required|must\s+be\s+included(?:\s+in\s+the\s+request)?)$",
    re.I,
)

_COMBINED_ITEM: Final[re.Pattern[str]] = re.compile(
    r"^(?:the\s+|your\s+)?(?:vehicle(?:'s)?\s+|car(?:'s)?\s+)?"
    r"(?:license\s+plate(?:\s+number)?|plate(?:\s+number)?|"
    r"registration\s+and\s+plate(?:\s+number)?)"
    r"\s+(?:and|&)\s+(?:the\s+|your\s+)?"
    r"(?:(?:registration|issuing|license\s+plate|plate)\s+)?state$"
    r"|^(?:the\s+|your\s+)?state\s+(?:and|&)\s+"
    r"(?:vehicle\s+)?(?:license\s+plate(?:\s+number)?|plate(?:\s+number)?)$",
    re.I,
)

_PLATE_ITEM: Final[re.Pattern[str]] = re.compile(
    r"^(?:the\s+|your\s+)?(?:vehicle(?:'s)?\s+|car(?:'s)?\s+)?"
    r"(?:license\s+plate(?:\s+number)?|plate\s+number|plate|tag\s+number|"
    r"registration\s+and\s+plate(?:\s+number)?)$",
    re.I,
)

_STATE_ITEM: Final[re.Pattern[str]] = re.compile(
    r"^(?:the\s+|your\s+)?"
    r"(?:(?:vehicle\s+|car\s+)?(?:license\s+plate\s+|plate\s+|registration\s+|issuing\s+)state"
    r"|state\s+of\s+(?:registration|issuance|license\s+plate)"
    r"|state\s+where\s+(?:your\s+|the\s+)?(?:car|vehicle)\s+is\s+registered"
    r"|state)$",
    re.I,
)

_VEHICLE_QUOTE_CONTEXT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:(?:registration|issuing|plate) state|state of (?:registration|issuance)|"
    r"state (?:where|in which) (?:the |your )?(?:vehicle|car) is registered|"
    r"(?:plate|tag)(?: number)?\s*(?:and|&|/)\s*(?:its |the |your )?state\b"
    r"(?! of (?:residence|issue of the ticket))|state and (?:your )?(?:license )?plate)\b",
    re.I,
)

_POSTAL_CITY_STATE_ZIP: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Za-z .'-]{2,40},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"
)

_COMBINED_OR_PLATE_LABEL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*\u2022]\s*)?"
    r"(?P<label>"
    r"(?:vehicle\s+)?license\s+plate(?:\s*(?:number|no\.?|#))?(?:\s*(?:and|&|/)\s*state)?"
    r"|plate(?:\s*(?:number|no\.?|#))?(?:\s*(?:and|&|/)\s*state)?"
    r"|vehicle\s+plate(?:\s*(?:and|&|/)\s*state)?"
    r"|registration\s*(?:/|and)\s*plate(?:\s*(?:number|no\.?|#))?"
    r")"
    r"\s*[:\-]\s*(?P<rhs>[^\r\n]*)\s*$",
    re.I,
)

_DEDICATED_STATE_LABEL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*\u2022]\s*)?"
    r"(?P<label>"
    r"(?:vehicle\s+|license\s+plate\s+|plate\s+|registration\s+|issuing\s+)state"
    r"|state\s+of\s+(?:registration|issuance)"
    r")"
    r"\s*[:\-]\s*(?P<rhs>[^\r\n]*)\s*$",
    re.I,
)

_STATE_NAME_ALT: Final[str] = "|".join(
    re.escape(name) for name in sorted(STATE_NAMES_TO_CODE, key=len, reverse=True)
)

_PREFIX_STATE = rf"(?:[A-Z]{{2}}|(?i:{_STATE_NAME_ALT}))"
_SUFFIX_STATE = rf"(?i:[a-z]{{2}}|{_STATE_NAME_ALT})"
_STATE_ON_NEXT_LINE = re.compile(rf"^\(\s*{_SUFFIX_STATE}\s*\)(?=[.,!?\s]|$)")
_VALID_SUFFIX = (
    r"(?i:" + "|".join(sorted(US_AND_CA_JURISDICTION_CODES)) + "|" + _STATE_NAME_ALT + ")"
)
# A second word can belong to a spaced plate. Digits or an explicit state suffix
# distinguish it from ordinary words following the number.
_SEGMENT_WITH_DIGIT = r"(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]+"
_PROSE_TOKEN = (
    r"(?:[A-Z0-9]+|" + _SEGMENT_WITH_DIGIT + r")"
    r"(?:-[A-Za-z0-9-]+|\s+"
    + _SEGMENT_WITH_DIGIT
    + r"|\s+[A-Z]+(?=\s*\(\s*"
    + _VALID_SUFFIX
    + r"\s*\)))?"
    r"(?![A-Za-z0-9-])"
)
_PROSE_LABELED_PLATE = re.compile(
    r"\b(?:(?P<pre>" + _PREFIX_STATE + r")[ \t]+)?"
    r"(?i:(?:vehicle\s+)?license\s+plate(?:\s+number)?|(?:my|vehicle)\s+plate|plate\s+number)"
    r"(?:\s*[:#]\s*|\s+(?:(?i:is|was|reads?)\s+)?)"
    r"(?P<plate>" + _PROSE_TOKEN + r")"
    r"(?:\s*\(\s*(?P<paren>" + _SUFFIX_STATE + r")\s*\)"
    r"|(?i:\s*,\s*registered\s+in\s+)(?P<registered>(?i:" + _STATE_NAME_ALT + r")\b|[A-Za-z]+))?"
    r"(?=$|[\s.,;:)!?])"
)


def _clean_line(raw: str) -> str:
    return " ".join(raw.replace("\xa0", " ").translate(_DASH_TRANSLATION).split())


def normalize_plate_token(raw: str | None) -> str | None:
    """Canonicalize an alphanumeric plate token (collapsing spaces and hyphens)."""
    if not isinstance(raw, str):
        return None
    stripped = _clean_line(raw)
    if not stripped or len(stripped) > 16:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]*[A-Za-z0-9]|[A-Za-z0-9]", stripped):
        return None
    compact = re.sub(r"[ -]+", "", stripped).upper()
    return compact if 1 <= len(compact) <= 10 else None


def normalize_state_code(raw: str | None) -> str | None:
    """Canonicalize a 2-letter US/CA jurisdiction code or full US state name."""
    if not isinstance(raw, str):
        return None
    cleaned = _clean_line(raw)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if upper in US_AND_CA_JURISDICTION_CODES:
        return upper
    return STATE_NAMES_TO_CODE.get(cleaned.casefold())


def classify_identifier_item(item: str, quote: str = "") -> IdentifierKind | None:
    """Return the identifier kind when `item` names a written plate/state requirement."""
    cleaned = _TRAILING_CLAUSE.sub("", _clean_line(item).strip(" ."))
    if (
        not cleaned
        or _DOCUMENT_OR_COPY_NOISE.search(cleaned)
        or _DOCUMENT_OR_COPY_NOISE.search(quote)
    ):
        return None
    if _COMBINED_ITEM.fullmatch(cleaned):
        return IdentifierKind.PLATE_AND_STATE
    if _PLATE_ITEM.fullmatch(cleaned):
        return IdentifierKind.PLATE_NUMBER
    if _STATE_ITEM.fullmatch(cleaned):
        if cleaned.casefold() in {"state", "the state", "your state"} and not (
            _VEHICLE_QUOTE_CONTEXT.search(quote)
        ):
            return None
        return IdentifierKind.PLATE_STATE
    return None


def expected_vehicle_identifiers(plan: PacketPlan) -> ExpectedVehicleIdentifiers | None:
    """Bind expected plate and state strictly from `plan.customer_fields`."""
    fields = dict(plan.customer_fields)
    if len(fields) != len(plan.customer_fields):
        return None

    plate_values = {
        fields[k].strip() for k in ("plate", "license_plate") if k in fields and fields[k].strip()
    }
    if len(plate_values) > 1:
        return None
    raw_plate = next(iter(plate_values), None)

    state_values = {
        fields[k].strip()
        for k in ("plate_state", "license_plate_state")
        if k in fields and fields[k].strip()
    }
    if len(state_values) > 1:
        return None
    raw_state = next(iter(state_values), None)

    return ExpectedVehicleIdentifiers(plate=raw_plate, plate_state=raw_state)


def _parse_combined_rhs(rhs: str, *, label_demands_state: bool) -> LabeledPlateSpan:
    cleaned = _clean_line(rhs).rstrip(".")
    if not cleaned or _POSTAL_CITY_STATE_ZIP.search(cleaned):
        return LabeledPlateSpan(None, None, malformed_plate=True, malformed_state=True)

    m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9 -]{0,14})\s*\(\s*([^()]{1,25})\s*\)", cleaned)
    if m:
        plate = normalize_plate_token(m.group(1))
        state = normalize_state_code(m.group(2))
        return LabeledPlateSpan(
            plate_token=plate,
            state_code=state,
            malformed_plate=plate is None,
            malformed_state=state is None,
        )

    m = re.fullmatch(
        r"([A-Za-z0-9][A-Za-z0-9 -]{0,14})\s*(?:,\s*registered in\s+|[,/|]\s*)([A-Za-z ]{2,25})",
        cleaned,
        re.I,
    )
    if m:
        plate = normalize_plate_token(m.group(1))
        state = normalize_state_code(m.group(2))
        return LabeledPlateSpan(
            plate_token=plate,
            state_code=state,
            malformed_plate=plate is None,
            malformed_state=state is None,
        )

    m = re.fullmatch(r"([A-Za-z]{2})\s*[-/:|]\s*([A-Za-z0-9][A-Za-z0-9 -]{0,14})", cleaned)
    if m and normalize_state_code(m.group(1)) is not None:
        state = normalize_state_code(m.group(1))
        plate = normalize_plate_token(m.group(2))
        return LabeledPlateSpan(
            plate_token=plate,
            state_code=state,
            malformed_plate=plate is None,
            malformed_state=False,
        )

    plate = normalize_plate_token(cleaned)
    return LabeledPlateSpan(
        plate_token=plate,
        state_code=None,
        malformed_plate=plate is None,
        malformed_state=label_demands_state or plate is None,
    )


def _prose_spans(text: str) -> list[LabeledPlateSpan]:
    spans: list[LabeledPlateSpan] = []
    for match in _PROSE_LABELED_PLATE.finditer(text):
        pre = match["pre"]
        prefix = normalize_state_code(pre)
        # Common two-letter words are ambiguous within an all-caps sentence.
        # At the start of a statement "OR LICENSE PLATE ..." identifies Oregon.
        if pre in {"IN", "ON", "AS", "OR", "ME", "HI", "OK", "OH", "ID"}:
            before = text[: match.start()].rstrip(" \t")
            if (
                before
                and before[-1] not in ".?!:\n"
                and not re.search(r"\b(?:my|our|your|an?|the|his|her|its|their)$", before, re.I)
            ):
                prefix = None
        post = match["paren"] or match["registered"]
        suffix = normalize_state_code(post)
        plate = normalize_plate_token(match["plate"])
        # A count in a new paragraph cannot establish a plate. A state attached to
        # the number disambiguates a continued label, even after a blank line.
        before_plate = text[match.start() : match.start("plate")]
        trailing_words = text[match.end() :].partition("\n")[0].strip(" .")
        ambiguous = (
            "\n" in before_plate
            and not prefix
            and not post
            and ("\n\n" in before_plate or bool(trailing_words))
        )
        spans.append(
            LabeledPlateSpan(
                plate,
                suffix or prefix,
                malformed_plate=plate is None or ambiguous,
                malformed_state=(post is not None and suffix is None)
                or (prefix is not None and suffix is not None and prefix != suffix),
            )
        )
    return spans


def extract_labeled_plate_spans(letter_page_texts: Sequence[str]) -> tuple[LabeledPlateSpan, ...]:
    """Read structured fields and adjacent prose lines inside the verified letter."""
    spans: list[LabeledPlateSpan] = []
    for text in letter_page_texts:
        prose: list[str] = []
        lines = [_clean_line(raw) for raw in text.splitlines()]
        index = 0
        while index < len(lines):
            line = lines[index]
            index += 1
            state = _DEDICATED_STATE_LABEL.fullmatch(line)
            field = state or _COMBINED_OR_PLATE_LABEL.fullmatch(line)
            if field is not None:
                spans.extend(_prose_spans("\n".join(prose)))
                prose.clear()
                rhs = field["rhs"].strip()
                while index < len(lines) and not lines[index]:
                    index += 1
                if index < len(lines):
                    following = lines[index]
                    if not rhs and not re.search(r"[:#]", following):
                        rhs = following
                        index += 1
                    elif state is None and (suffix := _STATE_ON_NEXT_LINE.match(following)):
                        rhs += " " + suffix.group()
                        # Keep any following text for independent conflict checks.
                        lines[index] = following[suffix.end() :].lstrip()
                if state:
                    code = normalize_state_code(rhs.rstrip("."))
                    spans.append(LabeledPlateSpan(None, code, malformed_state=code is None))
                else:
                    spans.append(
                        _parse_combined_rhs(
                            rhs, label_demands_state="state" in field["label"].casefold()
                        )
                    )
            elif _POSTAL_CITY_STATE_ZIP.fullmatch(line) or re.match(r"^[\w -]+:", line):
                spans.extend(_prose_spans("\n".join(prose)))
                prose.clear()
                spans.extend(_prose_spans(line))
            else:
                prose.append(line)
        spans.extend(_prose_spans("\n".join(prose)))
    return tuple(spans)


def plan_identifier_fulfilled(
    kind: IdentifierKind,
    plan: PacketPlan,
    *,
    letter_page_texts: Sequence[str] | None,
) -> bool:
    """Check pre-freeze readiness (`letter_page_texts=None`) or frozen PDF compliance."""
    bound = expected_vehicle_identifiers(plan)
    if bound is None:
        return False

    exp_plate = normalize_plate_token(bound.plate)
    exp_state = normalize_state_code(bound.plate_state)

    needs_plate = kind in (IdentifierKind.PLATE_NUMBER, IdentifierKind.PLATE_AND_STATE)
    needs_state = kind in (IdentifierKind.PLATE_STATE, IdentifierKind.PLATE_AND_STATE)

    if needs_plate and exp_plate is None:
        return False
    if needs_state and exp_state is None:
        return False

    if letter_page_texts is None:
        return True

    spans = extract_labeled_plate_spans(letter_page_texts)
    observed_plates = [s.plate_token for s in spans if s.plate_token is not None]
    observed_states = [s.state_code for s in spans if s.state_code is not None]

    if needs_plate and (
        any(s.malformed_plate for s in spans)
        or not observed_plates
        or any(p != exp_plate for p in observed_plates)
    ):
        return False

    if not needs_state:
        return True
    return (
        bool(observed_states)
        and not any(s.malformed_state for s in spans)
        and all(state == exp_state for state in observed_states)
    )
