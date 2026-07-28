"""The stack, and the closing unit that works it.

Two stacks, because a rig has one or the other and they are not the same
equipment: a surface stack is one annular, two pipe rams and a blind shear ram
over a flow cross with double HCRs a side; a subsea stack is two annulars, two
shear rams and two variable bore rams with double fail-safe valves on each of
four outlets. What is tested here is that the *differences* are real ones --
a casing shear ram that does not seal, a double barrier that needs both
valves, a control pod that can stop answering -- rather than four extra
labels on a drawing.
"""

import pytest

from wellcontrol import examples
from wellcontrol.wellconfig import BOPLocation, Trajectory, WellConfiguration
from wellcontrol.sim import FlowPath, Node, Session, Simulator, ValveState
from wellcontrol.sim.scenario import Fault, FaultKind

SURFACE = WellConfiguration(BOPLocation.SURFACE, Trajectory.VERTICAL)
SUBSEA = WellConfiguration(BOPLocation.SUBSEA, Trajectory.VERTICAL)


def make(config=SURFACE) -> Simulator:
    return Simulator(well=examples.well_state(config))


class TestTheStackFollowsTheWell:
    """Which stack is under the rig is a property of the well, not a setting."""

    def test_a_surface_well_gets_a_surface_stack(self):
        names = [v.name for v in make().flow_path.bop_elements]
        assert names == [
            "annular", "upper_rams", "blind_shear_rams", "lower_rams"
        ]

    def test_a_subsea_well_gets_a_subsea_stack(self):
        elements = make(SUBSEA).flow_path.bop_elements
        assert [v.bop_kind for v in elements].count("annular") == 2
        assert sum(v.shears for v in elements) == 2, "blind shear and casing shear"
        assert len(elements) == 6

    def test_a_surface_stack_has_one_flow_cross_and_subsea_has_two_outlets(self):
        surface = make().flow_path
        assert {v.outlet for v in surface.outlet_valves("choke")} == {""}
        subsea = make(SUBSEA).flow_path
        assert {v.outlet for v in subsea.outlet_valves("choke")} == {
            "upper", "lower"
        }
        # Two fail-safes on each of two outlets, on each of two sides.
        assert len(subsea.outlet_valves("kill")) == 4


class TestTheDoubleBarrier:
    """Two valves in series, not two valves in parallel.

    The whole point of a double barrier is that opening one of them does
    nothing. Modelled as one valve joining the annulus straight to the line,
    a pair would mean *either* opening gave a route -- the opposite claim.
    """

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_one_valve_alone_does_not_reach_the_line(self, config):
        path = make(config).flow_path
        inner = next(
            v for v in path.outlet_valves("kill") if "inner" in v.name
        )
        path.set_valve(inner.name, ValveState.OPEN)
        assert not path.kill_line_is_open

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_both_together_do(self, config):
        path = make(config).flow_path
        path.open("kill_line")
        assert path.kill_line_is_open

    def test_the_upper_inner_and_lower_outer_are_not_a_route(self):
        """Each outlet needs its *own* pair.

        Sharing one spool between the upper and lower outlets would let a valve
        on one outlet and a valve on the other add up to a path that does not
        exist on the stack.
        """
        path = make(SUBSEA).flow_path
        path.open("upper_kill_inner", "lower_kill_outer")
        assert not path.kill_line_is_open

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_returns_need_the_whole_choke_side_open(self, config):
        path = make(config).flow_path
        path.close("annular")
        path.open("choke_line_to_manifold", "choke_1")
        inner = next(
            v for v in path.outlet_valves("choke") if "inner" in v.name
        )
        path.set_valve(inner.name, ValveState.OPEN)
        assert not path.return_route().open_to_pits, "one valve is not a route"
        path.open("choke_line")
        assert path.return_route().through_choke


