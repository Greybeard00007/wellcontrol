"""Bullheading: pushing the influx back where it came from.

The formation works both ways. Underbalanced it gives up gas through the
productivity index; pushed hard enough it takes fluid back. Bullheading is the
deliberate use of the second, and it is the one method that does not bring the
influx to surface at all.

**The threshold is the interesting part.** A zone does not start accepting mud
the moment it is overbalanced -- filter cake, near-wellbore skin and relative
permeability all have to be pushed through first. Without that threshold a
normal kill, flown a few tens of psi above pore pressure, would quietly bleed
into the formation for its whole duration, and bullheading would need no
pressure worth speaking of.
"""

from __future__ import annotations

import pytest

from wellcontrol import examples
from wellcontrol.sim import Simulator
from wellcontrol.sim.engine import INJECTION_THRESHOLD_PSI
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)


def bullheading(**kwargs) -> Simulator:
    """Shut in, pumping into a closed well with the choke shut."""
    sim = Simulator(
        well=examples.well_state(SV), migration_rate_ft_per_hr=0.0, **kwargs
    )
    sim.flow_path.close("annular")
    sim.flow_path.open("pump_discharge", "standpipe_valve")
    sim.choke.command(0.0)
    return sim


class TestTheFormationTakesFluidBothWays:
    def test_a_small_overbalance_goes_nowhere(self):
        """A well sitting overbalanced does not quietly bleed away."""
        sim = bullheading()
        sim.pump.command(1.0)
        sim.run(30, dt=0.1)
        over = sim.state.bottomhole_pressure_psi - sim.formation_pressure_psi
        if over < INJECTION_THRESHOLD_PSI:
            assert sim.state.injection_rate_bpm == pytest.approx(0.0)

    def test_pushing_past_the_threshold_starts_injection(self):
        sim = bullheading()
        sim.pump.command(15.0)
        sim.run(120, dt=0.1)
        over = sim.state.bottomhole_pressure_psi - sim.formation_pressure_psi
        assert over > INJECTION_THRESHOLD_PSI
        assert sim.state.injection_rate_bpm > 0.0

    def _circulating(self):
        sim = Simulator(well=examples.well_state(SV))
        sim.flow_path.close("annular")
        sim.flow_path.open(
            "pump_discharge", "standpipe_valve",
            "choke_line", "choke_line_to_manifold", "choke_1",
        )
        sim.choke.command(20.0)
        sim.pump.command(30.0)
        return sim

    def test_a_well_flown_kill_does_not_leak_into_the_formation(self):
        """Held on the formation, nothing goes back.

        Otherwise every kill would quietly lose mud to the zone for hours, and
        the injection model would be rewriting results it has no business
        touching.
        """
        sim = self._circulating()
        target = sim.formation_pressure_psi
        previous = target - sim.state.bottomhole_pressure_psi
        for _ in range(3000):
            error = target - sim.state.bottomhole_pressure_psi
            rate = (error - previous) / 0.1
            previous = error
            sim.choke.command(
                sim.choke.commanded_position - 0.02 * error - 0.2 * rate
            )
            sim.step(0.1)
        assert sim.state.injected_bbl == pytest.approx(0.0, abs=0.1)

    def test_a_badly_over_pressured_kill_does_push_fluid_back(self):
        """The other side of the same coin, and worth a student knowing.

        Hold far too much back-pressure and the zone starts taking mud. It
        looks like the kick is dying, because casing pressure stops climbing --
        but what is happening is that the well is being squeezed.
        """
        sim = self._circulating()
        sim.choke.command(6.0)               # far too tight, deliberately
        sim.run(300, dt=0.1)
        assert sim.state.injected_bbl > 0.5


@pytest.fixture(scope="module")
def bullheaded():
    sim = bullheading()
    sim.pump.command(15.0)
    for _ in range(30):
        sim.run(60, dt=0.1)
        if sim.state.influx_volume_bbl < 0.5:
            break
    return sim


class TestSqueezingTheInfluxAway:

    def test_the_influx_goes_back_into_the_zone(self, bullheaded):
        assert bullheaded.state.influx_volume_bbl < 0.5

    def test_it_never_came_to_surface(self, bullheaded):
        """The reason to bullhead: nothing is circulated out at all."""
        assert not bullheaded.state.gas_at_surface

    def test_the_volume_injected_is_recorded(self, bullheaded):
        assert bullheaded.state.injected_bbl > 5.0

    def test_it_takes_real_pressure(self, bullheaded):
        """Bullheading is not a nudge; the gauges show what it costs."""
        assert bullheaded.state.peak_casing_pressure_psi > 1000.0

    def test_this_well_breaks_its_shoe_doing_it(self, bullheaded):
        """Not scripted -- the surface pressure needed simply exceeds the shoe.

        Which is exactly why bullheading is a last resort on a well with a weak
        casing seat, and why the method's applicability check exists.
        """
        assert bullheaded.state.peak_shoe_pressure_psi > bullheaded.frac_pressure_psi
        assert bullheaded.state.lost_bbl > 0.0

    def test_pumping_gently_enough_does_not_break_it(self):
        """The same well, flown within its limits, keeps the shoe."""
        sim = bullheading()
        sim.pump.command(4.0)
        sim.run(600, dt=0.1)
        assert sim.state.peak_shoe_pressure_psi < sim.frac_pressure_psi
