"""Well control simulator -- a forward model of a rig under a student's hands.

The rest of this package answers "what should the driller hold?". This
subpackage answers the opposite question: **the student set the choke to 35%
and the pump to 30 spm -- what does the well do?** It has to represent failure
as faithfully as success, because failing safely is the point of a trainer.

Usage::

    from wellcontrol import examples
    from wellcontrol.sim import Simulator, ValveState

    sim = Simulator(well=examples.well_state(config))

    sim.flow_path.close("annular")            # shut in
    sim.flow_path.open("choke_line_valve", "choke_line_to_manifold", "choke_1")
    sim.choke.command(30.0)                   # 30% open
    sim.pump.command(30.0)                    # 30 spm

    sim.run(seconds=60)
    print(sim.readout())

See ``docs/simulator-design.md`` for the design and what it deliberately does
not attempt.
"""

from __future__ import annotations

from .assessment import (
    Assessment,
    Event,
    EventKind,
    EventLog,
    Grade,
    Metric,
    Severity,
    Standard,
    TraceSample,
    assess,
)
from .auth import RateLimiter, TokenSigner, class_code, hash_password, verify_password
from .classroom import TICK_S, Classroom, ClassroomStore, Student
from .recording import Recording, RecordingStore, record_session
from .drilling import DrillingModel, RotarySystem
from .engine import MUD_COMPRESSIBILITY, SimState, Simulator
from .equipment import (
    AdjustableChoke,
    Alarm,
    MudPump,
    PitSystem,
    ReturnFlowMeter,
    TransferPump,
    TripTank,
)
from .flowpath import (
    FlowPath,
    Node,
    ReturnRoute,
    Valve,
    ValveState,
    default_valves,
    subsea_stack,
    surface_stack,
)
from .session import SPEEDS, Session, SessionStore

__all__ = [
    "AdjustableChoke",
    "Alarm",
    "Assessment",
    "Classroom",
    "ClassroomStore",
    "DrillingModel",
    "Event",
    "EventKind",
    "EventLog",
    "FlowPath",
    "Grade",
    "MUD_COMPRESSIBILITY",
    "Metric",
    "MudPump",
    "Node",
    "PitSystem",
    "RateLimiter",
    "Recording",
    "RecordingStore",
    "ReturnFlowMeter",
    "ReturnRoute",
    "RotarySystem",
    "SPEEDS",
    "Session",
    "SessionStore",
    "Severity",
    "SimState",
    "Simulator",
    "Standard",
    "Student",
    "TICK_S",
    "TokenSigner",
    "TransferPump",
    "TraceSample",
    "TripTank",
    "Valve",
    "ValveState",
    "assess",
    "record_session",
    "class_code",
    "default_valves",
    "subsea_stack",
    "surface_stack",
    "hash_password",
    "verify_password",
]
