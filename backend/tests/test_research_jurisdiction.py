from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import date, timedelta

import pytest

from parkingbreaker.agencies import IssuerType
from parkingbreaker.research.jurisdiction import (
    DotGovRecord,
    DotGovSnapshot,
    IssuerCandidate,
    IssuerQuery,
    MatchOutcome,
    issuer_candidates,
    match_outcome,
    parse_dotgov_csv,
    snapshot_is_current,
)

HEADER = (
    "Domain name,Domain type,Organization name,Suborganization name,City,State,"
    "Security contact email\n"
)
SNAPSHOT_DATE = date(2026, 10, 13)


def snapshot(rows: str) -> DotGovSnapshot:
    return parse_dotgov_csv(HEADER + rows, SNAPSHOT_DATE)


def test_snapshot_hash_is_stable_and_identifies_exact_text() -> None:
    text = HEADER + "orange.gov,City,City of Orange,,Orange,CA,\n"
    original = parse_dotgov_csv(text, SNAPSHOT_DATE)
    assert original.content_sha256 == parse_dotgov_csv(text, SNAPSHOT_DATE).content_sha256
    changed = parse_dotgov_csv(text.replace("City of Orange", "City of orange"), SNAPSHOT_DATE)
    assert original.content_sha256 != changed.content_sha256


@pytest.mark.parametrize(("age_days", "current"), [(0, True), (31, True), (32, False), (-1, False)])
def test_snapshot_freshness_boundaries(age_days: int, current: bool) -> None:
    assert snapshot_is_current(snapshot(""), SNAPSHOT_DATE + timedelta(days=age_days)) is current


def test_snapshot_freshness_uses_caller_supplied_max_age() -> None:
    original = snapshot("")
    assert snapshot_is_current(original, SNAPSHOT_DATE, max_age_days=0)
    assert not snapshot_is_current(original, SNAPSHOT_DATE + timedelta(days=1), max_age_days=0)


def candidates(
    printed: str, rows: str, city: str = "Orange", state: str = "CA"
) -> tuple[IssuerCandidate, ...]:
    return issuer_candidates(IssuerQuery(printed, city, state), snapshot(rows))


def test_parse_real_header_normalises_fields_and_drops_email() -> None:
    result = snapshot(
        'EXAMPLE.GOV,City,"City of Example, California",,Example,ca,secret@example.gov\r\n'
    )
    assert result.snapshot_date == SNAPSHOT_DATE
    assert result.records == (
        DotGovRecord("example.gov", "City", "City of Example, California", None, "Example", "CA"),
    )
    assert result.skipped_counts == {}
    assert {field.name for field in fields(DotGovRecord)} == {
        "domain",
        "domain_type",
        "organization",
        "suborganization",
        "city",
        "state",
    }
    assert "secret@example.gov" not in repr(asdict(result))


@pytest.mark.parametrize(
    ("header", "missing", "extra"),
    [
        ("", "Domain name", "[]"),
        (HEADER.replace("State,", "Province,"), "State", "Province"),
        (HEADER.replace("Security contact email", "Email"), "Security contact email", "Email"),
        (HEADER.rstrip() + ",Unexpected\n", "[]", "Unexpected"),
        (HEADER.replace("State,", "City,"), "State", "City"),
    ],
)
def test_exact_header_reports_missing_and_extra_columns(
    header: str, missing: str, extra: str
) -> None:
    with pytest.raises(ValueError, match="DotGov header") as exc:
        parse_dotgov_csv(header, SNAPSHOT_DATE)
    message = str(exc.value)
    assert missing in message.split("; extra columns")[0]
    assert extra in message.split("; extra columns")[1]


def test_reordered_header_is_rejected_even_with_same_columns() -> None:
    with pytest.raises(ValueError, match=r"missing columns \[\]; extra columns \[\].*order"):
        parse_dotgov_csv(HEADER.replace("City,State", "State,City"), SNAPSHOT_DATE)


