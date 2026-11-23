"""Offline lexical guard for customer-facing clerical and outcome copy."""

import ast
import html
import re
from pathlib import Path

import pytest

from parkingbreaker.appeal.proofread import BANNED_PHRASES

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "web/lib/legal.ts",
    "web/app/page.tsx",
    "backend/src/parkingbreaker/tracking_email.py",
    "backend/src/parkingbreaker/outcome_follow_up.py",
    *(str(p.relative_to(ROOT)) for p in sorted((ROOT / "support-system/apps-script").glob("*.gs"))),
)
# These exact disclosures deny legal advice or describe the existing service remedy.
APPROVED = {
    "web/lib/legal.ts": (
        "We are not a law firm, do not represent you, and do not provide legal advice.",
        "The correct-office guarantee does not cover a failure caused by your choice.",
    ),
    "backend/src/parkingbreaker/outcome_follow_up.py": (
        "We do not give legal advice and do not promise any outcome.",
    ),
}
BANNED = tuple((phrase, re.compile(rf"\b{re.escape(phrase)}\b", re.I)) for phrase in BANNED_PHRASES)


def static_copy(source: str, suffix: str) -> str:
    if suffix == ".py":
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and ast.get_docstring(node) is not None
            and isinstance(node.body[0], ast.Expr)
        }
        return "\n.\n".join(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        )
    source = re.sub(r"(?m)^[ \t]*//[^\n]*", "", source)
    # Strip the TypeScript return type and the JS operator used to discard a call.
    source = re.sub(r"(?<==>)\s*void(?=\s*[;}])", "", source)
    source = re.sub(r"\bvoid(?=\s+[\w$.]+\()", "", source)
    source = re.sub(r"""["'`]\s*\+\s*["'`]""", "", source)
    return re.sub(r"</?[a-z][a-z0-9]*\s*>", "", source)


def violations(source: str, suffix: str, allowed: tuple[str, ...] = ()) -> tuple[str, ...]:
    text = " ".join(html.unescape(static_copy(source, suffix)).split())
    for disclosure in allowed:
        text = text.replace(disclosure, " ")
    return tuple(phrase for phrase, pattern in BANNED if pattern.search(text))


@pytest.mark.parametrize("relative", SOURCES)
def test_customer_copy_stays_inside_clerical_boundary(relative: str) -> None:
    path = ROOT / relative
    assert not violations(path.read_text(), path.suffix, APPROVED.get(relative, ()))


@pytest.mark.parametrize(
    ("source", "suffix", "phrase"),
    [
        ("<p>We <b>recommend</b> that you dispute it.</p>", ".tsx", "we recommend"),
        ('const copy = "Your ticket will " + "be dismissed";', ".ts", "will be dismissed"),
        ('const copy = "We will void your ticket.";', ".gs", "void"),
        ('message = ("This ticket " "will be overturned.")', ".py", "will be overturned"),
    ],
)
def test_copy_guard_finds_static_promises(source: str, suffix: str, phrase: str) -> None:
    assert phrase in violations(source, suffix)
