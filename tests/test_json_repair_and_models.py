"""JSON repair of model output and Pydantic validation of findings."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.llm.json_repair import (
    InvalidLLMOutput,
    extract_json,
    parse_review_response,
    strip_code_fences,
)
from app.models import Finding, Severity

GOOD = {
    "findings": [
        {
            "file": "app/x.py",
            "line": 3,
            "severity": "high",
            "title": "Bad thing",
            "explanation": "Because.",
            "suggestion": "Fix it.",
        }
    ],
    "summary": "One issue.",
}


class TestExtractJson:
    def test_plain_json(self) -> None:
        assert extract_json(json.dumps(GOOD)) == GOOD

    def test_code_fence(self) -> None:
        assert extract_json(f"```json\n{json.dumps(GOOD)}\n```") == GOOD
        assert strip_code_fences("```\n{}\n```") == "{}"

    def test_prose_around_json(self) -> None:
        text = f"Here is my review:\n{json.dumps(GOOD)}\nHope this helps!"
        assert extract_json(text) == GOOD

    def test_braces_inside_strings(self) -> None:
        data = {"findings": [], "summary": "use {x} and } carefully"}
        assert extract_json("prefix " + json.dumps(data) + " suffix") == data

    def test_trailing_commas(self) -> None:
        assert extract_json('{"findings": [1, 2,], "summary": "s",}') == {
            "findings": [1, 2],
            "summary": "s",
        }

    @pytest.mark.parametrize("text", ["", "   ", "no json here", "{unclosed", "{'single': 1}"])
    def test_garbage_raises(self, text: str) -> None:
        with pytest.raises(InvalidLLMOutput):
            extract_json(text)


class TestParseReviewResponse:
    def test_valid(self) -> None:
        parsed = parse_review_response(json.dumps(GOOD))
        assert parsed.summary == "One issue."
        assert parsed.findings[0].severity is Severity.HIGH

    def test_bare_list_is_wrapped(self) -> None:
        parsed = parse_review_response(json.dumps(GOOD["findings"]))
        assert len(parsed.findings) == 1

    def test_empty_findings(self) -> None:
        assert parse_review_response('{"findings": [], "summary": "clean"}').findings == []
        assert parse_review_response('{"findings": null}').findings == []

    def test_invalid_items_are_dropped_but_valid_ones_kept(self) -> None:
        data = {"findings": [GOOD["findings"][0], {"title": "no file or severity"}]}
        parsed = parse_review_response(json.dumps(data))
        assert len(parsed.findings) == 1

    def test_all_items_invalid_is_rejected(self) -> None:
        with pytest.raises(InvalidLLMOutput, match="failed validation"):
            parse_review_response('{"findings": [{"nope": 1}, {"severity": "bogus"}]}')

    @pytest.mark.parametrize("text", ['"just a string"', "42", '{"findings": "x"}'])
    def test_wrong_shapes_are_rejected(self, text: str) -> None:
        with pytest.raises(InvalidLLMOutput):
            parse_review_response(text)

    def test_non_string_summary_is_ignored(self) -> None:
        assert parse_review_response('{"findings": [], "summary": 5}').summary == ""


class TestFindingValidation:
    def base(self, **kw: object) -> dict[str, object]:
        return {"file": "a.py", "severity": "low", "title": "t", **kw}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (12, 12),
            ("12", 12),
            ("12-15", 12),
            ("app.py:7", 7),
            ("L9", 9),
            (None, None),
            ("general", None),
            (0, None),
            (-3, None),
            (4.0, 4),
            (True, None),
        ],
    )
    def test_line_coercion(self, raw: object, expected: int | None) -> None:
        assert Finding.model_validate(self.base(line=raw)).line == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("HIGH", Severity.HIGH),
            (" Medium ", Severity.MEDIUM),
            ("warning", Severity.MEDIUM),
            ("blocker", Severity.CRITICAL),
            ("nit", Severity.LOW),
            ("note", Severity.INFO),
        ],
    )
    def test_severity_normalisation(self, raw: str, expected: Severity) -> None:
        assert Finding.model_validate(self.base(severity=raw)).severity is expected

    def test_unknown_severity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Finding.model_validate(self.base(severity="catastrophic"))

    @pytest.mark.parametrize("missing", ["file", "severity", "title"])
    def test_required_fields(self, missing: str) -> None:
        data = self.base()
        del data[missing]
        with pytest.raises(ValidationError):
            Finding.model_validate(data)

    def test_blank_title_rejected_after_strip(self) -> None:
        with pytest.raises(ValidationError):
            Finding.model_validate(self.base(title="   "))

    def test_severity_rank_order(self) -> None:
        ranks = [s.rank for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW)]
        assert ranks == sorted(ranks)
