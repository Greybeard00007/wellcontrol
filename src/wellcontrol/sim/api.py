"""HTTP interface to the simulator.

**The UI is a client of the engine, not part of it.** Everything here is
transport: it parses a request, calls a :class:`~wellcontrol.sim.session.Session`
or :class:`~wellcontrol.sim.classroom.Classroom` method, and serialises the
result. No physics, no well control decisions. If a rule ever needs to live
here, it belongs in the library instead.

Why a web app rather than Streamlit: Streamlit reruns the whole script on every
interaction and has no game loop, so gauges would be widgets rather than
instruments and the choke would feel laggy. The choke is the instrument the
whole exercise turns on, so that is the wrong trade.

## Two ways in

**Solo** -- ``POST /api/sessions`` and the ``/api/sessions/{id}`` routes. One
person, one well, no login. This is the original self-study trainer and it
still works exactly as it did.

**A class** -- an instructor logs in, creates a class, and reads out its code.
Students ``POST /api/join`` with the code and a name, and then work their own
well through ``/api/me``. See :mod:`~wellcontrol.sim.classroom` for why the
clock moved to the server to make that possible, and
:mod:`~wellcontrol.sim.auth` for why there are no student accounts.

## The driver

A single asyncio task ticks every class every :data:`TICK_S` seconds. That is
the only thing in this module that touches time, and it is here rather than in
the library because it is runtime plumbing -- the library exposes
:meth:`Classroom.tick` as an ordinary method so the whole time model is tested
without an event loop.

Run it with::

    .venv/Scripts/python.exe -m uvicorn wellcontrol.sim.api:app --reload

FastAPI is an optional dependency: ``pip install -e ".[sim]"``. Nothing in the
core library imports this module, so the dependency-free suite is unaffected.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import (
    INSTRUCTOR_TOKEN_TTL_S,
    STUDENT_TOKEN_TTL_S,
    RateLimiter,
    TokenSigner,
    verify_password,
)
from .classroom import TICK_S, Classroom, ClassroomStore, Student
from .config import Settings
from .recording import RecordingStore, record_session
from .session import SESSION_SPEEDS_DOC, SPEEDS, Session, SessionStore

STATIC = Path(__file__).resolve().parent / "static"

log = logging.getLogger("wellcontrol.sim")

settings = Settings.from_env()
signer = TokenSigner(secret=settings.secret_key)
store = SessionStore()
classrooms = ClassroomStore()
recordings = RecordingStore()

#: Separate cookies so an instructor can keep a student screen open in another
#: tab of the same browser without the two logins evicting each other.
INSTRUCTOR_COOKIE = "wc_instructor"
STUDENT_COOKIE = "wc_student"

#: Guessing a class code should be expensive; so should guessing the password.
join_limiter = RateLimiter(limit=12, window_s=60.0)
login_limiter = RateLimiter(limit=6, window_s=300.0)

#: Ticks between sweeps for expired classes. Cheap, so hourly is ample.
_PURGE_EVERY = int(3600 / TICK_S)


async def _drive() -> None:
    """Advance every well in every class, forever.

    **One well must not be able to stop the room.** An exception raised deep in
    one student's engine would otherwise kill this task and silently freeze
    fourteen other wells, so the loop logs and carries on. A student whose well
    is broken gets a stuck screen and can be restarted; the class keeps running.
    """
    ticks = 0
    while True:
        await asyncio.sleep(TICK_S)
        ticks += 1
        try:
            classrooms.tick_all()
            if ticks % _PURGE_EVERY == 0:
                classrooms.purge_expired()
        except Exception:  # noqa: BLE001 - see docstring
            log.exception("Tick failed; the class clock continues.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for note in settings.startup_notes():
        log.warning(note)
    task = asyncio.create_task(_drive())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Well Control Simulator",
    description=(
        "Forward model of a rig under a student's hands. The well responds to "
        "what the student actually does, not what they intended."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


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


class ChokeCommand(BaseModel):
    percent: float = Field(ge=0.0, le=100.0)
    choke: str = Field(
        default="choke_1",
        description=(
            "choke_1 or choke_2 (remote), manual_choke_1 or manual_choke_2 "
            "(handwheels on the manifold). Which one actually passes flow is "
            "decided by the lineup, not by this."
        ),
    )


class PumpCommand(BaseModel):
    spm: float = Field(ge=0.0, le=200.0)
    pump: int = Field(default=1, ge=1, le=2, description="Rig pump 1 or 2.")


class RotaryCommand(BaseModel):
    rpm: float = Field(ge=0.0, le=250.0)


class WeightOnBitCommand(BaseModel):
    klbf: float = Field(ge=0.0, le=100.0)


class AutoDrillerCommand(BaseModel):
    on: bool
    setpoint_klbf: float | None = Field(default=None, ge=0.0, le=100.0)


class TransferPumpCommand(BaseModel):
    name: str = Field(
        description=(
            "trip_tank_fillup, trip_tank_fill, trip_tank_drain or "
            "pit_transfer."
        )
    )
    running: bool


class ZeroCommand(BaseModel):
    instrument: str = Field(description="pits or trip_tank")


class AlarmCommand(BaseModel):
    name: str = Field(
        description="pit_deviation, trip_tank_level or return_flow"
    )
    low: float | None = None
    high: float | None = None
    armed: bool | None = Field(
        default=None,
        description=(
            "Disarming silences the horn but leaves the lamp, so the "
            "instructor can see what was silenced."
        ),
    )


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


class Login(BaseModel):
    password: str


class JoinRequest(BaseModel):
    code: str = Field(description="The class code, as read off the board.")
    name: str = Field(max_length=60, description="What the instructor will see.")


class FaultCommand(BaseModel):
    kind: str = Field(
        description=(
            "pump_failure, choke_plugged, choke_washout, accumulator_leak "
            "or weak_shoe. See /api/fault-kinds."
        )
    )
    student_id: str | None = Field(
        default=None,
        description="One student, or omit to break it for the whole class.",
    )
    message: str = Field(
        default="",
        description=(
            "What the student's log will say. Write it as the rig would "
            "report it, not as an explanation -- they have to work out what "
            "it means. Left blank, the fault's own wording is used."
        ),
    )


class NewClassroom(BaseModel):
    name: str = Field(default="Class", max_length=80)
    scenario: str | None = Field(
        default=None,
        description=(
            "Key of a named exercise. When set it decides the well, the "
            "lineup and the faults for every student in the class."
        ),
    )
    bop: str = Field(default="surface", description="surface or subsea")
    trajectory: str = Field(default="vertical")
    start: str = Field(default="shut_in")
    capacity: int = Field(default=30, ge=1, le=200)
    ttl_hours: float = Field(default=12.0, gt=0, le=72)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _session(session_id: str) -> Session:
    try:
        return store.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _caller(request: Request) -> str:
    """Who to rate-limit against.

    Behind a reverse proxy every request arrives from the proxy's own address,
    so throttling on that would take the whole class out the first time anyone
    fat-fingered a code. The forwarded address is used instead -- but only when
    configured, because a directly exposed server must not believe a header the
    client controls.
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _token(request: Request, cookie: str) -> str:
    """Bearer header first, then the cookie. The header is for API clients and
    tests; browsers get an httpOnly cookie they cannot leak to a script."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(cookie, "")


def require_instructor(request: Request) -> dict:
    payload = signer.verify(_token(request, INSTRUCTOR_COOKIE))
    if not payload or payload.get("role") != "instructor":
        raise HTTPException(status_code=401, detail="Instructor login required.")
    return payload


def require_student(request: Request) -> tuple[Classroom, Student]:
    """Resolve the caller's class and seat, or 401.

    **The roster is checked on every request, not just at join.** That is what
    makes dropping a student effective against a token that is still
    cryptographically valid -- there is no token blacklist, and there does not
    need to be one.
    """
    payload = signer.verify(_token(request, STUDENT_COOKIE))
    if not payload or payload.get("role") != "student":
        raise HTTPException(status_code=401, detail="Join a class first.")
    try:
        classroom = classrooms.get(payload.get("classroom_id", ""))
        student = classroom.student(payload.get("student_id", ""))
    except KeyError as exc:
        raise HTTPException(
            status_code=401, detail="That class has ended."
        ) from exc
    return classroom, student


def _capture(classroom: Classroom, student) -> dict | None:
    """Write a student's run to disk, and report what was written.

    Called before anything that destroys a run -- a restart, or the class
    ending -- because those are exactly the moments a debrief is about to be
    wanted and the evidence is about to go. Failure to write is logged and
    swallowed: a read-only volume is a deployment problem, not a reason to
    refuse to end a class.
    """
    try:
        recording = record_session(
            student.session,
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            classroom_name=classroom.name,
            classroom_id=classroom.classroom_id,
            student_name=student.name,
            student_id=student.student_id,
        )
        if recordings.save(recording) is None:
            log.warning("Could not write a recording to %s", recordings.directory)
            return None
        return recording.summary()
    except Exception:  # noqa: BLE001 - never lose a class over a debrief file
        log.exception("Recording failed for %s", student.name)
        return None


def _classroom(classroom_id: str) -> Classroom:
    try:
        return classrooms.get(classroom_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def exclusive(handler):
    """Serialise a handler against the class clock.

    **The engines have two callers now.** A sync FastAPI handler runs in a
    threadpool worker while :func:`_drive` runs on the event loop, so without
    this a student's request and the clock tick are genuinely inside the same
    engine at the same time -- double-counted elapsed time at best, a
    half-rebuilt fluid column at worst. Joining and dropping are no safer:
    they mutate the dictionaries ``tick_all`` is iterating.

    Applied to every route that touches a class; a structural test in
    ``test_classroom_api.py`` checks that, because the race is not reliably
    reproducible and the failure that will really happen is somebody adding a
    route beside these without thinking about the clock. The solo
    ``/api/sessions`` routes do not need it -- nothing else advances those.
    """
    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        with classrooms.exclusive():
            return handler(*args, **kwargs)
    # Marker, so a test can check every classroom route carries it rather than
    # trusting that whoever adds the next one remembers.
    wrapper.holds_engine_lock = True
    return wrapper


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def choke_panel() -> FileResponse:
    """The student's screens.

    Serves the same page whether the caller is flying solo or sitting in a
    class; the page asks the server which it is by probing ``/api/me``, because
    the class token is an httpOnly cookie it cannot read for itself.
    """
    return FileResponse(STATIC / "choke_panel.html")


@app.get("/join", include_in_schema=False)
def join_page() -> FileResponse:
    return FileResponse(STATIC / "join.html")


@app.get("/instructor", include_in_schema=False)
def instructor_page() -> FileResponse:
    return FileResponse(STATIC / "instructor.html")


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


@app.post("/api/sessions", status_code=201)
def create_session(body: NewSession) -> dict:
    try:
        session = store.create(
            scenario=body.scenario,
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


@app.get("/api/sessions/{session_id}/lineup")
def lineup(session_id: str) -> dict:
    return {"valves": _session(session_id).lineup()}


@app.post("/api/sessions/{session_id}/choke")
def set_choke(session_id: str, body: ChokeCommand) -> dict:
    session = _session(session_id)
    try:
        session.set_choke(body.percent, body.choke)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/pump")
def set_pump(session_id: str, body: PumpCommand) -> dict:
    session = _session(session_id)
    session.set_pump(body.spm, body.pump)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/rotary")
def set_rotary(session_id: str, body: RotaryCommand) -> dict:
    session = _session(session_id)
    session.set_rotary(body.rpm)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/wob")
def set_wob(session_id: str, body: WeightOnBitCommand) -> dict:
    session = _session(session_id)
    session.set_weight_on_bit(body.klbf)
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/autodriller")
def set_auto_driller(session_id: str, body: AutoDrillerCommand) -> dict:
    session = _session(session_id)
    session.set_auto_driller(body.on, body.setpoint_klbf)
    return {"gauges": session.gauges()}

@app.post("/api/sessions/{session_id}/transfer")
def set_transfer_pump(session_id: str, body: TransferPumpCommand) -> dict:
    session = _session(session_id)
    try:
        session.set_transfer_pump(body.name, body.running)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/zero")
def zero_instrument(session_id: str, body: ZeroCommand) -> dict:
    session = _session(session_id)
    try:
        session.zero(body.instrument)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": session.gauges()}


@app.post("/api/sessions/{session_id}/alarm")
def set_alarm(session_id: str, body: AlarmCommand) -> dict:
    session = _session(session_id)
    try:
        session.set_alarm(body.name, body.low, body.high, body.armed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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


@app.get("/api/sessions/{session_id}/schematic")
def schematic(session_id: str) -> dict:
    return _session(session_id).schematic()


@app.get("/api/sessions/{session_id}/killsheet")
def kill_sheet(session_id: str) -> dict:
    return _session(session_id).kill_sheet()


@app.get("/api/sessions/{session_id}/debrief")
def debrief(session_id: str, require_kill: bool | None = None) -> dict:
    """Grade the run. Omit require_kill to use the scenario's own standard."""
    return _session(session_id).debrief(require_kill=require_kill)


