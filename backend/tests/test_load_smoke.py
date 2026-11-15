from __future__ import annotations

import asyncio

from loadtest_flows import run_level


def test_five_concurrent_customers_get_one_letter_and_one_charge_each() -> None:
    report = asyncio.run(run_level(5, delay_ms=10))
    assert (report["completed"], report["failure_count"]) == (5, 0), report["failures"]
    assert (report["cases_with_letter"], report["letters_per_case_max"]) == (5, 1)
    assert (report["payments_captured"], report["captures_per_payment_max"]) == (5, 1)
    assert report["cross_customer_reads_allowed"] == 0
    assert report["cross_customer_non_404"] == 0
