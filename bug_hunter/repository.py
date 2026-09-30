"""Deterministic, read-only repository indexing for optional cross-file evidence search."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time

from .core import Source

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "node_modules",
    "dist", "build", "bug_hunter_runs",
}

_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
                    ".env")
_SECRET_EXACT = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                 ".npmrc", ".pypirc", ".netrc", "credentials",
                 "secrets.json", "credentials.json"}


def _secret_name(name: str) -> bool:
    """Dotfiles and known secret patterns are never evidence."""
    low = name.lower()
    return (name.startswith(".")
            or low.startswith("id_rsa") or low.startswith("id_ed25519")
            or low.startswith("id_ecdsa") or low.startswith("id_dsa")
            or low.endswith(_SECRET_SUFFIXES) or low in _SECRET_EXACT)


@dataclass(frozen=True)
class ProjectEntry:
    relpath: str
    source: Source


@dataclass(frozen=True)
class ProjectIndex:
    root: str
    entries: tuple[ProjectEntry, ...]
    files_considered: int
    files_loaded: int
    bytes_loaded: int
    limited: bool
    skipped_binary_or_invalid: int
    skipped_secret: int
    skipped_size: int
    limited_file_count: bool
    limited_total_bytes: bool
    unreadable_files: int = 0
    unreadable_directories: int = 0
    limited_line_count: bool = False
    deadline_exceeded: bool = False

    @classmethod
    def read(cls, root, *, primary_path=None, encoding="utf-8-sig",
             max_files=128, max_file_bytes=1048576, max_total_bytes=16777216,
             exclude_paths=(), deadline_at=None):
        """Read bounded regular text files under *root* without following symlinks.

        Files that fail strict decoding, contain NULs, exceed the per-file bound, or
        are not regular files are skipped. Reaching file/total limits is explicit.
        """
        if type(max_files) is not int or not 1 <= max_files <= 10000:
            raise ValueError("invalid_project_file_limit")
        if type(max_file_bytes) is not int or max_file_bytes < 1:
            raise ValueError("invalid_project_file_byte_limit")
        if type(max_total_bytes) is not int or max_total_bytes < 1:
            raise ValueError("invalid_project_total_byte_limit")
        root = Path(root).resolve()
        if not root.is_dir():
            raise ValueError("project_root_must_be_directory")
        primary = Path(primary_path).resolve() if primary_path else None
        excluded = tuple(Path(item).resolve() for item in exclude_paths if item is not None)
        def is_excluded(resolved):
            return any(resolved == item or item in resolved.parents for item in excluded)
        entries, considered, loaded, total, skipped = [], 0, 0, 0, 0
        skipped_secret = skipped_size = 0
        limited_file_count = limited_total_bytes = False
        limited = False
        unreadable_files = unreadable_directories = 0
        limited_line_count = deadline_exceeded = False
        def traversal_error(_error):
            nonlocal unreadable_directories, limited
            unreadable_directories += 1
            limited = True
        def expired():
            return deadline_at is not None and time.monotonic() >= deadline_at
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=traversal_error):
            if expired():
                limited = deadline_exceeded = True
                break
            admitted_dirs = []
            for directory in sorted(dirnames):
                try:
                    if (directory not in _SKIP_DIRS and not directory.startswith(".")
                            and not is_excluded((Path(dirpath) / directory).resolve())):
                        admitted_dirs.append(directory)
                except OSError as exc:
                    traversal_error(exc)
            dirnames[:] = admitted_dirs
            for name in sorted(filenames):
                if expired():
                    limited = deadline_exceeded = True
                    break
                path = Path(dirpath) / name
                considered += 1
                try:
                    if path.is_symlink() or not path.is_file():
                        skipped += 1
                        continue
                    resolved = path.resolve()
                    if primary is not None and resolved == primary:
                        continue
                    if is_excluded(resolved):
                        continue
                    # Secret safety: dotfiles and secret patterns never
                    # become evidence (they would ride to the provider).
                    if _secret_name(name):
                        # Policy exclusion, not a limit hit.
                        skipped_secret += 1
                        continue
                    # Prevent a surprising traversal through a mount/reparse point.
                    try:
                        rel = resolved.relative_to(root).as_posix()
                    except ValueError:
                        skipped += 1
                        continue
                    if loaded >= max_files:
                        limited = limited_file_count = True
                        break
                    remaining = max_total_bytes - total
                    # Source.read hashes and counts the same bytes from one
                    # handle. Pre-read stat values never admit or account data.
                    read_limit = max(1, min(max_file_bytes, remaining))
                    try:
                        source = Source.read(resolved, encoding=encoding,
                                             max_bytes=read_limit,
                                             max_lines=200000)
                    except (UnicodeError, LookupError):
                        skipped += 1
                        continue
                    except ValueError as exc:
                        if str(exc) == "input_too_large":
                            limited = True
                            if remaining < max_file_bytes:
                                limited_total_bytes = True
                            else:
                                skipped_size += 1
                        elif str(exc) == "too_many_lines":
                            limited = limited_line_count = True
                        else:
                            skipped += 1
                        continue
                    actual = source.byte_count
                    if actual is None:
                        raise ValueError("missing_source_byte_count")
                    if actual > remaining:
                        limited = limited_total_bytes = True
                        continue
                    entries.append(ProjectEntry(rel, source))
                    loaded += 1
                    total += actual
                except OSError:
                    unreadable_files += 1
                    limited = True
            if limited_file_count or limited_total_bytes or deadline_exceeded:
                break
        if expired():
            limited = deadline_exceeded = True
        return cls(str(root), tuple(entries), considered, loaded, total,
                   limited, skipped, skipped_secret, skipped_size,
                   limited_file_count, limited_total_bytes, unreadable_files,
                   unreadable_directories, limited_line_count, deadline_exceeded)
