"""The stack, and the closing unit that works it.

Two stacks, because a rig has one or the other and they are not the same
equipment: a surface stack is an annular over two pipe rams and a blind shear
ram, with a flow cross between the rams and another below them; a subsea stack
is two annulars, two blind shear rams, three pipe rams and seven outlet
lines. Both carry **two valves in series on every outlet**. What is tested
here is that the *differences* are real ones -- a riser connector that is not
a barrier, a double barrier that needs both valves, a control pod that can
stop answering -- rather than extra labels on a drawing.
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


def shift(sim: Simulator, name: str, state: ValveState) -> bool:
    """Function one element the way a student has to: arm, then operate.

    Every function on either panel is two-handed, so every one of these tests
    arms first. Written as a helper rather than inlined so the interlock has
    exactly one place to be got wrong, and so the tests that are *about* the
    interlock stand out by calling ``operate_stack_valve`` directly.
    """
    sim.accumulator.arm()
    return sim.operate_stack_valve(name, state)


class TestTheStackFollowsTheWell:
    """Which stack is under the rig is a property of the well, not a setting."""

    def test_a_surface_well_gets_a_surface_stack(self):
        names = [v.name for v in make().flow_path.bop_elements]
        assert names == [
            "annular", "upper_rams", "lower_rams", "blind_shear_rams"
        ]

    def test_the_stack_is_listed_top_to_bottom(self):
        """Both faces draw the stack by walking this list, so the order is it.

        The upper flow cross sits directly under the lower pipe rams and the
        lower one directly under the blind shears, and a change to that shape
        is a change to `surface_stack()` rather than to a picture of it.
        """
        shape = [
            v.name if v.is_bop else f"{v.outlet} outlet"
            for v in make().flow_path.valves
            if v.is_bop or v.side
        ]
        # Each outlet contributes four valves; collapse the runs.
        collapsed = [n for i, n in enumerate(shape) if i == 0 or n != shape[i - 1]]
        assert collapsed == [
            "annular", "upper_rams", "lower_rams", "upper outlet",
            "blind_shear_rams", "lower outlet",
        ]

    def test_a_subsea_well_gets_a_subsea_stack(self):
        elements = make(SUBSEA).flow_path.bop_elements
        kinds = [v.bop_kind for v in elements]
        assert kinds.count("annular") == 2
        assert kinds.count("ram") == 5, "three pipe rams and two shear rams"
        assert kinds.count("connector") == 1, "riser connector"
        assert sum(v.shears for v in elements) == 2, "two sets of blind shears"
        assert len(elements) == 8

    def test_the_subsea_stack_is_listed_top_to_bottom(self):
        """Both faces draw it by walking this list, so the order *is* it.

        Outlets interleave between the rams, which is the whole point of a
        stack this size: whichever ram is closed there is a line below it.
        """
        shape = [
            v.name if v.is_bop else f"{v.outlet} {v.side}"
            for v in make(SUBSEA).flow_path.valves
            if v.is_bop or v.side
        ]
        collapsed = [n for i, n in enumerate(shape) if i == 0 or n != shape[i - 1]]
        assert collapsed == [
            "annular", "riser_connector", "gas_relief choke", "lower_annular",
            "upper kill", "blind_shear_rams", "upper choke", "upper_rams",
            "middle kill", "middle_rams", "middle choke", "lower_rams",
            "lower kill", "lower_blind_shear_rams", "lower choke",
        ]

    def test_seven_lines_come_off_the_subsea_stack(self):
        """Four to the choke side, three to the kill side, two valves each.

        The gas relief line is the fourth choke-side line: it taps above the
        lower annular and is **piped into the choke line** rather than run to
        surface on its own, which is why it carries ``side="choke"``.
        """
        path = make(SUBSEA).flow_path
        choke = path.outlet_valves("choke")
        kill = path.outlet_valves("kill")
        assert {v.outlet for v in choke} == {
            "gas_relief", "upper", "middle", "lower"
        }
        assert {v.outlet for v in kill} == {"upper", "middle", "lower"}
        assert len(choke) == 8 and len(kill) == 6, "two fail-safes a line"
        relief = [v for v in choke if v.outlet == "gas_relief"]
        assert [v.to for v in relief][-1] is Node.CHOKE_LINE

    def test_the_gas_relief_line_reaches_the_choke_line(self):
        path = make(SUBSEA).flow_path
        path.open("gas_relief_inner", "gas_relief_outer")
        assert path._reachable(
            path._edges(), Node.ANNULUS, Node.CHOKE_LINE
        ) is not None

    def test_one_gas_relief_valve_alone_does_not(self):
        path = make(SUBSEA).flow_path
        path.open("gas_relief_inner")
        assert path._reachable(
            path._edges(), Node.ANNULUS, Node.CHOKE_LINE
        ) is None

    def test_the_riser_connector_is_not_a_barrier(self):
        """Unlatching it ends the well; it does not secure it.

        A connector drawn as a barrier would be a lie about the one control on
        the stack that walks away from the hole rather than shutting it in.
        """
        sim = make(SUBSEA)
        shift(sim, "riser_connector", ValveState.CLOSED)
        assert not sim.flow_path.bop_is_closed

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_both_stacks_have_an_upper_and_a_lower_outlet(self, config):
        """One outlet is one point of failure, on either stack.

        And the heights are real: closing a ram isolates the outlets above
        it, which is what `TestTheOutletHeightsAreReal` is about.
        """
        path = make(config).flow_path
        for side in ("kill", "choke"):
            outlets = {v.outlet for v in path.outlet_valves(side)}
            assert {"upper", "lower"} <= outlets, side
            # Two valves on every outlet, both sides, both stacks.
            assert len(path.outlet_valves(side)) == 2 * len(outlets)


class TestTheOutletHeightsAreReal:
    """Closing a ram isolates every outlet above it, exactly as the iron would.

    This used to be a drawing: every element joined the annulus to the stack
    and every outlet tapped the annulus, so the graph had one annulus and the
    interleaving was decorative. Now the bore is segments, an outlet taps the
    segment at its own height, and it reaches the well only while every
    element *below* that height is open -- which is what makes "take returns
    from a cross below the ram you closed" a real decision.
    """

    def test_surface_the_upper_cross_dies_with_the_blind_shears(self):
        """The upper flow cross sits above the blind shears; the lower does not.

        Worked on the FlowPath directly rather than through the closing unit,
        because what is under test is the graph and not the interlock.
        """
        path = make().flow_path
        path.close("blind_shear_rams")
        path.open("upper_choke_inner", "upper_choke_outer",
                  "choke_line_to_manifold", "choke_1")
        assert path.is_closed_in, "the upper cross is above the closed rams"
        path.open("lower_choke_inner", "lower_choke_outer")
        assert path.return_route().through_choke, \
            "the lower cross taps below them"

    def test_subsea_a_closed_ram_takes_the_lines_above_it(self):
        path = make(SUBSEA).flow_path
        path.close("upper_rams")
        path.open("upper_choke_inner", "upper_choke_outer",
                  "choke_line_to_manifold", "choke_1")
        assert path.is_closed_in, "the upper choke line taps above the rams"
        path.open("middle_choke_inner", "middle_choke_outer")
        assert path.return_route().through_choke, \
            "the middle choke line taps below them"

    def test_the_lower_blind_shears_take_the_lower_kill_line(self):
        """The lowest sealing element sits beneath the lower kill line's tap.

        Closing it isolates that line; the one line it cannot take is the
        lower choke, which taps the wellhead below the whole stack.
        """
        path = make(SUBSEA).flow_path
        path.close("lower_blind_shear_rams")
        path.open("lower_kill_inner", "lower_kill_outer")
        assert not path.kill_line_is_open
        path.open("lower_choke_inner", "lower_choke_outer",
                  "choke_line_to_manifold", "choke_1")
        assert path.return_route().through_choke

    def test_lubricating_into_an_isolated_segment_deadheads_the_pump(self):
        """Mud pumped in above a closed ram has nowhere to go.

        The upper kill line taps between the blind shears and the lower
        annular; with both closed the segment is sealed at both ends, and a
        pump lined up to it is pumping against a closed volume. Wrong lineups
        are not scored -- they simply do not work.
        """
        path = make(SUBSEA).flow_path
        path.close("blind_shear_rams")
        path.close("lower_annular")
        path.open("pump_to_kill_line", "upper_kill_inner", "upper_kill_outer")
        assert path.pump_is_deadheaded

    def test_the_kill_line_gauge_goes_dead_when_a_ram_below_its_tap_closes(self):
        path = make(SUBSEA).flow_path
        path.open("middle_kill_inner", "middle_kill_outer")
        assert path.kill_line_is_open
        path.close("lower_rams")
        assert not path.kill_line_is_open

    def test_the_standard_shut_in_is_untouched(self):
        """Shut on the annular, returns through the lower choke outlet.

        The annular is the top element and the group lines up the lowest
        outlet, so the ordinary kill never notices any of this -- which is
        the claim that matters most.
        """
        for config in (SURFACE, SUBSEA):
            path = make(config).flow_path
            path.close("annular")
            path.open("choke_line", "choke_line_to_manifold", "choke_1")
            assert path.return_route().through_choke, config


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

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_the_upper_inner_and_lower_outer_are_not_a_route(self, config):
        """Each outlet needs its *own* pair.

        Sharing one spool between the upper and lower outlets would let a valve
        on one outlet and a valve on the other add up to a path that does not
        exist on the stack.
        """
        path = make(config).flow_path
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

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_it_lines_up_the_lower_outlet(self, config):
        """Below every ram but the blind shears, so it survives most closures."""
        path = make(config).flow_path
        path.open("choke_line")
        assert path.valve("lower_choke_outer").is_open
        assert not path.valve("upper_choke_outer").is_open

    def test_operating_a_valve_still_names_it_exactly(self):
        """A group is a setup convenience. The panel operates one valve."""
        path = make().flow_path
        with pytest.raises(KeyError):
            path.set_valve("choke_line", ValveState.OPEN)


class TestTheShearRams:
    """Two sets of blind shears, and both of them are barriers.

    The lower set was a casing shear -- cut, do not seal -- until Kirk
    configured the stack with two blind shears. The `seals=False` machinery
    stays in the model (the riser connector still carries it), but nothing
    on either stack now shears without sealing.
    """

    @pytest.mark.parametrize(
        "name", ["blind_shear_rams", "lower_blind_shear_rams"]
    )
    def test_closing_either_set_shuts_the_well_in(self, name):
        sim = make(SUBSEA)
        shift(sim, name, ValveState.CLOSED)
        assert sim.flow_path.bop_is_closed

    def test_the_lower_set_shears_the_string_too(self):
        sim = make(SUBSEA)
        shift(sim, "lower_blind_shear_rams", ValveState.CLOSED)
        assert sim.flow_path.pipe_sheared

    def test_the_lower_set_is_two_handed_like_everything_else(self):
        """New element, same interlock: an unarmed press does nothing."""
        sim = make(SUBSEA)
        assert not sim.operate_stack_valve(
            "lower_blind_shear_rams", ValveState.CLOSED
        )
        assert sim.flow_path.valve("lower_blind_shear_rams").is_open


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
        shift(sim, "blind_shear_rams", ValveState.CLOSED)
        assert sim.flow_path.pipe_sheared
        assert sim.flow_path.pump_is_deadheaded

    def test_re_opening_the_ram_does_not_mend_the_pipe(self):
        sim = make()
        sim.flow_path.open("pump_discharge", "standpipe_valve")
        shift(sim, "blind_shear_rams", ValveState.CLOSED)
        shift(sim, "blind_shear_rams", ValveState.OPEN)
        assert sim.flow_path.pipe_sheared
        assert sim.flow_path.pump_is_deadheaded

    def test_it_is_logged_once(self):
        sim = make()
        shift(sim, "blind_shear_rams", ValveState.CLOSED)
        shift(sim, "blind_shear_rams", ValveState.OPEN)
        shift(sim, "blind_shear_rams", ValveState.CLOSED)
        sheared = [
            e for e in sim.state.event_log.events
            if e.kind.value == "pipe_sheared"
        ]
        assert len(sheared) == 1

    def test_an_ordinary_ram_does_not_shear(self):
        sim = make()
        shift(sim, "upper_rams", ValveState.CLOSED)
        assert not sim.flow_path.pipe_sheared


class TestTheClosingUnit:
    """Bottle pressure is not the whole unit."""

    def test_an_element_costs_pressure_and_gallons(self):
        sim = make()
        before = sim.accumulator.pressure_psi
        shift(sim, "upper_rams", ValveState.CLOSED)
        assert sim.accumulator.pressure_psi < before
        assert sim.accumulator.flow_gal == sim.accumulator.ram_gal

    def test_an_annular_costs_more_than_a_ram(self):
        annular, ram = make(), make()
        shift(annular, "annular", ValveState.CLOSED)
        shift(ram, "upper_rams", ValveState.CLOSED)
        assert annular.accumulator.flow_gal > ram.accumulator.flow_gal
        assert annular.accumulator.pressure_psi < ram.accumulator.pressure_psi

    def test_a_regulator_wound_off_stops_the_element_moving(self):
        """A healthy accumulator gauge and a stack that will not shift.

        This is the reason the regulators have their own gauges rather than
        being implied by the bottle pressure.
        """
        sim = make()
        sim.accumulator.manifold_regulator_psi = 0.0
        assert not shift(sim, "upper_rams", ValveState.CLOSED)
        assert sim.flow_path.valve("upper_rams").is_open
        # And the annular, on its own regulator, is unaffected.
        assert shift(sim, "annular", ValveState.CLOSED)

    def test_the_master_valve_deadens_the_whole_stack(self):
        sim = make()
        sim.accumulator.master_open = False
        assert not shift(sim, "annular", ValveState.CLOSED)
        assert not shift(sim, "upper_rams", ValveState.CLOSED)

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


class TestTwoHandedOperation:
    """Nothing on a BOP panel is a single press.

    The master valve handle on a surface unit and Push to Operate on a subsea
    one are the same interlock wearing two hats, and both buy exactly one
    function. These are the only tests here that call ``operate_stack_valve``
    without arming first, because the un-armed press is what they are about.
    """

    def test_an_unarmed_press_does_nothing_at_all(self):
        sim = make()
        assert not sim.operate_stack_valve("annular", ValveState.CLOSED)
        assert sim.flow_path.valve("annular").is_open
        # And it costs nothing, because no control fluid went anywhere.
        assert sim.accumulator.flow_gal == 0

    def test_arming_buys_one_function_and_only_one(self):
        sim = make()
        sim.accumulator.arm()
        assert sim.operate_stack_valve("annular", ValveState.CLOSED)
        assert not sim.operate_stack_valve("upper_rams", ValveState.CLOSED)
        assert sim.flow_path.valve("upper_rams").is_open

    def test_the_arm_is_spent_by_a_function_that_did_not_move(self):
        """One arm is one attempt, not one success.

        Otherwise a lamp left lit after a function that did nothing would
        announce the fault, and working out why the stack will not shift is
        the exercise rather than something the panel tells you.
        """
        sim = make()
        sim.accumulator.manifold_regulator_psi = 0.0
        sim.accumulator.arm()
        assert not sim.operate_stack_valve("upper_rams", ValveState.CLOSED)
        assert not sim.accumulator.armed

    def test_pressing_a_button_for_where_the_element_already_is_costs_nothing(self):
        sim = make()
        sim.accumulator.arm()
        assert sim.operate_stack_valve("annular", ValveState.OPEN)
        assert sim.accumulator.armed, "nothing was functioned"

    def test_the_arm_is_reported_and_worked_through_the_session(self):
        session = Session.for_configuration()
        assert session.gauges()["closing_unit"]["armed"] is False
        session.set_closing_unit(arm=True)
        assert session.gauges()["closing_unit"]["armed"] is True
        session.set_valve("upper_rams", "closed")
        assert not session.simulator.flow_path.valve("upper_rams").is_open
        assert session.gauges()["closing_unit"]["armed"] is False

    def test_a_session_will_not_shift_an_element_unarmed(self):
        session = Session.for_configuration()
        session.set_valve("upper_rams", "closed")
        assert session.simulator.flow_path.valve("upper_rams").is_open

    @pytest.mark.parametrize("config", [SURFACE, SUBSEA])
    def test_both_stacks_are_interlocked(self, config):
        """Surface or subsea, it is the same closing unit underneath."""
        sim = make(config)
        assert not sim.operate_stack_valve("annular", ValveState.CLOSED)
        assert shift(sim, "annular", ValveState.CLOSED)


class TestTheHCRsAreOnTheClosingUnitToo:
    """Hydraulically controlled remote. The clue is in the name.

    An HCR is worked from the BOP panel, off the same bottles and through the
    same master valve as the rams -- so lining the choke side up is a closing
    unit function, not a walk to the manifold. It matters the moment the unit
    is in trouble: a student who cannot shut an annular cannot open a choke
    line either, and finding that out is the point.
    """

    @pytest.mark.parametrize(
        "name", ["lower_kill_inner", "lower_choke_outer"]
    )
    def test_an_hcr_will_not_move_unarmed(self, name):
        sim = make()
        assert not sim.operate_stack_valve(name, ValveState.OPEN)
        assert not sim.flow_path.valve(name).is_open
        assert shift(sim, name, ValveState.OPEN)

    def test_a_subsea_fail_safe_is_the_same(self):
        sim = make(SUBSEA)
        assert not sim.operate_stack_valve("lower_choke_inner", ValveState.OPEN)
        assert shift(sim, "lower_choke_inner", ValveState.OPEN)

    def test_it_costs_the_bottles_and_the_counter(self):
        sim = make()
        before = sim.accumulator.pressure_psi
        shift(sim, "lower_choke_inner", ValveState.OPEN)
        assert sim.accumulator.pressure_psi < before
        assert sim.accumulator.flow_gal == sim.accumulator.hcr_gal

    def test_it_costs_less_than_a_ram(self):
        """A gate valve with an actuator on it, not a preventer."""
        hcr, ram = make(), make()
        shift(hcr, "lower_choke_inner", ValveState.OPEN)
        shift(ram, "upper_rams", ValveState.CLOSED)
        assert hcr.accumulator.flow_gal < ram.accumulator.flow_gal

    def test_it_is_on_the_manifold_regulator(self):
        """Wound off, the choke line goes with the pipe rams.

        The annular has its own regulator and is unaffected, which is what
        makes the two gauges worth having.
        """
        sim = make()
        sim.accumulator.manifold_regulator_psi = 0.0
        assert not shift(sim, "lower_choke_inner", ValveState.OPEN)
        assert shift(sim, "annular", ValveState.CLOSED)

    def test_the_master_shut_takes_the_hcrs_with_everything_else(self):
        sim = make()
        sim.accumulator.master_open = False
        assert not shift(sim, "lower_choke_inner", ValveState.OPEN)

    def test_a_manifold_gate_valve_is_still_just_a_valve(self):
        """Somebody walks over and turns it. No arm, no bottles, no cost."""
        session = Session.for_configuration()
        session.set_valve("choke_line_to_manifold", "open")
        assert session.simulator.flow_path.valve("choke_line_to_manifold").is_open
        assert session.gauges()["closing_unit"]["flow_gal"] == 0

    def test_the_engine_refuses_a_valve_that_is_not_on_the_panel(self):
        sim = make()
        with pytest.raises(ValueError, match="not worked from the BOP panel"):
            sim.operate_stack_valve("choke_line_to_manifold", ValveState.OPEN)


class TestAdjustingTheUnitIsTwoHandedAsWell:
    """The regulators and the bypass decide what every function gets.

    Reaching for them is reaching into the closing unit, so the master valve
    is held over for them exactly as it is for a ram -- but they are settings
    rather than functions, and are charged differently.
    """

    def test_a_regulator_will_not_move_unarmed(self):
        session = Session.for_configuration()
        before = session.gauges()["closing_unit"]["manifold_regulator_psi"]
        session.set_closing_unit(manifold_regulator_psi=900)
        assert session.gauges()["closing_unit"]["manifold_regulator_psi"] == before

    def test_armed_it_moves(self):
        session = Session.for_configuration()
        session.set_closing_unit(arm=True)
        session.set_closing_unit(manifold_regulator_psi=900)
        assert session.gauges()["closing_unit"]["manifold_regulator_psi"] == 900

    def test_adjusting_does_not_spend_the_arm(self):
        """Ten presses of Dec. to wind a regulator down is ten, not twenty.

        The master is held over while the adjustment is made and is still over
        for the function afterwards. A control charged an arm a press is a
        control nobody uses, which teaches nothing.
        """
        session = Session.for_configuration()
        session.set_closing_unit(arm=True)
        for psi in (1400, 1300, 1200):
            session.set_closing_unit(manifold_regulator_psi=psi)
        unit = session.gauges()["closing_unit"]
        assert unit["manifold_regulator_psi"] == 1200
        assert unit["armed"] is True
        # And the arm is still good for the function it was taken for.
        session.set_valve("upper_rams", "closed")
        assert not session.simulator.flow_path.valve("upper_rams").is_open

    def test_the_bypass_needs_it_too(self):
        session = Session.for_configuration()
        session.set_closing_unit(bypass_open=True)
        assert session.gauges()["closing_unit"]["bypass_open"] is False
        session.set_closing_unit(arm=True)
        session.set_closing_unit(bypass_open=True)
        assert session.gauges()["closing_unit"]["bypass_open"] is True

    def test_one_command_can_arm_and_adjust(self):
        """Field order in a command must not decide whether it works."""
        session = Session.for_configuration()
        session.set_closing_unit(annular_regulator_psi=800, arm=True)
        assert session.gauges()["closing_unit"]["annular_regulator_psi"] == 800

    def test_the_pod_selector_is_outside_the_interlock(self):
        """It chooses which control system answers, not what pressure it gets.

        Gating it would mean a student on a dead pod has to arm through the
        pod that is not answering to change to the one that is.
        """
        session = Session.for_configuration(bop="subsea")
        session.set_closing_unit(pod="yellow")
        assert session.gauges()["closing_unit"]["pod"] == "yellow"


class TestControlPods:
    """Subsea only, and a real selector rather than a legend."""

    def test_a_surface_stack_has_no_pod(self):
        assert make().accumulator.pod == ""

    def test_a_subsea_stack_starts_on_the_blue_pod(self):
        assert make(SUBSEA).accumulator.pod == "blue"

    def test_a_dead_pod_stops_the_stack_and_the_other_one_answers(self):
        sim = make(SUBSEA)
        sim.inject_fault(Fault(kind=FaultKind.POD_FAILURE))
        assert not shift(sim, "annular", ValveState.CLOSED)
        sim.accumulator.pod = "yellow"
        assert shift(sim, "annular", ValveState.CLOSED)

    def test_a_pod_failure_does_nothing_to_a_surface_stack(self):
        sim = make()
        sim.inject_fault(Fault(kind=FaultKind.POD_FAILURE))
        assert shift(sim, "annular", ValveState.CLOSED)


class TestThroughTheSession:
    """What the panel actually calls."""

    def test_the_lineup_carries_what_the_panel_draws_the_stack_from(self):
        rows = Session.for_configuration(bop="subsea").lineup()
        elements = [r for r in rows if r["is_bop"]]
        assert all(
            r["bop_kind"] in ("annular", "ram", "connector") for r in elements
        )
        assert not any(r["shears"] and not r["seals"] for r in elements), \
            "both shear rams are blind shears — they cut and seal"
        assert any(not r["seals"] and not r["shears"] for r in elements), \
            "riser connector"
        outlets = [r for r in rows if r["side"]]
        assert {r["outlet"] for r in outlets} == {
            "gas_relief", "upper", "middle", "lower"
        }

    def test_the_closing_unit_is_reported(self):
        unit = Session.for_configuration().gauges()["closing_unit"]
        assert unit["manifold_psi"] > 0
        assert unit["flow_gal"] == 0
        assert unit["master_open"] is True
        assert unit["pod"] == ""

    def test_settings_move_independently(self):
        """Sending one must not drag the other back to what the page drew."""
        session = Session.for_configuration()
        session.set_closing_unit(arm=True)
        session.set_closing_unit(manifold_regulator_psi=900)
        session.set_closing_unit(annular_regulator_psi=700)
        unit = session.gauges()["closing_unit"]
        assert unit["manifold_regulator_psi"] == 900
        assert unit["annular_regulator_psi"] == 700

    def test_the_flow_counter_can_be_zeroed(self):
        session = Session.for_configuration()
        session.set_closing_unit(arm=True)
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
