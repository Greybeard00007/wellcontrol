"""Gas migration, and the Volumetric method it makes possible.

Every other method in the simulator is driven by the pump. Volumetric is not:
the well is shut in, nothing is circulating, and the thing moving is the gas
itself. Without migration the simulator simply cannot represent it, and a
shut-in well would sit still forever -- which is exactly what it used to do.

**The number to hold on to.** Gas migrating in a closed well takes its pressure
with it: it cannot expand, so it arrives at a shallower depth still carrying the
pressure it had lower down, and everything beneath it gains that hydrostatic.
Surface pressure therefore climbs at the mud gradient times the speed the gas is
rising. The engine is not told this. It falls out of feeding the expansion the
gas *wants* into the same volume balance as every other flow.
"""

from __future__ import annotations

import pytest

from wellcontrol import examples
from wellcontrol.sim import EventKind, Simulator
from wellcontrol.sim.engine import DEFAULT_MIGRATION_FT_PER_HR
from wellcontrol.units import PSI_PER_PPG_FT
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)
SH = WellConfiguration(BOPLocation.SURFACE, Trajectory.HORIZONTAL)


def shut_in(config=SV, **kwargs) -> Simulator:
    sim = Simulator(well=examples.well_state(config), **kwargs)
    sim.flow_path.close("annular")
    sim.flow_path.open("choke_line", "choke_line_to_manifold", "choke_1")
    sim.choke.command(0.0)
    return sim


class TestTheGasClimbs:
    def test_a_vertical_well_migrates_by_default(self):
        """The well reports no *measured* rate, but gas still rises."""
        sim = shut_in()
        assert sim.well.gas_migration_rate_ft_per_hr == 0.0
        assert sim.migration_rate_ft_per_hr == DEFAULT_MIGRATION_FT_PER_HR

    def test_a_measured_rate_wins_over_the_default(self):
        sim = shut_in(migration_rate_ft_per_hr=250.0)
        assert sim.migration_rate_ft_per_hr == pytest.approx(250.0)

    def test_the_influx_rises_at_about_the_stated_rate(self):
        sim = shut_in()
        start = sim.state.influx_top_md
        sim.run(1800, dt=0.1)                       # half an hour
        climbed = start - sim.state.influx_top_md
        assert climbed == pytest.approx(
            DEFAULT_MIGRATION_FT_PER_HR / 2.0, rel=0.15
        )

    def test_gas_in_a_lateral_does_not_migrate(self):
        """No up to go in. This is why Volumetric is blocked on a horizontal."""
        sim = shut_in(SH)
        assert sim.migration_rate_ft_per_hr == 0.0
        before = sim.state.casing_pressure_psi
        sim.run(1800, dt=0.1)
        assert sim.state.casing_pressure_psi == pytest.approx(before, abs=2.0)

    def test_migration_can_be_switched_off(self):
        sim = shut_in(migration_rate_ft_per_hr=0.0)
        before = sim.state.influx_top_md
        sim.run(600, dt=0.1)
        assert sim.state.influx_top_md == pytest.approx(before, abs=1.0)


class TestThePressureRise:
    def test_surface_pressure_climbs_on_a_well_nobody_is_touching(self):
        """The signature of migration, and the reason to do anything at all."""
        sim = shut_in()
        before = sim.state.casing_pressure_psi
        sim.run(600, dt=0.1)
        assert sim.state.casing_pressure_psi > before + 20.0

    def test_bottomhole_pressure_climbs_with_it(self):
        """Gas carries its pressure up; the mud beneath adds its own."""
        sim = shut_in()
        before = sim.state.bottomhole_pressure_psi
        sim.run(600, dt=0.1)
        assert sim.state.bottomhole_pressure_psi > before + 20.0

    def test_the_rise_is_near_the_mud_gradient_times_the_climb(self):
        """The textbook result, arrived at without being told it.

        It comes out a little under, and that is not an error: the idealised
        figure assumes the gas cannot expand at all, while the engine gives the
        system real compliance -- mud compressibility and casing expansion --
        which absorbs part of the volume the gas wants. On this well that is
        about 30%, set by the gas volume against the mud volume in
        ``_capacitance_bbl_per_psi``.
        """
        sim = shut_in()
        before_casing = sim.state.casing_pressure_psi
        before_top = sim.state.influx_top_md
        sim.run(1800, dt=0.1)

        climbed_tvd = sim.well.survey.tvd_at(before_top) - sim.well.survey.tvd_at(
            sim.state.influx_top_md
        )
        idealised = PSI_PER_PPG_FT * sim.well.mud_weight_ppg * climbed_tvd
        actual = sim.state.casing_pressure_psi - before_casing
        assert 0.55 * idealised < actual < idealised

    def test_the_well_says_so_in_the_log(self):
        sim = shut_in()
        sim.run(60, dt=0.1)
        assert sim.state.event_log.has(EventKind.GAS_MIGRATING)

    def test_left_alone_it_walks_the_shoe_toward_fracture(self):
        """Doing nothing is not a neutral choice, which is the lesson."""
        sim = shut_in()
        before = sim.state.shoe_pressure_psi
        sim.run(3600, dt=0.2)
        assert sim.state.shoe_pressure_psi > before + 50.0

    def test_pressure_times_volume_still_holds_while_shut_in(self):
        """Nothing enters or leaves, so the gas inventory must not drift."""
        sim = shut_in()
        before = sim._gas_pv
        sim.run(1200, dt=0.1)
        assert sim._gas_pv == pytest.approx(before, rel=0.02)