@app.delete("/api/sessions/{session_id}", status_code=204)
def end_session(session_id: str) -> None:
    _session(session_id)
    store.drop(session_id)


# --------------------------------------------------------------------------
# The instructor
# --------------------------------------------------------------------------

def _set_token_cookie(response: Response, name: str, token: str, ttl: float) -> None:
    """httpOnly so no script can read it.

    ``secure`` follows ``trust_proxy`` rather than being hard-coded on: a class
    run over plain HTTP on a classroom LAN would never receive a Secure cookie,
    and the login would fail with nothing to see. On a server behind a TLS
    proxy, which is what trust_proxy means, it is set.
    """
    response.set_cookie(
        name,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.trust_proxy,
        max_age=int(ttl),
        path="/",
    )


@app.post("/api/instructor/login")
def instructor_login(body: Login, request: Request, response: Response) -> dict:
    """The one durable credential in the system."""
    caller = _caller(request)
    if not login_limiter.allow(caller):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Wait a few minutes."
        )
    if not verify_password(body.password, settings.password_hash):
        raise HTTPException(status_code=401, detail="Wrong password.")
    login_limiter.reset(caller)

    token = signer.sign({"role": "instructor"}, ttl_s=INSTRUCTOR_TOKEN_TTL_S)
    _set_token_cookie(response, INSTRUCTOR_COOKIE, token, INSTRUCTOR_TOKEN_TTL_S)
    return {"token": token, "expires_in_s": INSTRUCTOR_TOKEN_TTL_S}


