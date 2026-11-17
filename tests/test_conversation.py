import asyncio
import importlib
import itertools
import os
import threading
import time
from pathlib import Path

import pytest
from conftest import fake
from test_deadline import TreeClock, wait_for

from callva.harness_runner import FailureKind, Profile, Started, open_conversation

runner = importlib.import_module("callva.harness_runner.run")


def claude(**knobs) -> Profile:
    return Profile(harness="claude", **{"cli_path": fake("claude"), "timeout_seconds": 20, **knobs})


def ended(pid: int) -> bool:
    """Gone, or a zombie nobody has reaped yet."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    status = Path(f"/proc/{pid}/status")
    return status.exists() and "\nState:\tZ" in status.read_text()


def test_turns_share_one_process_from_worker_threads_and_close_ends_it(fake_env, tmp_path):
    env = {**fake_env, "FAKE_CLAUDE": "remember"}
    started: list[Started] = []
    events: list[dict] = []
    ticks: list[float] = []

    async def caller():
        # The caller's own loop keeps running while the conversation opens and answers.
        async def tick():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(tick())
        conversation = await asyncio.to_thread(open_conversation, claude(), tmp_path,
                                               environ=env, on_start=started.append)
        first = await asyncio.to_thread(conversation.send, "first", on_event=events.append)
        second = await asyncio.to_thread(conversation.send, "second")
        ticks.append(time.monotonic())
        ticker.cancel()
        return conversation, first, second

    conversation, first, second = asyncio.run(caller())
    assert max(b - a for a, b in itertools.pairwise(ticks)) < 1.0
    assert conversation.is_open and conversation.failure is None
    assert first.ok and second.ok, (first.failure, second.failure)
    assert first.answer.startswith("answer to: first; heard before: []")
    # The second turn reached the same process, which remembers the first.
    assert second.answer.startswith("answer to: second; heard before: ['first']")
    pid = started[0].pid
    assert len(started) == 1 and started[0].harness == "claude"
    assert first.answer.endswith(f"pid {pid}") and second.answer.endswith(f"pid {pid}")
    assert first.session_id == second.session_id == started[0].session_id
    assert conversation.session_id == first.session_id
    assert second.tokens.input == 100 and second.model == "claude-fake-1"
    # Each turn's cost is its own, though the process reports a running total.
    assert second.raw["total_cost_usd"] == pytest.approx(0.0246)
    assert first.cost_usd == pytest.approx(0.0123) and second.cost_usd == pytest.approx(0.0123)
    assert any(e.get("subtype") == "init" for e in events)

    conversation.close()
    assert not conversation.is_open
    deadline = time.monotonic() + 10
    while not ended(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ended(pid)
    conversation.close()
    after = conversation.send("third")
    assert not after.ok and after.failure.kind == FailureKind.ERROR
    assert "closed" in after.failure.message


def test_turns_sent_at_once_run_one_after_another(fake_env, tmp_path):
    with open_conversation(claude(), tmp_path,
                           environ={**fake_env, "FAKE_CLAUDE": "remember"}) as conversation:
        results = {}

        def send(prompt):
            results[prompt] = conversation.send(prompt)

        threads = [threading.Thread(target=send, args=(p,)) for p in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    answers = sorted(r.answer.split("; pid")[0] for r in results.values())
    assert answers in (
        ["answer to: a; heard before: []", "answer to: b; heard before: ['a']"],
        ["answer to: a; heard before: ['b']", "answer to: b; heard before: []"],
    )


def test_cancel_ends_the_turn_and_the_conversation(fake_env, tmp_path):
    conversation = open_conversation(claude(timeout_seconds=180), tmp_path,
                                     environ={**fake_env, "FAKE_CLAUDE": "remember"})
    assert conversation.send("first").ok
    cancel = threading.Event()

    def later():
        wait_for(fake_env["FAKE_PID_FILE"])
        cancel.set()

    threading.Thread(target=later).start()
    result = conversation.send("hang", cancel=cancel)
    assert result.failure.kind == FailureKind.CANCELLED
    assert result.session_id == conversation.session_id
    assert not conversation.is_open
    after = conversation.send("again")
    assert after.failure.kind == FailureKind.ERROR and "cancelled" in after.failure.message
    conversation.close()
    conversation.close()


def test_a_turn_past_the_profile_deadline_times_out(fake_env, tmp_path, monkeypatch):
    clock = TreeClock(fake_env["FAKE_PID_FILE"])
    monkeypatch.setattr(runner, "time", clock)
    conversation = open_conversation(claude(timeout_seconds=3), tmp_path,
                                     environ={**fake_env, "FAKE_CLAUDE": "remember"})
    assert conversation.send("first").ok
    result = conversation.send("hang")
    assert clock.tree_at is not None
    assert result.failure.kind == FailureKind.TIMEOUT
    assert "timed out after 3s" in result.failure.message
    assert time.monotonic() - clock.tree_at < 15
    assert not conversation.is_open
    conversation.close()


def test_a_harness_that_dies_at_start_is_a_failed_open(fake_env, tmp_path):
    conversation = open_conversation(claude(), tmp_path,
                                     environ={**fake_env, "FAKE_CLAUDE": "crash"})
    assert not conversation.is_open and conversation.failure is not None
    result = conversation.send("x")
    assert not result.ok and result.failure == conversation.failure
    conversation.close()
    conversation.close()


def test_codex_is_refused_at_open(fake_env, tmp_path):
    with pytest.raises(ValueError, match="claude only, not codex"):
        open_conversation(Profile(harness="codex", cli_path=fake("codex")), tmp_path,
                          environ=fake_env)
