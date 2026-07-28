"""The session and its clock, tested without the web stack.

The engine advances by simulated seconds and knows nothing about wall time.
:class:`~wellcontrol.sim.session.Session` decides *when* to step it, and that
decision has consequences a student can feel, so it is tested on its own with an
injected clock rather than through the HTTP layer or by sleeping.
"""

from __future__ import annotations

import pytest

from wellcontrol.sim import Session, SessionStore
from wellcontrol.sim.session import MAX_CATCHUP_S, STEP_S


class FakeClock:
    """A wall clock the test moves by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session(clock):
    return Session.for_configuration(clock=clock)


class TestTime:
    def test_a_new_session_has_not_advanced(self, session):
        assert session.simulator.state.time_s == 0.0

    def test_paused_by_default(self, session):
        assert session.paused

    def test_a_paused_session_does_not_move(self, session, clock):
        clock.tick(60.0)
        assert session.advance() == 0.0
        assert session.simulator.state.time_s == 0.0

    def test_the_first_advance_only_starts_the_clock(self, session, clock):
        """Nothing is owed for the time before anybody was watching."""
        session.speed = 1.0
        clock.tick(3600.0)
        assert session.advance() == 0.0

    def test_real_time_advances_one_for_one(self, session, clock):
        session.set_speed(1.0)
        clock.tick(10.0)
        assert session.advance() == pytest.approx(10.0, abs=STEP_S)
        assert session.simulator.state.time_s == pytest.approx(10.0, abs=STEP_S)

    def test_the_speed_multiplier_scales_simulated_time(self, session, clock):
        session.set_speed(10.0)
        clock.tick(2.0)
        assert session.advance() == pytest.approx(20.0, abs=STEP_S)

    def test_catch_up_is_clamped(self, session, clock):
        """A student who leaves the tab must not return to hours of well."""
        session.set_speed(60.0)
        clock.tick(600.0)                      # ten minutes away at 60x
        assert session.advance() == pytest.approx(MAX_CATCHUP_S, abs=STEP_S)

    def test_changing_speed_banks_the_time_already_run(self, session, clock):
        """Time elapsed at the old speed must not be integrated at the new one."""
        session.set_speed(1.0)
        clock.tick(10.0)
        session.set_speed(60.0)                # advances 10 s, then changes
        assert session.simulator.state.time_s == pytest.approx(10.0, abs=STEP_S)

    def test_pausing_stops_the_well(self, session, clock):
        session.set_speed(10.0)
        clock.tick(5.0)
        session.advance()
        stopped_at = session.simulator.state.time_s
        session.set_speed(0.0)
        clock.tick(600.0)
        session.advance()
        assert session.simulator.state.time_s == pytest.approx(stopped_at)

    def test_a_clock_that_goes_backwards_does_not_rewind_the_well(self, session, clock):
        session.set_speed(1.0)
        clock.tick(10.0)
        session.advance()
        before = session.simulator.state.time_s
        clock.tick(-5.0)
        session.advance()
        assert session.simulator.state.time_s >= before


class TestControls:
    def test_setting_the_choke_commands_it(self, session):
        session.set_choke(35.0)
        assert session.simulator.choke.commanded_position == pytest.approx(35.0)

    def test_setting_the_pump_commands_it(self, session):
        session.set_pump(30.0)
        assert session.simulator.pump.commanded_spm == pytest.approx(30.0)

    def test_zero_spm_stops_the_pump(self, session):
        session.set_pump(30.0)
        session.set_pump(0.0)
        assert session.simulator.pump.commanded_spm == pytest.approx(0.0)

    def test_a_control_change_advances_time_first(self, session, clock):
        """Otherwise the new setting would be credited with the old one's time."""
        session.set_speed(10.0)
        clock.tick(3.0)
        session.set_choke(50.0)
        assert session.simulator.state.time_s > 0.0

    def test_valves_can_be_operated(self, session):
        session.set_valve("annular", "open")
        assert not session.simulator.flow_path.bop_is_closed
        session.set_valve("annular", "closed")
        assert session.simulator.flow_path.bop_is_closed

    def test_an_unknown_valve_is_rejected(self, session):
        with pytest.raises(KeyError, match="No valve called"):
            session.set_valve("magic_valve", "open")

    def test_an_unknown_state_is_rejected(self, session):
        with pytest.raises(ValueError):
            session.set_valve("annular", "ajar")


class TestStartingPosition:
    def test_shut_in_start_is_ready_to_kill(self, session):
        """The exercise begins where the kill sheet begins."""
        path = session.simulator.flow_path
        assert path.bop_is_closed
        assert path.return_route().through_choke

    def test_drilling_start_leaves_the_stack_open(self, clock):
        session = Session.for_configuration(start="drilling", clock=clock)
        path = session.simulator.flow_path
        assert not path.bop_is_closed
        assert not path.return_route().through_choke

    def test_every_configuration_starts(self, clock):
        for bop in ("surface", "subsea"):
            for trajectory in ("vertical", "deviated", "horizontal"):
                built = Session.for_configuration(
                    bop=bop, trajectory=trajectory, clock=clock
                )
                assert built.gauges()["casing_psi"] > 0