@pytest.mark.parametrize(
    ("domain_type", "issuer_type"),
    [
        ("City", IssuerType.MUNICIPAL),
        ("County", IssuerType.COUNTY),
        ("State or territory", IssuerType.STATE_AGENCY),
        ("Special district", IssuerType.AUTHORITY),
        ("Interstate", IssuerType.AUTHORITY),
        ("School district", IssuerType.PUBLIC_EDUCATION),
        ("Tribal", IssuerType.TRIBAL),
        ("Federal - Executive", IssuerType.FEDERAL),
        ("Federal - Judicial", IssuerType.FEDERAL),
        ("Federal - Legislative", IssuerType.FEDERAL),
        ("Federal", IssuerType.FEDERAL),
    ],
)
def test_domain_type_table(domain_type: str, issuer_type: IssuerType) -> None:
    rows = f"example.gov,{domain_type},Federal Example,,Orange,CA,\n"
    result = candidates("Federal Example", rows)
    assert len(result) == 1
    assert result[0].record.domain_type == domain_type
    assert result[0].issuer_type is issuer_type


def test_elections_unknown_types_and_invalid_domains_are_counted() -> None:
    result = snapshot(
        "elections.gov,County - Election,Orange County Elections,,Orange,CA,private@example.gov\n"
        "cityvote.gov,City - Election,Orange City Elections,,Orange,CA,\n"
        "mystery.gov,New type,Unknown Organization,,Orange,CA,\n"
        "https://orange.gov,City,City of Orange,,Orange,CA,\n"
        "orange.gov,City,City of Orange,,Orange,CA,\n"
    )
    assert [record.domain for record in result.records] == ["orange.gov"]
    assert result.skipped_counts == {
        "election_type": 2,
        "unknown_type": 1,
        "invalid_domain": 1,
    }


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "https://orange.gov",
        "orange.gov/path",
        "orange.gov:443",
        "*.orange.gov",
        "bad_domain.gov",
    ],
)
def test_invalid_domain_is_skipped(domain: str) -> None:
    result = snapshot(f"{domain},City,City of Orange,,Orange,CA,\n")
    assert result.records == ()
    assert result.skipped_counts == {"invalid_domain": 1}


def test_wrong_row_width_is_counted_without_retaining_email() -> None:
    result = snapshot(
        "short.gov,City,City of Orange\n"
        "extra.gov,City,City of Orange,,Orange,CA,secret@example.gov,unexpected\n"
    )
    assert result.records == ()
    assert result.skipped_counts == {"malformed_row": 2}
    assert "secret@example.gov" not in repr(result)


def test_invalid_csv_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="invalid DotGov CSV"):
        snapshot('orange.gov,City,"unterminated organization\n')


def test_blank_federal_state_and_raw_domain_type_are_preserved() -> None:
    result = snapshot("nps.gov, Federal - Executive ,National Park Service,,Washington,,\n")
    assert result.records[0].state is None
    assert result.records[0].suborganization is None
    assert result.records[0].domain_type == " Federal - Executive "
    assert issuer_candidates(IssuerQuery("National Park Service", "Orange", "CA"), result)


@pytest.mark.parametrize(
    ("printed", "city", "state"),
    [
        (" ", "Orange", "CA"),
        ("Orange", " ", "CA"),
        ("Orange", "Orange", "ca"),
        ("Orange", "Orange", "California"),
        ("Orange", "Orange", "C1"),
        ("Orange", "Orange", "CA\n"),
        ("Orange", "Orange", "US-FED"),
    ],
)
def test_query_requires_nonblank_names_and_uppercase_state(
    printed: str, city: str, state: str
) -> None:
    with pytest.raises(ValueError, match=r"blank|two-letter uppercase"):
        IssuerQuery(printed, city, state)


def test_query_keeps_verbatim_notice_text() -> None:
    assert IssuerQuery("  DEPT. of Finance  ", "Orange", "CA").printed_issuer == (
        "  DEPT. of Finance  "
    )


@pytest.mark.parametrize("state", ["MD", "SD"])
def test_same_city_name_is_filtered_by_state(state: str) -> None:
    result = candidates(
        "City of Aberdeen",
        "aberdeenmd.gov,City,City of Aberdeen,,Aberdeen,MD,\n"
        "aberdeensd.gov,City,City of Aberdeen,,Aberdeen,SD,\n",
        city="Aberdeen",
        state=state,
    )
    assert [candidate.record.domain for candidate in result] == [f"aberdeen{state.lower()}.gov"]
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_nonfederal_blank_state_is_ineligible() -> None:
    assert not candidates("City of Orange", "orange.gov,City,City of Orange,,Orange,,\n")


