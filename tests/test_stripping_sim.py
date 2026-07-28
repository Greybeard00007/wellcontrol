"""Stripping pipe back to bottom through a closed BOP.

The last of the methods, and the only one where the *pipe* is the control. The
student runs pipe in and bleeds the steel volume it displaces, holding
bottomhole pressure constant. Nothing circulates and the pump stays off.

**Closed-end displacement is the number the method turns on.** A string being
stripped has a float in it, so the bore goes in full and displaces along with
the metal: it is the whole outside diameter that counts, not the wall. Taking
the open-end figure under-states the bleed by a factor of about three.

The geometry here is cross-checked against the planner, which computes the same
operation independently in ``wellcontrol/stripping.py``.
"""

from __future__ import annotations

import pytest

from wellcontrol import examples
from wellcontrol.sim import Simulator
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)
OFF_BOTTOM_FT = 1000.0
STAND_FT = 93.0


def stripping_rig(**kwargs) -> Simulator:
    """Shut in with the bit off bottom, lined up to bleed through the choke."""
    well = examples.well_state(SV, bit_off_bottom_ft=OFF_BOTTOM_FT)
    sim = Simulator(well=well, **kwargs)
    sim.flow_path.close("annular")
    sim.flow_path.open("choke_line", "choke_line_to_manifold", "choke_1")
    sim.choke.command(0.0)
    return sim


class TestDisplacement:
    def test_the_bit_starts_off_bottom(self):
        sim = stripping_rig()
        assert sim.state.bit_md_ft == pytest.approx(sim.hole_md - OFF_BOTTOM_FT)

    def test_closed_end_displacement_matches_the_planner(self):
        """Same well, same pipe, computed by two separate routes."""
        from wellcontrol.methods import get_method

        sim = stripping_rig()
        summary = get_method("stripping").plan(sim.well).summary
        assert sim.closed_end_displacement_bbl_per_ft == pytest.approx(
            summary["Displacement (bbl/ft)"], rel=0.01
        )

    def test_a_stand_displaces_the_planners_volume(self):
        from wellcontrol.methods import get_method

        sim = stripping_rig()
        summary = get_method("stripping").plan(sim.well).summary
        per_stand = sim.closed_end_displacement_bbl_per_ft * STAND_FT
        assert per_stand == pytest.approx(summary["Volume per stand (bbl)"], abs=0.1)

    def test_closed_end_is_used_not_open_end(self):
        """The open-ended figure is about a third of it, and would mislead."""
        sim = stripping_rig()
        pipe = max(
            sim.well.annulus.string.components, key=lambda c: c.length_ft
        )
        assert sim.closed_end_displacement_bbl_per_ft > (
            2.5 * pipe.metal_displacement_bbl_per_ft
        )

    def test_running_in_displaces_steel(self):
        sim = stripping_rig()
        sim.strip(30.0)
        sim.run(120, dt=0.1)
        assert sim.state.pipe_moved_ft == pytest.approx(60.0, rel=0.05)
        assert sim.state.pipe_displacement_bbl == pytest.approx(
            sim.closed_end_displacement_bbl_per_ft * sim.state.pipe_moved_ft,
            rel=0.01,
        )

    def test_pulling_out_takes_steel_back_out(self):
        sim = stripping_rig()
        sim.strip(-30.0)
        sim.run(60, dt=0.1)
        assert sim.state.pipe_moved_ft < 0
        assert sim.state.pipe_displacement_bbl < 0

    def test_the_pipe_stops_at_the_bottom_of_the_hole(self):
        sim = stripping_rig(migration_rate_ft_per_hr=0.0)
        sim.strip(600.0)
        sim.choke.command(30.0)
        sim.run(600, dt=0.1)
        assert sim.state.bit_md_ft <= sim.hole_md + 1.0


class TestThePressureResponse:
    def test_running_pipe_in_with_the_choke_shut_raises_pressure(self):
        """Steel going in is volume going in, and it has nowhere to go."""
        sim = stripping_rig(migration_rate_ft_per_hr=0.0)
        before = sim.state.casing_pressure_psi
        sim.strip(30.0)
        sim.run(60, dt=0.1)
        assert sim.state.casing_pressure_psi > before + 50.0

    def test_not_bleeding_at_all_over_pressures_the_well(self):
        sim = stripping_rig(migration_rate_ft_per_hr=0.0)
        target = sim.formation_pressure_psi
        sim.strip(30.0)
        sim.run(180, dt=0.1)
        assert sim.state.bottomhole_pressure_psi > target + 100.0

    def test_the_pipe_standing_still_displaces_nothing(self):
        sim = stripping_rig(migration_rate_ft_per_hr=0.0)
        sim.strip(0.0)
        sim.run(120, dt=0.1)
        assert sim.state.pipe_displacement_bbl == pytest.approx(0.0)


def strip_to_bottom(sim: Simulator, stands: int = 12) -> Simulator:
    """Run in a stand at a time, working the choke to hold bottomhole pressure."""
    target = sim.formation_pressure_psi
    previous = [target - sim.state.bottomhole_pressure_psi]

    def hold(seconds: float) -> None:
        for _ in range(int(seconds / 0.1)):
            error = target - sim.state.bottomhole_pressure_psi
            rate = (error - previous[0]) / 0.1
            previous[0] = error
            sim.choke.command(
                sim.choke.commanded_position - 0.05 * error - 0.3 * rate
            )
            sim.step(0.1)

    for _ in range(stands):
        start = sim.state.bit_md_ft
        to_go = min(STAND_FT, sim.hole_md - start)
        if to_go <= 0.5:
            break
        sim.strip(30.0)
        guard = 0
        while sim.state.bit_md_ft - start < to_go - 0.5 and guard < 600:
            hold(2.0)
            guard += 1
        sim.strip(0.0)
        hold(60.0)
    return sim


@pytest.fixture(scope="module")
def stripped():
    return strip_to_bottom(stripping_rig())


class TestStrippingBackToBottom:
    def test_the_bit_gets_there(self, stripped):
        assert stripped.state.bit_md_ft == pytest.approx(stripped.hole_md, abs=1.0)

    def test_the_total_displacement_matches_the_planner(self, stripped):
        from wellcontrol.methods import get_method

        summary = get_method("stripping").plan(stripped.well).summary
        assert stripped.state.pipe_displacement_bbl == pytest.approx(
            summary["Total volume (bbl)"], rel=0.02
        )

    def test_bottomhole_pressure_is_held(self, stripped):
        assert stripped.state.max_bhp_excursion_psi < 300.0

    def test_the_well_is_not_let_go_underbalanced(self, stripped):
        assert stripped.state.influx_taken_bbl < 0.5

    def test_the_shoe_survives(self, stripped):
        assert stripped.state.peak_shoe_pressure_psi < stripped.frac_pressure_psi

    def test_more_is_bled_than_the_steel_displaces(self, stripped):
        """Because the gas keeps migrating while the pipe goes in.

        How much more is a live question -- see the note in CLAUDE.md about the
        planner's migration allowance, which is computed on a different basis
        and comes out several times larger.
        """
        assert stripped.state.bled_bbl > stripped.state.pipe_displacement_bbl

    def test_nothing_was_circulated(self, stripped):
        """Stripping is done with the pump off; the volumes all come from the pipe."""
        assert stripped.state.pumped_bbl == pytest.approx(0.0)
