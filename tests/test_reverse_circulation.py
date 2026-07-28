"""Reverse circulation: down the annulus, back up the drill pipe.

The influx travels up the **inside of the string**, and that is the whole
attraction. It never passes the casing shoe, so the shoe sees clean mud and a
falling surface pressure the moment circulation starts. The string also holds a
fraction of the annulus volume, so the job is over in far fewer strokes.

The engine has to swap which gauge is the independent one to do it. Forward, the
choke is on the annulus and casing pressure is what the volume balance sets.
Reverse, the choke is on the drill pipe and the pump is on the annulus, so the
roles change hands -- while both gauges go on meaning what they say.
"""

from __future__ import annotations

import pytest

from wellcontrol import examples
from wellcontrol.sim import FlowPath, Node, Simulator
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)

REVERSE_LINEUP = (
    "pump_to_kill_line", "kill_line",
    "float_valve", "standpipe_valve", "standpipe_to_manifold", "choke_1",
)


def reverse_rig(**kwargs) -> Simulator:
    sim = Simulator(
        well=examples.well_state(SV), migration_rate_ft_per_hr=0.0, **kwargs
    )
    sim.flow_path.close("annular")
    sim.flow_path.open(*REVERSE_LINEUP)
    sim.flow_path.close("pump_discharge")
    return sim


class TestTheLineup:
    def test_the_lineup_is_recognised(self):
        assert reverse_rig().flow_path.is_reverse_circulating

    def test_returns_come_up_the_string(self):
        route = reverse_rig().flow_path.return_route()
        assert Node.STRING in route.route
        assert route.through_choke

    def test_the_float_valve_makes_it_impossible(self):
        """Most strings run one, and it passes nothing back up the pipe.

        This is the single reason reverse circulation is usually unavailable,
        and the applicability check in the planner says so.
        """
        sim = reverse_rig()
        sim.flow_path.close("float_valve")
        assert not sim.flow_path.is_reverse_circulating
        assert not sim.flow_path.return_route().open_to_pits

    def test_pumping_into_the_annulus_alone_is_not_reverse_circulating(self):
        """That is lubricating. The difference is where the returns go."""
        path = FlowPath()
        path.close("annular")
        path.open("pump_to_kill_line", "kill_line",
                  "choke_line", "choke_line_to_manifold", "choke_1")
        assert path.injecting_into_the_annulus
        assert not path.is_reverse_circulating

    def test_forward_circulation_is_not_mistaken_for_it(self):
        path = FlowPath()
        path.close("annular")
        path.open("pump_discharge", "standpipe_valve",
                  "choke_line", "choke_line_to_manifold", "choke_1")
        assert not path.is_reverse_circulating


def fly(sim: Simulator, minutes: int = 60) -> Simulator:
    """Hold bottomhole pressure with the choke, which is now on the pipe side."""
    target = sim.formation_pressure_psi
    sim.choke.command(20.0)
    sim.run(10, dt=0.1)
    sim.pump.command(30.0)
    previous = target - sim.state.bottomhole_pressure_psi
    for _ in range(minutes * 600):
        error = target - sim.state.bottomhole_pressure_psi
        rate = (error - previous) / 0.1
        previous = error
        sim.choke.command(
            sim.choke.commanded_position - 0.02 * error - 0.2 * rate
        )
        sim.step(0.1)
        if sim.state.gas_at_surface:
            break
    return sim


@pytest.fixture(scope="module")
def flown():
    return fly(reverse_rig())


class TestTheInfluxGoesUpThePipe:
    def test_it_leaves_the_annulus(self, flown):
        assert flown.state.influx_in_string

    def test_it_reaches_surface_up_the_string(self, flown):
        assert flown.state.gas_at_surface
        assert flown.state.influx_string_top_md == pytest.approx(0.0, abs=1.0)

    def test_it_expands_on_the_way(self, flown):
        assert flown.state.influx_volume_bbl > flown.well.pit_gain_bbl

    def test_the_annulus_is_left_clean(self, flown):
        """No influx zone in the annular column once it has been pushed out."""
        labels = {zone.label for zone in flown._annulus_column().zones}
        assert "influx" not in labels

    def test_the_string_column_carries_it_instead(self, flown):
        labels = {zone.label for zone in flown._string_column().zones}
        assert "influx" in labels


class TestTheShoeAdvantage:
    """The reason to reverse circulate at all."""

    def test_the_shoe_sees_less_than_it_did_shut_in(self, flown):
        """Because the influx leaves the annulus, so the casing lightens.

        Every circulating method that brings the influx up the annulus makes
        the shoe worse on the way past. This one never does.
        """
        shut_in_shoe = flown.state.trace[0].shoe_psi
        assert flown.state.shoe_pressure_psi < shut_in_shoe

    def test_the_shoe_never_approaches_fracture(self, flown):
        assert flown.state.peak_shoe_pressure_psi < flown.frac_pressure_psi

    def test_it_is_quicker_than_going_round_the_annulus(self, flown):
        """The string holds a fraction of what the annulus does."""
        assert flown.string_volume_bbl < 0.5 * flown.annulus_volume_bbl
        assert flown.state.time_s < 3600.0


class TestFlyingIt:
    def test_bottomhole_pressure_is_held(self, flown):
        assert flown.state.max_bhp_excursion_psi < 500.0

    def test_no_further_influx_is_taken(self, flown):
        assert flown.state.influx_taken_bbl == pytest.approx(0.0, abs=0.1)

    def test_the_two_gauges_still_mean_what_they_say(self, flown):
        """Casing is the annulus and drill pipe is the pipe, either direction.

        With clean mud in the annulus and gas in the string, the annulus is the
        heavier column -- so it needs *less* surface pressure to balance the
        same bottomhole pressure, and the drill pipe gauge reads higher.
        """
        assert flown.state.casing_pressure_psi < flown.state.standpipe_pressure_psi


class TestNoArtificialSpikeOnTheHandover:
    """Switching which gauge the balance drives must not move the well.

    The arithmetic linking the two surface pressures changes when the influx
    enters the string. Left alone that appeared as a 4,500 psi step in
    bottomhole pressure, which promptly took 7 bbl of fresh influx -- an
    artefact of changing equations, not anything the student did.
    """

    def test_bottomhole_pressure_does_not_jump_when_the_influx_enters(self):
        sim = reverse_rig()
        sim.choke.command(20.0)
        sim.run(10, dt=0.1)
        sim.pump.command(30.0)

        worst = 0.0
        previous = sim.state.bottomhole_pressure_psi
        for _ in range(600):
            sim.step(0.1)
            worst = max(worst, abs(sim.state.bottomhole_pressure_psi - previous))
            previous = sim.state.bottomhole_pressure_psi
            if sim.state.influx_in_string and sim.state.time_s > 40:
                break
        assert worst < 250.0, f"bottomhole pressure stepped {worst:.0f} psi"

    def test_the_gas_volume_stays_physical_through_the_handover(self):
        """A stale string position once read the pressure at surface and put
        a 20 bbl bubble at 240."""
        sim = reverse_rig()
        sim.choke.command(20.0)
        sim.run(10, dt=0.1)
        sim.pump.command(30.0)
        for _ in range(600):
            sim.step(0.1)
            assert sim.state.influx_volume_bbl < 100.0
