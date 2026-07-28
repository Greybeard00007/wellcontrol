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

### Scenarios and faults (`sim/scenario.py`)

A scenario names an exercise: which well, where it starts, the lineup, what the student is asked to achieve, and what is going to go wrong. **Scenarios are data** — plain dataclasses built from primitives, with `Scenario.from_dict` taking the same shape as JSON, so an instructor can add one without touching Python. `SCENARIOS` is the built-in library written out longhand.

**Faults are triggers, not scripts.** Each does one thing to one piece of equipment at one moment — the pump fails, the choke plugs, the seat is weaker than the leak-off said. What follows is whatever the well does about it, through the same volume balance as everything else. Nothing in `_break_equipment` knows what a kick is, and a fault cannot make the well behave in a way it could not have behaved on its own.

That distinction is worth defending. A scripted "and now the pressure spikes" teaches a student to recognise a scripted spike; a choke that plugs while they are holding 400 psi on it teaches them what a plugged choke feels like.

Triggers are time, stroke count, or an event already in the log — the last so a fault can hang on something the *student* caused, such as gas reaching surface, rather than on the clock.

**The listing never says which faults or when**, only that there are some. A student who knows the choke plugs at stroke 2,000 is not being tested on noticing that the choke has plugged. `Scenario.summary()` and `/api/scenarios` both carry a bare `has_faults` flag and a test pins that.

`WEAK_SHOE_MARGIN_PSI` is measured against the pressure the shoe is *already* carrying, not against its own hydrostatic. Halving the latter put the fracture pressure below the shut-in shoe pressure, so the well began the exercise already broken down and there was nothing to fly.

### Gas migration, and the Volumetric method (`_migrate_gas`)

**Every other method is driven by the pump. Volumetric is not** — the well is shut in, nothing circulates, and the only thing moving is the gas. Without migration the simulator cannot represent it at all, and a shut-in well sat still forever, which is what it used to do.

Gas migrating in a closed well **takes its pressure with it**: it cannot expand, so it arrives shallower still carrying the pressure it had lower down, and everything beneath gains that hydrostatic. Surface pressure climbs at the mud gradient times the climb rate.

The engine is not told that. `_migrate_gas` moves the bubble and returns the expansion it *wants* at its new depth; the caller feeds that into the same volume balance as every other flow, and in a closed system the arithmetic comes out at `dP = (V·ρ·dh/P) / (V/P) = ρ·dh` — the textbook answer, for free. Adding a rule instead would have been a rule to keep in step with everything else.

Two calibration points worth keeping:

- **Measured at the influx top, not the bottom.** The position is parameterised by the volume beneath the bubble, and the top is the leading edge — it decides when gas reaches the shoe and then surface. Taking the capacity at the bottom made it climb at two thirds of the stated rate wherever the annulus widened. The bottom then rises faster than the top, which is right: a fixed volume moving into a wider annulus gets shorter.
- **The rise comes out ~70% of the idealised `ρ·dh`, and that is not an error.** The textbook figure assumes the gas cannot expand at all; the engine gives the system real compliance (mud compressibility, casing expansion) which absorbs part of the wanted volume. The fraction is set by gas volume against mud volume in `_capacitance_bbl_per_psi`.

**The planner and the simulator mean different things by a zero migration rate.** `WellState.gas_migration_rate_ft_per_hr` is a *measured* figure, and zero means "do not assume migration" — right for planning, since a method that depends on it should not be recommended on an assumption. A simulator cannot take that view: gas in a vertical hole rises whether or not anyone measured it. `_default_migration_rate()` uses the measured value where there is one and `DEFAULT_MIGRATION_FT_PER_HR` otherwise, and zero in a lateral, which is why Volumetric is blocked on horizontal wells.

`state.bled_bbl` counts what leaves through the choke with the pump off. Volumetric is executed by bleeding *measured* increments, so it is the instrument the method is worked to, not a diagnostic.

### Stripping: the pipe is the control

The only method where the pump stays off and the *pipe* does the work. Running pipe in puts steel into a closed well, and that volume has to come out of the choke or bottomhole pressure climbs. `Simulator.strip(feet_per_min)` is the control; `state.pipe_displacement_bbl` is what the student is bleeding against.

**Closed-end displacement is the number the method turns on** — a string being stripped has a float in it, so the bore goes in full and displaces along with the metal. It is the whole outside diameter that counts, not the wall. The open-end figure is about a third of it and would under-state every bleed.

The simulator and the planner agree exactly on the geometry: 0.0243 bbl/ft, 2.3 bbl per stand, 24.3 bbl total over 1,000 ft, peak shoe 3,915 against the planner's 3,971.

#### Open question: the migration allowance

**They disagree by about 4× on how much extra to bleed for migration, and it is not yet resolved.**

`stripping.migration_bleed_per_stand_bbl` uses the textbook volumetric formula — gas rising `d` raises surface pressure by `0.052 × MW × d`, so bleed a mud column of the same height, `d × annular capacity`. That gives 7.0 bbl per stand here against 2.3 bbl of pipe displacement.

The simulator disagrees. Bleeding is governed by system capacitance: take a barrel out of a closed well and the gas expands a barrel, Boyle drops its pressure, and bottomhole pressure falls with it. Measured in the engine, **1 bbl of bleed drops bottomhole pressure 44 psi**, where the textbook relation implies about 11 psi. Bleeding the planner's 7 bbl in a stand would drop the well several hundred psi and take another influx.

