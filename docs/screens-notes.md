# Screens notes — the full histories

Moved out of CLAUDE.md to keep the always-loaded file lean. **Read the
matching section here before touching `sim/static/choke_panel.html`, either
BOP face, or any screen layout** — these are the measured geometries, the
drawing lessons that were bugs first, and the decisions that must not be
un-made. CLAUDE.md keeps the compressed rules; this file keeps the why.

#### The screens

One page, one poll loop; only the visible screen redraws. Everything is served from `static/choke_panel.html`, still self-contained — a rig has no internet and a test pins that. The operator screens and their order follow the reference console in `simulator_screens/sim_screen_expectations/`.

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

**Manifolds are two screens because they are two panels in two places** and a student is sent to one or the other. **The DCU and the choke panel are deliberately different jobs**: one is where you notice something is wrong, the other is where you do something about it, and merging them is how a trainer teaches students to stare at the choke while the pit gain runs away behind them.

**Trends offers both forms because the audience is split.** Stacked tracks sharing one axis is the readable answer and the default; the reference panel's six overlaid parallel axes is what a student will meet if they later sit that box, so it is a toggle rather than an omission. Each axis is drawn in its own trace's colour, which is the only thing making the overlaid form legible at all.

**`node --check` proves the script parses; it does not prove it runs.** A gauge path that does not exist throws on the first poll, blanks the screen, and looks exactly like a server fault. `tests/test_panel_render.py` extracts the real script, stubs just enough DOM, and drives every draw function with a payload built by a real `Session` — under both a shut-in start and a masked drilling one, since the downhole tracks must be *skipped* rather than drawn as nulls. A companion test breaks a gauge path on purpose so the harness is known to catch it.

**There are two BOP panels because a rig has two.** `gauges["is_subsea"]` picks which to draw, so the panel follows the well rather than a setting. See *The stack* below for what each one carries and why the engine had to grow a real stack before either could be built.

Two engine additions exist because a screen would otherwise be decoration:

- **`Accumulator`.** Every BOP element costs pressure to shift and the pumps put it back slowly. A student who cycles the stack because they are unsure can find they have nothing left to close with. Below the minimum the element simply does not move — consistent with the rest of the lineup model, where a thing that cannot work does not work rather than raising an error at the student. It now carries the rest of the closing unit too; see *The stack*.
- **`hookload_lbf`.** String weight less buoyancy, less the upward push of well pressure on the closed end of the pipe. That last term is why it belongs on the stripping screen: as surface pressure climbs the well tries to push the string out, and when hookload reaches zero the pipe has to be snubbed rather than stripped. Watching pressure gauges alone never shows it coming.

**A JavaScript parse error takes every screen down at once, and nothing else notices.** It happened: a second `const open` in one block stopped the whole script parsing, so every gauge read a dash while the API, the tests and the HTML were all fine. `TestThePanelScriptParses` now runs the page's script through **`node --check`**, and a companion test feeds it the exact mistake that got through so the guard is known to work. Node was installed for this (`winget install OpenJS.NodeJS.LTS`); the test finds it on PATH or under the winget package directory, and skips rather than approximating if it is absent — a hand-rolled substitute is exactly the kind of thing that passes on the next variation of the bug.

**The kill sheet is built from what the student read**, not from the figures the scenario was designed around. On a drilling exercise those differ: a slow shut-in takes more influx and reads higher, so a sheet computed from the designer's values answers a question nobody asked — and answers it wrongly for the kick actually taken. `state.measured_sidpp_psi` and friends latch when the pressures settle. Before that the sheet is *withheld* rather than guessed at, which is also the honest answer on a rig.

**The page is an instrument face, not a dashboard.** A choke panel is an orange console carrying white dials, black digital windows with green lettering, and grey switchgear, and the CSS variables say so: `--bg` is the console, `--sunk` is a window cut into it, `--metal` is switchgear, `--readout` is the green of a seven-segment display. Labels are silkscreened on the console *above* their window, which is why `.g .k` sits outside the black.

Two things follow that are easy to get wrong when repainting from dark to orange. **Mid-tone accents vanish.** The gold used for the briefing objective read perfectly on the old dark page and was invisible on orange; anything on the console needs to be near-black. And **prose does not belong on orange** — the event log, the tables and the charts all get their own black window, which is where the reference panel puts its readouts anyway.

**The panels are drawn the way a rig draws them**, from the references in `simulator_screens/`: round dials with needles for the pressures, the BOP stack in elevation with each element showing its own state, and handwheels on the manifold faces whose hub lamp goes green when the valve is passing. A digital tile tells you the number; a needle tells you the *rate* and which way it is going without being read, and working a choke is a scan rather than a series of readings. The dial's label and unit sit in the gap at the bottom where the 270° sweep does not reach — the upper half has no clear band, since the number ring runs right across it, so a label placed there collides whatever the font size. One thing deliberately **not** copied from the reference app: its trend chart puts pit volume on a second y-axis, which the `dataviz` rules forbid and which the separate panels here avoid.

Two more drawing details that were bugs first (a third — fanning valves that shared a node pair along their line — went with the P&ID overview card it belonged to): **the well schematic is sized by height**, since its viewBox is taller than wide and scaling on width alone made it a thousand pixels tall; and **depth labels are nudged apart with a leader line back to the true depth**, because the bit sits exactly on TD whenever it is on bottom.

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

**And it goes wall to wall.** The face fills the viewport's full width and
breaks out of `main`'s padding to do it (`width: calc(100% + 32px)` against
`margin: -16px -16px`), so console orange runs to the edge of the screen the
way it runs to the edge of the box on a rig. Height follows from the width
through the viewBox's aspect and is deliberately **not** capped: fitting the
height is what left dark strips down either side, and on a short screen the
lamp row and the cards below it scroll instead, which is the right thing to
give up. The screen-select tabs are centred over it with `justify-content:
safe center` — plain `center` spills an overflowing strip off *both* ends and
makes the first tab unreachable.

