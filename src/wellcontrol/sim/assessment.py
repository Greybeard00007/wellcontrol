"""Event log, recorded trace, and scoring.

Stage 2 of the simulator. The engine says what the well does; this says what
the student did about it, and whether it was any good.

## Scoring is on outcome and margin, never on procedure

A student who holds bottomhole pressure by a slightly unusual route has done
the job. One who follows the textbook order and still fractures the shoe has
not. So nothing here checks the *order* of actions, and nothing checks the
lineup: a wrong lineup is not marked incorrect, it simply does not work, and
the consequence -- no returns, a deadheaded pump, an uncontrolled well -- is
what shows up in the metrics. That is the same standard the rig applies.

## The shoe, not MAASP

MAASP is a surface proxy for "the shoe is about to break", needed on a real rig
because nobody can read a gauge at 7,000 ft. The simulator computes shoe
pressure directly from the fluid column, so the hard limit here is the actual
shoe pressure against the actual fracture pressure. Peak casing pressure
against MAASP is still reported, because it is what the student was watching,
but it is informational -- a student can exceed MAASP briefly with light mud in
the annulus without hurting the shoe, and the physical limit is the one that
matters.

## Two integrals the engine has to own

Time outside the bottomhole pressure band, and volume of extra influx, are time
integrals. Only the engine sees every timestep, so it accumulates them; sampling
them back off the trace would miss short excursions between samples. Everything
else here is computed from the recorded trace and the event log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from .engine import Simulator


def clock(time_s: float) -> str:
    """``HH:MM:SS`` for a simulated time in seconds."""
    minutes, seconds = divmod(int(time_s), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

class Severity(Enum):
    """How much a student should care.

    ``CRITICAL`` is reserved for things that damage the well or lose control of
    it, so that a debrief can lead with them.
    """

    INFO = "info"
    CAUTION = "caution"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)

    @property
    def label(self) -> str:
        return self.value.capitalize()


class EventKind(Enum):
    """What happened.

    A closed set, so a debrief can group and count events without parsing
    prose -- which is what the engine used to do, matching substrings against
    the last few log lines to avoid repeating itself.
    """

    SHUT_IN = "shut_in"
    DRILLING = "drilling"
    KICK_ZONE_DRILLED = "kick_zone_drilled"
    PIT_GAIN = "pit_gain"
    FLOW_CHECK = "flow_check"
    WELL_SHUT_IN_BY_STUDENT = "well_shut_in_by_student"
    PUMP_STARTED = "pump_started"
    PUMP_STOPPED = "pump_stopped"
    PUMP_DEADHEADED = "pump_deadheaded"
    NO_RETURNS = "no_returns"
    WELL_OPEN_TO_FLOWLINE = "well_open_to_flowline"
    INFLUX_TAKEN = "influx_taken"
    LOSSES = "losses"
    SHOE_FRACTURED = "shoe_fractured"
    MAASP_EXCEEDED = "maasp_exceeded"
    BHP_EXCURSION = "bhp_excursion"
    GAS_MIGRATING = "gas_migrating"
    GAS_AT_SURFACE = "gas_at_surface"
    KILL_MUD_AT_BIT = "kill_mud_at_bit"
    WELL_DEAD = "well_dead"
    EQUIPMENT_FAILURE = "equipment_failure"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()


@dataclass(frozen=True)
class Event:
    """One thing worth telling the student about, with when it happened."""

    time_s: float
    kind: EventKind
    severity: Severity
    message: str
    data: dict = field(default_factory=dict)

    @property
    def clock(self) -> str:
        return clock(self.time_s)

    def __str__(self) -> str:
        return f"[{self.clock}] {self.message}"


class EventLog:
    """An ordered log with edge detection.

    Most of what is worth logging is a *condition* -- taking losses, taking an
    influx, deadheaded -- which stays true for many thousands of timesteps. The
    log should carry one entry when it starts and, where it is useful, one when
    it stops. :meth:`set_condition` does that by latching per kind, which
    replaces the substring matching the engine used to do.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._latched: set[EventKind] = set()

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def record(
        self,
        kind: EventKind,
        message: str,
        *,
        time_s: float,
        severity: Severity = Severity.INFO,
        once: bool = False,
        **data,
    ) -> Event | None:
        """Add an entry. Returns it, or None if suppressed by ``once``."""
        if once and self.has(kind):
            return None
        event = Event(time_s, kind, severity, message, dict(data))
        self.events.append(event)
        return event

    def set_condition(
        self,
        kind: EventKind,
        active: bool,
        *,
        time_s: float,
        message: str = "",
        severity: Severity = Severity.WARNING,
        cleared_message: str = "",
        **data,
    ) -> Event | None:
        """Log the edges of a condition rather than every step it is true.

        Records ``message`` when the condition becomes true, and
        ``cleared_message`` -- if given -- when it stops. Between those, nothing.
        """
        latched = kind in self._latched
        if active and not latched:
            self._latched.add(kind)
            return self.record(
                kind, message, time_s=time_s, severity=severity, **data
            )
        if not active and latched:
            self._latched.discard(kind)
            if cleared_message:
                return self.record(
                    kind, cleared_message, time_s=time_s, severity=Severity.INFO,
                    **data,
                )
        return None

    def set_level(
        self,
        kind: EventKind,
        value: float,
        *,
        rises_above: float,
        falls_below: float,
        time_s: float,
        message: str = "",
        severity: Severity = Severity.WARNING,
        cleared_message: str = "",
        **data,
    ) -> Event | None:
        """A condition on a measured level, with hysteresis.

        Becomes true when ``value`` rises above ``rises_above`` and stays true
        until it falls below ``falls_below``. Without the gap between those two
        a condition sitting on its threshold logs on every crossing: a student
        holding bottomhole pressure exactly 50 psi off target produced over a
        hundred entries in a sixteen-minute run, which is a log nobody reads.

        The metrics do not use this -- time off target is integrated against the
        true band. This is for the readability of the log alone.
        """
        if falls_below > rises_above:
            raise ValueError(
                "falls_below must not be above rises_above, or the condition "
                "can never clear"
            )
        if kind in self._latched:
            active = value >= falls_below
        else:
            active = value > rises_above
        return self.set_condition(
            kind, active, time_s=time_s, message=message, severity=severity,
            cleared_message=cleared_message, **data,
        )

    def is_active(self, kind: EventKind) -> bool:
        return kind in self._latched

    def has(self, kind: EventKind) -> bool:
        return any(e.kind is kind for e in self.events)

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind is kind]

    def first(self, kind: EventKind) -> Event | None:
        for event in self.events:
            if event.kind is kind:
                return event
        return None

    def at_least(self, severity: Severity) -> list[Event]:
        return [e for e in self.events if e.severity.rank >= severity.rank]

    @property
    def worst_severity(self) -> Severity:
        return max(
            (e.severity for e in self.events),
            key=lambda s: s.rank,
            default=Severity.INFO,
        )

    @property
    def messages(self) -> list[str]:
        """The log as printable lines, which is what a debrief shows."""
        return [str(e) for e in self.events]


# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TraceSample:
    """One sample of everything a debrief plot needs.

    Sampled on an interval rather than every timestep: a three-hour kill at
    ``dt=0.1 s`` is over a hundred thousand steps, and nothing in a debrief
    needs that resolution.
    """

    time_s: float
    strokes: float
    pumped_bbl: float
    bottomhole_psi: float
    casing_psi: float
    standpipe_psi: float
    shoe_psi: float
    influx_bbl: float
    influx_top_md: float
    choke_percent: float
    pump_spm: float
    flow_in_bpm: float
    flow_out_bpm: float
    pit_gain_bbl: float
    #: Trip tank level against its mark. Zero unless a trip is being
    #: monitored, and the only trace that shows a kick taken on one.
    trip_tank_bbl: float = 0.0


# --------------------------------------------------------------------------
# Standard and grading
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Standard:
    """The thresholds a performance is judged against.

    Defaults are deliberately achievable by a competent student rather than
    perfect: holding bottomhole pressure to within 50 psi for 90% of a kill is
    a good kill, not a flawless one. An instructor can tighten them.
    """

    #: Bottomhole pressure is "on target" within this much of formation pressure.
    bhp_band_psi: float = 50.0
    #: A single excursion larger than this fails, however brief.
    max_bhp_excursion_psi: float = 200.0
    #: Fraction of the run allowed outside the band.
    max_time_outside_band: float = 0.10
    #: Extra influx that counts as having lost control of the well.
    max_additional_influx_bbl: float = 1.0
    #: Losses that count as having damaged the formation.
    max_losses_bbl: float = 1.0
    #: Tighter figures a performance must beat to be called excellent.
    excellent_time_outside_band: float = 0.02
    excellent_bhp_excursion_psi: float = 75.0
    #: Whether the exercise was meant to end with a dead well. A Driller's
    #: *first* circulation is flown correctly and still leaves the well live,
    #: so an exercise covering only that sets this False. When the scenario
    #: library lands (stage 5) this comes from the scenario's objective.
    require_kill: bool = True