Both cannot be right, and the direction matters: if the engine is right, the planner's figure is non-conservative. Two candidate resolutions, neither confirmed:

- The textbook figure may describe the volume bled across a *whole cycle* in which migration continues throughout, not an instantaneous pressure-to-volume relationship — in which case the two are answering different questions and both stand.
- Or the engine's capacitance is under-stating how much the annulus gives up, in which case `_capacitance_bbl_per_psi` needs revisiting.

**Nothing has been changed in the library on the strength of this.** The stripping test asserts only that more is bled than the steel displaces, not how much more. Settling it needs a domain judgement or a second source.

### Reverse circulation: the influx goes up the pipe

Down the annulus, back up the drill pipe. The influx travels up the **inside of the string**, so it never passes the casing shoe — which is the whole attraction, and the string holds a fraction of the annulus volume so the job is over in a third of the strokes.

**Which surface gauge is the independent one changes hands.** Forward, the choke is on the annulus and casing pressure is what the volume balance drives; reverse, the choke is on the drill pipe and the pump is on the annulus, so standpipe pressure becomes the controlled variable and casing is derived. Both gauges go on meaning what they say, which matters for the panel: "casing" is the annulus whichever way the mud is going.

Three things this needed, each of which was a bug first:

- **`_reconcile_controlled_gauge`.** When the equations change hands, the arithmetic linking the two gauges changes even though the well has done nothing sudden. Left alone that was a 4,500 psi step in bottomhole pressure the instant the influx entered the string, which took 7 bbl of fresh influx — an artefact of switching equations. On those transitions only, the controlled gauge is recomputed from the bottomhole pressure already there.
- **String positions are computed unconditionally in `_place_influx`.** The gas pressure is read from them, and on the step the influx first enters the string a stale pair read the pressure at *surface* instead of at the bit — putting a 20 bbl bubble at 240 bbl.
- **Friction follows the returns.** Reverse puts it on the string, because that is the way the mud is coming back. Get that wrong and the shoe reads 600 psi high, which would have hidden the method's entire advantage.

On the reference well the shoe sits at **3,984 psi while reverse circulating, below its own 4,154 psi shut-in value** — the planner's 3,894 plus the small annular friction term the planner neglects. Every method that brings the influx up the annulus makes the shoe worse on the way past; this one never does.

`float_valve` is why the method is usually unavailable: most strings run a non-return valve, it passes nothing back up the pipe, and with it shut there is no return route at all.

### Lube and Bleed: the pump at the other end of the well

Every other method pumps **down the string**, where mud comes round the bit and pushes the annulus up from below. Lubricating puts mud into the annulus from the **top**, through the kill line, and lets it fall through the gas to sit beneath it. Same pump, same barrels, opposite ends — and the engine has to know which, because they do completely different things to the fluid column.

`Valve.bidirectional` exists for this. A gate valve does not care which way flow goes: the kill line takes returns during a kill and takes mud *into* the annulus when lubricating, and it is the same valve doing both. `FlowPath.injecting_into_the_annulus` is how the engine tells them apart — the injection path reaching the annulus without passing through `Node.STRING`.

Lubricated volume adds to the mud beneath the influx, exactly as migration does, because that is where it ends up. `state.lubricated_bbl` is the instrument the method is worked to.

**Lube and Bleed only starts once the gas is at surface**, and getting it there is Volumetric's job. Letting it migrate up unbled for hours instead leaves the well grossly over-pressured — a different exercise, and one that squeezes the zone.

### Bullheading: the formation takes fluid both ways

Underbalanced, the zone gives up gas through `productivity_index`. Pushed hard enough it takes fluid back, and that is the whole of bullheading — the one method that never brings the influx to surface.

**`INJECTION_THRESHOLD_PSI` is the load-bearing part.** A zone does not start accepting mud the moment it is overbalanced: filter cake, near-wellbore skin and relative permeability have to be pushed through first. Without the threshold, every kill flown a few tens of psi above pore pressure would quietly bleed into the formation for hours, and bullheading would need no pressure worth speaking of. `INJECTION_INDEX` is well below the productivity index in the other direction, because a gas sand gives up gas far more readily than it accepts mud.

Gas is what goes back first, since it sits against the zone it came from — but **only while it is still there**, which `GAS_AT_ZONE_TOLERANCE_FT` enforces. Push on a well whose gas has risen to surface and you squeeze mud into the formation and leave the gas exactly where it was. Bullheading late is why the method has a reputation for not working.

**Mud that went into the formation never displaced anything**, so `_place_influx` subtracts `injected_bbl`. Without that, bullheading pushed the influx *up* the hole while claiming to squeeze it down: the pump advanced the annulus at the full rate even though every barrel was disappearing into the zone.

On the reference well, bullheading **breaks the shoe** — peak 4,714 psi against a 4,677 psi fracture — and nothing scripts that. The surface pressure needed simply exceeds what the casing seat will hold. Pumped gently enough the same well keeps its shoe, which is the trade the method is about.

### Drilling → kick → detection → shut-in (`StartState`)