**Never put a `max-height` on one of these faces.** An SVG given an explicit
width *clips* rather than scales when its height is capped, and it does so
silently. This was live on the DCU: measured at 369px tall against a natural
639, so the bottom row of that screen — standpipe and both pump rates — was
simply not being drawn, on a face whose whole purpose is that a kick is
noticed on it. A test asserts neither face carries one.

**Every control on the face must work.** The joystick drives the choke in 5%
steps; decoration would be worse than nothing, because a student learns the
panel and then finds half of it inert.

**The orange is the console, not the page.** Everything outside the instrument
face is slate (`--bg`); the face and its raised areas use `--console*`. A whole
page of console orange reads as a website with a colour scheme rather than a
panel bolted into a room.

Done so far:

- **Choke Panel — built to `choke_panel_exact.jpg`, verbatim.** Every
  coordinate in `FACE` and in `drawChokeFace` is a pixel measurement off that
  image scaled by 1440/942 into the viewBox, because the mockup's panel
  occupies 942x417 px of it. Casing gauge left at cx 320, drill pipe right at
  cx 1119, both r 228 so they dominate the face; their labels and digital
  windows sit **above** them on one line with the kill line window, so the top
  edge reads as a row of numbers and the gauges below are pure needle. The
  centre is a single vertical stack — choke position dial (eighths), its
  window, the joystick, the Choke2/Choke1 selector, choke control — so
  everything about the choke reads top to bottom in one line. **Everything
  pump-related in one block** bottom left, with RESET on bare console above
  the counter it clears. `kill_line_psi` was added to the gauges for it — the
  engine had no such reading.

  Three things that follow from the gauges growing by a third, and are worth
  keeping if they grow again: **the bezel and every ring inside the gauge are
  fractions of `r`, not fixed insets.** A number ring at `r - 35` drifts
  outwards as the gauge grows, leaving a hole in the middle and lettering too
  small to read across a cabin; the bezel at a fixed 11 units becomes a
  hairline. Same for the small choke position dial, where fixed insets put the
  eighths ring on top of the hub. And **the face is capped by width, not
  height** — `max-width: min(100%, calc((100vh - 200px) * 2.25))`. A
  `max-height` on an SVG with an explicit width makes the browser *clip*
  rather than scale, which silently loses the pump block off the bottom.

  Two labels are deliberately gone from the earlier version: **"Hold"** on the
  joystick arc, which named something the control does not do — the buttons at
  the ends of that arc step the choke 5% per click — and **"spm"** under the
  pump steppers, which fell off the bottom edge of the face.
- **DCU.** Pit deviation and return flow down the left with their alarm
  blocks, torque/RPM/WOB across the top, the stroke, ROP and depth windows in
  the middle, standpipe and both pump rates along the bottom, a drawworks
  lever and a top drive cluster with the auto-driller, and on the far right
  **the mud pump cluster** — the same one the choke panel carries. A kick is
  noticed on this screen and the first thing done about it is usually to the
  pumps, so making the student change screens to touch them is making them
  look away from the gauge that just told them something.

  **`mudPumpBlock` draws it for both faces.** The DCU stacks it into a column
  because that is the shape of the space the reference leaves on the right of
  that screen and the choke panel lays it out across the bottom, but the
  windows, the reset, the steppers and the shutdowns are the same controls in
  the same order. Drawing them separately is how two panels drift into
  disagreeing about what a pump control looks like. The viewBox widened from
  1080 to 1300 to carry it.

  **The alarm blocks are controls now, not pictures of controls.** The ON/OFF
  switch and both set points were inert: the numbers were live but nothing on
  the block could be touched, so a student could see the pit alarm was going
  to fire at 10 bbl and had no way to move it. `alarmBlock` takes a `keys`
  object naming its five `data-face` actions, so the two blocks dispatch to
  different alarms through one drawing. Set points move independently —
  `set_alarm` takes each on its own, so sending only the one that changed
  cannot drag the other back to whatever the page last drew — and low is
  clamped at or below high, because crossed set points give an alarm that is
  always in one state and tells a student nothing. `miniStepper` exists
  because `stepper`'s 30-unit buttons do not fit a 120-unit row, and a set
  point a student cannot reach is the same as no set point.

  **Reset Zero** re-marks the gain against the level that is there now. A
  reserve transfer raises the active level without the well having done
  anything, and a pit alarm with no way to clear it is a pit alarm that gets
  ignored — see *The mud system*.

  **Return flow reads either bbl/min or percent of flow in**, switched on the
  face. The reference carries a switch rather than picking one, and it is
  right to: 100% is balanced whatever the pump is doing, so a student watching
  percent sees the well contributing without holding the circulating rate in
  their head — but percent says nothing at all with the pump off, and that is
  exactly when flow out is the loudest indicator there is. The switch never
  reaches the server, because the well has no opinion about units. **The set
  points stay in bbl/min either way**, because the alarm is on the
  *differential* rather than on the return rate.

  Pit deviation and gain/loss are deliberately **one gauge**. In the engine
  `deviation_bbl` and `gain_bbl` are the same number, and the reference panel
  labels that single dial "Pit Deviation" with LOSS/GAIN across its face.
- **BOP — both of them**, and this one needed the engine before it needed a
  pixel, three times over: a real stack, then **two-handed operation** across
  everything on the closing unit, then both stacks reconfigured to what the
  rigs actually carry. Surface is an elevation with the preventers drawn as
  preventers; subsea is a fifteen-row P&ID with seven outlet lines running to
  a kill and a choke header. See *The stack* below — it is the longest section
  here and it earns it.