class TestNamingASide:
    """Setup code and scenario files name a side; the stack decides the valves.

    How many valves "the kill line" is depends on which rig it is, and neither
    a scenario file nor a test should have to know.
    """

    def test_the_group_opens_every_valve_it_needs(self):
        path = make(SUBSEA).flow_path
        path.open("choke_line")
        assert path._reachable(
            path._edges(), Node.ANNULUS, Node.CHOKE_LINE
        ) is not None

    def test_subsea_lines_up_the_lower_outlet(self):
        """Below every ram on the stack, so it survives whatever is closed."""
        path = make(SUBSEA).flow_path
        path.open("choke_line")
        assert path.valve("lower_choke_outer").is_open
        assert not path.valve("upper_choke_outer").is_open

    def test_operating_a_valve_still_names_it_exactly(self):
        """A group is a setup convenience. The panel operates one valve."""
        path = make().flow_path
        with pytest.raises(KeyError):
            path.set_valve("choke_line", ValveState.OPEN)


class TestCasingShearRams:
    """They cut, and they do not seal. That is the whole point of the flag."""

    def test_closing_them_does_not_shut_the_well_in(self):
        sim = make(SUBSEA)
        sim.operate_bop_element("casing_shear_rams", ValveState.CLOSED)
        assert not sim.flow_path.bop_is_closed

    def test_a_blind_shear_ram_does_shut_it_in(self):
        sim = make(SUBSEA)
        sim.operate_bop_element("blind_shear_rams", ValveState.CLOSED)
        assert sim.flow_path.bop_is_closed


class TestShearingTheString:
    """A shear ram closed on pipe cuts it, and nothing circulates afterwards.

    The well may well be sealed -- a blind shear ram does seal -- but the
    exercise is over as a kill, which is the honest consequence of reaching
    for them and is what makes them a real control rather than a third ram.
    """

    def test_the_pump_loses_its_route(self):
        sim = make()
        sim.flow_path.open("pump_discharge", "standpipe_valve")
        assert not sim.flow_path.pump_is_deadheaded
        sim.operate_bop_element("blind_shear_rams", ValveState.CLOSED)
        assert sim.flow_path.pipe_sheared
        assert sim.flow_path.pump_is_deadheaded

    def test_re_opening_the_ram_does_not_mend_the_pipe(self):
        sim = make()
        sim.flow_path.open("pump_discharge", "standpipe_valve")
        sim.operate_bop_element("blind_shear_rams", ValveState.CLOSED)
        sim.operate_bop_element("blind_shear_rams", ValveState.OPEN)
        assert sim.flow_path.pipe_sheared
        assert sim.flow_path.pump_is_deadheaded

    def test_it_is_logged_once(self):
        sim = make()
        sim.operate_bop_element("blind_shear_rams", ValveState.CLOSED)
        sim.operate_bop_element("blind_shear_rams", ValveState.OPEN)
        sim.operate_bop_element("blind_shear_rams", ValveState.CLOSED)
        sheared = [
            e for e in sim.state.event_log.events
            if e.kind.value == "pipe_sheared"
        ]
        assert len(sheared) == 1

    def test_an_ordinary_ram_does_not_shear(self):
        sim = make()
        sim.operate_bop_element("upper_rams", ValveState.CLOSED)
        assert not sim.flow_path.pipe_sheared


class TestTheClosingUnit:
    """Bottle pressure is not the whole unit."""

    def test_an_element_costs_pressure_and_gallons(self):
        sim = make()
        before = sim.accumulator.pressure_psi
        sim.operate_bop_element("upper_rams", ValveState.CLOSED)
        assert sim.accumulator.pressure_psi < before
        assert sim.accumulator.flow_gal == sim.accumulator.ram_gal

    def test_an_annular_costs_more_than_a_ram(self):
        annular, ram = make(), make()
        annular.operate_bop_element("annular", ValveState.CLOSED)
        ram.operate_bop_element("upper_rams", ValveState.CLOSED)
        assert annular.accumulator.flow_gal > ram.accumulator.flow_gal
        assert annular.accumulator.pressure_psi < ram.accumulator.pressure_psi

    def test_a_regulator_wound_off_stops_the_element_moving(self):
        """A healthy accumulator gauge and a stack that will not shift.

        This is the reason the regulators have their own gauges rather than
        being implied by the bottle pressure.
        """
        sim = make()
        sim.accumulator.manifold_regulator_psi = 0.0
        assert not sim.operate_bop_element("upper_rams", ValveState.CLOSED)
        assert sim.flow_path.valve("upper_rams").is_open
        # And the annular, on its own regulator, is unaffected.
        assert sim.operate_bop_element("annular", ValveState.CLOSED)

    def test_the_master_valve_deadens_the_whole_stack(self):
        sim = make()
        sim.accumulator.master_open = False
        assert not sim.operate_bop_element("annular", ValveState.CLOSED)
        assert not sim.operate_bop_element("upper_rams", ValveState.CLOSED)

    def test_the_bypass_puts_bottle_pressure_on_the_manifold(self):
        sim = make()
        sim.accumulator.manifold_regulator_psi = 800.0
        assert sim.accumulator.manifold_psi == 800.0
        sim.accumulator.bypass_open = True
        assert sim.accumulator.manifold_psi == sim.accumulator.pressure_psi

    def test_a_readback_never_exceeds_what_is_in_the_bottles(self):
        sim = make()
        sim.accumulator.pressure_psi = 600.0
        sim.accumulator.manifold_regulator_psi = 3000.0
        assert sim.accumulator.manifold_psi == 600.0

    def test_the_charging_pumps_stop_without_air(self):
        sim = make()
        sim.accumulator.pressure_psi = 1000.0
        sim.accumulator.air_psi = 0.0
        sim.accumulator.advance(600.0)
        assert sim.accumulator.pressure_psi == 1000.0


