"""Does the panel's drawing code actually *run*?

``node --check`` proves the script parses. It does not prove that
``drawTripTank`` can read the payload the API sends -- and a gauge path that
does not exist throws at the first poll, blanks the screen, and looks exactly
like a server problem. That happened once already with a redeclared ``const``,
which is why the parse check exists; this is the next layer.

So: take the real script out of the page, stub just enough DOM for it to load,
hand it a **real gauges payload built by the real Session**, and call every
draw function. Anything that throws fails here rather than in front of a class.

Skipped rather than approximated where node is absent, for the reason given in
``test_sim_api.TestThePanelScriptParses``.
"""

import json
import subprocess

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from wellcontrol.sim import Session, api  # noqa: E402

from test_sim_api import _node  # noqa: E402


@pytest.fixture
def client():
    with TestClient(api.app) as test_client:
        yield test_client

#: Enough DOM for the script to load and the draw functions to run. Elements
#: report a size, so the canvas paths execute rather than returning early.
HARNESS = r"""
process.on("unhandledRejection", () => {});

function ctx() {
  const noop = () => {};
  return new Proxy({}, {
    get: (t, k) => (k in t ? t[k] : noop),
    set: (t, k, v) => { t[k] = v; return true; },
  });
}

function element(id) {
  const el = {
    id, innerHTML: "", textContent: "", value: "0", checked: false,
    clientWidth: 600, clientHeight: 200, width: 600, height: 200,
    childElementCount: 0, dataset: {}, style: {},
    classList: {
      toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false,
    },
    addEventListener: () => {}, removeEventListener: () => {},
    appendChild: () => {}, remove: () => {},
    getContext: () => ctx(),
    closest: () => null,
    querySelectorAll: () => [],
    querySelector: () => null,
    getBoundingClientRect: () => ({left: 0, top: 0, width: 600, height: 200}),
  };
  el.nextElementSibling = {textContent: ""};
  // The page walks up from a readout to shade its whole cell, so the stub
  // needs a parent with the same shape.
  el.parentElement = {
    classList: {
      toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false,
    },
    style: {},
  };
  return el;
}

const cache = {};
global.document = {
  getElementById: (id) => (cache[id] = cache[id] || element(id)),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: () => {},
  activeElement: null,
};
global.window = {
  addEventListener: () => {}, devicePixelRatio: 1,
  location: {href: ""},
};
global.getComputedStyle = () => ({getPropertyValue: () => "#3fdc6a"});
global.fetch = () => Promise.resolve({
  ok: false, status: 500, text: () => Promise.resolve("stubbed"),
  json: () => Promise.resolve({}),
});
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.sessionStorage = {getItem: () => null, setItem: () => {}, removeItem: () => {}};
global.location = {href: ""};

__SCRIPT__

/* ---- now drive it with a real payload ---------------------------------- */
const gauges = __GAUGES__;
const traceRows = __TRACE__;

const failures = [];
function attempt(name, fn) {
  try { fn(); } catch (e) { failures.push(name + ": " + (e && e.message)); }
}

attempt("paint", () => paint(gauges));
attempt("drawDcu", () => drawDcu(gauges));

/* The BOP face and the three manifold faces draw themselves from the lineup,
   so they need one -- and the BOP face needs *both* stacks driven, because
   the two faces share almost no code and a payload from a surface well would
   never reach the subsea one. */
lineupCache = __LINEUP__;
attempt("drawSpManifoldFace", () => drawSpManifoldFace(gauges));
attempt("drawChokeManifoldFace", () => drawChokeManifoldFace(gauges));
attempt("drawTripTankFace", () => drawTripTankFace(gauges));
attempt("drawBopPanel surface", () => { isSubsea = false; drawBopPanel(gauges); });
lineupCache = __SUBSEA_LINEUP__;
attempt("drawBopPanel subsea", () => { isSubsea = true; drawBopPanel(gauges); });
attempt("drawTripTankFace subsea", () => drawTripTankFace(gauges));
isSubsea = false;

trace = traceRows;
attempt("drawTripTankChart", () => drawTripTankChart());
attempt("drawCharts", () => drawCharts());
attempt("drawTracks", () => drawTracks());
attempt("drawParallel", () => drawParallel());
attempt("dial bipolar", () => dial({
  value: -12, min: -50, max: 50, label: "Pit", unit: "bbl",
  leftLabel: "LOSS", rightLabel: "GAIN", decimals: 1,
  zones: [{from: -50, to: -10, colour: "#b4530e"}],
}));

console.log(JSON.stringify(failures));
"""


def _script(client) -> str:
    page = client.get("/").text
    return page.split("<script>")[1].split("</script>")[0]


def _payload(start: str) -> tuple[dict, list, list]:
    session = Session.for_configuration(start=start)
    session.simulator.run(60.0, dt=0.05)
    return session.gauges(), session.trace(limit=60), session.lineup()


#: A real subsea lineup, so the subsea BOP face is driven with the stack it is
#: drawn for rather than with a surface one that happens to be to hand.
def _subsea_lineup() -> list:
    return Session.for_configuration(bop="subsea").lineup()


def _run(
    node: str, tmp_path, script: str, gauges: dict, trace: list, lineup: list
) -> list:
    source = (
        HARNESS.replace("__SCRIPT__", script)
        .replace("__GAUGES__", json.dumps(gauges))
        .replace("__TRACE__", json.dumps(trace))
        .replace("__LINEUP__", json.dumps(lineup))
        .replace("__SUBSEA_LINEUP__", json.dumps(_subsea_lineup()))
    )
    path = tmp_path / "harness.mjs"
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=90
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestThePanelDraws:
    """Every draw function, against a payload the API really produces."""

    @pytest.mark.parametrize(
        "start",
        [
            "shut_in",   # downhole visible: the extra tracks are drawn
            "drilling",  # masked: they must be skipped, not drawn as null
        ],
    )
    def test_nothing_throws(self, client, tmp_path, start):
        node = _node()
        if node is None:
            pytest.skip("node not installed; winget install OpenJS.NodeJS.LTS")

        gauges, trace, lineup = _payload(start)
        failures = _run(node, tmp_path, _script(client), gauges, trace, lineup)
        assert failures == [], failures

    def test_the_harness_would_catch_a_bad_gauge_path(self, client, tmp_path):
        """Proof the guard works, by breaking the thing it guards.

        Without this the test above could be passing because the harness
        silently does nothing.
        """
        node = _node()
        if node is None:
            pytest.skip("node not installed")

        gauges, trace, lineup = _payload("shut_in")
        broken = _script(client).replace(
            "const pit = g.pits,", "const pit = g.no_such_key,"
        )
        assert "g.no_such_key" in broken, "the edit did not apply"

        failures = _run(node, tmp_path, broken, gauges, trace, lineup)
        assert any("drawDcu" in f for f in failures), failures