- **SP Manifold, Choke Manifold and Trip Tank — built to `IMG_1195/96/97.PNG`**
  (in `simulator_screens/`), each as one wall-to-wall face, **coordinates
  measured off the photographs** the way the choke panel's were — the first
  pass took the idiom and improvised the layout, and that was not what was
  asked for. The idiom: pale panel, oxide-red pipework reusing the subsea
  face's pipe helpers plus `pipeBlock` junction fittings, **handwheels with a
  lamp in the hub** on the manifolds and **P&ID bowties on the trip tank
  face** (its photograph uses them), dual-scale psi/bar gauges hung on thin
  black stems with the reading in a window under a large hub, white
  Windows-style **spinners** for the choke positions and alarm set points,
  pumps as volutes, tanks as cylinders with a *light* top ellipse carrying
  the capacity in black. Everything dispatches through `faceAction`
  (`valve:`/`pump:`/`ckpos±:`/the trip tank actions), so the scrape test
  covers it all. What the photographs forced on the engine:
  - **The manifolds became real plumbing** (`SPM_UPPER/LOWER/EAST`,
    `CKM_WEST/EAST/LOWER` nodes), segmented like the stack bore. The SP
    manifold has **two standpipes on two columns** — mud pump 1 up the west
    (the `STANDPIPE` node) to standpipe 1, mud pump 2 up the east to
    standpipe 2 — with section wheels on both runs; the choke manifold's
    header is split by two section wheels at the To Pits riser, with each
    choke's isolation wheel on its riser and a lower run joining the choke
    and kill lines with the SP feed in its middle. Every wheel is a real
    valve; the section-valve defaults (headers open, lower runs mostly
    closed) keep every existing lineup working unchanged.
  - **Two method lineups changed, both toward the rig.** `pump_to_kill_line`
    now hangs off the SP manifold's lower run, so **lubricating closes the
    standpipe valve rather than the pump discharge** — the pump is how mud
    reaches the lower run at all. **Reverse circulation feeds the kill line
    from mud pump 2 through the east column** and takes the string returns
    through `standpipe_to_manifold` to the choke manifold's *lower run* and
    up through the choke line entry to remote choke 1; the closed section
    valves (`lower_west_valve`, `header_east_valve`) are what keep the two
    flows apart, which is what section valves are *for*. See
    `scenario._REVERSE`/`_LUBRICATE` and their `_CLOSED` tuples.
  - **The cement lines stay blanked** (`Node.CEMENT_HEAD/CEMENT_PUMP`, dead
    ends off the lower run): the valve moves and nothing changes, which is
    what opening it on this rig would do.
  - **The trip tank's fill and drain lines carry real isolation valves**
    (`tank_fill_suction`/`tank_fill_discharge`/`trip_tank_drain_valve`) that
    the engine's `_advance_mud_system` consults — a transfer pump against a
    closed valve moves nothing. They are **`Valve.off_graph`**: the volume
    moves in the balance, not the flow graph, and a graph edge into the pits
    would invent circulation routes through the pit room.
  - Deliberate departures from the photographs, all capability: the To Pits
    riser carries the `separator_to_pits` and `choke_bypass` wheels (the
    photo shows none, but an unshuttable bypass defeats the choke), the
    gauges take small captions, and the trip tank face keeps the in-service
    lamp, mark window, ZERO, shakers bowtie and the reserve pit with its
    transfer pump. The old P&ID overview card is gone (`id="pid"` pinned
    absent) and `drawLineup` lists only the stack elements.

### The stack (`sim/flowpath.py`, `Accumulator`, the BOP face)

**The screen was the reason, but the stack was the problem.** The engine had
three elements called annular, upper rams and lower rams, and one valve a side
called "the choke line". No rig has that. Asked what the fourth element on the
reference photo should do, the answer was to configure the stack properly:
*"the surface bop needs one annular at least 2 pipe rams and a blind shear ram
… all flow cross with double HCRs on each kill/choke side … the subsea BOP
needs to have 2 annulars, 2 VBRs, 2 shear rams and upper and lower choke and
kill lines with double fail-safe valves on each."* So `default_valves` became
`surface_stack()` and `subsea_stack()`, and `Simulator` builds whichever the
well has. **Both have since been configured again and that quote is history**
— read the two tables below for what is actually built.

**The surface stack was later configured again, and it now runs, top to
bottom:**

| | |
| --- | --- |
| annular | |
| upper pipe rams | |
| lower pipe rams | |
| **upper flow cross** | kill and choke outlets, two HCRs each |
| blind shear rams | |
| **lower flow cross** | kill and choke outlets, two HCRs each |

**Two flow crosses, because one outlet is one point of failure.** The upper
cross is below both pipe rams, so it is the one to take returns from with
either of them shut; the lower is below the blind shears as well. That is why
`_group` resolves "the choke line" to the **lower** outlet on both stacks now
rather than only subsea.

**The heights are the model now.** The stack bore is split into segments —
`ANNULUS` below the lowest element, `STACK_BORE_1..7` between adjacent
elements, `BOP` (the bell nipple or riser) above the top — each element is an
edge between the segments either side of it, and each outlet taps the segment
at its own height. So closing a ram isolates every outlet above it from the
well, "take returns from a cross below the ram you closed" is a real decision,
and mud pumped into a line above a closed ram deadheads instead of reaching
the well. A closed element that does not seal still passes flow — the riser
connector today, a casing shear ram on any stack that carried one. Two
approximations remain, deliberately:
the few barrels **trapped between two closed elements are not their own
pressure vessel** (no trapped-pressure-between-rams exercises), and the few
feet between outlet taps are **hydrostatically invisible** — `FluidColumn`
still sees one continuous annulus, which is fine because the tap heights
matter for connectivity, not for the weight of a column thousands of feet
tall. `TestTheOutletHeightsAreReal` in `tests/test_bop_stack.py` pins all of
it, including that the standard shut-in — annular closed, lower outlet lined
up — never notices the change.

One consequence for `return_route()`: the flowline is found **by name, not by
search**. The bore chain made the flowline path longer than the choke path,
and a breadth-first search would prefer the choke — but an open well takes the
path of least resistance, and that is the flowline at atmospheric whatever the
choke is set to.

**The two stacks are not the same shape and are not meant to converge.** This
has had to be said more than once: they are different equipment, they are
drawn differently, and a change to one is not a change to the other. The
subsea stack runs, top to bottom:

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

