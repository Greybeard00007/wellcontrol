"""A class of students on one server clock.

The whole point of :mod:`wellcontrol.sim.classroom` is that wells advance
whether or not anybody is looking at them, so these tests drive the clock the
way the production driver does -- in ``TICK_S`` increments -- rather than
jumping it, which would run into the catch-up clamp and test something else.
"""

import pytest

from wellcontrol.sim.classroom import TICK_S, Classroom, ClassroomStore


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def run(classroom: Classroom, wall_seconds: float, clock: FakeClock) -> None:
    """Drive a class for some wall time exactly as the server does."""
    for _ in range(int(wall_seconds / TICK_S)):
        clock.tick(TICK_S)
        classroom.tick()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def classroom(clock):
    return Classroom(name="Well Control 101", clock=clock)


class TestRoster:
    def test_joining_builds_a_well(self, classroom):
        student = classroom.join("Brian")
        assert student.name == "Brian"
        assert student.session is not None
        assert classroom.students[student.student_id] is student

    def test_each_student_gets_a_private_well(self, classroom):
        """Same exercise, separate engines. They are not sharing a well."""
        a = classroom.join("A")
        b = classroom.join("B")
        assert a.session is not b.session
        assert a.session.simulator is not b.session.simulator

    def test_capacity_is_enforced(self, clock):
        """A leaked code should not be able to enrol two hundred wells."""
        classroom = Classroom(capacity=2, clock=clock)
        classroom.join("A")
        classroom.join("B")
        with pytest.raises(ValueError, match="full"):
            classroom.join("C")

    def test_closed_class_refuses(self, classroom):
        classroom.close()
        with pytest.raises(ValueError, match="no longer open"):
            classroom.join("Late")

    def test_expired_class_refuses(self, clock):
        classroom = Classroom(ttl_s=10, clock=clock)
        clock.tick(11)
        assert classroom.expired
        with pytest.raises(ValueError, match="no longer open"):
            classroom.join("Late")

    def test_blank_name_still_gets_a_seat(self, classroom):
        """Refusing the join would strand somebody mid-lesson over a typo."""
        assert classroom.join("   ").name == "Unnamed"

    def test_remove_drops_them(self, classroom):
        student = classroom.join("Gone")
        classroom.remove(student.student_id)
        assert student.student_id not in classroom.students
        with pytest.raises(KeyError):
            classroom.student(student.student_id)

    def test_restart_gives_a_fresh_well_on_the_same_exercise(self, classroom, clock):
        """A student who breaks the shoe in two minutes should not sit out."""
        student = classroom.join("Brian")
        classroom.set_speed(1.0)
        run(classroom, 5.0, clock)
        assert student.session.simulator.state.time_s > 0

        classroom.restart(student.student_id)
        assert student.session.simulator.state.time_s == 0
        assert student.session.speed == 1.0, "should rejoin the class speed"


class TestCodes:
    def test_matches_what_a_student_types(self, classroom):
        assert classroom.matches(classroom.code.replace("-", "").lower())

    def test_store_finds_by_code(self, clock):
        store = ClassroomStore(clock=clock)
        classroom = store.create(name="Monday")
        assert store.by_code(classroom.code) is classroom

    def test_store_does_not_find_a_closed_class(self, clock):
        """A stale code and a wrong code must look the same to somebody probing."""
        store = ClassroomStore(clock=clock)
        classroom = store.create()
        classroom.close()
        with pytest.raises(KeyError):
            store.by_code(classroom.code)

    def test_store_does_not_find_an_expired_class(self, clock):
        store = ClassroomStore(clock=clock)
        classroom = store.create(ttl_s=10)
        clock.tick(11)
        with pytest.raises(KeyError):
            store.by_code(classroom.code)

    def test_summary_withholds_the_code_from_students(self, classroom):
        """There is no reason to hand the code back to the room."""
        assert "code" in classroom.summary()
        assert "code" not in classroom.summary(include_code=False)


