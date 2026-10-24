"""HTTP interface to the simulator.

Stage 3. **The UI is a client of the engine, not part of it.** Everything here
is transport: it parses a request, calls a :class:`~wellcontrol.sim.session.Session`
method, and serialises the result. No physics, no well control decisions, no
state beyond the session store. If a rule ever needs to live here, it belongs in
the library instead.

Why a web app rather than Streamlit: Streamlit reruns the whole script on every
interaction and has no game loop, so gauges would be widgets rather than
instruments and the choke would feel laggy. The choke is the instrument the
whole exercise turns on, so that is the wrong trade.

Run it with::

    .venv/Scripts/python.exe -m uvicorn wellcontrol.sim.api:app --reload

then open http://127.0.0.1:8000/. Binds to localhost only by default, as the
Streamlit launcher does.

FastAPI is an optional dependency: ``pip install -e ".[sim]"``. Nothing in the
core library imports this module, so the dependency-free suite is unaffected.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..examples import well_state
from ..methods import get_method
from ..plotting import plot_kill, plot_session_kill, save_kill_plot
from ..wellconfig import WellConfiguration
from .crew import CrewManager, CrewMember, CrewRole, STANDARD_CALLOUTS, can_operate
from .session import SESSION_SPEEDS_DOC, SPEEDS, Session, SessionStore

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Well Control Simulator",
    description=(
        "Forward model of a rig under a student's hands. The well responds to "
        "what the student actually does, not what they intended."
    ),
    version="0.1.0",
)

store = SessionStore()
crew_manager = CrewManager()


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------

class NewSession(BaseModel):
    scenario: str | None = Field(
        default=None,
        description=(
            "Key of a named exercise from /api/scenarios. When given it "
            "decides the well, the lineup and any faults, and the rest of "
            "this body is ignored."
        ),
    )
    student_name: str | None = Field(
        default=None,
        description="Optional name or callsign for the student session.",
    )
    instructor_name: str | None = Field(
        default=None,
        description="Optional assessor or instructor name.",
    )
    well_name: str | None = Field(
        default=None,
        description="Optional well name or well program identifier.",
    )
    complication: str | None = Field(
        default=None,
        description="Optional incident or complication note.",
    )
    bop: str = Field(default="surface", description="surface or subsea")
    trajectory: str = Field(
        default="vertical", description="vertical, deviated or horizontal"
    )
    start: str = Field(
        default="shut_in",
        description=(
            "shut_in hands over a kick already in the hole, with the well "
            "closed on it and the pressures on the gauges. drilling starts on "
            "a well that is behaving; the kick arrives later and the student "
            "has to spot it and shut in themselves."
        ),
    )


class SessionMetadataUpdate(BaseModel):
    student_name: str | None = None
    instructor_name: str | None = None
    well_name: str | None = None
    complication: str | None = None
    date: str | None = None


class ChokeCommand(BaseModel):
    percent: float = Field(ge=0.0, le=100.0)


class PumpCommand(BaseModel):
    spm: float = Field(ge=0.0, le=200.0)


class ValveCommand(BaseModel):
    name: str
    state: str = Field(description="open or closed")


class SpeedCommand(BaseModel):
    speed: float = Field(ge=0.0, le=240.0, description=SESSION_SPEEDS_DOC)


class StripCommand(BaseModel):
    feet_per_min: float = Field(
        ge=-120.0, le=120.0,
        description="Pipe speed through the stripping element. Positive runs in.",
    )


class MudWeightCommand(BaseModel):
    ppg: float = Field(
        ge=5.0, le=25.0,
        description="Mud weight in active pits (ppg).",
    )


class FaultInjectionCommand(BaseModel):
    kind: str = Field(
        description="Fault kind: pump_failure, choke_plugged, choke_washout, accumulator_leak, weak_shoe, mud_weight_drop"
    )
    message: str = Field(default="", description="Optional custom rig alert message")


class FreezeCommand(BaseModel):
    freeze: bool = Field(default=True, description="True to freeze (pause) session; False to resume")


class StudentNameCommand(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CalloutBody(BaseModel):
    sender_name: str = Field(default="Trainee")
    role: str = Field(default="driller")
    message: str = Field(min_length=1, max_length=240)
    is_standard: bool = Field(default=False)


class CustomScenarioBody(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=240)
    briefing: str = Field(default="")
    bop: str = Field(default="surface")
    trajectory: str = Field(default="vertical")
    start: str = Field(default="shut_in")
    bit_off_bottom_ft: float = 0.0
    mud_weight_ppg: float | None = None
    sidpp_psi: float | None = None
    sicp_psi: float | None = None
    pit_gain_bbl: float | None = None
    frac_gradient_ppg: float | None = None
    shoe_md_ft: float | None = None
    kick_after_s: float | None = None
    require_kill: bool = False
    migration_rate_ft_per_hr: float | None = None
    faults: list[dict[str, Any]] = []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _session(session_id: str) -> Session:
    try:
        return store.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def choke_panel() -> FileResponse:
    return FileResponse(STATIC / "choke_panel.html")


@app.get("/teacher", include_in_schema=False)
def teacher_console() -> FileResponse:
    return FileResponse(STATIC / "teacher.html")


@app.get("/crew", include_in_schema=False)
def crew_console() -> FileResponse:
    return FileResponse(STATIC / "crew.html")


@app.get("/api/speeds")
def speeds() -> dict:
    return {"speeds": list(SPEEDS)}


@app.get("/api/scenarios")
def scenarios() -> dict:
    """The exercise library.

    Faults are reported only as a flag, never as a list. A student who knows
    the choke is going to plug at stroke 2,000 is not being tested on noticing
    that the choke has plugged.
    """
    from .scenario import scenario_list

    return {"scenarios": scenario_list()}


@app.get("/api/scenarios/custom")
def get_custom_scenarios() -> dict:
    """List all custom instructor-created scenarios."""
    from .scenario import custom_scenario_list

    return {"custom_scenarios": custom_scenario_list()}


@app.get("/api/scenarios/templates")
def get_scenario_templates() -> dict:
    """Pre-engineered scenario templates ready to be customized."""
    from .scenario import get_custom_templates

    return {"templates": get_custom_templates()}


@app.post("/api/scenarios/custom", status_code=201)
def create_custom_scenario(body: CustomScenarioBody) -> dict:
    """Create or update a custom well control scenario and save to disk."""
    from .scenario import save_custom_scenario

    try:
        data = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        scen = save_custom_scenario(data)
        return {"scenario": scen.summary()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/scenarios/custom/{key}")
def delete_scenario(key: str) -> dict:
    """Delete a custom scenario from memory and disk."""
    from .scenario import delete_custom_scenario

    ok = delete_custom_scenario(key)
    if not ok:
        raise HTTPException(
            status_code=404, detail=f"Custom scenario '{key}' not found."
        )
    return {"deleted": True, "key": key}


@app.post("/api/sessions", status_code=201)
def create_session(body: NewSession) -> dict:
    try:
        session = store.create(
            scenario=body.scenario,
            student_name=body.student_name,
            instructor_name=body.instructor_name,
            well_name=body.well_name,
            complication=body.complication,
            **({} if body.scenario else {
                "bop": body.bop,
                "trajectory": body.trajectory,
                "start": body.start,
            }),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # An unknown BOP location or trajectory: the enum says what is valid.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "session_id": session.session_id,
        "student_name": session.student_name,
        "gauges": session.gauges(),
        "lineup": session.lineup(),
        "scenario": (
            None if session.scenario is None else session.scenario.summary()
        ),
    }


@app.get("/api/sessions/{session_id}")
def snapshot(session_id: str, since_event: int = 0, trace_limit: int = 240) -> dict:
    """The panel's poll: gauges, new events and the strip chart, one instant."""
    return _session(session_id).snapshot(
        since_event=since_event, trace_limit=trace_limit
    )


