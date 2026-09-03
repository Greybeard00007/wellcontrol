"""Two rig pumps, four chokes, and the drills that need them.

Neither addition is about matching a photograph. A pump that fails mid-kill and
a choke that plugs mid-kill are both faults this simulator can throw, and until
now there was nothing to change over *to* -- the fault was simply the end of the
exercise. With a second of each, the answer to the fault is the thing a rig
would actually do.
"""

import pytest

from wellcontrol import examples
from wellcontrol.sim import Simulator
from wellcontrol.sim.engine import StartState
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation("surface"), Trajectory("vertical"))


def drilling() -> Simulator:
    sim = Simulator(well=examples.well_state(SV), start=StartState.DRILLING)
    sim.flow_path.open("pump_discharge", "standpipe_valve")
    return sim


def shut_in() -> Simulator:
    sim = Simulator(well=examples.well_state(SV))
    sim.flow_path.close("annular")
    sim.flow_path.open("choke_line", "choke_line_to_manifold")
    sim.flow_path.open("pump_discharge", "standpipe_valve")
    return sim


class TestTheManifoldBranches:
    """The SP manifold face's branches are real valves, not decoration.

    The cement lines run to equipment this simulator does not model, so
    they are blanked: the valves move and nothing changes, which is what
    opening them on this rig would do. The second discharge line parallels
    the first, because the graph has one PUMP node for both pumps.
    """

    def test_the_cement_lines_are_blanked(self):
        sim = shut_in()
        before = sim.flow_path.return_route().route
        assert not sim.flow_path.pump_is_deadheaded
        sim.flow_path.open("cement_head_valve", "cement_pump_valve")
        assert sim.flow_path.return_route().route == before
        assert not sim.flow_path.pump_is_deadheaded

    def test_the_second_discharge_parallels_the_first(self):
        sim = shut_in()
        sim.flow_path.close("pump_discharge")
        assert sim.flow_path.pump_is_deadheaded
        sim.flow_path.open("pump_2_discharge")
        assert not sim.flow_path.pump_is_deadheaded

    def test_the_second_discharge_starts_closed(self):
        """Every scenario that blocks the pump closes `pump_discharge`; a
        second line open by default would quietly defeat it."""
        sim = shut_in()
        assert not sim.flow_path.valve("pump_2_discharge").is_open


class TestTwoPumps:
    def test_the_flows_add(self):
        sim = drilling()
        sim.pump.command(30.0)
        sim.run(60.0, dt=0.05)
        one = sim.total_pump_flow_bbl_per_min

        sim.pump_2.command(30.0)
        sim.run(60.0, dt=0.05)
        assert sim.total_pump_flow_bbl_per_min == pytest.approx(2 * one, rel=0.02)

    def test_either_pump_alone_gives_the_same_rate(self):
        """They are the same pump, so the well should not be able to tell."""
        first = drilling()
        first.pump.command(30.0)
        first.run(60.0, dt=0.05)

        second = drilling()
        second.pump_2.command(30.0)
        second.run(60.0, dt=0.05)

        assert second.total_pump_flow_bbl_per_min == pytest.approx(
            first.total_pump_flow_bbl_per_min, rel=1e-6
        )
        assert second.state.strokes == pytest.approx(first.state.strokes, rel=1e-6)

    def test_strokes_follow_volume_not_stroke_count(self):
        """The kill schedule is volume, counted in strokes.

        With both pumps turning the count has to advance at the combined rate,
        or the schedule stops describing where the kill mud actually is.
        """
        sim = drilling()
        sim.pump.command(30.0)
        sim.pump_2.command(30.0)
        sim.run(120.0, dt=0.05)   # long enough to be past the ramp

        before = sim.state.strokes
        sim.run(60.0, dt=0.05)
        assert sim.state.strokes - before == pytest.approx(60.0, rel=0.02), (
            "two pumps at 30 spm should count 60 strokes in a minute"
        )

    def test_pumping_is_true_if_either_is_turning(self):
        sim = drilling()
        assert not sim.pumping
        sim.pump_2.command(20.0)
        sim.run(20.0, dt=0.05)
        assert sim.pumping

    def test_a_pump_failure_takes_the_one_that_is_running(self):
        """So the fault always bites, and the answer is to change pumps."""
        sim = drilling()
        sim.pump_2.command(30.0)
        sim.run(60.0, dt=0.05)
        assert sim.total_pump_flow_bbl_per_min > 0

        sim._break_equipment(_fault("pump_failure"))
        sim.run(60.0, dt=0.05)
        assert sim.total_pump_flow_bbl_per_min == pytest.approx(0.0, abs=1e-6)

        # The other one is untouched, which is the point.
        sim.pump.command(30.0)
        sim.run(60.0, dt=0.05)
        assert sim.total_pump_flow_bbl_per_min > 0