def test_nyc_owner_name_without_name_overlap_falls_back_to_search() -> None:
    result = candidates(
        "City of New York Department of Finance",
        "nyc.gov,City,The NYC Office of Technology and Innovation,,Brooklyn,NY,\n",
        city="New York",
        state="NY",
    )
    # No NYC alias is invented, and geography alone cannot establish name overlap.
    # NONE leads to search, never refusal or a private-issuer classification.
    assert result == ()
    assert match_outcome(result) is MatchOutcome.NONE


def test_airport_beats_city_even_when_directory_type_is_city() -> None:
    result = candidates(
        "San Francisco International Airport",
        "sfgov.gov,City,City and County of San Francisco,,San Francisco,CA,\n"
        "flysfo.gov,City,City and County of San Francisco,San Francisco International Airport,"
        "San Francisco,CA,\n",
        city="San Francisco",
    )
    assert [candidate.record.domain for candidate in result] == ["flysfo.gov", "sfgov.gov"]
    assert [candidate.score for candidate in result] == [29, 19]
    assert all("'airport' disagrees with MUNICIPAL" in c.reasons[-1] for c in result)
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_county_signal_penalises_city_without_excluding_it() -> None:
    result = candidates(
        "Orange County",
        "cityorange.gov,City,City of Orange,,Orange,CA,\n"
        "orangecounty.gov,County,County of Orange,,Orange,CA,\n",
    )
    assert [candidate.record.domain for candidate in result] == [
        "orangecounty.gov",
        "cityorange.gov",
    ]
    assert [candidate.score for candidate in result] == [26, 9]
    assert result[1].reasons[-1] == "printed issuer kind 'county' disagrees with MUNICIPAL"


def test_special_district_transit_authority() -> None:
    result = candidates(
        "Orange Transit Auth.",
        "orangetransit.gov,Special district,Orange Transit Authority,,Orange,CA,\n",
    )
    assert result[0].issuer_type is IssuerType.AUTHORITY
    assert result[0].score == 29
    assert result[0].reasons == (
        "organization/suborganization share tokens: orange",
        "printed issuer covers all distinctive organization/suborganization tokens: orange",
        "record city matches customer city",
        "printed issuer kind 'authority' agrees with AUTHORITY",
        "printed issuer kind 'transit' agrees with AUTHORITY",
    )


def test_tribal_candidate_keeps_nation_signal() -> None:
    result = candidates(
        "Cherokee Nation",
        "cherokee.gov,Tribal,Cherokee Nation,,Tahlequah,OK,\n",
        city="Tahlequah",
        state="OK",
    )
    assert result[0].issuer_type is IssuerType.TRIBAL
    assert result[0].score == 26
    assert match_outcome(result) is MatchOutcome.SINGLE


@pytest.mark.parametrize("state", ["CA", "DC"])
@pytest.mark.parametrize(
    ("printed", "eligible"),
    [("National Park Service", True), ("Park Service", False), ("Washington", False)],
)
def test_federal_shared_signal_required_even_in_same_state(
    state: str, printed: str, eligible: bool
) -> None:
    result = candidates(
        printed,
        "nps.gov,Federal - Executive,National Park Service,,Washington,DC,\n",
        city="Washington",
        state=state,
    )
    assert bool(result) is eligible
    if eligible:
        assert result[0].issuer_type is IssuerType.FEDERAL


def test_federal_signal_must_be_shared_with_this_record() -> None:
    assert not candidates(
        "Federal Orange Office",
        "nps.gov,Federal,National Park Service,Orange Office,Washington,,\n",
    )


def test_district_court_eligible_across_states() -> None:
    result = candidates(
        "U.S. District Court Violation Notice",
        "uscourts.gov,Federal - Judicial,United States District Court,,Washington,DC,\n",
    )
    assert result[0].issuer_type is IssuerType.FEDERAL
    assert result[0].score == 20
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_private_looking_issuer_and_city_bonus_alone_yield_none() -> None:
    result = candidates("ABC Parking LLC", "orange.gov,City,City of Orange,,Orange,CA,\n")
    assert result == ()
    assert match_outcome(result) is MatchOutcome.NONE


