"""The trip tank, the pits, and catching a kick while tripping.

A large share of kicks arrive on a trip, and the instrument that catches them
is a small calibrated tank. Pulling steel out of the hole leaves a void that
has to be filled, so the tank level falls by the volume of steel removed. If it
falls by *less* than that, something down the hole is giving fluid back.

None of that is scripted. It falls out of the same volume balance as everything
else, once the tank is actually in it -- which it was not before: pipe
displacement reached no surface vessel at all, so a trip moved no volume and a
kick taken during one was invisible.
"""

import pytest

from wellcontrol import examples
from wellcontrol.sim import Simulator
from wellcontrol.sim.engine import StartState
from wellcontrol.sim.equipment import Alarm, PitSystem, TripTank
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation("surface"), Trajectory("vertical"))


def drilling_well() -> Simulator:
    """A well lined up to drill: hole open, returns up the flowline."""
    sim = Simulator(well=examples.well_state(SV), start=StartState.DRILLING)
    sim.flow_path.open("pump_discharge", "standpipe_valve")
    return sim


def in_service() -> Simulator:
    """The lineup a driller is taught: tank to the annulus, fill-up pump on."""
    sim = drilling_well()
    sim.flow_path.open("trip_tank_valve")
    sim.trip_tank.fillup_pump.start()
    return sim


def pull(sim: Simulator, feet_per_min: float = 60.0, seconds: float = 60.0):
    """Pull pipe, and report the steel volume and what the tank did."""
    tank_before = sim.trip_tank.volume_bbl
    pits_before = sim.pits.active_bbl
    md_before = sim.state.bit_md_ft

    sim.strip(-feet_per_min)
    sim.run(seconds, dt=0.05)
    sim.strip(0.0)

    steel = (md_before - sim.state.bit_md_ft) * sim.closed_end_displacement_bbl_per_ft
    return (
        steel,
        sim.trip_tank.volume_bbl - tank_before,
        sim.pits.active_bbl - pits_before,
    )


class TestTheTankMeasuresTheTrip:
    def test_the_tank_falls_by_the_volume_of_steel_removed(self):
        """The whole basis of trip monitoring, and it has to be exact."""
        sim = in_service()
        steel, tank_change, _ = pull(sim)

        assert steel > 0.5, "expected a measurable amount of steel"
        assert tank_change == pytest.approx(-steel, abs=0.02)

    def test_running_pipe_back_in_puts_it_back(self):
        """Steel going in displaces mud out of the hole and the tank gains."""
        sim = in_service()
        pull(sim, seconds=60.0)

        tank_before = sim.trip_tank.volume_bbl
        md_before = sim.state.bit_md_ft
        sim.strip(60.0)
        sim.run(60.0, dt=0.05)
        sim.strip(0.0)

        steel = (
            sim.state.bit_md_ft - md_before
        ) * sim.closed_end_displacement_bbl_per_ft
        assert sim.trip_tank.volume_bbl - tank_before == pytest.approx(
            steel, abs=0.02
        )

    def test_a_flowing_well_makes_the_tank_rise_instead(self):
        """The kick signature: the hole gives back more than the steel took.

        Nothing here tells the tank a kick has happened. The influx displaces
        mud up the annulus, the annulus is open to the tank, and the level goes
        the wrong way -- which is what the driller sees.
        """
        sim = in_service()
        sim.trigger_kick()
        steel, tank_change, _ = pull(sim)

        assert sim.state.influx_volume_bbl > 1.0, "the well should be flowing"
        assert tank_change > 0, (
            "with the well giving back, the tank should rise even while "
            f"pulling steel out; it moved {tank_change:+.2f} bbl"
        )
        shortfall = steel + tank_change
        assert shortfall == pytest.approx(
            sim.state.influx_volume_bbl, rel=0.15
        ), "the discrepancy should account for the influx"


class TestWhatCountsAsInService:
    def test_both_the_valve_and_the_pump(self):
        """Lining up without starting the pump is the classic half-lineup."""
        sim = drilling_well()
        assert not sim.trip_tank_in_service

        sim.flow_path.open("trip_tank_valve")
        assert not sim.trip_tank_in_service, "valve alone is not in service"

        sim.trip_tank.fillup_pump.start()
        assert sim.trip_tank_in_service

    def test_a_half_lineup_leaves_the_trip_unmeasured(self):
        """The consequence is the rig's: the trip is not being measured.

        The volume still goes somewhere -- an 800 bbl active system -- where a
        barrel and a half does not show. That is exactly why a trip tank
        exists, and why getting the lineup wrong hides a kick until the pit
        gain is very much larger.
        """
        sim = drilling_well()
        sim.flow_path.open("trip_tank_valve")   # pump left off

        steel, tank_change, pit_change = pull(sim)
        assert tank_change == pytest.approx(0.0, abs=1e-6)
        assert pit_change == pytest.approx(-steel, abs=0.02)

    def test_closed_in_the_steel_becomes_pressure_not_volume(self):
        """Stripping into a shut-in well is the other half of the model.

        There is nowhere for the displaced volume to go, so it shows up as
        pressure -- which is the thing the student is bleeding against. It must
        not *also* appear in a tank, or it would be counted twice.
        """
        # Off bottom, because that is the well a stripping job starts on --
        # and because there has to be hole left to strip into.
        sim = Simulator(well=examples.well_state(SV, bit_off_bottom_ft=500.0))
        sim.flow_path.close("annular")
        sim.flow_path.open(
            "choke_line_valve", "choke_line_to_manifold", "choke_1"
        )
        sim.flow_path.open("trip_tank_valve")
        sim.trip_tank.fillup_pump.start()

        tank_before = sim.trip_tank.volume_bbl
        casing_before = sim.state.casing_pressure_psi

        sim.strip(60.0)
        sim.run(30.0, dt=0.05)

        assert sim.state.pipe_displacement_bbl > 0.3, "steel should have gone in"
        assert sim.trip_tank.volume_bbl == pytest.approx(tank_before, abs=1e-6)
        assert sim.state.casing_pressure_psi > casing_before


