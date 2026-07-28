"""A class: one exercise, one clock, up to a roomful of students.

Stage 1 of the multi-user simulator, and it overturns a design decision that
held for as long as this was a self-study trainer.

## The clock moves to the server

:mod:`~wellcontrol.sim.session` advanced time *on demand*: each request measured
the wall time since the last one and stepped the engine that far. For one person
at one screen that is right -- nothing runs when nobody is watching, and every
test is deterministic without sleeping.

It does not survive a class. Three ways it breaks:

* An instructor's *pause all* has to stop the wells, not the browsers.
* A student reading their kill sheet, or whose laptop slept, has a **frozen
  well** -- and then a lurch when they come back.
* The instructor's monitor stepping fifteen engines races each student's own
  polling for the same engine.

So a :class:`Classroom` owns a clock and drives every session on it, whether or
not anyone is looking. What is kept from the old design is the part that made it
testable: the clock is still *injected*, this module still has **no web
dependency**, and :meth:`Classroom.tick` is an ordinary method a test calls in a
loop. The asyncio task that calls it in production lives in the API layer, which
is where runtime plumbing belongs.

## Speed is the instructor's, with one exception

The class runs at one speed because a class is flown together. The exception is
:meth:`Session._should_slow_down`, which drops a single student to real time
when *their* well starts flowing -- that is a safety behaviour giving back the
seconds a driller at the panel would have had, and it should not wait for the
instructor to notice. Setting the class speed afterwards overrides it, which is
correct: the instructor is in charge.

## Students are not accounts

A student is a name, a slot and a session, alive for as long as the class is.
See :mod:`~wellcontrol.sim.auth` for why there is no user table.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .auth import class_code, normalise_code
from .session import Session

#: How long a class lives unless told otherwise. Long enough for a teaching day.
DEFAULT_CLASS_TTL_S: float = 12 * 3600

#: Wall seconds between engine ticks. The engine integrates at STEP_S = 0.05 s
#: internally; this is only how often the driver hands it elapsed wall time, so
#: it costs nothing to keep well under the ~0.25 s a gauge needs to feel live.
TICK_S: float = 0.1

#: Refuse to admit more than this. Not a licence check -- a guard, so a leaked
#: code cannot quietly enrol two hundred wells and stall the class.
DEFAULT_CAPACITY: int = 30


@dataclass
class Student:
    """One seat: a name, a well, and when they were last actually looking.

    Args:
        name: What the instructor sees. Not unique and not a credential.
        session: Their own well. Every student in a class flies the same
            exercise, but each has a private engine -- they are not sharing a
            well and cannot see each other's.
    """

    name: str
    session: Session
    student_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    joined_at: float = 0.0
    #: Wall clock at their last poll. The monitor greys a tile that has gone
    #: quiet, which is how an instructor spots a student who has walked off or
    #: whose browser died -- distinct from one whose well is simply idle.
    last_seen: float = 0.0

    def summary(self) -> dict:
        return {
            "student_id": self.student_id,
            "name": self.name,
            "joined_at": self.joined_at,
            "last_seen": self.last_seen,
        }


@dataclass
class Classroom:
    """One exercise, run for a roomful of students at once.

    Args:
        name: What the instructor called it. Free text.
        scenario: Key of a named exercise. When set it decides the well, the
            lineup and the faults for every student in the class.
        bop / trajectory / start: Used only when ``scenario`` is None.
        speed: Simulated seconds per wall second, for the whole class. Starts
            at 0 -- a class begins paused, so students can join and get their
            bearings before the well starts moving.
        clock: Injected. Monotonic in production, a fake in tests.
    """

    name: str = "Class"
    scenario: str | None = None
    bop: str = "surface"
    trajectory: str = "vertical"
    start: str = "shut_in"
    speed: float = 0.0
    capacity: int = DEFAULT_CAPACITY
    ttl_s: float = DEFAULT_CLASS_TTL_S
    clock: Callable[[], float] = time.monotonic
    classroom_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    code: str = field(default_factory=class_code)
    students: dict[str, Student] = field(default_factory=dict)
    created_at: float = field(default=0.0)
    #: Speed to go back to on resume. Remembered so *pause all* is reversible
    #: without the instructor having to recall what it was running at.
    _resume_speed: float = field(default=1.0, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = self.clock()
        self._validate_exercise()

    def _validate_exercise(self) -> None:
        """Reject an unflyable exercise now, not when the room fills.

        Sessions are built per student at join time, so an unknown scenario key
        or a bad trajectory would otherwise sail through class creation and
        surface as an error thrown at the *first student who joins* -- with the
        instructor having already read the code out. Resolving it here costs
        nothing and puts the failure where it can still be fixed.

        Raises:
            KeyError: no such scenario.
            ValueError: no such BOP location, trajectory or start state.
        """
        from ..wellconfig import BOPLocation, Trajectory, WellConfiguration
        from .engine import StartState
        from .scenario import get_scenario

        if self.scenario:
            get_scenario(self.scenario)
            return
        WellConfiguration(BOPLocation(self.bop), Trajectory(self.trajectory))
        StartState(self.start)

    # -- lifecycle -----------------------------------------------------------

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_s

    @property
    def expired(self) -> bool:
        return self.clock() >= self.expires_at

    @property
    def open(self) -> bool:
        """Whether the code still admits anybody."""
        return not self._closed and not self.expired

    def close(self) -> None:
        """Stop admitting students. Those already in keep their wells."""
        self._closed = True

    def matches(self, typed_code: str) -> bool:
        return normalise_code(typed_code) == normalise_code(self.code)

    # -- roster --------------------------------------------------------------

    def join(self, name: str) -> Student:
        """Admit a student and build them a well.

        Raises:
            ValueError: if the class is closed, expired or full.
        """
        if not self.open:
            raise ValueError("That class is no longer open.")
        if len(self.students) >= self.capacity:
            raise ValueError("That class is full.")

        name = name.strip() or "Unnamed"
        session = self._new_session()
        # Joining a class already under way picks up its speed, so a late
        # arrival is not sitting on a frozen well waiting for the instructor.
        session.speed = self.speed
        now = self.clock()
        student = Student(
            name=name, session=session, joined_at=now, last_seen=now
        )
        self.students[student.student_id] = student
        return student

    def _new_session(self) -> Session:
        if self.scenario:
            return Session.for_scenario(self.scenario, clock=self.clock)
        return Session.for_configuration(
            bop=self.bop,
            trajectory=self.trajectory,
            start=self.start,
            clock=self.clock,
        )

    def student(self, student_id: str) -> Student:
        """Raises KeyError if there is no such student in this class."""
        if student_id not in self.students:
            raise KeyError(f"No student '{student_id}' in this class")
        return self.students[student_id]

    def remove(self, student_id: str) -> None:
        """Drop a student. Their token stops working, because the roster is
        checked on every request rather than trusted from the token alone."""
        self.students.pop(student_id, None)

    def touch(self, student_id: str) -> None:
        """Record that a student's screen just polled."""
        student = self.students.get(student_id)
        if student is not None:
            student.last_seen = self.clock()

    def restart(self, student_id: str) -> Student:
        """Give one student a fresh well on the same exercise.

        Wanted more often than it sounds: a student who breaks down the shoe in
        the first two minutes has nothing left to learn from that well, and
        should not sit out the rest of the lesson.
        """
        student = self.student(student_id)
        student.session = self._new_session()
        student.session.speed = self.speed
        return student

    # -- the instructor's hand -----------------------------------------------

    def inject_fault(
        self,
        kind: str,
        student_id: str | None = None,
        message: str = "",
    ) -> list[str]:
        """Break something, now, for one student or for the whole room.

        Returns the ids it was applied to.

        **Nothing about this reaches the student as an instructor action.** It
        goes through the same engine call as a fault written into the exercise,
        and the log entry is the one the rig would print. A student who can
        tell the instructor just did something to them starts flying the
        instructor rather than the well.

        Raises:
            KeyError: no such student, or no such fault kind.
        """
        from .scenario import Fault, FaultKind

        fault = Fault(kind=FaultKind(kind), message=message)
        targets = (
            [self.student(student_id)] if student_id is not None
            else list(self.students.values())
        )
        for student in targets:
            student.session.simulator.inject_fault(fault)
        return [student.student_id for student in targets]

    # -- the clock -----------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self.speed <= 0.0

    def tick(self) -> None:
        """Advance every student's well to the present.

        Called on a timer by whatever is driving the process. Cheap when the
        class is paused, because each session's own advance returns immediately.
        """
        for student in self.students.values():
            student.session.advance()

    def set_speed(self, speed: float) -> None:
        """Set the speed for the whole class.

        Each session banks the time it has already run at the old speed before
        changing, so seconds elapsed at 1x are never re-integrated at 10x.
        """
        speed = max(0.0, float(speed))
        if speed > 0:
            self._resume_speed = speed
        self.speed = speed
        for student in self.students.values():
            student.session.set_speed(speed)

    def pause(self) -> None:
        """Stop every well, remembering the speed to resume at."""
        if not self.paused:
            self._resume_speed = self.speed
        self.set_speed(0.0)

    def resume(self) -> None:
        self.set_speed(self._resume_speed)

    # -- what the instructor sees -------------------------------------------

    def monitor(self, quiet_after_s: float = 15.0) -> dict:
        """The grid: one tile per student, plus the state of the class.

        Args:
            quiet_after_s: A student whose screen has not polled in this long
                is flagged. Distinguishes "gone" from "sitting on a dead well",
                which look identical from the numbers alone.
        """
        now = self.clock()
        tiles = []
        for student in self.students.values():
            tile = student.session.instructor_view()
            tile.update({
                "student_id": student.student_id,
                "name": student.name,
                "quiet_s": round(now - student.last_seen, 1),
                "connected": (now - student.last_seen) < quiet_after_s,
            })
            tiles.append(tile)
        tiles.sort(key=lambda t: t["name"].lower())
        return {"classroom": self.summary(), "students": tiles}

    def summary(self, *, include_code: bool = True) -> dict:
        """The class itself. ``include_code`` is False for anything a student
        can see -- there is no reason to hand the code back to the room."""
        summary = {
            "classroom_id": self.classroom_id,
            "name": self.name,
            "scenario": self.scenario,
            "bop": self.bop,
            "trajectory": self.trajectory,
            "start": self.start,
            "speed": self.speed,
            "paused": self.paused,
            "open": self.open,
            "student_count": len(self.students),
            "capacity": self.capacity,
            "seconds_remaining": max(0.0, round(self.expires_at - self.clock())),
        }
        if include_code:
            summary["code"] = self.code
        return summary


