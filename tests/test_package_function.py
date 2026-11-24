"""Tests for the Cloud Function source packager used by Terraform."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from tools import package_function as pf

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


MINIMAL = {
    "main.py": "print('hi')\n",
    "requirements.txt": "requests\n",
}


# --- .gcloudignore semantics ---------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        ("*.md", "README.md", False, True),
        ("*.md", "docs/guide.md", False, True),  # unanchored: matches at any depth
        ("tests/", "tests", True, True),
        ("tests/", "tests", False, False),  # trailing slash: directories only
        ("whoop_exporter/cli.py", "whoop_exporter/cli.py", False, True),
        ("whoop_exporter/cli.py", "other/whoop_exporter/cli.py", False, False),  # anchored
        ("/main.py", "main.py", False, True),
        (".config*.json", ".config.prod.json", False, True),
        ("WhoopExport-*.json", "WhoopExport-2026-01-01.json", False, True),
    ],
)
def test_rule_matching(pattern: str, path: str, is_dir: bool, expected: bool) -> None:
    rule = pf.IgnoreRule.parse(pattern)
    assert rule is not None
    assert rule.matches(PurePosixPath(path), is_dir=is_dir) is expected


def test_comments_and_blank_lines_are_not_rules() -> None:
    assert pf.parse_ignore_file("# comment\n\n   \n*.md\n") == [pf.IgnoreRule("*.md")]


def test_last_matching_rule_wins() -> None:
    rules = pf.parse_ignore_file("*.json\n!keep.json\n")
    assert pf.is_ignored(PurePosixPath("drop.json"), rules, is_dir=False)
    assert not pf.is_ignored(PurePosixPath("keep.json"), rules, is_dir=False)


def test_ignored_directory_is_pruned_even_with_negation(tmp_path: Path) -> None:
    _tree(tmp_path, {**MINIMAL, "tests/keep.py": "", "tests/other.py": ""})
    rules = pf.parse_ignore_file("tests/\n!tests/keep.py\n")
    assert pf.collect_files(tmp_path, rules) == [
        PurePosixPath("main.py"),
        PurePosixPath("requirements.txt"),
    ]


def test_caches_and_virtualenvs_are_always_skipped(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {**MINIMAL, "__pycache__/main.cpython-312.pyc": "", ".venv/lib/x.py": "", "dist/a.zip": ""},
    )
    assert [f.as_posix() for f in pf.collect_files(tmp_path, [])] == ["main.py", "requirements.txt"]


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    _tree(tmp_path, MINIMAL)
    outside = _tree(tmp_path / "outside", {"secret.txt": "x"})
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "link.txt").symlink_to(outside / "secret.txt")
    files = pf.collect_files(tmp_path, pf.parse_ignore_file("outside/\n"))
    assert PurePosixPath("src/link.txt") not in files


# --- safety checks -------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret", [".config.json", ".whoop_token.json", "credentials.json", "infra/prod.tfvars"]
)
def test_credential_files_are_refused_even_if_not_ignored(tmp_path: Path, secret: str) -> None:
    _tree(tmp_path, {**MINIMAL, secret: "{}"})
    with pytest.raises(pf.PackagingError, match="credential"):
        pf.build(tmp_path, tmp_path / "out")
    assert not (tmp_path / "out" / pf.ARCHIVE_NAME).exists()


def test_missing_entry_point_is_an_error(tmp_path: Path) -> None:
    _tree(tmp_path, {"requirements.txt": ""})
    with pytest.raises(pf.PackagingError, match=r"main\.py"):
        pf.build(tmp_path, tmp_path / "out")


# --- archive properties --------------------------------------------------------------


def test_archive_is_byte_for_byte_reproducible(tmp_path: Path) -> None:
    src = _tree(tmp_path / "src", {**MINIMAL, "pkg/a.py": "A = 1\n"})
    first = pf.build(src, tmp_path / "one")
    # Different mtimes and permissions must not change the output.
    (src / "pkg" / "a.py").touch()
    (src / "main.py").chmod(0o755)
    second = pf.build(src, tmp_path / "two")
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == hashlib.sha256(first.path.read_bytes()).hexdigest()


def test_content_change_changes_the_digest(tmp_path: Path) -> None:
    src = _tree(tmp_path / "src", MINIMAL)
    before = pf.build(src, tmp_path / "out").sha256
    (src / "main.py").write_text("print('changed')\n", encoding="utf-8")
    assert pf.build(src, tmp_path / "out").sha256 != before


def test_archive_entries_are_sorted_with_fixed_metadata(tmp_path: Path) -> None:
    src = _tree(tmp_path / "src", {**MINIMAL, "b/z.py": "", "a.py": ""})
    result = pf.build(src, tmp_path / "out")
    with zipfile.ZipFile(result.path) as archive:
        infos = archive.infolist()
    names = [i.filename for i in infos]
    assert names == sorted(names)
    assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {i.external_attr >> 16 for i in infos} == {0o644}


# --- the real repository -------------------------------------------------------------


@pytest.fixture(scope="module")
def repo_build(tmp_path_factory: pytest.TempPathFactory) -> pf.BuildResult:
    return pf.build(REPO_ROOT, tmp_path_factory.mktemp("dist"))


def test_repository_archive_ships_only_the_function(repo_build: pf.BuildResult) -> None:
    files = set(repo_build.files)
    assert {"main.py", "requirements.txt", "whoop_exporter/client.py"} <= files
    for excluded in ("whoop.py", "whoop_exporter/cli.py", "whoop_exporter/sample.py"):
        assert excluded not in files
    prefixes = ("tests/", "health_pipeline/", "sample_data/", "infra/", "tools/", ".github/")
    assert not [f for f in files if f.startswith(prefixes)]
    assert not [f for f in files if f.endswith((".md", ".json"))]


def test_repository_archive_imports_in_isolation(
    repo_build: pf.BuildResult, tmp_path: Path
) -> None:
    """The deployed function must not depend on anything the archive leaves out."""
    with zipfile.ZipFile(repo_build.path) as archive:
        archive.extractall(tmp_path)
    code = "import main; assert callable(main.trigger_upload); print('ok')"
    proc = subprocess.run(  # noqa: S603 (fixed interpreter and arguments)
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(tmp_path)!r}); {code}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_cli_prints_path_and_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _tree(tmp_path / "src", MINIMAL)
    assert pf.main(["--root", str(src), "--out", str(tmp_path / "out")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] == 2
    assert Path(payload["path"]).is_file()
    assert len(payload["sha256"]) == 64


def test_cli_reports_errors_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _tree(tmp_path / "src", {**MINIMAL, ".config.json": "{}"})
    assert pf.main(["--root", str(src), "--out", str(tmp_path / "out")]) == 1
    assert capsys.readouterr().err.startswith("error: refusing to package")