**Seven outlet lines: four to the choke side and three to the kill**, each
with two fail-safes, so fourteen outlet valves. `_failsafes()` builds a line,
because a stack with fourteen valves hand-listed is a stack with a transposed
node in it somewhere. **The gas relief line is the fourth choke-side line** —
it taps above the lower annular and is piped into the choke line rather than
run to surface on its own, so its valves carry `side="choke"` and the group
that opens "the choke line" finds them.

Interleaving the outlets between the rams is the whole point of a stack this
size: whichever ram is closed, there is a line below it — and since the bore
is segmented, that is now true in the flow model as well as on the drawing.

**Both shear rams are blind shears now — they cut *and* seal.** The lower set
was a casing shear (`seals=False`) until 3 Sep 2026, when Kirk made it blind.
The only non-sealing element left is the **riser connector**, and it is
non-sealing for its own reason: unlatching it walks away from the hole rather
than failing to shut it in. The face's warning logic is unchanged — only
`shears && !seals` gets a "does not seal" caption, so today no plate carries
one, and the machinery keys off the engine's flags rather than a name in case
a future stack carries a casing shear. One height consequence: the lower
blind shears sit **beneath the lower kill line's tap**, so closing them
isolates that line — only the lower choke, on the wellhead below the whole
stack, survives them.

What the two stacks share is the *outlet naming* (`upper_kill_inner` and
friends), because an outlet is the same idea on both and nothing driving them
should have to care which rig it is. What differs is the **label**: a surface
outlet valve is an **HCR**, a subsea one is a **fail-safe**, and that is a real
distinction — a fail-safe shuts on loss of control pressure and an HCR stays
where it was.

**`surface_stack()` and `subsea_stack()` list their valves top to bottom, and
both faces draw the stack by walking that list** (`stackRows` in
`choke_panel.html`). Elements and outlet groups interleave in list order, so
re-ordering a stack is an edit to `flowpath.py` and nothing else. An outlet
group takes **two rows** — inner valves on the spool's own line, outer below —
because two on one line would read as one barrier.

Four things in that are load-bearing rather than cosmetic:

- **A double barrier is two valves in series, and a graph will not model that
  by itself.** Two valves both joining the annulus to the choke line are in
  *parallel*: either one opening gives a route, which is the opposite of what
  a double barrier is for. Each outlet therefore has its own spool node
  between its two valves — and each of a subsea stack's four outlets needs its
  *own* spool, or the upper inner valve and the lower outer valve add up to a
  path that does not exist on the stack. `_RETURN_ENTRIES` lists the spools
  rather than the lines for the same reason: looking for the line is a way to
  have a well that is closed in whatever the student does with the HCRs.
- **`Valve.seals` is one flag and it is the entire difference between a blind
  shear and a casing shear.** A casing shear ram cuts and does not seal —
  closing one is not shutting the well in, however final it feels, and a
  student who reached for one expecting a barrier would have a sheared string
  in a live well. Neither stack currently carries one (both subsea shear rams
  are blind), but the flag and its graph behaviour stay, carried today by the
  riser connector.
- **A shear ram closed on pipe severs it** (`FlowPath.pipe_sheared`). Every
  edge through the string and the bit goes, so the pump deadheads and nothing
  can be circulated out again — re-opening the ram does not mend it. The well
  may well be sealed; the exercise is over as a kill, which is the honest
  consequence of reaching for them. *Approximation*: the string's hydrostatic
  is left as it was rather than modelling the pipe dropping downhole.
- **Naming a side, not a valve.** "Open the choke line" is one instruction on
  the floor and two valves on either stack, so `FlowPath.open("choke_line")`
  resolves the group and everything that *operates* one valve still names it
  exactly. Setup code and scenario files stay stack-agnostic; `set_valve`
  still raises on a group, so the panel cannot accidentally work four valves
  with one press. Subsea it opens the **lower** outlet — the lowest line on
  its side, so it survives everything closed above it. For the choke side
  that is the whole stack; the lower *kill* line sits one element up and can
  be taken by the lower blind shears beneath its tap.

**The closing unit is most of what a BOP panel is**, and none of it existed.
`Accumulator` gained the two regulators and their readbacks, the bypass, the
master valve, supply air and a gallons flow counter. Each earns its place by
being a way for the stack not to move while the accumulator gauge still reads
3,000 psi:

- **Set point and readback are different numbers.** The readback is capped by
  what is in the bottles, so watching it fall away from the set point is how a
  student sees the unit losing the stack before the accumulator gauge looks
  alarming. Drawing one gauge for both would delete the lesson.
- **A regulator wound below `minimum_operating_psi` stops its own elements**
  and leaves the others alone — the rams *and the HCRs* are on the manifold
  regulator and the annular on its own. **The master valve deadens
  everything.** **Air below the minimum stops the charging pumps**, so the
  bottles never come back.
- **The flow counter is the only instrument on the panel that says an element
  moved.** A lamp says a switch was thrown. Zero it, operate, and the gallons
  are what that function took.
- **The pod selector is a real control**, with a `pod_failure` fault behind
  it: the pod in use stops answering, nothing on the stack shifts, and the
  answer is to change over — the same shape as the pump and choke faults.
  Blank on a surface stack, which has no pods.

**One command carries the whole unit** (`/closingunit`), with every field
optional and moving on its own. Same reason the DCU's alarm set points are
independent: a face that redraws four times a second must not drag one setting
back while the student is working another.

#### Two-handed operation

**Nothing on a BOP panel is a single press**, and the engine enforces it:
`Accumulator.arm()` buys **one** function, and `operate()` returns False
without it. Both stacks are the same interlock wearing two hats — the master
valve handle on the surface remote panel, *Push to Operate* on the subsea
screen — because underneath they are the same closing unit. `arm` is a field on
`/closingunit`, so it moves on its own like every other setting there.