class TestControlPods:
    """Subsea only, and a real selector rather than a legend."""

    def test_a_surface_stack_has_no_pod(self):
        assert make().accumulator.pod == ""

    def test_a_subsea_stack_starts_on_the_blue_pod(self):
        assert make(SUBSEA).accumulator.pod == "blue"

    def test_a_dead_pod_stops_the_stack_and_the_other_one_answers(self):
        sim = make(SUBSEA)
        sim.inject_fault(Fault(kind=FaultKind.POD_FAILURE))
        assert not sim.operate_bop_element("annular", ValveState.CLOSED)
        sim.accumulator.pod = "yellow"
        assert sim.operate_bop_element("annular", ValveState.CLOSED)

    def test_a_pod_failure_does_nothing_to_a_surface_stack(self):
        sim = make()
        sim.inject_fault(Fault(kind=FaultKind.POD_FAILURE))
        assert sim.operate_bop_element("annular", ValveState.CLOSED)


class TestThroughTheSession:
    """What the panel actually calls."""

    def test_the_lineup_carries_what_the_panel_draws_the_stack_from(self):
        rows = Session.for_configuration(bop="subsea").lineup()
        elements = [r for r in rows if r["is_bop"]]
        assert all(r["bop_kind"] in ("annular", "ram") for r in elements)
        assert any(r["shears"] and not r["seals"] for r in elements), \
            "casing shear rams"
        outlets = [r for r in rows if r["side"]]
        assert {r["outlet"] for r in outlets} == {"upper", "lower"}

    def test_the_closing_unit_is_reported(self):
        unit = Session.for_configuration().gauges()["closing_unit"]
        assert unit["manifold_psi"] > 0
        assert unit["flow_gal"] == 0
        assert unit["master_open"] is True
        assert unit["pod"] == ""

    def test_settings_move_independently(self):
        """Sending one must not drag the other back to what the page drew."""
        session = Session.for_configuration()
        session.set_closing_unit(manifold_regulator_psi=900)
        session.set_closing_unit(annular_regulator_psi=700)
        unit = session.gauges()["closing_unit"]
        assert unit["manifold_regulator_psi"] == 900
        assert unit["annular_regulator_psi"] == 700

    def test_the_flow_counter_can_be_zeroed(self):
        session = Session.for_configuration()
        session.set_valve("upper_rams", "closed")
        assert session.gauges()["closing_unit"]["flow_gal"] > 0
        session.zero("flow_counter")
        assert session.gauges()["closing_unit"]["flow_gal"] == 0

    def test_a_pod_cannot_be_selected_on_a_surface_stack(self):
        with pytest.raises(KeyError):
            Session.for_configuration().set_closing_unit(pod="yellow")

    def test_the_kill_line_gauge_needs_the_whole_side_open(self):
        session = Session.for_configuration()
        assert session.gauges()["kill_line_psi"] == 0
        session.simulator.flow_path.open("kill_line")
        assert session.gauges()["kill_line_psi"] > 0