@app.get("/api/sessions/{session_id}/trace")
def trace(session_id: str, limit: int = 1200) -> dict:
    """Historical trace telemetry specifically for the EDR trend recorder."""
    return {"trace": _session(session_id).trace(limit=limit)}


@app.get("/api/sessions/{session_id}/lineup")
def lineup(session_id: str) -> dict:
    return {"valves": _session(session_id).lineup()}


@app.post("/api/sessions/{session_id}/choke")
def set_choke(session_id: str, body: ChokeCommand) -> dict:
    session = _session(session_id)
    session.set_choke(body.percent)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/pump")
def set_pump(session_id: str, body: PumpCommand) -> dict:
    session = _session(session_id)
    session.set_pump(body.spm)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/valve")
def set_valve(session_id: str, body: ValveCommand) -> dict:
    """Operate a valve.

    A wrong lineup is not an error -- it simply does not work -- so the only
    failures here are a valve that does not exist or a state that is not open
    or closed.
    """
    session = _session(session_id)
    try:
        session.set_valve(body.name, body.state)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"gauges": session.gauges(), "lineup": session.lineup()}


@app.post("/api/sessions/{session_id}/speed")
def set_speed(session_id: str, body: SpeedCommand) -> dict:
    session = _session(session_id)
    session.set_speed(body.speed)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/strip")