class TestReadouts:
    def test_gauges_carry_the_instrument_scan(self, session):
        gauges = session.gauges()
        for key in ("drill_pipe_psi", "casing_psi", "strokes", "choke_percent"):
            assert key in gauges

    def test_lineup_lists_every_valve(self, session):
        names = {valve["name"] for valve in session.lineup()}
        assert "annular" in names
        assert "choke_1" in names

    def test_bop_elements_are_marked(self, session):
        bops = [v for v in session.lineup() if v["is_bop"]]
        assert {v["name"] for v in bops} == {
            "annular", "upper_rams", "lower_rams"
        }

    def test_events_can_be_fetched_incrementally(self, session, clock):
        first = session.events_since(0)
        assert first["events"], "shut-in should be logged"

        session.set_speed(10.0)
        session.set_pump(30.0)
        session.set_choke(0.0)
        clock.tick(20.0)
        session.advance()

        later = session.events_since(first["next_index"])
        assert later["next_index"] >= first["next_index"]
        for event in later["events"]:
            assert event not in first["events"]

    def test_trace_is_limited(self, session, clock):
        session.set_speed(60.0)
        for _ in range(20):
            clock.tick(0.5)
            session.advance()
        assert len(session.trace(limit=5)) <= 5

    def test_snapshot_advances_and_reports_together(self, session, clock):
        session.set_speed(10.0)
        clock.tick(2.0)
        snapshot = session.snapshot()
        assert snapshot["gauges"]["time_s"] > 0
        assert "events" in snapshot and "trace" in snapshot

    def test_debrief_is_serialisable(self, session, clock):
        session.set_speed(10.0)
        clock.tick(3.0)
        report = session.debrief()
        assert report["grade"] in ("excellent", "pass", "marginal", "fail")
        assert isinstance(report["metrics"], list)
        assert "report" in report


class TestSessionStore:
    def test_created_sessions_are_retrievable(self):
        store = SessionStore()
        session = store.create()
        assert store.get(session.session_id) is session

    def test_ids_are_distinct(self):
        store = SessionStore()
        assert store.create().session_id != store.create().session_id

    def test_missing_session_raises(self):
        with pytest.raises(KeyError, match="No session"):
            SessionStore().get("nope")

    def test_sessions_can_be_dropped(self):
        store = SessionStore()
        session = store.create()
        store.drop(session.session_id)
        assert len(store) == 0


class TestTheWellStillResponds:
    """The session must not blunt what the engine does.

    These duplicate simulator tests on purpose: the point is that driving the
    engine through a session, on a wall clock, gives the same physics.
    """

    def test_working_the_choke_shut_builds_pressure(self, session, clock):
        session.set_speed(10.0)
        session.set_pump(30.0)
        session.set_choke(0.0)
        before = session.gauges()["casing_psi"]
        for _ in range(6):
            clock.tick(1.0)
            session.advance()
        assert session.gauges()["casing_psi"] > before + 100

    def test_circulating_with_the_bop_open_does_nothing(self, clock):
        session = Session.for_configuration(start="drilling", clock=clock)
        session.set_speed(10.0)
        session.set_valve("choke_line_valve", "open")
        session.set_valve("choke_line_to_manifold", "open")
        session.set_valve("choke_1", "open")
        session.set_pump(30.0)
        session.set_choke(2.0)
        for _ in range(6):
            clock.tick(1.0)
            session.advance()
        assert session.gauges()["casing_psi"] == 0
        assert "no pressure control" in session.gauges()["returns"]


class TestTheStrokeCounterResets:
    """RESET on the choke panel clears the window, not the engine's count.

    The engine's stroke total drives fault triggers and the strokes-to-bit
    figures on the kill sheet, so it has to keep running underneath. A driller
    zeroing the counter before a circulation is re-marking a gauge, not undoing
    the pumping that has already happened.
    """

    def _pump_some_strokes(self, session, clock):
        session.set_speed(10.0)
        session.set_pump(30.0)
        for _ in range(6):
            clock.tick(1.0)
            session.advance()

    def test_the_window_goes_back_to_zero(self, session, clock):
        self._pump_some_strokes(session, clock)
        assert session.gauges()["strokes"] > 0
        session.zero("strokes")
        assert session.gauges()["strokes"] == 0

    def test_the_engine_keeps_its_own_count(self, session, clock):
        self._pump_some_strokes(session, clock)
        engine_strokes = session.simulator.state.strokes
        assert engine_strokes > 0
        session.zero("strokes")
        assert session.simulator.state.strokes == pytest.approx(
            engine_strokes, abs=1.0
        )

    def test_the_window_counts_up_again_from_the_reset(self, session, clock):
        self._pump_some_strokes(session, clock)
        session.zero("strokes")
        self._pump_some_strokes(session, clock)
        after = session.gauges()["strokes"]
        assert 0 < after < round(session.simulator.state.strokes)

    def test_an_unknown_instrument_still_names_what_there_is(self, session):
        with pytest.raises(KeyError, match="strokes"):
            session.zero("hookload")
