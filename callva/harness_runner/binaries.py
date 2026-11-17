"""Find a harness outside a login shell, and ask it what it is."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from .renamed import refuse_attribute

_FALLBACK_GLOBS = (
    "~/.local/bin",
    "~/.local/state/fnm_multishells/*/bin",
    "~/.fnm/node-versions/*/installation/bin",
    "~/.local/share/fnm/node-versions/*/installation/bin",
    "~/.nvm/versions/node/*/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def _iter_candidate_dirs(environ: Mapping[str, str] | None = None):
    """PATH first, then the usual places, each once and in order. Lazy, so a
    lookup that PATH answers never lists a fallback folder: one of them, fnm's
    per-shell folders, can hold tens of thousands of entries."""
    env = os.environ if environ is None else environ
    seen = set()
    for directory in (env.get("PATH") or "").split(os.pathsep):
        if directory and directory not in seen:
            seen.add(directory)
            yield directory
    home = env.get("HOME") or os.path.expanduser("~")
    for pattern in _FALLBACK_GLOBS:
        expanded = pattern.replace("~", home, 1) if pattern.startswith("~") else pattern
        for path in sorted(glob.glob(expanded), reverse=True):
            if path not in seen:
                seen.add(path)
                yield path


def candidate_dirs(environ: Mapping[str, str] | None = None) -> list[str]:
    return list(_iter_candidate_dirs(environ))


def find_binary(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    for directory in _iter_candidate_dirs(environ):
        found = shutil.which(name, path=directory)
        if found:
            return found
    return None


@dataclass(frozen=True)
class Probe:
    harness: str
    found: bool
    path: str | None = None
    version: str | None = None
    error: str | None = None

    def __getattr__(self, name: str):
        raise refuse_attribute("Probe", name)


def version_line(path: str, environ: Mapping[str, str] | None = None,
                 timeout: float = 20.0) -> tuple[str | None, str | None]:
    """The first line `path --version` prints, or why there is none."""
    try:
        done = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=timeout,
                              env=dict(os.environ if environ is None else environ))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if done.returncode != 0:
        return None, (done.stderr or done.stdout).strip()[-300:] or f"exit {done.returncode}"
    text = (done.stdout or done.stderr).strip()
    if not text:
        return None, "printed no version"
    return text.splitlines()[0], None


def probe(harness: str, environ: Mapping[str, str] | None = None, timeout: float = 20.0) -> Probe:
    """Whether the harness is present, and what version answers."""
    path = find_binary(harness, environ)
    if not path:
        return Probe(harness, False,
                     error=f"no {harness} executable on PATH or in the usual places")
    version, error = version_line(path, environ, timeout)
    return Probe(harness, True, path=path, version=version, error=error)
