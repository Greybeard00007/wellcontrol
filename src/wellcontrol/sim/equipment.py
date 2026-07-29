"""Equipment the student operates: the pump, the choke, the trip tank.

Everything here answers one question -- how much flow, given what the student
has set? The engine takes those answers and does the volume balance.

The choke is the important one. It is the instrument the whole exercise turns
on, and its behaviour has to be right in a specific way: **the same choke
movement must matter differently depending on what is in the hole.** That is
not modelled here directly; it falls out of the volume balance in
:mod:`wellcontrol.sim.engine`. What this module supplies is an honest
relationship between opening, pressure drop and flow.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..geometry import Pump


@dataclass
class MudPump:
    """A rig pump under the driller's control.

    Wraps the library :class:`~wellcontrol.geometry.Pump` with the state a
    simulator needs: whether it is running, how fast, and how quickly it can
    change speed. Pumps do not step instantly from zero to kill rate, and the
    ramp is where a lot of well control goes wrong.
    """

    pump: Pump
    running: bool = False
    spm: float = 0.0
    commanded_spm: float = 0.0
    max_spm: float = 120.0
    #: How fast the pump can change speed, spm per second.
    ramp_rate: float = 2.0

    def command(self, spm: float) -> None:
        """Ask for a speed. The pump ramps toward it rather than jumping."""
        self.commanded_spm = max(0.0, min(spm, self.max_spm))
        if self.commanded_spm > 0:
            self.running = True

    def stop(self) -> None:
        self.commanded_spm = 0.0

    def advance(self, dt: float) -> None:
        """Move the actual speed toward the commanded speed."""
        step = self.ramp_rate * dt
        if self.spm < self.commanded_spm:
            self.spm = min(self.commanded_spm, self.spm + step)
        elif self.spm > self.commanded_spm:
            self.spm = max(self.commanded_spm, self.spm - step)
        self.running = self.spm > 1e-6

    @property
    def flow_bbl_per_min(self) -> float:
        return self.pump.output_bbl_per_stroke * self.spm

    def strokes_in(self, dt: float) -> float:
        return self.spm * dt / 60.0


@dataclass
class AdjustableChoke:
    """An adjustable choke.

    Flow follows the standard orifice relation::

        Q = Cd . A . sqrt(2 . dP / rho)

    What matters for training is the **shape**, not the constant: flow goes
    with the square root of pressure drop and roughly with the square of the
    opening near the seat. So the choke is insensitive when wide open and
    vicious when nearly shut, which is where a kill is actually controlled.

    Attributes:
        position: Opening, 0 (shut) to 100 (wide open).
        full_open_area_in2: Flow area at 100%.
        discharge_coefficient: Cd, around 0.8 for a choke bean.
        travel_rate: How fast the position can change, percent per second. A
            real choke takes seconds to move, and a student who expects it to
            be instant will over-correct.
    """

    position: float = 0.0
    full_open_area_in2: float = 2.0
    discharge_coefficient: float = 0.8
    travel_rate: float = 10.0
    commanded_position: float = 0.0

    def command(self, position: float) -> None:
        self.commanded_position = max(0.0, min(100.0, position))

    def advance(self, dt: float) -> None:
        step = self.travel_rate * dt
        if self.position < self.commanded_position:
            self.position = min(self.commanded_position, self.position + step)
        elif self.position > self.commanded_position:
            self.position = max(self.commanded_position, self.position - step)

    @property
    def area_in2(self) -> float:
        """Flow area at the current opening.

        Squared rather than linear with position: a choke's effective area
        closes far faster than its travel near the seat, which is why the last
        ten percent of the handle does most of the work.
        """
        fraction = max(0.0, min(1.0, self.position / 100.0))
        return self.full_open_area_in2 * fraction ** 2

    def flow_bbl_per_min(
        self, pressure_drop_psi: float, density_ppg: float
    ) -> float:
        """Flow the choke will pass at a given pressure drop, bbl/min.

        Returns zero for a shut choke or a negative pressure drop -- a choke
        does not flow backwards.
        """
        if self.area_in2 <= 0.0 or pressure_drop_psi <= 0.0 or density_ppg <= 0:
            return 0.0

        # Q(gpm) = 29.81 . Cd . d^2 . sqrt(dP / rho) is the familiar nozzle
        # form with d in inches; expressed through area it becomes the constant
        # below. Converted to bbl/min for the volume balance.
        gpm = (
            37.94
            * self.discharge_coefficient
            * self.area_in2
            * math.sqrt(pressure_drop_psi / density_ppg)
        )
        return gpm / 42.0

    @property
    def is_shut(self) -> bool:
        return self.position <= 1e-9


@dataclass
class Alarm:
    """A set point, a lamp, and a deadband so the lamp does not chatter.

    A value sitting *on* its set point crosses it repeatedly, and a lamp that
    flickers is a lamp a student learns to ignore. The deadband means the alarm
    latches on at the set point and only clears once the value has come back
    past it by a margin -- the same reasoning as ``EventLog.set_level``, which
    is what stopped a well-flown kill logging the same condition a hundred
    times.

    Attributes:
        low: Set point below which the low lamp lights. None disables it.
        high: Set point above which the high lamp lights. None disables it.
        armed: The panel switch. A disarmed alarm still tracks, so the
            instructor can see what the student silenced.
        deadband: How far back past the set point the value must come before
            the lamp clears.
    """

    low: float | None = None
    high: float | None = None
    armed: bool = True
    deadband: float = 0.0
    low_active: bool = False
    high_active: bool = False

    def update(self, value: float) -> None:
        """Take a reading and settle the lamps."""
        if self.low is None:
            self.low_active = False
        elif self.low_active:
            self.low_active = value <= self.low + self.deadband
        else:
            self.low_active = value < self.low

        if self.high is None:
            self.high_active = False
        elif self.high_active:
            self.high_active = value >= self.high - self.deadband
        else:
            self.high_active = value > self.high

    @property
    def sounding(self) -> bool:
        """Whether the horn is actually going -- lamps lit *and* armed."""
        return self.armed and (self.low_active or self.high_active)

    @property
    def active(self) -> bool:
        """Whether either lamp is lit, armed or not."""
        return self.low_active or self.high_active

    def summary(self) -> dict:
        return {
            "low": self.low,
            "high": self.high,
            "armed": self.armed,
            "low_active": self.low_active,
            "high_active": self.high_active,
            "sounding": self.sounding,
        }


@dataclass
class TransferPump:
    """A pump that moves mud from one vessel to another.

    Not the rig pump -- these are the small fixed-rate pumps around the pit
    room and the trip tank. They either run or they do not, and how much they
    actually move is limited by what is in the vessel they draw from and how
    much room is in the one they fill. That limiting is done by whoever owns
    both vessels; this only says how much it *wants* to move.
    """

    rate_bbl_per_min: float = 5.0
    running: bool = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def set(self, running: bool) -> None:
        self.running = bool(running)

    def demand_bbl(self, dt: float) -> float:
        """Volume it would move in ``dt`` seconds if nothing limited it."""
        return self.rate_bbl_per_min * dt / 60.0 if self.running else 0.0


def _transfer(
    source: float, sink: float, sink_capacity: float, wanted: float
) -> float:
    """How much can actually move: limited by supply and by room.

    A pump cannot draw a tank below empty or push one past its top, and both
    limits matter -- a trip tank overflowing while the fill pump runs is a
    thing that happens, and the level then stops telling you anything.
    """
    return max(0.0, min(wanted, source, sink_capacity - sink))


@dataclass
class TripTank:
    """The tank the hole is filled from, and the instrument a trip turns on.

    **This is how a kick is caught while tripping**, which is when a large
    share of them arrive. Pulling steel out of the hole leaves a void that has
    to be filled; the tank level falls by the volume of steel removed. If it
    falls by *less* than that, something down the hole is giving fluid back.
    Nothing about that is scripted -- it falls out of the same volume balance
    as everything else, once the tank is actually in it.

    Defaults follow the reference panel: a 35 bbl tank sitting at 21 bbl, with
    the low alarm at 10 and the high at 30.

    Attributes:
        fillup_pump: Tank to the well. Keeps the hole full while pulling out.
        fill_pump: Active pits to the tank. Tops the tank back up.
        drain_pump: Tank to the active pits. Empties it after a trip.
    """

    volume_bbl: float = 21.0
    capacity_bbl: float = 35.0
    baseline_bbl: float = 21.0
    alarm: Alarm = field(
        default_factory=lambda: Alarm(low=10.0, high=30.0, deadband=0.5)
    )
    fillup_pump: TransferPump = field(
        default_factory=lambda: TransferPump(rate_bbl_per_min=4.0)
    )
    fill_pump: TransferPump = field(
        default_factory=lambda: TransferPump(rate_bbl_per_min=6.0)
    )
    drain_pump: TransferPump = field(
        default_factory=lambda: TransferPump(rate_bbl_per_min=6.0)
    )

    @property
    def change_bbl(self) -> float:
        """Positive when the tank has gained -- the well gave fluid back."""
        return self.volume_bbl - self.baseline_bbl

    @property
    def level_fraction(self) -> float:
        if self.capacity_bbl <= 0:
            return 0.0
        return max(0.0, min(1.0, self.volume_bbl / self.capacity_bbl))

    def zero(self) -> None:
        self.baseline_bbl = self.volume_bbl

    def add(self, bbl: float) -> float:
        """Put volume in (or take it out, negative). Returns what actually moved.

        Clamped at empty and at the top. The difference between what was asked
        for and what moved is a tank that has run dry or overflowed, and either
        one means the level has stopped measuring the well.
        """
        target = self.volume_bbl + bbl
        clamped = max(0.0, min(self.capacity_bbl, target))
        moved = clamped - self.volume_bbl
        self.volume_bbl = clamped
        return moved

    def update_alarms(self) -> None:
        self.alarm.update(self.volume_bbl)

    def summary(self) -> dict:
        return {
            "volume_bbl": round(self.volume_bbl, 1),
            "capacity_bbl": self.capacity_bbl,
            "change_bbl": round(self.change_bbl, 2),
            "level_fraction": round(self.level_fraction, 3),
            "alarm": self.alarm.summary(),
            "fillup_pump": self.fillup_pump.running,
            "fill_pump": self.fill_pump.running,
            "drain_pump": self.drain_pump.running,
        }


@dataclass
class ReturnFlowMeter:
    """The flow paddle in the flowline, and the alarm on it.

    **The differential is the kick indicator, not the flow.** More coming back
    than is going in means the well is contributing, and that shows up sooner
    than a pit gain because it is a rate rather than an accumulated volume.
    Flow continuing with the pump off says the same thing louder.

    So the alarm sits on ``differential_bpm`` rather than on the return rate.
    Alarming on the rate itself would fire every time the driller changed pump
    speed, which is how a real flow alarm gets switched off and left off.

    The reference DCU shows this either as a rate or as a percentage of what is
    being pumped, hence both. Percentage is None with the pump off, because
    dividing by nothing is not 100% -- it is a different question, and the
    honest answer is the rate.
    """

    flow_in_bpm: float = 0.0
    flow_out_bpm: float = 0.0
    alarm: Alarm = field(
        default_factory=lambda: Alarm(low=-0.5, high=0.5, deadband=0.15)
    )

    def update(self, flow_in_bpm: float, flow_out_bpm: float) -> None:
        self.flow_in_bpm = flow_in_bpm
        self.flow_out_bpm = flow_out_bpm
        self.alarm.update(self.differential_bpm)

    @property
    def differential_bpm(self) -> float:
        """Positive when more is coming back than is going in."""
        return self.flow_out_bpm - self.flow_in_bpm

    @property
    def percent_of_flow_in(self) -> float | None:
        """Returns as a percentage of what is pumped. None with the pump off."""
        if self.flow_in_bpm <= 1e-6:
            return None
        return 100.0 * self.flow_out_bpm / self.flow_in_bpm

    @property
    def flowing_with_the_pump_off(self) -> bool:
        """The one that is never anything else.

        With nothing going in, anything coming back is the well. No pump rate
        change, no surface explanation, no ambiguity.
        """
        return self.flow_in_bpm <= 1e-6 and self.flow_out_bpm > 0.05

    def summary(self) -> dict:
        percent = self.percent_of_flow_in
        return {
            "flow_in_bpm": round(self.flow_in_bpm, 2),
            "flow_out_bpm": round(self.flow_out_bpm, 2),
            "differential_bpm": round(self.differential_bpm, 2),
            "percent": None if percent is None else round(percent, 1),
            "flowing_with_the_pump_off": self.flowing_with_the_pump_off,
            "alarm": self.alarm.summary(),
        }


@dataclass
class Accumulator:
    """The BOP closing unit, and the fact that it is not free.

    Every element operated takes hydraulic volume out of the bottles, and the
    pressure falls. The pumps put it back, slowly. A student who cycles the
    stack because they are unsure can find they have nothing left to close
    with, which is a real way to lose a well and is invisible if the BOP panel
    is drawn as a row of indicator lights.

    Bottle pressure is not the whole unit, and the panel makes that obvious.
    Stack functions run on **regulated** pressure, not on what is in the
    bottles: the manifold regulator feeds the rams and the annular regulator
    feeds the annular, and an element turned down below what it takes to shift
    will not shift however full the bottles are. That is a real way to find a
    stack unresponsive with a healthy accumulator gauge, and it is the reason
    the two regulators have their own gauges and their own Inc./Dec. buttons on
    the panel rather than being implied by the accumulator reading.

    Attributes:
        pressure_psi: What the gauge on the closing unit reads.
        minimum_psi: Below this there is not enough to shift an element. Stands
            in for the accumulator precharge.
        recharge_psi_per_min: How fast the pumps make it back up.
        annular_cost_psi: Pressure given up closing or opening the annular. It
            has the largest closing volume of the three.
        ram_cost_psi: The same for a set of rams.
        manifold_regulator_psi: What the rams are given to work on.
        annular_regulator_psi: The same for the annular, set lower on a real
            unit so the element is not damaged closing on pipe.
        bypass_open: The bypass puts full bottle pressure on the manifold,
            past the regulator. It is there for the moment a regulator will not
            pass enough, and it is not where the unit is left.
        master_open: The master valve between the bottles and the manifold.
            Shut, the whole stack is dead however healthy every gauge on the
            unit looks -- which is why it is on the panel rather than assumed.
        air_psi: Supply air to the closing unit's pumps. **The pumps stop
            without it** and the bottles never come back up, which is what an
            air pressure gauge on a BOP panel is for.
        flow_gal: The flow counter, gallons of control fluid used. This is how
            a driller tells an element actually *moved*: the right number of
            gallons went out to it. A lamp only says a switch was thrown.
        pod: Which control pod is selected, subsea. Blank on a surface stack.
        failed_pods: Pods that will not answer. Select the other one.
        armed: Whether the panel has been armed for one function. See
            ``arm`` -- **nothing on the stack shifts without it.**
    """

    pressure_psi: float = 3000.0
    capacity_psi: float = 3000.0
    minimum_psi: float = 1200.0
    recharge_psi_per_min: float = 60.0
    annular_cost_psi: float = 250.0
    ram_cost_psi: float = 150.0
    #: An HCR is a gate valve with an actuator on it, not a preventer, so it
    #: costs a fraction of what a ram does -- but it is not free, and a student
    #: who lines the choke side up twice has spent it.
    hcr_cost_psi: float = 40.0
    connector_cost_psi: float = 320.0

    manifold_regulator_psi: float = 1500.0
    annular_regulator_psi: float = 1500.0
    bypass_open: bool = False
    master_open: bool = True
    air_psi: float = 120.0
    flow_gal: float = 0.0
    annular_gal: float = 20.0
    ram_gal: float = 10.0
    hcr_gal: float = 2.5
    connector_gal: float = 26.0
    pod: str = ""
    failed_pods: set[str] = field(default_factory=set)
    armed: bool = False

    #: Regulated pressure below which an element will not shift at all.
    minimum_operating_psi: float = 400.0
    #: Supply air below which the charging pumps stop.
    minimum_air_psi: float = 60.0

    def arm(self) -> None:
        """Arm the panel for **one** function. Two-handed operation.

        Nothing on a BOP panel is a single press, and that is a safety design
        rather than an inconvenience: closing a blind shear ram on a live well
        is not something anybody should be able to do by brushing a switch. So
        every function takes two deliberate acts -- the master valve handle
        functioned on a surface unit, the *Push to Operate* button held down on
        a subsea one -- and *then* the element.

        The arm is spent by the one function that follows, whether or not the
        element actually moved. A student who arms, presses Close and finds the
        rams where they left them has to arm again to try, which is what the
        panel gives them: see ``operate``, where a dead pod, an empty bottle or
        a regulator wound off all read as the element simply not shifting.
        """
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    @property
    def can_operate(self) -> bool:
        return (
            self.pressure_psi > self.minimum_psi
            and self.master_open
            and not self.pod_is_dead
        )

    @property
    def pod_is_dead(self) -> bool:
        """Whether the selected pod will answer. Never true on a surface stack."""
        return bool(self.pod) and self.pod in self.failed_pods

    @property
    def fraction(self) -> float:
        return max(0.0, min(1.0, self.pressure_psi / self.capacity_psi))

    @property
    def manifold_psi(self) -> float:
        """Regulated pressure to the rams, as the readback gauge shows it.

        Never more than there is in the bottles, and the bypass takes the
        regulator out of the way altogether.
        """
        if self.bypass_open:
            return self.pressure_psi
        return min(self.manifold_regulator_psi, self.pressure_psi)

    @property
    def annular_psi(self) -> float:
        if self.bypass_open:
            return self.pressure_psi
        return min(self.annular_regulator_psi, self.pressure_psi)

    def supply_psi(self, is_annular: bool) -> float:
        if not self.master_open:
            return 0.0
        return self.annular_psi if is_annular else self.manifold_psi

    #: What each kind of function costs the bottles, and what it puts through
    #: the counter. Keyed by ``Valve.bop_kind``, with ``hcr`` for an outlet
    #: valve -- which has no ``bop_kind`` because it is not a preventer.
    _COSTS = {
        "annular": ("annular_cost_psi", "annular_gal"),
        "ram": ("ram_cost_psi", "ram_gal"),
        "hcr": ("hcr_cost_psi", "hcr_gal"),
        # A riser connector is the largest single function on a subsea stack:
        # it is holding the whole riser on.
        "connector": ("connector_cost_psi", "connector_gal"),
    }

    def operate(self, kind: str) -> bool:
        """Spend the volume for one function. False if it will not shift.

        ``kind`` is ``annular``, ``ram`` or ``hcr``. **An HCR is on the
        closing unit exactly as a ram is** -- hydraulically controlled remote
        is the whole of the name -- so it comes through here rather than being
        set like the gate valve on the manifold, and everything below applies
        to lining a side up as much as to shutting the well in.

        Four ways it will not shift: the panel was never armed (see ``arm`` --
        every function is two-handed), nothing left in the bottles, the master
        shut, or a regulator wound down below what the function takes. All of
        them read as the valve simply not moving, which is what they look like
        on the rig.

        **The arm is spent first, and unconditionally.** One arm buys one
        attempt, not one success -- otherwise the lamp staying lit after a
        function that did nothing would announce the fault, and working out why
        the stack will not shift is the exercise.
        """
        if not self.armed:
            return False
        self.armed = False
        if not self.can_operate:
            return False
        # The annular has its own regulator; the rams and the HCRs are both on
        # the manifold one, which is why winding that off takes the choke line
        # away as well as the pipe rams.
        is_annular = kind == "annular"
        if self.supply_psi(is_annular) < self.minimum_operating_psi:
            return False
        cost, gal = self._COSTS.get(kind, self._COSTS["ram"])
        self.pressure_psi = max(0.0, self.pressure_psi - getattr(self, cost))
        self.flow_gal += getattr(self, gal)
        return True

    def adjust(self) -> bool:
        """Whether a *setting* on the unit may be changed. Two-handed as well.

        The regulators and the bypass decide what pressure every function gets,
        so reaching for them is reaching into the closing unit and the master
        valve is held over for it exactly as it is for a ram.

        **It does not spend the arm**, and that is the difference between a
        setting and a function. Winding a regulator from 1,500 psi down to 500
        is ten presses of Dec.; charged an arm apiece it would be twenty, and a
        control nobody uses teaches nothing. The master is held over while the
        adjustment is made, and it is still over for the function afterwards.
        """
        return self.armed

    def reset_flow_counter(self) -> None:
        self.flow_gal = 0.0

    def advance(self, dt: float) -> None:
        if self.air_psi < self.minimum_air_psi:
            return
        self.pressure_psi = min(
            self.capacity_psi,
            self.pressure_psi + self.recharge_psi_per_min * dt / 60.0,
        )


@dataclass
class PitSystem:
    """The active system, and the gain that tells you a well is flowing.

    Two vessels, because the reference panel has two and because the
    distinction matters: the **active** pit is in the circulating loop and its
    level is the kick indicator, while the **reserve** holds mud that is not
    being circulated. Transferring reserve into active raises the level without
    the well having done anything -- which is exactly the false gain a student
    has to learn to discount, and a real reason pit-gain alarms get ignored.

    ``gain_bbl`` stays what it always was: active level against the mark, which
    is what the driller reads.

    Attributes:
        deviation_alarm: Set points on the *gain*, not the level. The dial on
            the reference DCU reads LOSS/GAIN either side of zero.
    """

    active_bbl: float = 800.0
    active_capacity_bbl: float = 1000.0
    baseline_bbl: float = 800.0
    reserve_bbl: float = 400.0
    reserve_capacity_bbl: float = 700.0
    deviation_alarm: Alarm = field(
        default_factory=lambda: Alarm(low=-10.0, high=10.0, deadband=1.0)
    )
    transfer_pump: TransferPump = field(
        default_factory=lambda: TransferPump(rate_bbl_per_min=10.0)
    )

    @property
    def gain_bbl(self) -> float:
        return self.active_bbl - self.baseline_bbl

    @property
    def deviation_bbl(self) -> float:
        """What the LOSS/GAIN dial reads. Same number, named as the dial is."""
        return self.gain_bbl

    @property
    def total_bbl(self) -> float:
        return self.active_bbl + self.reserve_bbl

    def zero(self) -> None:
        self.baseline_bbl = self.active_bbl

    def add(self, bbl: float) -> float:
        """Change the active level, clamped. Returns what actually moved."""
        target = self.active_bbl + bbl
        clamped = max(0.0, min(self.active_capacity_bbl, target))
        moved = clamped - self.active_bbl
        self.active_bbl = clamped
        return moved

    def transfer_reserve_to_active(self, bbl: float) -> float:
        """Move mud from reserve into the active system. Returns what moved.

        **This does not touch the baseline**, so it shows up as a gain. That is
        deliberate and it is what happens on a rig: the level went up and the
        gauge cannot tell why. Re-zeroing after a transfer is the driller's job.
        """
        moved = _transfer(
            self.reserve_bbl, self.active_bbl, self.active_capacity_bbl, bbl
        )
        self.reserve_bbl -= moved
        self.active_bbl += moved
        return moved

    def update_alarms(self) -> None:
        self.deviation_alarm.update(self.gain_bbl)

    def summary(self) -> dict:
        return {
            "active_bbl": round(self.active_bbl, 1),
            "active_capacity_bbl": self.active_capacity_bbl,
            "reserve_bbl": round(self.reserve_bbl, 1),
            "reserve_capacity_bbl": self.reserve_capacity_bbl,
            "gain_bbl": round(self.gain_bbl, 1),
            "deviation_bbl": round(self.deviation_bbl, 1),
            "alarm": self.deviation_alarm.summary(),
            "transfer_pump": self.transfer_pump.running,
        }