**It covers the whole unit, not just the rams.** `Valve.on_the_closing_unit`
is `is_bop or side`, which is the stack elements **and the outlet HCRs** —
*hydraulically controlled remote* is the whole of that name, and an HCR is
shifted by the same bottles through the same master valve as a preventer. So
lining the choke side up is a closing unit function and not a walk to the
manifold, and `Simulator.operate_stack_valve` (was `operate_bop_element`)
handles both. The consequence is the point: **a student who cannot shut an
annular cannot open a choke line either.** The gate valve on the choke manifold
is untouched — somebody walks over and turns it.

An HCR is a gate valve with an actuator on it rather than a preventer, so it
costs `hcr_cost_psi` / `hcr_gal` — a fraction of a ram, but not nothing, and a
student who lines a side up twice has spent it. It runs off the **manifold**
regulator with the rams, which is why winding that off takes the choke line
away as well as the pipe rams while the annular, on its own regulator, still
works.

**The regulators and the bypass need the arm too, and do not spend it**
(`Accumulator.adjust`). They decide what pressure every function gets, so
reaching for them is reaching into the closing unit — but a setting is not a
function. Winding a regulator from 1,500 psi to 500 is ten presses of Dec.;
charged an arm apiece it would be twenty, and a control nobody uses teaches
nothing. The master is held over while the adjustment is made and is still over
for the function afterwards.

Four more things that are load-bearing:

- **The interlock is in the engine, not the page.** A face that merely refused
  to send the second press would be defeated by a POST, invisible to the
  instructor's drill-down and untestable. `Session.set_valve` reaches
  `Accumulator.operate` for every route there is.
- **The arm is spent by the *attempt*, not by the success.** One arm is one
  press, not one result. A lamp left lit after a function that did nothing
  would announce that something is wrong with the unit — and a dead pod, empty
  bottles or a regulator wound off are all supposed to read as the element
  simply not shifting, which is the whole shape of the rest of this model.
- **Pressing the button for the position an element is already in costs
  nothing**, because `operate_stack_valve` returns early before it reaches the
  accumulator. Arming and then pressing Open on something already open is not
  a spent arm.
- **`pod` and `master_open` are outside the interlock.** The pod selector
  chooses which control system answers rather than doing anything with
  pressure, and gating it would mean a student on a dead pod has to arm
  *through the pod that is not answering* to change to the one that is.
  `master_open` **is** the interlock, so gating it on itself would leave a shut
  master with no way back.

The arm indicator is a **lamp with a fixed legend** (`armLamp`), directly under
the master valve on the surface face and beside Push to Operate subsea: the
word `ARMED` stays put and the light behind it comes on, which is how every
other indicator on a rig works. It replaced a banner whose *text* changed
between "ARMED — ONE FUNCTION" and "NOT ARMED" — a legend that rewrites itself
has to be read, and the handle is what you work so the lamp belongs beside it.

`set_closing_unit` processes `arm` **first**, so one command carrying `arm` and
a setting works. The face sends them separately, but a command that quietly
ignored half of itself depending on field order is a trap for anything else
driving this.

Every test that functions the stack now arms first — `shift()` in
`tests/test_bop_stack.py` — and the tests that call `operate_stack_valve`
*without* arming are the ones about the interlock itself. Watch for tests that
pass **vacuously** after a change like this: `test_circulating_with_the_bop_
open_does_nothing` lined up the choke side and then asserted something that was
true anyway, so it went on passing with both HCRs shut. It now asserts each one
opened.

The master valve is no longer a toggle on the surface face; the handle is the
arm. `master_open` survives as a state a fault or the instructor can shut, and
the lever says `MASTER SHUT` when it is, so the student can see why a well-
armed function did nothing. Nothing currently shuts it.

**The two faces share almost nothing, so both are driven in the harness.**
`tests/test_panel_render.py` runs each with a real lineup for its own stack —
a surface payload would never reach the subsea face, and "it did not throw" on
one proves nothing about the other.

**Both faces were then rebuilt against their photographs**, because the first
pass had the right pieces in roughly the wrong places. Every coordinate on the
surface face is now a pixel measurement off `surface_bop.JPG` — whose panel
occupies 1152x647 px of it — scaled by 1600/1152 across and 680/647 down. The
two scales differ because the reference window is a different shape from this
face; **positions can take a non-uniform scale and radii cannot**, so gauge
radii are set by the height, which is the tighter of the two. What that moved:

- **Master valve to the panel edge**, low and left, between the two gauges,
  drawn as the chrome body and ball handle the photograph shows rather than as
  a labelled button. It leans over when armed — the handle is the report.
- **Gauges are 0–5,000 psi faces**, as the reference has them, not 0 to
  whatever the bottles hold. A full accumulator then sits at three fifths of
  the sweep instead of pegged at full scale, and there is somewhere for the
  needle to *go*. A dial that reads full when everything is normal cannot show
  anything but bad news.
- **The stack got flanges.** Studded bands top and bottom of every body, stubs
  and flanges on the outlets, a wellhead under the lowest rams. Four
  rectangles on a pipe read as a diagram of a stack; the flanges are what make
  it a picture of one, which is the whole difference the photograph makes.
- **Flow Reset and the counter to the bottom**, under the controls they report
  on, where the reference puts them.
- **The side names moved over their pairs.** The reference sets "Kill Line"
  *beside* its buttons because it has one valve a side; this stack has two, and
  a name level with one of two rows reads as belonging to that row.

The subsea face was re-laid to the same photograph's **columns** — readback
gauges, pod and regulators, kill side, bore, choke side, closing-unit gauges —
with each outlet on a **double-height row**, outer fail-safe over inner, as
`ss_bop.JPG` stacks them. Two on one line would read as one barrier. The bore
trunk is drawn in oxide red because the piping on that screen is, which is also
why the element icons on it are **steel and not the tan of the position
windows**: anything warm-coloured sitting on the trunk reads as part of the
piping.

