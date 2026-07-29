"""The forward engine: what the well does, given what the student does.

Everywhere else in this package the question is "what pressure must be held to
keep bottomhole pressure constant?" Here it is the opposite: the student has set
a choke position and a pump speed, and the well responds -- correctly if they
got it right, and badly if they did not.

## The equation the whole thing turns on

Surface pressure is not commanded. It emerges from a volume balance::

    dP/dt = (Q_in + q_influx - Q_out - q_loss) / capacitance

where ``capacitance`` is how many barrels the closed system swallows per psi::

    capacitance = V_mud x c_mud + V_gas / P_gas_absolute

That second term is the one that teaches. Mud is nearly incompressible, so with
little gas in the hole the capacitance is tiny, the system is **stiff**, and a
small choke movement swings casing pressure hundreds of psi. As gas rises and
expands at falling pressure, ``V_gas / P`` grows by orders of magnitude, the
system goes **soft**, and the same choke movement does almost nothing --
until the gas is at surface and it goes violent again.

Nothing about that is scripted. A student who learns to anticipate it has
learned to work a choke.

## What emerges rather than being coded

* Choke too open  -> BHP falls below pore pressure -> another influx (step 6)
* Choke too shut  -> shoe over-pressured -> losses, then breakdown (step 7)
* Choke shut with the pump running -> pressure spike (small capacitance)
* Pump started against a closed valve -> deadhead (no injection path)
* BOP left open -> returns up the flowline, casing pressure stays at zero, and
  the choke does nothing at all

## Deliberate simplifications

Single gas bubble, ideal and isothermal. Annular friction scaled from the
measured slow circulating rate by the square of pump speed. No gas going into
solution in oil-based mud. These are the same simplifications the rest of the
library makes, and they are documented rather than hidden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from ..hydrostatics import expanded_gas_density
from ..methods.base import FluidColumn, WellState, Zone
from ..units import PSI_PER_PPG_FT, psi_to_psia
from .assessment import (
    Assessment,
    EventKind,
    EventLog,
    Severity,
    Standard,
    TraceSample,
)  # noqa: F401  -- Severity is used throughout the event calls below
from .drilling import DrillingModel
from .equipment import (
    Accumulator,
    AdjustableChoke,
    MudPump,
    PitSystem,
    ReturnFlowMeter,
    TripTank,
)
from .flowpath import FlowPath, ValveState

#: Compressibility of mud, 1/psi. Includes a little for casing expansion.
MUD_COMPRESSIBILITY: float = 3.0e-6

#: Pressure downstream of the choke, psig. Atmospheric via the separator.
DOWNSTREAM_PRESSURE: float = 0.0

#: How much the mud is overbalanced on the zone being drilled, psi, before the
#: well drills into anything it cannot hold. Enough to be comfortably stable,
#: little enough that the kick is not absurd when it comes.
DRILLING_OVERBALANCE_PSI: float = 150.0

#: Productivity index, bbl/min per psi of underbalance, for a shut-in start.
#: The kick is already in the hole there, so this only governs what happens if
#: the student lets the well go underbalanced again.
TIGHT_PRODUCTIVITY_INDEX: float = 0.004

#: Overbalance needed before the formation will take fluid at all, psi.
#:
#: A zone does not start accepting mud the moment it is overbalanced. Filter
#: cake, near-wellbore skin and relative permeability all have to be pushed
#: through first, which is why a well can sit a hundred psi overbalanced all day
#: without losing anything. It also keeps a normal kill -- flown a few tens of
#: psi above pore pressure -- from quietly bleeding into the formation, and it
#: is why bullheading needs real pressure rather than a nudge.
INJECTION_THRESHOLD_PSI: float = 300.0

#: How readily the zone takes fluid once that threshold is passed, bbl/min per
#: psi. Much lower than the productivity index in the other direction: a gas
#: sand gives up gas far more easily than it accepts mud.
INJECTION_INDEX: float = 0.004

#: Margin left above the shut-in shoe pressure by the weak-shoe fault, psi.
#: Tight enough to punish a heavy hand on the choke, wide enough that the
#: exercise can still be flown.
WEAK_SHOE_MARGIN_PSI: float = 250.0

#: How close the influx has to stay to the zone, ft, for bullheading to push it
#: back rather than merely squeezing mud past it.
GAS_AT_ZONE_TOLERANCE_FT: float = 200.0

#: Representative gas migration rate, ft/hr, where nobody has measured one.
#: Field figures run from a few hundred to a couple of thousand; 1,000 is the
#: number the teaching content works to.
DEFAULT_MIGRATION_FT_PER_HR: float = 1000.0

#: Productivity index for a drilling start. Higher, because the kick has to
#: **develop while the student is watching**. A tight zone bleeding in at a
#: fraction of a barrel a minute is realistic for some wells and useless as an
#: exercise: the pit gain takes an hour to become visible. This is a permeable
#: sand, which is also the case worth training for.
LIVE_PRODUCTIVITY_INDEX: float = 0.02


class StartState(str, Enum):
    """Where a session begins.

    ``SHUT_IN`` hands the student a kick that is already in the hole, sized and
    identified, with the pressures on the gauges. That is an exercise in working
    a choke.

    ``DRILLING`` hands them a well that is behaving. The kick has not happened
    yet; when it does they have to **notice it** -- pit gain, and flow out
    exceeding flow in -- shut in on their own initiative, and read their own
    shut-in pressures off the gauges. Everything the shut-in start gives away is
    something they have to earn.

    The second is the one that makes this a simulator rather than a trainer, and
    it carries a check the first cannot: the shut-in pressures that emerge from
    the volume balance have to agree with what the kill sheet says they should
    be. Nothing tells the engine what SIDPP ought to read.
    """

    SHUT_IN = "shut_in"
    DRILLING = "drilling"


@dataclass
class SimState:
    """Everything that changes as the simulation runs."""

    time_s: float = 0.0
    casing_pressure_psi: float = 0.0
    standpipe_pressure_psi: float = 0.0
    #: Cumulative volume pumped into the string, which is what advances the
    #: annulus contents.
    pumped_bbl: float = 0.0
    #: Annular volume the influx has climbed past under its own buoyancy,
    #: as distinct from what the pump has displaced beneath it.
    migrated_bbl: float = 0.0
    #: Mud put into the annulus from the top and left to fall through the gas.
    #: Lube and Bleed is executed in measured slugs of this, so it is the
    #: instrument the method is worked to.
    lubricated_bbl: float = 0.0
    #: Reverse circulating pushes the annulus contents back *down*, so this
    #: cancels the volume beneath the influx until there is none left and the
    #: influx is at the bit.
    reverse_displaced_bbl: float = 0.0
    #: Volume that has followed the influx into the string once it reached the
    #: bit. Positions the influx on its way up the inside of the pipe.
    reverse_pumped_bbl: float = 0.0
    #: True once the influx has been pushed out of the annulus and into the
    #: string, where reverse circulation carries it to surface.
    influx_in_string: bool = False
    #: Where it has got to inside the pipe, once it is in there.
    influx_string_top_md: float = 0.0
    influx_string_bottom_md: float = 0.0

    # -- what the student actually read off the gauges ------------------------
    #: Captured once, when the pressures settle after a shut-in. These are the
    #: numbers a kill sheet is built from, and on a drilling exercise they are
    #: emphatically not the ones the scenario was designed around -- a slow
    #: shut-in takes more influx and reads higher.
    measured_sidpp_psi: float | None = None
    measured_sicp_psi: float | None = None
    measured_pit_gain_bbl: float | None = None

    # -- stripping -----------------------------------------------------------
    #: Pipe run into the hole since the start, ft. Negative is pulling out.
    pipe_moved_ft: float = 0.0
    #: Hole made this run. The DCU counts it, and it is the
    #: difference between well depth and bit depth going to zero.
    hole_drilled_ft: float = 0.0
    #: Steel put into the well by that movement, bbl. Closed-end, because a
    #: string being stripped has a float in it. This is the volume that has to
    #: come out of the choke to hold bottomhole pressure.
    pipe_displacement_bbl: float = 0.0
    #: Where the bit has got to.
    bit_md_ft: float = 0.0
    strokes: float = 0.0
    influx_volume_bbl: float = 0.0
    influx_bottom_md: float = 0.0
    influx_top_md: float = 0.0
    bottomhole_pressure_psi: float = 0.0
    shoe_pressure_psi: float = 0.0
    flow_in_bpm: float = 0.0
    flow_out_bpm: float = 0.0
    influx_rate_bpm: float = 0.0
    #: Fluid going back into the kicking zone, bbl/min. Bullheading is the
    #: deliberate case; it is also what a badly over-pressured well does.
    injection_rate_bpm: float = 0.0
    #: Cumulative volume squeezed back into the formation, bbl.
    injected_bbl: float = 0.0
    loss_rate_bpm: float = 0.0
    kill_mud_pumped_bbl: float = 0.0
    #: Volume taken out through the choke with the pump off. The Volumetric
    #: method is executed by bleeding *measured* increments, so this is the
    #: instrument the whole method is worked to, not a diagnostic.
    bled_bbl: float = 0.0
    gas_at_surface: bool = False
    well_is_dead: bool = False

    # -- recorded for the debrief -------------------------------------------
    #: Structured log. Prefer this to :attr:`events`, which is the same thing
    #: flattened to strings.
    event_log: EventLog = field(default_factory=EventLog)
    trace: list[TraceSample] = field(default_factory=list)

    #: Time integrals. Only the engine sees every timestep, so it accumulates
    #: these rather than letting the assessor sample them off the trace and
    #: miss short excursions between samples.
    seconds_outside_band: float = 0.0
    influx_taken_bbl: float = 0.0
    lost_bbl: float = 0.0

    #: Running extremes, for the same reason.
    max_bhp_excursion_psi: float = 0.0
    peak_casing_pressure_psi: float = 0.0
    peak_shoe_pressure_psi: float = 0.0

    @property
    def events(self) -> list[str]:
        """The log as printable lines."""
        return self.event_log.messages

    def record(self, kind: EventKind, message: str, **kwargs) -> None:
        self.event_log.record(kind, message, time_s=self.time_s, **kwargs)

    def set_condition(self, kind: EventKind, active: bool, **kwargs) -> None:
        self.event_log.set_condition(kind, active, time_s=self.time_s, **kwargs)

    def set_level(self, kind: EventKind, value: float, **kwargs) -> None:
        self.event_log.set_level(kind, value, time_s=self.time_s, **kwargs)


@dataclass
class Simulator:
    """A well, a rig, and a student's hands on the controls.

    Args:
        well: The well and the kick, reusing the library's model.
        flow_path: The lineup. Defaults to a rig lined up for normal drilling.
        pump: The mud pump.
        choke: The adjustable choke the student works.
        productivity_index: How hard the formation flows when underbalanced,
            bbl/min per psi of underbalance. Higher makes a well less
            forgiving.
        loss_index: How readily the shoe takes fluid once fractured, bbl/min
            per psi over the fracture pressure.
        kill_mud_weight_ppg: What is in the pits. Set to the original mud
            weight for a Driller's first circulation.
    """

    well: WellState
    #: Left unset, the lineup is built for the stack this well actually has --
    #: a surface stack and a subsea one carry different elements and different
    #: numbers of outlet valves, so which one is under the rig is not something
    #: a default factory can know.
    flow_path: FlowPath | None = None
    pump: MudPump | None = None
    #: The second rig pump. Real because pump 1 failing mid-kill is a
    #: drill worth running, and because a rig has two.
    pump_2: MudPump | None = None
    #: Remote choke 1 -- the one a kill is normally flown on, and the one
    #: every existing caller means by ``sim.choke``.
    choke: AdjustableChoke = field(default_factory=AdjustableChoke)
    choke_2: AdjustableChoke = field(default_factory=AdjustableChoke)
    #: The handwheels on the manifold. Same orifice, slower to move, because
    #: that is the difference a student actually feels when the remote fails.
    manual_choke_1: AdjustableChoke = field(
        default_factory=lambda: AdjustableChoke(travel_rate=4.0)
    )
    manual_choke_2: AdjustableChoke = field(
        default_factory=lambda: AdjustableChoke(travel_rate=4.0)
    )
    pits: PitSystem = field(default_factory=PitSystem)
    trip_tank: TripTank = field(default_factory=TripTank)
    #: The flowline paddle. Its differential is the earliest kick
    #: indicator the rig has -- a rate, where a pit gain is a volume.
    return_flow: ReturnFlowMeter = field(default_factory=ReturnFlowMeter)
    accumulator: Accumulator = field(default_factory=Accumulator)
    #: Weight on bit, rotary, torque and penetration rate. Only ever acts
    #: while drilling ahead -- see `drilling_ahead`.
    drilling: DrillingModel = field(default_factory=DrillingModel)
    #: bbl/min per psi of underbalance. None picks a default from the start
    #: state: a drilling kick has to develop while the student watches, where a
    #: shut-in start only needs this if they lose control again.
    productivity_index: float | None = None
    #: bbl/min per psi of overbalance beyond INJECTION_THRESHOLD_PSI.
    injection_index: float = INJECTION_INDEX
    loss_index: float = 0.01
    kill_mud_weight_ppg: float | None = None
    #: How fast gas climbs through the mud, ft/hr. None takes the well's own
    #: figure, which is zero where migration is not reliable.
    migration_rate_ft_per_hr: float | None = None
    #: Pipe speed through the stripping element, ft/min. Positive runs in.
    strip_rate_ft_per_min: float = 0.0
    #: Faults waiting to fire. Each does one thing to one piece of equipment;
    #: see wellcontrol.sim.scenario. Typed loosely to keep the engine from
    #: importing the scenario module, which imports it.
    faults: tuple = ()
    #: Where the session begins. See :class:`StartState`.
    start: StartState = StartState.SHUT_IN
    #: Seconds of *circulating* before the bit drills into the kicking zone.
    #: Only used from a drilling start. None leaves the kick to be triggered by
    #: hand, which is how an instructor injects it.
    kick_after_s: float | None = 120.0
    #: Bottomhole pressure counts as on target within this much of formation
    #: pressure. The engine needs it because time-off-target is a time integral.
    bhp_band_psi: float = 50.0
    #: How often the trace is sampled, in simulated seconds.
    trace_interval_s: float = 10.0
    state: SimState = field(default_factory=SimState)

    def __post_init__(self) -> None:
        subsea = self.well.configuration.is_subsea
        if self.flow_path is None:
            self.flow_path = FlowPath.for_stack(subsea=subsea)
        # A subsea stack is worked through one of two control pods; a surface
        # one has no such thing, and a blank pod is what tells the closing
        # unit -- and the panel -- which of the two it is.
        if subsea and not self.accumulator.pod:
            self.accumulator.pod = "blue"
        if self.pump is None:
            self.pump = MudPump(pump=self.well.pump)
        if self.pump_2 is None:
            self.pump_2 = MudPump(pump=self.well.pump)
        if self.kill_mud_weight_ppg is None:
            self.kill_mud_weight_ppg = self.well.mud_weight_ppg
        if self.productivity_index is None:
            self.productivity_index = (
                LIVE_PRODUCTIVITY_INDEX if self.start is StartState.DRILLING
                else TIGHT_PRODUCTIVITY_INDEX
            )

        self.formation_pressure_psi = self.well.formation_pressure_psi
        self.frac_pressure_psi = self.well.frac_pressure_psi
        self._reset_geometry()

        # Set before the first column is built, because the density of the gas
        # is measured against them. At shut-in the gas is at its reference
        # state, so the fallback in _influx_density_ppg is the right answer
        # until these are filled in below.
        self._initial_gas_pv = 0.0
        self._initial_gas_volume_bbl = self.well.pit_gain_bbl
        self.state.bit_md_ft = self.well.bit_md_ft

        #: Pore pressure once the kicking zone is drilled. Held aside from
        #: ``formation_pressure_psi`` so a drilling start can raise it later.
        self.kick_pressure_psi = self.well.formation_pressure_psi
        self.kick_taken = self.start is StartState.SHUT_IN
        self._circulating_s = 0.0
        #: (reverse circulating, influx in the string). A change means the
        #: relationship between the two surface gauges has changed hands.
        self._mode = (False, False)

        if self.start is StartState.SHUT_IN:
            self._begin_shut_in()
        else:
            self._begin_drilling()

        self._next_sample_s = 0.0
        self._record_sample()

    def _begin_shut_in(self) -> None:
        """The kick is already in the hole and the well is closed on it."""
        self.state.influx_volume_bbl = self.well.pit_gain_bbl
        self._place_influx()
        self.state.casing_pressure_psi = self.well.sicp_psi
        self._gas_pv = self._gas_pressure_psia() * self.well.pit_gain_bbl
        self._initial_gas_pv = self._gas_pv
        self._update_pressures()
        self.state.record(
            EventKind.SHUT_IN,
            f"Shut in. SIDPP {self.well.sidpp_psi:.0f} psi, "
            f"SICP {self.well.sicp_psi:.0f} psi, "
            f"pit gain {self.well.pit_gain_bbl:.1f} bbl.",
            sidpp_psi=self.well.sidpp_psi,
            sicp_psi=self.well.sicp_psi,
            pit_gain_bbl=self.well.pit_gain_bbl,
        )

    def _begin_drilling(self) -> None:
        """A well that is behaving, on a zone the mud can hold.

        Pore pressure starts below the static mud column by
        :data:`DRILLING_OVERBALANCE_PSI`, so nothing flows until the bit reaches
        the zone the mud weight cannot hold. Nothing is placed in the annulus
        and no surface pressure is set: whatever the student ends up reading,
        they made happen.
        """
        self.state.influx_volume_bbl = 0.0
        self.state.casing_pressure_psi = 0.0
        self._gas_pv = 0.0
        self._initial_gas_pv = 0.0
        self._initial_gas_volume_bbl = 0.0
        self._place_influx()

        hydrostatic = self._annulus_column().total_hydrostatic
        self.formation_pressure_psi = hydrostatic - DRILLING_OVERBALANCE_PSI
        self._update_pressures()

        self.state.record(
            EventKind.DRILLING,
            f"Drilling ahead at {self.well.bit_md_ft:,.0f} ft. Mud "
            f"{self.well.mud_weight_ppg:.1f} ppg, "
            f"{DRILLING_OVERBALANCE_PSI:.0f} psi overbalanced. "
            "Watch the pit level and the flow line.",
            bit_md_ft=self.well.bit_md_ft,
        )

    def trigger_kick(self) -> None:
        """Drill into the zone the mud weight cannot hold.

        Pore pressure steps up to what the kill sheet was built on, which is how
        a kick actually arrives -- the mud was adequate, and then it was not.
        Nothing else is done to the well: the influx enters through the same
        productivity-index response as any other underbalance, so the pit gain
        and the flow-out excess the student is watching for are consequences,
        not a script.
        """
        if self.kick_taken:
            return
        self.kick_taken = True
        self.formation_pressure_psi = self.kick_pressure_psi
        self.state.record(
            EventKind.KICK_ZONE_DRILLED,
            "Drilled into a higher pressure zone.",
            severity=Severity.INFO,
            pore_pressure_psi=self.kick_pressure_psi,
        )

    # -- geometry ------------------------------------------------------------

    def _reset_geometry(self) -> None:
        self.annulus_volume_bbl = self.well.annulus.volume_bbl
        self.string_volume_bbl = self.well.annulus.string.internal_volume_bbl
        self.hole_md = self.well.hole_md_ft
        self.shoe_md = self.well.shoe_md
        self.influx_density_ppg = self.well.influx_density_ppg
        self.influx_gradient = self.well.influx_gradient_psi_per_ft
        if self.migration_rate_ft_per_hr is None:
            self.migration_rate_ft_per_hr = self._default_migration_rate()

    def _place_influx(self) -> None:
        """Put the influx where the mud beneath it says it is.

        Three things put mud under the influx: what the pump displaced round
        the bit, what the gas climbed past under its own buoyancy, and what was
        lubricated in on top and fell through it.

        One thing takes it away again. **Mud that went into the formation never
        displaced anything.** It leaves at the bit, where it was pumped to, so
        it does not push the annulus up. Without that subtraction, bullheading
        pushed the influx *up* the hole while claiming to be squeezing it back
        down -- the pump advanced the annulus at the full rate even though
        every barrel was disappearing into the zone.
        """
        state = self.state
        below = min(
            max(
                0.0,
                max(0.0, state.pumped_bbl - state.injected_bbl)
                + state.migrated_bbl
                + state.lubricated_bbl
                - state.reverse_displaced_bbl,
            ),
            self.annulus_volume_bbl,
        )
        state.influx_bottom_md = self.well.annulus_md_at_volume_from_bottom(below)
        capacity_above = self.annulus_volume_bbl - below
        # Once the influx has been pushed into the string it occupies no
        # annulus, so the annular zones close up around where it was rather
        # than leaving a hole in the column.
        volume = (
            0.0 if state.influx_in_string
            else max(0.0, min(state.influx_volume_bbl, capacity_above))
        )
        state.influx_top_md = self.well.annulus_md_at_volume_from_bottom(
            below + volume
        )
        state.gas_at_surface = volume > 0 and state.influx_top_md <= 1.0

        # Where the influx would be inside the pipe. Computed unconditionally,
        # not only once it is in there: the gas pressure is read from these,
        # and on the step the influx first enters the string a stale pair reads
        # the pressure at surface instead of at the bit. That put the bubble at
        # 240 bbl and spiked the well by 4,500 psi -- entirely an artefact of
        # an uninitialised position. With nothing pumped they both sit at the
        # bit, which is where the influx is.
        in_string = min(state.reverse_pumped_bbl, self.string_volume_bbl)
        string_volume = min(
            state.influx_volume_bbl, self.string_volume_bbl - in_string
        )
        state.influx_string_bottom_md = self.well.string_md_at_volume_from_bit(
            in_string
        )
        state.influx_string_top_md = self.well.string_md_at_volume_from_bit(
            in_string + string_volume
        )
        if state.influx_in_string:
            state.gas_at_surface = (
                string_volume > 0 and state.influx_string_top_md <= 1.0
            )

    def _influx_density_ppg(self) -> float:
        """Density of the gas as it currently stands, ppg.

        A fixed mass of gas in a bigger volume is lighter, so density falls as
        the bubble expands on its way up. Holding the shut-in density would
        create gas mass from nothing, and the resulting excess hydrostatic
        under-states casing pressure -- by 182 psi at the peak on the reference
        well, which is the wrong direction to be wrong in.

        Unlike the planner's one-way circulation, the simulator can take *more*
        gas when the student lets the well go underbalanced, so this scales by
        mass as well as by volume. ``_gas_pv`` stands in for the mass: at fixed
        temperature ``PV`` is proportional to how many moles are down there.
        """
        state = self.state
        if state.influx_volume_bbl <= 0.0 or self._initial_gas_pv <= 0.0:
            return self.influx_density_ppg
        mass_ratio = self._gas_pv / self._initial_gas_pv
        return mass_ratio * expanded_gas_density(
            self.influx_density_ppg,
            self._initial_gas_volume_bbl,
            state.influx_volume_bbl,
        )

    def _default_migration_rate(self) -> float:
        """How fast gas climbs, ft/hr, when nobody has measured it.

        **The planner and the simulator mean different things by zero.**
        ``WellState.gas_migration_rate_ft_per_hr`` is a *measured* rate, and
        zero there means "do not assume the gas is migrating" -- the right
        conservative default when planning, because a method that depends on
        migration should not be recommended on an assumption.

        A simulator cannot take that view. The well does what it does, and gas
        in a vertical hole rises whether or not anybody put a number on it. So
        a measured rate wins where there is one, and otherwise the gas migrates
        at a representative rate wherever the trajectory gives it an up to go
        in. In a lateral it stays where it is, which is the whole reason
        Volumetric is blocked on horizontal wells.
        """
        measured = self.well.gas_migration_rate_ft_per_hr
        if measured > 0.0:
            return measured
        if self.well.configuration.trajectory.migration_reliable:
            return DEFAULT_MIGRATION_FT_PER_HR
        return 0.0

    def _migrate_gas(self, dt: float) -> float:
        """Let the influx climb, and return the expansion it wants, bbl.

        **Gas migrating in a shut-in well takes its pressure with it.** It
        cannot expand -- the well is closed and mud is nearly incompressible --
        so a bubble that rises 1,000 ft arrives still carrying the pressure it
        had 1,000 ft lower. The mud now beneath it adds its own hydrostatic on
        top, and bottomhole pressure climbs. Left alone it will break the shoe;
        bleeding it off in controlled steps is the Volumetric method, and this
        is the physics that method exists to answer.

        Rather than special-case the pressure rise, this returns the volume the
        gas *would* take up at its new depth if it were free to expand, and the
        caller feeds that into the same volume balance as everything else. In a
        closed system the balance turns it into pressure, and the arithmetic
        comes out at the textbook answer::

            dP = (dV_wanted) / capacitance
               = (V (rho dh) / P) / (V / P)
               = rho dh

        -- the mud gradient times the distance climbed, which is exactly what a
        gauge on a shut-in well does. Getting that for free is the reason for
        expressing it this way rather than adding a rule.

        Gas that has reached surface has nowhere further to go, and gas in a
        lateral has no "up" to go in, so both return zero.
        """
        state = self.state
        if state.influx_volume_bbl <= 0.0 or self.migration_rate_ft_per_hr <= 0.0:
            return 0.0
        if state.influx_top_md <= 1.0:
            return 0.0

        feet = self.migration_rate_ft_per_hr * dt / 3600.0
        if feet <= 0.0:
            return 0.0

        # A lateral has no vertical component for buoyancy to act on, and the
        # survey is what says so.
        climb = self.well.survey.vertical_height_of(
            max(0.0, state.influx_top_md - feet), state.influx_top_md
        )
        if climb <= 1e-6:
            return 0.0

        before_psia = self._gas_pressure_psia()
        # Measured at the influx *top*, because the position is parameterised
        # by the volume beneath the bubble and the top is the leading edge --
        # it is what decides when gas reaches the casing shoe and then surface.
        # Taking it at the bottom instead made the bubble climb at two thirds
        # of the stated rate wherever the annulus widened above it. The bottom
        # then rises faster than the top, which is correct: a fixed volume
        # moving into a wider annulus gets shorter.
        capacity = self.well.annulus.capacity_at(state.influx_top_md)
        state.migrated_bbl = min(
            state.migrated_bbl + capacity * feet, self.annulus_volume_bbl
        )
        self._place_influx()
        after_psia = self._gas_pressure_psia()

        if after_psia >= before_psia:
            return 0.0
        return self._gas_pv / after_psia - self._gas_pv / before_psia

    # -- faults --------------------------------------------------------------

    def _apply_faults(self) -> None:
        """Fire any fault whose moment has come.

        **A fault does one thing to one piece of equipment.** What follows is
        whatever the well does about it, through the same volume balance as
        everything else. Nothing here knows what a kick is or how a kill should
        go, and a fault cannot make the well behave in a way it could not have
        behaved on its own.

        That distinction is the point. A scripted "and now the pressure spikes"
        teaches a student to recognise a scripted spike; a choke that plugs
        while they are holding 400 psi on it teaches them what a plugged choke
        feels like.
        """
        if not self.faults:
            return
        state = self.state
        remaining = []
        for fault in self.faults:
            if not fault.is_due(state.time_s, state.strokes, state.event_log):
                remaining.append(fault)
                continue
            # The same call an instructor makes by hand. Deliberately one path:
            # a fault whose moment came on its own and one thrown live must be
            # indistinguishable to the student, and the surest way to keep them
            # so is for there to be nothing to keep in step.
            self.inject_fault(fault)
        self.faults = tuple(remaining)

    def inject_fault(self, fault) -> None:
        """Fire a fault now, rather than waiting for its trigger.

        **The instructor's hand, and the student must not be able to feel it.**
        This goes through exactly the same `_break_equipment` and the same log
        entry as a scenario fault whose moment arrived on its own, so a run
        with a fault thrown at it is indistinguishable from one where the same
        fault was written into the exercise. Nothing in the event, the gauges
        or the trace records that a human chose the moment.

        That is the whole point. A student who can tell an instructor just did
        something to them starts flying the instructor instead of the well.
        """
        self._break_equipment(fault)
        self.state.record(
            EventKind.EQUIPMENT_FAILURE,
            fault.message or f"{fault.kind.label}.",
            severity=fault.severity,
            fault=fault.kind.value,
        )

    # -- the stack ------------------------------------------------------------

    def operate_stack_valve(self, name: str, target: ValveState) -> bool:
        """Shift one thing on the closing unit, and pay for it.

        The stack elements **and the outlet HCRs**: both are worked from the
        BOP panel, through the same master valve and off the same bottles, so
        both come through here. Lining the choke side up is a closing unit
        function and not a walk to the manifold, which matters the moment the
        unit is in trouble -- a student who cannot shut an annular cannot open
        a choke line either, and finding that out is the point.

        Everything about working the stack that is not simply setting a valve
        lives here rather than in the session, because all of it is well
        control: what a function costs the closing unit, whether there is
        enough regulated pressure behind it to move at all, and what closing a
        shear ram does to the string.

        **A thing that cannot work does not work.** Un-armed, with the bottles
        down, a regulator wound off or a dead pod selected, the valve simply
        does not move -- no error, no message. That is what the rig gives you,
        and working out why is the exercise.

        Returns:
            True if it moved.
        """
        valve = self.flow_path.valve(name)
        if not valve.on_the_closing_unit:
            raise ValueError(f"{name} is not worked from the BOP panel")
        if valve.state is target:
            return True
        if not self.accumulator.operate(valve.bop_kind or "hcr"):
            return False

        closing = target is ValveState.CLOSED
        # Shearing is a one-way door and only happens on the way shut, with
        # pipe across the rams. The string being cut is a fact about the well
        # from then on, not a state the student can put back by re-opening.
        if closing and valve.shears and not self.flow_path.pipe_sheared:
            self.flow_path.pipe_sheared = True
            self.state.record(
                EventKind.PIPE_SHEARED,
                f"{valve.label} closed on the string.",
                severity=Severity.CRITICAL,
                element=valve.name,
            )
        self.flow_path.set_valve(name, target)
        return True

    def _break_equipment(self, fault) -> None:
        kind = fault.kind.value
        if kind == "pump_failure":
            # Whichever one is turning, so the fault always bites -- and so the
            # answer is to change pumps, which is what a rig would do.
            failed = next((p for p in self.pumps if p.running), self.pump)
            failed.stop()
            failed.spm = 0.0
            failed.max_spm = 0.0
        elif kind == "choke_plugged":
            # The handle still moves; nothing gets through it. Plugs the
            # choke actually in service, so changing over is the answer.
            plugged = next(iter(self.chokes_in_service), self.choke)
            plugged.full_open_area_in2 = 0.02
        elif kind == "choke_washout":
            # The opposite problem: it cannot hold pressure any more. Cuts
            # out whichever one is being flown, for the same reason.
            washed = next(iter(self.chokes_in_service), self.choke)
            washed.full_open_area_in2 *= 4.0
        elif kind == "accumulator_leak":
            self.accumulator.pressure_psi = 0.0
            self.accumulator.recharge_psi_per_min = 0.0
        elif kind == "pod_failure":
            # Whichever pod is selected, so the fault always bites and the
            # answer is always to change over -- the same shape as the pump
            # and choke faults. Nothing to fail on a surface stack.
            if self.accumulator.pod:
                self.accumulator.failed_pods.add(self.accumulator.pod)
        elif kind == "weak_shoe":
            if self.frac_pressure_psi is not None:
                # Measured against the pressure the shoe is *already* carrying,
                # not against its own hydrostatic. Halving the latter put the
                # fracture pressure below the shut-in shoe pressure, so the
                # well began the exercise already broken down and there was
                # nothing to fly. This leaves a real but unforgiving window.
                self.frac_pressure_psi = min(
                    self.frac_pressure_psi,
                    self.state.shoe_pressure_psi + WEAK_SHOE_MARGIN_PSI,
                )

    # -- the driller's own instruments ---------------------------------------

    @property
    def hookload_lbf(self) -> float:
        """What the weight indicator reads, lbf.

        String weight in air, less buoyancy, less the upward push of well
        pressure on the closed end of the pipe. That last term is why this
        belongs on a stripping screen rather than being scenery: as surface
        pressure climbs the well tries to push the string out of the hole, the
        hookload falls, and when it reaches zero the pipe has to be snubbed in
        rather than stripped. A student watching only pressure gauges never
        sees it coming.
        """
        weight_in_air = sum(
            component.length_ft * self.well.pipe_weight_lb_per_ft
            for component in self.well.annulus.string.components
        )
        buoyancy = max(0.0, 1.0 - self.well.mud_weight_ppg / 65.5)
        area_in2 = math.pi / 4.0 * self._stripping_pipe.od_in ** 2
        upward = self.state.casing_pressure_psi * area_in2
        # Weight put on the bit is weight taken off the hook. This is the
        # relation the driller actually works to: they lower the block
        # until the indicator drops by the weight they want on bottom.
        on_bit = self.drilling.wob_klbf * 1000.0
        return weight_in_air * buoyancy - upward - on_bit

    @property
    def _stripping_pipe(self):
        """The pipe going through the element -- the drill pipe, in practice."""
        return max(
            self.well.annulus.string.components, key=lambda c: c.length_ft
        )

    # -- stripping -----------------------------------------------------------

    def strip(self, feet_per_min: float) -> None:
        """Run pipe in, or pull it out, at a given speed.

        Positive runs in. The student sets a speed and works the choke against
        it, exactly as they set a pump rate and work the choke against that.
        """
        self.strip_rate_ft_per_min = float(feet_per_min)

    @property
    def closed_end_displacement_bbl_per_ft(self) -> float:
        """Steel displaced per foot of pipe run in.

        **Closed-end**, because a string being stripped has a float in it: the
        bore goes in full and displaces along with the metal, so it is the
        whole outside diameter that counts and not just the wall. Taking the
        open-end figure instead under-states what has to be bled by a factor of
        three or so, and the bleed volume *is* the method.

        Measured on the drill pipe, which is what is going through the
        stripping element for all but the first stand or two.
        """
        components = self.well.annulus.string.components
        pipe = max(components, key=lambda c: c.length_ft)
        return pipe.closed_end_displacement_bbl_per_ft

    def _advance_stripping(self, dt: float) -> float:
        """Move the pipe, and return the volume of steel that put in, bbl.

        The displaced volume is handed to the same balance as everything else.
        Closed in, there is nowhere to put it and it becomes pressure -- which
        is the thing the student is bleeding against.
        """
        if self.strip_rate_ft_per_min == 0.0:
            return 0.0
        state = self.state
        moved = self.strip_rate_ft_per_min * dt / 60.0

        # Stop at the bottom of the hole going in, and at surface coming out.
        limit_in = self.hole_md - state.bit_md_ft
        limit_out = -state.bit_md_ft
        moved = max(limit_out, min(moved, limit_in))
        if moved == 0.0:
            return 0.0

        state.pipe_moved_ft += moved
        state.bit_md_ft += moved
        displaced = self.closed_end_displacement_bbl_per_ft * moved
        state.pipe_displacement_bbl += displaced
        return displaced

    # -- the mud system ------------------------------------------------------

    @property
    def trip_tank_in_service(self) -> bool:
        """Whether the trip tank is the vessel measuring the well.

        **Both the valve and the pump.** Lining the tank up to the annulus and
        running the fill-up pump is what puts a trip tank in service on a rig:
        the pump circulates into the hole, the overflow comes back, and the
        level shows the net. Open the valve without the pump and nothing is
        keeping the hole full, so what the well takes comes from wherever
        somebody is filling it from -- the active system, unmeasured.

        That is the lineup a driller is taught, and getting it wrong here has
        the same consequence it has on the rig: the trip is not being measured
        and a kick that arrives during it is invisible until the pit gain shows
        it, by which time it is very much larger.
        """
        return (
            self.flow_path.trip_tank_is_lined_up
            and self.trip_tank.fillup_pump.running
        )

    @property
    def drilling_ahead(self) -> bool:
        """Whether the bit is actually making hole.

        **Every condition here is a lock-out.** The drilling model must not be
        able to touch a kill: with the BOP closed, or the pump off, or the
        rotary stopped, or no weight on the bit, nothing is drilled and nothing
        in this module runs. That is what keeps a validated shut-in circulation
        exactly as it was before any of this existed.
        """
        return (
            self.start is StartState.DRILLING
            and not self.flow_path.bop_is_closed
            and self.pumping
            and self.drilling.rotary.turning
            and self.drilling.wob_klbf > 0.1
            and self.state.bit_md_ft >= self.hole_md - 1.0
        )

    def _make_hole(self, dt: float) -> float:
        """Drill, and return the mud the new hole swallowed, bbl.

        The rate comes from the differential pressure the bit is working
        against, so a zone the mud cannot hold announces itself as a **drilling
        break** before anything reaches the pits. Nothing here knows a kick is
        coming; it reads the same bottomhole pressure as everything else.
        """
        overbalance = (
            self.state.bottomhole_pressure_psi - self.formation_pressure_psi
        )
        rop = self.drilling.penetration_rate(overbalance)
        if rop <= 0.0:
            return 0.0

        drilled = rop * dt / 3600.0
        self.hole_md += drilled
        self.state.bit_md_ft += drilled
        self.state.hole_drilled_ft += drilled

        # New hole is volume that has to be filled. The pipe run in behind the
        # bit displaces its own steel, so only the difference is mud.
        per_ft = max(
            0.0,
            self._hole_capacity_bbl_per_ft
            - self.closed_end_displacement_bbl_per_ft,
        )
        self.annulus_volume_bbl += per_ft * drilled
        return per_ft * drilled

    @property
    def _hole_capacity_bbl_per_ft(self) -> float:
        diameter = self.well.annulus.wellbore.sections[-1].id_in
        return diameter * diameter / 1029.4

    def _settle_surface_volume(
        self,
        returns_bbl: float,
        displacement_bbl: float,
        lost_bbl: float,
        route,
    ) -> None:
        """Put the volume the well gave up, or swallowed, somewhere real.

        Two things decide where it goes and how much there is.

        **Is the hole open?** Closed in, steel run into the well has nowhere to
        go and becomes pressure -- the balance above has already done that, and
        adding it here as well would count it twice. Open, the same steel
        pushes an equal volume of mud out of the hole and it has to land in a
        tank. That path was previously unmodelled, which meant tripping moved
        no surface volume at all and a kick taken while tripping was invisible.

        **Which tank?** The trip tank if it is in service, otherwise the active
        pits. That choice is the whole of trip monitoring: a 35 bbl tank
        resolves a couple of barrels, and an 800 bbl active system does not.
        """
        hole_is_open = route.open_to_pits and not route.through_choke

        change = returns_bbl
        if hole_is_open:
            # Steel in displaces mud out; steel out leaves a void to fill.
            change += displacement_bbl
            # Mud going into the formation has to come from surface. Only
            # counted on the open path: closed in, losses move the pressure
            # balance instead and the choke flow already carries the effect.
            change -= lost_bbl

        if change == 0.0:
            return

        if hole_is_open and self.trip_tank_in_service:
            self.trip_tank.add(change)
        else:
            self.pits.add(change)

    def _advance_mud_system(self, dt: float) -> None:
        """Run the transfer pumps around the pit room, and settle the lamps.

        These move mud between vessels and never into or out of the well, so
        none of it is a volume balance -- but it is not cosmetic either. The
        reserve-to-active transfer raises the pit level without the well having
        done anything, which is precisely the false gain that teaches students
        to distrust the alarm.
        """
        tank, pits = self.trip_tank, self.pits

        # Active pits -> trip tank, to top it back up.
        wanted = min(tank.fill_pump.demand_bbl(dt), max(0.0, pits.active_bbl))
        if wanted > 0:
            pits.add(-tank.add(wanted))

        # Trip tank -> active pits, to empty it after a trip.
        wanted = min(tank.drain_pump.demand_bbl(dt), max(0.0, tank.volume_bbl))
        if wanted > 0:
            pits.add(-tank.add(-wanted))

        # Reserve -> active. Deliberately shows up as a gain; see PitSystem.
        pits.transfer_reserve_to_active(pits.transfer_pump.demand_bbl(dt))

        tank.update_alarms()
        pits.update_alarms()
        self.return_flow.update(
            self.state.flow_in_bpm, self.state.flow_out_bpm
        )

    def _reconcile_controlled_gauge(self, bottomhole_psi: float) -> None:
        """Re-reference the controlled gauge so bottomhole pressure is continuous.

        The volume balance drives one surface gauge and the other is read from
        it -- casing when circulating forward, drill pipe when reverse. When
        that changes hands, or when the influx moves from the annulus into the
        string, the *arithmetic* linking the two changes even though the well
        has not physically done anything sudden.

        Left alone that shows up as a step change in bottomhole pressure. It
        was a 4,900 psi spike the instant the influx entered the string, which
        promptly took 7 bbl of fresh influx -- an artefact of switching
        equations, not something the student did. So on those transitions the
        controlled gauge is recomputed from the bottomhole pressure that was
        already there, and the physics carries on from where it was.
        """
        friction = self._annular_friction_psi()
        if self.flow_path.is_reverse_circulating:
            self.state.standpipe_pressure_psi = max(
                0.0,
                bottomhole_psi - self._string_column().total_hydrostatic - friction,
            )
        else:
            self.state.casing_pressure_psi = max(
                0.0,
                bottomhole_psi - self._annulus_column().total_hydrostatic - friction,
            )

    def _advance_reverse(self, volume_bbl: float) -> None:
        """Move the well on by a volume pumped *down the annulus*.

        Two stages, in order. Anything under the influx has to come back out
        from under it first -- reverse circulating pushes the annulus contents
        down, so an influx that has climbed is driven back toward the bit. Only
        once it is at the bit does further pumping carry it into the string,
        and from there up the inside of the pipe.

        A student who has let the influx migrate a long way up therefore spends
        a lot of pumping getting it back down before reverse circulation starts
        achieving anything, which is a fair account of why the method wants an
        influx still on bottom.
        """
        if volume_bbl <= 0.0:
            return
        state = self.state

        still_below = max(
            0.0,
            max(0.0, state.pumped_bbl - state.injected_bbl)
            + state.migrated_bbl
            + state.lubricated_bbl
            - state.reverse_displaced_bbl,
        )
        pushed_down = min(volume_bbl, still_below)
        state.reverse_displaced_bbl += pushed_down
        remaining = volume_bbl - pushed_down

        if remaining <= 0.0:
            return
        state.influx_in_string = True
        state.reverse_pumped_bbl += remaining

    def _string_column(self) -> FluidColumn:
        """The inside of the drill pipe, bottom to top.

        Only interesting while reverse circulating, when the influx is in here
        rather than in the annulus. Positions are measured as volumes up from
        the bit, because that is the end the returns start from.
        """
        state = self.state
        kill_mw = self.kill_mud_weight_ppg
        original = self.well.mud_weight_ppg

        if not state.influx_in_string or state.influx_volume_bbl <= 0.0:
            # Ordinary case: whatever kill mud has been pumped, original mud
            # above it. This is the same column the forward model reads.
            front_md = self.well.kill_sheet.kill_mud_depth_at_volume(
                min(state.pumped_bbl, self.string_volume_bbl)
            )
            zones = []
            if front_md > 0:
                zones.append(Zone(0.0, front_md, kill_mw, "kill mud"))
            if front_md < self.well.bit_md_ft:
                zones.append(
                    Zone(front_md, self.well.bit_md_ft, original, "mud")
                )
            return FluidColumn(zones, self.well.survey)

        below = min(state.reverse_pumped_bbl, self.string_volume_bbl)
        volume = min(state.influx_volume_bbl, self.string_volume_bbl - below)
        influx_bottom = self.well.string_md_at_volume_from_bit(below)
        influx_top = self.well.string_md_at_volume_from_bit(below + volume)

        zones: list[Zone] = []
        if influx_bottom < self.well.bit_md_ft:
            zones.append(
                Zone(influx_bottom, self.well.bit_md_ft, original,
                     "mud behind influx")
            )
        if influx_top < influx_bottom:
            zones.append(
                Zone(
                    influx_top, influx_bottom,
                    expanded_gas_density(
                        self.influx_density_ppg,
                        self._initial_gas_volume_bbl or self.well.pit_gain_bbl,
                        state.influx_volume_bbl,
                    ),
                    "influx",
                )
            )
        if influx_top > 0:
            zones.append(Zone(0.0, influx_top, original, "original mud"))
        return FluidColumn(zones, self.well.survey)

    def _annulus_column(self) -> FluidColumn:
        """The annulus as it currently stands, bottom to top."""
        state = self.state
        kill_in_annulus = max(0.0, state.pumped_bbl - self.string_volume_bbl)
        kill_in_annulus = min(kill_in_annulus, self.annulus_volume_bbl)
        kill_top_md = self.well.annulus_md_at_volume_from_bottom(kill_in_annulus)

        zones: list[Zone] = []
        if kill_in_annulus > 0:
            zones.append(
                Zone(kill_top_md, self.hole_md, self.kill_mud_weight_ppg,
                     "kill mud")
            )
        if state.influx_bottom_md < kill_top_md:
            zones.append(
                Zone(state.influx_bottom_md, kill_top_md,
                     self.well.mud_weight_ppg, "mud behind influx")
            )
        if state.influx_volume_bbl > 0 and state.influx_top_md < state.influx_bottom_md:
            zones.append(
                Zone(state.influx_top_md, state.influx_bottom_md,
                     self._influx_density_ppg(), "influx")
            )
        if state.influx_top_md > 0:
            zones.append(
                Zone(0.0, state.influx_top_md, self.well.mud_weight_ppg,
                     "original mud")
            )
        return FluidColumn(zones, self.well.survey)

    # -- pressures -----------------------------------------------------------

    @property
    def _kill_rate_spm(self) -> float:
        return max(1.0, self.well.slow_circulating_rate.spm)

    @property
    def pumps(self) -> tuple[MudPump, MudPump]:
        return (self.pump, self.pump_2)

    @property
    def pumping(self) -> bool:
        """Whether any rig pump is turning."""
        return any(p.running for p in self.pumps)

    @property
    def total_pump_flow_bbl_per_min(self) -> float:
        return sum(p.flow_bbl_per_min for p in self.pumps)

    @property
    def total_pump_commanded_spm(self) -> float:
        return sum(p.commanded_spm for p in self.pumps)

    @property
    def total_pump_spm(self) -> float:
        """Combined rate, expressed as spm of pump 1.

        **Friction and strokes follow flow, not stroke count**, and the two
        pumps need not share a displacement per stroke. Converting the combined
        flow back to an equivalent rate on pump 1 keeps every relation in this
        module -- all of which are written in spm against the slow circulating
        rate -- correct without rewriting any of them. With one pump running it
        is exactly that pump's spm, so nothing about the single-pump case moves.
        """
        per_stroke = self.pump.pump.output_bbl_per_stroke
        if per_stroke <= 0:
            return sum(p.spm for p in self.pumps)
        return self.total_pump_flow_bbl_per_min / per_stroke

    @property
    def chokes(self) -> dict:
        """Every choke, keyed by the valve that puts it in service."""
        return {
            "choke_1": self.choke,
            "choke_2": self.choke_2,
            "manual_choke_1": self.manual_choke_1,
            "manual_choke_2": self.manual_choke_2,
        }

    @property
    def choke_in_service(self):
        """The choke the gauges and the trace should follow.

        The first one lined up, or choke 1 when nothing is. A student who has
        changed over to choke 2 because the first plugged should see the
        handle they are actually working, not the one they abandoned.
        """
        return next(iter(self.chokes_in_service), self.choke)

    @property
    def chokes_in_service(self) -> list:
        """The chokes actually on a live return route, in valve order."""
        return [
            self.chokes[v.name]
            for v in self.flow_path.active_chokes()
            if v.name in self.chokes
        ]

    def _choke_flow_bbl_per_min(
        self, pressure_drop_psi: float, density_ppg: float
    ) -> float:
        """What the manifold passes in total.

        **Chokes in parallel add.** Each open one sees the same pressure drop
        and passes its own flow, so leaving a second choke cracked while
        working the first lets past more than the handle in the driller's hand
        accounts for -- and the well goes underbalanced for reasons that are
        not on the gauge they are watching. That is a lineup error with a
        consequence, which is the only kind this simulator has.
        """
        return sum(
            choke.flow_bbl_per_min(pressure_drop_psi, density_ppg)
            for choke in self.chokes_in_service
        )

    def _annular_friction_psi(self) -> float:
        """Annular friction at the current pump speed.

        Taken as a fixed fraction of the measured slow circulating rate --
        annular loss is a small part of total system friction at kill rates --
        and scaled by the square of pump speed.
        """
        if not self.pumping:
            return 0.0
        ratio = (self.total_pump_spm / self._kill_rate_spm) ** 2
        return 0.15 * self.well.slow_circulating_rate.pressure_psi * ratio

    def _update_pressures(self) -> None:
        """Resolve the two surface gauges and bottomhole pressure.

        **Which gauge is the independent one depends on which way round the
        well is being circulated.** Forward, the choke is on the annulus, so
        casing pressure is what the volume balance sets and everything else is
        read from it. Reverse, the choke is on the drill pipe and the pump is
        on the annulus, so the roles swap: standpipe pressure is the controlled
        variable and casing pressure is derived.

        Both gauges keep meaning what they say in either case, which matters
        for the panel -- a student reading "casing" should be reading the
        annulus whichever way the mud is going.
        """
        state = self.state
        annulus = self._annulus_column()

        friction = 0.0
        if self.pumping:
            ratio = (self.total_pump_spm / self._kill_rate_spm) ** 2
            friction = self.well.slow_circulating_rate.pressure_psi * ratio

        if self.flow_path.is_reverse_circulating:
            # Returns come up the *string*, so that is the side carrying the
            # system friction -- the mirror of the forward case, where it sits
            # on the drill pipe because that is where the mud goes down.
            string = self._string_column()
            state.bottomhole_pressure_psi = (
                state.standpipe_pressure_psi
                + string.total_hydrostatic
                + friction
            )
            # Down the annulus now, so annular friction is a loss on the way
            # in and the pump reads higher by it, rather than lower. Small
            # either way -- the library carries system friction on the returns
            # side and treats the annulus as nearly frictionless.
            state.casing_pressure_psi = max(
                0.0,
                state.bottomhole_pressure_psi
                - annulus.total_hydrostatic
                + self._annular_friction_psi(),
            )
        else:
            state.bottomhole_pressure_psi = (
                state.casing_pressure_psi
                + annulus.total_hydrostatic
                + self._annular_friction_psi()
            )
            # Standpipe pressure reads back down the string from the bit.
            string_hydrostatic = self._string_column().total_hydrostatic
            state.standpipe_pressure_psi = max(
                0.0,
                state.bottomhole_pressure_psi - string_hydrostatic + friction,
            )

        state.shoe_pressure_psi = (
            annulus.pressure_at(self.shoe_md, state.casing_pressure_psi)
            if self.shoe_md is not None else 0.0
        )

    # -- capacitance ---------------------------------------------------------

    def _capacitance_bbl_per_psi(self) -> float:
        """How many barrels the closed system absorbs per psi.

        The single most important number in the model. Mud contributes almost
        nothing; free gas contributes ``V/P`` and dominates once there is any.
        """
        state = self.state
        gas = state.influx_volume_bbl
        mud = max(0.0, self.annulus_volume_bbl + self.string_volume_bbl - gas)

        capacitance = mud * MUD_COMPRESSIBILITY
        if gas > 0:
            column = self._annulus_column()
            midpoint = (state.influx_top_md + state.influx_bottom_md) / 2.0
            gas_pressure = column.pressure_at(midpoint, state.casing_pressure_psi)
            capacitance += gas / max(psi_to_psia(gas_pressure), 20.0)
        # Never zero: a perfectly incompressible closed system would divide by
        # zero and is not physical anyway.
        return max(capacitance, 1e-6)

    # -- the step ------------------------------------------------------------

    def step(self, dt: float = 0.05) -> SimState:
        """Advance the simulation by ``dt`` seconds."""
        state = self.state
        minutes = dt / 60.0

        self._apply_faults()
        for _pump in self.pumps:
            _pump.advance(dt)
        for _choke in self.chokes.values():
            _choke.advance(dt)
        self.accumulator.advance(dt)

        route = self.flow_path.return_route()
        deadheaded = self.flow_path.pump_is_deadheaded
        reverse = self.flow_path.is_reverse_circulating
        # Lubricating and reverse circulating both put mud into the annulus.
        # The difference is where the returns go, and it changes everything
        # about what the pumped volume does.
        lubricating = self.flow_path.injecting_into_the_annulus and not reverse

        # Drilling only makes hole while the pump is actually turning, so the
        # kick arrives after a stretch of circulating rather than after a
        # stretch of wall-clock time. A student who never starts the pump never
        # drills into anything.
        if not self.kick_taken and self.kick_after_s is not None:
            if self.pumping and not deadheaded:
                self._circulating_s += dt
                if self._circulating_s >= self.kick_after_s:
                    self.trigger_kick()

        # -- flow in ---------------------------------------------------------
        flow_in = 0.0 if deadheaded else self.total_pump_flow_bbl_per_min
        state.flow_in_bpm = flow_in
        state.set_condition(
            EventKind.PUMP_DEADHEADED,
            deadheaded and self.pumping,
            message="Pump running against a closed valve — deadheaded.",
            severity=Severity.WARNING,
            cleared_message="Injection path restored — the pump is delivering.",
        )

        # -- flow out --------------------------------------------------------
        returns_density = (
            self._influx_density_ppg() if state.gas_at_surface
            else self.well.mud_weight_ppg
        )
        if not route.open_to_pits:
            flow_out = 0.0
        elif not route.through_choke:
            # Flowline: the well is open to atmosphere. Whatever goes in comes
            # out and there is no pressure control at all.
            flow_out = flow_in + state.influx_rate_bpm
            state.casing_pressure_psi = 0.0
        elif self.flow_path.bypass_is_open():
            flow_out = flow_in + state.influx_rate_bpm
            state.casing_pressure_psi = max(0.0, state.casing_pressure_psi - 50.0 * dt)
        else:
            drop = state.casing_pressure_psi - DOWNSTREAM_PRESSURE
            flow_out = self._choke_flow_bbl_per_min(drop, returns_density)
        state.flow_out_bpm = flow_out

        # -- the formation's response ----------------------------------------
        # It works both ways. Underbalanced it gives up gas; pushed hard enough
        # it takes fluid back, which is the whole of bullheading. The threshold
        # between them is not zero -- see INJECTION_THRESHOLD_PSI.
        underbalance = self.formation_pressure_psi - state.bottomhole_pressure_psi
        influx_rate = self.productivity_index * underbalance if underbalance > 0 else 0.0
        state.influx_rate_bpm = influx_rate

        squeeze = -underbalance - INJECTION_THRESHOLD_PSI
        injection_rate = self.injection_index * squeeze if squeeze > 0 else 0.0
        state.injection_rate_bpm = injection_rate

        loss_rate = 0.0
        over = 0.0
        if self.frac_pressure_psi is not None:
            over = state.shoe_pressure_psi - self.frac_pressure_psi
            if over > 0:
                loss_rate = self.loss_index * over
        state.loss_rate_bpm = loss_rate
        state.set_level(
            EventKind.LOSSES,
            over,
            rises_above=5.0,
            falls_below=1.0,
            message=f"Shoe over-pressured by {over:.0f} psi — taking losses.",
            severity=Severity.CRITICAL,
            cleared_message="Shoe pressure back below fracture — losses stopped.",
            over_psi=over,
        )

        # -- the influx climbs under its own buoyancy ------------------------
        # Returns the expansion it wants at its new depth. Closed in, there is
        # nowhere to put that volume and it becomes pressure instead.
        migration_bbl = self._migrate_gas(dt)

        # -- pipe run into the hole ------------------------------------------
        # Steel going in is volume going in, and it lands in the same balance.
        displacement_bbl = self._advance_stripping(dt)

        # -- volume balance drives surface pressure --------------------------
        if route.open_to_pits and not route.through_choke:
            pass  # open hole to atmosphere; casing pressure pinned at zero
        else:
            net = (
                (
                    flow_in + influx_rate
                    - flow_out - loss_rate - injection_rate
                ) * minutes
                + migration_bbl
                + displacement_bbl
            )
            change = net / self._capacitance_bbl_per_psi()
            if reverse:
                # The choke is on the drill pipe, so that is the gauge the
                # balance moves; casing follows from it in _update_pressures.
                state.standpipe_pressure_psi = max(
                    0.0, state.standpipe_pressure_psi + change
                )
            else:
                state.casing_pressure_psi = max(
                    0.0, state.casing_pressure_psi + change
                )

        # -- advance the well ------------------------------------------------
        # Which end the mud goes in decides what it does. Down the string it
        # comes round the bit and pushes the annulus up from below. Into the
        # kill line it arrives on top of the gas and falls through it, ending
        # up beneath the cap either way -- so it adds to the volume under the
        # influx without displacing anything round the bit.
        if reverse:
            self._advance_reverse(flow_in * minutes)
        elif lubricating:
            state.lubricated_bbl += flow_in * minutes
        else:
            state.pumped_bbl += flow_in * minutes
        state.strokes += self.total_pump_spm * dt / 60.0
        if self.kill_mud_weight_ppg != self.well.mud_weight_ppg:
            state.kill_mud_pumped_bbl = min(state.pumped_bbl, self.string_volume_bbl)

        # New gas enters at bottomhole pressure and joins the inventory.
        if influx_rate > 0:
            self._gas_pv += psi_to_psia(state.bottomhole_pressure_psi) * (
                influx_rate * minutes
            )
            if self._initial_gas_pv <= 0.0:
                # First gas of a drilling start. Fix the reference state here:
                # it came in at formation pressure, where its density is the
                # influx density the well reports. Everything after this is
                # measured against it, and because the ratio works out to
                # P / P_reference it stays right however much more comes in.
                self._initial_gas_pv = self._gas_pv
                self._initial_gas_volume_bbl = self._gas_pv / psi_to_psia(
                    self.formation_pressure_psi
                )
        # Logged on a meaningful underbalance rather than on any at all: a
        # well held within a psi or two of balance drifts either side of it
        # constantly, and "taking an influx" is the wrong thing to tell a
        # student who is flying it correctly. The volume taken is still
        # integrated in full, however small the rate.
        state.set_level(
            EventKind.INFLUX_TAKEN,
            underbalance,
            rises_above=10.0,
            falls_below=2.0,
            message=f"Underbalanced by {underbalance:.0f} psi — taking an influx.",
            severity=Severity.CRITICAL,
            cleared_message="Back overbalanced — the influx has stopped.",
            underbalance_psi=underbalance,
        )

        # Fluid squeezed back into the zone takes the influx with it -- but
        # only while the influx is still *against* that zone. That is the
        # assumption bullheading is built on, and it stops being true the
        # moment the gas has risen away: pushing on a well whose gas is at
        # surface squeezes mud into the formation and leaves the gas exactly
        # where it was. Bullheading late is why the method has a reputation for
        # not working.
        if injection_rate > 0:
            pushed_back = injection_rate * minutes
            state.injected_bbl += pushed_back
            at_the_zone = (
                state.influx_bottom_md >= self.hole_md - GAS_AT_ZONE_TOLERANCE_FT
            )
            if at_the_zone and state.influx_volume_bbl > 1e-9:
                fraction = min(1.0, pushed_back / state.influx_volume_bbl)
                self._gas_pv *= 1.0 - fraction

        # Gas reaching surface leaves with the returns.
        if state.gas_at_surface and flow_out > 0 and route.through_choke:
            if state.influx_volume_bbl > 0:
                fraction = min(
                    1.0, (flow_out * minutes) / state.influx_volume_bbl
                )
                self._gas_pv *= 1.0 - fraction

        self._settle_gas()

        # -- making hole -----------------------------------------------------
        # Locked out of everything but drilling ahead; see drilling_ahead.
        self.drilling.advance(
            dt, on_bottom=self.state.bit_md_ft >= self.hole_md - 1.0
        )
        drilled_bbl = self._make_hole(dt) if self.drilling_ahead else 0.0

        # -- surface volume --------------------------------------------------
        self._settle_surface_volume(
            returns_bbl=(flow_out - flow_in) * minutes,
            displacement_bbl=displacement_bbl,
            lost_bbl=loss_rate * minutes + drilled_bbl,
            route=route,
        )
        self._advance_mud_system(dt)

        # -- what the run will be judged on ----------------------------------
        state.influx_taken_bbl += influx_rate * minutes
        state.lost_bbl += loss_rate * minutes

        # Bleeding is what comes out with nothing going in. With the pump
        # running this is circulating returns and means something else.
        if not self.pumping and route.through_choke:
            state.bled_bbl += flow_out * minutes

        state.time_s += dt

        # Only when the equations change hands, not every step -- doing it
        # every step would override the volume balance with its own answer.
        mode = (reverse, state.influx_in_string)
        if mode != self._mode:
            self._mode = mode
            self._reconcile_controlled_gauge(state.bottomhole_pressure_psi)

        self._update_pressures()
        self._check_dead()
        self._accumulate(dt, route)
        self._sample_if_due()
        return state

    # -- recording -----------------------------------------------------------

    def _accumulate(self, dt: float, route) -> None:
        """Running totals and conditions, evaluated once pressures are current.

        Time off target has to be integrated here rather than sampled off the
        trace: a student can swing 300 psi and recover between two samples, and
        that must not vanish from the debrief.
        """
        state = self.state

        excursion = abs(
            state.bottomhole_pressure_psi - self.formation_pressure_psi
        )
        # Only meaningful once there is a kick to hold. Drilling ahead is
        # deliberately overbalanced, so measuring that against pore pressure
        # would score correct practice as an excursion.
        killing = self.kick_taken
        if killing and excursion > self.bhp_band_psi:
            state.seconds_outside_band += dt
        state.max_bhp_excursion_psi = max(state.max_bhp_excursion_psi, excursion)
        state.peak_casing_pressure_psi = max(
            state.peak_casing_pressure_psi, state.casing_pressure_psi
        )
        state.peak_shoe_pressure_psi = max(
            state.peak_shoe_pressure_psi, state.shoe_pressure_psi
        )

        state.set_level(
            EventKind.BHP_EXCURSION,
            excursion if killing else 0.0,
            rises_above=self.bhp_band_psi,
            falls_below=0.6 * self.bhp_band_psi,
            message=(
                f"Bottomhole pressure {excursion:.0f} psi off formation "
                f"pressure."
            ),
            severity=Severity.CAUTION,
            cleared_message="Bottomhole pressure back on target.",
        )

        # Circulating with the stack open is the lesson students most need: the
        # choke does nothing and the well is not under control at all.
        state.set_condition(
            EventKind.WELL_OPEN_TO_FLOWLINE,
            self.pumping and route.open_to_pits and not route.through_choke,
            message=(
                "Circulating with the BOP open — returns are going up the "
                "flowline and the choke has no effect."
            ),
            severity=Severity.CRITICAL,
        )
        state.set_condition(
            EventKind.NO_RETURNS,
            self.pumping and not route.open_to_pits,
            message="Pumping into a closed system — no returns route.",
            severity=Severity.WARNING,
            cleared_message="Returns route open.",
        )

        # -- the two things a driller watches to catch a kick ----------------
        # Flow out exceeding flow in comes first and is the more sensitive of
        # the two; the pit gain follows once enough has come in to see. Both
        # are consequences of the volume balance, so a student who is watching
        # gets the same warning a real one does, at the same point.
        state.set_level(
            EventKind.FLOW_CHECK,
            state.flow_out_bpm - state.flow_in_bpm,
            rises_above=0.4,
            falls_below=0.1,
            message="Flow out is greater than flow in — the well is flowing.",
            severity=Severity.CRITICAL,
            cleared_message="Flow out back in balance with flow in.",
        )
        state.set_level(
            EventKind.PIT_GAIN,
            self.pits.gain_bbl,
            rises_above=5.0,
            falls_below=2.0,
            message=(
                f"Pit gain {self.pits.gain_bbl:.1f} bbl and rising."
            ),
            severity=Severity.CRITICAL,
            cleared_message="Pit level back to baseline.",
        )

        # The moment of truth on a drilling start. Two events, not one: the
        # well being closed in, and -- later -- the pressures having settled.
        # Quoting the gauges at the instant of shut-in would report numbers
        # still climbing through the influx, which is exactly the mistake the
        # message is warning against.
        closed_in = (
            self.flow_path.bop_is_closed
            and not self.pumping
            and state.influx_volume_bbl > 0
        )
        if closed_in:
            state.record(
                EventKind.WELL_SHUT_IN_BY_STUDENT,
                "Well shut in. Let the pressures stabilise before reading them.",
                severity=Severity.INFO,
                once=True,
            )
        # "Stable" means the influx condition has cleared, not that the rate is
        # exactly zero: as the well comes back into balance the underbalance
        # hovers a psi or two above nothing, so a literal zero never arrives.
        # Reusing the hysteresis keeps the log and the gauges telling the same
        # story.
        if (
            closed_in
            and not state.event_log.is_active(EventKind.INFLUX_TAKEN)
            and state.event_log.has(EventKind.WELL_SHUT_IN_BY_STUDENT)
        ):
            if state.measured_sidpp_psi is None:
                # Latch what the gauges read at the moment they settle. The
                # kill sheet is built from these, not from whatever the
                # scenario was designed around.
                state.measured_sidpp_psi = state.standpipe_pressure_psi
                state.measured_sicp_psi = state.casing_pressure_psi
                state.measured_pit_gain_bbl = max(0.0, self.pits.gain_bbl)
            state.record(
                EventKind.SHUT_IN,
                f"Pressures stable. SIDPP {state.standpipe_pressure_psi:.0f} psi, "
                f"SICP {state.casing_pressure_psi:.0f} psi, "
                f"pit gain {self.pits.gain_bbl:.1f} bbl. "
                "Build the kill sheet from these.",
                severity=Severity.INFO,
                once=True,
            )

        maasp = self.maasp_psi
        if maasp is not None:
            state.set_level(
                EventKind.MAASP_EXCEEDED,
                state.casing_pressure_psi,
                rises_above=maasp,
                falls_below=maasp - 25.0,
                message=(
                    f"Casing pressure {state.casing_pressure_psi:.0f} psi is "
                    f"above MAASP ({maasp:.0f} psi)."
                ),
                severity=Severity.WARNING,
                cleared_message="Casing pressure back below MAASP.",
            )

        # Shut in, not circulating, and the pressure climbing anyway: the well
        # is telling the student the gas is migrating and that doing nothing is
        # not an option. This is the cue the Volumetric method answers.
        state.set_condition(
            EventKind.GAS_MIGRATING,
            (
                self.flow_path.bop_is_closed
                and not self.pumping
                and state.influx_volume_bbl > 0
                and self.migration_rate_ft_per_hr > 0
                and not state.gas_at_surface
            ),
            message=(
                "Shut in and the gas is migrating — surface pressure will keep "
                "climbing on its own. Bleed in measured steps to hold "
                "bottomhole pressure, or the shoe takes it."
            ),
            severity=Severity.CAUTION,
            cleared_message="Migration no longer driving the pressure.",
        )

        if state.gas_at_surface:
            state.record(
                EventKind.GAS_AT_SURFACE,
                "Gas at surface — expect the choke to go violent.",
                severity=Severity.CAUTION,
                once=True,
            )
        if (
            self.kill_mud_weight_ppg != self.well.mud_weight_ppg
            and state.pumped_bbl >= self.string_volume_bbl
        ):
            state.record(
                EventKind.KILL_MUD_AT_BIT,
                "Kill mud at the bit — hold final circulating pressure from here.",
                severity=Severity.INFO,
                once=True,
            )

        state.set_condition(
            EventKind.PUMP_STARTED,
            self.pumping,
            # The commanded speed, not the instantaneous one: the pump ramps,
            # so at the moment it starts turning it is still doing 0 spm and
            # logging that reads as a fault rather than an action.
            message=f"Pump running up to {self.total_pump_commanded_spm:.0f} spm.",
            severity=Severity.INFO,
            cleared_message="Pump stopped.",
        )

    def _record_sample(self) -> None:
        state = self.state
        state.trace.append(
            TraceSample(
                time_s=state.time_s,
                strokes=state.strokes,
                pumped_bbl=state.pumped_bbl,
                bottomhole_psi=state.bottomhole_pressure_psi,
                casing_psi=state.casing_pressure_psi,
                standpipe_psi=state.standpipe_pressure_psi,
                shoe_psi=state.shoe_pressure_psi,
                influx_bbl=state.influx_volume_bbl,
                influx_top_md=state.influx_top_md,
                choke_percent=self.choke_in_service.position,
                pump_spm=self.total_pump_spm,
                flow_in_bpm=state.flow_in_bpm,
                flow_out_bpm=state.flow_out_bpm,
                pit_gain_bbl=self.pits.gain_bbl,
                trip_tank_bbl=self.trip_tank.change_bbl,
            )
        )

    def _sample_if_due(self) -> None:
        if self.state.time_s + 1e-9 >= self._next_sample_s + self.trace_interval_s:
            self._next_sample_s = self.state.time_s
            self._record_sample()

    def assess(self, standard: Standard | None = None) -> Assessment:
        """Grade the run so far. See :mod:`wellcontrol.sim.assessment`."""
        from .assessment import assess

        return assess(self, standard)

    @property
    def maasp_psi(self) -> float | None:
        """Maximum allowable annular surface pressure for the mud in the hole.

        Reported in the debrief because it is the number the student was
        watching. The hard limit is the shoe pressure the engine computes
        directly.
        """
        try:
            return self.well.maasp_psi()
        except (TypeError, ValueError):
            return None

    def _gas_pressure_psia(self) -> float:
        """Pressure at the middle of the gas, absolute.

        Read from whichever side of the well the gas is actually in. Reverse
        circulating puts it inside the string, where the surface pressure above
        it is the drill pipe gauge rather than the casing gauge.
        """
        state = self.state
        if state.influx_in_string:
            column = self._string_column()
            midpoint = (
                state.influx_string_top_md + state.influx_string_bottom_md
            ) / 2.0
            surface = state.standpipe_pressure_psi
        else:
            column = self._annulus_column()
            midpoint = (state.influx_top_md + state.influx_bottom_md) / 2.0
            surface = state.casing_pressure_psi
        return max(psi_to_psia(column.pressure_at(midpoint, surface)), 20.0)

    def _settle_gas(self) -> None:
        """Resolve gas volume and position together.

        The inventory ``self._gas_pv`` is the conserved quantity -- pressure
        times volume, which stands in for moles. Volume follows from it at
        whatever pressure the gas currently sits at.

        Volume and pressure are mutually dependent: a taller gas column changes
        the hydrostatic above it, which changes its pressure, which changes its
        volume. Two passes settle it to well inside the resolution of anything
        the student can read off a gauge.
        """
        state = self.state
        if self._gas_pv <= 0.0:
            state.influx_volume_bbl = 0.0
            self._place_influx()
            return

        for _ in range(2):
            state.influx_volume_bbl = self._gas_pv / self._gas_pressure_psia()
            self._place_influx()

    def _check_dead(self) -> None:
        state = self.state
        if state.well_is_dead:
            return
        killed = (
            state.influx_volume_bbl < 0.5
            and state.casing_pressure_psi < 25.0
            and state.bottomhole_pressure_psi >= self.formation_pressure_psi
        )
        if killed and state.pumped_bbl > self.string_volume_bbl:
            state.well_is_dead = True
            state.record(
                EventKind.WELL_DEAD,
                "Well is dead — no influx, no surface pressure.",
                severity=Severity.INFO,
                once=True,
            )

    # -- convenience ---------------------------------------------------------

    def run(self, seconds: float, dt: float = 0.05) -> SimState:
        """Advance by a stretch of time, holding the current settings."""
        steps = max(1, int(round(seconds / dt)))
        for _ in range(steps):
            self.step(dt)
        return self.state

    def readout(self) -> dict[str, object]:
        """What the gauges say, for a panel or a debrief."""
        state = self.state
        return {
            "Time (min)": round(state.time_s / 60.0, 1),
            "Pump (spm)": round(self.total_pump_spm, 1),
            "Choke (%)": round(self.choke_in_service.position, 1),
            "Standpipe (psi)": round(state.standpipe_pressure_psi),
            "Casing (psi)": round(state.casing_pressure_psi),
            "Bottomhole (psi)": round(state.bottomhole_pressure_psi),
            "Formation (psi)": round(self.formation_pressure_psi),
            "Overbalance (psi)": round(
                state.bottomhole_pressure_psi - self.formation_pressure_psi
            ),
            "Shoe (psi)": round(state.shoe_pressure_psi),
            "Strokes": round(state.strokes),
            "Influx (bbl)": round(state.influx_volume_bbl, 1),
            "Influx top (ft MD)": round(state.influx_top_md),
            "Flow in (bpm)": round(state.flow_in_bpm, 2),
            "Flow out (bpm)": round(state.flow_out_bpm, 2),
            "Pit gain (bbl)": round(self.pits.gain_bbl, 1),
            "Returns": self.flow_path.return_route().description,
        }