**A session can start two ways, and the difference is the difference between a trainer and a simulator.** `StartState.SHUT_IN` hands over a kick already in the hole, sized and identified, with the pressures on the gauges. `StartState.DRILLING` starts on a well that is behaving; pore pressure is `DRILLING_OVERBALANCE_PSI` below the mud column, and after a stretch of *circulating* the bit drills into a zone the mud cannot hold.

Nothing about the kick is scripted. Raising pore pressure is the only thing `trigger_kick()` does — the influx arrives through the same productivity-index response as any other underbalance, so pit gain and the flow-out excess are consequences the student can watch for.

**The check worth keeping.** Nothing tells the engine what SIDPP should read. The student drills in, takes an influx, shuts in, and the drill pipe gauge settles wherever the volume balance puts it. `tests/test_drilling_kick.py` asserts it equals pore pressure minus mud hydrostatic — the kill sheet identity — to within 5 psi. If the forward and inverse models ever stop describing the same well, that test says so.

Three things that fell out of building it:

- **The downhole truth is hidden until the well is shut in** (`Session.downhole_visible`). A rig has no bottomhole gauge and no pore pressure readout; showing them turns kick detection, which is the skill, into reading a number. The trace withholds the same fields, or a chart would leak what the gauges do not. It comes back once they have shut in, where it is a teaching aid rather than a giveaway.
- **An undetected kick accelerates**, because gas replacing mud lightens the annulus, which deepens the underbalance. At 60× it is over between two glances. `Session._should_slow_down()` drops the clock to 1× once the flow line shows something wrong — once only, or the student could never speed back up to circulate out. This does not make the well easier; it gives back the seconds a driller at the panel would have had.
- **The productivity index has to be higher for a drilling start** (`LIVE_PRODUCTIVITY_INDEX`). A tight zone bleeding in at a fraction of a barrel a minute is realistic and useless as an exercise — the pit gain takes an hour to become visible.

Drilling ahead is deliberately overbalanced, so the bottomhole-pressure band check and `seconds_outside_band` are gated on `kick_taken`. Without that, correct practice before the kick scores as an excursion.

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

One page, one poll loop; only the visible screen redraws. Everything is served from `static/choke_panel.html`, still self-contained — a rig has no internet and a test pins that. The operator screens and their order follow the reference console in `simulator_screens/sim_screen_expectations/`.

| Screen | What it is for |
| --- | --- |
| **Choke Panel** | Drill pipe and casing dials, lamps, the choke, both pressure charts, the event log. |
| **DCU** | Where a kick is *noticed*: pit deviation, return flow, standpipe, both pump rates, alarms with set points, depths and stroke counter. Pump and **stripping** controls. |
| **BOP** | The control panel — red remote panel for a surface stack, monitoring screen for a subsea one. |
| **SP Manifold** | Standpipe manifold and the whole circulating system. |
| **Choke Manifold** | The manifold, plus all four chokes with their own handles and lineup. |
| **Trip Tank** | Tank, active and reserve pits, the transfer pumps, level alarms, and whether the tank is actually in service. |
| **Trends** | Stacked tracks or the reference panel's overlaid parallel axes, against time or strokes. |
| **Well** | Cross-section with live fluid zones. Instructor-side in a class. |
| **Kill sheet** | Built from the student's own readings; plan against actual. |
| **Debrief** | Verdict, measures against their limits, and what happened. Instructor-side in a class. |

**Manifolds are two screens because they are two panels in two places** and a student is sent to one or the other. **The DCU and the choke panel are deliberately different jobs**: one is where you notice something is wrong, the other is where you do something about it, and merging them is how a trainer teaches students to stare at the choke while the pit gain runs away behind them.

**Trends offers both forms because the audience is split.** Stacked tracks sharing one axis is the readable answer and the default; the reference panel's six overlaid parallel axes is what a student will meet if they later sit that box, so it is a toggle rather than an omission. Each axis is drawn in its own trace's colour, which is the only thing making the overlaid form legible at all.

**`node --check` proves the script parses; it does not prove it runs.** A gauge path that does not exist throws on the first poll, blanks the screen, and looks exactly like a server fault. `tests/test_panel_render.py` extracts the real script, stubs just enough DOM, and drives every draw function with a payload built by a real `Session` — under both a shut-in start and a masked drilling one, since the downhole tracks must be *skipped* rather than drawn as nulls. A companion test breaks a gauge path on purpose so the harness is known to catch it.

**There are two BOP panels because a rig has two.** A surface stack is worked from a red remote panel with a rotary selector per element; a subsea stack is watched on a screen, where what matters is each element's position and health on both pods. `gauges["is_subsea"]` picks which to draw, so the panel follows the well rather than a setting.

Two engine additions exist because a screen would otherwise be decoration:

- **`Accumulator`.** Every BOP element costs pressure to shift and the pumps put it back slowly. A student who cycles the stack because they are unsure can find they have nothing left to close with. Below the minimum the element simply does not move — consistent with the rest of the lineup model, where a thing that cannot work does not work rather than raising an error at the student.
- **`hookload_lbf`.** String weight less buoyancy, less the upward push of well pressure on the closed end of the pipe. That last term is why it belongs on the stripping screen: as surface pressure climbs the well tries to push the string out, and when hookload reaches zero the pipe has to be snubbed rather than stripped. Watching pressure gauges alone never shows it coming.