class ClassroomStore:
    """Every live class, keyed by id, with lookup by code.

    In memory. A class is a thing that happens on an afternoon, and the price
    of a restart is that everyone rejoins -- which is acceptable in a way that
    losing a half-flown kill is not, so recordings persist separately.

    ## Why there is a lock

    The engines now have **two** callers that were previously one: the clock
    driver, and whatever request a student's screen just made. Those genuinely
    run at the same time -- a sync FastAPI handler executes in a threadpool
    worker while the driver runs on the event loop -- so without a lock two
    threads can be inside the same :class:`Session` at once. The cheap failure
    is elapsed time counted twice, because both read ``_last_tick`` before
    either writes it; the expensive one is a half-rebuilt fluid column.

    One lock for every class rather than one each. Stepping fifteen wells takes
    well under a millisecond, so contention is not the constraint, and a single
    lock cannot be acquired in the wrong order.

    **Never hold it across an ``await``.** The driver takes it on the event loop
    thread, so a coroutine that suspends while holding it would block the loop
    that has to resume the handler holding the other end -- a deadlock rather
    than a slowdown. Everything that takes it is synchronous and stays that way.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._classrooms: dict[str, Classroom] = {}
        self._lock = threading.RLock()

    def exclusive(self):
        """Hold while touching any engine. See the class docstring."""
        return self._lock

    def create(self, **kwargs) -> Classroom:
        kwargs.setdefault("clock", self.clock)
        classroom = Classroom(**kwargs)
        self._classrooms[classroom.classroom_id] = classroom
        return classroom

    def get(self, classroom_id: str) -> Classroom:
        if classroom_id not in self._classrooms:
            raise KeyError(f"No class '{classroom_id}'")
        return self._classrooms[classroom_id]

    def by_code(self, typed_code: str) -> Classroom:
        """Find an *open* class by the code a student typed.

        Expired and closed classes are not found rather than reported, so a
        stale code cannot be distinguished from a wrong one by someone probing.
        """
        for classroom in self._classrooms.values():
            if classroom.open and classroom.matches(typed_code):
                return classroom
        raise KeyError("No open class with that code")

    def drop(self, classroom_id: str) -> None:
        self._classrooms.pop(classroom_id, None)

    def purge_expired(self) -> int:
        """Forget classes past their expiry. Returns how many went."""
        dead = [
            cid for cid, classroom in self._classrooms.items()
            if classroom.expired
        ]
        for cid in dead:
            del self._classrooms[cid]
        return len(dead)

    def tick_all(self) -> None:
        """Advance every well in every class. The driver calls this."""
        with self._lock:
            for classroom in self._classrooms.values():
                classroom.tick()

    @property
    def all(self) -> list[Classroom]:
        return list(self._classrooms.values())

    def __len__(self) -> int:
        return len(self._classrooms)