def strip(session_id: str, body: StripCommand) -> dict:
    session = _session(session_id)
    session.strip(body.feet_per_min)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/mud-weight")
def set_mud_weight(session_id: str, body: MudWeightCommand) -> dict:
    session = _session(session_id)
    session.set_mud_weight(body.ppg)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/zero-trip-tank")
def zero_trip_tank(session_id: str) -> dict:
    session = _session(session_id)
    session.zero_trip_tank()
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/fill-trip-tank")
def fill_trip_tank(session_id: str) -> dict:
    session = _session(session_id)
    session.refill_trip_tank()
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/zero-pits")
def zero_pits(session_id: str) -> dict:
    session = _session(session_id)
    session.zero_pits()
    return {"gauges": session.gauges()}


@app.get("/api/sessions/{session_id}/schematic")
def schematic(session_id: str, reveal: bool = False) -> dict:
    return _session(session_id).schematic(force_visible=reveal)


@app.post("/api/sessions/{session_id}/toggle-downhole")
def toggle_downhole(session_id: str, reveal: bool | None = None) -> dict:
    sess = _session(session_id)
    state = sess.toggle_downhole_reveal(reveal)
    return {"force_reveal_downhole": state, "schematic": sess.schematic()}


@app.get("/api/sessions/{session_id}/killsheet")
def kill_sheet(session_id: str) -> dict:
    return _session(session_id).kill_sheet()


@app.get("/api/sessions/{session_id}/debrief")
def debrief(session_id: str, require_kill: bool | None = None) -> dict:
    """Grade the run. Omit require_kill to use the scenario's own standard."""
    return _session(session_id).debrief(require_kill=require_kill)


@app.get("/api/sessions/{session_id}/ai-debrief")
def ai_debrief(session_id: str) -> dict:
    """AI-powered qualitative assessment, score, and instructor coaching notes."""
    return _session(session_id).ai_debrief()


@app.get("/api/sessions/{session_id}/metadata")
def get_session_metadata(session_id: str) -> dict:
    """Student and certification audit metadata for the kill plot."""
    session = _session(session_id)
    return {
        "session_id": session.session_id,
        "student_name": session.student_name,
        "instructor_name": getattr(session, "instructor_name", "Lead Assessor"),
        "well_name": session.get_well_name(),
        "complication": session.get_complication(),
        "date": getattr(session, "created_at", None),
    }


@app.post("/api/sessions/{session_id}/metadata")
def update_session_metadata(session_id: str, body: SessionMetadataUpdate) -> dict:
    """Update certification metadata (student name, instructor name, well name, complication, date)."""
    session = _session(session_id)
    if body.student_name is not None:
        session.student_name = body.student_name
    if body.instructor_name is not None:
        session.instructor_name = body.instructor_name
    if body.well_name is not None:
        session.well_name = body.well_name
    if body.complication is not None:
        session.complication = body.complication
    if body.date is not None:
        session.created_at = body.date
    return {
        "session_id": session.session_id,
        "student_name": session.student_name,
        "instructor_name": getattr(session, "instructor_name", "Lead Assessor"),
        "well_name": session.get_well_name(),
        "complication": session.get_complication(),
        "date": getattr(session, "created_at", None),
    }