@app.post("/api/instructor/logout", status_code=204)
def instructor_logout(response: Response) -> None:
    response.delete_cookie(INSTRUCTOR_COOKIE, path="/")


@app.post("/api/instructor/classes", status_code=201)
@exclusive
def create_classroom(
    body: NewClassroom, _: dict = Depends(require_instructor)
) -> dict:
    """Open a class and mint its code.

    The class starts paused. Students join, find their screens and get their
    bearings; the well does not move until the instructor starts it.
    """
    try:
        classroom = classrooms.create(
            name=body.name,
            scenario=body.scenario,
            bop=body.bop,
            trajectory=body.trajectory,
            start=body.start,
            capacity=body.capacity,
            ttl_s=body.ttl_hours * 3600.0,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return classroom.summary()


@app.get("/api/instructor/classes")
@exclusive
def list_classrooms(_: dict = Depends(require_instructor)) -> dict:
    return {"classes": [c.summary() for c in classrooms.all]}


@app.get("/api/instructor/classes/{classroom_id}")
@exclusive
def get_classroom(
    classroom_id: str, _: dict = Depends(require_instructor)
) -> dict:
    return _classroom(classroom_id).summary()


@app.get("/api/fault-kinds")
def fault_kinds() -> dict:
    """What can be broken. Instructor-facing, but harmless: it names the
    catalogue, not what any exercise is going to do."""
    from .scenario import FaultKind

    return {
        "kinds": [
            {"kind": k.value, "label": k.label} for k in FaultKind
        ]
    }


@app.post("/api/instructor/classes/{classroom_id}/faults")
@exclusive
def inject_fault(
    classroom_id: str,
    body: FaultCommand,
    _: dict = Depends(require_instructor),
) -> dict:
    """Break something, now.

    The student sees a rig fault and nothing else -- same engine call, same log
    entry as a fault written into the exercise.
    """
    classroom = _classroom(classroom_id)
    try:
        applied = classroom.inject_fault(body.kind, body.student_id, body.message)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"applied_to": applied, "count": len(applied)}


