"""Simulator tests.

Two kinds. First, that every failure mode a student must feel **emerges** from
the model rather than being scripted -- if these pass, the well is responding to
the controls rather than playing back an animation. Second, that a
well-flown kill reproduces the quasi-static solver, which is the check that the
forward model and the inverse model describe the same well.
"""

import pytest

from wellcontrol import examples
from wellcontrol.sim import (
    EventKind,
    EventLog,
    FlowPath,
    Grade,
    Node,
    Severity,
    Simulator,
    Standard,
    ValveState,
)
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration

SV = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)
SH = WellConfiguration(BOPLocation.SURFACE, Trajectory.HORIZONTAL)


def make(config=SV, **kwargs) -> Simulator:
    return Simulator(well=examples.well_state(config), **kwargs)


def lined_up_to_kill(sim: Simulator) -> Simulator:
    """Shut in on the annular, returns through choke 1."""
    sim.flow_path.close("annular")
    sim.flow_path.open(
        "pump_discharge", "standpipe_valve",
        "choke_line", "choke_line_to_manifold", "choke_1",
    )
    return sim


# --------------------------------------------------------------------------
# Lineup
# --------------------------------------------------------------------------

class TestFlowPath:
    def test_rig_starts_lined_up_for_drilling(self):
        path = FlowPath()
        assert not path.bop_is_closed
        assert not path.return_route().through_choke

    def test_closing_any_element_shuts_the_stack(self):
        for element in ("annular", "upper_rams", "lower_rams"):
            path = FlowPath()
            path.close(element)
            assert path.bop_is_closed

    def test_open_stack_returns_up_the_flowline(self):
        path = FlowPath()
        route = path.return_route()
        assert route.open_to_pits
        assert not route.through_choke
        assert Node.FLOWLINE in route.route

    def test_shut_in_with_nothing_open_is_closed_in(self):
        path = FlowPath()
        path.close("annular")
        assert path.is_closed_in

    def test_choke_line_gives_a_return_route_with_the_stack_shut(self):
        path = FlowPath()
        path.close("annular")
        path.open("choke_line", "choke_line_to_manifold", "choke_1")
        route = path.return_route()
        assert route.open_to_pits
        assert route.through_choke

    def test_kill_line_works_too(self):
        path = FlowPath()
        path.close("annular")
        path.open("kill_line", "kill_line_to_manifold", "choke_1")
        assert path.return_route().through_choke

    def test_pump_deadheads_without_a_path(self):
        path = FlowPath()
        assert path.pump_is_deadheaded

    def test_pump_path_opens_with_the_right_valves(self):
        path = FlowPath()
        path.open("pump_discharge", "standpipe_valve")
        assert not path.pump_is_deadheaded

    def test_closed_kelly_cock_blocks_the_pump(self):
        path = FlowPath()
        path.open("pump_discharge", "standpipe_valve")
        path.close("kelly_cock")
        assert path.pump_is_deadheaded

    def test_bypass_defeats_the_choke(self):
        path = FlowPath()
        path.close("annular")
        path.open("choke_line", "choke_line_to_manifold", "choke_bypass")
        assert path.bypass_is_open()

    def test_unknown_valve_is_rejected(self):
        with pytest.raises(KeyError, match="No valve called"):
            FlowPath().set_valve("magic_valve", ValveState.OPEN)


# --------------------------------------------------------------------------
# The behaviours a student must feel
# --------------------------------------------------------------------------

