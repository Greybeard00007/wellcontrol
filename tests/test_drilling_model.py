"""Making hole, and the drilling break that warns before the pit gain does.

The drilling model earns its place for one reason: **penetration rate responds
to differential pressure**, so drilling into a zone the mud cannot hold speeds
the bit up *because* the well is going underbalanced -- before anything reaches
the pits. That link is emergent here, not announced: the model reads the same
bottomhole pressure as everything else and knows nothing about kicks.

The other half of these tests is the lock-out. The drilling model must be
incapable of touching a kill.
"""

import pytest

from wellcontrol import examples
from wellcontrol.sim import Simulator
from wellcontrol.sim.drilling import DrillingModel, RotarySystem
from wellcontrol.sim.engine import StartState
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation("surface"), Trajectory("vertical"))


def drilling(**kwargs) -> Simulator:
    """A well drilling ahead: pump on, rotating, weight on bottom."""
    sim = Simulator(
        well=examples.well_state(SV), start=StartState.DRILLING,
        kick_after_s=None, **kwargs,
    )
    sim.flow_path.open("pump_discharge", "standpipe_valve")
    sim.pump.command(60.0)
    sim.drilling.rotary.command(120.0)
    sim.drilling.command_wob(20.0)
    sim.run(120.0, dt=0.05)          # past the ramps
    return sim


class TestTheDrillingBreak:
    def test_a_well_drilling_normally_sits_near_its_nominal_rate(self):
        """If the baseline were not near 1.0, a *change* would mean nothing."""
        sim = drilling()
        assert sim.drilling.hold_down_factor == pytest.approx(1.0, abs=0.15)
        assert 40 < sim.drilling.rop_ft_per_hr < 90

    def test_drilling_into_the_zone_roughly_doubles_the_rate(self):
        """The break. Nothing tells the bit a kick is coming."""
        sim = drilling()
        before = sim.drilling.rop_ft_per_hr

        sim.trigger_kick()
        sim.run(30.0, dt=0.05)

        assert sim.drilling.rop_ft_per_hr > 1.6 * before, (
            f"{before:.0f} -> {sim.drilling.rop_ft_per_hr:.0f} ft/hr"
        )

    def test_the_break_arrives_before_the_pit_gain(self):
        """The reason the model is here at all.

        Thirty seconds after the bit enters the zone the rate has already
        moved decisively, while the pit gain is still inside the noise a
        driller would ever act on.
        """
        sim = drilling()
        before = sim.drilling.rop_ft_per_hr

        sim.trigger_kick()
        sim.run(30.0, dt=0.05)

        assert sim.drilling.rop_ft_per_hr / before > 1.6
        assert sim.pits.gain_bbl < 1.0, (
            f"pit gain was already {sim.pits.gain_bbl:.2f} bbl -- the break "
            "should be the earlier signal"
        )

    def test_the_rate_follows_the_pressure_not_a_timer(self):
        """Restoring the overbalance takes the break away again."""
        model = DrillingModel()
        model.rotary.rpm = 120.0
        model.wob_klbf = 20.0
        fast = model.penetration_rate(overbalance_psi=0.0)
        slow = model.penetration_rate(overbalance_psi=900.0)
        assert fast > slow * 1.5

    def test_hold_down_is_clamped(self):
        """An unbounded exponential would let a wild well drill absurdly."""
        model = DrillingModel()
        assert model.hold_down(-100_000) <= 3.0
        assert model.hold_down(100_000) >= 0.15


class TestTheLockOut:
    """The drilling model must be incapable of touching a kill."""

    @pytest.mark.parametrize(
        "name,break_it",
        [
            ("BOP closed", lambda s: s.flow_path.close("annular")),
            ("pump off", lambda s: s.pump.stop()),
            ("rotary stopped", lambda s: s.drilling.rotary.stop()),
            ("no weight on bit", lambda s: s.drilling.command_wob(0.0)),
        ],
    )
    def test_each_condition_stops_the_bit(self, name, break_it):
        sim = drilling()
        assert sim.drilling_ahead, "should be drilling to start with"

        break_it(sim)
        sim.run(60.0, dt=0.05)      # long enough for any ramp to finish
        assert not sim.drilling_ahead, name

        made = sim.state.hole_drilled_ft
        sim.run(120.0, dt=0.05)
        assert sim.state.hole_drilled_ft == pytest.approx(made, abs=1e-6), name

    def test_a_shut_in_start_never_drills(self):
        """Every existing kill exercise is a shut-in start, and none of them
        may be touched by any of this."""
        sim = Simulator(well=examples.well_state(SV))
        sim.drilling.rotary.command(120.0)
        sim.drilling.command_wob(20.0)
        sim.run(120.0, dt=0.05)
        assert not sim.drilling_ahead
        assert sim.state.hole_drilled_ft == 0.0