class TestBleeding:
    def test_bleeding_is_measured(self):
        sim = shut_in()
        sim.choke.command(20.0)
        sim.run(120, dt=0.1)
        assert sim.state.bled_bbl > 0.0

    def test_circulating_returns_are_not_counted_as_bleeding(self):
        """Bleeding is what comes out with nothing going in."""
        sim = shut_in()
        sim.flow_path.open("pump_discharge", "standpipe_valve")
        sim.pump.command(30.0)
        sim.choke.command(20.0)
        sim.run(120, dt=0.1)
        assert sim.state.bled_bbl == pytest.approx(0.0)

    def test_bleeding_brings_the_pressure_back_down(self):
        sim = shut_in()
        sim.run(900, dt=0.1)                        # let it climb
        risen = sim.state.casing_pressure_psi
        sim.choke.command(20.0)
        sim.run(120, dt=0.1)
        assert sim.state.casing_pressure_psi < risen

    def test_bleeding_too_hard_lets_the_well_flow_again(self):
        """The failure the method is built to avoid, and it is not scripted."""
        sim = shut_in()
        sim.choke.command(60.0)
        sim.run(600, dt=0.1)
        assert sim.state.bottomhole_pressure_psi < sim.formation_pressure_psi
        assert sim.state.influx_taken_bbl > 0.0


@pytest.fixture(scope="module")
def flown():
    sim = shut_in()
    target = sim.formation_pressure_psi
    start = sim.state.casing_pressure_psi

    guard = 0
    while sim.state.casing_pressure_psi < start + 100.0 and guard < 500:
        sim.run(10, dt=0.1)
        guard += 1
    ceiling = sim.state.casing_pressure_psi

    for _ in range(6):
        guard = 0
        while sim.state.casing_pressure_psi < ceiling + 100.0 and guard < 500:
            sim.run(10, dt=0.1)
            guard += 1
        guard = 0
        while sim.state.casing_pressure_psi > ceiling and guard < 600:
            error = sim.state.casing_pressure_psi - ceiling
            sim.choke.command(max(6.0, min(30.0, 0.4 * error)))
            sim.run(1, dt=0.1)
            guard += 1
        sim.choke.command(0.0)
        sim.run(20, dt=0.1)
        if sim.state.gas_at_surface:
            break
    return sim, target


class TestFlyingTheVolumetricMethod:
    """Hold, let it rise a working increment, bleed it back. Repeat."""

    def test_the_gas_gets_a_long_way_up_the_hole(self, flown):
        sim, _ = flown
        assert sim.state.influx_top_md < 7000.0

    def test_the_gas_expands_on_the_way(self, flown):
        sim, _ = flown
        assert sim.state.influx_volume_bbl > sim.well.pit_gain_bbl

    def test_mud_was_bled_off_to_make_room(self, flown):
        sim, _ = flown
        assert sim.state.bled_bbl > 2.0

    def test_the_well_never_goes_underbalanced(self, flown):
        """The whole point: hold bottomhole pressure without circulating."""
        sim, _ = flown
        assert sim.state.influx_taken_bbl == pytest.approx(0.0, abs=0.05)

    def test_the_shoe_survives(self, flown):
        sim, _ = flown
        assert sim.state.peak_shoe_pressure_psi < sim.frac_pressure_psi

    def test_it_takes_hours_not_minutes(self, flown):
        """Volumetric is slow, and a student should feel that it is."""
        sim, _ = flown
        assert sim.state.time_s > 3600.0
