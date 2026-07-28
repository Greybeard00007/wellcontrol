"""Keeping a run after the class has gone home.

A recording is **data, not a paused engine**. It holds the trace, the event log,
the kill sheet the student built and the assessment, and replaying it means
drawing those again -- not re-running physics. That is the honest design: the
run already happened, and a replay that re-simulated it could disagree with what
the student actually saw, which would make it useless as evidence.

Two things follow that are worth keeping:

* **Recording is unconditional.** Storage is not the constraint -- a three-hour
  kill sampled onto the trace is a few hundred kilobytes -- and a debrief you
  have to remember to switch on is a debrief you do not have when you need it.
* **A recording outlives its class.** Classes are in memory and a restart loses
  them; recordings are files, so the thing worth keeping is the thing that
  survives.

Files are JSON, one per run, named so they sort by time and can be found by
eye in a directory listing. No database: the access pattern is "list them" and
"open one", which a directory does perfectly well.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

#: Where recordings live. The container mounts a volume here.
DEFAULT_DATA_DIR = "/data"

#: Trailing samples kept. A three-hour kill at the trace's own sampling is a
#: few thousand rows; this is a backstop against a runaway, not a budget.
MAX_TRACE_ROWS = 20_000


def data_dir() -> Path:
    """The directory recordings are written to, from the environment."""
    return Path(os.environ.get("WELLCONTROL_DATA_DIR", DEFAULT_DATA_DIR))


def _safe(text: str) -> str:
    """A filename fragment from free text a student typed.

    **No dots.** Separators alone would be enough to stop traversal -- and they
    are stripped -- but a name like ``../../etc/passwd`` still comes through as
    ``..-..-etc-passwd``, which is a filename nobody should have to reason
    about. The extension is added separately, so the fragment never needs one.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", (text or "").strip())
    return cleaned.strip("-")[:40] or "unnamed"


@dataclass
class Recording:
    """One student's run, complete enough to debrief from months later."""

    recording_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: Wall-clock ISO timestamp, supplied by the caller -- this module has no
    #: opinion about clocks and the engine's own time is simulated.
    recorded_at: str = ""
    classroom_name: str = ""
    classroom_id: str = ""
    student_name: str = ""
    student_id: str = ""
    scenario: dict | None = None
    #: Simulated seconds the run covered.
    duration_s: float = 0.0
    trace: list = field(default_factory=list)
    events: list = field(default_factory=list)
    kill_sheet: dict = field(default_factory=dict)
    debrief: dict = field(default_factory=dict)

    @property
    def filename(self) -> str:
        stamp = re.sub(r"[^0-9]", "", self.recorded_at)[:14] or "00000000000000"
        return f"{stamp}-{_safe(self.student_name)}-{self.recording_id}.json"

    def summary(self) -> dict:
        """The listing row. Small on purpose: a class of fifteen makes fifteen
        of these per exercise and the list is read far more often than a run."""
        return {
            "recording_id": self.recording_id,
            "recorded_at": self.recorded_at,
            "classroom_name": self.classroom_name,
            "student_name": self.student_name,
            "scenario": (self.scenario or {}).get("name"),
            "duration_s": round(self.duration_s, 1),
            "grade": self.debrief.get("grade"),
            "passed": self.debrief.get("passed"),
            "killed": self.debrief.get("killed"),
            "samples": len(self.trace),
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Recording":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def record_session(
    session,
    *,
    recorded_at: str,
    classroom_name: str = "",
    classroom_id: str = "",
    student_name: str = "",
    student_id: str = "",
) -> Recording:
    """Capture a session as it stands.

    Takes the debrief and the kill sheet as well as the trace, because those
    are what a debrief is actually conducted from -- and because computing them
    later would need the engine, which is exactly what a recording exists to
    avoid keeping.
    """
    return Recording(
        recorded_at=recorded_at,
        classroom_name=classroom_name,
        classroom_id=classroom_id,
        student_name=student_name,
        student_id=student_id,
        scenario=(
            None if session.scenario is None else session.scenario.summary()
        ),
        duration_s=session.simulator.state.time_s,
        trace=session.trace(limit=MAX_TRACE_ROWS),
        events=session.events_since(0)["events"],
        kill_sheet=session.kill_sheet(),
        debrief=session.debrief(),
    )


class RecordingStore:
    """Recordings on disk, newest first.

    Tolerant by design: a directory that cannot be created, or a file that will
    not parse, must not take the server down mid-class. Saving reports failure
    to the caller; listing skips what it cannot read.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory else data_dir()

    def _ensure(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    def save(self, recording: Recording) -> Path | None:
        """Write it out. None if the directory is not writable.

        **Losing a recording must not lose the class.** A read-only volume is a
        deployment problem, not a reason to fail the request that triggered it.
        """
        if not self._ensure():
            return None
        path = self.directory / recording.filename
        try:
            path.write_text(
                json.dumps(recording.to_dict(), indent=1), encoding="utf-8"
            )
        except OSError:
            return None
        return path

    def list(self, limit: int = 200) -> list[dict]:
        """Summaries, newest first. Unreadable files are skipped, not raised."""
        if not self.directory.is_dir():
            return []
        rows = []
        for path in sorted(self.directory.glob("*.json"), reverse=True)[:limit]:
            recording = self._read(path)
            if recording is not None:
                rows.append(recording.summary())
        return rows

    def get(self, recording_id: str) -> Recording:
        """Raises KeyError if there is no such recording."""
        for path in self.directory.glob(f"*-{recording_id}.json"):
            recording = self._read(path)
            if recording is not None:
                return recording
        raise KeyError(f"No recording '{recording_id}'")

    def delete(self, recording_id: str) -> bool:
        for path in self.directory.glob(f"*-{recording_id}.json"):
            try:
                path.unlink()
                return True
            except OSError:
                return False
        return False

    @staticmethod
    def _read(path: Path) -> Recording | None:
        try:
            return Recording.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError):
            return None