@pytest.mark.parametrize(
    ("printed", "expanded", "domain_type", "score"),
    [
        ("ORANGE, DEPT.", "Orange Department", "State or territory", 25),
        ("Orange Univ.", "Orange University", "School district", 26),
        ("Orange Auth.", "Orange Authority", "Special district", 26),
        ("Orange Twp.", "Orange Township", "City", 26),
        ("Orange Co.", "Orange County", "County", 26),
    ],
)
def test_small_abbreviation_table(
    printed: str, expanded: str, domain_type: str, score: int
) -> None:
    rows = f"orange.gov,{domain_type},{expanded},,Orange,CA,\n"
    assert candidates(printed, rows) == candidates(expanded, rows)
    assert candidates(printed, rows)[0].score == score


@pytest.mark.parametrize("suffix", ["LLC", "Ltd.", "Company", "Inc.", "Corporation"])
def test_co_before_company_suffix_does_not_expand_to_county(suffix: str) -> None:
    rows = "orange.gov,County,Orange County,,Orange,CA,\n"
    result = candidates(f"Orange Co. {suffix}", rows)
    assert result == candidates(f"Orange Co {suffix}", rows)
    assert result[0].score == 23
    assert "county" not in result[0].reasons[0]


@pytest.mark.parametrize("prefix", ["City of", "Town of", "Village of"])
def test_only_fillers_are_dropped_and_kind_word_is_kept(prefix: str) -> None:
    result = candidates(
        f"  The {prefix} ORANGE  ",
        f"orange.gov,City,{prefix} Orange,,Orange,CA,\n",
    )
    assert result[0].score == 26
    assert result[0].reasons[0] == "organization/suborganization share tokens: orange"
    assert result[0].reasons[-1] == (
        f"printed issuer kind '{prefix.split()[0].lower()}' agrees with MUNICIPAL"
    )
    assert "the" not in result[0].reasons[0]


@pytest.mark.parametrize(
    "word",
    [
        "district",
        "police",
        "park",
        "parks",
        "court",
    ],
)
def test_distinguishing_words_survive_normalisation(word: str) -> None:
    result = candidates(f"Orange {word}", f"orange.gov,City,Orange {word},,Orange,CA,\n")
    assert len(result) == 1
    assert result[0].reasons[0] == (
        f"organization/suborganization share tokens: {', '.join(sorted(('orange', word)))}"
    )


def test_case_punctuation_whitespace_and_city_normalisation() -> None:
    result = candidates(
        " ORANGE---Transit,  Transit AUTH. ",
        'orange.gov,Special district,Orange Transit Authority,,"  ORANGE!  ",CA,\n',
    )
    # Repeated tokens count once, and the exact normalised city earns five points.
    assert result[0].score == 29


def test_matching_city_bonus_is_only_five_points() -> None:
    rows = (
        "other.gov,City,City of Orange North,,Other,CA,\n"
        "orange.gov,City,City of Orange South,,Orange,CA,\n"
    )
    result = candidates("City of Orange", rows)
    assert [candidate.score for candidate in result] == [18, 13]
    assert match_outcome(result) is MatchOutcome.AMBIGUOUS


def test_ties_are_ambiguous_and_domain_order_is_deterministic() -> None:
    rows = (
        "zorange.gov,City,City of Orange North,,Orange,CA,\n"
        "aorange.gov,City,City of Orange South,,Orange,CA,\n"
    )
    query = IssuerQuery("City of Orange", "Orange", "CA")
    original = snapshot(rows)
    result = issuer_candidates(query, original)
    assert [candidate.record.domain for candidate in result] == ["aorange.gov", "zorange.gov"]
    assert match_outcome(result) is MatchOutcome.AMBIGUOUS
    assert issuer_candidates(query, replace(original, records=original.records[::-1])) == result
    assert issuer_candidates(query, original) == result


