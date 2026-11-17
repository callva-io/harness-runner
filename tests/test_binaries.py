import os
import time

from callva.harness_runner import binaries, find_binary


def _executable(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_harness_on_path_is_found_without_listing_a_crowded_fallback_folder(
        tmp_path, monkeypatch):
    # fnm leaves one folder per shell that ever ran `fnm env`; a Mac had 19,577.
    shells = tmp_path / "home" / ".local" / "state" / "fnm_multishells"
    for n in range(5000):
        (shells / str(n) / "bin").mkdir(parents=True)
    on_path = _executable(tmp_path / "path-bin", "codex")
    environ = {"PATH": str(on_path.parent), "HOME": str(tmp_path / "home")}
    listed = []
    real_glob = binaries.glob.glob
    monkeypatch.setattr(binaries.glob, "glob", lambda p: listed.append(p) or real_glob(p))

    started = time.perf_counter()
    found = find_binary("codex", environ)
    elapsed = time.perf_counter() - started

    assert found == str(on_path)
    assert listed == []
    assert elapsed < 0.010


def test_a_harness_off_path_is_still_found_in_the_usual_places(tmp_path):
    home = tmp_path / "home"
    in_fallback = _executable(home / ".local" / "bin", "claude")
    environ = {"PATH": os.pathsep.join([str(tmp_path / "empty"), str(tmp_path / "empty")]),
               "HOME": str(home)}
    assert find_binary("claude", environ) == str(in_fallback)
    dirs = binaries.candidate_dirs(environ)
    assert dirs[0] == str(tmp_path / "empty")
    assert len(dirs) == len(set(dirs))
    assert str(home / ".local" / "bin") in dirs