@app.get("/api/sessions/{session_id}/kill-plot.png")
def session_kill_plot_png(
    session_id: str,
    theme: str = "light",
    student_name: str | None = None,
    instructor_name: str | None = None,
    well_name: str | None = None,
    complication: str | None = None,
    date: str | None = None,
    status: str | None = None,
    x_axis: str = "strokes",
):
    """Render high-resolution PNG kill plot stamped with certification audit title block."""
    session = _session(session_id)
    buf = io.BytesIO()
    save_kill_plot(
        session,
        buf,
        format="png",
        theme=theme,
        student_name=student_name or session.student_name,
        instructor_name=instructor_name or getattr(session, "instructor_name", "Lead Assessor"),
        well_name=well_name or session.get_well_name(),
        complication=complication or session.get_complication(),
        date=date or getattr(session, "created_at", None),
        status=status,
        x_axis=x_axis,
    )
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="kill_plot_{session_id}.png"'},
    )


@app.get("/api/sessions/{session_id}/kill-plot.svg")
def session_kill_plot_svg(
    session_id: str,
    theme: str = "light",
    student_name: str | None = None,
    instructor_name: str | None = None,
    well_name: str | None = None,
    complication: str | None = None,
    date: str | None = None,
    status: str | None = None,
    x_axis: str = "strokes",
):
    """Render vector SVG kill plot stamped with certification audit title block."""
    session = _session(session_id)
    buf = io.BytesIO()
    save_kill_plot(
        session,
        buf,
        format="svg",
        theme=theme,
        student_name=student_name or session.student_name,
        instructor_name=instructor_name or getattr(session, "instructor_name", "Lead Assessor"),
        well_name=well_name or session.get_well_name(),
        complication=complication or session.get_complication(),
        date=date or getattr(session, "created_at", None),
        status=status,
        x_axis=x_axis,
    )
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'inline; filename="kill_plot_{session_id}.svg"'},
    )


@app.get("/api/methods/{method_key}/kill-plot.png")
def method_kill_plot_png(
    method_key: str,
    theme: str = "light",
    student_name: str = "Student Trainee",
    instructor_name: str = "Lead Assessor",
    well_name: str = "Reference Well #1",
    complication: str = "Standard Planned Procedure",
    date: str | None = None,
    status: str = "VERIFIED PLAN",
):
    """Render PNG kill plot for theoretical methods (Driller's, W&W, Bullheading, Volumetric, Lube & Bleed)."""
    try:
        method = get_method(method_key)
        cfg = WellConfiguration.standard()[0]
        well = well_state(cfg)
        plan = method.plan(well)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown method {method_key}") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    buf = io.BytesIO()
    save_kill_plot(
        plan,
        buf,
        format="png",
        theme=theme,
        student_name=student_name,
        instructor_name=instructor_name,
        well_name=well_name,
        complication=complication,
        date=date,
        status=status,
    )
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="kill_plot_{method_key}.png"'},
    )


@app.delete("/api/sessions/{session_id}", status_code=204)
def end_session(session_id: str) -> None:
    _session(session_id)
    store.drop(session_id)


# --------------------------------------------------------------------------
# Teacher / Instructor endpoints
# --------------------------------------------------------------------------

@app.get("/api/teacher/sessions")
def teacher_sessions() -> dict:
    """Multi-student overview for the teacher dashboard."""
    return {
        "sessions": [s.summary() for s in store.list()],
        "total": len(store),
    }


@app.post("/api/teacher/sessions/{session_id}/fault")
def inject_fault(session_id: str, body: FaultInjectionCommand) -> dict:
    """Inject an unexpected live equipment failure onto a student's session."""
    session = _session(session_id)
    try:
        session.inject_fault(body.kind, body.message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "session": session.summary()}


@app.post("/api/teacher/sessions/{session_id}/freeze")
def freeze_session(session_id: str, body: FreezeCommand) -> dict:
    """Remotely freeze/unfreeze a specific student for whiteboard review."""
    session = _session(session_id)
    session.freeze(body.freeze)
    return {"ok": True, "session": session.summary()}