@pytest.mark.parametrize(
    ("top", "second", "outcome"),
    [
        (14, None, MatchOutcome.NONE),
        (15, None, MatchOutcome.SINGLE),
        (20, 15, MatchOutcome.AMBIGUOUS),
        (21, 15, MatchOutcome.SINGLE),
        (15, 15, MatchOutcome.AMBIGUOUS),
        (14, 0, MatchOutcome.NONE),
        (14, 14, MatchOutcome.NONE),
        (15, 9, MatchOutcome.SINGLE),
    ],
)
def test_minimum_and_margin_boundaries(top: int, second: int | None, outcome: MatchOutcome) -> None:
    candidate = candidates("City of Orange", "orange.gov,City,City of Orange,,Orange,CA,\n")[0]
    result: tuple[IssuerCandidate, ...] = (replace(candidate, score=top),)
    if second is not None:
        result += (
            replace(
                candidate,
                record=replace(candidate.record, domain="other.gov", organization="Other Orange"),
                domains=("other.gov",),
                score=second,
            ),
        )
    assert match_outcome(result) is outcome
    assert match_outcome(result[::-1]) is outcome


def test_one_weak_candidate_is_none() -> None:
    result = candidates("Orange", "orange.gov,City,City of Orange Heights,,Other,CA,\n")
    assert result[0].score == 10
    assert match_outcome(result) is MatchOutcome.NONE


@pytest.mark.parametrize(
    "kind",
    [
        "city",
        "town",
        "township",
        "borough",
        "village",
        "county",
        "parish",
        "university",
        "college",
        "school",
        "transit",
        "airport",
        "port",
        "authority",
        "tribe",
        "tribal",
        "nation",
        "federal",
    ],
)
def test_kind_words_alone_do_not_count_as_name_overlap(kind: str) -> None:
    result = candidates(
        f"{kind} Seattle",
        f"other.gov,City,{kind} Other,,Seattle,WA,\n",
        city="Seattle",
        state="WA",
    )
    assert result == ()


def test_city_of_seattle_is_single_over_other_washington_cities() -> None:
    result = candidates(
        "City of Seattle",
        "seattle.gov,City,City of Seattle,,Seattle,WA,\n"
        "tacoma.gov,City,City of Tacoma,,Tacoma,WA,\n"
        "spokane.gov,City,City of Spokane,,Spokane,WA,\n",
        city="Seattle",
        state="WA",
    )
    assert [candidate.record.domain for candidate in result] == ["seattle.gov"]
    assert result[0].score == 26
    assert result[0].reasons[-1] == "printed issuer kind 'city' agrees with MUNICIPAL"
    assert match_outcome(result) is MatchOutcome.SINGLE


@pytest.mark.parametrize("domain_type", ["City", "County", "State or territory"])
def test_seattle_municipal_court_is_not_penalised_as_federal(domain_type: str) -> None:
    result = candidates(
        "Seattle Municipal Court",
        f"seattle.gov,{domain_type},City of Seattle,,Seattle,WA,\n",
        city="Seattle",
        state="WA",
    )
    assert result[0].score == 23
    assert not any("disagrees" in reason for reason in result[0].reasons)
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_national_park_service_acronym_breaks_shared_interior_name_tie() -> None:
    result = candidates(
        "National Park Service",
        "denali.gov,Federal - Executive,U.S. Department of the Interior,National Park Service,"
        "Denali Park,AK,\n"
        "nps.gov,Federal - Executive,U.S. Department of the Interior,National Park Service,"
        "Washington,DC,\n"
        "npshistory.gov,Federal - Executive,U.S. Department of the Interior,National Park Service,"
        "Washington,DC,\n",
    )
    assert [candidate.record.domain for candidate in result] == [
        "nps.gov",
        "denali.gov",
    ]
    assert result[0].domains == ("nps.gov", "npshistory.gov")
    assert [candidate.score for candidate in result] == [40, 30]
    assert "domain first label matches printed issuer acronym: nps" in result[0].reasons
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_berkeley_city_is_too_weak_for_university_of_california_berkeley() -> None:
    result = candidates(
        "University of California Berkeley",
        "berkeleyca.gov,City,City of Berkeley,,Berkeley,CA,\n",
        city="Berkeley",
    )
    assert result[0].score == 9
    assert result[0].reasons[-1] == "printed issuer kind 'university' disagrees with MUNICIPAL"
    assert match_outcome(result) is MatchOutcome.NONE


