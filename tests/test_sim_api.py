"""The HTTP layer.

Skipped when FastAPI is absent, so the core suite stays dependency-free --
the same arrangement ``test_streamlit_app.py`` uses for Streamlit.

**Everything here is transport.** The physics is tested in ``test_simulator.py``
and the time model in ``test_session.py``; what these check is that a request
reaches the right session method and that the result serialises. Anything that
looks like a well control decision in this file would mean logic had leaked into
the API layer.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="pip install -e '.[sim]'")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from wellcontrol.sim import api  # noqa: E402


@pytest.fixture
def client():
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture
def session_id(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201
    return response.json()["session_id"]


class TestSessionLifecycle:
    def test_creating_a_session_returns_gauges_and_lineup(self, client):
        body = client.post("/api/sessions", json={}).json()
        assert body["session_id"]
        assert body["gauges"]["casing_psi"] > 0
        assert any(v["name"] == "annular" for v in body["lineup"])

    def test_a_new_session_starts_shut_in_and_lined_up(self, client):
        body = client.post("/api/sessions", json={"start": "shut_in"}).json()
        assert "choke" in body["gauges"]["returns"]

    def test_drilling_start_is_available(self, client):
        body = client.post("/api/sessions", json={"start": "drilling"}).json()
        assert "flowline" in body["gauges"]["returns"]

    @pytest.mark.parametrize("bop", ["surface", "subsea"])
    @pytest.mark.parametrize(
        "trajectory", ["vertical", "deviated", "horizontal"]
    )
    def test_every_configuration_can_be_created(self, client, bop, trajectory):
        response = client.post(
            "/api/sessions", json={"bop": bop, "trajectory": trajectory}
        )
        assert response.status_code == 201

    def test_an_unknown_configuration_is_rejected(self, client):
        response = client.post("/api/sessions", json={"bop": "underground"})
        assert response.status_code == 422

    def test_a_missing_session_is_a_404(self, client):
        assert client.get("/api/sessions/nope").status_code == 404

    def test_a_session_can_be_ended(self, client, session_id):
        assert client.delete(f"/api/sessions/{session_id}").status_code == 204
        assert client.get(f"/api/sessions/{session_id}").status_code == 404

    def test_sessions_are_independent(self, client):
        first = client.post("/api/sessions", json={}).json()["session_id"]
        second = client.post("/api/sessions", json={}).json()["session_id"]
        client.post(f"/api/sessions/{first}/choke", json={"percent": 40.0})
        other = client.get(f"/api/sessions/{second}").json()
        assert other["gauges"]["choke_commanded"] == 0.0


class TestScenarioLibrary:
    def test_the_library_is_published(self, client):
        body = client.get("/api/scenarios").json()["scenarios"]
        assert len(body) >= 8
        assert any(s["key"] == "drillers" for s in body)

    def test_a_session_can_be_built_from_one(self, client):
        body = client.post("/api/sessions", json={"scenario": "stripping"}).json()
        assert body["scenario"]["key"] == "stripping"
        assert body["gauges"]["bit_md_ft"] < 10000

    def test_an_unknown_scenario_is_a_404_naming_the_options(self, client):
        response = client.post("/api/sessions", json={"scenario": "nope"})
        assert response.status_code == 404
        assert "drillers" in response.json()["detail"]

    def test_the_listing_does_not_reveal_the_faults(self, client):
        """Only that there are some. Which and when is the exercise."""
        for s in client.get("/api/scenarios").json()["scenarios"]:
            assert "faults" not in s
            assert isinstance(s["has_faults"], bool)

    def test_the_debrief_defaults_to_the_scenarios_standard(self, client):
        created = client.post("/api/sessions", json={"scenario": "drillers"}).json()
        body = client.get(
            f"/api/sessions/{created['session_id']}/debrief"
        ).json()
        assert body["scenario"]["key"] == "drillers"

    def test_the_panel_offers_the_library(self, client):
        text = client.get("/").text
        assert "/api/scenarios" in text
        assert "brief-obj" in text


class TestSnapshot:
    def test_snapshot_carries_gauges_events_and_trace(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}").json()
        assert set(body) >= {"session_id", "gauges", "events", "trace"}

    def test_events_are_incremental(self, client, session_id):
        first = client.get(f"/api/sessions/{session_id}").json()["events"]
        index = first["next_index"]
        again = client.get(
            f"/api/sessions/{session_id}?since_event={index}"
        ).json()["events"]
        assert again["events"] == []
        assert again["next_index"] == index

    def test_trace_limit_is_honoured(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}?trace_limit=1").json()
        assert len(body["trace"]) <= 1


class TestControls:
    def test_choke_position_is_commanded(self, client, session_id):
        body = client.post(
            f"/api/sessions/{session_id}/choke", json={"percent": 35.0}
        ).json()
        assert body["gauges"]["choke_commanded"] == pytest.approx(35.0)

    def test_choke_beyond_the_stops_is_rejected(self, client, session_id):
        for bad in (-1.0, 101.0):
            response = client.post(
                f"/api/sessions/{session_id}/choke", json={"percent": bad}
            )
            assert response.status_code == 422

    def test_pump_speed_is_commanded(self, client, session_id):
        body = client.post(
            f"/api/sessions/{session_id}/pump", json={"spm": 30.0}
        ).json()
        assert body["gauges"]["pump_commanded"] == pytest.approx(30.0)

    def test_valves_can_be_operated(self, client, session_id):
        body = client.post(
            f"/api/sessions/{session_id}/valve",
            json={"name": "annular", "state": "open"},
        ).json()
        states = {v["name"]: v["state"] for v in body["lineup"]}
        assert states["annular"] == "open"

    def test_an_unknown_valve_is_a_404_naming_the_options(self, client, session_id):
        response = client.post(
            f"/api/sessions/{session_id}/valve",
            json={"name": "magic_valve", "state": "open"},
        )
        assert response.status_code == 404
        assert "annular" in response.json()["detail"]

    def test_an_unknown_valve_state_is_rejected(self, client, session_id):
        response = client.post(
            f"/api/sessions/{session_id}/valve",
            json={"name": "annular", "state": "ajar"},
        )
        assert response.status_code == 422

    def test_speed_can_be_changed(self, client, session_id):
        body = client.post(
            f"/api/sessions/{session_id}/speed", json={"speed": 10.0}
        ).json()
        assert body["gauges"]["speed"] == pytest.approx(10.0)

    def test_the_available_speeds_are_published(self, client):
        assert 0.0 in client.get("/api/speeds").json()["speeds"]


class TestTheRigScreenEndpoints:
    def test_pipe_can_be_stripped(self, client, session_id):
        body = client.post(
            f"/api/sessions/{session_id}/strip", json={"feet_per_min": 30.0}
        ).json()
        assert body["gauges"]["strip_rate_ft_per_min"] == pytest.approx(30.0)

    def test_an_absurd_pipe_speed_is_rejected(self, client, session_id):
        response = client.post(
            f"/api/sessions/{session_id}/strip", json={"feet_per_min": 5000.0}
        )
        assert response.status_code == 422

    def test_the_schematic_describes_the_hole(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}/schematic").json()
        assert body["hole_md_ft"] > 0
        assert body["visible"] is True
        assert any(z["label"] == "influx" for z in body["annulus_zones"])

    def test_the_schematic_is_withheld_while_detecting(self, client):
        created = client.post("/api/sessions", json={"start": "drilling"}).json()
        body = client.get(
            f"/api/sessions/{created['session_id']}/schematic"
        ).json()
        assert body["visible"] is False
        assert "annulus_zones" not in body

    def test_the_kill_sheet_is_the_librarys_own(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}/killsheet").json()
        for key in ("kill_mud_weight_ppg", "icp_psi", "fcp_psi",
                    "total_strokes", "maasp_psi"):
            assert body[key] is not None
        assert body["icp_psi"] > body["fcp_psi"]

    def test_a_drilling_session_has_no_kill_sheet_yet(self, client):
        """Inventing one would hand over the exercise."""
        created = client.post("/api/sessions", json={"start": "drilling"}).json()
        body = client.get(
            f"/api/sessions/{created['session_id']}/killsheet"
        ).json()
        assert body["available"] is False
        assert "shut" in body["reason"].lower()
        assert "kill_mud_weight_ppg" not in body

    def test_a_shut_in_session_says_the_readings_were_given(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}/killsheet").json()
        assert body["available"] is True
        assert body["readings_are_the_students"] is False

    def test_the_snapshot_carries_the_lineup_and_schematic(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}").json()
        assert body["lineup"] and body["schematic"]


class TestAccumulator:
    def test_it_starts_full(self, client, session_id):
        gauges = client.get(f"/api/sessions/{session_id}").json()["gauges"]
        assert gauges["accumulator_ok"] is True
        assert gauges["accumulator_fraction"] == pytest.approx(1.0)

    def test_operating_the_stack_costs_pressure(self, client, session_id):
        before = client.get(
            f"/api/sessions/{session_id}"
        ).json()["gauges"]["accumulator_psi"]
        body = client.post(
            f"/api/sessions/{session_id}/valve",
            json={"name": "annular", "state": "open"},
        ).json()
        assert body["gauges"]["accumulator_psi"] < before

    def test_an_empty_accumulator_will_not_shift_an_element(self, client, session_id):
        """It simply does not move, in keeping with the rest of the lineup."""
        from wellcontrol.sim import api as api_module

        session = api_module.store.get(session_id)
        session.simulator.accumulator.pressure_psi = 0.0
        was_closed = session.simulator.flow_path.bop_is_closed
        client.post(
            f"/api/sessions/{session_id}/valve",
            json={"name": "annular", "state": "open"},
        )
        assert session.simulator.flow_path.bop_is_closed == was_closed


class TestDebrief:
    def test_debrief_is_available_immediately(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}/debrief").json()
        assert body["grade"] in ("excellent", "pass", "marginal", "fail")
        assert isinstance(body["metrics"], list)

    def test_requiring_a_kill_changes_the_verdict(self, client, session_id):
        lenient = client.get(f"/api/sessions/{session_id}/debrief").json()
        strict = client.get(
            f"/api/sessions/{session_id}/debrief?require_kill=true"
        ).json()
        assert strict["grade"] == "fail"
        assert lenient["grade"] != "fail"

    def test_metrics_name_their_limits(self, client, session_id):
        body = client.get(f"/api/sessions/{session_id}/debrief").json()
        named = {metric["name"] for metric in body["metrics"]}
        assert "Peak shoe pressure" in named


def _node() -> str | None:
    """Find node, including a winget install that is not yet on PATH."""
    import os
    import shutil
    from pathlib import Path

    found = shutil.which("node")
    if found:
        return found
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    if packages.is_dir():
        for candidate in packages.glob("OpenJS.NodeJS*/**/node.exe"):
            return str(candidate)
    return None


class TestThePanelScriptParses:
    """A syntax error in the page takes every screen down at once.

    It happened: a second ``const open`` in one block stopped the whole script
    parsing, so nothing initialised and every gauge on every screen read a dash
    -- while the API returned 200, the HTML was intact and the entire suite
    passed, because nothing in it runs the script.

    ``node --check`` is the real answer and is what this uses. It is skipped
    rather than approximated where node is absent: a hand-rolled substitute
    passed happily on the very error it was written for once the shape of the
    mistake changed.
    """

    def test_the_script_is_valid_javascript(self, client, tmp_path):
        node = _node()
        if node is None:
            pytest.skip("node not installed; winget install OpenJS.NodeJS.LTS")

        import subprocess

        script = client.get("/").text.split("<script>")[1].split("</script>")[0]
        path = tmp_path / "panel.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr

    def test_the_guard_would_catch_a_redeclaration(self, tmp_path):
        """The exact mistake that got through, so the guard is known to work."""
        node = _node()
        if node is None:
            pytest.skip("node not installed")

        import subprocess

        path = tmp_path / "bad.js"
        path.write_text(
            "function f(v) { const open = 1; const open = 2; return open; }",
            encoding="utf-8",
        )
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode != 0
        assert "already been declared" in result.stderr


class TestScreens:
    """Every screen in the design doc is present and reachable."""

    def test_the_panel_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Well Control Simulator" in response.text

    def test_every_operator_screen_exists(self, client):
        """The screens the reference console has, plus ours.

        Manifolds are two screens rather than one because they are two panels
        in two places, and a student is sent to one or the other.
        """
        text = client.get("/").text
        for screen in ("choke", "dcu", "bop", "spmanifold", "chokemanifold",
                       "triptank", "trends", "well", "sheet", "debrief"):
            assert f'data-screen="{screen}"' in text, screen
            assert f'id="s-{screen}"' in text, screen

    def test_both_bop_panels_are_drawn(self, client):
        """A rig has two: a red remote panel, and a subsea monitoring screen."""
        text = client.get("/").text
        assert "function drawSurfaceBopFace(" in text
        assert "function drawSubseaBopFace(" in text
        assert "function drawBopPanel(" in text

    def test_the_panel_follows_the_well(self, client):
        """Surface wells get the remote panel; subsea wells get the screen."""
        surface = client.post(
            "/api/sessions", json={"bop": "surface"}
        ).json()["gauges"]
        subsea = client.post(
            "/api/sessions", json={"bop": "subsea"}
        ).json()["gauges"]
        assert surface["is_subsea"] is False
        assert subsea["is_subsea"] is True

    def test_both_manifolds_are_drawn(self, client):
        text = client.get("/").text
        assert "function drawStandpipeManifold(" in text
        assert "function drawChokeKillManifold(" in text
        assert 'id="standpipe"' in text
        assert 'id="ckmanifold"' in text

    def test_the_dcu_is_one_instrument_face(self, client):
        """The reference is a single dense console, not rows of cards."""
        text = client.get("/").text
        assert 'id="dcuface"' in text
        assert "function drawDcuFace(" in text
        assert "function rigDial(" in text
        assert "function alarmBlock(" in text

    def test_the_dcu_carries_its_instruments(self, client):
        """Everything driller_screen.JPG has, by the name it prints on it."""
        text = client.get("/").text
        for label in (
            "Pit Deviation", "Return Flow",        # the two kick gauges
            "TD Torque", "RPM", "WOB",             # drilling
            "Stroke Counter (#)", "ROP (ft/hr)",
            "Bit Depth (ft)", "Well Depth (ft)",
            "Stand Pipe Pressure",
            "Pump 1 Stroke Rate", "Pump 2 Stroke Rate",
            "Drawworks", "Top Drive", "Rotary", "Hookload",
        ):
            assert label in text, label

    def test_every_face_control_is_dispatched(self, client):
        """A control drawn but not handled is worse than one absent.

        A student learns the panel and then finds half of it inert. So every
        `data-face` value the faces draw has to appear in faceAction's switch.
        """
        import re

        text = client.get("/").text
        # The BOP face's element buttons carry which element they operate --
        # `bop-open:annular` -- because the stack is drawn from the lineup and
        # there is no fixed list of names on the page. Only the part before
        # the colon is dispatched, so that is what has to be handled.
        drawn = set(re.findall(r'data-face="([a-z0-9+-]+)(?::[^"]*)?"', text))
        drawn -= {"${act}", "none"}
        handled = set(re.findall(r'case "([a-z0-9+-]+)":', text))
        # The joystick and block lever are dragged, not clicked.
        missing = drawn - handled - {"stick"}
        assert not missing, f"drawn but not handled: {sorted(missing)}"

    def test_the_choke_panel_carries_the_pump_controls(self, client):
        """The driller brings the pump to kill rate while watching these two
        gauges, so the reference keeps the pumps on this panel."""
        text = client.get("/").text
        assert "MUD PUMPS" in text
        for act in ('"p1-"', '"p1+"', '"p2-"', '"p2+"'):
            assert act in text, act

    def test_each_pump_has_its_own_shutdown(self, client):
        """One action, not six presses of minus.

        Stepping down to zero is five or six presses, and the moment a driller
        wants a pump off is exactly the moment they have something else to
        watch. The shutdowns are per pump because a kill on one pump with the
        other cracked open is a lineup a student can get into.
        """
        text = client.get("/").text
        assert text.count("SHUTDOWN") >= 2
        for act in ('"p1off"', '"p2off"'):
            assert act in text, act
        assert 'send("pump", {spm: 0, pump: 1})' in text
        assert 'send("pump", {spm: 0, pump: 2})' in text

    def test_the_joystick_is_proportional_and_spring_centred(self, client):
        """Held deflection sets a rate; it is not a swipe that lands on release.

        The three things that made the first version feel dead are each pinned:
        the handle leans while held, the step is integrated against measured
        elapsed time rather than the timer's nominal interval, and the rate is
        the choke's own travel speed at full deflection.
        """
        text = client.get("/").text
        body = text.split("function startStick(")[1].split("function stopStick(")[0]
        assert "performance.now()" in body            # measured, not nominal
        assert "STICK.target" in body                 # local, not read back
        assert "choke_travel_rate" in body            # capped at travel speed
        assert "STICK.deflection / STICK.FULL" in body
        # The handle leans, and springs back when let go.
        assert "MAX_LEAN_DEG" in text
        assert "rotate(" in text.split("function joystick(")[1][:2600]
        stop = text.split("function stopStick(")[1][:300]
        assert "STICK.deflection = 0" in stop

    def test_the_handle_leans_the_way_it_is_pushed(self, client):
        """The panel must not disagree with itself.

        `deflection` is positive to the left because left is open, and SVG's
        rotate is positive *clockwise*, which swings everything above the
        pivot to the right. Unnegated, a left push opened the choke correctly
        while the handle leaned right -- a worse fault than either direction
        being wrong on its own, because the two halves of one control
        contradicted each other.
        """
        text = client.get("/").text
        assert "const lean = -(STICK.deflection / STICK.FULL)" in text
        # And the open end of the arc is the end the handle opens towards.
        assert '[[-1, "Open", "open"], [1, "Close", "close"]]' in text
        assert "STICK.deflection = Math.max(-STICK.FULL," in text
        assert "Math.min(STICK.FULL, from - e.clientX)" in text

    def test_the_student_has_a_transport_but_not_a_speed(self, client):
        """There is no solo mode: an instructor sets every run up, so the
        multiplier is theirs and the student gets an indicator of it.

        The transport is a separate thing and the student does have it -- they
        must be able to stop their own well to read a kill sheet.
        """
        text = client.get("/").text
        assert 'id="speed-pill"' in text
        assert 'id="play"' in text and 'id="pause"' in text
        # No exercise or speed *control* on the student's header. The scenario
        # select survives only hidden, as the way a server with no class on it
        # starts something to look at.
        assert "<select id=\"speed\"" not in text
        assert '<select id="scenario" hidden' in text
        assert 'send("pause", {paused})' in text

    def test_the_dcu_carries_the_same_pump_cluster(self, client):
        """A kick is noticed on the DCU and the first thing done about it is
        usually to the pumps.

        Both faces draw it through one function, so the two panels cannot
        drift into disagreeing about what a pump control looks like. The DCU
        stacks it into a column because that is the shape of the space the
        reference leaves on the right of that screen; the controls and their
        order are the same.
        """
        text = client.get("/").text
        assert "function mudPumpBlock(" in text
        choke = text.split("function drawChokeFace(")[1].split("\nfunction ")[0]
        dcu = text.split("function drawDcuFace(")[1].split("\nfunction ")[0]
        assert "mudPumpBlock(" in choke
        assert "mudPumpBlock(" in dcu
        # And neither face hand-rolls its own copy of the controls.
        for face in (choke, dcu):
            assert '"p1off"' not in face and '"p2off"' not in face

    def test_the_dcu_can_zero_the_gain(self, client):
        """A reserve transfer raises the active level without the well having
        done anything, and a pit alarm with no way to clear it is a pit alarm
        that gets ignored."""
        text = client.get("/").text
        assert "Reset Zero" in text
        assert '"zeropits"' in text
        assert 'send("zero", {instrument: "pits"})' in text

    def test_both_dcu_alarms_can_be_set_and_armed(self, client):
        """The block used to be a picture of an alarm: the numbers were live
        but nothing on it could be touched, so a student could see the pit
        alarm was going to fire at 10 bbl and had no way to move it."""
        text = client.get("/").text
        for act in ('"pitlow-"', '"pitlow+"', '"pithigh-"', '"pithigh+"',
                    '"flowlow-"', '"flowlow+"', '"flowhigh-"', '"flowhigh+"',
                    '"pitarm"', '"flowarm"'):
            assert act in text, act
        # Each set point moves on its own, and they cannot cross.
        assert "send(\"alarm\", {name, low: Math.min(a.high," in text
        assert "send(\"alarm\", {name, high: Math.max(a.low," in text

    def test_the_return_flow_dial_switches_percent_and_volume(self, client):
        """Percent is the better kick indicator and the rate is the better
        instrument, so the reference carries a switch rather than picking one.

        The switch never reaches the server: the well does not have an opinion
        about units.
        """
        text = client.get("/").text
        assert '"flowunits"' in text
        assert "Percent" in text and "Vol/Min" in text
        assert "% of flow in" in text
        dispatch = text.split('case "flowunits":')[1].split("case \"")[0]
        assert "flowInPercent = !flowInPercent" in dispatch
        assert "send(" not in dispatch

    def test_the_pit_deviation_gauge_reads_loss_and_gain(self, client):
        """Bipolar with zero centred: the question is whether the needle has
        left centre, not whether it reached a number."""
        text = client.get("/").text
        assert 'leftLabel: "LOSS", rightLabel: "GAIN"' in text
        assert "min: -50, max: 50" in text

    def test_the_dcu_alarms_have_set_points_and_lamps(self, client):
        text = client.get("/").text
        for element in (
            "sp-pit-low", "sp-pit-high", "sp-flow-low", "sp-flow-high",
            "arm-pit", "arm-flow", "set-wob", "set-rpm", "auto-driller",
        ):
            assert element in text, element
        # The lamps are drawn on the face by alarmBlock, from the alarm state.
        assert "alarm.low_active" in text and "alarm.high_active" in text
        assert "alarm.sounding" in text

    def test_the_trip_tank_screen_is_operable(self, client):
        """Both halves of the lineup, and a way to mark the tank."""
        text = client.get("/").text
        for element in (
            'data-tt="trip_tank_fillup"', 'data-tt="trip_tank_fill"',
            'data-tt="trip_tank_drain"', 'data-tt="pit_transfer"',
            'data-valve-toggle="trip_tank_valve"',
            'data-zero="trip_tank"', "tt-service",
        ):
            assert element in text, element

    def test_the_choke_bank_offers_all_four(self, client):
        text = client.get("/").text
        assert "choke-bank" in text
        for name in ("choke_1", "choke_2", "manual_choke_1", "manual_choke_2"):
            assert name in text, name

    def test_the_trends_screen_offers_both_views(self, client):
        """Stacked tracks by default, with the commercial panel's overlaid
        parallel-axis form available for students who will sit that box."""
        text = client.get("/").text
        assert 'value="tracks" selected' in text
        assert 'value="parallel"' in text
        assert 'value="strokes"' in text, "should plot against strokes too"

    def test_stripping_is_operable(self, client):
        """The engine has supported it for a while; the panel now reaches it."""
        text = client.get("/").text
        assert 'id="strip"' in text
        assert 'data-strip="30"' in text

    def test_the_schematics_are_drawn(self, client):
        text = client.get("/").text
        assert 'id="pid"' in text
        assert 'id="schematic"' in text

    def test_the_closing_unit_is_on_the_bop_face(self, client):
        """Bottle pressure is not the whole unit, and the panel says so.

        The regulators, the bypass, the master and the gallons counter are all
        gauges and controls in their own right: an element will not shift with
        a regulator wound off however full the bottles are, and the flow
        counter is the only thing on the panel that says a ram actually moved.
        """
        text = client.get("/").text
        for label in (
            "Accumulator Pressure", "Air Pressure",
            "Manifold Pressure", "Annular Pressure",
            "Manifold Regulator Pressure", "Annular Regulator Pressure",
            "By Pass Valve", "Flow Counter", "Flow Reset", "MASTER",
        ):
            assert label in text, label

    def test_exactly_one_screen_starts_visible(self, client):
        """The whole page rendered blank for a while and nothing caught it.

        `.screen` is display:none until it carries `active`. Renaming the tabs
        dropped that class from every section, so every screen was hidden --
        the header still painted, so it looked like a styling problem rather
        than a page with no visible content at all. No test noticed, because
        every test asked the HTML what it *contained*, not what it would
        *show*.
        """
        import re

        text = client.get("/").text
        sections = re.findall(r'<section class="screen([^"]*)" id="s-([a-z]+)"', text)
        assert sections, "no screens found at all"
        visible = [name for cls, name in sections if "active" in cls]
        assert len(visible) == 1, (
            f"expected exactly one screen visible on load, got {visible}"
        )

        # And it must be the tab that is highlighted, or the page opens showing
        # one screen with a different tab lit.
        tab = re.search(r'<button data-screen="([a-z]+)" class="active"', text)
        assert tab, "no tab is marked active"
        assert tab.group(1) == visible[0], (
            f'tab "{tab.group(1)}" is lit but screen "{visible[0]}" is shown'
        )

    def test_the_choke_panel_is_one_instrument_face(self, client):
        """Not cards containing gauges -- the console the reference photographs.

        A driller's scan is *spatial*: they look at where the gauge is, not at
        where a layout engine put it this frame. So the panel is one SVG at
        fixed coordinates, and everything the reference carries is on it.
        """
        text = client.get("/").text
        assert 'id="chokeface"' in text
        assert "function drawChokeFace(" in text
        for part in (
            "function pressureGauge(",     # bolted bezel, psi and bar scales
            "function segment(",           # black window, red seven-segment
            "function chokePositionDial(",  # scaled in eighths
            "function joystick(",          # the handle and its travel arc
            "function rotarySelector(",    # Choke2 / Choke1
        ):
            assert part in text, part

    def test_the_choke_panel_carries_the_reference_readouts(self, client):
        """Every lettered block on simulator_screens/choke_panel_exact.jpg.

        "Hold" used to be pinned here and is deliberately gone: it labelled the
        joystick's arc on a panel where the two buttons at the ends of that arc
        step the choke by 5% per click rather than holding it, so it named
        something the control does not do, and the approved layout omits it.
        """
        text = client.get("/").text
        for label in (
            "DRILL PIPE PRESSURE", "CASING PRESSURE", "STROKE RATE",
            "STROKE COUNTER", "KILL LINE PRESSURE", "Choke Position",
            "CHOKE CONTROL", "Travel speed", "MUD PUMPS", "RESET",
        ):
            assert label in text, label

    def test_the_choke_position_dial_is_scaled_in_eighths(self, client):
        """Open, 7/8 … 1/8, Close -- the way the reference gauge reads."""
        text = client.get("/").text
        for mark in ('"7/8"', '"3/4"', '"5/8"', '"1/2"', '"3/8"', '"1/4"', '"1/8"'):
            assert mark in text, mark

    def test_the_joystick_actually_works_the_choke(self, client):
        """Decoration would be worse than nothing: it is the panel's control.

        Asserted on the mechanism rather than on a rendered attribute -- the
        handles are templated, so `data-face="open"` never appears literally in
        the source and a test looking for it would pass or fail for the wrong
        reason.
        """
        text = client.get("/").text
        assert '[[-1, "Open", "open"], [1, "Close", "close"]]' in text
        assert 'data-face="${act}"' in text
        # The handler has to reach the choke, not just exist -- and it has to
        # hang off pointerdown, because a `click` is only delivered on release
        # and the face has redrawn out from under the pressed node by then.
        assert 'host.addEventListener("pointerdown"' in text
        assert 'host.addEventListener("click"' not in text
        assert 'e.target.closest("[data-face]")' in text
        assert 'send("choke", {percent: next})' in text

    def test_the_casing_gauge_is_on_the_left(self, client):
        """Transposed from the reference box, deliberately and confirmed.

        It is the kind of thing a later reader "corrects" back, so it is
        pinned: casing left, drill pipe right, each with its own digital
        window directly above it.
        """
        text = client.get("/").text
        face = text.split("const FACE = {")[1].split("};")[0]
        csg = float(face.split("csg:")[1].split("cx:")[1].split(",")[0])
        dp = float(face.split("dp:")[1].split("cx:")[1].split(",")[0])
        assert csg < dp
        body = text.split("function drawChokeFace(")[1]
        assert body.index("CASING PRESSURE") < body.index("DRILL PIPE PRESSURE")

    def test_the_reset_button_resets_the_stroke_counter(self, client):
        """Every control on the face works; this one used to be a no-op."""
        text = client.get("/").text
        assert 'send("zero", {instrument: "strokes"})' in text

    def test_the_bop_stack_is_drawn_in_elevation(self, client):
        """Not a list of words: a student who shut the wrong element sees it.

        Both panels draw it -- the surface one alongside its selectors, the
        subsea one with position and health per pod.
        """
        text = client.get("/").text
        assert 'id="bopface"' in text
        for panel in ("function drawSurfaceBopFace(", "function drawSubseaBopFace("):
            assert panel in text
        # One drawing serves both, so the rams meet across the bore when shut
        # and draw back into the body when open on either stack.
        body = text.split("function stackElement(")[1][:1400]
        assert "shut ?" in body


class TestPanel:

    def test_the_face_runs_to_the_edges_of_the_screen(self, client):
        """Console orange to the edge, like a box bolted into a room.

        The negative margins cancel `main`'s padding exactly, so there is no
        slate showing beside the face. A `max-height` must not come back: with
        an explicit width the browser clips the SVG rather than scaling it,
        and what goes off the bottom is the mud pump block.
        """
        text = client.get("/").text
        for host in ("#chokeface {", "#dcuface {", "#bopface {"):
            face = text.split(host)[1].split("}")[0]
            assert "width: calc(100% + 32px)" in face, host
            assert "margin: -16px -16px" in face, host
            # The DCU's cap was not shrinking that face, it was cutting it off:
            # 369px tall against a natural 639, so the bottom row of the DCU
            # was off the screen and nothing said so.
            assert "max-height" not in face, host
            assert "max-width" not in face, host

    def test_the_screen_tabs_are_centred(self, client):
        """`safe center`, not plain centring: centred flex items that overflow
        spill off both ends and the first tab becomes unreachable."""
        text = client.get("/").text
        nav = text.split("\n  nav {")[1].split("}")[0]
        assert "justify-content: safe center" in nav

    def test_the_panel_is_self_contained(self, client):
        """No CDN: the rig has no internet and neither should this."""
        text = client.get("/").text
        assert "http://" not in text.replace("http://127.0.0.1", "")
        assert "https://" not in text

    def test_the_panel_shows_the_instrument_scan(self, client):
        """Four gauges in one view -- the scan a student has to build."""
        text = client.get("/").text
        for element in ("g-dp", "g-csg", "g-stk", "g-chk"):
            assert element in text

    def test_the_panel_offers_the_exercise_picker(self, client):
        """Populated from the library rather than hard-coded."""
        text = client.get("/").text
        assert 'id="scenario"' in text
        assert "loadScenarios" in text


class TestWhatTheStudentCanSee:
    """A rig has no bottomhole gauge; nor does the panel until it is earned."""

    def test_a_drilling_start_withholds_the_downhole_truth(self, client):
        body = client.post("/api/sessions", json={"start": "drilling"}).json()
        gauges = body["gauges"]
        assert gauges["downhole_visible"] is False
        assert gauges["bottomhole_psi"] is None
        assert gauges["formation_psi"] is None

    def test_a_drilling_start_still_shows_the_rig_instruments(self, client):
        gauges = client.post(
            "/api/sessions", json={"start": "drilling"}
        ).json()["gauges"]
        for key in ("drill_pipe_psi", "casing_psi", "pit_gain_bbl",
                    "flow_out_bpm"):
            assert gauges[key] is not None

    def test_a_shut_in_start_shows_it(self, client):
        gauges = client.post(
            "/api/sessions", json={"start": "shut_in"}
        ).json()["gauges"]
        assert gauges["downhole_visible"] is True
        assert gauges["bottomhole_psi"] is not None
