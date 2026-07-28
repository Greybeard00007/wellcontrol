"""The classroom HTTP layer: instructor login, class codes, and /api/me.

Skips itself when the ``[sim]`` extra is absent, like ``test_sim_api.py``, so
the dependency-free suite is unaffected.

The *time model* is tested against a fake clock in ``test_classroom.py``.
What is tested here is transport and authorisation -- who can reach what --
plus one end-to-end check that the background driver is actually wired up.
"""

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from wellcontrol.sim import api  # noqa: E402
from wellcontrol.sim.auth import RateLimiter, hash_password  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def clean_server():
    """A fresh server per test.

    The stores and limiters are module-level singletons, so without this a
    class created in one test would be joinable from the next and the rate
    limiter would carry attempts across tests.
    """
    api.settings.password_hash = hash_password(PASSWORD)
    api.classrooms._classrooms.clear()
    api.join_limiter = RateLimiter(limit=12, window_s=60.0)
    api.login_limiter = RateLimiter(limit=6, window_s=300.0)
    yield
    api.classrooms._classrooms.clear()


@pytest.fixture
def client():
    return TestClient(api.app)


def instructor_token(client) -> str:
    response = client.post("/api/instructor/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return response.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_class(client, token, **body) -> dict:
    body.setdefault("name", "Well Control 101")
    response = client.post(
        "/api/instructor/classes", json=body, headers=auth(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


def join(client, code, name="Brian") -> dict:
    response = client.post("/api/join", json={"code": code, "name": name})
    assert response.status_code == 200, response.text
    return response.json()


class TestInstructorLogin:
    def test_right_password_returns_a_token(self, client):
        body = client.post(
            "/api/instructor/login", json={"password": PASSWORD}
        ).json()
        assert body["token"]

    def test_wrong_password_rejected(self, client):
        response = client.post(
            "/api/instructor/login", json={"password": "nope"}
        )
        assert response.status_code == 401

    def test_sets_an_httponly_cookie(self, client):
        """The browser gets a cookie no script can read; API clients use the
        bearer header instead."""
        response = client.post(
            "/api/instructor/login", json={"password": PASSWORD}
        )
        cookie = response.headers["set-cookie"]
        assert api.INSTRUCTOR_COOKIE in cookie
        assert "HttpOnly" in cookie

    def test_repeated_failures_are_throttled(self, client):
        """The one durable credential should not be brute-forceable."""
        api.login_limiter = RateLimiter(limit=3, window_s=300.0)
        codes = [
            client.post("/api/instructor/login", json={"password": "x"}).status_code
            for _ in range(4)
        ]
        assert codes == [401, 401, 401, 429]

    def test_instructor_routes_need_the_token(self, client):
        assert client.get("/api/instructor/classes").status_code == 401
        assert client.post("/api/instructor/classes", json={}).status_code == 401


class TestCreatingAClass:
    def test_returns_a_code(self, client):
        token = instructor_token(client)
        summary = make_class(client, token)
        assert summary["code"]
        assert summary["student_count"] == 0

    def test_a_class_starts_paused(self, client):
        """Students join and find their screens before the well moves."""
        summary = make_class(client, instructor_token(client))
        assert summary["paused"] is True
        assert summary["speed"] == 0.0

    def test_listed_for_the_instructor(self, client):
        token = instructor_token(client)
        make_class(client, token, name="Morning")
        make_class(client, token, name="Afternoon")
        listed = client.get("/api/instructor/classes", headers=auth(token)).json()
        assert {c["name"] for c in listed["classes"]} == {"Morning", "Afternoon"}

    def test_unknown_scenario_is_a_422(self, client):
        token = instructor_token(client)
        response = client.post(
            "/api/instructor/classes",
            json={"scenario": "not-a-real-exercise"},
            headers=auth(token),
        )
        assert response.status_code == 422


class TestJoining:
    def test_a_student_joins_with_the_code(self, client):
        summary = make_class(client, instructor_token(client))
        body = join(client, summary["code"])
        assert body["token"]
        assert body["student"]["name"] == "Brian"

    def test_the_code_is_not_handed_back_to_the_room(self, client):
        summary = make_class(client, instructor_token(client))
        body = join(client, summary["code"])
        assert "code" not in body["classroom"]

    def test_a_typed_code_is_forgiving(self, client):
        """Students type it lowercase, without the hyphen, with a space."""
        summary = make_class(client, instructor_token(client))
        assert join(client, summary["code"].replace("-", "").lower())["token"]

    def test_wrong_code_is_a_404(self, client):
        assert client.post(
            "/api/join", json={"code": "WRON-GXXX", "name": "A"}
        ).status_code == 404

    def test_a_closed_class_looks_the_same_as_a_wrong_code(self, client):
        """Somebody probing must not be able to tell a stale code from a guess."""
        token = instructor_token(client)
        summary = make_class(client, token)
        client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/close",
            headers=auth(token),
        )
        response = client.post(
            "/api/join", json={"code": summary["code"], "name": "Late"}
        )
        assert response.status_code == 404

    def test_a_full_class_says_so(self, client):
        token = instructor_token(client)
        summary = make_class(client, token, capacity=1)
        join(client, summary["code"], "First")
        response = client.post(
            "/api/join", json={"code": summary["code"], "name": "Second"}
        )
        assert response.status_code == 409
        assert "full" in response.json()["detail"].lower()

    def test_guessing_is_throttled(self, client):
        api.join_limiter = RateLimiter(limit=2, window_s=60.0)
        codes = [
            client.post(
                "/api/join", json={"code": "AAAA-BBBB", "name": "x"}
            ).status_code
            for _ in range(3)
        ]
        assert codes == [404, 404, 429]


class TestStudentAccess:
    def test_a_student_reaches_their_own_well(self, client):
        summary = make_class(client, instructor_token(client))
        token = join(client, summary["code"])["token"]
        snapshot = client.get("/api/me", headers=auth(token)).json()
        assert snapshot["gauges"]["casing_psi"] >= 0
        assert snapshot["classroom"]["name"] == "Well Control 101"

    def test_students_have_separate_wells(self, client):
        summary = make_class(client, instructor_token(client))
        a = join(client, summary["code"], "A")["token"]
        b = join(client, summary["code"], "B")["token"]

        client.post("/api/me/choke", json={"percent": 80.0}, headers=auth(a))
        assert client.get("/api/me", headers=auth(a)).json()[
            "gauges"]["choke_commanded"] == 80.0
        assert client.get("/api/me", headers=auth(b)).json()[
            "gauges"]["choke_commanded"] == 0.0

    def test_no_token_is_a_401(self, client):
        assert client.get("/api/me").status_code == 401

    def test_an_instructor_token_is_not_a_student_token(self, client):
        """Roles are separate. An instructor has no seat in the class."""
        token = instructor_token(client)
        assert client.get("/api/me", headers=auth(token)).status_code == 401

    def test_a_student_token_is_not_an_instructor_token(self, client):
        summary = make_class(client, instructor_token(client))
        token = join(client, summary["code"])["token"]
        assert client.get(
            "/api/instructor/classes", headers=auth(token)
        ).status_code == 401

    def test_a_student_cannot_set_the_class_speed(self, client):
        """A class is flown together, so there is no student speed control at
        all -- not merely a forbidden one."""
        summary = make_class(client, instructor_token(client))
        token = join(client, summary["code"])["token"]
        assert client.post(
            "/api/me/speed", json={"speed": 60.0}, headers=auth(token)
        ).status_code in (404, 405)

    def test_a_student_can_stop_and_restart_their_own_well(self, client):
        """The transport is theirs even though the multiplier is not.

        They have to be able to stop the well to read a kill sheet or think.
        What they cannot do is choose how fast it runs when it is running:
        resuming puts them back on the instructor's clock, whatever that is
        now, rather than on a figure of their own.
        """
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        token = join(client, summary["code"])["token"]
        client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/speed",
            json={"speed": 10.0}, headers=auth(instructor),
        )

        paused = client.post(
            "/api/me/pause", json={"paused": True}, headers=auth(token)
        )
        assert paused.status_code == 200
        assert paused.json()["gauges"]["paused"] is True
        assert paused.json()["gauges"]["speed"] == 0

        resumed = client.post(
            "/api/me/pause", json={"paused": False}, headers=auth(token)
        )
        assert resumed.json()["gauges"]["paused"] is False
        assert resumed.json()["gauges"]["speed"] == 10.0

    def test_pausing_is_one_student_not_the_class(self, client):
        """Otherwise a student could stop the room by stopping themselves."""
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        one = join(client, summary["code"], "One")["token"]
        two = join(client, summary["code"], "Two")["token"]
        client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/speed",
            json={"speed": 10.0}, headers=auth(instructor),
        )

        client.post("/api/me/pause", json={"paused": True}, headers=auth(one))
        assert client.get("/api/me", headers=auth(one)).json()["gauges"]["paused"]
        assert not client.get("/api/me", headers=auth(two)).json()["gauges"]["paused"]

    def test_a_removed_student_is_locked_out_at_once(self, client):
        """There is no token blacklist and no need for one: the roster is
        checked on every request."""
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        body = join(client, summary["code"])
        token = body["token"]
        assert client.get("/api/me", headers=auth(token)).status_code == 200

        client.delete(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{body['student']['student_id']}",
            headers=auth(instructor),
        )
        assert client.get("/api/me", headers=auth(token)).status_code == 401

    def test_ending_the_class_locks_everyone_out(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        token = join(client, summary["code"])["token"]
        client.delete(
            f"/api/instructor/classes/{summary['classroom_id']}",
            headers=auth(instructor),
        )
        assert client.get("/api/me", headers=auth(token)).status_code == 401


class TestTheMonitor:
    def test_shows_every_student(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        for name in ("Charlie", "alice", "Bob"):
            join(client, summary["code"], name)

        grid = client.get(
            f"/api/instructor/classes/{summary['classroom_id']}/monitor",
            headers=auth(instructor),
        ).json()
        assert [t["name"] for t in grid["students"]] == ["alice", "Bob", "Charlie"]

    def test_the_instructor_sees_through_the_students_mask(self, client):
        """On a drilling start the student's gauges withhold the downhole
        truth. The tile must not."""
        instructor = instructor_token(client)
        summary = make_class(client, instructor, start="drilling")
        token = join(client, summary["code"])["token"]

        gauges = client.get("/api/me", headers=auth(token)).json()["gauges"]
        assert gauges["bottomhole_psi"] is None

        tile = client.get(
            f"/api/instructor/classes/{summary['classroom_id']}/monitor",
            headers=auth(instructor),
        ).json()["students"][0]
        assert tile["bottomhole_psi"] > 0
        assert tile["downhole_visible"] is False

    def test_needs_the_instructor_token(self, client):
        """A *fresh* client, deliberately.

        TestClient keeps cookies, and the login that created the class set one
        -- so reusing it here would have carried the instructor's credential
        into a request meant to have none, and the test would pass whatever the
        server did.
        """
        summary = make_class(client, instructor_token(client))
        stranger = TestClient(api.app)
        assert stranger.get(
            f"/api/instructor/classes/{summary['classroom_id']}/monitor"
        ).status_code == 401

    def test_restarting_a_student_gives_them_a_fresh_well(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        body = join(client, summary["code"])
        token = body["token"]

        client.post("/api/me/choke", json={"percent": 60.0}, headers=auth(token))
        response = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{body['student']['student_id']}/restart",
            headers=auth(instructor),
        )
        assert response.status_code == 200
        gauges = client.get("/api/me", headers=auth(token)).json()["gauges"]
        assert gauges["choke_commanded"] == 0.0, "should be a new well"


class TestSpeedControl:
    def test_the_instructor_moves_the_whole_class(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        cid = summary["classroom_id"]
        join(client, summary["code"], "A")
        join(client, summary["code"], "B")

        client.post(
            f"/api/instructor/classes/{cid}/speed",
            json={"speed": 10.0},
            headers=auth(instructor),
        )
        grid = client.get(
            f"/api/instructor/classes/{cid}/monitor", headers=auth(instructor)
        ).json()
        assert all(tile["speed"] == 10.0 for tile in grid["students"])
        assert not any(tile["paused"] for tile in grid["students"])

    def test_pause_and_resume(self, client):
        instructor = instructor_token(client)
        cid = make_class(client, instructor)["classroom_id"]
        client.post(
            f"/api/instructor/classes/{cid}/speed",
            json={"speed": 10.0},
            headers=auth(instructor),
        )
        paused = client.post(
            f"/api/instructor/classes/{cid}/pause", headers=auth(instructor)
        ).json()
        assert paused["paused"] is True

        resumed = client.post(
            f"/api/instructor/classes/{cid}/resume", headers=auth(instructor)
        ).json()
        assert resumed["speed"] == 10.0, "resume returns to the previous speed"


class TestTheMudSystemControls:
    """The stage 2 instruments, reachable by a student in a class."""

    def student(self, client):
        summary = make_class(client, instructor_token(client))
        return summary, join(client, summary["code"])["token"]

    def test_the_gauges_carry_the_mud_system(self, client):
        _, token = self.student(client)
        gauges = client.get("/api/me", headers=auth(token)).json()["gauges"]

        assert gauges["trip_tank"]["capacity_bbl"] > 0
        assert gauges["pits"]["reserve_bbl"] > 0
        assert gauges["return_flow"]["differential_bpm"] == 0
        assert len(gauges["pumps"]) == 2
        assert set(gauges["chokes"]) == {
            "choke_1", "choke_2", "manual_choke_1", "manual_choke_2"
        }

    def test_each_choke_is_positioned_separately(self, client):
        """Asserted on the command, not the travel.

        A real choke takes seconds to move and the class here is paused, so
        nothing has travelled anywhere. The command is what the student asked
        for and it must land on the right handle.
        """
        _, token = self.student(client)
        client.post(
            "/api/me/choke",
            json={"percent": 40.0, "choke": "choke_2"},
            headers=auth(token),
        )
        chokes = client.get(
            "/api/me", headers=auth(token)
        ).json()["gauges"]["chokes"]
        assert chokes["choke_2"]["commanded"] == 40.0
        assert chokes["choke_1"]["commanded"] == 0.0

    def test_an_unknown_choke_is_a_404(self, client):
        _, token = self.student(client)
        assert client.post(
            "/api/me/choke",
            json={"percent": 10.0, "choke": "choke_9"},
            headers=auth(token),
        ).status_code == 404

    def test_the_second_pump_is_reachable(self, client):
        _, token = self.student(client)
        client.post(
            "/api/me/pump", json={"spm": 30.0, "pump": 2}, headers=auth(token)
        )
        pumps = client.get("/api/me", headers=auth(token)).json()["gauges"]["pumps"]
        assert pumps[1]["commanded"] == 30.0
        assert pumps[0]["commanded"] == 0.0

    def test_putting_the_trip_tank_in_service(self, client):
        """Valve and pump both, which is the lineup a driller is taught."""
        _, token = self.student(client)
        client.post(
            "/api/me/valve",
            json={"name": "trip_tank_valve", "state": "open"},
            headers=auth(token),
        )
        assert not client.get(
            "/api/me", headers=auth(token)
        ).json()["gauges"]["trip_tank_in_service"]

        client.post(
            "/api/me/transfer",
            json={"name": "trip_tank_fillup", "running": True},
            headers=auth(token),
        )
        assert client.get(
            "/api/me", headers=auth(token)
        ).json()["gauges"]["trip_tank_in_service"]

    def test_zeroing_re_marks_the_gauge(self, client):
        _, token = self.student(client)
        session = api.classrooms.all[0].students and list(
            api.classrooms.all[0].students.values()
        )[0].session
        session.simulator.pits.add(12.0)
        assert client.get(
            "/api/me", headers=auth(token)
        ).json()["gauges"]["pit_gain_bbl"] == 12.0

        client.post(
            "/api/me/zero", json={"instrument": "pits"}, headers=auth(token)
        )
        assert client.get(
            "/api/me", headers=auth(token)
        ).json()["gauges"]["pit_gain_bbl"] == 0.0

    def test_an_unknown_instrument_is_a_404(self, client):
        _, token = self.student(client)
        assert client.post(
            "/api/me/zero", json={"instrument": "hookload"}, headers=auth(token)
        ).status_code == 404

    def test_disarming_an_alarm_leaves_the_lamp(self, client):
        """So the instructor can see what the student silenced."""
        _, token = self.student(client)
        client.post(
            "/api/me/alarm",
            json={"name": "pit_deviation", "high": 2.0, "armed": False},
            headers=auth(token),
        )
        session = list(api.classrooms.all[0].students.values())[0].session
        session.simulator.pits.add(20.0)
        session.simulator.pits.update_alarms()

        alarm = client.get(
            "/api/me", headers=auth(token)
        ).json()["gauges"]["pits"]["alarm"]
        assert alarm["high_active"] is True
        assert alarm["sounding"] is False

    def test_the_instructor_tile_shows_the_trip_tank(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        join(client, summary["code"])

        tile = client.get(
            f"/api/instructor/classes/{summary['classroom_id']}/monitor",
            headers=auth(instructor),
        ).json()["students"][0]
        assert tile["trip_tank_in_service"] is False
        assert "alarms_sounding" in tile


class TestLiveFaultInjection:
    """Breaking something by hand, without the student being able to tell.

    A student who can detect that the instructor just did something to them
    starts flying the instructor rather than the well, which is the opposite of
    the exercise.
    """

    def open_class(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        body = join(client, summary["code"])
        return instructor, summary, body

    def test_a_fault_can_be_thrown_at_one_student(self, client):
        instructor, summary, body = self.open_class(client)
        other = join(client, summary["code"], "Other")

        response = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/faults",
            json={
                "kind": "choke_plugged",
                "student_id": body["student"]["student_id"],
            },
            headers=auth(instructor),
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1

        students = list(api.classrooms.all[0].students.values())
        hit = [s for s in students if s.student_id == body["student"]["student_id"]][0]
        missed = [s for s in students if s.name == "Other"][0]
        assert hit.session.simulator.choke.full_open_area_in2 < 0.1
        assert missed.session.simulator.choke.full_open_area_in2 > 1.0

    def test_a_fault_can_be_thrown_at_the_whole_class(self, client):
        instructor, summary, _ = self.open_class(client)
        join(client, summary["code"], "Second")

        response = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/faults",
            json={"kind": "accumulator_leak"},
            headers=auth(instructor),
        )
        assert response.json()["count"] == 2
        for student in api.classrooms.all[0].students.values():
            assert student.session.simulator.accumulator.pressure_psi == 0.0

    def test_the_student_is_told_only_what_the_rig_would_tell_them(self, client):
        instructor, summary, body = self.open_class(client)
        token = body["token"]
        client.get("/api/me", headers=auth(token))          # drain existing events

        client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/faults",
            json={"kind": "pump_failure", "message": "Pump 1 tripped."},
            headers=auth(instructor),
        )
        snapshot = client.get("/api/me", headers=auth(token)).json()
        text = json.dumps(snapshot).lower()

        messages = [e["message"] for e in snapshot["events"]["events"]]
        assert any("pump 1 tripped" in m.lower() for m in messages), messages

        # Nothing anywhere in what the student can see says a human did it.
        for giveaway in (
            "instructor", "injected", "inject", "thrown", "fault_kind",
            "admin", "manual fault",
        ):
            assert giveaway not in text, f"{giveaway!r} leaked to the student"

    def test_a_thrown_fault_looks_like_a_scheduled_one(self, client):
        """The strongest form of the guarantee: same event, either way.

        One session gets the fault because its scheduled moment arrived; the
        other because an instructor chose the moment. What reaches the student
        must be identical.
        """
        from wellcontrol.sim import Session
        from wellcontrol.sim.scenario import Fault, FaultKind

        fault = Fault(kind=FaultKind.CHOKE_PLUGGED, at_seconds=1.0,
                      message="Choke not responding.")

        scheduled = Session.for_configuration()
        scheduled.simulator.faults = (fault,)
        scheduled.simulator.run(5.0, dt=0.05)

        thrown = Session.for_configuration()
        thrown.simulator.run(5.0, dt=0.05)
        thrown.simulator.inject_fault(fault)

        def signature(session):
            return [
                (e["kind"], e["severity"], e["message"])
                for e in session.events_since(0)["events"]
                if "choke" in e["message"].lower()
            ]

        assert signature(scheduled) == signature(thrown)
        assert signature(thrown), "the fault should have been logged at all"

    def test_an_unknown_fault_kind_is_a_422(self, client):
        instructor, summary, _ = self.open_class(client)
        assert client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/faults",
            json={"kind": "meteor_strike"},
            headers=auth(instructor),
        ).status_code == 422

    def test_students_cannot_throw_faults(self, client):
        _, summary, body = self.open_class(client)
        assert client.post(
            f"/api/instructor/classes/{summary['classroom_id']}/faults",
            json={"kind": "pump_failure"},
            headers=auth(body["token"]),
        ).status_code == 401

    def test_the_fault_catalogue_names_kinds_not_exercises(self, client):
        """Listing what *can* break gives nothing away about what will."""
        kinds = client.get("/api/fault-kinds").json()["kinds"]
        assert {k["kind"] for k in kinds} >= {
            "pump_failure", "choke_plugged", "weak_shoe"
        }


class TestStudentDrillDown:
    def test_the_instructor_sees_the_students_whole_screen(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor, start="drilling")
        body = join(client, summary["code"])

        detail = client.get(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{body['student']['student_id']}",
            headers=auth(instructor),
        ).json()

        assert detail["student"]["name"] == "Brian"
        assert "gauges" in detail and "trace" in detail and "events" in detail
        assert "kill_sheet" in detail
        # The student's own gauges stay masked; the instructor view does not.
        assert detail["gauges"]["bottomhole_psi"] is None
        assert detail["instructor_view"]["bottomhole_psi"] > 0

    def test_an_unknown_student_is_a_404(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        assert client.get(
            f"/api/instructor/classes/{summary['classroom_id']}/students/nope",
            headers=auth(instructor),
        ).status_code == 404

    def test_a_student_cannot_read_another_students_screen(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        first = join(client, summary["code"], "First")
        second = join(client, summary["code"], "Second")

        assert client.get(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{second['student']['student_id']}",
            headers=auth(first["token"]),
        ).status_code == 401


class TestRecordings:
    """A run has to outlive the class it came from.

    Classes are in memory and a restart loses them; recordings are files, so
    the thing worth keeping is the thing that survives.
    """

    @pytest.fixture(autouse=True)
    def temp_store(self, tmp_path):
        """Never write to the real /data from a test."""
        from wellcontrol.sim.recording import RecordingStore

        original = api.recordings
        api.recordings = RecordingStore(tmp_path)
        yield
        api.recordings = original

    def running_class(self, client):
        instructor = instructor_token(client)
        summary = make_class(client, instructor)
        body = join(client, summary["code"])
        return instructor, summary, body

    def test_restarting_a_student_keeps_the_run_first(self, client):
        """The run about to be thrown away is the one worth keeping."""
        instructor, summary, body = self.running_class(client)
        sid = body["student"]["student_id"]

        response = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{sid}/restart",
            headers=auth(instructor),
        )
        assert response.status_code == 200
        assert response.json()["recording"] is not None

        listed = client.get(
            "/api/instructor/recordings", headers=auth(instructor)
        ).json()["recordings"]
        assert len(listed) == 1
        assert listed[0]["student_name"] == "Brian"

    def test_ending_a_class_keeps_everybody(self, client):
        instructor, summary, _ = self.running_class(client)
        join(client, summary["code"], "Second")

        client.delete(
            f"/api/instructor/classes/{summary['classroom_id']}",
            headers=auth(instructor),
        )
        listed = client.get(
            "/api/instructor/recordings", headers=auth(instructor)
        ).json()["recordings"]
        assert {r["student_name"] for r in listed} == {"Brian", "Second"}

    def test_a_run_can_be_kept_without_ending_it(self, client):
        instructor, summary, body = self.running_class(client)
        sid = body["student"]["student_id"]

        response = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{sid}/record",
            headers=auth(instructor),
        )
        assert response.status_code == 201
        # The student flies on.
        assert client.get("/api/me", headers=auth(body["token"])).status_code == 200

    def test_a_recording_carries_what_a_debrief_needs(self, client):
        instructor, summary, body = self.running_class(client)
        made = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{body['student']['student_id']}/record",
            headers=auth(instructor),
        ).json()

        full = client.get(
            f"/api/instructor/recordings/{made['recording_id']}",
            headers=auth(instructor),
        ).json()
        assert full["student_name"] == "Brian"
        assert "trace" in full and "events" in full
        assert "kill_sheet" in full and "debrief" in full

    def test_recordings_outlive_the_class(self, client):
        instructor, summary, body = self.running_class(client)
        client.post(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{body['student']['student_id']}/record",
            headers=auth(instructor),
        )
        client.delete(
            f"/api/instructor/classes/{summary['classroom_id']}",
            headers=auth(instructor),
        )
        assert client.get(
            "/api/instructor/recordings", headers=auth(instructor)
        ).json()["recordings"]

    def test_deleting_one(self, client):
        instructor, summary, body = self.running_class(client)
        made = client.post(
            f"/api/instructor/classes/{summary['classroom_id']}"
            f"/students/{body['student']['student_id']}/record",
            headers=auth(instructor),
        ).json()
        assert client.delete(
            f"/api/instructor/recordings/{made['recording_id']}",
            headers=auth(instructor),
        ).status_code == 204
        assert client.delete(
            f"/api/instructor/recordings/{made['recording_id']}",
            headers=auth(instructor),
        ).status_code == 404

    def test_students_cannot_reach_recordings(self, client):
        _, _, body = self.running_class(client)
        assert client.get(
            "/api/instructor/recordings", headers=auth(body["token"])
        ).status_code == 401


class TestConcurrency:
    """The engines have two callers now, and they really do overlap.

    A sync FastAPI handler runs in a threadpool worker while the clock driver
    runs on the event loop, so a student's request and a tick can be inside the
    same engine at the same instant. Cheapest symptom is elapsed time counted
    twice; worst is a half-rebuilt fluid column, or a dictionary mutated while
    ``tick_all`` iterates it.
    """

    def test_every_classroom_route_holds_the_engine_lock(self, client):
        """Structural, because the race itself is not reliably reproducible.

        Catches the failure that will actually happen: somebody adds a route
        next to these and does not think about the clock.
        """
        unguarded = []
        for route in api.app.routes:
            path = str(getattr(route, "path", ""))
            touches_a_class = (
                path.startswith("/api/me")
                or path.startswith("/api/instructor/classes")
                or path == "/api/join"
            )
            if not touches_a_class:
                continue
            if not getattr(route.endpoint, "holds_engine_lock", False):
                unguarded.append(f"{sorted(route.methods)[0]} {path}")

        assert not unguarded, (
            "these touch a class without @exclusive, so the clock driver can "
            f"be inside the same engine at the same time: {unguarded}"
        )

    def test_the_solo_routes_are_deliberately_unguarded(self, client):
        """Nothing else advances a solo session, so the lock would be noise."""
        solo = [
            r for r in api.app.routes
            if str(getattr(r, "path", "")).startswith("/api/sessions")
        ]
        assert solo, "expected the solo routes to still exist"
        assert not any(
            getattr(r.endpoint, "holds_engine_lock", False) for r in solo
        )

    def test_joining_while_the_clock_ticks(self):
        """The locking discipline survives contention without deadlocking.

        The lock is a *server* discipline, not something ``Classroom`` does to
        itself -- direct library use is single-threaded, and self-locking every
        method would be a cost paid by every test for a hazard only the web
        layer has. So this drives it the way the API does: request-shaped work
        inside ``store.exclusive()``, the clock ticking against it.

        **Be clear about what this does and does not prove.** It is a deadlock
        and consistency smoke test -- the reentrant lock is taken by both the
        driver and request-shaped work, and a wrong order or a non-reentrant
        lock would hang here. It is *not* a detector for the race itself:
        removing the ``with`` was tried, and this still passes, because the
        GIL happens not to switch at the damaging instant. The real guard
        against forgetting the lock is the structural test above.
        """
        import threading

        from wellcontrol.sim.classroom import ClassroomStore

        store = ClassroomStore()
        classroom = store.create(name="Stress")
        classroom.set_speed(1.0)
        errors: list[Exception] = []
        stop = threading.Event()

        def ticker():
            try:
                while not stop.is_set():
                    store.tick_all()
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                errors.append(exc)

        def requester():
            try:
                for i in range(150):
                    with store.exclusive():          # what @exclusive does
                        student = classroom.join(f"S{i}")
                        classroom.monitor()
                        classroom.remove(student.student_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        clock = threading.Thread(target=ticker)
        workers = [threading.Thread(target=requester) for _ in range(3)]
        clock.start()
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
        stop.set()
        clock.join(timeout=10)

        assert not errors, errors


class TestTheNewPages:
    """The same two guards the student panel already has, on the new pages.

    A JavaScript parse error takes a whole page down while the API, the HTML
    and every other test stay green -- so each page that ships a script gets
    run through ``node --check``. And no page may reference an external host:
    a rig has no internet.
    """

    @pytest.mark.parametrize("path", ["/instructor", "/join"])
    def test_served(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert "<html" in response.text.lower()

    @pytest.mark.parametrize("path", ["/instructor", "/join"])
    def test_self_contained(self, client, path):
        text = client.get(path).text
        assert "http://" not in text.replace("http://127.0.0.1", "")
        assert "https://" not in text

    def test_the_console_can_induce_a_fault(self, client):
        text = client.get("/instructor").text
        for element in ("fault-kind", "fault-target", "fault-message", "fault-go"):
            assert element in text, element

    def test_the_console_has_drill_down_and_recordings(self, client):
        text = client.get("/instructor").text
        for element in (
            "detail-gauges", "detail-sheet", "detail-log", "detail-record",
            "reclist", "rec-refresh", "data-detail",
        ):
            assert element in text, element

    def test_a_recorded_run_can_be_reviewed(self, client):
        """Recordings that cannot be looked at are not a debrief."""
        text = client.get("/instructor").text
        for element in (
            "data-recview", "review-chart", "review-verdict", "review-log",
        ):
            assert element in text, element

    @pytest.mark.parametrize("path", ["/instructor", "/join"])
    def test_the_script_is_valid_javascript(self, client, path, tmp_path):
        from test_sim_api import _node

        node = _node()
        if node is None:
            pytest.skip("node not installed; winget install OpenJS.NodeJS.LTS")

        import subprocess

        script = client.get(path).text.split("<script>")[1].split("</script>")[0]
        file = tmp_path / "page.js"
        file.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(file)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr

class TestTheDriver:
    def test_wells_advance_with_nobody_polling(self, client):
        """End-to-end proof that the background tick is actually wired up.

        Everything else about the clock is tested against a fake clock in
        ``test_classroom.py``; this one really sleeps, because the thing being
        checked is that the asyncio task exists and runs. Nothing polls
        ``/api/me`` while the clock runs -- under the old on-demand model this
        well would not move at all.
        """
        import time

        with TestClient(api.app) as running:  # enter lifespan: driver starts
            instructor = instructor_token(running)
            summary = make_class(running, instructor)
            cid = summary["classroom_id"]
            token = join(running, summary["code"])["token"]

            running.post(
                f"/api/instructor/classes/{cid}/speed",
                json={"speed": 10.0},
                headers=auth(instructor),
            )
            time.sleep(1.0)

            elapsed = running.get(
                "/api/me", headers=auth(token)
            ).json()["gauges"]["time_s"]

        assert elapsed > 2.0, (
            f"one wall second at 10x should be several simulated seconds, got {elapsed}"
        )
