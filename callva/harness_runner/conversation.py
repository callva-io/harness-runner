"""A conversation kept open across turns: one harness process, one turn after another."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import tree
from .classify import classify
from .profile import Profile, Session
from .result import Failure, FailureKind, Result, Started
from .run import (
    WIND_DOWN_SECONDS,
    _claude_ids,
    _claude_result,
    _prepare,
    _StartNotice,
    _supervise,
    _tail,
    _unreported,
)


class Conversation:
    """One harness process kept open between turns. Made by `open_conversation`.

    `send` runs one turn and returns its `Result`; turns are serialised, and each may be
    sent from any thread. `close` ends the process. A conversation that did not open
    carries the reason in `failure`, and every turn sent into it returns that failure.
    """

    def __init__(self, profile: Profile, *, stderr_tail: int) -> None:
        self.profile = profile
        self.failure: Failure | None = None
        self.session_id: str | None = None
        self.harness_version: str | None = None
        self.warnings: tuple[str, ...] = ()
        self._stderr_tail = stderr_tail
        self._stderr: deque = deque(maxlen=200)
        self._resume_id: str | None = None
        self._launch: Any = None
        self._context: dict = {}
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ended: Failure | None = None
        self._cost_so_far = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        """True while a turn sent now would reach the harness."""
        return self.failure is None and self._ended is None and self._client is not None

    def send(
        self,
        prompt: str,
        *,
        on_event: Callable[[dict], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Result:
        """Run one turn in the conversation and return what came of it.

        Never raises for anything the harness did. The turn has the profile's
        `timeout_seconds`; `on_event` and `cancel` are as `run` takes them. `cost_usd` is
        this turn's cost: claude's running total for the process, in `raw`, less what it was
        before the turn. A turn that times out or is cancelled kills the harness's tree, as
        `run` does, and ends the conversation, as does a turn that ends without the
        harness's report: every later turn returns a failure saying so, and `is_open` is
        false.
        """
        with self._lock:
            if not self.is_open:
                failure = self.failure or self._ended or Failure(
                    FailureKind.ERROR, "the conversation is closed")
                return Result(ok=False, harness="claude", failure=failure,
                              session_id=self.session_id,
                              harness_version=self.harness_version, warnings=self.warnings)
            from . import claude

            outcome = claude.Outcome(stderr=self._stderr)
            pending: list = []

            def work() -> None:
                future = asyncio.run_coroutine_threadsafe(
                    claude.client_turn(self._client, prompt, outcome, on_event), self._loop)
                pending.append(future)
                with contextlib.suppress(BaseException):
                    future.result()

            def unblock() -> None:
                for future in pending:
                    future.cancel()

            started = time.monotonic()
            ending = _supervise(work, self._launch, timeout=self.profile.timeout_seconds,
                                cancel=cancel, on_stop=unblock)
            elapsed = int((time.monotonic() - started) * 1000)
            result = _claude_result(self.profile, self._launch, outcome, ending,
                                    session_id=self.session_id, resume_id=self._resume_id,
                                    elapsed=elapsed, stderr_tail=self._stderr_tail,
                                    context=self._context)
            if result.cost_usd is not None:
                # Claude reports the process's running total; a turn's Result holds its own.
                total = result.cost_usd
                result = dataclasses.replace(result, cost_usd=total - self._cost_so_far)
                self._cost_so_far = total
            if ending.stopped or outcome.result is None:
                # A stopped turn's tree was killed; a turn without a report lost its harness.
                what = ("was cancelled" if ending.cancelled else "timed out" if ending.stopped
                        else "ended without the harness's report")
                self._ended = Failure(FailureKind.ERROR,
                                      f"the conversation ended when a turn {what}")
                self._shut()
            return result

    def close(self) -> None:
        """End the harness process and everything it started. Closing again, or closing a
        conversation that did not open, does nothing. A turn still running is let finish
        first; set its `cancel` to end it sooner."""
        with self._lock:
            if self._ended is None and self.failure is None:
                self._ended = Failure(FailureKind.ERROR, "the conversation is closed")
            self._shut()

    def __enter__(self) -> Conversation:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _shut(self) -> None:
        """Disconnect the client, kill what is left of the tree, stop the loop, and remove
        the launcher. Each step runs once."""
        loop, client, launch = self._loop, self._client, self._launch
        self._client = None
        if loop is not None and client is not None:
            with contextlib.suppress(BaseException):
                asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(
                    WIND_DOWN_SECONDS)
        if launch is not None:
            tree.kill_tree(launch.stop())
        self._loop = None
        if loop is not None:
            with contextlib.suppress(BaseException):
                asyncio.run_coroutine_threadsafe(_drain(), loop).result(WIND_DOWN_SECONDS)
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(WIND_DOWN_SECONDS)
            if not loop.is_running():
                loop.close()
        self._thread = None
        if launch is not None:
            launch.close()
            self._launch = None


async def _drain() -> None:
    """Cancel what the SDK left running on the conversation's loop."""
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.get_running_loop().shutdown_asyncgens()