@app.post("/api/teacher/sessions/{session_id}/name")
def rename_student(session_id: str, body: StudentNameCommand) -> dict:
    """Rename a student session."""
    session = _session(session_id)
    session.student_name = body.name
    return {"ok": True, "session": session.summary()}


@app.post("/api/teacher/freeze-all")
def freeze_all() -> dict:
    """Master freeze across all active sessions simultaneously."""
    store.freeze_all(True)
    return {"ok": True, "sessions": [s.summary() for s in store.list()]}


@app.post("/api/teacher/resume-all")
def resume_all() -> dict:
    """Master resume across all active sessions simultaneously."""
    store.freeze_all(False)
    return {"ok": True, "sessions": [s.summary() for s in store.list()]}


@app.post("/api/teacher/spawn-demo-class")
def spawn_demo_class() -> dict:
    """Spawn a representative cohort of students for immediate training & demonstration."""
    demos = [
        ("Alex M.", "drillers", 10.0),
        ("Elena R.", "wait_and_weight", 10.0),
        ("Carlos D.", "bullhead", 10.0),
        ("Priya S.", "reverse", 10.0),
        ("Tom H.", "find_the_kick", 1.0),
    ]
    spawned = []
    for name, scenario, spd in demos:
        s = store.create(scenario=scenario, student_name=name)
        s.set_speed(spd)
        spawned.append(s.summary())
    return {
        "ok": True,
        "spawned": len(spawned),
        "sessions": [s.summary() for s in store.list()],
    }


@app.get("/api/sessions/{session_id}/replay")
def replay_session(session_id: str) -> dict:
    """Full flight-recorder telemetry sequence and events for second-by-second scrubbing."""
    return _session(session_id).replay_data()


@app.get("/api/teacher/gradebook")
def teacher_gradebook() -> dict:
    """Cohort gradebook entries with AI scores and metrics."""
    return {"entries": store.gradebook()}


@app.get("/api/teacher/gradebook.csv")
def teacher_gradebook_csv() -> Response:
    """Export cohort gradebook as downloadable CSV for Excel / LMS."""
    import csv
    import io

    entries = store.gradebook()
    output = io.StringIO()
    if entries:
        writer = csv.DictWriter(output, fieldnames=list(entries[0].keys()))
        writer.writeheader()
        writer.writerows(entries)
    else:
        writer = csv.writer(output)
        writer.writerow(["session_id", "student_name", "scenario", "score", "letter_grade", "iwcf_standard"])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="wellcontrol_gradebook.csv"'},
    )


# --------------------------------------------------------------------------
# Crew CRM & Multi-User Collaboration
# --------------------------------------------------------------------------

def _execute_crew_action(session: Session, action: str, params: dict) -> str:
    """Execute a validated crew member action on the shared session."""
    if action == "pump":
        spm = float(params.get("spm", 0.0))
        session.set_pump(spm)
        return f"Set pump rate to {spm:.0f} SPM"
    elif action == "choke":
        pct = float(params.get("percent", params.get("position", 0.0)))
        session.set_choke(pct)
        return f"Positioned choke to {pct:.1f}%"
    elif action == "valve":
        name = str(params.get("name", ""))
        state = str(params.get("state", "open"))
        session.set_valve(name, state)
        return f"{state.title()} {name.replace('_', ' ')}"
    elif action == "bop":
        name = str(params.get("name", ""))
        state = str(params.get("state", "closed"))
        session.set_valve(name, state)
        return f"{state.title()} BOP {name.replace('_', ' ')}"
    elif action == "speed":
        spd = float(params.get("speed", 1.0))
        session.set_speed(spd)
        return f"Set simulation speed to {spd:.0f}x"
    elif action == "strip":
        fpm = float(params.get("feet_per_min", 0.0))
        session.strip(fpm)
        direction = "Running in" if fpm > 0 else ("Pulling out" if fpm < 0 else "Stopped")
        return f"Stripping: {direction} ({abs(fpm):.0f} ft/min)"
    elif action == "mud_weight":
        ppg = float(params.get("ppg", 0.0))
        session.set_mud_weight(ppg)
        return f"Weighted active pit to {ppg:.1f} ppg"
    elif action == "kill_mud":
        session.fill_with_kill_mud()
        return "Filled active pits with calculated kill mud"
    elif action == "zero_pit":
        session.zero_pit_totalizer()
        return "Reset pit volume totalizer"
    elif action == "zero_tt":
        session.zero_trip_tank()
        return "Reset trip tank totalizer"
    return f"Executed {action}"