**A JavaScript parse error takes every screen down at once, and nothing else notices.** It happened: a second `const open` in one block stopped the whole script parsing, so every gauge read a dash while the API, the tests and the HTML were all fine. `TestThePanelScriptParses` now runs the page's script through **`node --check`**, and a companion test feeds it the exact mistake that got through so the guard is known to work. Node was installed for this (`winget install OpenJS.NodeJS.LTS`); the test finds it on PATH or under the winget package directory, and skips rather than approximating if it is absent — a hand-rolled substitute is exactly the kind of thing that passes on the next variation of the bug.

**The kill sheet is built from what the student read**, not from the figures the scenario was designed around. On a drilling exercise those differ: a slow shut-in takes more influx and reads higher, so a sheet computed from the designer's values answers a question nobody asked — and answers it wrongly for the kick actually taken. `state.measured_sidpp_psi` and friends latch when the pressures settle. Before that the sheet is *withheld* rather than guessed at, which is also the honest answer on a rig.

**The page is an instrument face, not a dashboard.** A choke panel is an orange console carrying white dials, black digital windows with green lettering, and grey switchgear, and the CSS variables say so: `--bg` is the console, `--sunk` is a window cut into it, `--metal` is switchgear, `--readout` is the green of a seven-segment display. Labels are silkscreened on the console *above* their window, which is why `.g .k` sits outside the black.

Two things follow that are easy to get wrong when repainting from dark to orange. **Mid-tone accents vanish.** The gold used for the briefing objective read perfectly on the old dark page and was invisible on orange; anything on the console needs to be near-black. And **prose does not belong on orange** — the event log, the tables and the charts all get their own black window, which is where the reference panel puts its readouts anyway.

**The panels are drawn the way a rig draws them**, from the references in `simulator_screens/`: round dials with needles for the pressures, the BOP stack in elevation with each element showing its own state, and gate-valve bowties on the P&ID that go green when open. A digital tile tells you the number; a needle tells you the *rate* and which way it is going without being read, and working a choke is a scan rather than a series of readings. The dial's label and unit sit in the gap at the bottom where the 270° sweep does not reach — the upper half has no clear band, since the number ring runs right across it, so a label placed there collides whatever the font size. One thing deliberately **not** copied from the reference app: its trend chart puts pit volume on a second y-axis, which the `dataviz` rules forbid and which the separate panels here avoid.

Three more drawing details that were bugs first: **valves sharing a node pair are fanned along the line** (the three BOP elements all join annulus to stack, so only one was visible or clickable); **both schematics are sized by height**, since their viewBoxes are taller than wide and scaling on width alone made them a thousand pixels tall; and **depth labels are nudged apart with a leader line back to the true depth**, because the bit sits exactly on TD whenever it is on bottom.

### Assessment (`sim/assessment.py`)

Scoring is on **outcome and margin, never procedure**. Nothing checks the order of actions and nothing checks the lineup — a wrong lineup is not marked incorrect, it simply does not work, and the consequence is what shows up in the metrics.

Three things worth knowing:

- **The hard limit is shoe pressure against fracture pressure, not casing against MAASP.** MAASP is a surface proxy that exists because nobody can read a gauge at 7,000 ft; the simulator computes shoe pressure directly, so it uses the real thing. MAASP is still reported, because it is what the student was watching.
- **`EventLog.set_level()` is a Schmitt trigger, and that is not decoration.** Conditions are logged on their edges, but a value sitting *on* its threshold logs every crossing — a well-flown kill produced 111 entries, mostly the same one toggling. Separate set and reset points cut it to 2. The metrics still integrate against the true band; the hysteresis is for the log alone.
- **`Standard.require_kill` exists because a Driller's first circulation, flown perfectly, leaves the well live.** It becomes the scenario objective at stage 5.

Seconds off target and barrels of extra influx are time integrals, so the engine accumulates them every timestep; sampling them off the trace would lose excursions that happen between samples. The trace is sampled on an interval — a three-hour kill at `dt=0.1 s` is over a hundred thousand steps.

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
- **The presentation layer is being rebuilt, and this is the live piece of work.** The engine is sound; the screens missed. See *Faithful to the panel* below for what that means and what is done so far.
- **The Docker image has never been built** — Docker was simply not installed on the dev box, not a Windows limitation. First `docker compose up -d --build` on the Ubuntu box will shake it out.

## Faithful to the panel

**The first attempt at the screens was built without ever seeing one, and it
missed.** Not in detail — structurally. Asked whether to reproduce the
reference console or take its ideas, the answer given was "better than this
simulator", which was read as licence to design freely. What was actually
wanted, and said in the same breath, was *"tabs orientated just like the
photos"* and *"look more professional"*: **same layout, more capability.**

What came out was a web dashboard with round gauges in it — cards, headings, a
scrolling page. What was wanted is that instrument panel. The engine underneath
was never the problem.

So the target is now explicit: **faithful reproduction of the reference layout
first, capability added on top of it.** The references are the photographs in
`simulator_screens/sim_screen_expectations/`, committed at 1600px.

### What that means in practice

