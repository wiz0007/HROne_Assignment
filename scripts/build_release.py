"""
Release Packaging and Integrity Verification Script
Sprint 5 - Requirement S5-REL-01

Assembles release_submission.zip from intended source files, ensuring:
- Strict exclusion of secrets (.env), .git, .venv, caches (__pycache__, .pytest_cache), and local files.
- Complete inclusion of source code, test suites, benchmark tools, documentation, and dependencies.
- Zip file integrity testing via zipfile.testzip().
"""
import os
import re
import zipfile
from pathlib import Path

# Repository root directory
REPO_ROOT = Path(__file__).resolve().parent.parent

# Forbidden filename/path regex patterns (case-insensitive)
FORBIDDEN_PATTERNS = [
    re.compile(r"(^|[/\\])\.env($|[/\\]|\..*)", re.IGNORECASE),  # .env, .env.local, .env.prod, etc.
    re.compile(r"(^|[/\\])\.git($|[/\\])", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.venv($|[/\\])", re.IGNORECASE),
    re.compile(r"(^|[/\\])__pycache__($|[/\\])", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.pytest_cache($|[/\\])", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.ruff_cache($|[/\\])", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.vscode($|[/\\])", re.IGNORECASE),
    re.compile(r"(^|[/\\])\.idea($|[/\\])", re.IGNORECASE),
    re.compile(r"\.py[cod]$", re.IGNORECASE),
    re.compile(r"\.DS_Store$", re.IGNORECASE),
    re.compile(r"\.(key|pem|cert|crt|pfx|p12|token|dump|sql|sqlite|sqlite3)$", re.IGNORECASE),
    re.compile(r"(^|[/\\])(id_rsa|id_ed25519)($|\..*)", re.IGNORECASE),
    re.compile(r"(^|[/\\])(credentials|secrets?|passwords?|auth_token)(\.json|\.yaml|\.yml|\.txt|\.xml)?$", re.IGNORECASE),
    re.compile(r"(^|[/\\])release_submission.*\.zip$", re.IGNORECASE),
    re.compile(r"(^|[/\\]).*\.tmp\.zip$", re.IGNORECASE),
]

# Required files that must be present in a valid release
REQUIRED_FILES = [
    "app/main.py",
    "app/__init__.py",
    "tests/conftest.py",
    "tests/test_sprint1.py",
    "tests/test_sprint2.py",
    "tests/test_sprint3.py",
    "tests/test_sprint4.py",
    "tests/test_sprint5.py",
    "benchmarks/scale_benchmark.py",
    "benchmarks/benchmark_results.json",
    "requirements.txt",
    "README.md",
    "openapi.yaml",
    "DATA_MODEL.md",
    "DECISIONS.md",
    "SPRINT4_VERIFICATION.md",
    "SPRINT5_VERIFICATION.md",
    "assignment_report.md",
    ".env.example",
]


def check_env_example_safe(env_example_path: Path) -> None:
    """Verifies that .env.example contains only safe placeholders and no leaked credentials."""
    if not env_example_path.exists():
        return
    try:
        content = env_example_path.read_text(encoding="utf-8")
    except Exception as e:
        raise ValueError(f"Unable to read .env.example: {e}")

    # Check for actual secret patterns or tokens
    leak_patterns = [
        re.compile(r"://([^:@\s]+):([^@\s]+)@", re.IGNORECASE),  # URI with user:pass
        re.compile(r"(api[_-]?key|secret[_-]?key|auth[_-]?token)\s*=\s*(?!your_|placeholder|dummy|example|\s*$)[a-zA-Z0-9_\-]{16,}", re.IGNORECASE),
        re.compile(r"password\s*=\s*(?!your_|placeholder|dummy|example|\s*$)[^\s]+", re.IGNORECASE),
    ]
    for p in leak_patterns:
        if p.search(content):
            raise ValueError(".env.example contains sensitive credentials or secret values!")


def is_forbidden(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    if normalized == ".env.example" or normalized.endswith("/.env.example"):
        return False
    return any(p.search(normalized) for p in FORBIDDEN_PATTERNS)


def build_release_zip(output_path: str | None = None, source_dir: str | None = None, required_files: list[str] | None = None) -> str:
    """
    Builds release zip archive from repository root or specified source directory.
    Rejects external symlinks, validates paths, ensures atomic archive replacement,
    and guarantees temp file cleanup on both success and failure.
    """
    root_dir = Path(source_dir).resolve() if source_dir else REPO_ROOT
    if output_path is None:
        output_path = str(root_dir / "release_submission.zip")

    target_zip = Path(output_path).resolve()
    temp_zip = target_zip.with_suffix(".tmp.zip")

    if temp_zip.exists():
        temp_zip.unlink()

    check_required = required_files if required_files is not None else REQUIRED_FILES

    # Check .env.example safety
    check_env_example_safe(root_dir / ".env.example")

    included_count = 0
    try:
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(root_dir, followlinks=False):
                # Check directory symlinks for path traversal outside repository
                valid_dirs = []
                for d in dirs:
                    d_path = Path(root) / d
                    if os.path.islink(d_path):
                        resolved_d = d_path.resolve()
                        try:
                            resolved_d.relative_to(root_dir)
                        except ValueError:
                            raise ValueError(f"External directory symlink rejected (points outside repository): {d}")
                    rel_d = os.path.relpath(d_path, root_dir)
                    if not is_forbidden(rel_d):
                        valid_dirs.append(d)
                dirs[:] = valid_dirs

                for file in files:
                    abs_file = Path(root) / file
                    rel_file = abs_file.relative_to(root_dir).as_posix()

                    # Prevent accidental inclusion of target or temporary archive
                    if abs_file.resolve() in (target_zip.resolve(), temp_zip.resolve()):
                        continue

                    # Check file symlinks for path traversal outside repository
                    if os.path.islink(abs_file):
                        resolved_f = abs_file.resolve()
                        try:
                            resolved_f.relative_to(root_dir)
                        except ValueError:
                            raise ValueError(f"External file symlink rejected (points outside repository): {rel_file}")
                        # Also check if symlink target is forbidden
                        target_rel = resolved_f.relative_to(root_dir).as_posix()
                        if is_forbidden(target_rel):
                            continue

                    if is_forbidden(rel_file):
                        continue

                    zf.write(abs_file, arcname=rel_file)
                    included_count += 1

        # Inspect and verify archive integrity
        corrupted = None
        names = set()
        with zipfile.ZipFile(temp_zip, "r") as zf:
            corrupted = zf.testzip()
            if not corrupted:
                names = set(zf.namelist())

        if corrupted:
            raise RuntimeError(f"Zip integrity test failed! Corrupted entry: {corrupted}")

        # Fail build if any mandatory file is missing
        missing_files = [req for req in check_required if req not in names]
        if missing_files:
            raise ValueError(f"Mandatory release file(s) missing from archive: {', '.join(missing_files)}")

        # Fail build if any forbidden pattern was included
        for name in names:
            if is_forbidden(name):
                raise ValueError(f"Forbidden file leaked into release archive: {name}")

        # Fully atomic replacement: os.replace atomically overwrites the target file
        # on both POSIX and Windows (NTFS), preserving the original file if an error occurs.
        os.replace(temp_zip, target_zip)

        print(f"[+] Release package built cleanly: {target_zip} ({included_count} files, {target_zip.stat().st_size:,} bytes)")
        return str(target_zip)

    finally:
        # Guarantee cleanup of temporary archive on success and failure
        if temp_zip.exists():
            try:
                temp_zip.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    out = build_release_zip()
    print("Done.")
