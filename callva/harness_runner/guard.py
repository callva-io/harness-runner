"""The harness guard: which harness CLI versions this release was tested with.

The SDKs pin nothing about the CLI they are pointed at, so a machine can move
its CLI past what this library knows. Before every turn the installed CLI's
version is read and held against the range below: older than the oldest tested
version refuses the turn, newer than the newest runs it with a warning.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .binaries import version_line

# The inclusive range of CLI versions this release was tested with, per harness.
# The low end is the CLI installed where the release was tested; the high end
# is the newer of the CLI the pinned SDK bundles, which is what that SDK release
# was built against, and an installed CLI a live run of this release passed on.
TESTED_VERSIONS: dict[str, tuple[str, str]] = {
    "claude": ("2.1.280", "2.1.310"),
    "codex": ("0.159.0", "0.162.1"),
}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


@dataclass(frozen=True)
class Verdict:
    version: str | None
    refusal: str | None = None
    warning: str | None = None


_cache: dict[tuple[str, int, int], tuple[str | None, str | None]] = {}


def _read(path: str, environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    try:
        stat = os.stat(path)
        key = (os.path.realpath(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None and key in _cache:
        return _cache[key]
    line, error = version_line(path, environ)
    if key is not None and line is not None:
        _cache[key] = (line, error)
    return line, error


def check(harness: str, path: str, environ: Mapping[str, str]) -> Verdict:
    """Hold the CLI at `path` against the tested range for `harness`."""
    low, high = TESTED_VERSIONS[harness]
    line, error = _read(path, environ)
    found = parse_version(line)
    if found is None:
        said = line if line is not None else error
        return Verdict(None, refusal=(
            f"could not read the version of {harness} at {path} ({said}); "
            f"this release was tested with {harness} {low} to {high}"))
    version = ".".join(str(part) for part in found)
    if found < parse_version(low):
        return Verdict(version, refusal=(
            f"{harness} {version} at {path} is older than {low}, the oldest version this "
            f"release was tested with (tested {low} to {high})"))
    if found > parse_version(high):
        return Verdict(version, warning=(
            f"{harness} {version} at {path} is newer than {high}, the newest version this "
            f"release was tested with (tested {low} to {high}); the turn ran anyway"))
    return Verdict(version)