@app.get("/api/crew/roles")
def crew_roles() -> dict:
    """Available rig crew roles, titles, and badge colors."""
    return {
        "roles": [
            {
                "role": r.value,
                "title": r.title,
                "badge_color": r.badge_color,
            }
            for r in CrewRole
        ]
    }


@app.get("/api/crew/callouts/standard")
def standard_callouts() -> dict:
    """Standard IADC/IWCF rig-floor CRM radio callouts."""
    return {"callouts": STANDARD_CALLOUTS}


@app.get("/api/crew/{session_id}/roster")
def crew_roster(session_id: str) -> dict:
    """Roster of active crew members in session room."""
    _session(session_id)
    room = crew_manager.get_or_create_room(session_id)
    return {"session_id": session_id, "roster": room.roster()}


@app.post("/api/crew/{session_id}/callout")
def post_crew_callout(session_id: str, body: CalloutBody) -> dict:
    """Broadcast an intercom radio callout to the crew."""
    _session(session_id)
    room = crew_manager.get_or_create_room(session_id)
    try:
        role = CrewRole(body.role)
    except ValueError:
        role = CrewRole.DRILLER
    callout = room.post_callout(body.sender_name, role, body.message, body.is_standard)
    return {"callout": callout.to_dict()}


@app.get("/api/crew/{session_id}/callouts")
def get_crew_callouts(session_id: str) -> dict:
    """Recent intercom radio callouts history."""
    _session(session_id)
    room = crew_manager.get_or_create_room(session_id)
    return {"callouts": [c.to_dict() for c in room.callouts]}


@app.websocket("/ws/crew/{session_id}")
async def crew_websocket(
    websocket: WebSocket,
    session_id: str,
    role: str = "driller",
    name: str = "Trainee",
):
    """Bi-directional real-time collaboration socket for crew members."""
    try:
        session = _session(session_id)
    except HTTPException:
        await websocket.close(code=4004, reason="Session not found")
        return

    try:
        crew_role = CrewRole(role)
    except ValueError:
        crew_role = CrewRole.DRILLER

    await websocket.accept()
    room = crew_manager.get_or_create_room(session_id)
    member = CrewMember(name=name, role=crew_role, websocket=websocket)
    room.add_member(member)

    # Broadcast roster update
    await room.broadcast({
        "type": "roster",
        "roster": room.roster(),
        "event": f"{member.name} joined as {member.role.title}",
    })

    # Initial state to client
    await websocket.send_json({
        "type": "init",
        "member": member.to_dict(),
        "roster": room.roster(),
        "callouts": [c.to_dict() for c in room.callouts],
        "snapshot": session.snapshot(),
    })

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "action")

            if msg_type == "callout":
                msg = str(data.get("message", "")).strip()
                if msg:
                    is_std = bool(data.get("is_standard", False))
                    c = room.post_callout(member.name, member.role, msg, is_std)
                    await room.broadcast({
                        "type": "callout",
                        "callout": c.to_dict(),
                    })
            elif msg_type == "action":
                action = data.get("action", "")
                params = data.get("params", {})
                if not can_operate(member.role, action):
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Action '{action}' is reserved for another rig role.",
                    })
                    continue

                desc = _execute_crew_action(session, action, params)
                snap = session.snapshot()
                await room.broadcast({
                    "type": "telemetry",
                    "snapshot": snap,
                    "event": f"[{member.role.title}] {member.name}: {desc}",
                })
            elif msg_type == "sync":
                await websocket.send_json({
                    "type": "telemetry",
                    "snapshot": session.snapshot(),
                })
    except WebSocketDisconnect:
        room.remove_member(member.member_id)
        await room.broadcast({
            "type": "roster",
            "roster": room.roster(),
            "event": f"{member.name} ({member.role.title}) left the console",
        })
