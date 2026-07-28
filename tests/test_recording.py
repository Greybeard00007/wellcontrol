"""Keeping a run after the class has gone home.

Two properties matter and both are tested here: a recording holds everything a
debrief is actually conducted from, and **failing to write one never takes the
class down** -- a read-only volume is a deployment problem, not a reason to
refuse to end a class.
"""

import json

import pytest

from wellcontrol.sim import Session
from wellcontrol.sim.recording import (
    Recording,
    RecordingStore,
    record_session,
)


def flown_session() -> Session:
    session = Session.for_configuration()
    session.simulator.choke.command(35.0)
    session.simulator.run(120.0, dt=0.05)
    return session


class TestCapturing:
    def test_carries_what_a_debrief_needs(self):
        """Trace, events, kill sheet and verdict -- not just the numbers.

        Computing the debrief later would need the engine, which is precisely
        what a recording exists to avoid keeping.
        """
        recording = record_session(
            flown_session(),
            recorded_at="2026-07-27T14:00:00+00:00",
            classroom_name="Well Control 101",
            student_name="Brian",
        )
        assert recording.trace, "the trace should not be empty"
        assert recording.duration_s > 100
        assert recording.kill_sheet["available"] is True
        assert recording.debrief["grade"]
        assert recording.student_name == "Brian"

    def test_the_summary_is_small(self):
        """A class of fifteen makes fifteen per exercise, and the list is read
        far more often than a run."""
        recording = record_session(
            flown_session(), recorded_at="2026-07-27T14:00:00+00:00",
            student_name="Brian",
        )
        summary = recording.summary()
        assert "trace" not in summary and "events" not in summary
        assert summary["samples"] == len(recording.trace)
        assert summary["grade"] == recording.debrief["grade"]

    def test_the_filename_sorts_by_time_and_names_the_student(self, tmp_path):
        recording = Recording(
            recorded_at="2026-07-27T14:05:09+00:00", student_name="Dana Ó'Brien",
        )
        assert recording.filename.startswith("20260727140509")
        assert "Dana" in recording.filename
        assert recording.filename.endswith(".json")
        # Whatever the student typed must not escape the directory.
        assert "/" not in recording.filename and "\\" not in recording.filename

    def test_a_nasty_name_cannot_escape_the_directory(self):
        recording = Recording(student_name="../../etc/passwd")
        assert ".." not in recording.filename.replace("..json", "")
        assert "/" not in recording.filename


class TestTheStore:
    def test_round_trip(self, tmp_path):
        store = RecordingStore(tmp_path)
        recording = record_session(
            flown_session(), recorded_at="2026-07-27T14:00:00+00:00",
            student_name="Brian",
        )
        path = store.save(recording)
        assert path is not None and path.exists()

        loaded = store.get(recording.recording_id)
        assert loaded.student_name == "Brian"
        assert len(loaded.trace) == len(recording.trace)
        assert loaded.debrief["grade"] == recording.debrief["grade"]

    def test_listed_newest_first(self, tmp_path):
        store = RecordingStore(tmp_path)
        for stamp, name in [
            ("2026-07-27T09:00:00+00:00", "Early"),
            ("2026-07-27T17:00:00+00:00", "Late"),
            ("2026-07-27T13:00:00+00:00", "Middle"),
        ]:
            store.save(Recording(recorded_at=stamp, student_name=name))
        assert [r["student_name"] for r in store.list()] == [
            "Late", "Middle", "Early"
        ]

    def test_an_unreadable_file_is_skipped_not_raised(self, tmp_path):
        """One corrupt file must not make the whole list unavailable."""
        store = RecordingStore(tmp_path)
        store.save(Recording(recorded_at="2026-07-27T09:00:00+00:00",
                             student_name="Good"))
        (tmp_path / "20260727100000-broken-abcdef123456.json").write_text(
            "{not json at all", encoding="utf-8"
        )
        rows = store.list()
        assert [r["student_name"] for r in rows] == ["Good"]

    def test_missing_directory_lists_empty(self, tmp_path):
        assert RecordingStore(tmp_path / "nope").list() == []

    def test_unknown_id_raises(self, tmp_path):
        with pytest.raises(KeyError):
            RecordingStore(tmp_path).get("deadbeef")

    def test_delete(self, tmp_path):
        store = RecordingStore(tmp_path)
        recording = Recording(recorded_at="2026-07-27T09:00:00+00:00")
        store.save(recording)
        assert store.delete(recording.recording_id)
        assert not store.delete(recording.recording_id)

    def test_an_unwritable_directory_reports_rather_than_raises(self, tmp_path):
        """Losing a recording must not lose the class.

        A file where the directory should be is the cheapest way to make
        mkdir fail on every platform.
        """
        blocked = tmp_path / "blocked"
        blocked.write_text("I am a file, not a directory", encoding="utf-8")
        store = RecordingStore(blocked)
        assert store.save(Recording()) is None

    def test_the_file_is_plain_json(self, tmp_path):
        """Readable without this program, which is the point of a file."""
        store = RecordingStore(tmp_path)
        recording = record_session(
            flown_session(), recorded_at="2026-07-27T14:00:00+00:00",
            student_name="Brian",
        )
        path = store.save(recording)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["student_name"] == "Brian"
        assert isinstance(data["trace"], list)
