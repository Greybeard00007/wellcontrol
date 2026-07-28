"""Drill ahead, take a kick, detect it, shut in.

The sequence that makes this a simulator rather than a choke trainer. On a
shut-in start the kick is handed over already sized and identified with the
pressures on the gauges; here the student has to earn all of it.

**The check that matters is the last one.** Nothing tells the engine what SIDPP
ought to read. The student drills into a zone, takes an influx through the
productivity-index response, shuts the well in, and the pressure that appears on
the drill pipe gauge is whatever the volume balance produces. It has to equal
pore pressure minus the mud hydrostatic -- the kill sheet identity -- or the
forward model and the rest of the library disagree about the same well.
"""

from __future__ import annotations

import pytest

from wellcontrol import examples
from wellcontrol.sim import EventKind, Session, Simulator
from wellcontrol.sim.engine import DRILLING_OVERBALANCE_PSI, StartState
from wellcontrol.units import PSI_PER_PPG_FT
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)

DRILLING_SPM = 60.0


def drilling(**kwargs) -> Simulator:
    """A rig drilling ahead: pump to the string, returns up the flowline."""
    sim = Simulator(
        well=examples.well_state(SV), start=StartState.DRILLING, **kwargs
    )
    sim.flow_path.open("pump_discharge", "standpipe_valve")
    return sim


def drill_until_kicking(sim: Simulator, gain_bbl: float = 6.0,
                        limit_s: float = 1800.0) -> None:
    """Circulate until the pit gain reaches a given size, or give up."""
    sim.pump.command(DRILLING_SPM)
    while sim.pits.gain_bbl < gain_bbl and sim.state.time_s < limit_s:
        sim.run(10, dt=0.1)


def shut_in(sim: Simulator, settle_s: float = 600.0) -> None:
    """Stop the pump, close the annular, line up to the choke, let it settle."""
    sim.pump.stop()
    sim.run(20, dt=0.1)
    sim.flow_path.close("annular")
    sim.flow_path.open("choke_line", "choke_line_to_manifold")
    sim.choke.command(0.0)
    sim.run(settle_s, dt=0.1)


class TestDrillingAhead:
    def test_the_well_starts_with_no_kick_in_it(self):
        sim = drilling()
        assert sim.state.influx_volume_bbl == pytest.approx(0.0)
        assert sim.state.casing_pressure_psi == pytest.approx(0.0)

    def test_the_well_starts_overbalanced(self):
        sim = drilling()
        assert sim.state.bottomhole_pressure_psi > sim.formation_pressure_psi
        assert sim.state.bottomhole_pressure_psi - sim.formation_pressure_psi == (
            pytest.approx(DRILLING_OVERBALANCE_PSI, abs=1.0)
        )

    def test_nothing_flows_before_the_kick_zone(self):
        sim = drilling(kick_after_s=None)
        sim.pump.command(DRILLING_SPM)
        sim.run(300, dt=0.1)
        assert sim.state.influx_volume_bbl == pytest.approx(0.0)
        assert sim.pits.gain_bbl == pytest.approx(0.0, abs=0.1)

    def test_returns_take_the_flowline(self):
        route = drilling().flow_path.return_route()
        assert route.open_to_pits
        assert not route.through_choke

    def test_circulating_raises_bottomhole_pressure(self):
        """ECD: the well is harder to kick while the pump is running."""
        sim = drilling(kick_after_s=None)
        static = sim.state.bottomhole_pressure_psi
        sim.pump.command(DRILLING_SPM)
        sim.run(60, dt=0.1)
        assert sim.state.bottomhole_pressure_psi > static


class TestTheKickArrives:
    def test_the_kick_needs_the_pump_turning(self):
        """No circulating, no hole made, no zone drilled into."""
        sim = drilling(kick_after_s=60.0)
        sim.run(600, dt=0.1)              # ten minutes with the pump off
        assert not sim.kick_taken

    def test_drilling_into_the_zone_raises_pore_pressure(self):
        sim = drilling(kick_after_s=30.0)
        before = sim.formation_pressure_psi
        sim.pump.command(DRILLING_SPM)
        sim.run(90, dt=0.1)
        assert sim.kick_taken
        assert sim.formation_pressure_psi > before

    def test_an_instructor_can_inject_it_by_hand(self):
        sim = drilling(kick_after_s=None)
        sim.trigger_kick()
        assert sim.kick_taken

    def test_the_influx_is_not_scripted(self):
        """It arrives through the same underbalance response as anything else."""
        sim = drilling(kick_after_s=30.0)
        sim.pump.command(DRILLING_SPM)
        sim.run(120, dt=0.1)
        assert sim.state.influx_rate_bpm > 0
        assert sim.state.bottomhole_pressure_psi < sim.formation_pressure_psi


