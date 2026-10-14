"""A deterministic, offline LLM used by tests, the demo, and ``LLM_PROVIDER=mock``.

It does not call any model. It runs simple per-agent heuristics (regexes over the added lines
of the diff) and returns JSON in the same shape the real prompts ask Gemini for. The output is
realistic enough to exercise the whole pipeline: parsing, validation, dedupe, rendering.

It also supports **fault injection**, so tests and the demo can prove fault isolation:

- ``error``   – raise ``LLMError`` on every call
- ``timeout`` – sleep far beyond any agent timeout
- ``garbage`` – return text that is not JSON
- ``flaky``   – fail the first call for that agent, then behave normally (exercises the retry)
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import LLMClient, LLMError, LLMRequest, LLMResponse

FAULT_MODES = frozenset({"error", "timeout", "garbage", "flaky"})

_FILE_RE = re.compile(r"^File under review: (\S+)", re.MULTILINE)
_ALL_FILES_RE = re.compile(r"^Files changed in this PR: (.*)$", re.MULTILINE)
_DIFF_RE = re.compile(r"<diff[^>]*>\n(.*?)\n</diff>", re.DOTALL)
_LINE_RE = re.compile(r"^\s*(\d+) ([+ ]) (.*)$")  # numbered added/context lines
_DEF_RE = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)\s*\(")
_CLASS_RE = re.compile(r"^(\s*)class\s+(\w+)")
_FOR_RE = re.compile(r"^(\s*)for\s+\w+.*\bin\b")


@dataclass
class _Line:
    number: int
    text: str
    added: bool = True

    @property
    def indent(self) -> int:
        return len(self.text) - len(self.text.lstrip())


def _numbered_lines(diff: str) -> list[_Line]:
    """Added and context lines (removed lines have no new-file number)."""
    out = []
    for raw in diff.splitlines():
        if m := _LINE_RE.match(raw):
            out.append(_Line(int(m.group(1)), m.group(3), added=m.group(2) == "+"))
    return out


def _finding(
    file: str, line: int, severity: str, title: str, explanation: str, suggestion: str
) -> dict[str, Any]:
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "title": title,
        "explanation": explanation,
        "suggestion": suggestion,
    }


def parse_faults(spec: str) -> dict[str, str]:
    """Parse ``"tests:timeout,documentation:garbage"`` into ``{"tests": "timeout", ...}``."""
    faults: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        agent, _, mode = item.partition(":")
        if mode not in FAULT_MODES:
            raise ValueError(
                f"unknown mock fault {mode!r} for {agent!r}; use {sorted(FAULT_MODES)}"
            )
        faults[agent.strip()] = mode
    return faults


# --------------------------------------------------------------------------------------------
# Heuristic "agents"
# --------------------------------------------------------------------------------------------


def _security(file: str, lines: list[_Line], _all_files: list[str]) -> list[dict[str, Any]]:
    out = []
    secret = re.compile(
        r"\b\w*(password|passwd|secret|api_key|apikey|token)\w*\s*=\s*[\"'][^\"']{6,}[\"']", re.I
    )
    sql_concat = re.compile(r"(select|insert|update|delete)\b.*[\"']\s*\+\s*\w+", re.I)
    sql_fstring = re.compile(r"f[\"'].*\b(select|insert|update|delete)\b.*\{", re.I)
    current_def = ""
    for ln in lines:
        if m := _DEF_RE.match(ln.text):
            current_def = m.group(3)
        t = ln.text
        if secret.search(t):
            name = t.split("=")[0].strip()
            out.append(_finding(
                file, ln.number, "critical", f"Hardcoded secret in `{name}`",
                "A credential is committed to source control. Anyone with read access to the "
                "repository (or its history) can use it.",
                f"Load `{name}` from an environment variable or a secrets manager, and rotate "
                "the exposed value.",
            ))  # fmt: skip
        if sql_concat.search(t) or sql_fstring.search(t):
            out.append(_finding(
                file, ln.number, "critical", "SQL injection via string-built query",
                "User-controlled values are concatenated into SQL, so input such as "
                "`' OR '1'='1` changes the query's meaning.",
                "Use parameterised queries, e.g. `conn.execute(\"... WHERE username = ?\", "
                "(username,))`.",
            ))  # fmt: skip
        if re.search(r"\b(eval|exec)\(", t):
            out.append(_finding(
                file, ln.number, "critical", "Arbitrary code execution via `eval`/`exec`",
                "Evaluating file or request content executes arbitrary Python.",
                "Parse data with a safe loader such as `json.loads` or `ast.literal_eval`.",
            ))  # fmt: skip
        if re.search(r"pickle\.loads?\(", t):
            out.append(_finding(
                file, ln.number, "high", "Unsafe deserialisation with pickle",
                "Unpickling untrusted data can execute arbitrary code.",
                "Use a data-only format such as JSON.",
            ))  # fmt: skip
        if re.search(r"shell\s*=\s*True", t):
            out.append(_finding(
                file, ln.number, "high", "Command injection risk (`shell=True`)",
                "Passing a string to the shell lets metacharacters in input run commands.",
                "Pass an argument list and leave `shell=False`.",
            ))  # fmt: skip
        if re.search(r"\brandom\.(randint|random|choice|randrange)\(", t) and re.search(
            r"token|session|secret|password|otp|nonce", current_def + t, re.I
        ):
            out.append(_finding(
                file, ln.number, "high", "Predictable token from the `random` module",
                "`random` is not cryptographically secure, and a 6-digit space is brute-forceable.",
                "Use `secrets.token_urlsafe(32)`.",
            ))  # fmt: skip
        if re.search(r"open\(.*os\.path\.join\(", t):
            out.append(_finding(
                file, ln.number, "high", "Path traversal in file open",
                "A caller-supplied path joined onto a base directory can escape it via `../`.",
                "Resolve the path and check that it stays under the base directory before "
                "opening it.",
            ))  # fmt: skip
        if re.search(r"requests\.(get|post|put|delete)\((url|f[\"'])", t):
            out.append(_finding(
                file, ln.number, "medium", "Outbound request built from caller input (SSRF risk)",
                "The request URL is derived from a parameter with no allow-list, so a caller can "
                "make the server reach internal hosts.",
                "Validate the host against an allow-list and URL-encode path parameters.",
            ))  # fmt: skip
    return out


def _performance(file: str, lines: list[_Line], _all_files: list[str]) -> list[dict[str, Any]]:
    out = []
    loop_stack: list[int] = []  # indents of enclosing `for` loops
    async_indent: int | None = None
    db_call = re.compile(r"\b(get_\w+_from_db|\.execute|\.query|requests\.(get|post))\(")
    for ln in lines:
        t = ln.text
        if not t.strip():
            continue
        while loop_stack and ln.indent <= loop_stack[-1]:
            loop_stack.pop()
        if (m := _DEF_RE.match(t)) is not None:
            async_indent = len(m.group(1)) if m.group(2) else None
            loop_stack.clear()
            continue
        if async_indent is not None and ln.indent <= async_indent:
            async_indent = None
        if m := _FOR_RE.match(t):
            if loop_stack and re.search(r"range\(len\(", t):
                out.append(_finding(
                    file, ln.number, "high", "Quadratic nested loop over the same collection",
                    "Two nested loops over `len(data)` make this O(n²); it slows down sharply "
                    "on large inputs.",
                    "Sort once, or use a set/dict lookup to make it O(n log n) or O(n).",
                ))  # fmt: skip
            loop_stack.append(len(m.group(1)))
            continue
        if loop_stack and db_call.search(t):
            out.append(_finding(
                file, ln.number, "high", "N+1 query pattern: data access inside a loop",
                "A database/API call runs once per loop iteration, so latency grows with the "
                "input size.",
                "Batch the lookup (e.g. `WHERE id IN (...)`) before the loop, then join in memory.",
            ))  # fmt: skip
        if async_indent is not None and re.search(r"\brequests\.\w+\(|time\.sleep\(", t):
            out.append(_finding(
                file, ln.number, "high", "Blocking call inside `async def`",
                "A synchronous HTTP call or sleep blocks the event loop, stalling every other "
                "coroutine.",
                "Use `httpx.AsyncClient` / `asyncio.sleep`, or move the call to "
                "`asyncio.to_thread`.",
            ))  # fmt: skip
        if re.search(r"requests\.\w+\(", t) and "timeout" not in t:
            out.append(_finding(
                file, ln.number, "medium", "HTTP call without a timeout",
                "`requests` waits forever by default, so one slow upstream can exhaust workers.",
                "Pass an explicit `timeout=(3, 10)`.",
            ))  # fmt: skip
        if re.search(r"SELECT \*", t, re.I) and not re.search(r"\bLIMIT\b|WHERE", t, re.I):
            out.append(_finding(
                file, ln.number, "medium", "Unbounded `SELECT *` loaded into memory",
                "`fetchall()` on an unfiltered table loads every row and column at once.",
                "Select only the columns you need, and paginate or stream with `LIMIT`/`OFFSET`.",
            ))  # fmt: skip
    return out


def _documentation(file: str, lines: list[_Line], _all_files: list[str]) -> list[dict[str, Any]]:
    """Missing docstrings, single-letter names and magic numbers, grouped per function."""
    out: list[dict[str, Any]] = []
    by_number = {ln.number: ln for ln in lines}
    allowed_short = {"i", "j", "k", "x", "y", "_", "e", "f"}
    # Per enclosing def: (def line number, name) -> collected short names / magic numbers.
    short_names: dict[tuple[int, str], list[str]] = {}
    magic_numbers: dict[tuple[int, str], list[tuple[int, str]]] = {}
    scope = (lines[0].number if lines else 1, "<module>")
    for ln in lines:
        t = ln.text
        m = _DEF_RE.match(t) or _CLASS_RE.match(t)
        if m:
            is_def = m.re is _DEF_RE
            name = m.group(3) if is_def else m.group(2)
            nxt = by_number.get(ln.number + 1)
            has_doc = nxt is not None and nxt.text.strip().startswith(('"""', "'''"))
            if not name.startswith("_") and not has_doc:
                kind = "function" if is_def else "class"
                out.append(_finding(
                    file, ln.number, "low", f"Missing docstring on public {kind} `{name}`",
                    "Readers have to infer the contract (inputs, return value, errors) from the "
                    "body.",
                    f"Add a docstring describing what `{name}` does and returns.",
                ))  # fmt: skip
            if is_def:
                scope = (ln.number, name)
                params = re.findall(r"\b([a-z])\b(?=\s*[,)])", t.split("(", 1)[1])
                short_names.setdefault(scope, []).extend(p for p in params if p not in {"_"})
            continue
        if (a := re.match(r"^\s*([a-z])\s*=[^=]", t)) and a.group(1) not in allowed_short:
            short_names.setdefault(scope, []).append(a.group(1))
        for magic in re.finditer(r"[*/+-]\s*(\d+\.\d+|\d{2,})\b", t):
            magic_numbers.setdefault(scope, []).append((ln.number, magic.group(1)))
        if re.search(r"#\s*(TODO|FIXME|HACK)\b(?!.*[#(]\w*\d)", t):
            out.append(_finding(
                file, ln.number, "low", "TODO without an owner or ticket",
                "Untracked TODOs tend to become permanent.",
                "Link an issue (e.g. `# TODO(#123): ...`) or resolve it before merging.",
            ))  # fmt: skip
    for (line, name), names in short_names.items():
        unique = list(dict.fromkeys(names))
        if unique:
            out.append(_finding(
                file, line, "low", f"Single-letter names in `{name}`",
                f"{', '.join(f'`{n}`' for n in unique)} do not convey what they hold.",
                "Rename them to describe their meaning (e.g. `weight`, `offset`, `items`).",
            ))  # fmt: skip
    for (_line, name), numbers in magic_numbers.items():
        values = ", ".join(f"`{v}`" for _, v in numbers)
        out.append(_finding(
            file, numbers[0][0], "low", f"Magic numbers in `{name}`: {values}",
            "Unexplained numeric literals hide intent and are easy to change inconsistently.",
            "Extract them into named module-level constants with a short comment.",
        ))  # fmt: skip
    return out


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
    )


