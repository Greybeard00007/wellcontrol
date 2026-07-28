"""Making hole: weight on bit, rotary, torque, penetration rate.

Stage 5, and the only part of this simulator that is not about well control.
It earns its place for one reason:

**A drilling break is a kick indicator, and it arrives before the pit gain.**
Penetration rate rises when the chip hold-down effect weakens -- that is, when
the mud is less overbalanced than it was. Drill into a zone the mud cannot hold
and the bit speeds up *because* the well is going underbalanced, seconds to
minutes before anything reaches the pits. A student taught to notice that is
being taught the earliest warning a rig has, and no amount of pit-gain practice
substitutes for it.

So the model here is deliberately shaped to make that link real rather than
scripted. Everything else -- torque, bit wear, the auto-driller -- exists
because a driller works to those instruments and a screen without them is a
screen a driller does not recognise.

## What this is not

Not a drilling optimisation model. The exponents are the familiar
Bourgoyne-Young *shape* -- rate going roughly linearly with weight and as a
fractional power of speed -- with coefficients chosen so the reference well
drills at a plausible few tens of feet an hour. Nothing here should be used to
plan a bit run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Differential pressure the rate is normalised against, psi. At this
#: overbalance the chip hold-down factor is exactly 1, so a well drilling
#: normally sits at its nominal rate and any *change* is information.
#:
#: **Measured, not assumed.** A well drilling ahead does not carry the static
#: DRILLING_OVERBALANCE_PSI: circulating adds annular friction on top, and on
#: the reference well the bit is working against about 500 psi rather than 150.
#: Normalising against the static figure put the baseline hold-down at 0.34,
#: so a well drilling normally sat at a third of its nominal rate and the
#: break came out at five times -- theatrical, and not what a driller sees.
NOMINAL_OVERBALANCE_PSI: float = 500.0

#: How hard penetration rate responds to differential pressure. Set so that
#: losing the whole overbalance roughly doubles the rate: a break a student
#: cannot miss, and about what a real one looks like.
HOLD_DOWN_SENSITIVITY: float = 1.2

#: Weight exponent (roughly linear) and speed exponent (fractional).
WOB_EXPONENT: float = 1.0
RPM_EXPONENT: float = 0.6

#: Feet per hour at 20 klbf, 120 rpm, nominal overbalance, sharp bit.
REFERENCE_ROP_FT_PER_HR: float = 60.0

#: Bit life. Wear runs 0 (new) to 1 (finished) and costs most of the rate.
WEAR_PER_KLBF_RPM_HOUR: float = 4.0e-6
WEAR_RATE_PENALTY: float = 0.7


@dataclass
class RotarySystem:
    """The top drive: a speed the driller asks for, and the torque it costs."""

    rpm: float = 0.0
    commanded_rpm: float = 0.0
    max_rpm: float = 200.0
    #: rpm per second. A top drive is not instant and neither is stopping it.
    ramp_rate: float = 15.0
    torque_kftlb: float = 0.0

    def command(self, rpm: float) -> None:
        self.commanded_rpm = max(0.0, min(float(rpm), self.max_rpm))

    def stop(self) -> None:
        self.commanded_rpm = 0.0

    def advance(self, dt: float) -> None:
        step = self.ramp_rate * dt
        if self.rpm < self.commanded_rpm:
            self.rpm = min(self.commanded_rpm, self.rpm + step)
        elif self.rpm > self.commanded_rpm:
            self.rpm = max(self.commanded_rpm, self.rpm - step)

    @property
    def turning(self) -> bool:
        return self.rpm > 1e-6


@dataclass
class DrillingModel:
    """Weight on bit, penetration rate, bit wear, and the auto-driller.

    Args:
        rock_strength: Relative drillability. 1.0 is the reference formation;
            higher is harder and slower.
        auto_driller: Whether the machine is holding the weight rather than
            the driller. Real rigs mostly leave it on, which is exactly why a
            student should have to notice it working against them.
        wob_setpoint_klbf: What the auto-driller holds to.
    """

    rotary: RotarySystem = field(default_factory=RotarySystem)
    wob_klbf: float = 0.0
    commanded_wob_klbf: float = 0.0
    max_wob_klbf: float = 60.0
    #: klbf per second the string can be fed off. Slower than it sounds.
    feed_rate: float = 2.0
    auto_driller: bool = False
    wob_setpoint_klbf: float = 20.0
    rock_strength: float = 1.0
    bit_wear: float = 0.0
    #: Last computed values, for the panel.
    rop_ft_per_hr: float = 0.0
    hold_down_factor: float = 1.0

    # -- controls ------------------------------------------------------------

    def command_wob(self, klbf: float) -> None:
        self.commanded_wob_klbf = max(0.0, min(float(klbf), self.max_wob_klbf))

    def set_auto(self, on: bool, setpoint_klbf: float | None = None) -> None:
        self.auto_driller = bool(on)
        if setpoint_klbf is not None:
            self.wob_setpoint_klbf = max(
                0.0, min(float(setpoint_klbf), self.max_wob_klbf)
            )

    # -- the model -----------------------------------------------------------

    def hold_down(self, overbalance_psi: float) -> float:
        """How much the mud column is holding the cuttings down.

        **This is the whole reason the model is here.** Rate rises as the
        differential pressure falls, so drilling into a zone the mud cannot
        hold produces a break *before* anything reaches the pits -- and it
        falls out of the pressures rather than being announced.

        Normalised to 1.0 at :data:`NOMINAL_OVERBALANCE_PSI`. Clamped above,
        because past a point more overbalance stops making much difference and
        an unbounded exponential would let a wildly underbalanced well drill
        at an absurd rate.
        """
        excess = NOMINAL_OVERBALANCE_PSI - overbalance_psi
        factor = math.exp(HOLD_DOWN_SENSITIVITY * excess / 1000.0)
        return max(0.15, min(3.0, factor))

    def penetration_rate(self, overbalance_psi: float) -> float:
        """Feet per hour, given what the bit currently has to work with."""
        if not self.rotary.turning or self.wob_klbf <= 0.1:
            self.rop_ft_per_hr = 0.0
            return 0.0

        self.hold_down_factor = self.hold_down(overbalance_psi)
        wear_factor = max(0.05, 1.0 - WEAR_RATE_PENALTY * self.bit_wear)
        rate = (
            REFERENCE_ROP_FT_PER_HR
            * (self.wob_klbf / 20.0) ** WOB_EXPONENT
            * (self.rotary.rpm / 120.0) ** RPM_EXPONENT
            * self.hold_down_factor
            * wear_factor
            / max(0.1, self.rock_strength)
        )
        self.rop_ft_per_hr = max(0.0, rate)
        return self.rop_ft_per_hr

    def torque(self) -> float:
        """Rotary torque, kft-lb.

        Rises with weight and with speed, and a worn bit drags more. Not a
        friction model -- it is the shape a driller reads, and a torque that
        climbs while rate falls is what a dulling bit looks like.
        """
        if not self.rotary.turning:
            self.rotary.torque_kftlb = 0.0
            return 0.0
        value = (
            0.22 * self.wob_klbf * (1.0 + self.rotary.rpm / 240.0)
            * (1.0 + 0.8 * self.bit_wear)
        )
        self.rotary.torque_kftlb = value
        return value

    def advance(self, dt: float, *, on_bottom: bool) -> None:
        """Move the rotary, settle the weight, and wear the bit.

        Off bottom there is no weight to hold whatever the driller asked for,
        and the auto-driller has nothing to hold it against -- which is why
        picking up off bottom drops the weight to zero rather than leaving the
        last value on the gauge.
        """
        self.rotary.advance(dt)

        target = 0.0
        if on_bottom:
            target = (
                self.wob_setpoint_klbf if self.auto_driller
                else self.commanded_wob_klbf
            )
        step = self.feed_rate * dt
        if self.wob_klbf < target:
            self.wob_klbf = min(target, self.wob_klbf + step)
        elif self.wob_klbf > target:
            self.wob_klbf = max(target, self.wob_klbf - step)

        if self.rotary.turning and self.wob_klbf > 0.1:
            self.bit_wear = min(1.0, self.bit_wear + (
                WEAR_PER_KLBF_RPM_HOUR
                * self.wob_klbf * self.rotary.rpm * dt / 3600.0
            ))
        self.torque()

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict:
        return {
            "wob_klbf": round(self.wob_klbf, 1),
            "wob_commanded_klbf": round(self.commanded_wob_klbf, 1),
            "wob_setpoint_klbf": round(self.wob_setpoint_klbf, 1),
            "auto_driller": self.auto_driller,
            "rpm": round(self.rotary.rpm, 0),
            "rpm_commanded": round(self.rotary.commanded_rpm, 0),
            "torque_kftlb": round(self.rotary.torque_kftlb, 1),
            "rop_ft_per_hr": round(self.rop_ft_per_hr, 1),
            "bit_wear": round(self.bit_wear, 3),
            "hold_down_factor": round(self.hold_down_factor, 3),
        }
