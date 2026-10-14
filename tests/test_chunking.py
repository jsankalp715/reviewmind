"""Diff parsing, skip rules, per-file chunking, and hunk/line splitting of oversized files."""

from __future__ import annotations

import pytest

from app.chunking import (
    ChunkPlan,
    annotate_hunk,
    chunk_file,
    parse_unified_diff,
    plan_chunks,
    skip_reason,
    split_hunks,
)
from app.models import ChangedFile


def make_hunk(start: int, n: int, prefix: str = "line") -> str:
    body = "\n".join(f"+{prefix} {i} " + "x" * 40 for i in range(n))
    return f"@@ -{start},0 +{start},{n} @@\n{body}"


def changed(
    name: str, patch: str | None = "@@ -1 +1 @@\n+x", status: str = "modified"
) -> ChangedFile:
    return ChangedFile(filename=name, status=status, patch=patch)


class TestParseUnifiedDiff:
    def test_parses_fixture(self, sample_files: list[ChangedFile]) -> None:
        names = [f.filename for f in sample_files]
        assert names == [
            "app/auth.py",
            "app/reports.py",
            "app/utils.py",
            "assets/logo.png",
            "poetry.lock",
        ]
        by_name = {f.filename: f for f in sample_files}
        assert by_name["app/auth.py"].status == "modified"
        assert by_name["app/reports.py"].status == "added"
        assert by_name["assets/logo.png"].patch is None
        assert by_name["app/reports.py"].additions == 34

    def test_removed_line_starting_with_dashes_is_not_a_header(self) -> None:
        diff = (
            "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n"
            "@@ -1,2 +1,1 @@\n--- a SQL comment\n+++ not a header\n-SELECT 1;\n"
        )
        # The hunk says 2 old lines and 1 new line: "--- a SQL comment" and "-SELECT 1;" are
        # removals, and "+++ not a header" is an addition.
        files = parse_unified_diff(diff)
        assert [f.filename for f in files] == ["q.sql"]
        assert files[0].deletions == 2 and files[0].additions == 1

    def test_deleted_and_renamed_files(self) -> None:
        diff = (
            "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-gone\n"
            "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
        )
        files = {f.filename: f for f in parse_unified_diff(diff)}
        assert files["old.py"].status == "removed"
        assert files["b.py"].status == "renamed" and files["b.py"].previous_filename == "a.py"

    def test_plain_diff_without_git_headers(self) -> None:
        diff = (
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
            "--- /dev/null\n+++ b/y.py\n@@ -0,0 +1 @@\n+c\n"
        )
        files = parse_unified_diff(diff)
        assert [(f.filename, f.status) for f in files] == [("x.py", "modified"), ("y.py", "added")]


class TestSkipRules:
    @pytest.mark.parametrize(
        "name",
        ["package-lock.json", "web/yarn.lock", "poetry.lock", "go.sum", "Cargo.lock", "uv.lock"],
    )
    def test_lockfiles(self, name: str) -> None:
        assert skip_reason(changed(name)) == "lockfile"

    @pytest.mark.parametrize("name", ["logo.png", "docs/manual.PDF", "lib.so", "font.woff2"])
    def test_binaries_by_extension(self, name: str) -> None:
        assert skip_reason(changed(name)) == "binary file"

    def test_binary_without_patch(self) -> None:
        assert "no textual diff" in (skip_reason(changed("data.custom", patch=None)) or "")

    def test_minified_vendored_deleted_empty(self) -> None:
        assert skip_reason(changed("static/app.min.js")) == "generated/minified asset"
        assert skip_reason(changed("frontend/node_modules/x/index.js")) == "vendored dependency"
        assert skip_reason(changed("x.py", status="removed")) == "file deleted"
        assert skip_reason(changed("x.py", patch="  \n")) == "empty diff"

    def test_normal_source_file_is_reviewed(self) -> None:
        assert skip_reason(changed("app/main.py")) is None


class TestAnnotation:
    def test_new_file_line_numbers(self) -> None:
        hunk = (
            "@@ -10,3 +20,3 @@ def f():\n context\n-removed\n+added\n more\n"
            "\\ No newline at end of file"
        )
        out = annotate_hunk(hunk)
        assert out[0].startswith("@@ -10,3 +20,3 @@")
        assert out[1] == "   20   context"
        assert out[2] == "      - removed"
        assert out[3] == "   21 + added"
        assert out[4] == "   22   more"
        assert len(out) == 5  # the "\ No newline" marker is dropped

    def test_split_hunks_drops_file_headers(self) -> None:
        patch = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n+a\n@@ -5 +5 @@\n+b"
        assert split_hunks(patch) == ["@@ -1 +1 @@\n+a", "@@ -5 +5 @@\n+b"]