A hundred units of that face are held back at the bottom for *Push to Operate*
and the arm lamp. The reference has a bottom band carrying it; this face is
taller in proportion and had to find one, and the alternative — tucking the
one control without which nothing works into a corner beside the gauges — put
it where nobody would look.

### Drawing the stack, and the controls on it

Both were rebuilt again after *"the buttons all long oblong and very
cartoonish"* and *"make the shapes of the actual bop look more realistic
instead of just blocks and text"*. What that came down to:

**A pushbutton is a bezel with a lens recessed into it.** `bopButton` is six
flat layers — shadow, bezel, bezel highlight, lens, lens gloss, lettering —
and no `<defs>`: the face redraws four times a second and replaces its
innerHTML whole, so gradient definitions would be re-parsed every frame for
every button. Flat fills at low opacity give the same rounded look for a
fraction of the work. Buttons are also **squarer**; the old 78x46 oblongs were
half the problem.

**Unlit is dark, not pale**, and that is most of the rest of it. The reference
panel's off buttons are black — a lamp behind a pushbutton is off, and an off
lamp is black. The pale grey that was there made every button on the face look
permanently illuminated, which is exactly what reads as a cartoon.

**A preventer is mostly its side operators.** A ram preventer carries a bonnet
each side with the closing cylinder bolted to it, and that silhouette is how
one is recognised across a rig floor; an annular has none, because its packer
is squeezed from underneath rather than driven in from the sides. Drawing them
as a square box and a round box got the distinction across. Drawing what they
look like gets it across without anybody having been told. The stack is
therefore drawn **wide** (colW 250), the ram body only 0.46 of that, and the
buttons moved outboard to make room — squeezing the operators out is what made
the earlier version a column of boxes.

**Draw the bonnet cylinder first, then the bolting flange over its inner
end.** A fully rounded bar drawn outboard of the flange curves *away* from the
body and reads as a blob floating beside it; tucked under the flange the same
shape reads as a cylinder bolted on. This was the difference between "you
can't hardly even see the bonnets" and a stack you can recognise.

**Each outlet is one line carrying two valves** (`lineValve`), not two rows of
buttons. Stacked in two rows the inner and outer pair read as two separate
lines, which is the one thing a double barrier is not — so the surface face
draws four lines with eight valves on them, gate-valve bowties with their
hydraulic operators on top, green passing and red shut. **Open goes above its
valve and Close below it**, which is what makes it obvious which button works
which valve without a single label. A student can trace a route across the
face without reading anything.

`REACH` stops short of the regulator column: an outlet line that runs on under
the By Pass buttons says the line goes somewhere it does not.

**Pipe is not a line** (`pipeRun`, `pipeFlange`, `pipeTee` — subsea face). A
run of pipe is a cylinder, so it is drawn in three passes over one path: a
dark casing, the body, and a highlight shifted up by a quarter of the bore,
with round joins and caps so elbows turn instead of mitring to a point. A
flange at every joint and a collar at every tee does the rest. Three passes
rather than a gradient for the usual reason — this face redraws four times a
second and replaces its innerHTML whole, so a `<defs>` would be re-parsed
every frame.

**The two headers are what make the subsea drawing say what the stack is.**
Every branch runs out to a vertical kill or choke header with a tee at each
junction, so "four lines to the choke side and three to the kill" is visible
rather than something you count. They are named at the **top**: the choke
header's lowest tie-in is the last row on the face and a caption under it
falls off the edge.

Fifteen rows in 680 units is what sets everything else on that face — compact
control blocks, small labels, and **both fail-safes on a line side by side on
it**. Stacked in two rows as the surface face does it, seven lines would need
twenty-two rows and there is no version of that which fits.

Also in `stackElement` and `flowCrossBody`: studded flange pairs **straddling
the body edge**, so adjacent bodies land their flanges within a pixel or two
of each other and read as bolted together; a through bore dark strip, so
everything reads as being on one bore; two flat overlays for cylindrical
shading; a riser above the stack and a casing head under it.

- **`flowCrossBody` takes body width and stub reach separately**, because they
  are separate on a rig: how fat the spool is has nothing to do with how far
  its lines run before the first valve. Coupled, the choke stub ran on past
  its own buttons — the spool sits at the centre of the face and the two
  button columns did not. `REACH` is now the source of truth and both blocks
  are placed *from* it.
- **The ram position is drawn in the bore, above the name plate.** Shut, the
  two halves meet across it; open, they are drawn back into the body. A plate
  across the middle of the body would cover the one thing on the drawing that
  says which way the element is.
- **An annular's packer sits lower than a ram's**, because the body above it
  is drawing in towards the head and a wedge across that taper reads as damage
  rather than as a seal.

Layout notes worth keeping if either face is touched again:

- **The name plate sits on the element and must be narrower than it**, or the
  rams are invisible and the two buttons are the only report. It is riveted —
  a bevel and four corner studs — because a nameplate is a piece of steel
  screwed to the body, and without them it is a white label pasted over the
  drawing. Plate lettering is sized to the plate rather than the other way
  about, because the labels come from the engine and "Lower blind shear rams"
  is three times the length of "Annular" — and a warning suffix, when an
  element earns one, makes it longer still.
- **On the subsea face the "does not seal" warning is *in* the plate**, not
  beside it. There is no free line under a row on a schematic that tight, and
  the note drawn under what was then the casing shear rams landed inside the
  next row's controls and was invisible. Red lettering on its own only says
  "different". (No element earns the warning on the current stack; the logic
  stays, keyed off the flags.)
- **Surface is an elevation and subsea is a schematic**, which is what the two
  references are. A surface stack is one cabinet you stand at; a subsea one is
  a control system you read, and which outlet is still usable depends on what
  is closed above it — only obvious from a drawing.
- **An annular is drawn round and a ram square**, so the two are told apart
  without reading the plate. They behave differently: an annular closes on
  anything, a pipe ram seals only on the size it was dressed for.

