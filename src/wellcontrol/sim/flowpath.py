"""The circulating system as a graph of equipment joined by valves.

Lining the rig up is half of well control practice, and it is the half a
calculation cannot teach. Here the system is a directed graph: equipment are
nodes, valves are edges, and every timestep the engine asks two questions.

* Is there a path from the pump into the well? If not, the pump deadheads.
* Is there a path from the well out to the pits? If not, the system is closed
  and anything pumped in has nowhere to go.

**Wrong lineups are not scored as mistakes; they simply do not work.** A student
who leaves the choke line closed gets no returns and has to work out why, which
is exactly what happens on a rig. Nothing here tells them what they did wrong.

## The rule that matters most

With the BOP **open**, returns take the flowline and there is no pressure
control -- the well is open to atmosphere and the choke is irrelevant. With the
BOP **closed**, returns can only leave through the choke or kill line, and the
choke controls the well.

Students reliably try to circulate a kick with the annular still open. The model
lets them, and nothing happens to the casing pressure, which is the lesson.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class Node(str, Enum):
    """A piece of equipment fluid can be in or pass through."""

    PUMP = "mud pump"
    STANDPIPE = "standpipe"
    STRING = "drill string"
    BIT = "bit"
    ANNULUS = "annulus"
    BOP = "BOP stack"
    FLOWLINE = "flowline"
    CHOKE_LINE = "choke line"
    KILL_LINE = "kill line"
    CHOKE_MANIFOLD = "choke manifold"
    SEPARATOR = "mud gas separator"
    PITS = "active pits"
    TRIP_TANK = "trip tank"

    # The standpipe manifold's blanked branches. The cement head and cement
    # pump are equipment this simulator does not model, so the lines go
    # nowhere -- but the manifold face draws the manifold a rig has, and a
    # drawn valve must be a real valve. Opening one does exactly what it
    # would on this rig: nothing.
    CEMENT_HEAD = "cement head"
    CEMENT_PUMP = "cement pump"

    # The spool between an outlet's two isolation valves. It exists so the
    # pair reads as **in series** rather than in parallel: two valves both
    # joining the annulus straight to the choke line would mean either one
    # opening gave a route, which is the opposite of what a double barrier
    # is for. A subsea stack has two outlets a side, each with its own pair,
    # so each needs its own spool -- sharing one would let the upper inner
    # valve and the lower outer valve add up to a route that does not exist.
    CHOKE_OUTLET = "choke outlet"
    CHOKE_OUTLET_MIDDLE = "middle choke outlet"
    CHOKE_OUTLET_LOWER = "lower choke outlet"
    KILL_OUTLET = "kill outlet"
    KILL_OUTLET_MIDDLE = "middle kill outlet"
    KILL_OUTLET_LOWER = "lower kill outlet"
    #: The gas relief line taps above the lower annular and is **piped into
    #: the choke line**, which is why its valves carry ``side="choke"``: it is
    #: a fourth way to the choke manifold, not a third line of its own.
    GAS_RELIEF_OUTLET = "gas relief outlet"

    # The stack bore between two adjacent elements, numbered from the bottom.
    # These are what make the outlet heights *real*: an outlet taps the
    # segment at its own height, an element is an edge between the segments
    # either side of it, and closing a ram therefore isolates every outlet
    # above it from the well -- which is the decision "take returns from a
    # cross below the ram you closed" turns on. ANNULUS is the bore below the
    # lowest element and BOP the bore above the highest, so a surface stack
    # uses segments 1-3 and a subsea one 1-7.
    STACK_BORE_1 = "stack bore 1"
    STACK_BORE_2 = "stack bore 2"
    STACK_BORE_3 = "stack bore 3"
    STACK_BORE_4 = "stack bore 4"
    STACK_BORE_5 = "stack bore 5"
    STACK_BORE_6 = "stack bore 6"
    STACK_BORE_7 = "stack bore 7"


class ValveState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Valve:
    """A valve, or a BOP element, joining two nodes.

    Attributes:
        name: Key used to operate it.
        label: What it is called on the rig floor.
        frm: Upstream node.
        to: Downstream node.
        state: Open or closed.
        is_bop: True for BOP elements, which seal the annulus when *closed* --
            the opposite sense to every other valve here.
        throttling: True for an adjustable choke, whose opening is a position
            rather than a state.
        bidirectional: True where the line genuinely runs both ways. A gate
            valve does not care which way the flow goes; the kill line takes
            returns during a kill and takes mud *into* the annulus when
            lubricating, and it is the same valve doing both.
        bop_kind: ``annular`` or ``ram`` for a stack element, blank otherwise.
            The closing unit charges by element type -- an annular has much
            the largest closing volume -- and the panel draws the two shapes
            differently. Reading it off the name worked while there were three
            elements all called what they are; it does not survive a stack with
            variable bore rams and two kinds of shear ram on it.
        seals: Whether closing this element shuts the annulus in.
            A casing shear ram would not -- it cuts the string and leaves a
            hole for the well to come through, so a student who reached for
            one expecting a barrier would have a sheared string in a live
            well. Neither stack currently carries one (the lower subsea
            shears became blind shears on Kirk's call), but the flag and
            the graph behaviour stay: a closed non-sealing element still
            passes flow. The riser connector carries it today, because
            unlatching is walking away from the hole, not securing it.
        shears: Whether closing it cuts whatever is through it. Closed on pipe,
            a shear ram severs the string: the well may be sealed but nothing
            can be circulated afterwards.
        side: ``choke`` or ``kill`` for an outlet isolation valve, blank
            otherwise. With it the panel can draw the two sides of the stack
            without knowing any valve names.
        outlet: ``upper`` or ``lower`` for a subsea stack's two outlets a
            side; blank on a surface stack, which has one flow cross.
    """

    name: str
    label: str
    frm: Node
    to: Node
    state: ValveState = ValveState.CLOSED
    is_bop: bool = False
    throttling: bool = False
    bidirectional: bool = False
    bop_kind: str = ""
    seals: bool = True
    shears: bool = False
    side: str = ""
    outlet: str = ""

    @property
    def is_open(self) -> bool:
        return self.state is ValveState.OPEN

    @property
    def on_the_closing_unit(self) -> bool:
        """Whether this is worked from the BOP panel rather than by hand.

        The stack elements and the outlet valves both are: an HCR is a
        *hydraulically controlled remote* valve, which is the whole of its
        name, and it is shifted by the same bottles and through the same
        master valve as the rams. So it costs closing unit pressure, it needs
        the panel armed, and it will not move with the unit dead -- none of
        which is true of the gate valve on the choke manifold, which somebody
        walks over and turns.
        """
        return self.is_bop or bool(self.side)


def surface_stack() -> list[Valve]:
    """A surface stack: one annular, two pipe rams, a blind shear ram.

    **Listed top to bottom, and that order is load-bearing** -- both faces draw
    the stack by walking this list, so the interleaving of elements and flow
    crosses here is the drawing:

        annular
        upper pipe rams
        lower pipe rams
        upper flow cross      -- kill and choke outlets, two HCRs each
        blind shear rams
        lower flow cross      -- kill and choke outlets, two HCRs each

    **Two flow crosses, because one outlet is one point of failure.** The upper
    cross is below both pipe rams, so it is the one to take returns from with
    either of them shut; the lower is below the blind shears as well, so it
    survives even a stack closed on nothing. That is why "the choke line"
    resolves to the *lower* outlet -- see ``_group``.

    **The heights are the model now, not just the drawing.** The stack bore
    is split into segments -- ``ANNULUS`` below the lowest element, a
    ``STACK_BORE_n`` between each adjacent pair, ``BOP`` above the top -- and
    each element is an edge between the segments either side of it. An outlet
    taps the segment at its own height, so closing a ram isolates every
    outlet above it from the well, exactly as the iron would. Two
    approximations remain, both deliberate: the few barrels trapped between
    two closed elements are not tracked as their own pressure vessel, and the
    few feet of height between outlet taps are hydrostatically invisible next
    to the thousands of feet of annulus below them.

    Every outlet carries **two hydraulically operated valves in series**, an
    inner and an outer HCR. Both have to be open for that side to pass
    anything, which is the whole point of a double barrier and is why they are
    two valves here rather than one called "the choke line". Each outlet needs
    its own spool node between its pair, or an upper inner and a lower outer
    would add up to a route that does not exist on the stack.

    Names match the subsea stack's, because the outlets are the same idea on
    both and nothing driving them should have to care which rig it is. What
    differs is the label: a surface outlet valve is an **HCR**, and a subsea
    one is a **fail-safe** -- which is the real distinction, since a fail-safe
    shuts on loss of control pressure and an HCR simply stays where it was.
    """
    return [
        # Elements are edges between adjacent bore segments, frm below and to
        # above, numbered from the bottom: the well is ANNULUS, the bell
        # nipple is BOP, and each ram separates the two spaces it sits
        # between. The upper flow cross taps segment 1 (above the blind
        # shears), the lower one taps the well itself.
        Valve("annular", "Annular preventer", Node.STACK_BORE_3, Node.BOP,
              state=ValveState.OPEN, is_bop=True, bop_kind="annular"),
        Valve("upper_rams", "Upper pipe rams", Node.STACK_BORE_2,
              Node.STACK_BORE_3,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram"),
        Valve("lower_rams", "Lower pipe rams", Node.STACK_BORE_1,
              Node.STACK_BORE_2,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram"),

        Valve("upper_kill_inner", "Upper kill inner HCR", Node.STACK_BORE_1,
              Node.KILL_OUTLET, bidirectional=True, side="kill",
              outlet="upper"),
        Valve("upper_kill_outer", "Upper kill outer HCR", Node.KILL_OUTLET,
              Node.KILL_LINE, bidirectional=True, side="kill", outlet="upper"),
        Valve("upper_choke_inner", "Upper choke inner HCR", Node.STACK_BORE_1,
              Node.CHOKE_OUTLET, bidirectional=True, side="choke",
              outlet="upper"),
        Valve("upper_choke_outer", "Upper choke outer HCR", Node.CHOKE_OUTLET,
              Node.CHOKE_LINE, bidirectional=True, side="choke",
              outlet="upper"),

        Valve("blind_shear_rams", "Blind shear rams", Node.ANNULUS,
              Node.STACK_BORE_1,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram", shears=True),

        Valve("lower_kill_inner", "Lower kill inner HCR", Node.ANNULUS,
              Node.KILL_OUTLET_LOWER, bidirectional=True, side="kill",
              outlet="lower"),
        Valve("lower_kill_outer", "Lower kill outer HCR",
              Node.KILL_OUTLET_LOWER, Node.KILL_LINE, bidirectional=True,
              side="kill", outlet="lower"),
        Valve("lower_choke_inner", "Lower choke inner HCR", Node.ANNULUS,
              Node.CHOKE_OUTLET_LOWER, bidirectional=True, side="choke",
              outlet="lower"),
        Valve("lower_choke_outer", "Lower choke outer HCR",
              Node.CHOKE_OUTLET_LOWER, Node.CHOKE_LINE, bidirectional=True,
              side="choke", outlet="lower"),
    ]


def _failsafes(key: str, name: str, side: str, outlet: str, bore: Node,
               spool: Node, line: Node) -> list[Valve]:
    """One outlet line: two fail-safe valves in series through their spool.

    Written once because there are **seven of these on a subsea stack** and a
    stack with fourteen valves hand-listed is a stack with a transposed node
    in it somewhere. The spool between the pair is what makes them a double
    barrier rather than two parallel routes; each line needs its own, or an
    inner from one line and an outer from another add up to a path that does
    not exist.

    ``bore`` is the stack bore segment the line taps -- the space between the
    two elements it sits between, which is what makes its height real: the
    line reaches the well only while every element below that segment is open.
    """
    return [
        Valve(f"{key}_inner", f"Inner {name}", bore, spool,
              bidirectional=True, side=side, outlet=outlet),
        Valve(f"{key}_outer", f"Outer {name}", spool, line,
              bidirectional=True, side=side, outlet=outlet),
    ]


def subsea_stack() -> list[Valve]:
    """A subsea stack: two annulars, a riser connector, two shear rams, three
    pipe rams, and **seven outlet lines**.

    Listed top to bottom, which is the order both faces draw it in:

        upper annular
        riser connector
        gas relief line       -- piped into the choke line
        lower annular
        upper kill
        blind shear rams
        upper choke
        upper pipe rams
        middle kill
        middle pipe rams
        middle choke
        lower pipe rams
        lower kill
        lower blind shear rams
        lower choke

    **Four lines to the choke side and three to the kill side.** The gas relief
    line is the fourth choke-side line: it taps above the lower annular and is
    piped into the choke line rather than run to the surface on its own, so its
    valves carry ``side="choke"`` and the group that opens "the choke line"
    finds them. Interleaving the outlets between the rams is the whole point of
    a stack this size -- whichever ram you close, there is a line below it.

    Every outlet carries **two fail-safes in series** (see ``_failsafes``): a
    fail-safe shuts on loss of control pressure, which is why they are drawn
    shut and opened deliberately, and is the real difference between one of
    these and a surface HCR.

    **Both shear rams are blind shears -- they cut *and* seal.** The lower
    set was a casing shear (cut, do not seal) until Kirk configured the stack
    with two blind shears; the ``seals`` flag and its does-not-seal graph
    behaviour stay in the model for the riser connector and for any stack
    that does carry a casing shear. One height consequence worth knowing:
    the lower blind shears sit **beneath the lower kill line's tap**, so
    closing them isolates that line -- the lower choke, tapping the wellhead
    below the whole stack, is the one line they cannot take.

    The **riser connector** is `seals=False`: unlatching it does not shut
    anything in, and a connector drawn as a barrier would be a lie about the
    one control on the stack that ends the well rather than securing it.

    The pipe rams keep the keys ``upper_rams`` and ``lower_rams``, and the
    upper shears keep ``blind_shear_rams``: everything that operates a stack
    element addresses the same keys on both stacks, and what a student reads
    is the label.
    """
    return [
        # Eight elements, so seven bore segments between them, numbered from
        # the bottom: ANNULUS below the lower blind shears, BOP (the riser)
        # above the upper annular. Each outlet taps the segment at its listed
        # height, which is what makes the interleaving real rather than
        # decorative: whichever ram is closed, the lines below it still reach
        # the well and the lines above it do not.
        Valve("annular", "Upper annular", Node.STACK_BORE_7, Node.BOP,
              state=ValveState.OPEN, is_bop=True, bop_kind="annular"),
        Valve("riser_connector", "Riser connector", Node.STACK_BORE_6,
              Node.STACK_BORE_7,
              state=ValveState.OPEN, is_bop=True, bop_kind="connector",
              seals=False),

        *_failsafes("gas_relief", "gas relief", "choke", "gas_relief",
                    Node.STACK_BORE_6, Node.GAS_RELIEF_OUTLET,
                    Node.CHOKE_LINE),

        Valve("lower_annular", "Lower annular", Node.STACK_BORE_5,
              Node.STACK_BORE_6,
              state=ValveState.OPEN, is_bop=True, bop_kind="annular"),

        *_failsafes("upper_kill", "upper kill", "kill", "upper",
                    Node.STACK_BORE_5, Node.KILL_OUTLET, Node.KILL_LINE),

        Valve("blind_shear_rams", "Upper blind shear rams", Node.STACK_BORE_4,
              Node.STACK_BORE_5,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram", shears=True),

        *_failsafes("upper_choke", "upper choke", "choke", "upper",
                    Node.STACK_BORE_4, Node.CHOKE_OUTLET, Node.CHOKE_LINE),

        Valve("upper_rams", "Upper pipe rams", Node.STACK_BORE_3,
              Node.STACK_BORE_4,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram"),

        *_failsafes("middle_kill", "middle kill", "kill", "middle",
                    Node.STACK_BORE_3, Node.KILL_OUTLET_MIDDLE,
                    Node.KILL_LINE),

        Valve("middle_rams", "Middle pipe rams", Node.STACK_BORE_2,
              Node.STACK_BORE_3,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram"),

        *_failsafes("middle_choke", "middle choke", "choke", "middle",
                    Node.STACK_BORE_2, Node.CHOKE_OUTLET_MIDDLE,
                    Node.CHOKE_LINE),

        Valve("lower_rams", "Lower pipe rams", Node.STACK_BORE_1,
              Node.STACK_BORE_2,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram"),

        *_failsafes("lower_kill", "lower kill", "kill", "lower",
                    Node.STACK_BORE_1, Node.KILL_OUTLET_LOWER,
                    Node.KILL_LINE),

        # Blind, like the upper set: cuts and seals. Sits beneath the lower
        # kill line's tap, so closing it isolates that line -- only the
        # lower choke, on the wellhead below it, survives.
        Valve("lower_blind_shear_rams", "Lower blind shear rams", Node.ANNULUS,
              Node.STACK_BORE_1,
              state=ValveState.OPEN, is_bop=True, bop_kind="ram", shears=True),

        *_failsafes("lower_choke", "lower choke", "choke", "lower",
                    Node.ANNULUS, Node.CHOKE_OUTLET_LOWER, Node.CHOKE_LINE),
    ]


def default_valves(subsea: bool = False) -> list[Valve]:
    """The simplified lineup: the valves that change the outcome.

    Deliberately not a full choke manifold. Every valve here has a consequence
    a student can feel; adding the rest would add tedium without adding
    lessons. The **stack** is the exception and is modelled in full, because a
    stack a student cannot recognise is one they cannot practise on: see
    `surface_stack` and `subsea_stack`.
    """
    return (subsea_stack() if subsea else surface_stack()) + [
        Valve("pump_discharge", "Mud pump discharge", Node.PUMP, Node.STANDPIPE),
        # The second pump's discharge line. The graph has one PUMP node for
        # both pumps, so the two lines are parallel edges and either one open
        # gives the pumps their route -- an approximation (the graph cannot
        # tell which pump is turning), and why this one starts closed: every
        # scenario that blocks the pump closes `pump_discharge`, and a second
        # line open by default would quietly defeat it.
        Valve("pump_2_discharge", "Mud pump 2 discharge", Node.PUMP,
              Node.STANDPIPE),
        # The manifold's blanked branches -- see Node.CEMENT_HEAD. Real
        # valves on lines to equipment the simulator does not model, because
        # the manifold face draws them and a control that is only a picture
        # is worse than none.
        Valve("cement_head_valve", "Cement head isolation", Node.STANDPIPE,
              Node.CEMENT_HEAD),
        Valve("cement_pump_valve", "Cement pump isolation", Node.CEMENT_PUMP,
              Node.STANDPIPE),
        Valve("standpipe_valve", "Standpipe valve", Node.STANDPIPE, Node.STRING,
              bidirectional=True),
        # Deliberately one-way. Making this bidirectional would hand reverse
        # flow a route back into the string that bypasses the float, which is
        # the one thing actually standing in the way.
        Valve("kelly_cock", "Kelly cock", Node.STRING, Node.BIT,
              state=ValveState.OPEN),
        # The bit is always open to the annulus unless the nozzles plug.
        Valve("bit_nozzles", "Bit nozzles", Node.BIT, Node.ANNULUS,
              state=ValveState.OPEN, bidirectional=True),

        # A non-return valve in the string. Shut is the normal state -- most
        # strings run one -- and it passes mud down and nothing back up, which
        # is precisely what makes reverse circulation impossible until it is
        # pulled or ported. Modelled as the only route from the bit back into
        # the string, so opening it is what a ported float buys you.
        Valve("float_valve", "String float valve", Node.BIT, Node.STRING),

        # Returns with the stack open.
        Valve("flowline", "Flowline to shakers", Node.BOP, Node.FLOWLINE,
              state=ValveState.OPEN),
        Valve("shakers_to_pits", "Shakers to pits", Node.FLOWLINE, Node.PITS,
              state=ValveState.OPEN),

        # Returns with the stack closed. The stack's own isolation valves are
        # above; these are the lines from the stack up to the manifold.
        Valve("choke_line_to_manifold", "Choke line to manifold",
              Node.CHOKE_LINE, Node.CHOKE_MANIFOLD),
        Valve("kill_line_to_manifold", "Kill line to manifold",
              Node.KILL_LINE, Node.CHOKE_MANIFOLD),

        # Four throttling routes across the manifold, as a real one has: two
        # remote chokes worked from the panel and two handwheels on the
        # manifold itself. The pair is not decoration -- a choke that plugs
        # mid-kill is a fault this simulator can throw, and changing over to
        # the other one is the answer. Each has its own opening; see
        # Simulator.chokes.
        Valve("choke_1", "Remote choke 1", Node.CHOKE_MANIFOLD,
              Node.SEPARATOR, throttling=True),
        Valve("choke_2", "Remote choke 2", Node.CHOKE_MANIFOLD,
              Node.SEPARATOR, throttling=True),
        Valve("manual_choke_1", "Manual choke 1", Node.CHOKE_MANIFOLD,
              Node.SEPARATOR, throttling=True),
        Valve("manual_choke_2", "Manual choke 2", Node.CHOKE_MANIFOLD,
              Node.SEPARATOR, throttling=True),
        Valve("choke_bypass", "Choke manifold bypass", Node.CHOKE_MANIFOLD,
              Node.SEPARATOR),
        Valve("separator_to_pits", "Separator to pits", Node.SEPARATOR,
              Node.PITS, state=ValveState.OPEN),

        Valve("trip_tank_valve", "Trip tank to annulus", Node.TRIP_TANK,
              Node.ANNULUS),

        # Lubricating: the pump lined up to the kill line rather than the
        # standpipe, to put mud into the annulus on top of a gas cap. Shut
        # during any circulating kill, where mud goes down the string.
        Valve("pump_to_kill_line", "Pump to kill line", Node.PUMP,
              Node.KILL_LINE),

        # Reverse circulation takes returns up the drill pipe, so they leave
        # through the standpipe rather than the annulus and have to be routed
        # to the choke manifold to be controlled at all.
        Valve("standpipe_to_manifold", "Standpipe to choke manifold",
              Node.STANDPIPE, Node.CHOKE_MANIFOLD),
    ]


#: Where returns can leave the annulus once the stack is shut, in the order
#: they are tried. **These are the outlet spools, not the lines**: with a
#: double barrier on each side the annulus reaches the spool between the two
#: valves first, and reaches the line itself only if the outer one is open
#: too. Looking for the line here instead is a way to have a well that is
#: closed in whatever the student does with the HCRs.
_RETURN_ENTRIES: tuple[Node, ...] = (
    Node.CHOKE_OUTLET,
    Node.CHOKE_OUTLET_MIDDLE,
    Node.CHOKE_OUTLET_LOWER,
    Node.GAS_RELIEF_OUTLET,
    Node.KILL_OUTLET,
    Node.KILL_OUTLET_MIDDLE,
    Node.KILL_OUTLET_LOWER,
    Node.BIT,
)


@dataclass
class ReturnRoute:
    """How returns are leaving the well, if they are."""

    open_to_pits: bool
    through_choke: bool
    route: tuple[Node, ...] = ()

    @property
    def description(self) -> str:
        if not self.open_to_pits:
            return "closed in — no return path"
        if self.through_choke:
            return "returns through the choke manifold"
        return "returns up the flowline — no pressure control"


@dataclass
class FlowPath:
    """The lineup, and what it permits."""

    valves: list[Valve] = field(default_factory=default_valves)

    #: Set once a shear ram has been closed on the string. The pipe is cut and
    #: nothing can be circulated through it again -- see `_edges`.
    pipe_sheared: bool = False

    def __post_init__(self) -> None:
        self._by_name = {v.name: v for v in self.valves}

    @classmethod
    def for_stack(cls, subsea: bool) -> "FlowPath":
        """The lineup a rig with this stack on it would have."""
        return cls(valves=default_valves(subsea=subsea))

    # -- operating -----------------------------------------------------------

    def valve(self, name: str) -> Valve:
        if name not in self._by_name:
            raise KeyError(
                f"No valve called '{name}'. Available: "
                f"{', '.join(sorted(self._by_name))}"
            )
        return self._by_name[name]

    def set_valve(self, name: str, state: ValveState | str) -> None:
        self.valve(name).state = ValveState(state)

    def _group(self, name: str) -> list[Valve] | None:
        """The valves a rig hand means by "the choke line" or "the kill line".

        Lining a side up is one instruction on the floor and several valves on
        the stack: two HCRs on each surface flow cross, two fail-safes per
        outlet subsea. Setup code and scenario files should not have to know
        how many, so they name the side and this resolves it. Anything that
        *operates* one valve -- `set_valve`, and so the panel and the API --
        still names it exactly.

        On either stack this opens the **lower** outlet, the lowest line on
        its side of the stack, so it keeps its route to the well through
        everything except an element even lower down. On the surface stack
        that is nothing at all; subsea, the lower *choke* taps the wellhead
        below the whole stack, while the lower *kill* sits one element up
        and can be taken by the lower blind shears. Now that the outlet
        heights are real, this is a property of the graph rather than a
        promise.
        """
        if name not in ("choke_line", "kill_line"):
            return None
        side = name.split("_")[0]
        outlets = {v.outlet for v in self.valves if v.side == side}
        wanted = "lower" if "lower" in outlets else ""
        return [
            v for v in self.valves if v.side == side and v.outlet == wanted
        ]

    def open(self, *names: str) -> None:
        for name in names:
            group = self._group(name)
            if group is None:
                self.set_valve(name, ValveState.OPEN)
            else:
                for valve in group:
                    valve.state = ValveState.OPEN

    def close(self, *names: str) -> None:
        for name in names:
            group = self._group(name)
            if group is None:
                self.set_valve(name, ValveState.CLOSED)
            else:
                for valve in group:
                    valve.state = ValveState.CLOSED

    # -- state ---------------------------------------------------------------

    @property
    def bop_elements(self) -> list[Valve]:
        return [v for v in self.valves if v.is_bop]

    @property
    def bop_is_closed(self) -> bool:
        """True when a *sealing* element is closed, shutting the annulus in.

        The `seals` flag is what a non-sealing element opts out with: the
        riser connector today, a casing shear ram on any stack that carries
        one. Closing such an element is not shutting the well in however
        final it feels.
        """
        return any(not v.is_open and v.seals for v in self.bop_elements)

    @property
    def closed_bop_elements(self) -> list[str]:
        return [v.label for v in self.bop_elements if not v.is_open]

    def outlet_valves(self, side: str) -> list[Valve]:
        """Every isolation valve on the choke or kill side, in stack order."""
        return [v for v in self.valves if v.side == side]

    @property
    def kill_line_is_open(self) -> bool:
        """Whether the annulus actually reaches the kill line.

        The gauge on the kill line reads the annulus only when a complete route
        to it is open, which on either stack means a pair of valves rather than
        one. Asking the graph is the only version of this that survives the
        stack changing under it.
        """
        return self._reachable(
            self._edges(), Node.ANNULUS, Node.KILL_LINE
        ) is not None

    # -- path resolution -----------------------------------------------------

    def _edges(self) -> dict[Node, list[Node]]:
        """Adjacency for the currently open valves.

        A BOP element is an edge between the bore segments either side of it,
        and its sense is inverted -- *closing* one removes the route. An open
        element passes both ways, because fluid in an open bore does. A closed
        element that does not seal still passes: an unlatched riser connector
        has not shut anything in, and a casing shear ram -- were a stack to
        carry one -- would cut and leave the bore open. The well reaches the
        bell nipple, and so the flowline,
        only through the whole chain of open elements; an outlet taps its own
        segment, so it reaches the well only while every element *below* it is
        open. That is what makes "take returns from a cross below the ram you
        closed" a real decision rather than a drawing.

        **A sheared string is not a route.** Once a shear ram has been closed
        on pipe the string is cut at the stack, so every edge through it goes:
        the pump has nowhere to go and nothing can be circulated out. The well
        may well be sealed -- blind shear rams do seal -- but the exercise is
        over as a kill, which is the honest consequence of reaching for them.
        """
        edges: dict[Node, list[Node]] = {}
        severed = {Node.STRING, Node.BIT}
        for valve in self.valves:
            if valve.is_bop:
                if not valve.is_open and valve.seals:
                    continue
                edges.setdefault(valve.frm, []).append(valve.to)
                edges.setdefault(valve.to, []).append(valve.frm)
                continue
            if not valve.is_open:
                continue
            if self.pipe_sheared and (
                valve.frm in severed or valve.to in severed
            ):
                continue
            edges.setdefault(valve.frm, []).append(valve.to)
            if valve.bidirectional:
                edges.setdefault(valve.to, []).append(valve.frm)
        return edges

    @staticmethod
    def _reachable(
        edges: dict[Node, list[Node]], start: Node, goal: Node
    ) -> tuple[Node, ...] | None:
        queue: deque[tuple[Node, ...]] = deque([(start,)])
        seen = {start}
        while queue:
            path = queue.popleft()
            if path[-1] is goal:
                return path
            for nxt in edges.get(path[-1], ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(path + (nxt,))
        return None

    def injection_path(self) -> tuple[Node, ...] | None:
        """Route from the pump into the annulus, or None if the pump is blocked.

        None means the pump is deadheaded: it is running against a closed
        valve, and pressure will build with nowhere to go.
        """
        return self._reachable(
            self._edges(), Node.PUMP, Node.ANNULUS
        )

    @property
    def pump_is_deadheaded(self) -> bool:
        return self.injection_path() is None

    @property
    def injecting_into_the_annulus(self) -> bool:
        """True when the pump reaches the annulus without going down the pipe.

        The difference matters to the engine rather than to the graph. Mud
        pumped down the string comes round the bit and pushes the annulus
        contents *up* from the bottom. Mud pumped into the kill line arrives at
        the *top* and falls through whatever gas is up there to sit beneath it.
        Same pump, same barrels, opposite ends of the well.
        """
        path = self.injection_path()
        if path is None:
            return False
        return Node.STRING not in path

    def return_route(self) -> ReturnRoute:
        """How returns leave the well, given the lineup and the BOP.

        With the stack open, returns take the flowline and there is no pressure
        control however the choke is set. With the stack closed, they can only
        go out the choke or kill line.
        """
        edges = self._edges()

        # The flowline is checked for by name rather than found by search:
        # with the bore now a chain of segments it is a longer path than the
        # choke route, and a breadth-first search would prefer the choke --
        # but an open well takes the path of least resistance, and that is
        # the flowline at atmospheric whatever the choke is set to.
        if not self.bop_is_closed:
            lead = self._reachable(edges, Node.ANNULUS, Node.FLOWLINE)
            if lead is not None:
                rest = self._reachable(edges, Node.FLOWLINE, Node.PITS)
                if rest is not None:
                    return ReturnRoute(True, False, lead + rest[1:])

        # Stack closed: the flowline is sealed off, so only routes through the
        # choke or kill line count -- or, reverse circulating, back through the
        # bit and up the inside of the string. An entry is only usable if the
        # well actually reaches it, which is where a closed ram takes the
        # outlets above it out of play.
        #
        # Whatever the pump is coming in through is an inlet, not an outlet.
        # Without that exclusion a line that runs both ways gets picked as the
        # return route it is currently feeding, and the reported path doubles
        # back on itself.
        inlet = self.injection_path() or ()
        for entry in _RETURN_ENTRIES:
            if entry in inlet:
                continue
            lead = self._reachable(edges, Node.ANNULUS, entry)
            if lead is None:
                continue
            path = self._reachable(edges, entry, Node.PITS)
            if path is not None:
                return ReturnRoute(True, True, lead + path[1:])

        return ReturnRoute(False, False, ())

    @property
    def is_reverse_circulating(self) -> bool:
        """Pumping down the annulus and taking returns up the drill pipe.

        The influx then travels up the *inside of the string*, which is the
        whole attraction: it never passes the casing shoe, and the string holds
        a fraction of the annulus volume so it is out in far fewer strokes.

        Both halves have to be true. Pumping into the annulus with returns
        still coming up the annulus is lubricating, not reverse circulating.
        """
        if not self.injecting_into_the_annulus:
            return False
        route = self.return_route()
        return route.open_to_pits and Node.STRING in route.route

    @property
    def is_closed_in(self) -> bool:
        """True when nothing can leave the well."""
        return not self.return_route().open_to_pits

    def active_chokes(self) -> list[Valve]:
        """Throttling valves on a live return route.

        More than one open at a time is a lineup error rather than a plan, and
        the engine bills it as one: each passes its own flow, so two half-shut
        chokes let past far more than the driller working one of them expects.
        """
        route = self.return_route()
        if not route.through_choke:
            return []
        return [
            v for v in self.valves
            if v.throttling and v.is_open
            and self._reachable(
                self._edges(), v.to, Node.PITS
            ) is not None
        ]

    def bypass_is_open(self) -> bool:
        """The bypass defeats the choke: returns go round it, uncontrolled."""
        valve = self.valve("choke_bypass")
        if not valve.is_open:
            return False
        edges = self._edges()
        return self._reachable(edges, valve.to, Node.PITS) is not None

    @property
    def trip_tank_is_lined_up(self) -> bool:
        """Whether the hole is being kept full from the trip tank.

        The valve joins the tank straight to the annulus below the stack, so
        there is nothing to search for -- it is open or it is not. What it
        decides is which vessel the well's volume comes out of, and therefore
        which gauge is measuring the trip.
        """
        return self.valve("trip_tank_valve").is_open

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict[str, object]:
        route = self.return_route()
        return {
            "BOP": (
                f"closed: {', '.join(self.closed_bop_elements)}"
                if self.bop_is_closed else "open"
            ),
            "Pump path": (
                "blocked — deadheaded" if self.pump_is_deadheaded
                else " → ".join(n.value for n in self.injection_path())
            ),
            "Returns": route.description,
            "Open valves": ", ".join(
                v.label for v in self.valves if v.is_open and not v.is_bop
            ) or "none",
        }