class TestChunkFile:
    def test_small_file_is_one_chunk(self) -> None:
        chunks = chunk_file(changed("a.py", make_hunk(1, 5)), max_chars=10_000)
        assert len(chunks) == 1
        assert chunks[0].label == "a.py" and chunks[0].total_parts == 1

    def test_oversized_file_splits_on_hunk_boundaries(self) -> None:
        patch = "\n".join(make_hunk(1 + i * 100, 10, prefix=f"h{i}") for i in range(6))
        hunk_len = len("\n".join(annotate_hunk(make_hunk(1, 10))))
        chunks = chunk_file(changed("big.py", patch), max_chars=hunk_len * 2 + 10)
        assert len(chunks) == 3
        assert all(len(c.content) <= hunk_len * 2 + 10 for c in chunks)
        assert [c.part for c in chunks] == [1, 2, 3]
        assert chunks[1].label == "big.py (part 2/3)"
        # Every chunk starts at a hunk header and no hunk is lost or duplicated.
        assert all(c.content.startswith("@@") for c in chunks)
        joined = "\n".join(c.content for c in chunks)
        for i in range(6):
            assert joined.count(f"+ h{i} 0 ") == 1

    def test_single_oversized_hunk_splits_on_lines_keeping_line_numbers(self) -> None:
        patch = make_hunk(100, 200)
        chunks = chunk_file(changed("huge.py", patch), max_chars=2_000)
        assert len(chunks) > 1
        assert all(len(c.content) <= 2_000 for c in chunks)
        assert chunks[1].content.splitlines()[0].endswith("(continued)")
        # Original new-file line numbers survive the split.
        assert "  100 + line 0 " in chunks[0].content
        assert "  299 + line 199 " in chunks[-1].content
        all_lines = [ln for c in chunks for ln in c.content.splitlines() if " + line " in ln]
        assert len(all_lines) == 200

    def test_single_enormous_line_is_truncated_not_dropped(self) -> None:
        patch = "@@ -0,0 +1,1 @@\n+" + "A" * 10_000
        chunks = chunk_file(changed("min.txt", patch), max_chars=500)
        assert len(chunks) == 1 and len(chunks[0].content) <= 500
        assert "[truncated]" in chunks[0].content

    def test_patch_without_hunk_header(self) -> None:
        chunks = chunk_file(changed("odd.txt", "+just a line"), max_chars=1000)
        assert len(chunks) == 1 and "just a line" in chunks[0].content


class TestPlanChunks:
    def test_fixture_plan(self, sample_files: list[ChangedFile]) -> None:
        plan = plan_chunks(sample_files, max_chars=24_000, max_chunks=40)
        assert isinstance(plan, ChunkPlan)
        assert [c.filename for c in plan.chunks] == [
            "app/auth.py",
            "app/reports.py",
            "app/utils.py",
        ]
        assert {(s.filename, s.reason) for s in plan.skipped} == {
            ("assets/logo.png", "binary file"),
            ("poetry.lock", "lockfile"),
        }
        assert plan.files_reviewed == 3

    def test_chunk_limit_skips_remaining_files_explicitly(self) -> None:
        files = [changed(f"f{i}.py", make_hunk(1, 3)) for i in range(5)]
        plan = plan_chunks(files, max_chars=10_000, max_chunks=3)
        assert len(plan.chunks) == 3
        assert [s.filename for s in plan.skipped] == ["f3.py", "f4.py"]
        assert all("size limit" in s.reason for s in plan.skipped)

    def test_oversized_file_contributes_multiple_chunks(self) -> None:
        files = [changed("huge.py", make_hunk(1, 300)), changed("small.py", make_hunk(1, 2))]
        plan = plan_chunks(files, max_chars=3_000, max_chunks=40)
        assert sum(c.filename == "huge.py" for c in plan.chunks) > 1
        assert plan.chunks[-1].filename == "small.py"
        assert plan.files_reviewed == 2