class TestDetection:
    """What the driller actually watches. Both are volume-balance consequences."""

    def test_flow_out_exceeds_flow_in(self):
        sim = drilling(kick_after_s=30.0)
        drill_until_kicking(sim, gain_bbl=2.0)
        assert sim.state.flow_out_bpm > sim.state.flow_in_bpm

    def test_the_pit_gains(self):
        sim = drilling(kick_after_s=30.0)
        drill_until_kicking(sim, gain_bbl=5.0)
        assert sim.pits.gain_bbl >= 5.0

    def test_flow_check_is_the_earlier_warning(self):
        """It fires before the pit gain does -- which is why it is watched."""
        sim = drilling(kick_after_s=30.0)
        drill_until_kicking(sim, gain_bbl=6.0)
        log = sim.state.event_log
        flow = log.first(EventKind.FLOW_CHECK)
        gain = log.first(EventKind.PIT_GAIN)
        assert flow is not None and gain is not None
        assert flow.time_s < gain.time_s

    def test_the_kick_develops_fast_enough_to_watch(self):
        """A kick nobody can see develop is not an exercise."""
        sim = drilling(kick_after_s=30.0)
        drill_until_kicking(sim, gain_bbl=5.0)
        assert sim.state.time_s < 900.0, "took over 15 simulated minutes"


@pytest.fixture(scope="module")
def shut_in_well():
    """A kick taken while drilling, then shut in and left to settle.

    **Migration is switched off here deliberately.** The shut-in identity being
    checked below -- SIDPP equals pore pressure minus the mud hydrostatic --
    holds while the gas is still where it entered. Once it starts climbing it
    carries its pressure up with it and both gauges rise above the identity,
    which is correct and is covered in ``test_migration.py``. Leaving migration
    on here would test two things at once and pin neither.
    """
    sim = drilling(kick_after_s=30.0, migration_rate_ft_per_hr=0.0)
    drill_until_kicking(sim, gain_bbl=6.0)
    shut_in(sim)
    return sim


@pytest.fixture(scope="module")
def shut_in_and_migrating():
    sim = drilling(kick_after_s=30.0)
    drill_until_kicking(sim, gain_bbl=6.0)
    shut_in(sim)
    return sim