def _tests(file: str, lines: list[_Line], all_files: list[str]) -> list[dict[str, Any]]:
    """Flag public functions that are new or have changed lines, when the PR adds no tests."""
    if _is_test_file(file) or any(_is_test_file(f) for f in all_files):
        return []
    risky = re.compile(r"auth|login|password|token|session|payment|config|score", re.I)
    changed: dict[str, int] = {}  # function name -> def line
    current: tuple[str, int, int] | None = None  # (name, def line, indent)
    for ln in lines:
        if (m := _DEF_RE.match(ln.text)) is not None:
            current = (m.group(3), ln.number, len(m.group(1)))
        elif current and ln.text.strip() and ln.indent <= current[2]:
            current = None  # dedented out of the function body
        if current and ln.added and not current[0].startswith("_"):
            changed.setdefault(current[0], current[1])
    out = []
    for name, line in changed.items():
        severity = "high" if risky.search(name) else "medium"
        out.append(_finding(
            file, line, severity, f"No tests for `{name}`",
            f"`{name}` is new or changed, and this PR contains no test changes covering it.",
            f"Add `test_{name}` covering the happy path, empty/None input, and the error path.",
        ))  # fmt: skip
    return out


HEURISTICS: dict[str, Callable[[str, list[_Line], list[str]], list[dict[str, Any]]]] = {
    "security": _security,
    "performance": _performance,
    "documentation": _documentation,
    "tests": _tests,
}