def open_conversation(
    profile: Profile,
    cwd: str | os.PathLike[str],
    *,
    session: Session | None = None,
    environ: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    stderr_tail: int = 40,
    on_start: Callable[[Started], None] | None = None,
) -> Conversation:
    """Start one harness process and keep it open for turns sent with `Conversation.send`.

    Claude only: a codex profile raises ValueError. Never raises for anything the harness
    did: a conversation that could not open has `failure` set, and `is_open` false.
    `session`, `environ`, `extra_env` and `on_start` are as `run` takes them; `on_start` is
    called once, while the conversation opens. Opening has the profile's
    `timeout_seconds`. The conversation runs on an event loop of its own on a thread of
    its own, so it is used from synchronous code on any thread and never touches the
    caller's event loop; a caller on an event loop sends from a worker thread.
    """
    if profile.harness != "claude":
        raise ValueError(
            f"a kept-open conversation runs on claude only, not {profile.harness}; "
            "run each turn with run() and Session.resume(id)")
    conversation = Conversation(profile, stderr_tail=stderr_tail)
    prepared = _prepare(profile, environ, extra_env)
    if isinstance(prepared, Result):
        conversation.failure = prepared.failure
        conversation.harness_version = prepared.harness_version
        return conversation
    launch, env, context = prepared
    conversation._launch, conversation._context = launch, context
    conversation.harness_version = context["harness_version"]
    conversation.warnings = context["warnings"]
    session_id, resume_id = _claude_ids(session or Session.fresh())
    conversation.session_id = session_id or resume_id
    conversation._resume_id = resume_id

    from . import claude

    opts = claude.options(profile, cli_path=str(launch.wrapper), cwd=str(Path(cwd)), env=env,
                          session_id=session_id, resume_id=resume_id,
                          stderr=conversation._stderr.append)
    client = claude.client(opts)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="harness-conversation",
                              daemon=True)
    thread.start()
    conversation._loop, conversation._thread = loop, thread
    notice = _StartNotice("claude", launch, on_start, lambda: conversation.session_id)
    errors: list[BaseException] = []
    pending: list = []

    def work() -> None:
        future = asyncio.run_coroutine_threadsafe(client.connect(), loop)
        pending.append(future)
        try:
            future.result()
        except BaseException as exc:  # every ending is read, none is raised
            errors.append(exc)

    def unblock() -> None:
        for future in pending:
            future.cancel()

    ending = _supervise(work, launch, timeout=profile.timeout_seconds, cancel=None,
                        on_stop=unblock, tick=notice)
    notice(final=True)
    if ending.stopped:
        conversation.failure = Failure(
            classify(None, timed_out=True),
            f"the conversation did not open within {profile.timeout_seconds:g}s")
    elif errors:
        message = _unreported(errors[0], _tail(conversation._stderr, stderr_tail))
        conversation.failure = Failure(classify(message, had_report=False), message)
    if conversation.failure is not None:
        conversation._shut()
    else:
        conversation._client = client
    return conversation


__all__ = ["Conversation", "open_conversation"]