@pytest.mark.parametrize("domain", ["sfmta.gov", "sf-mta.gov"])
def test_sfmta_acronym_provides_overlap_without_shared_name_tokens(domain: str) -> None:
    result = candidates(
        "The San Francisco Municipal Transportation Agency",
        f"{domain},City,City and County of SF,,San Francisco,CA,\n",
        city="San Francisco",
    )
    assert result[0].score == 15
    assert result[0].reasons == (
        "domain first label matches printed issuer acronym: sfmta",
        "record city matches customer city",
    )
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_acronym_keeps_kind_words_and_drops_only_fillers() -> None:
    result = candidates(
        "The University of California Berkeley",
        "ucb.gov,School district,Public Education Office,,Berkeley,CA,\n",
        city="Berkeley",
    )
    assert result[0].score == 18
    assert result[0].reasons[0] == "domain first label matches printed issuer acronym: ucb"


def test_acronym_does_not_override_state_or_federal_eligibility() -> None:
    assert not candidates(
        "San Francisco Municipal Transportation Agency",
        "sfmta.gov,City,City and County of SF,,San Francisco,CA,\n",
        state="WA",
    )
    assert not candidates(
        "National Park Service",
        "nps.gov,Federal - Executive,Department of the Interior,,Washington,DC,\n",
    )


def test_boston_domains_are_one_candidate_with_full_name_coverage() -> None:
    result = candidates(
        "City of Boston Transportation Department",
        "cityofboston.gov,City,City of Boston,,Boston,MA,\n"
        "boston.gov,City,City of Boston,,Boston,MA,\n",
        city="Boston",
        state="MA",
    )
    assert len(result) == 1
    assert result[0].domains == ("boston.gov", "cityofboston.gov")
    assert result[0].record.domain == "boston.gov"
    assert result[0].score == 26
    assert (
        "printed issuer covers all distinctive organization/suborganization tokens: boston"
        in result[0].reasons
    )
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_san_diego_domains_beat_distinct_retirement_system() -> None:
    result = candidates(
        "City of San Diego",
        "sannet.gov,City,The City of San Diego,,San Diego,CA,\n"
        "sdcers.gov,City,San Diego City Employees' Retirement System (SDCERS),,San Diego,CA,\n"
        "sandiego.gov,City,The City of San Diego,,San Diego,CA,\n",
        city="San Diego",
    )
    assert [candidate.domains for candidate in result] == [
        ("sandiego.gov", "sannet.gov"),
        ("sdcers.gov",),
    ]
    assert result[0].record.domain == "sandiego.gov"
    assert [candidate.score for candidate in result] == [36, 28]
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_la_city_beats_water_and_power_and_police_for_transportation() -> None:
    result = candidates(
        "City of Los Angeles Department of Transportation",
        "ladwp.gov,City,Los Angeles Department of Water and Power,,Los Angeles,CA,\n"
        "lapd.gov,City,Los Angeles Police Department,,Los Angeles,CA,\n"
        "lacity.gov,City,City of Los Angeles,,Los Angeles,CA,\n",
        city="Los Angeles",
    )
    assert [candidate.record.domain for candidate in result] == [
        "lacity.gov",
        "ladwp.gov",
        "lapd.gov",
    ]
    assert [candidate.score for candidate in result] == [36, 30, 30]
    assert (
        "organization/suborganization share administrative tokens: department" in result[1].reasons
    )
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_sacramento_county_beats_airports_and_law_library() -> None:
    result = candidates(
        "County of Sacramento",
        "flysmf.gov,County,Sacramento County Department of Airports,,Sacramento,CA,\n"
        "saclaw.gov,County,Sacramento County Public Law Library,,Sacramento,CA,\n"
        "saccounty.gov,County,County of Sacramento,,Sacramento,CA,\n",
        city="Sacramento",
    )
    assert [candidate.record.domain for candidate in result] == [
        "saccounty.gov",
        "flysmf.gov",
        "saclaw.gov",
    ]
    assert [candidate.score for candidate in result] == [26, 18, 18]
    assert match_outcome(result) is MatchOutcome.SINGLE


def test_cherry_hill_township_beats_police_with_directory_typo() -> None:
    result = candidates(
        "Township of Cherry Hill",
        "chpdnj.gov,City,Cherry Hill Police Deparment,,Cherry Hill,NJ,\n"
        "chnj.gov,City,Township of Cherry Hill,,Cherry Hill,NJ,\n",
        city="Cherry Hill",
        state="NJ",
    )
    assert [candidate.record.domain for candidate in result] == ["chnj.gov", "chpdnj.gov"]
    assert [candidate.score for candidate in result] == [36, 28]
    assert match_outcome(result) is MatchOutcome.SINGLE


