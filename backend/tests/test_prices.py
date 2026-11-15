"""The price lives in one place: no payment rule repeats the amount as a literal."""

import ast
import re
from pathlib import Path

import pytest

from parkingbreaker.prices import CERTIFIED_CENTS, STANDARD_CENTS

SOURCE = Path(__file__).parents[1] / "src" / "parkingbreaker"


def _allowed_cost_rung_tokens(path: Path, source: str) -> set[tuple[int, int, int]]:
    """Allow only the named non-price rung, using AST UTF-8 byte columns."""
    if path != SOURCE / "settings_registry.py":
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    allowed = set[tuple[int, int, int]]()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Setting"
            and len(node.args) >= 3
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "ad_stop_loss_cents"
            and isinstance(node.args[2], ast.Tuple)
        ):
            continue
        for rung in node.args[2].elts:
            if (
                isinstance(rung, ast.Constant)
                and type(rung.value) is int
                and rung.value == 2500
                and rung.lineno == rung.end_lineno
                and rung.end_col_offset is not None
            ):
                allowed.add((rung.lineno, rung.col_offset, rung.end_col_offset))
    return allowed


def test_prices_are_positive_cents() -> None:
    assert CERTIFIED_CENTS > STANDARD_CENTS > 0


def test_no_price_literal_outside_prices_module() -> None:
    offenders = []
    for path in SOURCE.rglob("*.py"):
        if path.name in {"prices.py", "config_fakes.py"}:
            continue
        source = path.read_text()
        allowed = _allowed_cost_rung_tokens(path, source)
        for number, line in enumerate(source.splitlines(), start=1):
            for match in re.finditer(r"(?<![\w.])(?:1999|2499|2999|2500)(?![\w.])", line):
                token = (
                    number,
                    len(line[: match.start()].encode("utf-8")),
                    len(line[: match.end()].encode("utf-8")),
                )
                if token not in allowed:
                    offenders.append(f"{path.relative_to(SOURCE)}:{number}")
    assert offenders == []


@pytest.mark.parametrize(
    ("filename", "source", "should_fail"),
    [
        ("settings_registry.py", 'Setting("ad_stop_loss_cents", "cents", (0, 2500))', False),
        ("settings_registry.py", 'Setting("ad_stop_loss_cents", "cents", (1999, 2500))', True),
        ("settings_registry.py", 'Setting("ad_stop_loss_cents", "cents", (2999, 2500))', True),
        (
            "settings_registry.py",
            'Setting("ad_stop_loss_cents", "cents", (0, 2500)); SERVICE_PRICE = 2500',
            True,
        ),
        ("settings_registry.py", 'Setting("other_setting", "cents", (0, 2500))', True),
        ("other.py", 'Setting("ad_stop_loss_cents", "cents", (0, 2500))', True),
        ("settings_registry.py", 'Setting("ad_stop_loss_cents", "¢", (0, 2500))', False),
        (
            "settings_registry.py",
            'Setting("ad_stop_loss_cents", "cents", (0, 2500))  # 2500',
            True,
        ),
    ],
)
def test_price_guard_cost_rung_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    source: str,
    should_fail: bool,
) -> None:
    monkeypatch.setitem(globals(), "SOURCE", tmp_path)
    (tmp_path / filename).write_text(source + "\n")
    if should_fail:
        with pytest.raises(AssertionError):
            test_no_price_literal_outside_prices_module()
    else:
        test_no_price_literal_outside_prices_module()
