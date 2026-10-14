"""Turn a PR's changed files into LLM-sized chunks.

Strategy:
1. Skip files that are not worth reviewing: lockfiles, binaries, minified/generated assets,
   vendored code, deleted files, and files without a textual patch.
2. One chunk per file. Each diff line is prefixed with its line number in the new file, so
   the model can cite lines accurately.
3. If a single file's annotated diff exceeds ``max_chars``, split it on hunk boundaries and
   greedily pack consecutive hunks into chunks of at most ``max_chars``.
4. If a single hunk is still too large, split it on line boundaries. Every piece keeps the
   hunk header and the original line numbers.
5. Stop at ``max_chunks``. Files that do not fit are reported as skipped instead of being
   silently truncated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.models import ChangedFile, SkippedFile

LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "Pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
        "mix.lock",
        "pubspec.lock",
        "Podfile.lock",
        "packages.lock.json",
        "flake.lock",
    }
)

BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd",
        ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".rar", ".jar",
        ".war", ".whl", ".egg", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3",
        ".mp4", ".mov", ".avi", ".wav", ".flac", ".ogg", ".exe", ".dll", ".so",
        ".dylib", ".bin", ".o", ".a", ".pyc", ".class", ".db", ".sqlite", ".sqlite3",
        ".parquet", ".pkl", ".npy", ".onnx", ".pt",
    }
)  # fmt: skip

GENERATED_SUFFIXES = (".min.js", ".min.css", ".js.map", ".css.map")
VENDORED_DIRS = ("node_modules/", "vendor/", "third_party/", "dist/")

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffChunk:
    """A slice of one file's diff, small enough for a single LLM call."""

    filename: str
    status: str
    content: str  # annotated diff text sent to the model
    part: int = 1
    total_parts: int = 1

    @property
    def label(self) -> str:
        if self.total_parts == 1:
            return self.filename
        return f"{self.filename} (part {self.part}/{self.total_parts})"


@dataclass(frozen=True)
class ChunkPlan:
    chunks: list[DiffChunk]
    skipped: list[SkippedFile]

    @property
    def files_reviewed(self) -> int:
        return len({c.filename for c in self.chunks})


def skip_reason(file: ChangedFile) -> str | None:
    """Return why ``file`` should not be reviewed, or ``None`` if it should be."""
    path = PurePosixPath(file.filename)
    lowered = file.filename.lower()
    if path.name in LOCKFILES:
        return "lockfile"
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return "binary file"
    if lowered.endswith(GENERATED_SUFFIXES):
        return "generated/minified asset"
    if any(lowered.startswith(d) or f"/{d}" in lowered for d in VENDORED_DIRS):
        return "vendored dependency"
    if file.status == "removed":
        return "file deleted"
    if file.patch is None:
        return "no textual diff (binary or too large for the GitHub API)"
    if not file.patch.strip():
        return "empty diff"
    return None


def split_hunks(patch: str) -> list[str]:
    """Split a single file's patch into hunks, each starting with its ``@@`` header.

    Any text before the first hunk header (e.g. ``---``/``+++`` lines) is dropped.
    """
    hunks: list[list[str]] = []
    for line in patch.splitlines():
        if HUNK_HEADER_RE.match(line):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
    return ["\n".join(h) for h in hunks]


def annotate_hunk(hunk: str) -> list[str]:
    """Prefix each diff line with its new-file line number (blank for removed lines).

    ``@@ -1,2 +10,3 @@`` followed by ``+foo`` becomes ``  10 + foo``.
    """
    lines = hunk.splitlines()
    if not lines:
        return []
    header = HUNK_HEADER_RE.match(lines[0])
    new_line = int(header.group(3)) if header else 1
    out = [lines[0]]
    for raw in lines[1:]:
        marker, text = (raw[0], raw[1:]) if raw else (" ", "")
        if marker == "+":
            out.append(f"{new_line:>5} + {text}")
            new_line += 1
        elif marker == "-":
            out.append(f"{'':>5} - {text}")
        elif marker == "\\":  # "\ No newline at end of file"
            continue
        else:
            out.append(f"{new_line:>5}   {text}")
            new_line += 1
    return out