class Grade(Enum):
    EXCELLENT = "excellent"
    PASS = "pass"
    MARGINAL = "marginal"
    FAIL = "fail"

    @property
    def label(self) -> str:
        return self.value.capitalize()

    @property
    def passed(self) -> bool:
        return self in (Grade.EXCELLENT, Grade.PASS)


@dataclass(frozen=True)
class Metric:
    """One measured quantity against the limit it had to meet."""

    name: str
    value: float
    limit: float | None
    unit: str
    passed: bool
    critical: bool = False
    detail: str = ""

    def __str__(self) -> str:
        mark = "OK" if self.passed else "FAIL"
        limit = "" if self.limit is None else f" (limit {self.limit:,.1f})"
        return f"{mark:>4}  {self.name}: {self.value:,.1f} {self.unit}{limit}"


@dataclass(frozen=True)
class Assessment:
    """The verdict on a run, computed from the trace rather than a script."""

    grade: Grade
    killed: bool
    duration_min: float
    metrics: list[Metric]
    events: list[Event]
    standard: Standard
    summary: str

    @property
    def passed(self) -> bool:
        return self.grade.passed

    @property
    def failures(self) -> list[Metric]:
        return [m for m in self.metrics if not m.passed]

    @property
    def critical_failures(self) -> list[Metric]:
        return [m for m in self.metrics if not m.passed and m.critical]

    def format_report(self) -> str:
        """A plain-text debrief, for a console or a report pane."""
        if self.killed:
            outcome = "yes"
        elif self.standard.require_kill:
            outcome = "no"
        else:
            outcome = "no — this exercise did not require it"
        lines = [
            f"VERDICT: {self.grade.label.upper()}",
            self.summary,
            "",
            f"Well killed: {outcome}",
            f"Time taken:  {self.duration_min:.0f} min",
            "",
            "MEASURES",
        ]
        lines.extend(f"  {m}" for m in self.metrics)
        notable = [
            e for e in self.events if e.severity.rank >= Severity.CAUTION.rank
        ]
        if notable:
            lines.extend(["", "EVENTS"])
            lines.extend(f"  {e}" for e in notable)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def assess(sim: "Simulator", standard: Standard | None = None) -> Assessment:
    """Grade a completed run.

    Args:
        sim: The simulator, after the run. Its recorded totals and event log
            are read; nothing is modified.
        standard: Thresholds to judge against. Defaults to :class:`Standard`.
    """
    standard = standard or Standard()
    state = sim.state
    metrics: list[Metric] = []

    elapsed = max(state.time_s, 1e-9)
    outside_fraction = state.seconds_outside_band / elapsed

    # -- outcome ------------------------------------------------------------
    if standard.require_kill:
        metrics.append(
            Metric(
                name="Well killed",
                value=1.0 if state.well_is_dead else 0.0,
                limit=None,
                unit="",
                passed=state.well_is_dead,
                critical=True,
                detail=(
                    "No influx left and no surface pressure."
                    if state.well_is_dead
                    else "The well was still live when the run ended."
                ),
            )
        )

    # -- the shoe, which is the thing that actually breaks -------------------
    if sim.frac_pressure_psi is not None:
        margin = sim.frac_pressure_psi - state.peak_shoe_pressure_psi
        metrics.append(
            Metric(
                name="Peak shoe pressure",
                value=state.peak_shoe_pressure_psi,
                limit=sim.frac_pressure_psi,
                unit="psi",
                passed=margin >= 0.0,
                critical=True,
                detail=f"{margin:,.0f} psi of margin at the shoe.",
            )
        )

    # -- control quality ----------------------------------------------------
    metrics.append(
        Metric(
            name="Worst bottomhole excursion",
            value=state.max_bhp_excursion_psi,
            limit=standard.max_bhp_excursion_psi,
            unit="psi",
            passed=state.max_bhp_excursion_psi <= standard.max_bhp_excursion_psi,
            detail="Largest departure from formation pressure at any moment.",
        )
    )
    metrics.append(
        Metric(
            name="Time off target",
            value=100.0 * outside_fraction,
            limit=100.0 * standard.max_time_outside_band,
            unit="%",
            passed=outside_fraction <= standard.max_time_outside_band,
            detail=(
                f"Share of the run with bottomhole pressure more than "
                f"{standard.bhp_band_psi:.0f} psi off formation pressure."
            ),
        )
    )

    # -- what the well was made to do ---------------------------------------
    metrics.append(
        Metric(
            name="Additional influx taken",
            value=state.influx_taken_bbl,
            limit=standard.max_additional_influx_bbl,
            unit="bbl",
            passed=state.influx_taken_bbl <= standard.max_additional_influx_bbl,
            critical=True,
            detail="Formation fluid that entered after the run started.",
        )
    )
    metrics.append(
        Metric(
            name="Mud lost to the formation",
            value=state.lost_bbl,
            limit=standard.max_losses_bbl,
            unit="bbl",
            passed=state.lost_bbl <= standard.max_losses_bbl,
            critical=True,
            detail="Pumped away into the shoe rather than around the well.",
        )
    )

    # -- informational: what the student was watching -----------------------
    maasp = sim.maasp_psi
    if maasp is not None:
        metrics.append(
            Metric(
                name="Peak casing pressure",
                value=state.peak_casing_pressure_psi,
                limit=maasp,
                unit="psi",
                passed=state.peak_casing_pressure_psi <= maasp,
                detail=(
                    "Against MAASP. The surface proxy -- peak shoe pressure "
                    "above is the physical limit."
                ),
            )
        )

    grade, summary = _grade(state, metrics, standard, outside_fraction)
    return Assessment(
        grade=grade,
        killed=state.well_is_dead,
        duration_min=state.time_s / 60.0,
        metrics=metrics,
        events=list(state.event_log.events),
        standard=standard,
        summary=summary,
    )


def _grade(state, metrics, standard: Standard, outside_fraction: float):
    """Turn the metrics into a verdict and a sentence explaining it."""
    critical = [m for m in metrics if not m.passed and m.critical]
    if critical:
        if [m.name for m in critical] == ["Well killed"]:
            return (
                Grade.FAIL,
                "The well was not killed. Nothing else counts until it is.",
            )
        named = ", ".join(
            m.name.lower() for m in critical if m.name != "Well killed"
        )
        return Grade.FAIL, f"Lost control of the well: {named}."

    soft = [m for m in metrics if not m.passed]
    if soft:
        named = ", ".join(m.name.lower() for m in soft)
        return (
            Grade.MARGINAL,
            f"The well was killed safely, but control was loose: {named}.",
        )

    if (
        outside_fraction <= standard.excellent_time_outside_band
        and state.max_bhp_excursion_psi <= standard.excellent_bhp_excursion_psi
    ):
        return (
            Grade.EXCELLENT,
            "Bottomhole pressure held on the formation throughout. "
            "No extra influx, no losses, shoe intact.",
        )
    return (
        Grade.PASS,
        "The well was killed with bottomhole pressure under control "
        "and the shoe intact.",
    )
