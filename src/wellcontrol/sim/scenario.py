"""Scenarios, and the faults an instructor can inject into them.

Stage 5. Until now a session was "the reference vertical well, shut in or
drilling". A scenario names an exercise: which well, where it starts, what the
student is expected to achieve, and what -- if anything -- is going to go wrong
part way through.

**Scenarios are data.** Everything here is a plain dataclass built from
primitives, and :meth:`Scenario.from_dict` takes the same shape as JSON, so an
instructor can add an exercise without touching Python. The built-in library in
:data:`SCENARIOS` is the same structure, written out longhand.

## Faults are triggers, not scripts

A fault does one thing to one piece of equipment at one moment: the pump fails,
the choke plugs, the accumulator leaks. What follows is whatever the well does
about it, through the same volume balance as everything else. Nothing here
knows what a kick is or how a kill should go, and a fault cannot make the well
behave in a way it could not have behaved on its own.

That distinction is worth keeping. A scripted "and now the pressure spikes"
teaches a student to recognise a scripted spike. A choke that plugs while they
are holding 400 psi on it teaches them what a plugged choke feels like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .assessment import EventKind, Severity, Standard
from .engine import StartState


class FaultKind(str, Enum):
    """What can be made to go wrong.

    Each acts on one piece of equipment. The consequences are the model's.
    """

    PUMP_FAILURE = "pump_failure"
    CHOKE_PLUGGED = "choke_plugged"
    CHOKE_WASHOUT = "choke_washout"
    ACCUMULATOR_LEAK = "accumulator_leak"
    WEAK_SHOE = "weak_shoe"
    #: The control pod in use stops answering. Nothing on the stack will
    #: shift until the other pod is selected, which is the whole drill --
    #: and it does nothing at all on a surface stack, which has no pods.
    POD_FAILURE = "pod_failure"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()


@dataclass(frozen=True)
class Fault:
    """One thing going wrong, at one moment.

    Args:
        kind: What breaks.
        at_seconds: Simulated time to fire at.
        at_strokes: Stroke count to fire at. Used instead of, or as well as,
            the time -- whichever comes first.
        after_event: Fire once this has appeared in the log. Lets a fault be
            hung on something the *student* caused, such as gas reaching
            surface, rather than on the clock.
        message: What the log says. Written as the rig would report it, not as
            an explanation -- the student has to work out what it means.
        severity: How it is logged.
    """

    kind: FaultKind
    at_seconds: float | None = None
    at_strokes: float | None = None
    after_event: EventKind | None = None
    message: str = ""
    severity: Severity = Severity.CRITICAL

    def is_due(self, time_s: float, strokes: float, log) -> bool:
        """Whether this should fire now. Any one trigger is enough."""
        if self.at_seconds is not None and time_s >= self.at_seconds:
            return True
        if self.at_strokes is not None and strokes >= self.at_strokes:
            return True
        if self.after_event is not None and log.has(self.after_event):
            return True
        return False

    @classmethod
    def from_dict(cls, data: dict) -> "Fault":
        return cls(
            kind=FaultKind(data["kind"]),
            at_seconds=data.get("at_seconds"),
            at_strokes=data.get("at_strokes"),
            after_event=(
                EventKind(data["after_event"]) if data.get("after_event") else None
            ),
            message=data.get("message", ""),
            severity=Severity(data.get("severity", "critical")),
        )


@dataclass(frozen=True)
class Scenario:
    """One exercise.

    Args:
        key: Stable identifier, used in URLs.
        name: What the student sees in the picker.
        objective: What they are being asked to do, in a sentence.
        briefing: The rest of what they are told before starting. Deliberately
            does not include anything they should be reading off the gauges.
        bop: ``surface`` or ``subsea``.
        trajectory: ``vertical``, ``deviated`` or ``horizontal``.
        start: Where it begins -- see :class:`~wellcontrol.sim.engine.StartState`.
        bit_off_bottom_ft: Pull the bit up, for stripping exercises.
        open_valves: Valves to open beyond the start state's own lineup.
        closed_valves: Valves to shut.
        faults: What is going to go wrong, if anything.
        require_kill: Whether the exercise is only complete with a dead well.
        migration_rate_ft_per_hr: Override the well's own figure.
    """

    key: str
    name: str
    objective: str
    briefing: str = ""
    bop: str = "surface"
    trajectory: str = "vertical"
    start: StartState = StartState.SHUT_IN
    bit_off_bottom_ft: float = 0.0
    open_valves: tuple[str, ...] = ()
    closed_valves: tuple[str, ...] = ()
    faults: tuple[Fault, ...] = ()
    require_kill: bool = False
    migration_rate_ft_per_hr: float | None = None

    @property
    def standard(self) -> Standard:
        return Standard(require_kill=self.require_kill)

    def summary(self) -> dict:
        """What the picker and the briefing panel show."""
        return {
            "key": self.key,
            "name": self.name,
            "objective": self.objective,
            "briefing": self.briefing,
            "bop": self.bop,
            "trajectory": self.trajectory,
            "start": self.start.value,
            "require_kill": self.require_kill,
            # Named, but not when or what they do. A student who knows the
            # choke is going to plug at stroke 2,000 is not being tested on
            # noticing that the choke has plugged.
            "has_faults": bool(self.faults),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scenario":
        """Build from the same shape as JSON, so exercises can live in a file."""
        return cls(
            key=data["key"],
            name=data["name"],
            objective=data["objective"],
            briefing=data.get("briefing", ""),
            bop=data.get("bop", "surface"),
            trajectory=data.get("trajectory", "vertical"),
            start=StartState(data.get("start", "shut_in")),
            bit_off_bottom_ft=float(data.get("bit_off_bottom_ft", 0.0)),
            open_valves=tuple(data.get("open_valves", ())),
            closed_valves=tuple(data.get("closed_valves", ())),
            faults=tuple(Fault.from_dict(f) for f in data.get("faults", ())),
            require_kill=bool(data.get("require_kill", False)),
            migration_rate_ft_per_hr=data.get("migration_rate_ft_per_hr"),
        )


# --------------------------------------------------------------------------
# The built-in library
# --------------------------------------------------------------------------

#: Lineups the scenarios build on, named so the intent is readable.
#: ``kill_line`` and ``choke_line`` name a *side* rather than a valve, because
#: how many valves that side has depends on the stack -- two HCRs on a surface
#: flow cross, two fail-safes per outlet subsea. A scenario file should be able
#: to say "open the kill line" without knowing which rig it will run on.
#: Reverse circulation feeds the kill line from the SP manifold's east side
#: (mud pump 2 through the lower run) and takes the string returns down to
#: the choke manifold's lower run and up through the choke line entry to
#: remote choke 1. The *closed* valves matter as much: the lower west and
#: header east section valves are what keep the kill line feed and the
#: string returns in separate halves of the manifold -- which is exactly
#: what section valves are for.
_REVERSE = (
    "pump_to_kill_line", "kill_line",
    "float_valve", "standpipe_valve", "standpipe_to_manifold", "choke_1",
    "pump_2_discharge", "lower_east_valve",
    "ck_lower_west", "choke_line_to_manifold",
)
_REVERSE_CLOSED = (
    "pump_discharge", "choke_line", "lower_west_valve", "header_east_valve",
)
#: Lubricating feeds the kill line from the SP manifold's lower run, so the
#: standpipe valve is what gets closed -- mud must not also have the string
#: to fall down. The pump discharge stays open; it is how the mud reaches
#: the lower run at all.
_LUBRICATE = ("pump_to_kill_line", "kill_line")
_LUBRICATE_CLOSED = ("standpipe_valve",)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="drillers",
        name="Driller's Method — shut in on a kick",
        objective="Circulate the influx out at constant bottomhole pressure.",
        briefing=(
            "The well is shut in on a kick taken while drilling. The pressures "
            "have stabilised and the choke line is lined up. Bring the pump to "
            "kill rate and hold bottomhole pressure on the formation."
        ),
        require_kill=False,
    ),
    Scenario(
        key="find_the_kick",
        name="Drilling ahead — find the kick",
        objective="Spot the kick, shut in, and read your own shut-in pressures.",
        briefing=(
            "Drilling ahead, everything normal. There is no bottomhole gauge on "
            "a rig: watch the pit level and the flow line. When you are sure "
            "the well is flowing, shut in and let the pressures stabilise."
        ),
        start=StartState.DRILLING,
    ),
    Scenario(
        key="volumetric",
        name="Volumetric — the pump is down",
        objective=(
            "Hold bottomhole pressure by bleeding alone, with no circulation."
        ),
        briefing=(
            "Shut in on a kick, and the mud pump will not run. The gas is "
            "migrating and surface pressure will climb on its own. Let it rise "
            "in controlled steps and bleed mud back to hold bottomhole "
            "pressure. Doing nothing is not a neutral choice."
        ),
        faults=(
            Fault(
                kind=FaultKind.PUMP_FAILURE,
                at_seconds=0.0,
                message="Mud pump will not run — fluid end failure.",
            ),
        ),
    ),
    Scenario(
        key="lube_and_bleed",
        name="Lube and Bleed — gas at surface",
        objective="Replace the gas cap with mud, a slug at a time.",
        briefing=(
            "The gas has migrated to surface and the well is shut in. Line the "
            "pump up to the kill line, lubricate mud in on top of the gas, let "
            "it fall through, then bleed the gas off. Repeat."
        ),
        open_valves=_LUBRICATE,
        closed_valves=_LUBRICATE_CLOSED,
    ),
    Scenario(
        key="stripping",
        name="Stripping back to bottom",
        objective="Run the bit back to bottom through a closed BOP.",
        briefing=(
            "A kick was taken with the bit off bottom, and the well is shut in. "
            "Strip the pipe back down, bleeding the steel it displaces to hold "
            "bottomhole pressure. Watch the hookload as well as the gauges."
        ),
        bit_off_bottom_ft=1000.0,
    ),
    Scenario(
        key="reverse",
        name="Reverse circulation — float ported",
        objective="Bring the influx up the drill pipe instead of the annulus.",
        briefing=(
            "The float has been ported, so returns can come back up the string. "
            "Pump down the annulus through the kill line and take returns up "
            "the pipe to the choke. The influx never passes the shoe."
        ),
        open_valves=_REVERSE,
        closed_valves=_REVERSE_CLOSED,
    ),
    Scenario(
        key="choke_plugs",
        name="The choke plugs",
        objective="Circulate the influx out, and deal with what happens.",
        briefing=(
            "A routine Driller's Method circulation. Hold bottomhole pressure "
            "on the formation and get the influx out."
        ),
        faults=(
            Fault(
                kind=FaultKind.CHOKE_PLUGGED,
                at_strokes=900.0,
                message=(
                    "Choke 1 is not responding — pressure climbing with the "
                    "handle where it is."
                ),
            ),
        ),
    ),
    Scenario(
        key="horizontal",
        name="Horizontal well — Driller's preferred",
        objective="Circulate out a kick in a lateral.",
        briefing=(
            "A horizontal well. The influx sits in the lateral, where it has no "
            "vertical height, so it barely shows on the shut-in pressures and "
            "it will not migrate. Wait and Weight would need a separate drill "
            "pipe schedule for every section."
        ),
        trajectory="horizontal",
    ),
    Scenario(
        key="subsea",
        name="Subsea BOP — choke line friction",
        objective="Circulate out with the stack on the seabed.",
        briefing=(
            "A subsea stack. Choke line friction acts on the formation, and the "
            "gauge is at the rig floor while the BOP is at the mudline. Both "
            "have to be allowed for."
        ),
        bop="subsea",
    ),
    Scenario(
        key="weak_shoe",
        name="Weak casing seat",
        objective="Get the influx out without breaking down the shoe.",
        briefing=(
            "The casing seat on this well is not what the leak-off test said. "
            "There is less margin than the kill sheet suggests."
        ),
        faults=(
            Fault(
                kind=FaultKind.WEAK_SHOE,
                at_seconds=0.0,
                message="Formation integrity below the recorded leak-off.",
                severity=Severity.WARNING,
            ),
        ),
    ),
)

_BY_KEY = {s.key: s for s in SCENARIOS}


def get_scenario(key: str) -> Scenario:
    """Look one up by key.

    Raises:
        KeyError: naming the ones that exist, so a typo is self-correcting.
    """
    if key not in _BY_KEY:
        raise KeyError(
            f"No scenario '{key}'. Available: {', '.join(sorted(_BY_KEY))}"
        )
    return _BY_KEY[key]


def scenario_list() -> list[dict]:
    """Every scenario, for the picker."""
    return [s.summary() for s in SCENARIOS]