class TestTheInstruments:
    def test_weight_on_bit_comes_off_the_hook(self):
        """The relation a driller actually works to: lower the block until the
        indicator drops by the weight you want on bottom."""
        sim = drilling()
        loaded = sim.hookload_lbf

        sim.drilling.command_wob(0.0)
        sim.run(60.0, dt=0.05)
        assert sim.hookload_lbf > loaded
        assert sim.hookload_lbf - loaded == pytest.approx(20_000, rel=0.1)

    def test_torque_rises_with_weight(self):
        sim = drilling()
        light = sim.drilling.rotary.torque_kftlb
        sim.drilling.command_wob(40.0)
        sim.run(60.0, dt=0.05)
        assert sim.drilling.rotary.torque_kftlb > light

    def test_torque_is_zero_with_the_rotary_stopped(self):
        sim = drilling()
        sim.drilling.rotary.stop()
        sim.run(30.0, dt=0.05)
        assert sim.drilling.rotary.torque_kftlb == 0.0

    def test_the_rotary_ramps_rather_than_jumping(self):
        rotary = RotarySystem(ramp_rate=15.0)
        rotary.command(150.0)
        rotary.advance(1.0)
        assert rotary.rpm == pytest.approx(15.0)

    def test_a_worn_bit_drills_slower_and_drags_more(self):
        sharp = DrillingModel()
        sharp.rotary.rpm = 120.0
        sharp.wob_klbf = 20.0
        fast = sharp.penetration_rate(500.0)
        easy = sharp.torque()

        sharp.bit_wear = 0.9
        assert sharp.penetration_rate(500.0) < fast
        assert sharp.torque() > easy

    def test_the_auto_driller_holds_its_setpoint(self):
        sim = drilling()
        sim.drilling.set_auto(True, setpoint_klbf=35.0)
        sim.run(60.0, dt=0.05)
        assert sim.drilling.wob_klbf == pytest.approx(35.0, abs=0.5)

    def test_picking_up_off_bottom_drops_the_weight(self):
        """Not a stale number left on the gauge."""
        sim = drilling()
        assert sim.drilling.wob_klbf > 10

        sim.state.bit_md_ft -= 30.0     # off bottom
        sim.run(60.0, dt=0.05)
        assert sim.drilling.wob_klbf == pytest.approx(0.0, abs=0.1)


class TestMakingHole:
    def test_the_hole_and_the_bit_advance_together(self):
        sim = drilling()
        assert sim.state.hole_drilled_ft > 0.5
        assert sim.state.bit_md_ft == pytest.approx(sim.hole_md, abs=1.0)

    def test_new_hole_takes_mud_from_the_pits(self):
        """Drilling ahead consumes mud, because new hole is new volume."""
        sim = drilling()
        before = sim.pits.active_bbl
        sim.run(600.0, dt=0.05)
        assert sim.state.hole_drilled_ft > 5.0
        assert sim.pits.active_bbl < before


class TestThroughTheSession:
    """The controls a student actually reaches, and what the gauges carry."""

    def test_the_gauges_carry_the_drilling_instruments(self):
        from wellcontrol.sim import Session

        session = Session.for_configuration(start="drilling")
        gauges = session.gauges()
        assert set(gauges["drilling"]) >= {
            "wob_klbf", "rpm", "torque_kftlb", "rop_ft_per_hr",
            "bit_wear", "auto_driller",
        }
        assert gauges["drilling_ahead"] is False
        assert gauges["hole_md_ft"] > 0

    def test_the_controls_reach_the_model(self):
        from wellcontrol.sim import Session

        session = Session.for_configuration(start="drilling")
        session.set_rotary(90.0)
        session.set_weight_on_bit(25.0)
        assert session.simulator.drilling.rotary.commanded_rpm == 90.0
        assert session.simulator.drilling.commanded_wob_klbf == 25.0

        session.set_auto_driller(True, 30.0)
        assert session.simulator.drilling.auto_driller
        assert session.simulator.drilling.wob_setpoint_klbf == 30.0