**A panel is one surface at fixed coordinates, not a layout.** A driller's scan
is *spatial* — they look at where the gauge is, not where a layout engine put
it this frame. So the Choke Panel is a single SVG at a fixed viewBox with
everything positioned on it, and the same will go for the rest. A gauge that
moves because a column reflowed has stopped being readable without being read,
which is the entire argument for a needle over a number.

**Every control on the face must work.** The joystick drives the choke in 5%
steps; decoration would be worse than nothing, because a student learns the
panel and then finds half of it inert.

**The orange is the console, not the page.** Everything outside the instrument
face is slate (`--bg`); the face and its raised areas use `--console*`. A whole
page of console orange reads as a website with a colour scheme rather than a
panel bolted into a room.

Done so far:

- **Choke Panel.** Two large bolted gauges with dual psi/bar scales as the
  centrepiece; their labels and digital readouts sit **above** them on one line
  with the kill line window, so the top edge reads as a row of numbers and the
  gauges below are pure needle. The joystick is on the centreline between them
  with the choke position dial (scaled in eighths) above it. Choke control
  bottom left, and **everything pump-related in one block** bottom right —
  stroke rate, stroke counter, its reset, and both pump controls. Spread across
  the panel those read as unrelated instruments; together they are one system.
  `kill_line_psi` was added to the gauges for it — the engine had no such
  reading.
- **DCU.** Pit deviation and return flow down the left with their alarm blocks,
  torque/RPM/WOB across the top, the stroke, ROP and depth windows in the
  middle, standpipe and both pump rates along the bottom, and on the right a
  drawworks lever and a top drive cluster with the auto-driller.

### Controls, and the trap under them

**Every control drawn on a face must work.** A student learns the panel and
then finds half of it is a picture, which is worse than it not being there. A
test scrapes every `data-face` value out of the page and asserts each appears
in `faceAction`'s switch.

**Handlers go on the SVG host, attached once — never on its children.** The
face redraws four times a second and each redraw replaces the host's entire
`innerHTML`, so anything bound to a child is gone by the next poll: a click
lands on a node already thrown away, and a drag dies mid-gesture. That is
exactly why the joystick and the choke selector did nothing for a while. The
host survives because only its contents change, and the current gauges hang off
it (`host._gauges`) rather than being captured in a closure that would go stale
with them.

### Open: a 422 on two of the controls

The choke selector and the travel-speed steppers reach the server and come back
**422 Unprocessable Content**. The same bodies posted directly return 200, and
`Session.select_choke` / `set_choke_speed` behave correctly when called in
Python — so the routes and the models are right and the fault is in what the
page sends. Not yet found. Next step is to log the actual request body out of
`send()`.

**A related trap, already paid for once:** the dev server was started without
`--reload`, so newly added routes 404'd while the source clearly had them. If a
route looks missing, check the server was restarted before checking the code.

Still to do: the **instructor station** — the biggest structural change of the
three. The reference is a fixed-height admin console with a fifteen-entry left
navigation tree (Load Exercise/Recording, Fault Induction Panel, Kill Sheets,
Drilling Plots Report, Operator Panels…) and an *Operator Panels* view that
opens a student's real screens. Ours is a flat scrolling page of tiles with no
navigation at all.

The groundwork for Operator Panels is in: **`/instructor/panel?classroom=&student=`
serves the same page the student is looking at**, polling the instructor's
drill-down endpoint instead of `/api/me`. It is read-only and the controls are
visibly dimmed — an instructor nudging a choke while a student flies it would be
indistinguishable to them from the well doing it.

### The lesson worth keeping

**Do not build screens blind.** Roughly 7,800 lines went in before anyone
looked at a pixel, because the browser extension would not connect and the work
carried on anyway. The render harness in `tests/test_panel_render.py` is
genuinely useful — it proves every draw function executes against a real
payload — but "does not throw" is a far weaker claim than "reads correctly",
and no test was ever going to catch *this is the wrong shape entirely*. Get
eyes on a screen before building the next one.

## Making hole (`sim/drilling.py`)

The only part of this simulator that is not about well control, and it earns
its place for one reason:

**A drilling break is a kick indicator, and it arrives before the pit gain.**
Penetration rate rises as the chip hold-down effect weakens — that is, as the
mud becomes less overbalanced. Drill into a zone the mud cannot hold and the bit
speeds up *because* the well is going underbalanced. Measured on the reference
well: the rate **doubles within 30 seconds**, while the pit has gained 0.16 bbl.
Nothing tells the bit a kick is coming; it reads the same bottomhole pressure as
everything else.

**`NOMINAL_OVERBALANCE_PSI` is 500, not the static 150.** A well drilling ahead
does not carry `DRILLING_OVERBALANCE_PSI` — circulating adds annular friction on
top, and the bit is working against about 500 psi. Normalising against the static
figure put the baseline hold-down at 0.34, so a well drilling normally sat at a
third of its nominal rate and the break came out at *five* times: theatrical, and
not what a driller sees. Calibrated against the engine, the baseline is 0.99 and
the break is 2.0×.

**`Simulator.drilling_ahead` is a lock-out, not a status.** BOP closed, pump off,
rotary stopped, no weight on bit, or bit off bottom — any one of them and nothing
in this module runs. That is what keeps every validated kill exactly as it was:
they are all shut-in or circulating exercises, and none can reach the drilling
model. A parametrised test asserts each condition stops the bit dead.