class TestFourChokes:
    def test_a_second_open_choke_passes_more(self):
        """Parallel orifices add, and that is a lineup error with a cost.

        Two half-shut chokes let past far more than the handle in the driller's
        hand accounts for.
        """
        one = shut_in()
        one.flow_path.open("choke_1")
        one.choke.command(30.0)
        one.run(30.0, dt=0.05)
        single = one.state.flow_out_bpm

        both = shut_in()
        both.flow_path.open("choke_1", "choke_2")
        both.choke.command(30.0)
        both.choke_2.command(30.0)
        both.run(30.0, dt=0.05)

        assert both.state.flow_out_bpm > single * 1.3, (
            f"two chokes passed {both.state.flow_out_bpm:.2f} against "
            f"{single:.2f} through one"
        )

    def test_each_choke_holds_its_own_position(self):
        sim = shut_in()
        sim.flow_path.open("choke_1")
        sim.choke.command(40.0)
        sim.manual_choke_1.command(10.0)
        sim.run(20.0, dt=0.05)

        assert sim.choke.position == pytest.approx(40.0, abs=0.5)
        assert sim.manual_choke_1.position == pytest.approx(10.0, abs=0.5)
        assert sim.choke_2.position == pytest.approx(0.0, abs=1e-6)

    def test_a_handwheel_moves_slower_than_the_remote(self):
        """The difference a student feels when the remote fails."""
        sim = shut_in()
        sim.choke.command(100.0)
        sim.manual_choke_1.command(100.0)
        sim.run(5.0, dt=0.05)
        assert sim.choke.position > sim.manual_choke_1.position

    def test_the_gauges_follow_the_choke_in_service(self):
        """A student changed over to choke 2 should see the handle they are
        working, not the one they abandoned."""
        sim = shut_in()
        sim.flow_path.open("choke_2")
        sim.choke_2.command(35.0)
        sim.run(20.0, dt=0.05)

        assert sim.choke_in_service is sim.choke_2
        assert sim.readout()["Choke (%)"] == pytest.approx(35.0, abs=0.5)

    def test_a_plugged_choke_can_be_changed_over(self):
        """The whole reason there is more than one.

        The fault plugs the choke actually being flown; the other is untouched,
        so lining it up restores control.
        """
        sim = shut_in()
        sim.flow_path.open("choke_1")
        sim.choke.command(40.0)
        sim.run(30.0, dt=0.05)
        assert sim.state.flow_out_bpm > 0

        sim._break_equipment(_fault("choke_plugged"))
        sim.run(30.0, dt=0.05)
        assert sim.state.flow_out_bpm < 0.05, "the plugged choke passes nothing"

        sim.flow_path.close("choke_1")
        sim.flow_path.open("choke_2")
        sim.choke_2.command(40.0)
        sim.run(30.0, dt=0.05)
        assert sim.state.flow_out_bpm > 0, "changing over should restore returns"

    def test_nothing_lined_up_passes_nothing(self):
        sim = shut_in()
        sim.run(30.0, dt=0.05)
        assert sim.state.flow_out_bpm == pytest.approx(0.0, abs=1e-9)


def _fault(kind: str):
    """The smallest thing `_break_equipment` will accept."""
    from wellcontrol.sim.scenario import Fault, FaultKind

    return Fault(kind=FaultKind(kind), at_seconds=0.0)
