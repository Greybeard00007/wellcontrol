"""A student's session: the engine, a clock, and the controls they can reach.

The engine advances by simulated seconds and knows nothing about wall time;
something has to decide *when* to step it. That is this module, and it is
deliberately kept free of any web framework so the time model
can be tested without the HTTP stack.

## Who calls advance, and when

:meth:`Session.advance` looks at how much wall time has passed since the last
call, multiplies by the speed setting, and steps the engine that far. **What
changed with the classroom is who calls it.**

* **Solo** -- each request calls it. Nothing runs when nobody is watching,
  which is right for a self-study trainer: a student who walks away has not
  lost the kick, and every test stays deterministic.
* **In a class** -- :meth:`wellcontrol.sim.classroom.Classroom.tick` calls it
  on a timer, whether or not anyone is looking. It has to: an instructor's
  *pause all* must stop the wells rather than the browsers, and a student
  reading their kill sheet must not be sitting on a frozen kick. See that
  module for why the clock moved.

**Catch-up is clamped** either way. Solo, without it, a student who leaves the
tab for ten minutes at 60x comes back to ten hours of simulated well integrated
in one request. Driven on a timer the clamp is a backstop against a stalled
event loop instead. :attr:`MAX_CATCHUP_S` caps what a single advance can cover.

**Mutating methods here are not thread-safe on their own.** With a class, the
clock driver and a student's request genuinely run at the same time; the lock
that makes that safe belongs to
:class:`~wellcontrol.sim.classroom.ClassroomStore` and is applied at the API
boundary, because direct library use is single-threaded and should not pay for
a hazard only the web layer has.

## Why the clock is injected

``clock`` defaults to :func:`time.monotonic` but tests pass their own, so the
whole time model is exercised without sleeping. Monotonic rather than wall
clock: the simulation must not jump when the machine's clock is adjusted.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .. import examples
from ..wellconfig import BOPLocation, Trajectory, WellConfiguration
from .assessment import Assessment, EventKind, Severity, Standard
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
    #: The exercise this session is running, if it came from one.
    scenario: object | None = None
    #: Drop to real time once, when the well first flows. See _should_slow_down.
    auto_slow: bool = True
    #: Wall-clock reading at the last advance.
    _last_tick: float | None = field(default=None, repr=False)
    _auto_slowed: bool = field(default=False, repr=False)

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
        simulator = Simulator(
            well=well,
            start=scenario.start,
            faults=scenario.faults,
            migration_rate_ft_per_hr=scenario.migration_rate_ft_per_hr,
        )
        session = cls(simulator=simulator, **kwargs)
        session.scenario = scenario

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

    def set_choke(self, percent: float, choke: str = "choke_1") -> None:
        """Position one of the four chokes. Defaults to remote choke 1.

        Raises:
            KeyError: if there is no such choke.
        """
        self.advance()
        chokes = self.simulator.chokes
        if choke not in chokes:
            raise KeyError(
                f"No choke '{choke}'. Available: {', '.join(sorted(chokes))}"
            )
        chokes[choke].command(float(percent))

    def set_pump(self, spm: float, pump: int = 1) -> None:
        """Set a rig pump's speed. ``pump`` is 1 or 2.

        Raises:
            ValueError: if there is no such pump.
        """
        self.advance()
        if pump not in (1, 2):
            raise ValueError(f"No pump {pump}. The rig has two: 1 and 2.")
        unit = self.simulator.pump if pump == 1 else self.simulator.pump_2
        if spm <= 0:
            unit.stop()
        else:
            unit.command(float(spm))

    #: The small pumps around the pit room, by the name the panel gives them.
    TRANSFER_PUMPS = (
        "trip_tank_fillup", "trip_tank_fill", "trip_tank_drain", "pit_transfer"
    )

    def _transfer_pump(self, name: str):
        sim = self.simulator
        pumps = {
            "trip_tank_fillup": sim.trip_tank.fillup_pump,
            "trip_tank_fill": sim.trip_tank.fill_pump,
            "trip_tank_drain": sim.trip_tank.drain_pump,
            "pit_transfer": sim.pits.transfer_pump,
        }
        if name not in pumps:
            raise KeyError(
                f"No transfer pump '{name}'. Available: "
                f"{', '.join(sorted(pumps))}"
            )
        return pumps[name]

    def set_rotary(self, rpm: float) -> None:
        """Set the top drive speed. It ramps rather than jumping."""
        self.advance()
        self.simulator.drilling.rotary.command(float(rpm))

    def set_weight_on_bit(self, klbf: float) -> None:
        """Feed off weight onto the bit.

        Ignored while the auto-driller has it -- which is the point of having
        an auto-driller, and a thing a student should have to notice.
        """
        self.advance()
        self.simulator.drilling.command_wob(float(klbf))

    def set_auto_driller(
        self, on: bool, setpoint_klbf: float | None = None
    ) -> None:
        self.advance()
        self.simulator.drilling.set_auto(on, setpoint_klbf)

    def set_transfer_pump(self, name: str, running: bool) -> None:
        """Start or stop a transfer pump.

        The fill-up pump is the one that matters: with the trip tank valve
        open, running it is what puts the tank in service and makes a trip
        measurable. See ``Simulator.trip_tank_in_service``.
        """
        self.advance()
        self._transfer_pump(name).set(bool(running))

    def zero(self, instrument: str) -> None:
        """Re-mark a gauge against its present reading.

        Wanted after a reserve transfer, which raises the pit level without the
        well having done anything -- and after picking up off bottom, when the
        trip tank should be marked before the trip rather than after it.

        Raises:
            KeyError: if there is no such instrument.
        """
        self.advance()
        if instrument == "pits":
            self.simulator.pits.zero()
        elif instrument == "trip_tank":
            self.simulator.trip_tank.zero()
        else:
            raise KeyError(
                f"Cannot zero '{instrument}'. Available: pits, trip_tank"
            )

    def _alarm(self, name: str):
        sim = self.simulator
        alarms = {
            "pit_deviation": sim.pits.deviation_alarm,
            "trip_tank_level": sim.trip_tank.alarm,
            "return_flow": sim.return_flow.alarm,
        }
        if name not in alarms:
            raise KeyError(
                f"No alarm '{name}'. Available: {', '.join(sorted(alarms))}"
            )
        return alarms[name]

    def set_alarm(
        self,
        name: str,
        low: float | None = None,
        high: float | None = None,
        armed: bool | None = None,
    ) -> None:
        """Move a set point, or arm and disarm the horn.

        A disarmed alarm still tracks and still lights its lamp, so the
        instructor can see what was silenced. Omitted arguments are left alone.
        """
        self.advance()
        alarm = self._alarm(name)
        if low is not None:
            alarm.low = float(low)
        if high is not None:
            alarm.high = float(high)
        if armed is not None:
            alarm.armed = bool(armed)

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

    # -- readouts ------------------------------------------------------------

    def gauges(self) -> dict:
        """What the choke panel displays.

        The instrument scan a student has to build is drill pipe pressure,
        casing pressure, stroke counter and choke position together, so those
        four come first and everything else is secondary.
        """
        state = self.simulator.state
        route = self.simulator.flow_path.return_route()
        visible = self.downhole_visible

        def downhole(value):
            """None while the student is meant to be detecting, not reading."""
            return None if not visible else value

        return {
            # The rig's own instruments. Always readable, because they are the
            # ones a driller actually has.
            "drill_pipe_psi": round(state.standpipe_pressure_psi),
            "casing_psi": round(state.casing_pressure_psi),
            "strokes": round(state.strokes),
            # The choke actually lined up, not always choke 1 -- a student who
            # changed over after a plug should be watching the handle they are
            # working. Falls back to choke 1 when nothing is in service.
            "choke_percent": round(
                self.simulator.choke_in_service.position, 1
            ),
            "choke_commanded": round(
                self.simulator.choke_in_service.commanded_position, 1
            ),
            "pump_spm": round(self.simulator.total_pump_spm, 1),
            "pump_commanded": round(self.simulator.total_pump_commanded_spm, 1),
            "pit_gain_bbl": round(self.simulator.pits.gain_bbl, 1),

            # The mud system. The trip tank is the instrument a trip turns on
            # and the pit deviation is the one a drilling kick turns on, so
            # both come through whole rather than as a single number.
            # Making hole. Present whatever the well is doing, because a
            # driller reads these continuously; they simply read zero when
            # nothing is turning.
            "drilling": self.simulator.drilling.summary(),
            "drilling_ahead": self.simulator.drilling_ahead,
            "hole_md_ft": round(self.simulator.hole_md),
            "hole_drilled_ft": round(state.hole_drilled_ft, 1),
            "trip_tank": self.simulator.trip_tank.summary(),
            "trip_tank_in_service": self.simulator.trip_tank_in_service,
            "pits": self.simulator.pits.summary(),
            "return_flow": self.simulator.return_flow.summary(),

            # Per-unit, because the panel has a dial for each and because
            # changing over after a failure is the drill.
            "pumps": [
                {
                    "spm": round(unit.spm, 1),
                    "commanded": round(unit.commanded_spm, 1),
                    "running": unit.running,
                    "flow_bpm": round(unit.flow_bbl_per_min, 2),
                }
                for unit in self.simulator.pumps
            ],
            # Both numbers per choke: a real choke takes seconds to travel, so
            # where the handle is and where it is going are different things,
            # and a student who cannot see the difference over-corrects.
            "chokes": {
                name: {
                    "position": round(choke.position, 1),
                    "commanded": round(choke.commanded_position, 1),
                }
                for name, choke in self.simulator.chokes.items()
            },
            "choke_in_service": next(
                (
                    valve.name
                    for valve in self.simulator.flow_path.active_chokes()
                    if valve.name in self.simulator.chokes
                ),
                None,
            ),
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
            "reverse_circulating": self.simulator.flow_path.is_reverse_circulating,

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

    def instructor_view(self) -> dict:
        """One tile of the instructor's monitor grid.

        **This is the one place ``downhole_visible`` is deliberately bypassed.**
        The mask exists so a student detecting a kick cannot read the answer off
        a gauge; an instructor watching fifteen wells has the opposite problem
        and needs to see which of them is in trouble before the student does.

        Compact on purpose. Fifteen of these are fetched together and the point
        of a grid is that trouble is visible without reading it -- so the flags
        are booleans a tile can colour on, not numbers to compare.
        """
        sim = self.simulator
        state = sim.state
        shoe = state.shoe_pressure_psi
        frac = sim.frac_pressure_psi
        maasp = sim.maasp_psi

        shoe_broken = frac is not None and shoe > frac
        over_maasp = maasp is not None and state.casing_pressure_psi > maasp

        if state.well_is_dead:
            status = "dead"
        elif shoe_broken:
            status = "shoe broken down"
        elif over_maasp:
            status = "over maasp"
        elif sim.pump.spm > 0:
            status = "circulating"
        elif sim.flow_path.bop_is_closed:
            status = "shut in"
        else:
            status = "open"

        return {
            "status": status,
            "paused": self.paused,
            "speed": self.speed,
            "time_s": round(state.time_s, 1),
            # The gauges the student is looking at, so the instructor can see
            # what they see.
            "drill_pipe_psi": round(state.standpipe_pressure_psi),
            "casing_psi": round(state.casing_pressure_psi),
            "pit_gain_bbl": round(sim.pits.gain_bbl, 1),
            "strokes": round(state.strokes),
            "choke_percent": round(sim.choke_in_service.position, 1),
            "pump_spm": round(sim.total_pump_spm, 1),
            # Whether they have the trip tank actually in service, which is
            # the difference between a measured trip and an unmeasured one.
            "trip_tank_change_bbl": round(sim.trip_tank.change_bbl, 2),
            "trip_tank_in_service": sim.trip_tank_in_service,
            "flow_differential_bpm": round(sim.return_flow.differential_bpm, 2),
            "alarms_sounding": [
                name for name, alarm in (
                    ("pit deviation", sim.pits.deviation_alarm),
                    ("trip tank level", sim.trip_tank.alarm),
                    ("return flow", sim.return_flow.alarm),
                ) if alarm.sounding
            ],
            # The truth, whatever the student can see.
            "bottomhole_psi": round(state.bottomhole_pressure_psi),
            "formation_psi": round(sim.formation_pressure_psi),
            "overbalance_psi": round(
                state.bottomhole_pressure_psi - sim.formation_pressure_psi
            ),
            "shoe_psi": round(shoe),
            "frac_psi": None if frac is None else round(frac),
            "maasp_psi": None if maasp is None else round(maasp),
            "influx_bbl": round(state.influx_volume_bbl, 1),
            "gas_at_surface": state.gas_at_surface,
            # Running totals the debrief will be built from, so a tile can show
            # a run going wrong rather than only a well that already has.
            "additional_influx_bbl": round(state.influx_taken_bbl, 1),
            "seconds_outside_band": round(state.seconds_outside_band),
            "peak_shoe_psi": round(state.peak_shoe_pressure_psi),
            "shoe_broken": shoe_broken,
            "over_maasp": over_maasp,
            "well_is_dead": state.well_is_dead,
            "bop_closed": sim.flow_path.bop_is_closed,
            # What the student is allowed to see, so the instructor knows
            # whether they are still meant to be detecting.
            "downhole_visible": self.downhole_visible,
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

    def schematic(self) -> dict:
        """Everything the well cross-section draws.

        Depths and the fluids currently occupying them. Withheld along with the
        rest of the downhole picture while a kick is still being detected --
        a cross-section showing exactly where the influx is would answer the
        question the student is meant to be working out.
        """
        sim = self.simulator
        state = sim.state
        picture = {
            "hole_md_ft": round(sim.hole_md),
            "bit_md_ft": round(state.bit_md_ft),
            "shoe_md_ft": None if sim.shoe_md is None else round(sim.shoe_md),
            "casing_id_in": sim.well.annulus.wellbore.sections[0].id_in,
            "hole_id_in": sim.well.annulus.wellbore.sections[-1].id_in,
            "visible": self.downhole_visible,
        }
        if not self.downhole_visible:
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
            "icp_psi": rounding.pressure(sidpp + scr),
            # Built from the weight actually mixed, not the calculated one.
            "fcp_psi": rounding.pressure(scr * kmw / mud),
            "formation_psi": round(sim.formation_pressure_psi),
            "maasp_psi": (
                None if sim.maasp_psi is None else round(sim.maasp_psi)
            ),
            "frac_psi": (
                None if sim.frac_pressure_psi is None
                else round(sim.frac_pressure_psi)
            ),
            "strokes_to_bit": round(sheet.strokes_to_bit),
            "strokes_bit_to_surface": round(sheet.strokes_bit_to_surface),
            "total_strokes": round(sheet.total_strokes),
            "scr_psi": round(sim.well.slow_circulating_rate.pressure_psi),
            "scr_spm": round(sim.well.slow_circulating_rate.spm),
            # Live, for comparison against the plan.
            "actual_strokes": round(state.strokes),
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
        samples = self.simulator.state.trace[-limit:]
        rows = []
        for sample in samples:
            row = {
                "time_s": round(sample.time_s, 1),
                # The trends screen can plot against strokes as well as time,
                # which is how a kill sheet is actually worked.
                "strokes": round(sample.strokes),
                "trip_tank_bbl": round(sample.trip_tank_bbl, 2),
                "casing_psi": round(sample.casing_psi),
                "drill_pipe_psi": round(sample.standpipe_psi),
                "pit_gain_bbl": round(sample.pit_gain_bbl, 1),
                "flow_out_bpm": round(sample.flow_out_bpm, 2),
                "flow_in_bpm": round(sample.flow_in_bpm, 2),
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


class SessionStore:
    """In-memory sessions, keyed by id.

    Deliberately not a database. This is a self-study trainer running on one
    machine; persistence is a stage 5 problem alongside the scenario library.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, scenario: str | None = None, **kwargs) -> Session:
        session = (
            Session.for_scenario(scenario, **kwargs) if scenario
            else Session.for_configuration(**kwargs)
        )
        self._sessions[session.session_id] = session
        return session

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