Weight on bit comes **off the hookload**, which is the relation a driller
actually works to — lower the block until the indicator drops by the weight you
want on bottom. Picking up off bottom drops the weight to zero rather than
leaving a stale number on the gauge. New hole is new volume, so drilling ahead
slowly consumes mud from the active pits.

The exponents are the familiar Bourgoyne-Young *shape* — rate roughly linear in
weight, fractional power of speed — with coefficients chosen so the reference
well drills at a plausible 60 ft/hr. **Nothing here should be used to plan a bit
run.**

## The instructor's hand (`sim/recording.py`, `Simulator.inject_fault`)

**A thrown fault and a scheduled one are the same call.** `Simulator.inject_fault`
is what `_apply_faults` invokes when a scenario fault's moment arrives, *and*
what the instructor's button invokes. Deliberately one path: a student who can
tell that a human just did something to them starts flying the instructor rather
than the well, and the surest way to keep the two indistinguishable is for there
to be nothing to keep in step. A test asserts the two produce identical event
signatures, and another scans the whole student payload for the words that would
give it away.

Faults are still triggers rather than scripts — `inject_fault` only chooses the
*moment*. What follows is whatever the well does about it.

**Recordings are data, not a paused engine.** A recording holds the trace, the
event log, the kill sheet the student built and the assessment; replaying it
means drawing those again, not re-running physics. That matters: a re-simulation
could disagree with what the student actually saw, which would make it useless
as evidence. Recording is unconditional — storage is not the constraint, and a
debrief you have to remember to switch on is one you do not have when you need
it. Runs are captured automatically before anything destroys them (a restart, a
class ending), because those are exactly the moments a debrief is about to be
wanted and the evidence is about to go.

Two failure modes are handled rather than raised: **losing a recording must not
lose the class**, so an unwritable volume is logged and swallowed; and one
corrupt file must not make the whole listing unavailable, so `list()` skips what
it cannot parse. Files are plain JSON in `WELLCONTROL_DATA_DIR`, one per run,
named to sort by time — the access pattern is "list them" and "open one", which
a directory does perfectly well.

The filename fragment built from a student's name **strips dots as well as
separators**. Separators alone would stop traversal, but `../../etc/passwd`
still arrives as `..-..-etc-passwd`, which is a filename nobody should have to
reason about.

The console reviews a recording by drawing it: the pressure traces, the verdict
with every metric against its limit, and the whole event log. **A recording that
cannot be looked at is not a debrief**, so the viewer is part of the feature
rather than a later addition.
- **Open question**: the stripping migration allowance, where the planner and the simulator disagree by about 4×. Written up under *Stripping* below; nothing has been changed on the strength of it.
- **Importers**: done and validated against real-shaped files in `test_data/`, with an independent answer key. See *Importing real geometry* below.

## The mud system (`sim/equipment.py`)

Stage 2. The trip tank, the pits, the flow paddle and the alarms — the surface
instruments a kick is actually *detected* on, as opposed to the ones it is
killed on.

**Pipe displacement reached no surface vessel at all.** That was the gap. Steel
run into a shut-in well became pressure, correctly, but steel pulled out of an
open hole went nowhere: a trip moved no volume, no tank level changed, and a
kick taken while tripping — which is a large share of them — was invisible.
`_settle_surface_volume` now puts that volume somewhere real, and the exercise
falls out of the same balance as everything else. On the reference well, pulling
60 ft drops the tank by exactly the 1.46 bbl of steel; with the well flowing the
tank *rises* 7.78 bbl instead, and the discrepancy is the influx.

**The trip tank is in service only with the valve open *and* the fill-up pump
running.** That is the lineup a driller is taught — the pump circulates into the
hole, the overflow comes back, the level shows the net. Open the valve alone and
the volume goes to the active system instead, where a barrel and a half does not
show against 800. The consequence is the rig's: the trip is not measured, and
the kick stays hidden until the pit gain is much larger.

**A reserve-to-active transfer deliberately reads as a gain.** The level went up
and the gauge cannot tell why; re-zeroing after a transfer is the driller's job.
That is the false gain that teaches students to distrust the pit alarm, and
`Session.zero("pits")` is how they clear it.

**`Alarm` is a Schmitt trigger for the same reason `EventLog.set_level` is.** A
value sitting on its set point crosses it repeatedly, and a lamp that flickers
is a lamp a student learns to ignore. Disarming silences the horn but leaves the
lamp lit, so the instructor can see what was silenced.

**The flow alarm sits on the differential, not the return rate.** Alarming on
the rate would fire every time the driller changed pump speed, which is how a
real flow alarm gets switched off and left off. `flowing_with_the_pump_off` is
the indicator that is never anything else.

Two pumps and four chokes, and neither is about matching a photograph — both
faults this simulator already throws (`pump_failure`, `choke_plugged`) had
nothing to change over *to*, so the fault was simply the end of the exercise.
Both now bite whichever unit is actually in service, and the answer is what a
rig would do. Two consequences worth knowing:

- **`total_pump_spm` is the combined flow expressed as spm of pump 1.** Every
  relation in `engine.py` is written in spm against the slow circulating rate;
  converting back through pump 1's displacement keeps all of them correct
  without rewriting any, and reduces exactly to the old behaviour on one pump.
