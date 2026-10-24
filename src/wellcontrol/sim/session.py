"""A student's session: the engine, a clock, and the controls they can reach.

Stage 3 of the simulator. The engine advances by simulated seconds and knows
nothing about wall time; something has to decide *when* to step it. That is this
module, and it is deliberately kept free of any web framework so the time model
can be tested without the HTTP stack.

## Time advances on demand, not on a background thread

Each call to :meth:`Session.advance` looks at how much wall time has passed
since the last one, multiplies by the speed setting, and steps the engine that
far. No threads, no timers, nothing running when nobody is watching.

Two consequences worth stating:

* **The well only evolves while somebody is looking at it.** For a self-study
  trainer that is right -- a student who walks away has not lost the kick -- and
  it makes every test deterministic.
* **Catch-up is clamped.** Without the clamp, a student who leaves the tab for
  ten minutes at 60x comes back to ten hours of simulated well, all integrated
  in one request. :attr:`MAX_CATCHUP_S` caps how much simulated time a single
  advance can cover.

## Why the clock is injected

``clock`` defaults to :func:`time.monotonic` but tests pass their own, so the
whole time model is exercised without sleeping. Monotonic rather than wall
clock: the simulation must not jump when the machine's clock is adjusted.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .. import examples
from ..wellconfig import BOPLocation, Trajectory, WellConfiguration
from .assessment import Assessment, EventKind, Severity, Standard, clock
from .engine import Simulator, StartState
from .flowpath import ValveState

#: Simulated seconds a single advance may cover, however long the gap was.
MAX_CATCHUP_S: float = 30.0

#: Integration step. Small enough for the stiff pressure equation; see engine.
STEP_S: float = 0.05

#: Speeds the student can select. 0 is pause.
SPEEDS: tuple[float, ...] = (0.0, 1.0, 10.0, 60.0)

#: Shown in the API docs. A real kill runs three to five hours, so working one
#: entirely at 1x is not a training exercise anybody completes.
SESSION_SPEEDS_DOC = (
    "Simulated seconds per real second. 0 pauses. A real kill runs three to "
    "five hours, so 10x and 60x exist to skip the long circulating stretches; "
    "drop back to 1x when the gas nears surface and the choke gets violent."
)


@dataclass
class Session:
    """One student, one well, one attempt.

    Args:
        simulator: The engine. Defaults to the reference surface vertical well.
        speed: Simulated seconds per wall-clock second. 0 pauses.
        clock: Source of wall-clock seconds. Injected for testing.
    """

    simulator: Simulator
    speed: float = 0.0
    clock: Callable[[], float] = time.monotonic
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    student_name: str = "Student"
    instructor_name: str = "Lead Assessor"
    well_name: str = ""
    complication: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    is_frozen: bool = False
    _unfrozen_speed: float = 1.0
    #: The exercise this session is running, if it came from one.
    scenario: object | None = None
    #: Drop to real time once, when the well first flows. See _should_slow_down.
    auto_slow: bool = True
    force_reveal_downhole: bool = False
    #: Wall-clock reading at the last advance.
    _last_tick: float | None = field(default=None, repr=False)
    _auto_slowed: bool = field(default=False, repr=False)

    def get_well_name(self) -> str:
        """The identifier or name of the well."""
        if self.well_name:
            return self.well_name
        if self.scenario and hasattr(self.scenario, "name") and self.scenario.name:
            return self.scenario.name
        return "Reference Well #1"

    def get_complication(self) -> str:
        """Complication or scenario fault affecting this kill operation."""
        if self.complication:
            return self.complication
        failures = [
            e.message
            for e in self.simulator.state.event_log.events
            if e.kind
            in (
                EventKind.EQUIPMENT_FAILURE,
                EventKind.GAS_BREAKOUT,
                EventKind.PUMP_DEADHEADED,
                EventKind.SHOE_FRACTURED,
            )
        ]
        if failures:
            return "; ".join(failures[:2])
        if self.scenario and getattr(self.scenario, "has_faults", False):
            return "Equipment faults active (watch gauges)"
        return "None (Standard Procedure)"

    # -- construction --------------------------------------------------------

    @classmethod
    def for_configuration(
        cls,
        bop: str = "surface",
        trajectory: str = "vertical",
        start: str = "shut_in",
        **kwargs,
    ) -> "Session":
        """Build a session on one of the library's example wells.

        Args:
            bop: ``surface`` or ``subsea``.
            trajectory: ``vertical``, ``deviated`` or ``horizontal``.
            start: ``shut_in`` hands over a kick that is already in the hole,
                sized and identified, with the well closed on it and the choke
                line lined up -- an exercise in working a choke. ``drilling``
                starts on a well that is behaving, lined up for normal drilling
                with returns up the flowline; the kick arrives later and the
                student has to notice it and shut in themselves.
        """
        config = WellConfiguration(BOPLocation(bop), Trajectory(trajectory))
        state = StartState(start)
        session = cls(
            simulator=Simulator(well=examples.well_state(config), start=state),
            **kwargs,
        )
        path = session.simulator.flow_path
        if state is StartState.SHUT_IN:
            path.close("annular")
            path.open("choke_line_valve", "choke_line_to_manifold", "choke_1")
            path.open("pump_discharge", "standpipe_valve")
        else:
            # Lined up to drill: pump through the string, returns up the
            # flowline over the shakers. Nothing on the choke side is open,
            # which is the point -- shutting in is the student's job.
            path.open("pump_discharge", "standpipe_valve")
        return session

    @classmethod
    def for_scenario(cls, key: str, **kwargs) -> "Session":
        """Build a session from a named exercise.

        The scenario decides the well, where it starts, the lineup and what is
        going to go wrong; everything else is the same session as any other.
        """
        from .scenario import get_scenario

        scenario = get_scenario(key)
        config = WellConfiguration(
            BOPLocation(scenario.bop), Trajectory(scenario.trajectory)
        )
        well = examples.well_state(
            config, bit_off_bottom_ft=scenario.bit_off_bottom_ft
        )
        if getattr(scenario, "mud_weight_ppg", None) is not None:
            well.mud_weight_ppg = float(scenario.mud_weight_ppg)
        if getattr(scenario, "sidpp_psi", None) is not None:
            well.sidpp_psi = float(scenario.sidpp_psi)
        if getattr(scenario, "sicp_psi", None) is not None:
            well.sicp_psi = float(scenario.sicp_psi)
        if getattr(scenario, "pit_gain_bbl", None) is not None:
            well.pit_gain_bbl = float(scenario.pit_gain_bbl)
        if getattr(scenario, "frac_gradient_ppg", None) is not None:
            well.frac_gradient_ppg = float(scenario.frac_gradient_ppg)
        if getattr(scenario, "shoe_md_ft", None) is not None:
            well.shoe_md_ft = float(scenario.shoe_md_ft)

        kick_after = (
            float(scenario.kick_after_s)
            if getattr(scenario, "kick_after_s", None) is not None
            else 120.0
        )
        simulator = Simulator(
            well=well,
            start=scenario.start,
            faults=scenario.faults,
            migration_rate_ft_per_hr=scenario.migration_rate_ft_per_hr,
            kick_after_s=kick_after,
            is_obm=bool(getattr(scenario, "is_obm", False)),
            bubble_point_psi=float(getattr(scenario, "bubble_point_psi", 1500.0) or 1500.0),
        )
        session = cls(simulator=simulator, **kwargs)
        session.scenario = scenario

        if key in ("wait_and_weight", "bullhead"):
            # In Wait and Weight, the well is shut in while kill mud is weighted
            # up in the pits; when circulation begins, kill mud is ready.
            # In Bullheading, kill mud is mixed to bullhead into the hole.
            simulator.set_kill_mud_weight(well.kill_mud_weight_ppg)

        path = simulator.flow_path
        if scenario.start is StartState.SHUT_IN:
            path.close("annular")
            path.open("choke_line_valve", "choke_line_to_manifold", "choke_1")
            path.open("pump_discharge", "standpipe_valve")
        else:
            path.open("pump_discharge", "standpipe_valve")
        # The scenario's own lineup goes on last, so it can override the
        # default for the exercise it describes.
        if scenario.open_valves:
            path.open(*scenario.open_valves)
        if scenario.closed_valves:
            path.close(*scenario.closed_valves)
        return session

    # -- what the student is allowed to see ----------------------------------

    @property
    def downhole_visible(self) -> bool:
        """Whether the downhole truth is on display.

        **Hidden while a drilling kick is developing.** A rig has no bottomhole
        gauge and no pore pressure readout; showing them would turn kick
        detection -- which is the skill being taught -- into reading a number.
        Until the well is shut in the student gets what a driller gets: the pit
        level, the flow line, and the standpipe.

        Once they have shut in, it comes back. At that point it is a teaching
        aid rather than a giveaway: the kick has been taken, and seeing what
        bottomhole pressure does while they work the choke is the whole reason
        this is a simulator and not a rig.
        """
        if self.simulator.start is StartState.SHUT_IN:
            return True
        return self.simulator.state.event_log.has(
            EventKind.WELL_SHUT_IN_BY_STUDENT
        )

    # -- time ----------------------------------------------------------------

    def set_speed(self, speed: float) -> None:
        """Change the speed multiplier, banking the time already run.

        Advancing first matters: without it, time that elapsed at the old speed
        would be integrated at the new one.
        """
        self.advance()
        self.speed = max(0.0, float(speed))
        if self.speed > 0.0:
            self.is_frozen = False

    def freeze(self, freeze: bool = True) -> None:
        """Instructor freeze: pause the student's simulator to explain or review."""
        self.advance()
        if freeze:
            if self.speed > 0.0:
                self._unfrozen_speed = self.speed
            self.speed = 0.0
            self.is_frozen = True
            self.simulator.state.record(
                EventKind.INSTRUCTOR_ACTION,
                "Simulator frozen by instructor (Whiteboard Review Mode).",
                severity=Severity.INFO,
            )
        else:
            self.speed = getattr(self, "_unfrozen_speed", 1.0) or 1.0
            self.is_frozen = False
            self.simulator.state.record(
                EventKind.INSTRUCTOR_ACTION,
                f"Simulator resumed by instructor ({self.speed:.0f}x).",
                severity=Severity.INFO,
            )

    def inject_fault(self, kind: str, message: str = "") -> None:
        """Instructor injects an equipment failure on the fly."""
        from .scenario import Fault, FaultKind

        self.advance()
        fault_kind = FaultKind(kind)
        default_messages = {
            FaultKind.PUMP_FAILURE: "Mud pump stopped — fluid end failure.",
            FaultKind.CHOKE_PLUGGED: "Choke 1 is not responding — pressure climbing.",
            FaultKind.CHOKE_WASHOUT: "Choke washed out — unable to maintain backpressure.",
            FaultKind.ACCUMULATOR_LEAK: "BOP accumulator pressure drop — hydraulic fluid loss.",
            FaultKind.WEAK_SHOE: "Formation breakdown detected — shoe integrity degraded.",
            FaultKind.MUD_WEIGHT_DROP: "Active mud pits diluted — active mud weight dropped by 2.0 ppg.",
        }
        msg = message or default_messages.get(fault_kind, f"Equipment failure: {fault_kind.label}")
        fault = Fault(
            kind=fault_kind,
            at_seconds=self.simulator.state.time_s,
            message=msg,
            severity=Severity.CRITICAL if fault_kind != FaultKind.WEAK_SHOE else Severity.WARNING,
        )
        self.simulator._break_equipment(fault)
        self.simulator.state.record(
            EventKind.EQUIPMENT_FAILURE,
            msg,
            severity=fault.severity,
            fault=fault.kind.value,
        )

    def summary(self) -> dict:
        """Overview of the student's session for the multi-student teacher dashboard."""
        self.advance()
        state = self.simulator.state
        sim = self.simulator

        # Determine operational status
        if state.well_is_dead:
            status = "Dead"
        elif self.is_frozen:
            status = "Frozen"
        elif self.speed == 0.0:
            status = "Paused"
        elif not sim.flow_path.bop_is_closed:
            status = "Drilling" if sim.pump.running else "Flow Check"
        else:
            if sim.flow_path.is_reverse_circulating:
                status = "Reverse Circulating"
            elif sim.flow_path.injecting_into_the_annulus:
                status = "Lubricating"
            elif sim.flow_path.is_closed_in:
                status = "Bullheading" if sim.pump.running else "Shut-In"
            elif sim.pump.running:
                status = "Circulating"
            else:
                status = "Shut-In"

        # Determine alarms
        alarms: list[dict] = []
        if not sim.flow_path.bop_is_closed and (state.flow_out_bpm > state.flow_in_bpm + 0.3):
            alarms.append({"severity": "critical", "text": f"KICK FLOWING: Flow out +{(state.flow_out_bpm - state.flow_in_bpm):.1f} bpm"})
        elif state.influx_rate_bpm > 0:
            alarms.append({"severity": "critical", "text": f"Active Influx: +{state.influx_rate_bpm:.1f} bpm into well"})

        bhp_err = abs(state.bottomhole_pressure_psi - sim.formation_pressure_psi)
        if sim.kick_taken and bhp_err > sim.bhp_band_psi:
            direction = "Underbalanced" if state.bottomhole_pressure_psi < sim.formation_pressure_psi else "Overbalance High"
            alarms.append({"severity": "warning" if bhp_err < 150 else "critical", "text": f"BHP Excursion ({direction} by {bhp_err:.0f} psi)"})

        if sim.frac_pressure_psi is not None:
            margin = sim.frac_pressure_psi - state.shoe_pressure_psi
            if margin <= 0:
                alarms.append({"severity": "critical", "text": f"SHOE FRACTURED: Exceeded limit by {abs(margin):.0f} psi!"})
            elif margin < 150:
                alarms.append({"severity": "warning", "text": f"Low Shoe Margin ({margin:.0f} psi remaining)"})

        if sim.flow_path.pump_is_deadheaded and sim.pump.running:
            alarms.append({"severity": "critical", "text": "PUMP DEADHEADED: Discharge line blocked!"})

        failures = state.event_log.of_kind(EventKind.EQUIPMENT_FAILURE)
        if failures:
            alarms.append({"severity": "critical", "text": f"Fault: {failures[-1].message}"})

        shoe_margin = round(sim.frac_pressure_psi - state.shoe_pressure_psi) if sim.frac_pressure_psi is not None else None

        return {
            "session_id": self.session_id,
            "student_name": self.student_name,
            "scenario_key": self.scenario.key if self.scenario else "custom",
            "scenario_name": self.scenario.name if self.scenario else "Custom Configuration",
            "status": status,
            "speed": self.speed,
            "is_frozen": self.is_frozen,
            "well_is_dead": state.well_is_dead,
            "bop_closed": sim.flow_path.bop_is_closed,
            "bottomhole_psi": round(state.bottomhole_pressure_psi),
            "formation_psi": round(sim.formation_pressure_psi),
            "overbalance_psi": round(state.bottomhole_pressure_psi - sim.formation_pressure_psi),
            "shoe_psi": round(state.shoe_pressure_psi),
            "frac_psi": round(sim.frac_pressure_psi) if sim.frac_pressure_psi is not None else None,
            "shoe_margin_psi": shoe_margin,
            "standpipe_psi": round(state.standpipe_pressure_psi),
            "casing_psi": round(state.casing_pressure_psi),
            "pump_spm": round(sim.pump.spm, 1),
            "choke_percent": round(sim.choke.position, 1),
            "strokes": round(state.strokes),
            "influx_volume_bbl": round(state.influx_volume_bbl, 1),
            "pit_gain_bbl": round(sim.pits.gain_bbl, 1),
            "injected_bbl": round(state.injected_bbl, 1),
            "lubricated_bbl": round(state.lubricated_bbl, 1),
            "active_mud_weight_ppg": round(sim.kill_mud_weight_ppg or sim.well.mud_weight_ppg, 2),
            "alarms": alarms,
            "clock": clock(state.time_s),
        }

    @property
    def paused(self) -> bool:
        return self.speed <= 0.0

    def advance(self) -> float:
        """Step the engine up to the present. Returns simulated seconds run."""
        now = self.clock()
        if self._last_tick is None:
            self._last_tick = now
            return 0.0

        elapsed_wall = max(0.0, now - self._last_tick)
        self._last_tick = now
        if self.paused:
            return 0.0

        simulated = min(elapsed_wall * self.speed, MAX_CATCHUP_S)
        steps = int(simulated / STEP_S)
        for _ in range(steps):
            self.simulator.step(STEP_S)
            if self._should_slow_down():
                # Stop mid-batch. Running the rest of this batch at the old
                # speed would spend the seconds the slow-down exists to give
                # back.
                break
        return steps * STEP_S

    def _should_slow_down(self) -> bool:
        """Drop to real time the moment the well starts flowing.

        **An undetected kick accelerates.** Gas replacing mud lightens the
        annulus, which deepens the underbalance, which brings in more gas. At
        60x the whole thing is over between two glances at the screen, and the
        student learns nothing except that they lost.

        So the clock drops to 1x by itself at the moment the flow line tells
        them something is wrong. That is not making the exercise easier -- the
        well does exactly what it did before -- it is giving them the seconds a
        driller standing at the panel would actually have.
        """
        if not self.auto_slow or self.speed <= 1.0 or self._auto_slowed:
            return False
        if not self.simulator.state.event_log.is_active(EventKind.FLOW_CHECK):
            return False
        self._auto_slowed = True
        self.speed = 1.0
        self.simulator.state.record(
            EventKind.FLOW_CHECK,
            "Real time from here — the well is flowing.",
            severity=Severity.INFO,
        )
        return True

    # -- controls ------------------------------------------------------------

    def set_choke(self, percent: float) -> None:
        self.advance()
        self.simulator.choke.command(float(percent))

    def set_pump(self, spm: float) -> None:
        self.advance()
        if spm <= 0:
            self.simulator.pump.stop()
        else:
            self.simulator.pump.command(float(spm))

    def set_valve(self, name: str, state: str) -> None:
        """Open or close a valve, or operate a BOP element.

        BOP elements cost accumulator pressure to shift, and below the minimum
        there is not enough to shift one at all. The element simply does not
        move -- consistent with the rest of the lineup model, where a thing that
        cannot work does not work rather than raising an error at the student.

        Raises:
            KeyError: if there is no such valve.
        """
        self.advance()
        path = self.simulator.flow_path
        valve = path.valve(name)
        target = ValveState(state)
        if valve.is_bop and valve.state is not target:
            if not self.simulator.accumulator.operate(is_annular="annular" in name):
                return
        path.set_valve(name, target)

    def strip(self, feet_per_min: float) -> None:
        """Run pipe in or out through the stripping element."""
        self.advance()
        self.simulator.strip(feet_per_min)

    def set_mud_weight(self, ppg: float) -> None:
        """Update mud weight in the active pits."""
        self.advance()
        self.simulator.set_kill_mud_weight(ppg)

    def zero_trip_tank(self) -> None:
        """Zero the trip tank baseline."""
        self.simulator.zero_trip_tank()

    def refill_trip_tank(self, volume_bbl: float = 50.0) -> None:
        """Refill trip tank to target volume."""
        self.simulator.refill_trip_tank(volume_bbl)

    def zero_pits(self) -> None:
        """Zero the pit gain baseline."""
        self.simulator.zero_pits()

    # -- readouts ------------------------------------------------------------

    def gauges(self) -> dict:
        """What the choke panel displays.

        The instrument scan a student has to build is drill pipe pressure,
        casing pressure, stroke counter and choke position together, so those
        four come first and everything else is secondary.
        """
        state = self.simulator.state
        route = self.simulator.flow_path.return_route()
        visible = self.downhole_visible or self.force_reveal_downhole

        def downhole(value):
            """None while the student is meant to be detecting, not reading."""
            return None if not visible else value

        return {
            # The rig's own instruments. Always readable, because they are the
            # ones a driller actually has.
            "drill_pipe_psi": round(state.standpipe_pressure_psi),
            "casing_psi": round(state.casing_pressure_psi),
            "strokes": round(state.strokes),
            "choke_percent": round(self.simulator.choke.position, 1),
            "choke_commanded": round(self.simulator.choke.commanded_position, 1),
            "pump_spm": round(self.simulator.pump.spm, 1),
            "pump_commanded": round(self.simulator.pump.commanded_spm, 1),
            "pit_gain_bbl": round(self.simulator.pits.gain_bbl, 1),
            # The instrument the Volumetric method is worked to.
            "bled_bbl": round(state.bled_bbl, 2),
            "flow_in_bpm": round(state.flow_in_bpm, 2),
            "flow_out_bpm": round(state.flow_out_bpm, 2),
            "time_s": round(state.time_s, 1),
            "returns": route.description,
            "well_is_dead": state.well_is_dead,
            "speed": self.speed,

            # Driller's console and BOP panel.
            "hookload_klbf": round(self.simulator.hookload_lbf / 1000.0, 1),
            "trip_tank_bbl": round(self.simulator.trip_tank.change_bbl, 1),
            "accumulator_psi": round(self.simulator.accumulator.pressure_psi),
            "accumulator_fraction": round(self.simulator.accumulator.fraction, 3),
            "accumulator_ok": self.simulator.accumulator.can_operate,
            "bop_closed": self.simulator.flow_path.bop_is_closed,
            # Which BOP panel to draw. A subsea stack is monitored from a
            # screen; a surface stack is operated from a red remote panel.
            "is_subsea": self.simulator.well.configuration.is_subsea,
            "choke_line_friction_psi": round(
                self.simulator.well.effective_choke_line_friction
            ),
            "strip_rate_ft_per_min": self.simulator.strip_rate_ft_per_min,
            "bit_md_ft": round(state.bit_md_ft),
            "pipe_moved_ft": round(state.pipe_moved_ft, 1),
            "pipe_displacement_bbl": round(state.pipe_displacement_bbl, 2),
            "lubricated_bbl": round(state.lubricated_bbl, 2),
            "injected_bbl": round(state.injected_bbl, 2),
            "scenario": self.scenario.key if self.scenario else None,
            "reverse_circulating": self.simulator.flow_path.is_reverse_circulating,
            "active_mud_weight_ppg": round(self.simulator.kill_mud_weight_ppg, 2),
            "original_mud_weight_ppg": round(self.simulator.well.mud_weight_ppg, 2),
            "target_kill_mud_weight_ppg": round(self.simulator.well.kill_mud_weight_ppg, 2),
            "trip_tank_volume_bbl": round(self.simulator.trip_tank.volume_bbl, 1),
            "is_obm": self.simulator.is_obm,
            "obm_breakout": state.obm_breakout,
            "gas_dissolved_fraction": round(state.gas_dissolved_fraction, 2),
            "bubble_point_psi": round(self.simulator.bubble_point_psi),

            # Downhole truth. No rig has these; see downhole_visible.
            "downhole_visible": visible,
            "bottomhole_psi": downhole(round(state.bottomhole_pressure_psi)),
            "formation_psi": downhole(
                round(self.simulator.formation_pressure_psi)
            ),
            "overbalance_psi": downhole(round(
                state.bottomhole_pressure_psi
                - self.simulator.formation_pressure_psi
            )),
            "shoe_psi": downhole(round(state.shoe_pressure_psi)),
            "frac_psi": downhole(
                round(self.simulator.frac_pressure_psi)
                if self.simulator.frac_pressure_psi is not None else None
            ),
            "maasp_psi": downhole(
                round(self.simulator.maasp_psi)
                if self.simulator.maasp_psi is not None else None
            ),
            "influx_bbl": downhole(round(state.influx_volume_bbl, 1)),
            "influx_top_md": downhole(round(state.influx_top_md)),
            "gas_at_surface": downhole(state.gas_at_surface),
        }

    def lineup(self) -> list[dict]:
        """Every valve, its label and its state, for the lineup panel."""
        return [
            {
                "name": valve.name,
                "label": valve.label,
                "state": valve.state.value,
                "is_bop": valve.is_bop,
                "throttling": valve.throttling,
                "from": valve.frm.value,
                "to": valve.to.value,
            }
            for valve in self.simulator.flow_path.valves
        ]

    def schematic(self, force_visible: bool = False) -> dict:
        """Everything the well cross-section draws.

        Depths and the fluids currently occupying them. Withheld along with the
        rest of the downhole picture while a kick is still being detected --
        a cross-section showing exactly where the influx is would answer the
        question the student is meant to be working out.
        """
        sim = self.simulator
        state = sim.state
        is_visible = self.downhole_visible or self.force_reveal_downhole or force_visible
        picture = {
            "hole_md_ft": round(sim.hole_md),
            "bit_md_ft": round(state.bit_md_ft),
            "shoe_md_ft": None if sim.shoe_md is None else round(sim.shoe_md),
            "casing_id_in": sim.well.annulus.wellbore.sections[0].id_in,
            "hole_id_in": sim.well.annulus.wellbore.sections[-1].id_in,
            "visible": is_visible,
            "force_reveal_downhole": self.force_reveal_downhole,
        }
        if not is_visible:
            return picture

        zones = [
            {
                "top_md_ft": round(zone.top_md),
                "bottom_md_ft": round(zone.bottom_md),
                "density_ppg": round(zone.density_ppg, 2),
                "label": zone.label,
            }
            for zone in sim._annulus_column().zones
        ]
        picture.update({
            "annulus_zones": zones,
            "influx_in_string": state.influx_in_string,
            "influx_top_md_ft": round(
                state.influx_string_top_md if state.influx_in_string
                else state.influx_top_md
            ),
            "influx_bottom_md_ft": round(
                state.influx_string_bottom_md if state.influx_in_string
                else state.influx_bottom_md
            ),
            "influx_bbl": round(state.influx_volume_bbl, 1),
        })
        return picture

    def toggle_downhole_reveal(self, reveal: bool | None = None) -> bool:
        """Toggle or set instructor downhole override."""
        if reveal is None:
            self.force_reveal_downhole = not self.force_reveal_downhole
        else:
            self.force_reveal_downhole = bool(reveal)
        return self.force_reveal_downhole

    def kill_sheet(self) -> dict:
        """The kill sheet, built from what the student read off the gauges.

        **Not from the numbers the scenario was designed around.** On a drilling
        exercise those are two different things: a slow shut-in takes more
        influx and reads higher, and a kill sheet computed from the designer's
        figures is the answer to a question the student was not asked. Worse,
        it is the wrong answer for the kick they actually took.

        Until the pressures have settled there is nothing to build from, so the
        sheet is withheld rather than guessed at -- which is also the honest
        answer on a rig, where you do not have a kill sheet until you have read
        the gauges.

        A shut-in start hands the readings over as part of the exercise, so
        there the measured figures and the well's own are the same numbers.
        """
        from .. import rounding
        from ..units import PSI_PER_PPG_FT

        sim = self.simulator
        state = sim.state
        sheet = sim.well.kill_sheet

        if state.measured_sidpp_psi is not None:
            sidpp = state.measured_sidpp_psi
            sicp = state.measured_sicp_psi
            pit_gain = state.measured_pit_gain_bbl
            readings_are_the_students = True
        elif sim.start is StartState.SHUT_IN:
            sidpp = sim.well.sidpp_psi
            sicp = sim.well.sicp_psi
            pit_gain = sim.well.pit_gain_bbl
            readings_are_the_students = False
        else:
            # Drilling, and nothing has been shut in yet. There is no kill
            # sheet to show and inventing one would give the exercise away.
            return {
                "available": False,
                "reason": (
                    "No kill sheet yet. Shut the well in, let the pressures "
                    "stabilise, and it will be built from what you read."
                ),
                "actual_strokes": round(state.strokes),
                "actual_drill_pipe_psi": round(state.standpipe_pressure_psi),
                "actual_casing_psi": round(state.casing_pressure_psi),
            }

        mud = sim.well.mud_weight_ppg
        scr = sim.well.slow_circulating_rate.pressure_psi
        exact_kmw = mud + sidpp / (PSI_PER_PPG_FT * sim.well.bit_tvd_ft)
        kmw = rounding.kill_mud_weight(exact_kmw)
        icp = rounding.pressure(sidpp + scr)
        fcp = rounding.pressure(scr * kmw / mud)
        stk_bit = round(sheet.strokes_to_bit)

        schedule = []
        if stk_bit > 0:
            num_steps = 10
            step_stk = max(1, stk_bit // num_steps)
            for s in range(0, stk_bit + 1, step_stk):
                frac = s / stk_bit
                target_p = rounding.pressure(icp - frac * (icp - fcp))
                schedule.append({"strokes": s, "target_dp_psi": target_p})
            if schedule[-1]["strokes"] < stk_bit:
                schedule.append({"strokes": stk_bit, "target_dp_psi": fcp})
        else:
            schedule.append({"strokes": 0, "target_dp_psi": icp})

        current_strokes = round(state.strokes)
        if current_strokes <= 0:
            target_dp_now = icp
        elif stk_bit > 0 and current_strokes < stk_bit:
            frac_now = current_strokes / stk_bit
            target_dp_now = rounding.pressure(icp - frac_now * (icp - fcp))
        else:
            target_dp_now = fcp

        return {
            "available": True,
            "readings_are_the_students": readings_are_the_students,
            "mud_weight_ppg": round(mud, 1),
            # Rounded UP to the tenth: the rig mixes what it can mix.
            "kill_mud_weight_ppg": kmw,
            "kill_mud_weight_exact_ppg": round(exact_kmw, 3),
            "sidpp_psi": round(sidpp),
            "sicp_psi": round(sicp),
            "pit_gain_bbl": round(pit_gain, 1),
            "icp_psi": icp,
            # Built from the weight actually mixed, not the calculated one.
            "fcp_psi": fcp,
            "formation_psi": round(sim.formation_pressure_psi),
            "maasp_psi": (
                None if sim.maasp_psi is None else round(sim.maasp_psi)
            ),
            "frac_psi": (
                None if sim.frac_pressure_psi is None
                else round(sim.frac_pressure_psi)
            ),
            "strokes_to_bit": stk_bit,
            "strokes_bit_to_surface": round(sheet.strokes_bit_to_surface),
            "total_strokes": round(sheet.total_strokes),
            "scr_psi": round(sim.well.slow_circulating_rate.pressure_psi),
            "scr_spm": round(sim.well.slow_circulating_rate.spm),
            "schedule": schedule,
            "target_dp_psi": target_dp_now,
            # Live, for comparison against the plan.
            "actual_strokes": current_strokes,
            "actual_drill_pipe_psi": round(state.standpipe_pressure_psi),
            "actual_casing_psi": round(state.casing_pressure_psi),
        }

    def events_since(self, index: int = 0) -> dict:
        """Event log entries from ``index`` on, so the client can append.

        Returning the next index rather than a timestamp keeps the client
        simple and makes replays idempotent.
        """
        events = self.simulator.state.event_log.events
        start = max(0, min(index, len(events)))
        return {
            "next_index": len(events),
            "events": [
                {
                    "time_s": round(event.time_s, 1),
                    "clock": event.clock,
                    "kind": event.kind.value,
                    "severity": event.severity.value,
                    "message": event.message,
                }
                for event in events[start:]
            ],
        }

    def trace(self, limit: int = 240) -> list[dict]:
        """Recent trace samples for the panel's strip chart.

        Carries the surface pressures always and the downhole ones only when
        they are on display, so a chart cannot leak what the gauges withhold.
        """
        visible = self.downhole_visible
        samples = (
            self.simulator.state.trace[-limit:]
            if limit > 0
            else self.simulator.state.trace
        )
        rows = []
        for sample in samples:
            flow_in = round(sample.flow_in_bpm, 2)
            flow_out = round(sample.flow_out_bpm, 2)
            row = {
                "time_s": round(sample.time_s, 1),
                "casing_psi": round(sample.casing_psi),
                "drill_pipe_psi": round(sample.standpipe_psi),
                "pit_gain_bbl": round(sample.pit_gain_bbl, 1),
                "flow_out_bpm": flow_out,
                "flow_in_bpm": flow_in,
                "diff_flow_bpm": round(flow_out - flow_in, 2),
                "pump_spm": round(sample.pump_spm, 1),
                "choke_percent": round(sample.choke_percent, 1),
                "hookload_klbf": round(sample.hookload_klbf, 1),
                "trip_tank_bbl": round(sample.trip_tank_bbl, 1),
            }
            if visible:
                row["bottomhole_psi"] = round(sample.bottomhole_psi)
                row["shoe_psi"] = round(sample.shoe_psi)
                row["influx_bbl"] = round(sample.influx_bbl, 1)
            rows.append(row)
        return rows

    def snapshot(self, since_event: int = 0, trace_limit: int = 240) -> dict:
        """Everything the panel needs in one poll.

        One round trip rather than four: the gauges, whatever events are new,
        and the strip chart all describe the same instant, and fetching them
        separately would let them disagree.
        """
        self.advance()
        return {
            "session_id": self.session_id,
            "gauges": self.gauges(),
            "events": self.events_since(since_event),
            "trace": self.trace(trace_limit),
            "lineup": self.lineup(),
            "schematic": self.schematic(),
        }

    def assess(self, require_kill: bool | None = None) -> Assessment:
        """Grade the run. The scenario's own standard applies unless overridden."""
        self.advance()
        if require_kill is None:
            standard = (
                self.scenario.standard if self.scenario is not None
                else Standard(require_kill=False)
            )
        else:
            standard = Standard(require_kill=require_kill)
        return self.simulator.assess(standard)

    def debrief(self, require_kill: bool | None = None) -> dict:
        assessment = self.assess(require_kill)
        return {
            "scenario": (
                None if self.scenario is None else self.scenario.summary()
            ),
            "grade": assessment.grade.value,
            "passed": assessment.passed,
            "killed": assessment.killed,
            "duration_min": round(assessment.duration_min, 1),
            "summary": assessment.summary,
            "metrics": [
                {
                    "name": metric.name,
                    "value": round(metric.value, 1),
                    "limit": None if metric.limit is None else round(metric.limit, 1),
                    "unit": metric.unit,
                    "passed": metric.passed,
                    "critical": metric.critical,
                    "detail": metric.detail,
                }
                for metric in assessment.metrics
            ],
            "events": [
                {
                    "clock": event.clock,
                    "severity": event.severity.value,
                    "message": event.message,
                }
                for event in assessment.events
                if event.severity.rank >= Severity.CAUTION.rank
            ],
            "report": assessment.format_report(),
        }

    def ai_debrief(self) -> dict:
        """Automated IADC/IWCF competency scoring, narrative diagnosis, and coaching tips."""
        from .ai_coach import evaluate_student_session

        self.advance()
        return evaluate_student_session(self).to_dict()

    def replay_data(self) -> dict:
        """Full flight-recorder telemetry history and events for timeline review."""
        self.advance()
        state = self.simulator.state
        return {
            "session_id": self.session_id,
            "student_name": self.student_name,
            "scenario_key": self.scenario.key if self.scenario else "custom",
            "scenario_name": self.scenario.name if self.scenario else "Custom Configuration",
            "total_samples": len(state.trace),
            "trace": [
                {
                    "time_s": round(s.time_s, 1),
                    "clock": clock(s.time_s),
                    "strokes": round(s.strokes),
                    "pumped_bbl": round(s.pumped_bbl, 2),
                    "bottomhole_psi": round(s.bottomhole_psi),
                    "casing_psi": round(s.casing_psi),
                    "standpipe_psi": round(s.standpipe_psi),
                    "shoe_psi": round(s.shoe_psi),
                    "influx_bbl": round(s.influx_bbl, 2),
                    "choke_percent": round(s.choke_percent, 1),
                    "pump_spm": round(s.pump_spm, 1),
                    "pit_gain_bbl": round(s.pit_gain_bbl, 2),
                }
                for s in state.trace
            ],
            "events": [
                {
                    "time_s": round(e.time_s, 1),
                    "clock": e.clock,
                    "kind": e.kind.value,
                    "severity": e.severity.value,
                    "message": e.message,
                }
                for e in state.event_log.events
            ],
        }


class SessionStore:
    """In-memory sessions, keyed by id.

    Deliberately not a database. This is a self-study trainer running on one
    machine; persistence is a stage 5 problem alongside the scenario library.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(
        self,
        scenario: str | None = None,
        student_name: str | None = None,
        instructor_name: str | None = None,
        well_name: str | None = None,
        complication: str | None = None,
        date: str | None = None,
        **kwargs,
    ) -> Session:
        session = (
            Session.for_scenario(scenario, **kwargs) if scenario
            else Session.for_configuration(**kwargs)
        )
        if student_name:
            session.student_name = student_name
        else:
            session.student_name = f"Student {len(self._sessions) + 1}"
        if instructor_name:
            session.instructor_name = instructor_name
        if well_name:
            session.well_name = well_name
        if complication:
            session.complication = complication
        if date:
            session.created_at = date
        self._sessions[session.session_id] = session
        return session

    def list(self) -> list[Session]:
        """All active sessions in store."""
        return list(self._sessions.values())

    def freeze_all(self, freeze: bool = True) -> None:
        """Master freeze/unfreeze across all active sessions."""
        for session in self._sessions.values():
            session.freeze(freeze)

    def gradebook(self) -> list[dict]:
        """Cohort gradebook entries for all active sessions."""
        rows = []
        for s in self._sessions.values():
            ai = s.ai_debrief()
            state = s.simulator.state
            sim = s.simulator
            rows.append({
                "session_id": s.session_id,
                "student_name": s.student_name,
                "scenario": s.scenario.name if s.scenario else "Custom",
                "scenario_key": s.scenario.key if s.scenario else "custom",
                "score": ai["score"],
                "letter_grade": ai["letter_grade"],
                "iwcf_standard": ai["iwcf_standard"],
                "well_killed": state.well_is_dead,
                "duration_min": round(state.time_s / 60.0, 1),
                "peak_shoe_psi": round(state.peak_shoe_pressure_psi),
                "frac_margin_psi": round(sim.frac_pressure_psi - state.peak_shoe_pressure_psi) if sim.frac_pressure_psi is not None else None,
                "max_bhp_excursion_psi": round(state.max_bhp_excursion_psi),
                "influx_taken_bbl": round(state.influx_taken_bbl, 2),
                "losses_bbl": round(state.lost_bbl, 2),
            })
        return rows

    def add(self, session: Session) -> Session:
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise KeyError(f"No session '{session_id}'")
        return self._sessions[session_id]

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def ids(self) -> list[str]:
        return list(self._sessions)
