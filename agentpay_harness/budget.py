"""Shared budget store and deterministic interleaving for multi-agent races.

When a scenario runs several agent instances (``agents: 2``), they all spend
from one :class:`SharedBudget`.  An ``interleave`` fault adds an
:class:`InterleavingScheduler`: every budget operation is a checkpoint, and the
scheduler decides which agent's thread may pass each checkpoint, in a seeded or
explicit order.  That turns "might race" into a reproducible race.

A correct agent uses :meth:`SharedBudget.try_reserve` (check-and-decrement in
one step).  A racy agent uses :meth:`SharedBudget.read` then
:meth:`SharedBudget.write`, and an interleaving like ``[0, 1, 0, 1]`` makes both
agents read the same balance before either writes.
"""

from __future__ import annotations

import logging
import random
import threading

logger = logging.getLogger(__name__)


class InterleavingScheduler:
    """Lets agent threads through budget checkpoints in a fixed order.

    ``schedule`` lists agent indices; the n-th checkpoint reached (by anyone)
    is granted to ``schedule[n]``.  Turns belonging to an agent that has
    finished are skipped.  Once the schedule is used up, threads run freely.
    """

    def __init__(self, schedule: list[int], n_agents: int, timeout_s: float = 5.0) -> None:
        self.schedule = list(schedule)
        self.n_agents = n_agents
        self.timeout_s = timeout_s
        self._pos = 0
        self._done: set[int] = set()
        self._cond = threading.Condition()
        self._local = threading.local()
        self.trace: list[int] = []

    @classmethod
    def seeded(cls, seed: int, n_agents: int, turns: int = 8) -> InterleavingScheduler:
        """A schedule that alternates agents in a seeded random order."""
        rng = random.Random(seed)  # noqa: S311 - deterministic scheduling, not crypto
        order = [i % n_agents for i in range(turns)]
        rng.shuffle(order)
        return cls(order, n_agents)

    def bind(self, agent_index: int) -> None:
        """Declare which agent the calling thread runs."""
        self._local.agent = agent_index

    def finish(self, agent_index: int) -> None:
        """Mark an agent as done so its remaining turns are skipped."""
        with self._cond:
            self._done.add(agent_index)
            self._skip_finished()
            self._cond.notify_all()

    def _skip_finished(self) -> None:
        while self._pos < len(self.schedule) and self.schedule[self._pos] in self._done:
            self._pos += 1

    def checkpoint(self) -> None:
        """Block until it is this thread's agent's turn (or the schedule is exhausted)."""
        me = getattr(self._local, "agent", None)
        if me is None:
            return
        with self._cond:
            self._skip_finished()
            ok = self._cond.wait_for(
                lambda: self._pos >= len(self.schedule) or self.schedule[self._pos] == me,
                timeout=self.timeout_s,
            )
            if not ok:
                logger.warning(
                    "Interleaving schedule stalled at turn %d; releasing all agents", self._pos
                )
                self._pos = len(self.schedule)
            elif self._pos < len(self.schedule):
                self._pos += 1
                self._skip_finished()
            self.trace.append(me)
            self._cond.notify_all()


class SharedBudget:
    """A budget in cents that several agent instances spend from."""

    def __init__(self, total_cents: int, scheduler: InterleavingScheduler | None = None) -> None:
        self.total_cents = total_cents
        self.scheduler = scheduler
        self._spent = 0
        self._lock = threading.Lock()

    def _checkpoint(self) -> None:
        if self.scheduler is not None:
            self.scheduler.checkpoint()

    @property
    def spent_cents(self) -> int:
        with self._lock:
            return self._spent

    def try_reserve(self, amount_cents: int) -> bool:
        """Atomically reserve ``amount_cents`` if it fits.  The correct way to spend."""
        self._checkpoint()
        with self._lock:
            if self._spent + amount_cents > self.total_cents:
                return False
            self._spent += amount_cents
            return True

    def release(self, amount_cents: int) -> None:
        """Give back a reservation (failed or canceled payment)."""
        with self._lock:
            self._spent = max(0, self._spent - amount_cents)

    def read(self) -> int:
        """Read the amount spent so far (first half of a racy read-modify-write)."""
        self._checkpoint()
        with self._lock:
            return self._spent

    def write(self, spent_cents: int) -> None:
        """Overwrite the amount spent (second half of a racy read-modify-write)."""
        self._checkpoint()
        with self._lock:
            self._spent = spent_cents
