# Engine notes — the full histories

Moved out of CLAUDE.md to keep the always-loaded file lean. **Read the
matching section here before changing the subsystem it describes** — these
are the calibration points, the bugs that shaped the code, and the design
decisions with their reasons. CLAUDE.md keeps the compressed contracts; this
file keeps the why.

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

### Assessment (`sim/assessment.py`)

Scoring is on **outcome and margin, never procedure**. Nothing checks the order of actions and nothing checks the lineup — a wrong lineup is not marked incorrect, it simply does not work, and the consequence is what shows up in the metrics.

Three things worth knowing:

- **The hard limit is shoe pressure against fracture pressure, not casing against MAASP.** MAASP is a surface proxy that exists because nobody can read a gauge at 7,000 ft; the simulator computes shoe pressure directly, so it uses the real thing. MAASP is still reported, because it is what the student was watching.
- **`EventLog.set_level()` is a Schmitt trigger, and that is not decoration.** Conditions are logged on their edges, but a value sitting *on* its threshold logs every crossing — a well-flown kill produced 111 entries, mostly the same one toggling. Separate set and reset points cut it to 2. The metrics still integrate against the true band; the hysteresis is for the log alone.
- **`Standard.require_kill` exists because a Driller's first circulation, flown perfectly, leaves the well live.** It becomes the scenario objective at stage 5.

Seconds off target and barrels of extra influx are time integrals, so the engine accumulates them every timestep; sampling them off the trace would lose excursions that happen between samples. The trace is sampled on an interval — a three-hour kill at `dt=0.1 s` is over a hundred thousand steps.

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

`/` probes `/api/me` to find out whether there is a class behind it, because
the class token is an httpOnly cookie it cannot read. Every call goes through
`base()`, which is the only thing that knows the difference.

**The no-class branch is a development harness, not a product mode.** There is
no solo mode and there never will be: every run is set up by an instructor,
even a class of one. What the branch buys is a server you can point a browser
at without standing a class up first, which is how the screens are built and
how `tests/test_sim_api.py` drives them. Nothing student-facing should be
designed around it — see *The agreed choke panel* for what that settled about
the header.

In class mode the **Well cross-section and Debrief tabs are removed** — a live
cross-section shows where the gas is, which is what the student is meant to be
working out. Note the schematic is still in the student's snapshot payload and
should come out of it when the screens are rebuilt in stage 3.

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