class TestTheServerClock:
    def test_a_class_starts_paused(self, classroom, clock):
        """Students join and get their bearings before the well moves."""
        student = classroom.join("Brian")
        assert classroom.paused
        run(classroom, 10.0, clock)
        assert student.session.simulator.state.time_s == 0

    def test_wells_advance_with_nobody_looking(self, classroom, clock):
        """The whole reason the clock moved to the server.

        Nothing here polls a snapshot or touches a session -- the only thing
        driving these wells is the class tick.
        """
        student = classroom.join("Brian")
        classroom.set_speed(1.0)
        run(classroom, 20.0, clock)
        assert student.session.simulator.state.time_s == pytest.approx(20.0, abs=0.5)

    def test_speed_multiplies(self, classroom, clock):
        student = classroom.join("Brian")
        classroom.set_speed(10.0)
        run(classroom, 5.0, clock)
        assert student.session.simulator.state.time_s == pytest.approx(50.0, rel=0.05)

    def test_every_well_in_the_class_advances(self, classroom, clock):
        students = [classroom.join(name) for name in ("A", "B", "C")]
        classroom.set_speed(1.0)
        run(classroom, 10.0, clock)
        for student in students:
            assert student.session.simulator.state.time_s == pytest.approx(
                10.0, abs=0.5
            )

    def test_pause_stops_every_well(self, classroom, clock):
        student = classroom.join("Brian")
        classroom.set_speed(1.0)
        run(classroom, 5.0, clock)
        stopped_at = student.session.simulator.state.time_s

        classroom.pause()
        run(classroom, 10.0, clock)
        assert student.session.simulator.state.time_s == stopped_at

    def test_resume_returns_to_the_previous_speed(self, classroom, clock):
        """Pause all has to be reversible without the instructor recalling
        what it was running at."""
        classroom.join("Brian")
        classroom.set_speed(10.0)
        classroom.pause()
        assert classroom.speed == 0.0
        classroom.resume()
        assert classroom.speed == 10.0

    def test_changing_speed_banks_the_time_already_run(self, classroom, clock):
        """Seconds elapsed at 1x must never be re-integrated at 10x.

        The gap that matters is the one between the last tick and the speed
        change -- an instructor's request arriving mid-tick. So the clock is
        moved here *without* ticking the class, which is what really happens:
        five wall seconds at 1x, then one more unticked second, then two at 10x
        is 5 + 1 + 20 = 26 simulated seconds. Were the speed set before
        advancing, that unticked second would be credited at 10x and the answer
        would be 35.
        """
        student = classroom.join("Brian")
        classroom.set_speed(1.0)
        run(classroom, 5.0, clock)

        clock.tick(1.0)  # time passes; no tick has processed it yet
        classroom.set_speed(10.0)
        run(classroom, 2.0, clock)

        assert student.session.simulator.state.time_s == pytest.approx(26.0, rel=0.02)

    def test_a_late_arrival_picks_up_the_class_speed(self, classroom, clock):
        """Otherwise they sit on a frozen well waiting for the instructor."""
        classroom.set_speed(10.0)
        late = classroom.join("Late")
        assert late.session.speed == 10.0
        run(classroom, 2.0, clock)
        assert late.session.simulator.state.time_s > 0

    def test_a_late_arrival_does_not_inherit_elapsed_time(self, classroom, clock):
        """They start their own kick at zero, not part-way through everyone
        else's."""
        early = classroom.join("Early")
        classroom.set_speed(1.0)
        run(classroom, 10.0, clock)
        late = classroom.join("Late")
        run(classroom, 5.0, clock)
        assert early.session.simulator.state.time_s > (
            late.session.simulator.state.time_s + 5.0
        )


class TestTheMonitorGrid:
    def test_one_tile_per_student_sorted_by_name(self, classroom):
        for name in ("Charlie", "alice", "Bob"):
            classroom.join(name)
        names = [tile["name"] for tile in classroom.monitor()["students"]]
        assert names == ["alice", "Bob", "Charlie"]

    def test_the_instructor_sees_what_the_student_cannot(self, clock):
        """The one place downhole_visible is deliberately bypassed.

        On a drilling start the student is meant to be *detecting* a kick, so
        their gauges withhold bottomhole and pore pressure. The instructor
        watching fifteen wells has the opposite problem.
        """
        classroom = Classroom(start="drilling", clock=clock)
        student = classroom.join("Brian")

        assert student.session.gauges()["bottomhole_psi"] is None
        assert student.session.gauges()["downhole_visible"] is False

        tile = classroom.monitor()["students"][0]
        assert tile["bottomhole_psi"] > 0
        assert tile["formation_psi"] > 0
        assert tile["downhole_visible"] is False, (
            "the tile should say the student is still detecting"
        )

    def test_a_quiet_screen_is_flagged(self, classroom, clock):
        """Distinguishes a student who walked off from one sitting on an idle
        well -- identical from the numbers alone."""
        student = classroom.join("Brian")
        assert classroom.monitor()["students"][0]["connected"]

        clock.tick(60)
        tile = classroom.monitor()["students"][0]
        assert not tile["connected"]
        assert tile["quiet_s"] == pytest.approx(60.0, abs=0.1)

        classroom.touch(student.student_id)
        assert classroom.monitor()["students"][0]["connected"]

    def test_status_reads_the_well(self, classroom):
        student = classroom.join("Brian")
        assert classroom.monitor()["students"][0]["status"] == "shut in"

        student.session.simulator.pump.command(30.0)
        student.session.simulator.step(0.05)
        assert classroom.monitor()["students"][0]["status"] == "circulating"

    def test_summary_counts_the_room(self, classroom):
        for name in ("A", "B"):
            classroom.join(name)
        assert classroom.monitor()["classroom"]["student_count"] == 2


class TestTheStore:
    def test_tick_all_drives_every_class(self, clock):
        store = ClassroomStore(clock=clock)
        first = store.create(name="Morning")
        second = store.create(name="Afternoon")
        a = first.join("A")
        b = second.join("B")
        first.set_speed(1.0)
        second.set_speed(1.0)

        for _ in range(int(10.0 / TICK_S)):
            clock.tick(TICK_S)
            store.tick_all()

        assert a.session.simulator.state.time_s == pytest.approx(10.0, abs=0.5)
        assert b.session.simulator.state.time_s == pytest.approx(10.0, abs=0.5)

    def test_purge_expired(self, clock):
        store = ClassroomStore(clock=clock)
        store.create(ttl_s=10)
        store.create(ttl_s=1000)
        clock.tick(11)
        assert store.purge_expired() == 1
        assert len(store) == 1

    def test_unknown_class_raises(self, clock):
        with pytest.raises(KeyError):
            ClassroomStore(clock=clock).get("nope")