class TestShutIn:

    def test_the_influx_stops(self, shut_in_well):
        assert shut_in_well.state.influx_rate_bpm == pytest.approx(0.0, abs=0.02)

    def test_bottomhole_pressure_comes_to_rest_on_pore_pressure(self, shut_in_well):
        """The definition of a well that is shut in and stable."""
        assert shut_in_well.state.bottomhole_pressure_psi == pytest.approx(
            shut_in_well.formation_pressure_psi, abs=5.0
        )

    def test_the_emergent_sidpp_matches_the_kill_sheet_identity(self, shut_in_well):
        """The check this whole file exists for.

        SIDPP = pore pressure - mud hydrostatic to the bit. Nothing told the
        engine that; it fell out of the volume balance. If it disagrees, the
        forward model and the inverse model are describing different wells.
        """
        well = shut_in_well.well
        expected = shut_in_well.formation_pressure_psi - (
            PSI_PER_PPG_FT * well.mud_weight_ppg * well.bit_tvd_ft
        )
        assert shut_in_well.state.standpipe_pressure_psi == pytest.approx(
            expected, abs=5.0
        )

    def test_casing_pressure_exceeds_drill_pipe_pressure(self, shut_in_well):
        """Because the influx is lighter than the mud it displaced."""
        state = shut_in_well.state
        assert state.casing_pressure_psi > state.standpipe_pressure_psi

    def test_the_log_says_when_the_pressures_are_readable(self, shut_in_well):
        log = shut_in_well.state.event_log
        assert log.has(EventKind.WELL_SHUT_IN_BY_STUDENT)
        stable = log.first(EventKind.SHUT_IN)
        assert stable is not None
        assert "stable" in stable.message.lower()

    def test_the_stable_reading_comes_after_the_shut_in(self, shut_in_well):
        """Quoting the gauges at the moment of shut-in would quote them rising."""
        log = shut_in_well.state.event_log
        closed = log.first(EventKind.WELL_SHUT_IN_BY_STUDENT)
        stable = log.first(EventKind.SHUT_IN)
        assert stable.time_s >= closed.time_s

    def test_a_shut_in_well_does_not_stay_where_it_was_left(
        self, shut_in_and_migrating
    ):
        """The same kick with the gas free to climb.

        Shutting in stops the influx; it does not stop the well. The pressures
        keep rising as the gas migrates, which is what turns a successful
        shut-in into a problem if nobody does anything next.
        """
        sim = shut_in_and_migrating
        expected = sim.formation_pressure_psi - (
            PSI_PER_PPG_FT * sim.well.mud_weight_ppg * sim.well.bit_tvd_ft
        )
        assert sim.state.standpipe_pressure_psi > expected + 10.0

    def test_more_influx_arrives_after_the_pump_stops(self, shut_in_well):
        """Losing ECD deepens the underbalance, so a slow shut-in costs volume.

        Worth knowing: the pit gain under-reports the influx, because what came
        in after the well was closed never reached the pits.
        """
        assert shut_in_well.state.influx_volume_bbl > shut_in_well.pits.gain_bbl


class TestWhatTheStudentCanSee:
    """A rig has no bottomhole gauge and no pore pressure readout."""

    def _session(self, clock=None):
        return Session.for_configuration(start="drilling")

    def test_downhole_is_hidden_while_drilling(self):
        session = self._session()
        assert not session.downhole_visible
        gauges = session.gauges()
        assert gauges["bottomhole_psi"] is None
        assert gauges["formation_psi"] is None
        assert gauges["influx_bbl"] is None

    def test_the_rig_instruments_are_always_readable(self):
        gauges = self._session().gauges()
        for key in ("drill_pipe_psi", "casing_psi", "pit_gain_bbl",
                    "flow_in_bpm", "flow_out_bpm"):
            assert gauges[key] is not None

    def test_the_trace_does_not_leak_what_the_gauges_withhold(self):
        session = self._session()
        session.set_speed(10.0)
        session.simulator.run(30, dt=0.1)
        for row in session.trace():
            assert "bottomhole_psi" not in row
            assert "influx_bbl" not in row

    def test_downhole_appears_once_the_well_is_shut_in(self):
        session = self._session()
        sim = session.simulator
        drill_until_kicking(sim, gain_bbl=6.0)
        shut_in(sim, settle_s=120.0)
        assert session.downhole_visible
        assert session.gauges()["bottomhole_psi"] is not None

    def test_a_shut_in_start_shows_downhole_immediately(self):
        """There is nothing left to detect, so hiding it teaches nothing."""
        session = Session.for_configuration(start="shut_in")
        assert session.downhole_visible
        assert session.gauges()["bottomhole_psi"] is not None