**The subsea layout departs from its photograph in pitch, and this is the one
place on these screens where that is true.** Built to `ss_bop.JPG` verbatim,
the outlet fail-safe buttons landed on top of the gauge columns either side —
the reference screen is a wider, shorter window than this face and its own
controls sit closer together than ours can. What is kept is every idiom and the
column *order*: the pod selector, the position windows between the buttons, the
name plate *underneath* rather than on the element, the oxide-red piping, the
outer fail-safe over the inner, and the gauge columns down both sides. What
changed is the spacing. **Surface is verbatim; subsea is re-spaced, and it is
the one to check against the photo** if either face is revisited.

One thing about the subsea face that looks like spare room and is not: the
band between an outlet's controls and the bore is empty except on outlet rows,
because that is where the outlets actually tap the stack — filling it would say
there are taps that do not exist. And **the name goes under the control block
rather than on the element**, unlike the surface face: a long label with a
warning suffix — "Casing shear rams — does not seal", when the stack carried
one — would not fit between the buttons at any size worth reading.

### Controls, and the trap under them

**Every control drawn on a face must work.** A student learns the panel and
then finds half of it is a picture, which is worse than it not being there. A
test scrapes every `data-face` value out of the page and asserts each appears
in `faceAction`'s switch. RESET was the last one that did not: it was a
comment saying *stroke counter: display only*. It now calls
`zero("strokes")`, which moves a **display offset** on the session and leaves
the engine's stroke count running, because the faults and the kill sheet are
hung off that count. The trace keeps the true figure too, so a strokes-axis
trend is still a record while the window is an instrument.

**Handlers go on the SVG host, attached once — never on its children.** The
face redraws four times a second and each redraw replaces the host's entire
`innerHTML`, so anything bound to a child is gone by the next poll: a drag
dies mid-gesture, and a listener is thrown away with the node it was on. That
is exactly why the joystick and the choke selector did nothing for a while.
The host survives because only its contents change, and the current gauges
hang off it (`host._gauges`) rather than being captured in a closure that
would go stale with them.

**And they fire on `pointerdown`, not `click`.** Same redraw, subtler
symptom: a `click` is only delivered when the button comes back *up*, so press
a stepper, have a redraw land before you let go, and the node you pressed no
longer exists. The browser resolves the click against the SVG itself, the
handler finds no `data-face`, and the press is silently lost. It looks exactly
like a mis-aimed click, which is why it survived so long. A pointerdown's
target is read at the instant of the press, before any redraw can move it.

**Small controls get a transparent hit disc larger than the thing drawn.**
RESET is 26 css pixels across at the size the approved layout wants it, and a
press landing two pixels off does nothing — which reads as a dead button
rather than a missed one. Note `fill="transparent"`, not `fill="none"`: a fill
of none is not hit-tested at all.

### The agreed choke panel, and who controls what

`simulator_screens/choke_panel_exact.jpg` is the **approved layout** for the
choke panel — build to it verbatim. **The face is now built to it**; see the
Choke Panel entry under *Done so far* above for the geometry and what it cost.
Casing gauge **left**, drill pipe **right** (deliberate, and confirmed:
transposed from the reference box, which does not matter for this audience —
a test pins it, because it is the kind of thing a later reader "corrects"
back). Both gauges dominate the face. The centre is a single vertical control
stack — choke position, joystick, Choke2/Choke1 selector, choke control — so
everything about the choke reads top to bottom in one line. Mud pumps bottom
left with RESET above the counter it resets. The lamp row stays below the
panel: *Above MAASP* and *Well dead* are worth peripheral vision even though
the mockup omits them.

**There is no solo mode, and there never will be.** Every run is set up by an
instructor — a class of one is still a class. That settles what the header is:
the exercise and speed dropdowns are gone from the student's page, the
multiplier is an indicator (`#speed-pill`) rather than a control, and Restart
is hidden in a class because a student who could reset their own well could
take a kick twice and hand in whichever went better.

The transport is theirs, though, and `/api/me/pause` is the route for it —
**a transport, not a clock**. Pausing stops that one student's well so they
can read a kill sheet or think; resuming puts them back on whatever the
instructor's multiplier is *now*, not on a remembered figure of their own.
There is still no `/api/me/speed`, and there should not be.