- **Chokes in parallel add.** Each open one passes its own flow at the same
  pressure drop, so a second choke left cracked lets past more than the handle
  in the driller's hand accounts for. `active_chokes()` used to claim the engine
  did this; it did not, and now it does.

## The classroom (`sim/classroom.py`, `sim/auth.py`, `sim/config.py`)

Up to a roomful of students, each on their own well, all flying one exercise
that an instructor starts, pauses and watches.

**The clock moved to the server, and that overturns a documented decision.**
`session.py` used to advance time *on demand* — each request measured the wall
time since the last one and stepped the engine that far, so nothing ran when
nobody was watching. That is right for one person and does not survive a class:
*pause all* has to stop the wells rather than the browsers, a student reading
their kill sheet would sit on a frozen kick, and the instructor's monitor
stepping fifteen engines races each student's own polling for the same engine.

So a `Classroom` owns a clock and drives every session on it. What is kept from
the old design is the part that made it testable: the clock is still injected,
`classroom.py` still has **no web dependency**, and `Classroom.tick()` is an
ordinary method a test calls in a loop. The asyncio task that calls it in
production lives in `api.py`, because that is runtime plumbing.

`Session.advance()` is unchanged and still banks elapsed time before a speed
change — which matters more now, not less: an instructor's speed request
arrives between ticks, and without banking the seconds elapsed at 1× would be
re-integrated at 10×. `tests/test_classroom.py` pins it by moving the clock
*without* ticking, which is the only way that gap is reachable; a version of
that test that ticked right up to the change passed against a deliberately
broken implementation.

**Students are not accounts.** A class is fifteen people for an afternoon;
creating and deleting fifteen accounts per class means password resets during a
lesson and a permanent user table full of people who came once. The only
durable credential is the instructor's. Students get in with a **class code** —
eight characters with no O/0, I/1 or S/5 because it is read off a whiteboard —
which expires on its own. Tokens are HMAC-signed and carry their own expiry, so
there is no session table and a restart does not evict the room.

Four things that are easy to get wrong here:

- **The roster is checked on every request, not just at join.** That is what
  makes dropping a student effective against a token that is still
  cryptographically valid. There is no blacklist and there does not need to be.
- **A wrong code and an expired one give the same answer.** `by_code` does not
  find closed or expired classes rather than reporting them, so somebody
  probing cannot tell a stale code from a guess.
- **Rate limiting keys on the forwarded address only when `trust_proxy` is
  set.** Behind Nginx Proxy Manager every request arrives from the proxy, so
  throttling on that would take the whole class out the first time anyone
  mistyped a code — and on a directly exposed server the header is client-
  controlled and must not be believed.
- **`Classroom` validates its exercise at construction.** Sessions are built per
  student at join time, so an unknown scenario key would otherwise sail through
  class creation and surface as an error thrown at the first student to join,
  with the code already read out.

**The engines have two callers now, and they overlap.** A sync FastAPI handler
runs in a threadpool worker while the clock driver runs on the event loop, so a
student's request and a tick are genuinely inside the same `Session` at the same
instant. Cheapest symptom is elapsed time counted twice, because both read
`_last_tick` before either writes; worst is a half-rebuilt fluid column, or a
roster dictionary mutated while `tick_all` iterates it.

One reentrant lock on `ClassroomStore` covers every class — stepping fifteen
wells is well under a millisecond, so contention is not the constraint and a
single lock cannot be taken in the wrong order. `@exclusive` in `api.py` applies
it to every route that touches a class, and a **structural test** asserts that
every such route carries it. That test is the real guard: the race is not
reliably reproducible, and the threaded stress test beside it passes with the
lock removed, so it proves absence of deadlock rather than presence of the bug.

**Never hold that lock across an `await`.** The driver takes it on the event loop
thread, so a coroutine suspending while holding it would block the loop that has
to resume the handler holding the other end — a deadlock, not a slowdown.
Everything that takes it is synchronous and must stay that way.

**`Session.instructor_view()` is the one place `downhole_visible` is
deliberately bypassed.** The mask exists so a student detecting a kick cannot
read the answer off a gauge; an instructor watching fifteen wells has the
opposite problem. The tile also reports `downhole_visible` itself, so the
instructor knows whether that student is still meant to be detecting.

Speed is the instructor's, for the whole class — there is no student speed
route at all, not a forbidden one. The exception is `_should_slow_down`, which
drops a single student to real time when *their* well starts flowing; that is a
safety behaviour and should not wait for the instructor to notice.

### Running it

`Dockerfile` and `docker-compose.yml` target an Ubuntu box behind Nginx Proxy
Manager, which terminates TLS. **One uvicorn worker, and that is not a tuning
choice** — classes, rosters and every engine live in this process's memory and
one asyncio task steps them, so a second worker would get its own empty set of
classrooms and half the room would land in a class that does not exist.

Measured on an i7-1165G7: one circulating well runs at **5,300 engine steps per
second**, 530× realtime on one core. Fifteen wells need 150 steps/s at 1× and
1,500 at 10× — comfortable. Fifteen simultaneously at 60× would not fit on one
core, but that is not a thing a class does; if it ever matters, widen `dt` as
the speed multiplier rises rather than adding processes.

