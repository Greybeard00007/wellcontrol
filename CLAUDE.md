# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Bare `python` on this machine is a broken Microsoft Store alias stub** — it prints "Python was not found" and exits 49. Always invoke the venv interpreter by path.

```bash
.venv/Scripts/python.exe -m pytest                      # full suite (~5 min, 1400 tests)
.venv/Scripts/python.exe -m pytest tests/test_methods.py -q
.venv/Scripts/python.exe -m pytest -k "test_balance_point" -q      # single test
.venv/Scripts/python.exe -m pytest tests/test_stripping.py::TestForces -q
```

Setup from scratch (real interpreter lives at `%LOCALAPPDATA%\Programs\Python\Python313\python.exe`):

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev,app]"
```

Run the planner — or double-click `Launch Kill Planner.bat`, which builds the venv on first run and binds to localhost only:

```bash
.venv/Scripts/python.exe -m streamlit run app/streamlit_app.py
```

Run the simulator — or double-click `Launch Simulator.bat`:

```bash
.venv/Scripts/python.exe -m uvicorn wellcontrol.sim.api:app --reload
```

Three ways in: `/` is the student's screens (solo, no login), `/instructor` is
the instructor station, `/join` is where a student enters a class code. For a
class, deploy with `docker compose up -d --build` — see *The classroom* below.

`[dev]` is dependency-free (library + tests). `[app]` adds matplotlib, streamlit, pandas for the planner; `[sim]` adds fastapi, uvicorn, httpx for the simulator's web front end. `tests/test_streamlit_app.py` and `tests/test_sim_api.py` skip themselves via `importorskip`, so the core suite stays dependency-free.

## Rounding is directional

Never use `round()` on a well control quantity. Every rule goes the way that adds margin, and `wellcontrol/rounding.py` implements them:

| Quantity | Direction | Places |
| --- | --- | :-: |
| Kill mud weight | **up** | 1 |
| Max allowable mud weight | **down** | 1 |
| MAASP / MASP | **down** | 0 |
| Pressure per step | **down** | 0 |
| Strokes | **up** | 0 |
| Volume | nearest | 1 |

Pressures and strokes carry no decimals; volumes carry exactly one.

**Where a quantity rounds to nearest, it rounds half away from zero** — 3.45 bbl is 3.5. Python's `round` uses banker's rounding, so `round(0.25, 1)` is 0.2 and `round(2.5)` is 2, which is not what anyone filling in a kill sheet does. `rounding._half_up` goes through `Decimal(str(value))` to get both the direction and the decimal representation right.

**Rounded values are the operational ones.** The rig mixes 10.4 ppg, so FCP is built from 10.4, not 10.369. Where the unrounded figure is useful, it is available under an `_exact` name (`kill_mud_weight_exact_ppg`).

A consequence worth knowing: rounding the kill weight up makes the mud heavier than the mud that exactly balances the formation, so `KillSheet.rounding_overbalance_psi` (16 psi on the reference well) is real extra margin. It also means the depth-accurate schedule, which solves for exactly balanced, finishes *below* the nominal FCP by that amount. Not a bug — work to FCP.

The float handling matters: `10.4 * 10` is `104.00000000000001`, so a naive `ceil` would give 10.5. Every function snaps to a tolerance first.

## The invariant that governs everything

**Pressure calculations take TVD. Circulation calculations take MD.**

Hydrostatic pressure comes from the weight of the fluid column, so it depends on true vertical depth. Volumes, strokes and displacement depend on the measured length of hole. On a vertical well these are the same number and the distinction is invisible; on the horizontal reference well they differ by a factor of two, and mixing them produces a kill mud weight roughly half of what the well needs.

`Survey` owns the conversion. Any new calculation that touches depth must go through it rather than using a raw depth value.

## Architecture

Three layers, each depending only on the ones above it:

1. **Physics primitives** — `units`, `survey`, `geometry`, `hydrostatics`, `rheology`, `kick`, `killsheet`, `tolerance`, `subsea`, `hpht`, `mpd`, `stripping`. Pure functions and dataclasses, no cross-layer imports.
2. **Kill methods** — `methods/`. Composes the primitives into procedures.
3. **Presentation** — `plotting.py` and `app/`. Consume `KillPlan`, never recompute physics.

### App layout

`app/streamlit_app.py` is only a navigation entry. The pages are separate scripts under `app/views/` (`planner.py`, `learn.py`) with shared helpers in `app/shared.py`. They are separate files rather than functions so each can be run and tested on its own — `AppTest` cannot reach a function-based `st.Page`, and `switch_page` needs a file path.

Anything a page needs that isn't pure UI belongs in the library, not in `shared.py`. The chapter reading and link-rewriting live in `wellcontrol/content.py` for exactly this reason: they are testable without a Streamlit runtime, which is how `tests/test_content.py` checks that every method's `chapter` attribute names a file that exists.

**Restart Streamlit after changing anything under `src/`.** Hot reload re-runs the page script but leaves imported modules cached in `sys.modules`, so library edits do not reach a running app. This has twice produced a "bug" in the browser that did not exist in the code — verify against a fresh start before investigating.

**`st.switch_page` cannot be called from an `on_click` callback.** It is a rerun, and Streamlit makes reruns no-ops inside callbacks, so the click silently does nothing. Handle navigation on the button's return value instead. A test that only asserts the button *exists* will not catch this — click it and assert the resulting state.

### The `KillMethod` contract (`methods/base.py`)

Every method takes a `WellState` (well + string + pump + the three shut-in readings) and returns a `KillPlan` carrying: an `Applicability` verdict, headline `summary` numbers, ordered `stages` with actions, and a `TracePoint` series for plotting.

Two design decisions worth preserving:

**Applicability is part of the answer, not a precondition.** Not every method works on every well — Volumetric and Lube-and-Bleed depend on gas migrating, and gas in a horizontal lateral has no upward direction to migrate in. Methods return `blockers` (do not attempt) and `warnings` (proceed with care) rather than raising or silently producing plausible numbers for a procedure that would stall on the rig. Subclasses call `super().applicability(well)` and add their own checks.

**Pressures come from a fluid column, not a formula.** `FluidColumn` models the annulus as an explicit stack of `Zone`s — kill mud at the bottom, mud behind the influx, the influx, original mud above — and computes pressure at any depth from it. Casing pressure is derived as "whatever hydrostatic the column does not supply must appear at surface", and shoe pressure is read off the same column. This is what makes the shoe plot trustworthy rather than a correction applied to a surface reading.

### The circulation simulator (`methods/circulating.py`)

Driller's and Wait-and-Weight share `simulate_circulation()`. At each step it reconstructs the whole well: kill mud position in the string, the four-zone annulus, and the influx volume solved by Boyle's law against the pressure at its own depth so it expands as it rises.

Four invariants the tests enforce across every configuration — an arithmetic error anywhere breaks at least one:

- Zones tile the annulus exactly (mass conservation)
- Influx pressure × volume stays constant (Boyle)
- Drill pipe pressure starts at ICP and reaches FCP when kill mud hits the bit
- Casing pressure ends near zero on a complete kill

### Documented approximations

Keep these stated wherever they apply; they are deliberate, not oversights.

- **Annular friction while circulating is neglected.** System friction is carried on the drill pipe side as the measured slow circulating rate. Standard kill sheet convention.
- **Gas is ideal and isothermal** unless temperatures and z-factors are supplied. Slightly over-estimates expansion, which errs toward caution for kick tolerance.
- **Gas density falls as the bubble expands** — `hydrostatics.expanded_gas_density`, density ∝ 1/volume. This is not an approximation but a correction; see below.
- **The influx is one continuous bubble.** Real influxes disperse, and in oil-based mud dissolve entirely.
- **HPHT density uses a linear PVT model** with representative coefficients, not lab data.

## The simulator (`wellcontrol/sim/`)

A training simulator where a student lines the rig up, brings the pump on and works the choke. Design and staged plan in `docs/simulator-design.md`.

**It solves the opposite direction to the rest of the library, and that is the whole point.** `simulate_circulation` asks "what pressure must the driller hold?" and assumes success. `sim.Simulator` asks "the student set the choke to 35% — what does the well do?" and must represent failure as faithfully as success. Do not try to merge them.

Everything turns on one equation in `engine.py`:

```
dP/dt = (Q_in + q_influx − Q_out − q_loss) / capacitance
capacitance = V_mud × c_mud + V_gas / P_gas_absolute
```

Mud contributes almost nothing, so with little gas the system is **stiff** and a small choke movement swings casing pressure hundreds of psi; as gas rises and expands the capacitance grows by an order of magnitude and it goes **soft**. On the reference well: 0.0056 bbl/psi with gas deep, 0.038 shallow. That contrast is what makes a choke teachable, so **do not damp it out** to make the sim easier to fly.

Every failure mode emerges from that balance rather than being scripted — extra influx, shoe breakdown, deadhead, pressure spike, and the BOP-left-open case where returns take the flowline and the choke does nothing at all. If a future change makes any of those need special-casing, the model has drifted.

Lineup is a directed graph in `flowpath.py`: equipment are nodes, valves are edges, and each step asks whether a path exists pump→well and well→pits. **Wrong lineups are not scored as errors; they simply do not work.** BOP elements are inverted — closing one removes a route — so they are not ordinary edges.

Validated by an autopilot in `tests/test_simulator.py` standing in for a driller who never errs: bottomhole pressure holds within 75 psi of formation pressure, no further influx, shoe under fracture. That is the check that the forward and inverse models describe the same well; keep it passing.

**The autopilot needs its derivative term.** The plant integrates — choke position sets a flow, and pressure is the running total of the imbalance — and the choke is rate-limited on top of that. Proportional control alone limit cycles: it held bottomhole pressure to a steady 65 psi oscillation on a 25-second period. That is a mediocre driller, not a perfect one, and validating the physics against it is validating against the wrong thing. With damping the same run holds 16 psi. If a change makes the autopilot oscillate again, suspect the controller before the engine.


### The sim's subsystems, in brief

Deep histories and calibration notes for everything below live in
`docs/engine-notes.md` — **read its matching section before changing that
subsystem.** What must not be lost:

- **Scenarios are data** (`sim/scenario.py`): plain dataclasses,
  `Scenario.from_dict` takes the JSON shape. **Faults are triggers, not
  scripts** — one thing to one piece of equipment at one moment; what follows
  comes out of the volume balance, and a fault cannot make the well do
  anything it could not have done on its own. The listing never says which
  faults or when (`has_faults` is a bare flag, test-pinned).
- **Gas migration** (`_migrate_gas`): the bubble moves and *asks* for its
  expansion; the volume balance answers, and in a closed well the arithmetic
  reproduces the textbook ρ·dh rise (~70% of it — real compliance absorbs
  the rest, deliberately). Position is measured at the influx **top**. The
  planner's zero migration rate means "do not assume"; the simulator
  substitutes `DEFAULT_MIGRATION_FT_PER_HR` because gas rises whether
  measured or not — and zero in a lateral, which is why Volumetric is
  blocked on horizontal wells. `state.bled_bbl` is the instrument the
  method is worked to.
- **Stripping**: closed-end displacement is the number the method turns on;
  `state.pipe_displacement_bbl` is the instrument. **Open question, still
  unresolved:** the planner and the engine disagree ~4× on the migration
  bleed allowance (7.0 bbl/stand textbook against the engine's measured
  44 psi per bbl of bleed). Nothing has been changed on its strength;
  settling it needs a domain judgement or a second source. Full write-up in
  docs/engine-notes.md.
- **Reverse circulation**: the controlled gauge changes hands — standpipe
  becomes the independent one — `_reconcile_controlled_gauge` bridges the
  equation swap, and friction follows the returns onto the string. The
  float valve is why the method is usually unavailable.
- **Lube and Bleed**: `FlowPath.injecting_into_the_annulus` (the injection
  path reaching the annulus without `Node.STRING`) is how the engine knows
  mud is arriving at the *top*; lubricated volume joins the mud beneath the
  influx; `state.lubricated_bbl` is the instrument. Starts only once the
  gas is at surface — getting it there is Volumetric's job.
- **Bullheading**: `INJECTION_THRESHOLD_PSI` is load-bearing (a zone does
  not take mud the moment it is overbalanced); gas goes back first but only
  while still at the zone (`GAS_AT_ZONE_TOLERANCE_FT`); and `_place_influx`
  subtracts `injected_bbl`, or the pump "advances" mud that vanished into
  the formation.
- **Drilling start** (`StartState.DRILLING`): nothing scripted —
  `trigger_kick()` only raises pore pressure. `Session.downhole_visible`
  hides the downhole truth until shut-in (detection is the skill), the
  trace withholds the same fields, and the band metrics are gated on
  `kick_taken`. `tests/test_drilling_kick.py` asserting SIDPP = pore −
  hydrostatic is the forward/inverse cross-check — keep it passing.

### The web front end (`sim/session.py`, `sim/api.py`, `sim/static/`)

Run it with `Launch Simulator.bat`, or:

```bash
.venv/Scripts/python.exe -m uvicorn wellcontrol.sim.api:app --reload
```

`[sim]` (fastapi, uvicorn, httpx) is a separate extra from `[app]`, so the planner does not drag in a web server and vice versa. `tests/test_sim_api.py` skips itself via `importorskip`.

**Three layers, and the split is load-bearing.** `session.py` owns the wall-clock-to-simulated-time mapping and has **no web dependency**, so the time model is tested with a fake clock and no sleeping. `api.py` is transport only — parse, call a session method, serialise. If a well control rule ever appears in `api.py`, it is in the wrong file. The HTML is a client.

**Time advances on demand, not on a background thread.** Each request computes elapsed wall time × speed and steps the engine that far. The well only evolves while somebody is watching, which is right for self-study and makes tests deterministic. Catch-up is clamped (`MAX_CATCHUP_S`), or ten minutes away at 60× returns ten hours of well in one request. Every control change advances *first*, then acts — otherwise time elapsed at the old setting is credited to the new one.

**The panel is two charts, not one.** Surface pressure runs in hundreds of psi and downhole in thousands; on a shared axis the drill pipe and casing traces flatten onto the baseline, which is exactly the gauge the student is working to. Thresholds are labelled on the line rather than in the legend, because two identical dashes do not tell you which one the shoe is about to cross.


#### The screens

One page, one poll loop; only the visible screen redraws. Everything is
served self-contained from `static/choke_panel.html` — a rig has no internet
and a test pins that. Screen order follows the reference console in
`simulator_screens/sim_screen_expectations/`. Drawing idioms, colour
doctrine and every layout history live in `docs/screens-notes.md`.

| Screen | What it is for |
| --- | --- |
| **Choke Panel** | Drill pipe and casing dials, lamps, the choke, both pressure charts, the event log. |
| **DCU** | Where a kick is *noticed*: pit deviation, return flow, standpipe, both pump rates, alarms with set points, depths and stroke counter. Pump and **stripping** controls. |
| **BOP** | The control panel — red remote panel for a surface stack, monitoring screen for a subsea one. |
| **SP Manifold** | The standpipe manifold: gauge, runs, and every branch valve — both pumps, cement lines, kill line tie-in. |
| **Choke Manifold** | The manifold, plus all four chokes with their own handles and lineup. |
| **Trip Tank** | Tank, active and reserve pits, the transfer pumps, level alarms, and whether the tank is actually in service. |
| **Trends** | Stacked tracks or the reference panel's overlaid parallel axes, against time or strokes. |
| **Well** | Cross-section with live fluid zones. Instructor-side in a class. |
| **Kill sheet** | Built from the student's own readings; plan against actual. |
| **Debrief** | Verdict, measures against their limits, and what happened. Instructor-side in a class. |

**The DCU is where a kick is noticed; the choke panel is where something is
done about it** — merging them teaches students to stare at the choke while
the pit gain runs away behind them. Two engine additions exist so screens
are not decoration: `Accumulator` (an element costs closing-unit pressure
or it does not move) and `hookload_lbf` (well pressure pushes the string
out; at zero hookload the pipe must be snubbed, and pressure gauges alone
never show it coming).

**`node --check` proves the script parses; `tests/test_panel_render.py`
proves it runs.** The harness drives every draw function with a payload
built by a real `Session`, under both a shut-in start and a masked drilling
one, and a companion test breaks a gauge path on purpose so the guard is
known to work. A JS parse error takes every screen down at once and looks
exactly like a server fault — that is what `TestThePanelScriptParses`
exists for (node from winget; tests skip without it rather than
approximating).

**The kill sheet is built from what the student read**, not the scenario's
design figures: `state.measured_sidpp_psi` and friends latch when the
pressures settle, and before that the sheet is withheld rather than guessed.

### Assessment (`sim/assessment.py`)

Scoring is on **outcome and margin, never procedure** — a wrong lineup is
not marked, it simply does not work. The hard limit is **shoe pressure
against fracture pressure**, not casing against MAASP (MAASP is still
reported; it is what the student watched). `EventLog.set_level()` is a
Schmitt trigger so one condition cannot fill the log by toggling; the
metrics still integrate the true band, accumulated every timestep in the
engine, because sampling the trace would miss excursions between samples.
`Standard.require_kill` exists because a perfect Driller's first
circulation leaves the well live. Details: docs/engine-notes.md.

## The expanding gas must get lighter

**A fixed mass of gas in a bigger volume is lighter.** `hydrostatics.expanded_gas_density` scales density as 1/volume, and every place that builds a fluid column containing an influx must go through it: `methods/circulating.py`, `methods/volumetric.py` (the surface gas cap, which has expanded enormously), and `sim/engine.py`.

This was wrong for a long time. The influx zone was built at `well.influx_density_ppg` — the density at *shut-in* — while its volume expanded from 22 bbl to 102 bbl on the way up, which quietly multiplies the gas mass by 4.6. The over-heavy column supplies hydrostatic that is not there, so **the surface pressure needed to balance the formation comes out too low: 182 psi low at the peak on the reference deviated well.** That is the unsafe direction, at the exact moment the gas is near surface and the margin against MAASP matters most.

**Nothing in a thousand internally-consistent tests caught it**, because every test built its inputs with the same assumption as the code. It was found by differencing against an independent implementation of the same well (`test_data/second_source/`). That is the argument for keeping `tests/test_second_source.py` pointed at anything that touches the fluid column.

The simulator scales by mass as well as volume, because a student can take *more* gas: `_gas_pv` stands in for moles, since at fixed temperature `PV` is proportional to how much is down there.

A related resolution trap: **peak casing pressure is a sharp cusp**, reached as the gas top arrives at surface and gone again within a few hundred strokes. The summary peaks are read straight off the trace, so a coarse sample grid under-states a safety number rather than merely drawing a rough plot. `simulate_circulation` defaults to 300 steps for that reason; at 60 the peak came out 64 psi low.

## A data invariant worth knowing

**With an influx that has no vertical height, SICP equals SIDPP.** A lateral influx displaces no hydrostatic, so both gauges read through the same mud to the same TVD. The same holds for an influx entirely below an off-bottom bit. The horizontal examples originally violated this and nothing caught it for a long time, because nothing except the simulator computes bottomhole pressure from the annulus column at shut-in — every other path starts from SIDPP and never cross-checks the annulus side. Any new example data should be checked against it.

## Where this is up to

- **Single-user simulator: done.** Headless engine, lineup, validation, event log and assessment, the full drilling → kick → detection → shut-in sequence, every kill method operable rather than merely planned (Driller's, Wait and Weight, Volumetric, Bullheading, Lube and Bleed, Reverse Circulation, Stripping), and a scenario library with fault injection. **UI decided: FastAPI + browser** — Streamlit has no game loop and reruns the whole script per interaction, which would make gauges into widgets and the choke laggy. The planner stays on Streamlit; the two apps share the library and nothing else.
- **Classroom rebuild: all five stages done.** (1) Server clock, class codes, a session per student, instructor console with a live monitor grid, Docker/NPM deployment — *The classroom*. (2) The trip tank in the volume balance, active and reserve pits, the return flow meter, alarms, a second mud pump, four independent chokes — *The mud system*. (3) The student screens redrawn against the references in `simulator_screens/sim_screen_expectations/` — *The screens*. (4) Live fault induction concealed from the student, per-student drill-down, recordings that outlive the class — *The instructor's hand*. (5) The drilling model — *Making hole*.
- **The student-facing faces are done to their references** (3 Sep 2026): Choke Panel, DCU, both BOP faces, and the SP Manifold / Choke Manifold / Trip Tank trio built verbatim to `IMG_1195/96/97.PNG`. The same day settled the three open BOP decisions — the stack bore is segmented so **outlet heights are real**, both subsea shear rams are **blind shears**, and the bypass stays in the two-handed interlock — and gave the two manifolds real segmented plumbing of their own, which moved the lubricating and reverse-circulation lineups toward the rig (see *Faithful to the panel*). **The instructor station followed the same day** — the reference admin console with its navigation tree, built in that section — which closes the presentation rebuild. What remains on the list: the first Docker build on the Ubuntu box, and the stripping migration-allowance open question.
- **The Docker image has never been built** — Docker was simply not installed on the dev box, not a Windows limitation. First `docker compose up -d --build` on the Ubuntu box will shake it out.


## Faithful to the panel

**The first attempt at the screens was built without ever seeing one, and it
missed — structurally, not in detail.** What was wanted was *"tabs
orientated just like the photos"*: **faithful reproduction of the reference
layout first, capability added on top.** The references are the photographs
in `simulator_screens/` (`sim_screen_expectations/`, `choke_panel_exact.jpg`,
`IMG_1195/96/97.PNG`). The full histories — measured geometries, drawing
lessons, decision records — are in `docs/screens-notes.md`; **read it before
touching any face.** The rules that bite every session stay here:

- **A panel is one surface at fixed coordinates, not a layout.** Each face is
  a single SVG at a fixed viewBox, wall to wall (`width: calc(100% + 32px)`,
  `margin: -16px -16px`). **Never put a `max-height` on a face**: an SVG with
  an explicit width *clips* silently when its height is capped — this cut the
  DCU's bottom row off once. A test asserts no face carries one.
- **Every control drawn on a face must work.** A test scrapes every
  `data-face` value and asserts it appears in `faceAction`'s switch.
- **Handlers go on the SVG host, attached once — never on children.** Faces
  redraw four times a second and replace their whole innerHTML; anything
  bound to a child dies by the next poll. Gauges hang off `host._gauges`,
  never a closure. **Controls fire on `pointerdown`, not `click`** — a click
  is delivered on release, after the pressed node may have been redrawn away,
  and the press is silently lost. **Small controls get a transparent hit disc
  larger than the drawing** (`fill="transparent"`, never `"none"` — none is
  not hit-tested at all).
- **The orange is the console, not the page**; everything outside a face is
  slate. Mid-tone accents vanish on orange; prose goes in black windows.

All student-facing faces are built to their references (see *Where this is
up to*): Choke Panel to `choke_panel_exact.jpg` verbatim (casing gauge
**left**, drill pipe **right** — deliberate, confirmed, test-pinned because
a later reader will "correct" it back); the DCU; both BOP faces (surface an
elevation, verbatim; subsea a schematic, re-spaced — the one to check
against `ss_bop.JPG` if revisited); and the three manifold faces measured
off `IMG_1195/96/97.PNG`, whose engine consequences are recorded below.

### The stack (`sim/flowpath.py`, `Accumulator`)

The surface stack, top to bottom:

| | |
| --- | --- |
| annular | |
| upper pipe rams | |
| lower pipe rams | |
| **upper flow cross** | kill and choke outlets, two HCRs each |
| blind shear rams | |
| **lower flow cross** | kill and choke outlets, two HCRs each |

The subsea stack, top to bottom — **the two stacks are not the same shape
and are not meant to converge**:

| | |
| --- | --- |
| upper annular | |
| **riser connector** | `seals=False` |
| **gas relief line** | piped into the *choke* line |
| lower annular | |
| upper kill | |
| blind shear rams | |
| upper choke | |
| upper pipe rams | |
| middle kill | |
| middle pipe rams | |
| middle choke | |
| lower pipe rams | |
| lower kill | |
| lower blind shear rams | |
| lower choke | |

The model facts that everything else leans on:

- **The heights are the model.** The stack bore is segments (`ANNULUS` below
  the lowest element, `STACK_BORE_1..7` between elements, `BOP` above the
  top); each element is an edge between its neighbours, each outlet taps its
  own segment, so closing a ram isolates every outlet above it. A closed
  non-sealing element still passes flow (the riser connector today).
  Approximations, deliberate: no trapped-pressure between closed elements,
  and tap heights are hydrostatically invisible — `FluidColumn` still sees
  one annulus. `TestTheOutletHeightsAreReal` pins it all. One consequence:
  `return_route()` finds the flowline **by name, not search** — BFS would
  prefer the now-shorter choke path, but an open well takes atmospheric.
- **Both subsea shear rams are blind shears** (cut *and* seal, Kirk's call,
  3 Sep 2026). `Valve.seals` is the whole difference from a casing shear
  and the machinery stays, keyed off flags, for any stack that carries one.
  The lower blind shears sit beneath the lower kill line's tap — only the
  lower choke, on the wellhead, survives them.
- **A shear ram closed on pipe severs it** (`FlowPath.pipe_sheared`): the
  pump deadheads, nothing circulates again, re-opening does not mend it.
- **A double barrier is two valves in series through their own spool node**
  — parallel edges would mean either valve alone gives a route, and each
  outlet needs its *own* spool. `_RETURN_ENTRIES` lists spools, not lines.
- **Naming a side, not a valve**: `FlowPath.open("choke_line")` resolves the
  group (the lowest outlet on that side); `set_valve` still raises on a
  group. Outlet *names* are shared across stacks; the **label** differs —
  surface HCR, subsea fail-safe — and that is a real distinction.
- **Both faces draw the stack by walking the valve list top to bottom**, so
  re-ordering a stack is an edit to `flowpath.py` and nothing else.
- **The closing unit**: two regulators with set point *and* readback (the
  readback is capped by the bottles — the lesson), the bypass, the master
  valve, supply air (below minimum, charging stops), a gallons flow counter
  (the only instrument that says an element actually moved), and a pod
  selector with a `pod_failure` fault behind it. One command (`/closingunit`)
  carries the unit, every field independent.

#### Two-handed operation

`Accumulator.arm()` buys **one** function; `operate()` returns False
without it. Same interlock both stacks — the master valve handle surface,
*Push to Operate* subsea. The rules:

- **It covers the whole unit**: `Valve.on_the_closing_unit` is
  `is_bop or side`, so the outlet HCRs too — a student who cannot shut an
  annular cannot open a choke line either. HCRs cost `hcr_cost_psi` off the
  **manifold** regulator. The manifold gate valves are hand-turned, no arm.
- **The interlock is in the engine, not the page** (`Session.set_valve`
  reaches `Accumulator.operate` on every route).
- **The arm is spent by the attempt, not the success** — a dead pod, empty
  bottles or a wound-off regulator all read as the element simply not
  shifting. Pressing the button for the position an element is already in
  costs nothing. The regulators and bypass need the arm but do not spend it.
- **`pod` and `master_open` are outside the interlock** (a dead pod must be
  changeable; the master *is* the interlock). `set_closing_unit` processes
  `arm` first so one command carrying arm + a setting works.
- Tests that function the stack arm first (`shift()` in
  `tests/test_bop_stack.py`); the ones calling `operate_stack_valve` bare
  are *about* the interlock. **Watch for tests passing vacuously** after
  interlock changes — one asserted something true anyway with both HCRs
  shut.

### Who controls what

**There is no solo mode and never will be** — every run is set up by an
instructor; the no-class branch of `/` is a development harness. The student
gets a transport (play/pause via `/api/me/pause` — a transport, not a
clock) and a read-only speed pill; **a student never picks the speed**, and
there is no `/api/me/speed` on purpose. Restart is hidden in a class. The
instructor picks the exercise, the multiplier, and can put a student on the
**auto choke** — the autopilot's PD controller promoted into the engine
(it needs its derivative term; see the autopilot note above). The hidden
`<select id="scenario">` is how a class-less dev server starts something to
look at.

### The 422 is a stale server, and so is the next one

Paid for three times. `--reload` is necessary and not sufficient:

- **No `--reload`**: new routes 404, changed models 422, while the source
  plainly has them.
- **`--reload` wedges, reproducibly on this box**: the log says
  `Reloading...` and never `Started server process` — the old worker still
  holds the port and the old code keeps answering. **After editing anything
  under `src/`, kill every process on port 8020 and start again.** If the
  server answers with words no longer in the file, this is what happened.
- **The browser caches the page**: only a hard reload picks up an edited
  `choke_panel.html`.

The **instructor station is the admin console now**: a fixed-height window —
dark title bar, Pause All / Resume All / Stop All, *Simulation Speed* dropdown
and the green *Simulation Time* clock — with a dark left navigation tree built
to `sim_screen_expectations/admin_panel_left_side.JPG` using the reference's
own entry names, and a content pane that swaps views without scrolling.
*Operator Panels* embeds the student's real page in an iframe under the
reference chrome's screen-tab strip with a Quad View:
`/instructor/panel?classroom=&student=` serves the student's page read-only,
controls dimmed, polling the drill-down endpoint; `screen=` and `embed=1` on
that URL pick the tab and drop the page's own chrome. Reference entries with
nothing behind them (Remote Launch Panel, View Configuration, Mud Circulation
Panel, Show Auto Choke) are absent rather than inert, and a structural test
asserts every tree leaf opens a view that exists. **No new API routes** — every
view drives an endpoint that already existed, so the `@exclusive` structural
test is untouched. Full detail in `docs/screens-notes.md` under *The
instructor station is the admin console now*.

### The lesson worth keeping

**Do not build screens blind.** ~7,800 lines went in before anyone looked at
a pixel, and no test catches *this is the wrong shape entirely*. "Does not
throw" is far weaker than "reads correctly". Get eyes on a screen before
building the next one — and build to the photograph, not around it.

## Making hole, the instructor's hand, the mud system, the classroom

Full design notes for these four live in `docs/engine-notes.md` — read the
matching section before changing any of them. The rules that bite:

- **Making hole** (`sim/drilling.py`): a drilling break is a kick indicator
  and arrives before the pit gain (rate doubles in ~30 s on the reference
  well). `Simulator.drilling_ahead` is a **lock-out** — BOP closed, pump
  off, rotary stopped, no WOB, or off bottom and nothing in the module runs,
  which is what keeps every validated kill untouched. WOB comes off the
  hookload. Nothing here should plan a bit run.
- **The instructor's hand** (`sim/recording.py`, `inject_fault`): a thrown
  fault and a scheduled one are **the same call**, test-pinned
  indistinguishable in the student payload. Recordings are data, not a
  paused engine — replaying draws them, never re-simulates. Losing a
  recording must not lose the class; one corrupt file must not hide the
  listing; student-name filename fragments strip dots as well as separators.
- **The mud system** (`sim/equipment.py`): the trip tank is in service only
  with the valve open *and* the fill-up pump running; a reserve transfer
  deliberately reads as a false gain (`Session.zero("pits")` clears it);
  `Alarm` is a Schmitt trigger; the flow alarm sits on the **differential**;
  `total_pump_spm` expresses combined flow as pump-1 spm; chokes in
  parallel add. `_settle_surface_volume` is what makes a trip move real
  surface volume.
- **The classroom** (`sim/classroom.py`, `auth.py`, `config.py`): the server
  owns the clock — `Classroom.tick()`, injected, no web dependency;
  `Session.advance()` banks elapsed time before a speed change. Students
  are not accounts: class codes (no O/0, I/1, S/5), HMAC tokens, **roster
  checked on every request**; wrong and expired codes answer alike. One
  reentrant lock on `ClassroomStore` covers everything — **never hold it
  across an `await`**, and the structural test asserting `@exclusive` on
  every class route is the real guard. **One uvicorn worker, and that is
  not a tuning choice** (state lives in process memory).
  `Session.instructor_view()` is the one sanctioned `downhole_visible`
  bypass. Settings come from the environment, never the repository.

## Adding a kill method

Subclass `KillMethod` in `methods/`, set the class attributes (`key`, `name`, `summary`, `circulates`, `requires_migration`, and `chapter` / `chapter_depth` pointing at the content that explains it), implement `plan()`, and register it in the `METHODS` tuple in `methods/__init__.py`. The Streamlit app iterates that tuple, so it appears automatically — but check whether the app needs new sidebar inputs, and add the method name to `METHOD_LABELS` in `tests/test_streamlit_app.py` so it stays in the rendered matrix.

`plot_kill()` chooses panels from what the trace actually contains, so a method with no drill pipe reading gets fewer panels rather than an empty one.

## Off-bottom pressures

`formation_pressure_psi` is **not** simply `SIDPP + 0.052 × MW × TVD_bit`. SIDPP reads pressure at the *bit*; when the bit is off bottom the formation is deeper and `hydrostatic_below_bit_psi` accounts for what lies between (635 psi on the reference well). A related consequence: with the influx entirely below the bit, SICP equals SIDPP, because both gauges read through identical fluids. A difference means the influx has risen above the bit.

## Content and verified numbers

`content/` is teaching material where every worked example runs and **every quoted number has been checked against actual output**. When changing a calculation, re-run the affected examples and update the prose — several numbers in the content are load-bearing claims, not illustrations.

Four findings in the content contradict the textbook and are deliberate:

- **The drill pipe schedule reverses in a lateral.** Pressure has to be taken *up*, not down: the kill mud front gains no TVD so hydrostatic stops rising, while friction keeps climbing toward FCP. A single ICP-to-FCP straight line would have the driller bleeding pressure off where he should be adding it. `KillSheet.schedule_segments()` breaks the schedule at trajectory boundaries *and* at every pipe size change; `ScheduleSegment.pressure_rises` flags the reversal.
- **Wait-and-Weight's shoe-pressure advantage is conditional** on open hole annular volume exceeding drill string volume. On the reference well it does not, and both methods give an identical peak. `WaitAndWeight._shoe_advantage_note()` computes which case applies.
- **Peak shoe pressure is often set at shut-in, not while circulating.** Shut-in is a property of the well and identical for every method, so comparing overall peaks can show two methods as identical when they differ. Use `peak_circulating_shoe_pressure_psi` to discriminate. The cause is that gas rising from the collar annulus into the wider casing annulus gets *shorter* despite expanding — the textbook "worst moment is gas at the shoe" assumes a uniform annulus.
- **The migration allowance usually dominates stripping bleed volumes** — 7.0 bbl against 2.3 bbl of pipe displacement at 1,000 ft/hr.
- **Wait and Weight on a horizontal well needs one drill pipe schedule per section**, four on the reference well, and the schedule reverses in the lateral. The Driller's Method is generally preferred there for that reason. `WaitAndWeight.applicability()` warns rather than blocks — the method is harder to execute, not impossible.

## Importing real geometry

`wellcontrol/importers.py` reads a directional survey and a pipe tally from CSV, with fuzzy header matching (`MD` / `Measured Depth` / `Depth`, and so on). `samples/` holds working examples of both formats. `Survey.from_minimum_curvature` builds TVD from MD/inclination/azimuth, which is what a real survey file contains — a file's own TVD column wins when present, because the surveyor's is authoritative.

Bad files are rejected with a message naming the row, never coerced. A well control calculation built on a misread file is worse than none, because it looks authoritative.

**Three assumptions real data broke, all now fixed — do not reintroduce them:**

- **TVD may decrease.** A real lateral undulates either side of 90°, so the hole goes uphill in places. `Survey` used to reject that outright. Only a TVD change *faster* than the MD change is impossible.
- **Vertical spans between two survey points are signed.** `hydrostatic_pressure` rejects a negative depth, which is right for a depth and wrong for a span — in an undulating lateral the kill mud front can be deeper than the bit. Use `signed_hydrostatic` wherever both ends come from a survey.
- **A real BHA has many short components.** Breaking the schedule at each gives 0-stroke segments nobody can work to, so `schedule_segments(min_strokes=5)` merges slivers.

`Annulus` validates that the string physically fits the hole at construction, naming the component and section. Pairing a tally with the wrong hole section is a plausible mistake and used to fail deep inside a volume calculation.

### Three tiers of evidence, and the real-file validation

1. **Published**: `tests/test_second_source.py::TestPublishedSchedule` is
   the only test that checks the library against the outside world.
2. **Independent implementation**: `test_data/second_source/` — this is what
   found the gas density bug.
3. **Internal consistency**: everything else.

Still unverified: the *shape* of the choke and shoe curves between
endpoints, the influx expansion profile, and the friction treatment.

`test_data/` also holds a rig-shaped survey and tally with an
**independently computed answer key** — when `tests/test_real_files.py`
fails, **assume the library is wrong**. The importer traps real files
exposed (row-index columns swallowed as counts, token-boundary header
matching, surveys stopping behind the bit and `Survey.extended_to`) are
written up in `docs/engine-notes.md` — **read that before touching
`importers.py` or widening an alias list**, and extend the validation
rather than loosening the rejections.

## Stripping is half an operation

A kick with the bit off bottom needs stripping *then* a circulating kill. `strip_then_kill(well, "drillers" | "wait_and_weight")` returns a `KillSequence` pairing both phases, using `WellState.with_bit_at()` to rebuild the string with the bit on bottom for the second. It reports combined time and which phase peaks at the shoe — on the horizontal reference well that is the *stripping*, which planning the kill alone would miss.

## Plots

Follow the `dataviz` skill's rules, which the current plots already satisfy: no dual-axis charts (influx volume and depth are separate panels, never twin y-axes), solid hairline gridlines with dashing reserved for threshold lines, categorical slots 1–3 of the reference palette only, and legends only when a panel has two or more series. Both light and dark themes are defined; the app auto-detects which to use.

## Scope

These are engineering aids, not a substitute for certification, a company well control manual, or the judgement of a competent person on the rig. Where a measured value exists (slow circulating rate, choke line friction, PVT data), prefer it over anything computed here, and say so in the docstring.