class TestEmergentBehaviour:
    """None of these are scripted. Each falls out of the volume balance."""

    def test_shut_in_well_sits_still_with_nothing_migrating(self):
        """Closed in, nothing flowing, gas staying put: nothing should move.

        The migration case is the opposite and is the whole of
        ``test_migration.py`` -- a shut-in well with gas climbing does *not*
        sit still, and a student who assumes it does loses the shoe.
        """
        sim = make(migration_rate_ft_per_hr=0.0)
        sim.flow_path.close("annular")
        before = sim.state.casing_pressure_psi
        sim.run(120)
        assert sim.state.casing_pressure_psi == pytest.approx(before, abs=5.0)

    def test_shut_in_well_holds_bottomhole_at_formation_pressure(self):
        sim = make()
        sim.flow_path.close("annular")
        sim.run(60)
        assert sim.state.bottomhole_pressure_psi == pytest.approx(
            sim.formation_pressure_psi, abs=15.0
        )

    def test_bop_open_means_the_choke_does_nothing(self):
        """The lesson students most need: you cannot control a well with the
        annular open, however hard you work the choke."""
        sim = make()
        sim.flow_path.open(
            "pump_discharge", "standpipe_valve",
            "choke_line", "choke_line_to_manifold", "choke_1",
        )
        sim.choke.command(2.0)          # nearly shut
        sim.pump.command(30.0)
        sim.run(60)
        assert sim.state.casing_pressure_psi == pytest.approx(0.0, abs=1.0)

    def test_pump_against_a_closed_valve_deadheads(self):
        sim = make()
        sim.flow_path.close("annular")
        sim.pump.command(30.0)
        sim.run(30)
        assert sim.flow_path.pump_is_deadheaded
        assert sim.state.flow_in_bpm == pytest.approx(0.0)

    def test_shutting_the_choke_on_a_running_pump_spikes_pressure(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(0.0)
        sim.pump.command(30.0)
        before = sim.state.casing_pressure_psi
        sim.run(30)
        assert sim.state.casing_pressure_psi > before + 150.0

    def test_choke_wide_open_lets_the_well_go_underbalanced(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(100.0)
        sim.pump.command(30.0)
        sim.run(60)
        assert sim.state.bottomhole_pressure_psi < sim.formation_pressure_psi

    def test_underbalance_takes_another_influx(self):
        sim = lined_up_to_kill(make())
        started_with = sim.state.influx_volume_bbl
        sim.choke.command(100.0)
        sim.pump.command(30.0)
        sim.run(120)
        assert sim.state.influx_volume_bbl > started_with + 2.0
        assert any("influx" in e for e in sim.state.events)

    def test_over_pressuring_the_shoe_takes_losses(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(0.0)
        sim.pump.command(60.0)
        sim.run(180)
        assert sim.state.loss_rate_bpm > 0.0
        assert any("Shoe over-pressured" in e for e in sim.state.events)

    def test_closed_in_with_the_pump_running_builds_pressure(self):
        sim = make()
        sim.flow_path.close("annular")
        sim.flow_path.open("pump_discharge", "standpipe_valve")
        sim.pump.command(20.0)
        before = sim.state.casing_pressure_psi
        sim.run(20)
        assert sim.state.casing_pressure_psi > before + 50.0


class TestChokeFeel:
    """The capacitance term is what makes the choke teachable."""

    def test_system_is_stiff_with_gas_deep(self):
        sim = lined_up_to_kill(make())
        assert sim._capacitance_bbl_per_psi() < 0.02

    def test_system_softens_as_gas_rises_and_expands(self):
        deep = lined_up_to_kill(make())
        stiff = deep._capacitance_bbl_per_psi()

        shallow = lined_up_to_kill(make())
        shallow._gas_pv = 500.0 * 100.0      # a lot of gas at low pressure
        shallow.state.pumped_bbl = 400.0
        shallow._settle_gas()
        shallow._update_pressures()

        assert shallow._capacitance_bbl_per_psi() > stiff * 3.0

    def test_same_choke_movement_matters_less_when_soft(self):
        """A fixed net inflow moves pressure far less with gas in the hole."""
        stiff = lined_up_to_kill(make())
        soft = lined_up_to_kill(make())
        soft._gas_pv = 500.0 * 100.0
        soft.state.pumped_bbl = 400.0
        soft._settle_gas()
        soft._update_pressures()

        rise_stiff = 1.0 / stiff._capacitance_bbl_per_psi()
        rise_soft = 1.0 / soft._capacitance_bbl_per_psi()
        assert rise_stiff > rise_soft * 3.0


class TestGasInventory:
    def test_gas_expands_as_it_rises_when_pressure_is_held(self):
        """Only true if the kill is flown properly.

        On a fixed choke the pressure runs away and the gas *compresses*
        instead -- which is correct, and is why this uses the autopilot.
        """
        sim = lined_up_to_kill(make())
        sim.choke.command(20.0)
        sim.pump.command(30.0)
        sim.run(30, dt=0.1)
        started_with = sim.state.influx_volume_bbl
        autopilot_kill(sim, seconds=600)
        assert sim.state.influx_volume_bbl > started_with

    def test_gas_compresses_on_a_choke_left_too_tight(self):
        """The other side of the same coin, and the more instructive one."""
        sim = lined_up_to_kill(make())
        sim.choke.command(18.0)
        sim.pump.command(30.0)
        started_with = sim.state.influx_volume_bbl
        sim.run(600, dt=0.1)
        assert sim.state.casing_pressure_psi > 1000.0
        assert sim.state.influx_volume_bbl < started_with

    def test_gas_moves_up_the_hole(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(18.0)
        sim.pump.command(30.0)
        start_top = sim.state.influx_top_md
        sim.run(600, dt=0.1)
        assert sim.state.influx_top_md < start_top

    def test_pressure_times_volume_is_conserved_while_shut_in(self):
        """No gas in, none out, so the inventory must not drift."""
        sim = make()
        sim.flow_path.close("annular")
        before = sim._gas_pv
        sim.run(120)
        assert sim._gas_pv == pytest.approx(before, rel=0.02)


# --------------------------------------------------------------------------
# Agreement with the quasi-static solver
# --------------------------------------------------------------------------

def autopilot_kill(sim: Simulator, seconds: float, dt: float = 0.1,
                   gain: float = 0.02, damping: float = 0.2) -> list[float]:
    """Fly the kill perfectly, holding bottomhole pressure on the formation.

    A proportional-derivative controller on the choke, standing in for a driller
    who never makes a mistake. Used to check the forward model against the
    inverse one: if the well is flown exactly right, the two must agree.

    **The damping term is not optional.** The plant integrates -- choke position
    sets a flow, and pressure is the running total of the imbalance -- and the
    choke is rate-limited on top of that. Proportional control alone limit
    cycles: it held bottomhole pressure to a steady 65 psi oscillation on a
    25-second period, which is a mediocre driller rather than a perfect one, and
    is not the thing the physics should be validated against.
    """
    target = sim.formation_pressure_psi
    previous_error = target - sim.state.bottomhole_pressure_psi
    trace: list[float] = []
    for _ in range(int(seconds / dt)):
        error = target - sim.state.bottomhole_pressure_psi
        rate = (error - previous_error) / dt
        previous_error = error
        sim.choke.command(
            sim.choke.commanded_position - gain * error - damping * rate
        )
        sim.step(dt)
        trace.append(sim.state.bottomhole_pressure_psi)
    return trace


class TestAgreesWithTheQuasiStaticSolver:
    def test_a_perfectly_flown_kill_holds_bottomhole_pressure(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(20.0)
        sim.pump.command(30.0)
        sim.run(30, dt=0.1)            # bring the pump up

        trace = autopilot_kill(sim, seconds=900)
        settled = trace[300:]          # ignore the first 30 s of settling
        target = sim.formation_pressure_psi

        worst = max(abs(p - target) for p in settled)
        assert worst < 75.0, f"bottomhole pressure wandered {worst:.0f} psi"

    def test_holding_bottomhole_pressure_prevents_further_influx(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(20.0)
        sim.pump.command(30.0)
        sim.run(30, dt=0.1)
        started_with = sim.state.influx_volume_bbl

        autopilot_kill(sim, seconds=600)

        # Gas expands as it rises, so volume grows -- but the inventory must
        # not, because nothing new should come in.
        assert sim.state.influx_volume_bbl > started_with
        assert sim.state.loss_rate_bpm == pytest.approx(0.0)

    def test_shoe_stays_under_the_fracture_pressure_when_flown_well(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(20.0)
        sim.pump.command(30.0)
        sim.run(30, dt=0.1)
        autopilot_kill(sim, seconds=900)
        assert sim.state.shoe_pressure_psi < sim.frac_pressure_psi


# --------------------------------------------------------------------------
# Event log
# --------------------------------------------------------------------------

class TestEventLog:
    """Conditions are logged on their edges, not on every timestep."""

    def test_condition_logs_once_while_it_stays_true(self):
        log = EventLog()
        for time_s in range(10):
            log.set_condition(
                EventKind.LOSSES, True, time_s=float(time_s), message="losing"
            )
        assert len(log.of_kind(EventKind.LOSSES)) == 1

    def test_condition_logs_again_after_it_clears(self):
        log = EventLog()
        log.set_condition(EventKind.LOSSES, True, time_s=0.0, message="losing")
        log.set_condition(EventKind.LOSSES, False, time_s=1.0)
        log.set_condition(EventKind.LOSSES, True, time_s=2.0, message="losing")
        assert len(log.of_kind(EventKind.LOSSES)) == 2

    def test_clearing_can_log_its_own_entry(self):
        log = EventLog()
        log.set_condition(EventKind.LOSSES, True, time_s=0.0, message="on")
        log.set_condition(
            EventKind.LOSSES, False, time_s=1.0, cleared_message="off"
        )
        assert [e.message for e in log] == ["on", "off"]

    def test_once_suppresses_repeats(self):
        log = EventLog()
        for time_s in range(5):
            log.record(
                EventKind.WELL_DEAD, "dead", time_s=float(time_s), once=True
            )
        assert len(log) == 1

    def test_hysteresis_stops_a_condition_chattering_on_its_threshold(self):
        """A value sitting on the limit must not log on every crossing."""
        log = EventLog()
        for step, value in enumerate([49.0, 51.0] * 20):
            log.set_level(
                EventKind.BHP_EXCURSION, value, rises_above=50.0,
                falls_below=30.0, time_s=float(step), message="off target",
            )
        assert len(log.of_kind(EventKind.BHP_EXCURSION)) == 1

    def test_hysteresis_still_clears_on_a_real_recovery(self):
        log = EventLog()
        log.set_level(EventKind.BHP_EXCURSION, 90.0, rises_above=50.0,
                      falls_below=30.0, time_s=0.0, message="off")
        log.set_level(EventKind.BHP_EXCURSION, 10.0, rises_above=50.0,
                      falls_below=30.0, time_s=1.0, cleared_message="back")
        assert not log.is_active(EventKind.BHP_EXCURSION)

    def test_a_reset_point_above_the_set_point_is_rejected(self):
        log = EventLog()
        with pytest.raises(ValueError, match="never clear"):
            log.set_level(EventKind.LOSSES, 1.0, rises_above=10.0,
                          falls_below=20.0, time_s=0.0)

    def test_worst_severity_is_reported(self):
        log = EventLog()
        log.record(EventKind.SHUT_IN, "a", time_s=0.0, severity=Severity.INFO)
        log.record(EventKind.LOSSES, "b", time_s=1.0, severity=Severity.CRITICAL)
        log.record(EventKind.BHP_EXCURSION, "c", time_s=2.0,
                   severity=Severity.CAUTION)
        assert log.worst_severity is Severity.CRITICAL

    def test_events_render_with_a_clock(self):
        log = EventLog()
        log.record(EventKind.SHUT_IN, "shut in", time_s=3725.0)
        assert str(log.events[0]) == "[01:02:05] shut in"


# --------------------------------------------------------------------------
# Trace and assessment
# --------------------------------------------------------------------------

FIRST_CIRCULATION = Standard(require_kill=False)


def fly_well(sim: Simulator, seconds: float = 930.0) -> Simulator:
    """A kill flown properly: the choke worked from the moment the pump starts."""
    sim.choke.command(20.0)
    sim.pump.command(30.0)
    autopilot_kill(sim, seconds=seconds)
    return sim


class TestTrace:
    def test_a_sample_is_recorded_at_the_start(self):
        sim = lined_up_to_kill(make())
        assert len(sim.state.trace) == 1
        assert sim.state.trace[0].time_s == 0.0

    def test_samples_accumulate_on_the_interval(self):
        sim = lined_up_to_kill(make(trace_interval_s=10.0))
        sim.run(100, dt=0.1)
        assert len(sim.state.trace) == pytest.approx(11, abs=1)

    def test_the_trace_is_not_one_sample_per_timestep(self):
        """A three-hour kill at dt=0.1 would be a hundred thousand rows."""
        sim = lined_up_to_kill(make())
        sim.run(600, dt=0.1)
        assert len(sim.state.trace) < 100

    def test_a_sample_carries_what_a_debrief_plots(self):
        sim = lined_up_to_kill(make())
        sim.run(20, dt=0.1)
        sample = sim.state.trace[-1]
        assert sample.bottomhole_psi > 0
        assert sample.casing_psi > 0
        assert sample.influx_bbl > 0


class TestAssessment:
    """A good kill and a bad kill must produce sensibly different verdicts."""

    def test_a_well_flown_kill_passes(self):
        sim = fly_well(lined_up_to_kill(make()))
        assessment = sim.assess(FIRST_CIRCULATION)
        assert assessment.passed, assessment.format_report()

    def test_a_well_flown_kill_takes_no_extra_influx_and_no_losses(self):
        sim = fly_well(lined_up_to_kill(make()))
        assert sim.state.influx_taken_bbl < 1.0
        assert sim.state.lost_bbl == pytest.approx(0.0)

    def test_a_well_flown_kill_keeps_bottomhole_pressure_on_target(self):
        sim = fly_well(lined_up_to_kill(make()))
        assert sim.state.max_bhp_excursion_psi < 75.0

    def test_a_choke_left_alone_fails(self):
        """Fixed choke: pressure runs away and the shoe goes."""
        sim = lined_up_to_kill(make())
        sim.choke.command(18.0)
        sim.pump.command(30.0)
        sim.run(930, dt=0.1)
        assert sim.assess(FIRST_CIRCULATION).grade is Grade.FAIL

    def test_a_choke_left_alone_is_failed_for_the_shoe_not_the_procedure(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(18.0)
        sim.pump.command(30.0)
        sim.run(930, dt=0.1)
        failed = {m.name for m in sim.assess(FIRST_CIRCULATION).critical_failures}
        assert "Peak shoe pressure" in failed

    def test_a_wide_open_choke_fails_for_taking_an_influx(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(100.0)
        sim.pump.command(30.0)
        sim.run(600, dt=0.1)
        assessment = sim.assess(FIRST_CIRCULATION)
        assert assessment.grade is Grade.FAIL
        failed = {m.name for m in assessment.critical_failures}
        assert "Additional influx taken" in failed

    def test_circulating_with_the_bop_open_is_logged_as_critical(self):
        """The lesson students most need, and it must reach the debrief."""
        sim = make()
        sim.flow_path.open(
            "pump_discharge", "standpipe_valve",
            "choke_line", "choke_line_to_manifold", "choke_1",
        )
        sim.choke.command(2.0)
        sim.pump.command(30.0)
        sim.run(120, dt=0.1)
        assert sim.state.event_log.has(EventKind.WELL_OPEN_TO_FLOWLINE)
        assert sim.state.event_log.worst_severity is Severity.CRITICAL

    def test_the_good_kill_logs_far_less_than_the_bad_one(self):
        """A readable log is the point of the edge detection."""
        good = fly_well(lined_up_to_kill(make()))
        bad = lined_up_to_kill(make())
        bad.choke.command(100.0)
        bad.pump.command(30.0)
        bad.run(600, dt=0.1)
        assert len(good.state.event_log.at_least(Severity.CAUTION)) < 5

    def test_not_killing_the_well_fails_when_the_exercise_required_it(self):
        sim = fly_well(lined_up_to_kill(make()))
        assessment = sim.assess(Standard(require_kill=True))
        assert assessment.grade is Grade.FAIL
        assert "not killed" in assessment.summary

    def test_a_first_circulation_is_not_failed_for_leaving_the_well_live(self):
        """Flying the first circulation correctly still leaves a live well."""
        sim = fly_well(lined_up_to_kill(make()))
        assert not sim.state.well_is_dead
        assert sim.assess(FIRST_CIRCULATION).passed

    def test_the_report_names_what_went_wrong(self):
        sim = lined_up_to_kill(make())
        sim.choke.command(100.0)
        sim.pump.command(30.0)
        sim.run(600, dt=0.1)
        report = sim.assess(FIRST_CIRCULATION).format_report()
        assert "FAIL" in report
        assert "influx" in report.lower()

    def test_tightening_the_standard_makes_it_harder(self):
        sim = fly_well(lined_up_to_kill(make()))
        lenient = sim.assess(FIRST_CIRCULATION)
        strict = sim.assess(
            Standard(require_kill=False, bhp_band_psi=50.0,
                     max_bhp_excursion_psi=1.0)
        )
        assert lenient.passed
        assert not strict.passed

    def test_time_off_target_is_integrated_not_sampled(self):
        """A brief excursion between two trace samples must still count."""
        sim = lined_up_to_kill(make(trace_interval_s=600.0))
        sim.choke.command(0.0)
        sim.pump.command(30.0)
        sim.run(60, dt=0.1)
        assert sim.state.seconds_outside_band > 0.0
        assert len(sim.state.trace) == 1

    def test_assessment_does_not_disturb_the_run(self):
        sim = fly_well(lined_up_to_kill(make()))
        before = sim.state.time_s
        sim.assess(FIRST_CIRCULATION)
        sim.assess(FIRST_CIRCULATION)
        assert sim.state.time_s == before


# --------------------------------------------------------------------------
# Across well types
# --------------------------------------------------------------------------

@pytest.mark.parametrize("config", WellConfiguration.standard(),
                         ids=[c.key for c in WellConfiguration.standard()])
class TestEveryWellType:
    def test_simulator_starts(self, config):
        sim = make(config)
        assert sim.state.casing_pressure_psi > 0

    def test_starts_balanced_on_the_formation(self, config):
        sim = make(config)
        sim.flow_path.close("annular")
        sim.run(30)
        assert sim.state.bottomhole_pressure_psi == pytest.approx(
            sim.formation_pressure_psi, rel=0.02
        )

    def test_readout_is_complete(self, config):
        readout = make(config).readout()
        for key in ("Casing (psi)", "Bottomhole (psi)", "Choke (%)",
                    "Influx (bbl)", "Returns"):
            assert key in readout


# --------------------------------------------------------------------------
# Equipment
# --------------------------------------------------------------------------

class TestEquipment:
    def test_pump_ramps_rather_than_jumping(self):
        sim = make()
        sim.pump.command(30.0)
        sim.pump.advance(1.0)
        assert 0.0 < sim.pump.spm < 30.0

    def test_pump_reaches_the_commanded_speed(self):
        sim = make()
        sim.pump.command(30.0)
        for _ in range(200):
            sim.pump.advance(0.1)
        assert sim.pump.spm == pytest.approx(30.0)

    def test_choke_travels_rather_than_snapping(self):
        sim = make()
        sim.choke.command(50.0)
        sim.choke.advance(1.0)
        assert 0.0 < sim.choke.position < 50.0

    def test_shut_choke_passes_nothing(self):
        sim = make()
        sim.choke.position = 0.0
        assert sim.choke.flow_bbl_per_min(2000.0, 9.6) == pytest.approx(0.0)

    def test_choke_flow_rises_with_pressure_drop(self):
        sim = make()
        sim.choke.position = 40.0
        low = sim.choke.flow_bbl_per_min(200.0, 9.6)
        high = sim.choke.flow_bbl_per_min(800.0, 9.6)
        assert high > low

    def test_choke_area_closes_faster_than_travel(self):
        """The last of the handle does most of the work."""
        sim = make()
        sim.choke.position = 50.0
        half_travel = sim.choke.area_in2
        sim.choke.position = 100.0
        assert half_travel < sim.choke.full_open_area_in2 * 0.3

    def test_choke_does_not_flow_backwards(self):
        sim = make()
        sim.choke.position = 50.0
        assert sim.choke.flow_bbl_per_min(-500.0, 9.6) == pytest.approx(0.0)