class TestTheKillSheetIsTheirs:
    """Built from what the student read, not from the exercise's own figures.

    On a drilling start those are different numbers: a slow shut-in takes more
    influx and reads higher, so a sheet computed from the designer's values is
    the answer to a question nobody asked -- and the wrong answer for the kick
    that was actually taken.
    """

    def test_there_is_no_sheet_before_shutting_in(self):
        session = Session.for_configuration(start="drilling")
        sheet = session.kill_sheet()
        assert sheet["available"] is False
        assert "kill_mud_weight_ppg" not in sheet

    def test_the_readings_are_latched_when_the_pressures_settle(
        self, shut_in_and_migrating
    ):
        state = shut_in_and_migrating.state
        assert state.measured_sidpp_psi is not None
        assert state.measured_sicp_psi is not None

    def test_the_sheet_is_built_from_those_readings(self, shut_in_well):
        import sys
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        if str(app_dir) not in sys.path:
            sys.path.insert(0, str(app_dir))
        from wellcontrol.sim import Session

        session = Session(simulator=shut_in_well)
        sheet = session.kill_sheet()
        assert sheet["available"] is True
        assert sheet["readings_are_the_students"] is True
        assert sheet["sidpp_psi"] == pytest.approx(
            round(shut_in_well.state.measured_sidpp_psi), abs=1
        )

    def test_the_kill_weight_follows_the_rounding_rules(self, shut_in_well):
        from wellcontrol.sim import Session

        sheet = Session(simulator=shut_in_well).kill_sheet()
        # Rounded up to the tenth, because that is what the rig can mix.
        assert sheet["kill_mud_weight_ppg"] >= sheet["kill_mud_weight_exact_ppg"]
        assert sheet["kill_mud_weight_ppg"] == pytest.approx(
            round(sheet["kill_mud_weight_ppg"], 1)
        )

    def test_a_heavier_shut_in_gives_a_heavier_kill_weight(self):
        """The consequence a student should feel: a slow shut-in costs mud."""
        from wellcontrol.sim import Session
        from wellcontrol.units import PSI_PER_PPG_FT

        session = Session.for_configuration(start="drilling")
        sim = session.simulator
        sim.state.measured_sidpp_psi = 400.0
        sim.state.measured_sicp_psi = 600.0
        sim.state.measured_pit_gain_bbl = 20.0
        light = session.kill_sheet()["kill_mud_weight_ppg"]

        sim.state.measured_sidpp_psi = 700.0
        heavy = session.kill_sheet()["kill_mud_weight_ppg"]
        assert heavy > light


class TestAutomaticSlowDown:
    """The clock drops to real time when the well starts flowing."""

    class Clock:
        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            return self.now

        def tick(self, seconds):
            self.now += seconds

    def _drilling_session(self, **kwargs):
        clock = self.Clock()
        session = Session.for_configuration(
            start="drilling", clock=clock, **kwargs
        )
        return session, clock

    def test_speed_drops_to_real_time_when_the_well_flows(self):
        session, clock = self._drilling_session()
        session.set_speed(60.0)
        session.set_pump(DRILLING_SPM)
        for _ in range(40):
            clock.tick(1.0)
            session.advance()
            if session.speed <= 1.0:
                break
        assert session.speed == pytest.approx(1.0)

    def test_it_only_happens_once(self):
        """Otherwise the student could never speed back up to circulate out."""
        session, clock = self._drilling_session()
        session.set_speed(60.0)
        session.set_pump(DRILLING_SPM)
        for _ in range(40):
            clock.tick(1.0)
            session.advance()
            if session.speed <= 1.0:
                break
        session.set_speed(10.0)
        clock.tick(2.0)
        session.advance()
        assert session.speed == pytest.approx(10.0)

    def test_it_can_be_switched_off(self):
        session, clock = self._drilling_session(auto_slow=False)
        session.set_speed(60.0)
        session.set_pump(DRILLING_SPM)
        for _ in range(20):
            clock.tick(1.0)
            session.advance()
        assert session.speed == pytest.approx(60.0)

    def test_a_shut_in_start_is_unaffected_until_it_flows(self):
        """Nothing is flowing on a well that is closed in and holding."""
        clock = self.Clock()
        session = Session.for_configuration(start="shut_in", clock=clock)
        session.set_speed(60.0)
        clock.tick(2.0)
        session.advance()
        assert session.speed == pytest.approx(60.0)


class TestAssessmentOfADrillingStart:
    def test_drilling_overbalanced_is_not_scored_as_an_excursion(self):
        """Correct practice before the kick must not count against the student."""
        sim = drilling(kick_after_s=None)
        sim.pump.command(DRILLING_SPM)
        sim.run(300, dt=0.1)
        assert sim.state.seconds_outside_band == pytest.approx(0.0)
        assert not sim.state.event_log.has(EventKind.BHP_EXCURSION)

    def test_the_influx_taken_is_counted(self):
        sim = drilling(kick_after_s=30.0)
        drill_until_kicking(sim, gain_bbl=5.0)
        assert sim.state.influx_taken_bbl > 0.0