@dataclass
class MockLLMClient(LLMClient):
    """Offline ``LLMClient``. See the module docstring for the fault modes."""

    faults: dict[str, str] = field(default_factory=dict)
    latency_seconds: float = 0.0
    responder: Callable[[LLMRequest], str] | None = None
    calls: list[LLMRequest] = field(default_factory=list)
    max_in_flight: int = 0
    _in_flight: int = 0
    _flaky_seen: set[str] = field(default_factory=set)

    name = "mock"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.latency_seconds:
                await asyncio.sleep(self.latency_seconds)
            mode = self.faults.get(request.agent)
            if mode == "error":
                raise LLMError(f"injected failure for agent {request.agent!r}")
            if mode == "timeout":
                await asyncio.sleep(3600)
            if mode == "garbage":
                return LLMResponse(text="Sure! Here are my thoughts: the code looks {mostly} fine")
            if mode == "flaky" and request.agent not in self._flaky_seen:
                self._flaky_seen.add(request.agent)
                raise LLMError(f"injected transient failure for agent {request.agent!r}")
            text = self.responder(request) if self.responder else self._heuristic(request)
            return LLMResponse(
                text=text,
                input_tokens=(len(request.system) + len(request.prompt)) // 4,
                output_tokens=len(text) // 4,
            )
        finally:
            self._in_flight -= 1

    @staticmethod
    def _heuristic(request: LLMRequest) -> str:
        file_match = _FILE_RE.search(request.prompt)
        diff_match = _DIFF_RE.search(request.prompt)
        files_match = _ALL_FILES_RE.search(request.prompt)
        file = file_match.group(1) if file_match else "unknown"
        all_files = [f.strip() for f in files_match.group(1).split(",")] if files_match else []
        lines = _numbered_lines(diff_match.group(1)) if diff_match else []
        rule = HEURISTICS.get(request.agent)
        if request.agent != "tests":  # the other heuristics only look at added lines
            lines = [ln for ln in lines if ln.added]
        findings = rule(file, lines, all_files) if rule else []
        summary = (
            f"{len(findings)} {request.agent} issue(s) found in {file}."
            if findings
            else f"No {request.agent} issues found in {file}."
        )
        return json.dumps({"findings": findings, "summary": summary})