@pytest.mark.parametrize(
    "word",
    ["department", "office", "division", "bureau", "services", "agency", "administration"],
)
def test_administrative_overlap_alone_cannot_make_a_candidate(word: str) -> None:
    result = candidates(word, f"orange.gov,City,Orange {word},,Orange,CA,\n")
    assert result == ()
    assert match_outcome(result) is MatchOutcome.NONE


@pytest.mark.parametrize(
    "word",
    ["department", "office", "division", "bureau", "services", "agency", "administration"],
)
def test_administrative_tokens_earn_two_points_once(word: str) -> None:
    result = candidates(
        f"Orange {word} {word}",
        f"orange.gov,City,Orange Heights {word},,Orange,CA,\n",
    )
    assert result[0].score == 17
    assert result[0].reasons == (
        "organization/suborganization share tokens: orange",
        f"organization/suborganization share administrative tokens: {word}",
        "record city matches customer city",
    )


def test_acronym_can_use_administrative_words_without_vacuous_coverage_bonus() -> None:
    result = candidates("Office Agency", "oa.gov,City,Office Agency,,Orange,CA,\n")
    assert result[0].score == 19
    assert result[0].reasons == (
        "organization/suborganization share administrative tokens: agency, office",
        "domain first label matches printed issuer acronym: oa",
        "record city matches customer city",
    )


def test_group_normalisation_and_member_bonuses_are_order_independent() -> None:
    original = snapshot(
        "sfmta.gov,City,City and County of SF,,San Francisco,CA,\n"
        "acme.gov,City,The CITY and County of SF!,,Other,CA,\n"
        "sf-mta.gov,City,City and County of SF,,Other,CA,\n"
        "sfmta.gov,City,City and County of SF,,San Francisco,CA,\n"
    )
    query = IssuerQuery("San Francisco Municipal Transportation Agency", "San Francisco", "CA")
    result = issuer_candidates(query, original)
    assert len(result) == 1
    assert result[0].record.domain == "acme.gov"
    assert result[0].record.city == "Other"
    assert result[0].domains == ("acme.gov", "sf-mta.gov", "sfmta.gov")
    assert result[0].score == 15
    assert issuer_candidates(query, replace(original, records=original.records[::-1])) == result


def test_grouping_keeps_organization_and_suborganization_boundaries() -> None:
    result = candidates(
        "Orange North Division",
        "first.gov,City,Orange North,Division,Orange,CA,\n"
        "second.gov,City,Orange,North Division,Orange,CA,\n",
    )
    assert len(result) == 2
    assert [candidate.domains for candidate in result] == [("first.gov",), ("second.gov",)]
    assert match_outcome(result) is MatchOutcome.AMBIGUOUS


def test_grouping_keeps_issuer_types_distinct() -> None:
    result = candidates(
        "Orange",
        "first.gov,City,Orange,,Orange,CA,\nsecond.gov,County,Orange,,Orange,CA,\n",
    )
    assert len(result) == 2
    assert match_outcome(result) is MatchOutcome.AMBIGUOUS


def test_grouping_keeps_federal_record_states_distinct() -> None:
    result = candidates(
        "Federal Orange",
        "first.gov,Federal,Federal Orange,,Orange,CA,\n"
        "second.gov,Federal,Federal Orange,,Orange,WA,\n",
    )
    assert len(result) == 2
    assert match_outcome(result) is MatchOutcome.AMBIGUOUS


def test_match_outcome_compares_organizations_even_for_ungrouped_inputs() -> None:
    first = candidates("City of Orange", "first.gov,City,City of Orange,,Orange,CA,\n")[0]
    second = replace(
        first,
        record=replace(first.record, domain="second.gov", organization="The CITY of Orange!"),
        domains=("second.gov",),
    )
    assert match_outcome((first, second)) is MatchOutcome.SINGLE
    assert match_outcome((replace(first, score=14), second)) is MatchOutcome.SINGLE
    assert match_outcome((second, first)) is MatchOutcome.SINGLE
