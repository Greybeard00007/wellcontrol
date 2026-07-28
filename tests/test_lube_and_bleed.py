"""Lube and Bleed, and the flow path that makes it possible.

The method only exists once the gas is at surface: pump a measured slug of mud
in on top of it, let the mud fall through the gas to sit beneath it, then bleed
off the gas the mud displaced. Repeat until there is no gas left. Nothing
circulates and the influx never goes anywhere except out of the choke.

**It needs the pump at the other end of the well.** Every other method pumps
down the string, where mud comes round the bit and pushes the annulus up from
below. Lubricating puts mud into the annulus from the *top*, through the kill
line. Same pump, same barrels, opposite ends -- and the engine has to know
which, because the two do completely different things to the fluid column.
"""

from __future__ import annotations

import pytest

from wellcontrol import examples
from wellcontrol.sim import FlowPath, Node, Simulator
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)


class TestTheKillLineRunsBothWays:
    """A gate valve does not care which way the flow goes."""

    def test_returns_can_leave_through_the_kill_line(self):
        path = FlowPath()
        path.close("annular")
        path.open("kill_line", "kill_line_to_manifold", "choke_1")
        assert path.return_route().through_choke

    def test_the_pump_can_go_in_through_the_kill_line(self):
        path = FlowPath()
        path.close("annular")
        path.open("pump_to_kill_line", "kill_line")
        assert not path.pump_is_deadheaded
        assert Node.KILL_LINE in path.injection_path()

    def test_that_route_does_not_go_through_the_string(self):
        path = FlowPath()
        path.close("annular")
        path.open("pump_to_kill_line", "kill_line")
        assert path.injecting_into_the_annulus

    def test_pumping_down_the_string_is_not_lubricating(self):
        path = FlowPath()
        path.close("annular")
        path.open("pump_discharge", "standpipe_valve")
        assert not path.injecting_into_the_annulus

    def test_a_shut_kill_line_still_blocks_the_pump(self):
        """Bidirectional does not mean always open."""
        path = FlowPath()
        path.close("annular")
        path.open("pump_to_kill_line")
        assert path.pump_is_deadheaded


def gas_at_surface() -> Simulator:
    """Bring the influx up under volumetric control, which is how it arrives.

    Letting it climb unbled for hours would leave the well grossly
    over-pressured -- a different exercise, and one that squeezes the zone.
    """
    sim = Simulator(well=examples.well_state(SV))
    sim.flow_path.close("annular")
    sim.flow_path.open("choke_line", "choke_line_to_manifold", "choke_1")
    sim.choke.command(0.0)
    target = sim.formation_pressure_psi
    guard = 0
    while not sim.state.gas_at_surface and guard < 4000:
        over = sim.state.bottomhole_pressure_psi - target
        sim.choke.command(0.0 if over < 100.0 else min(20.0, 0.1 * over))
        sim.run(20, dt=0.2)
        guard += 1
    sim.choke.command(0.0)
    sim.run(30, dt=0.1)
    return sim


def line_up_to_lubricate(sim: Simulator) -> None:
    sim.flow_path.close("pump_discharge", "standpipe_valve")
    sim.flow_path.open("pump_to_kill_line", "kill_line")


@pytest.fixture(scope="module")
def at_surface():
    return gas_at_surface()


class TestGettingThereAtAll:
    def test_the_gas_reaches_surface(self, at_surface):
        assert at_surface.state.gas_at_surface

    def test_it_expanded_on_the_way_up(self, at_surface):
        """Boyle, over four thousand feet of falling pressure."""
        assert at_surface.state.influx_volume_bbl > 3 * at_surface.well.pit_gain_bbl

    def test_holding_bottomhole_pressure_took_no_further_influx(self, at_surface):
        assert at_surface.state.influx_taken_bbl == pytest.approx(0.0, abs=0.05)


@pytest.fixture(scope="module")
def lubed():
    """Returns the simulator and the gas volume it started the method with.

    Not the original 20 bbl kick: by the time it is at surface Boyle has taken
    it to four times that, and the method is judged against what is actually
    up there.
    """
    sim = gas_at_surface()
    started_with = sim.state.influx_volume_bbl
    line_up_to_lubricate(sim)
    for _ in range(10):
        sim.choke.command(0.0)
        start = sim.state.lubricated_bbl
        sim.pump.command(10.0)
        guard = 0
        while sim.state.lubricated_bbl - start < 5.0 and guard < 400:
            sim.run(5, dt=0.1)
            guard += 1
        sim.pump.stop()
        sim.run(30, dt=0.1)

        before = sim.state.casing_pressure_psi
        sim.choke.command(12.0)
        guard = 0
        while sim.state.casing_pressure_psi > before - 120.0 and guard < 400:
            sim.run(5, dt=0.1)
            guard += 1
        sim.choke.command(0.0)
        sim.run(30, dt=0.1)
    return sim, started_with


class TestFlyingLubeAndBleed:
    def test_mud_goes_into_the_annulus_not_round_the_bit(self, lubed):
        """The distinction the flow path exists to make."""
        sim, _ = lubed
        assert sim.state.lubricated_bbl > 40.0
        assert sim.state.pumped_bbl == pytest.approx(0.0)

    def test_the_gas_is_reduced(self, lubed):
        sim, started_with = lubed
        assert sim.state.influx_volume_bbl < 0.6 * started_with

    def test_casing_pressure_comes_down(self, lubed):
        sim, _ = lubed
        assert sim.state.casing_pressure_psi < 1100.0

    def test_gas_was_bled_off(self, lubed):
        sim, _ = lubed
        assert sim.state.bled_bbl > 20.0

    def test_the_well_is_never_let_go_underbalanced(self, lubed):
        sim, _ = lubed
        assert sim.state.influx_taken_bbl == pytest.approx(0.0, abs=0.05)

    def test_the_shoe_survives(self, lubed):
        sim, _ = lubed
        assert sim.state.peak_shoe_pressure_psi < sim.frac_pressure_psi


class TestBullheadingLateDoesNotWork:
    """The caveat the injection model has to honour.

    Pushing on a well whose gas has risen to surface squeezes mud into the
    formation and leaves the gas exactly where it is. Bullheading works on a
    kick that is still against the zone it came from, and that is the whole
    reason it is done early or not at all.
    """

    def test_squeezing_a_surfaced_kick_does_not_push_the_gas_back(self):
        """Pump hard on a well whose gas is at surface and watch it achieve
        nothing except pushing mud into the formation.

        A fresh well is built rather than reusing the shared one, because this
        deliberately over-pressures it and would poison anything downstream.
        """
        sim = gas_at_surface()
        before_gas = sim._gas_pv
        sim.flow_path.close("choke_1")
        sim.flow_path.open("pump_discharge", "standpipe_valve")
        sim.choke.command(0.0)
        sim.pump.command(40.0)

        guard = 0
        while sim.state.injected_bbl <= 0.0 and guard < 60:
            sim.run(30, dt=0.1)
            guard += 1
        sim.pump.stop()

        assert sim.state.injected_bbl > 0.0, "the zone should be taking fluid"
        assert sim._gas_pv == pytest.approx(before_gas, rel=0.02)