def _split_oversized_hunk(lines: list[str], max_chars: int) -> list[str]:
    """Split one annotated hunk on line boundaries, repeating the header on each piece."""
    header, body = lines[0], lines[1:]
    # Hunk headers carry function context and can be long; keep them to a quarter of the budget.
    header = header[: max_chars // 4]
    budget = max(max_chars - len(header) - 20, 40)
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in body:
        # A single line longer than the budget is truncated rather than dropped.
        if len(line) > budget:
            line = line[: budget - 15] + " …[truncated]"
        if current and size + len(line) + 1 > budget:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return [
        f"{header}{'' if i == 0 else ' (continued)'}\n{piece}" for i, piece in enumerate(pieces)
    ]


def chunk_file(file: ChangedFile, max_chars: int) -> list[DiffChunk]:
    """Chunk one reviewable file. Returns a single chunk unless the diff exceeds ``max_chars``."""
    patch = file.patch or ""
    hunks = [annotate_hunk(h) for h in split_hunks(patch)]
    if not hunks:
        # A patch without any hunk header (unusual); send it as-is, hard-split if huge.
        hunks = [["@@ (no hunk header) @@", *patch.splitlines()]]

    pieces: list[str] = []
    for hunk_lines in hunks:
        text = "\n".join(hunk_lines)
        pieces.extend(
            [text] if len(text) <= max_chars else _split_oversized_hunk(hunk_lines, max_chars)
        )

    # Greedily pack consecutive pieces of this file into chunks within the budget.
    packed: list[str] = []
    for piece in pieces:
        if packed and len(packed[-1]) + 1 + len(piece) <= max_chars:
            packed[-1] = f"{packed[-1]}\n{piece}"
        else:
            packed.append(piece)

    total = len(packed)
    return [
        DiffChunk(filename=file.filename, status=file.status, content=c, part=i, total_parts=total)
        for i, c in enumerate(packed, start=1)
    ]


def plan_chunks(files: list[ChangedFile], max_chars: int, max_chunks: int) -> ChunkPlan:
    """Build the full chunk plan for a PR."""
    chunks: list[DiffChunk] = []
    skipped: list[SkippedFile] = []
    for file in files:
        reason = skip_reason(file)
        if reason:
            skipped.append(SkippedFile(filename=file.filename, reason=reason))
            continue
        file_chunks = chunk_file(file, max_chars)
        if len(chunks) + len(file_chunks) > max_chunks:
            skipped.append(
                SkippedFile(
                    filename=file.filename,
                    reason=f"review size limit reached ({max_chunks} chunks per review)",
                )
            )
            continue
        chunks.extend(file_chunks)
    return ChunkPlan(chunks=chunks, skipped=skipped)


def parse_unified_diff(diff: str) -> list[ChangedFile]:
    """Parse a multi-file unified diff (``git diff`` / GitHub ``.diff`` output) into files.

    Hunk line counts are tracked, so removed lines that start with ``--`` are not mistaken
    for file headers.
    """
    files: list[ChangedFile] = []
    current: dict[str, object] | None = None
    patch_lines: list[str] = []
    old_left = new_left = 0

    def flush() -> None:
        if current is not None:
            current["patch"] = "\n".join(patch_lines) if patch_lines else current.get("patch")
            files.append(ChangedFile.model_validate(current))

    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        in_hunk = old_left > 0 or new_left > 0
        if in_hunk and line[:1] in (" ", "+", "-", "\\", ""):
            patch_lines.append(line)
            marker = line[:1]
            if marker in (" ", ""):
                old_left -= 1
                new_left -= 1
            elif marker == "-":
                old_left -= 1
            elif marker == "+":
                new_left -= 1
            old_left, new_left = max(old_left, 0), max(new_left, 0)
            i += 1
            continue
        old_left = new_left = 0

        if line.startswith("diff --git "):
            flush()
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            name = match.group(2) if match else line.split()[-1]
            current, patch_lines = {"filename": name, "status": "modified"}, []
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old_path, new_path = line[4:].strip(), lines[i + 1][4:].strip()
            if current is None or patch_lines:
                flush()
                current, patch_lines = {"filename": "", "status": "modified"}, []
            if old_path == "/dev/null":
                current["status"] = "added"
            if new_path == "/dev/null":
                current["status"] = "removed"
                current["filename"] = old_path.removeprefix("a/")
            else:
                current["filename"] = new_path.removeprefix("b/")
            i += 2
            continue
        elif current is not None and line.startswith("new file mode"):
            current["status"] = "added"
        elif current is not None and line.startswith("deleted file mode"):
            current["status"] = "removed"
        elif current is not None and line.startswith("rename from "):
            current["previous_filename"] = line[len("rename from ") :]
            current["status"] = "renamed"
        elif current is not None and line.startswith("Binary files "):
            current["patch"] = None
        elif current is not None and (header := HUNK_HEADER_RE.match(line)):
            patch_lines.append(line)
            old_left = int(header.group(2)) if header.group(2) is not None else 1
            new_left = int(header.group(4)) if header.group(4) is not None else 1
        i += 1
    flush()

    for f in files:
        if f.patch:
            f.additions = sum(
                1 for ln in f.patch.splitlines() if ln.startswith("+") and not ln.startswith("@@")
            )
            f.deletions = sum(1 for ln in f.patch.splitlines() if ln.startswith("-"))
    return [f for f in files if f.filename]
