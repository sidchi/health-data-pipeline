"""Build the Cloud Function source archive that Terraform uploads.

``gcloud functions deploy --source .`` reads ``.gcloudignore`` to decide what to
upload. Terraform has no such convention, so this module reproduces it: it walks
the repository, applies the same ``.gcloudignore`` rules, and writes a zip.

Two properties matter for infrastructure as code:

* **Deterministic output.** Entries are sorted and carry a fixed timestamp and
  fixed permissions, so the same source tree always produces byte-identical
  archives. Terraform names the uploaded object after the archive's SHA-256, so
  an unchanged tree means an unchanged object and no redeploy.
* **Fail closed on credentials.** Files that only ever hold local secrets
  (``.config.json``, token caches, OAuth client files) are refused even if a
  ``.gcloudignore`` edit stops excluding them.

Usage::

    python -m tools.package_function --out dist
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
IGNORE_FILE = ".gcloudignore"
ARCHIVE_NAME = "function-source.zip"

# Files the Cloud Function cannot start without.
REQUIRED_FILES = ("main.py", "requirements.txt")

# Never shipped, whatever .gcloudignore says. Matched against the file name.
FORBIDDEN_NAMES = (
    ".config.json",
    ".whoop_token.json",
    ".gdrive_token.json",
    "credentials.json",
    ".env",
    "*.tfstate",
    "*.tfvars",
)

# Directories that are never part of the deployable source, so the walk skips
# them without reading their contents.
ALWAYS_SKIPPED_DIRS = frozenset(
    {".git", ".venv", "__pycache__", "dist", ".terraform", "node_modules"}
)

# 1980-01-01 is the earliest timestamp the zip format can represent.
_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o644 << 16


class PackagingError(RuntimeError):
    """Raised when the source tree cannot be packaged safely."""


@dataclass(frozen=True)
class IgnoreRule:
    """One line of a ``.gcloudignore`` file (a subset of gitignore syntax).

    Supported: blank lines and ``#`` comments, ``*``/``?``/``[...]`` globs,
    a trailing ``/`` (matches directories only), a leading or embedded ``/``
    (anchors the pattern to the repository root) and ``!`` negation.
    """

    pattern: str
    negated: bool = False
    directory_only: bool = False
    anchored: bool = False

    @classmethod
    def parse(cls, line: str) -> IgnoreRule | None:
        line = line.rstrip()
        if not line or line.startswith("#"):
            return None
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        return cls(line.lstrip("/"), negated, directory_only, anchored)

    def matches(self, path: PurePosixPath, *, is_dir: bool) -> bool:
        if self.directory_only and not is_dir:
            return False
        if self.anchored:
            return fnmatch.fnmatchcase(path.as_posix(), self.pattern)
        return fnmatch.fnmatchcase(path.name, self.pattern)


def parse_ignore_file(text: str) -> list[IgnoreRule]:
    return [rule for line in text.splitlines() if (rule := IgnoreRule.parse(line))]


def is_ignored(path: PurePosixPath, rules: Sequence[IgnoreRule], *, is_dir: bool) -> bool:
    """Apply ``rules`` in order; the last matching rule wins (gitignore semantics)."""
    ignored = False
    for rule in rules:
        if rule.matches(path, is_dir=is_dir):
            ignored = not rule.negated
    return ignored


def collect_files(root: Path, rules: Sequence[IgnoreRule]) -> list[PurePosixPath]:
    """Return the repository-relative files to ship, sorted.

    An ignored directory is pruned entirely, as with gcloud: a negation rule
    cannot re-include a file inside it.
    """
    selected: list[PurePosixPath] = []

    def walk(directory: Path) -> None:
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            rel = PurePosixPath(entry.relative_to(root).as_posix())
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in ALWAYS_SKIPPED_DIRS or is_ignored(rel, rules, is_dir=True):
                    continue
                walk(entry)
            elif entry.is_file() and not is_ignored(rel, rules, is_dir=False):
                selected.append(rel)

    walk(root)
    return selected


def check_files(files: Iterable[PurePosixPath]) -> None:
    """Raise :class:`PackagingError` if required files are missing or secrets slipped in."""
    files = list(files)
    forbidden = sorted(
        str(f) for f in files if any(fnmatch.fnmatchcase(f.name, p) for p in FORBIDDEN_NAMES)
    )
    if forbidden:
        raise PackagingError(
            f"refusing to package credential files: {', '.join(forbidden)}. "
            f"Add them to {IGNORE_FILE}."
        )
    present = {f.as_posix() for f in files}
    missing = [name for name in REQUIRED_FILES if name not in present]
    if missing:
        raise PackagingError(f"source is missing required files: {', '.join(missing)}")


def write_archive(root: Path, files: Sequence[PurePosixPath], out: Path) -> str:
    """Write a deterministic zip of ``files`` to ``out`` and return its SHA-256 hex digest."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel.as_posix(), date_time=_FIXED_DATE_TIME)
            info.external_attr = _FILE_MODE
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so external_attr is read as POSIX mode bits
            archive.writestr(info, (root / rel).read_bytes(), compresslevel=9)
    return hashlib.sha256(out.read_bytes()).hexdigest()


@dataclass(frozen=True)
class BuildResult:
    path: Path
    sha256: str
    files: tuple[str, ...]


def build(root: Path = REPO_ROOT, out_dir: Path | None = None) -> BuildResult:
    """Package ``root`` into ``out_dir/function-source.zip``."""
    out_dir = out_dir if out_dir is not None else root / "dist"
    ignore_path = root / IGNORE_FILE
    rules = (
        parse_ignore_file(ignore_path.read_text(encoding="utf-8")) if ignore_path.is_file() else []
    )
    files = collect_files(root, rules)
    check_files(files)
    out = out_dir / ARCHIVE_NAME
    digest = write_archive(root, files, out)
    return BuildResult(out, digest, tuple(f.as_posix() for f in files))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default: dist/)")
    parser.add_argument("--list", action="store_true", help="print the packaged files")
    args = parser.parse_args(argv)
    try:
        result = build(args.root.resolve(), args.out)
    except PackagingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.list:
        for name in result.files:
            print(name, file=sys.stderr)
    summary = {"path": str(result.path), "sha256": result.sha256, "files": len(result.files)}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