@app.get("/api/instructor/classes/{classroom_id}/students/{student_id}")
@exclusive
def student_detail(
    classroom_id: str,
    student_id: str,
    since_event: int = 0,
    trace_limit: int = 400,
    _: dict = Depends(require_instructor),
) -> dict:
    """Everything one student has in front of them, plus the truth.

    Lets an instructor see what a student is looking at without taking their
    seat -- which matters, because half of what goes wrong is visible on their
    screens and invisible in a summary tile.
    """
    classroom = _classroom(classroom_id)
    try:
        student = classroom.student(student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session = student.session
    detail = session.snapshot(since_event=since_event, trace_limit=trace_limit)
    detail["student"] = student.summary()
    detail["instructor_view"] = session.instructor_view()
    detail["kill_sheet"] = session.kill_sheet()
    return detail


@app.get("/api/instructor/classes/{classroom_id}/students/{student_id}/killsheet")
@exclusive
def student_kill_sheet(
    classroom_id: str, student_id: str, _: dict = Depends(require_instructor)
) -> dict:
    classroom = _classroom(classroom_id)
    try:
        return classroom.student(student_id).session.kill_sheet()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/instructor/classes/{classroom_id}/students/{student_id}/record",
    status_code=201,
)
@exclusive
def record_student(
    classroom_id: str, student_id: str, _: dict = Depends(require_instructor)
) -> dict:
    """Capture a run without ending it. The student flies on."""
    classroom = _classroom(classroom_id)
    try:
        student = classroom.student(student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    saved = _capture(classroom, student)
    if saved is None:
        raise HTTPException(
            status_code=500, detail="Could not write the recording."
        )
    return saved


@app.get("/api/instructor/recordings")
def list_recordings(limit: int = 200, _: dict = Depends(require_instructor)) -> dict:
    """Every run kept, newest first. Outlives the class it came from."""
    return {"recordings": recordings.list(limit=limit)}


@app.get("/api/instructor/recordings/{recording_id}")
def get_recording(
    recording_id: str, _: dict = Depends(require_instructor)
) -> dict:
    """One run in full: trace, events, kill sheet and debrief.

    Replay is drawing this again, not re-running physics -- the run already
    happened, and a re-simulation could disagree with what the student saw.
    """
    try:
        return recordings.get(recording_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/instructor/recordings/{recording_id}", status_code=204)
def delete_recording(
    recording_id: str, _: dict = Depends(require_instructor)
) -> None:
    if not recordings.delete(recording_id):
        raise HTTPException(status_code=404, detail="No such recording.")


@app.get("/api/instructor/classes/{classroom_id}/monitor")
@exclusive
def monitor(classroom_id: str, _: dict = Depends(require_instructor)) -> dict:
    """The grid. One tile per student, with the downhole truth on every one."""
    return _classroom(classroom_id).monitor()


@app.post("/api/instructor/classes/{classroom_id}/speed")
@exclusive
def set_class_speed(
    classroom_id: str, body: SpeedCommand, _: dict = Depends(require_instructor)
) -> dict:
    """Speed is the instructor's, for the whole class.

    There is deliberately no student equivalent: a class is flown together.
    """
    classroom = _classroom(classroom_id)
    classroom.set_speed(body.speed)
    return classroom.summary()


@app.post("/api/instructor/classes/{classroom_id}/pause")
@exclusive
def pause_class(classroom_id: str, _: dict = Depends(require_instructor)) -> dict:
    classroom = _classroom(classroom_id)
    classroom.pause()
    return classroom.summary()


@app.post("/api/instructor/classes/{classroom_id}/resume")
@exclusive
def resume_class(classroom_id: str, _: dict = Depends(require_instructor)) -> dict:
    classroom = _classroom(classroom_id)
    classroom.resume()
    return classroom.summary()


@app.post("/api/instructor/classes/{classroom_id}/close")
@exclusive
def close_class(classroom_id: str, _: dict = Depends(require_instructor)) -> dict:
    """Stop the code admitting anybody. Students already in keep their wells."""
    classroom = _classroom(classroom_id)
    classroom.close()
    return classroom.summary()


@app.delete("/api/instructor/classes/{classroom_id}", status_code=204)
@exclusive
def end_class(classroom_id: str, _: dict = Depends(require_instructor)) -> None:
    classroom = _classroom(classroom_id)
    for student in list(classroom.students.values()):
        _capture(classroom, student)
    classrooms.drop(classroom_id)


@app.delete(
    "/api/instructor/classes/{classroom_id}/students/{student_id}",
    status_code=204,
)
@exclusive
def remove_student(
    classroom_id: str, student_id: str, _: dict = Depends(require_instructor)
) -> None:
    """Drop a student. Their token stops working immediately, because the
    roster is checked on every request rather than trusted from the token."""
    _classroom(classroom_id).remove(student_id)


@app.post("/api/instructor/classes/{classroom_id}/students/{student_id}/restart")
@exclusive
def restart_student(
    classroom_id: str, student_id: str, _: dict = Depends(require_instructor)
) -> dict:
    """Give one student a fresh well on the same exercise."""
    classroom = _classroom(classroom_id)
    try:
        existing = classroom.student(student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # The run about to be thrown away is the one worth keeping.
    saved = _capture(classroom, existing)
    student = classroom.restart(student_id)
    return {"student": student.summary(), "recording": saved}


@app.get("/api/instructor/classes/{classroom_id}/students/{student_id}/debrief")
@exclusive
def student_debrief(
    classroom_id: str,
    student_id: str,
    require_kill: bool | None = None,
    _: dict = Depends(require_instructor),
) -> dict:
    classroom = _classroom(classroom_id)
    try:
        student = classroom.student(student_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return student.session.debrief(require_kill=require_kill)


# --------------------------------------------------------------------------
# A student in a class
# --------------------------------------------------------------------------

@app.post("/api/join")
@exclusive
def join(body: JoinRequest, request: Request, response: Response) -> dict:
    """Enter a class with the code and a name. No account, no password.

    A wrong code and an expired one give the same answer, so somebody probing
    cannot tell a stale code from a guess.
    """
    if not join_limiter.allow(_caller(request)):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Wait a minute."
        )
    try:
        classroom = classrooms.by_code(body.code)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="No open class with that code."
        ) from exc
    try:
        student = classroom.join(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    token = signer.sign(
        {
            "role": "student",
            "classroom_id": classroom.classroom_id,
            "student_id": student.student_id,
        },
        ttl_s=STUDENT_TOKEN_TTL_S,
    )
    _set_token_cookie(response, STUDENT_COOKIE, token, STUDENT_TOKEN_TTL_S)
    return {
        "token": token,
        "student": student.summary(),
        # Without the code: there is no reason to hand it back to the room.
        "classroom": classroom.summary(include_code=False),
    }


@app.get("/api/me")
@exclusive
def my_snapshot(
    since_event: int = 0,
    trace_limit: int = 240,
    who: tuple[Classroom, Student] = Depends(require_student),
) -> dict:
    """The student's poll: gauges, new events and the strip chart.

    Note what this does *not* do: it no longer advances the well. The class
    clock does that, so a student reading their kill sheet is not sitting on a
    frozen kick.
    """
    classroom, student = who
    classroom.touch(student.student_id)
    snapshot = student.session.snapshot(
        since_event=since_event, trace_limit=trace_limit
    )
    snapshot["classroom"] = classroom.summary(include_code=False)
    snapshot["student"] = student.summary()
    return snapshot


@app.post("/api/me/choke")
@exclusive
def my_choke(
    body: ChokeCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    try:
        student.session.set_choke(body.percent, body.choke)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": student.session.gauges()}


@app.post("/api/me/pump")
@exclusive
def my_pump(
    body: PumpCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    student.session.set_pump(body.spm, body.pump)
    return {"gauges": student.session.gauges()}


@app.post("/api/me/rotary")
@exclusive
def my_rotary(
    body: RotaryCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    student.session.set_rotary(body.rpm)
    return {"gauges": student.session.gauges()}


@app.post("/api/me/wob")
@exclusive
def my_wob(
    body: WeightOnBitCommand,
    who: tuple[Classroom, Student] = Depends(require_student),
) -> dict:
    _, student = who
    student.session.set_weight_on_bit(body.klbf)
    return {"gauges": student.session.gauges()}


@app.post("/api/me/autodriller")
@exclusive
def my_auto_driller(
    body: AutoDrillerCommand,
    who: tuple[Classroom, Student] = Depends(require_student),
) -> dict:
    _, student = who
    student.session.set_auto_driller(body.on, body.setpoint_klbf)
    return {"gauges": student.session.gauges()}

@app.post("/api/me/transfer")
@exclusive
def my_transfer_pump(
    body: TransferPumpCommand,
    who: tuple[Classroom, Student] = Depends(require_student),
) -> dict:
    _, student = who
    try:
        student.session.set_transfer_pump(body.name, body.running)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": student.session.gauges()}


@app.post("/api/me/zero")
@exclusive
def my_zero(
    body: ZeroCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    try:
        student.session.zero(body.instrument)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": student.session.gauges()}


@app.post("/api/me/alarm")
@exclusive
def my_alarm(
    body: AlarmCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    try:
        student.session.set_alarm(body.name, body.low, body.high, body.armed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"gauges": student.session.gauges()}


@app.post("/api/me/valve")
@exclusive
def my_valve(
    body: ValveCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    try:
        student.session.set_valve(body.name, body.state)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"gauges": student.session.gauges(), "lineup": student.session.lineup()}


@app.post("/api/me/strip")
@exclusive
def my_strip(
    body: StripCommand, who: tuple[Classroom, Student] = Depends(require_student)
) -> dict:
    _, student = who
    student.session.strip(body.feet_per_min)
    return {"gauges": student.session.gauges()}


@app.get("/api/me/lineup")
@exclusive
def my_lineup(who: tuple[Classroom, Student] = Depends(require_student)) -> dict:
    _, student = who
    return {"valves": student.session.lineup()}


@app.get("/api/me/schematic")
@exclusive
def my_schematic(who: tuple[Classroom, Student] = Depends(require_student)) -> dict:
    _, student = who
    return student.session.schematic()


@app.get("/api/me/killsheet")
@exclusive
def my_kill_sheet(who: tuple[Classroom, Student] = Depends(require_student)) -> dict:
    _, student = who
    return student.session.kill_sheet()