Settings come from the environment (`sim/config.py`), never the repository.
With nothing set a password is generated and printed to the log, Jupyter-style
— the alternative is either an unprotected console or a server that refuses to
start on a laptop. `python -m wellcontrol.sim.auth` prints a hash for the env
file.

### What the student sees, for now

`/` serves the same page solo or in a class; it probes `/api/me` to find out
which, because the class token is an httpOnly cookie it cannot read. Every call
goes through `base()`, which is the only thing that knows the difference.

In class mode the **Well cross-section and Debrief tabs are removed** — a live
cross-section shows where the gas is, which is what the student is meant to be
working out. Note the schematic is still in the student's snapshot payload and
should come out of it when the screens are rebuilt in stage 3.

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

### Three tiers of evidence

Worth being precise about what is actually verified, because the tiers are not equal:

1. **Published.** `test_data/second_source/` reproduces a worked drill pipe pressure schedule from drillingformulas.com — its inputs and its printed table. `TestPublishedSchedule` in `tests/test_second_source.py` is the only test in the project that checks the library against the outside world. It agrees within 15 psi, and the residual is the article's own rounding (it steps by 47 psi where the exact figure is 46.57, and feeds a rounded 10.6 ppg that overshoots true BHP by 15 psi). Fed an exactly-round kill weight, the general fluid-column model reduces to the textbook straight line to 0.01 psi — which is the check that the machinery producing the *reversal* in a lateral is not inventing structure.
2. **Independent implementation.** `circulating_profile.csv` is a second model of the reference well. Agreement is a strong cross-check, not proof — two implementations can share a misunderstanding. This is what found the gas density bug.
3. **Internal consistency.** Everything else.

Still unverified: the *shape* of the choke and shoe curves between endpoints, the influx expansion profile, and the friction treatment. Closing that needs a recorded circulating trace from a real or fully worked kick with published geometry.

### Validated against `test_data/`

`test_data/` holds a rig-shaped survey (157 stations), a joint-by-joint pipe tally (405 components) and `well_control_test_data.xlsx`, whose `Well_Data` sheet is an **independently computed answer key** for the same well — expected TVDs, volumes, strokes and kill sheet. `tests/test_real_files.py` checks the library against it. Those numbers did not come from this library, so **when one of those tests fails, assume the library is wrong**. The data is synthetic but realistically shaped; it is committed, so the tests are portable.

Three more things real files broke:

- **`Item_No` is a row index, not a joint count.** The `count` alias list included `"no"`, which matched it, and the tally imported as 2,554,260 ft instead of 12,500 — a 200× error, silently. Fixed three ways, all worth keeping: the `count` aliases are now specific (`count`/`joints`/`jts`/`qty`…), `_looks_like_a_row_index()` rejects a 1,2,3… sequence, and `_cross_check_against_cumulative()` hard-fails on a >2% disagreement with the file's own cumulative column. **A tally must be cross-checked against something, not just parsed.**
- **Header matching is on token boundaries, not substrings.** The obvious fix above — requiring longer aliases — broke `MD_ft`, because `md` is two characters. `_resolve_columns` now runs three passes (exact header, then token, then substring for aliases of 4+ characters), so `MD_ft` → `["md","ft"]` matches `md` while `wood` → `["wood"]` does not match `od`. Widening an alias list is the thing most likely to reintroduce a silent misread; add a test to `test_real_files.py` when you do.
- **The survey stops behind the bit.** Surveys are taken at the MWD sensor, 40–100 ft up the BHA, so the deepest station is routinely short of TD — here 12,460 ft against a bit at 12,500 ft. This is normal, not a malformed file. `Survey.extended_to(md)` holds the last inclination and azimuth through the unsurveyed hole (a tangent projection, which is what a directional driller reports), and `build_well` calls it unconditionally. It refuses beyond 500 ft, where holding an angle stops being a projection and becomes a guess.

**Most real wells are deviated, not horizontal.** This one holds 35°. `trajectory_from_survey` can therefore return `DEVIATED` and override the sidebar selection, so `DEFAULTS` carries `surface_deviated` and `subsea_deviated` entries — all six `BOPLocation` × `Trajectory` keys exist, and a test pins that.

Expect the next real export to bring header spellings, unit rows, footers and merged cells this parser has not seen; extend `SURVEY_ALIASES` / `TALLY_ALIASES` and the validation rather than loosening the rejections.

## Stripping is half an operation

A kick with the bit off bottom needs stripping *then* a circulating kill. `strip_then_kill(well, "drillers" | "wait_and_weight")` returns a `KillSequence` pairing both phases, using `WellState.with_bit_at()` to rebuild the string with the bit on bottom for the second. It reports combined time and which phase peaks at the shoe — on the horizontal reference well that is the *stripping*, which planning the kill alone would miss.

## Plots

Follow the `dataviz` skill's rules, which the current plots already satisfy: no dual-axis charts (influx volume and depth are separate panels, never twin y-axes), solid hairline gridlines with dashing reserved for threshold lines, categorical slots 1–3 of the reference palette only, and legends only when a panel has two or more series. Both light and dark themes are defined; the app auto-detects which to use.

## Scope

These are engineering aids, not a substitute for certification, a company well control manual, or the judgement of a competent person on the rig. Where a measured value exists (slow circulating rate, choke line friction, PVT data), prefer it over anything computed here, and say so in the docstring.