`<select id="scenario">` survives, hidden. It is not a student control: it is
how a server with no class on it starts something to look at, which is the
harness this page is developed and tested against. The mockup's third
transport glyph — stop — is **not** built, because ending a run is a concept
the session does not have (there is no finished state, and the debrief tab is
the instructor's in a class). Drawing it would be decoration, which this panel
does not do.

`simulator_screens/current_screens/` holds screenshots of every screen as it
stood on 27 July, for comparison.

**The split of control between student and instructor is settled:**

| | Student | Instructor |
| --- | --- | --- |
| Which exercise | no | yes |
| Play / pause / stop their own run | yes | — |
| See what speed they are running at | yes, read-only | — |
| Change the speed multiplier | **no** | yes |
| Auto choke | no | yes — can put a student on it |

**A student never picks the speed.** They get a transport — play and pause —
and an indicator showing the multiplier they are on. Speeding a student up is
the instructor's call, because the whole point of 10x is to skip the long
circulating stretches on the instructor's schedule, not the student's.

**The joystick is a proportional control, not a swipe.** It used to read the
distance dragged and send one command on release, so nothing moved until you
let go and the size of the movement was a guess — which is why it read as
broken. It is now held: deflection sets a *rate*, the handle leans while it is
held and springs back to centre when let go, and full deflection commands
travel at the choke's own `Travel speed` so the command never runs ahead of
the actuator. Three things in `startStick` are load-bearing:

- **The step is integrated against measured elapsed time**, not the timer's
  nominal interval. Each post repaints the whole face, and Chrome throttles a
  hidden tab's timers to about 1 Hz — either way the callbacks come late, and
  a handle held hard over moved the choke at a quarter of the speed it
  advertises. The `dt` clamp that guards a suspended tab has to be generous
  for the same reason: set near the tick, it silently halves the travel.
- **The target is held locally** rather than read back off the gauges each
  tick, or every step waits for a round trip and the handle feels like it is
  negotiating with the server rather than driving a choke.
- **The lean is redrawn on `requestAnimationFrame`, not on the poll.** A
  quarter second of lag on a control you are holding is the difference between
  a joystick and a form field.
- **The lean is negated, and it has to be.** `deflection` is positive to the
  left because left is open; SVG's `rotate` is positive *clockwise*, which
  swings everything above the pivot to the right. Unnegated, a left push
  opened the choke correctly while the handle leaned right — which is a worse
  fault than either direction being wrong on its own, because the two halves
  of one control contradicted each other. A test pins the sign.

**Each mud pump has its own SHUTDOWN**, to the right of its steppers, so a
pump row reads `−5 [spm] +5 SHUTDOWN`. Stepping down to zero is five or six
presses, and the moment a driller wants a pump off is exactly the moment they
have something else to watch.

**Auto choke is new engine work.** The instructor can put a student's well on
an automatic choke that holds bottomhole pressure — the same PD controller the
autopilot in `tests/test_simulator.py` uses to validate the forward model
against the inverse one, promoted out of the tests and into the engine. It
needs its derivative term for the reason given there: proportional control
alone limit-cycles at about 65 psi on a 25-second period, which is a mediocre
driller rather than an automatic one.

### The 422 was a stale server, and so is the next one

The choke selector and the travel-speed steppers used to come back **422
Unprocessable Content** while the same bodies posted directly returned 200.
There was never anything wrong with the page: the dev server had been started
without `--reload`, so it was serving routes and models older than the source.
Restarted properly, both controls return 200 and both work from the face.

**That trap has now been paid for three times, and the third time it wore a
different hat.** `--reload` is necessary and not sufficient:

- **No `--reload` at all**: newly added routes 404, and changed models 422,
  while the source plainly has them.
- **`--reload` on, and the reload wedges anyway. This is reproducible on this
  box, not a one-off.** The log says `StatReload detected changes …
  Reloading...` and then never says `Started server process` again: the new
  worker cannot take the port because the old one still holds it, so the old
  code keeps answering. Seen twice in one session, once with the reloader
  parent dead and once with three python processes alive at the same time. A
  `zero` call answered with an error message that had been edited out of the
  source ten minutes earlier. **Do not trust `--reload` for Python changes
  here** — after editing anything under `src/`, kill every process on port
  8020 and start it again. If the server is answering with words that are no
  longer in the file, that is what has happened.
- **The browser caches the page.** `navigate` to the same URL re-serves from
  cache; only a hard reload picks up an edited `choke_panel.html`. A hit target
  measured at its old size is the giveaway.

### The instructor station is the admin console now

The flat scrolling page of tiles was rebuilt as the **reference admin
console**: a fixed-height application window — dark title bar reading
*Administrator Station - Well Control Simulator*, the top transport strip
(Pause All / Resume All / Stop All), *Simulation Speed* dropdown and the green
*Simulation Time* clock — with a dark left navigation tree and a content pane
that swaps views without the page ever scrolling. The chrome is built to
`simulator_screens/IMG_1195-97.PNG`; the tree to
`sim_screen_expectations/admin_panel_left_side.JPG`, using the reference's own
entry names wherever a capability exists to answer them: *Load Exercise*,
*Administrator Control Panel* (code, close, end), *Monitor Exercise
Parameters* (the tile grid, with the drill-down card), *Operator Panels*,
*Fault Induction Panel*, *Kill Sheets*, *Drilling Plots Report*, *Debrief
Report*, *Load Recording*, and *Manage Trainees*. Entries the reference has
and we have nothing behind — Remote Launch Panel, View Configuration, Mud
Circulation Panel, and the chrome's *Show Auto Choke* — are **absent rather
than inert**, the stop-glyph rule again: they go in when the capability does.
A structural test asserts every leaf opens a view that exists and vice versa.

**Operator Panels embeds the student's real page.** The groundwork was
already in — `/instructor/panel?classroom=&student=` serves the same page the
student is looking at, polling the instructor's drill-down endpoint instead of
`/api/me`, read-only with the controls visibly dimmed (an instructor nudging a
choke while a student flies it would be indistinguishable to them from the
well doing it). The console puts that page in an iframe under the reference
chrome's screen-tab strip (Choke Panel, DCU, BOP, SP Manifold, Choke
Manifold, Trip Tank, plus Trends and Well, with **Quad View** at the right
showing choke/DCU/BOP/trip tank four-up). Two query parameters were added to
`choke_panel.html` for it: `screen=` picks the tab on load — through the same
click path a press takes, not a second mechanism — and `embed=1` hides the
page's own header and tab strip so there are not two of them. Leaving the
view **tears the iframes down**, or a hidden student screen would still be
polling the drill-down four times a second behind another view.

Three things in the console worth knowing:

- **No new API routes.** Every view drives an endpoint that already existed —
  the monitor, the drill-down (whose trace feeds the Drilling Plots Report),
  the per-student kill sheet and debrief, recordings, faults — so nothing new
  needed `@exclusive` and the structural lock test is untouched.
- **The Simulation Time clock is the furthest-run well in the class.** The
  classroom itself has no simulated time, only the wells do; they ride one
  clock and only drift apart when one is paused or slowed.
- **Stop All is End Class wearing the chrome's name**, sharing the same
  two-click confirmation — it captures every run as a recording before the
  wells go, which is what the server already did on DELETE.

### The lesson worth keeping

**Do not build screens blind.** Roughly 7,800 lines went in before anyone
looked at a pixel, because the browser extension would not connect and the work
carried on anyway. The render harness in `tests/test_panel_render.py` is
genuinely useful — it proves every draw function executes against a real
payload — but "does not throw" is a far weaker claim than "reads correctly",
and no test was ever going to catch *this is the wrong shape entirely*. Get
eyes on a screen before building the next one.