class TestTheTransferPumps:
    def test_filling_the_tank_takes_it_from_the_active_system(self):
        sim = drilling_well()
        tank_before = sim.trip_tank.volume_bbl
        pits_before = sim.pits.active_bbl

        sim.trip_tank.fill_pump.start()
        sim.run(60.0, dt=0.05)

        gained = sim.trip_tank.volume_bbl - tank_before
        assert gained > 1.0
        assert sim.pits.active_bbl - pits_before == pytest.approx(-gained, abs=0.01)

    def test_draining_puts_it_back(self):
        sim = drilling_well()
        tank_before = sim.trip_tank.volume_bbl
        pits_before = sim.pits.active_bbl

        sim.trip_tank.drain_pump.start()
        sim.run(60.0, dt=0.05)

        lost = tank_before - sim.trip_tank.volume_bbl
        assert lost > 1.0
        assert sim.pits.active_bbl - pits_before == pytest.approx(lost, abs=0.01)

    def test_the_tank_cannot_overflow(self):
        """A tank pumped past its top stops measuring, so it must clamp."""
        sim = drilling_well()
        sim.trip_tank.fill_pump.start()
        sim.run(600.0, dt=0.05)
        assert sim.trip_tank.volume_bbl == pytest.approx(
            sim.trip_tank.capacity_bbl, abs=0.01
        )

    def test_the_tank_cannot_go_below_empty(self):
        sim = drilling_well()
        sim.trip_tank.drain_pump.start()
        sim.run(600.0, dt=0.05)
        assert sim.trip_tank.volume_bbl == pytest.approx(0.0, abs=0.01)

    def test_a_reserve_transfer_reads_as_a_gain(self):
        """The false gain a student has to learn to discount.

        The level went up and the gauge cannot tell why. Re-zeroing after a
        transfer is the driller's job, and a rig that transfers without
        re-zeroing is a rig where the pit alarm gets ignored.
        """
        sim = drilling_well()
        assert sim.pits.gain_bbl == pytest.approx(0.0, abs=0.01)

        sim.pits.transfer_pump.start()
        sim.run(60.0, dt=0.05)

        assert sim.pits.gain_bbl > 5.0
        assert sim.pits.reserve_bbl < 400.0
        assert sim.state.influx_volume_bbl == pytest.approx(0.0, abs=0.01), (
            "nothing came out of the formation — this is a surface transfer"
        )


class TestAlarms:
    def test_hysteresis_stops_the_lamp_chattering(self):
        """A value sitting on its set point crosses it repeatedly."""
        alarm = Alarm(low=10.0, high=30.0, deadband=0.5)

        alarm.update(35.0)
        assert alarm.high_active
        alarm.update(29.8)
        assert alarm.high_active, "inside the deadband, the lamp holds"
        alarm.update(29.4)
        assert not alarm.high_active

        alarm.update(9.5)
        assert alarm.low_active
        alarm.update(10.3)
        assert alarm.low_active
        alarm.update(10.6)
        assert not alarm.low_active

    def test_disarming_silences_the_horn_but_not_the_lamp(self):
        """So the instructor can see what the student silenced."""
        alarm = Alarm(low=10.0, high=30.0, armed=False)
        alarm.update(35.0)
        assert alarm.high_active
        assert alarm.active
        assert not alarm.sounding

    def test_the_tank_level_alarm_fires(self):
        sim = drilling_well()
        sim.trip_tank.drain_pump.start()
        sim.run(180.0, dt=0.05)
        assert sim.trip_tank.volume_bbl < 10.0
        assert sim.trip_tank.alarm.low_active

    def test_the_pit_deviation_alarm_fires_on_a_gain(self):
        sim = drilling_well()
        sim.trigger_kick()
        sim.pump.command(60.0)
        sim.run(300.0, dt=0.05)
        assert sim.pits.gain_bbl > 10.0
        assert sim.pits.deviation_alarm.high_active


class TestTheVessels:
    def test_add_clamps_and_reports_what_moved(self):
        tank = TripTank(volume_bbl=30.0, capacity_bbl=35.0)
        assert tank.add(10.0) == pytest.approx(5.0)
        assert tank.volume_bbl == pytest.approx(35.0)
        assert tank.add(-100.0) == pytest.approx(-35.0)
        assert tank.volume_bbl == pytest.approx(0.0)

    def test_gain_is_measured_against_the_mark(self):
        pits = PitSystem(active_bbl=800.0, baseline_bbl=800.0)
        pits.add(12.0)
        assert pits.gain_bbl == pytest.approx(12.0)
        assert pits.deviation_bbl == pytest.approx(12.0)
        pits.zero()
        assert pits.gain_bbl == pytest.approx(0.0)

    def test_a_reserve_transfer_is_limited_by_what_is_there(self):
        pits = PitSystem(reserve_bbl=5.0)
        assert pits.transfer_reserve_to_active(100.0) == pytest.approx(5.0)
        assert pits.reserve_bbl == pytest.approx(0.0)
