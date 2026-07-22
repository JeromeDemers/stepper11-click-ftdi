"""High-level driver for the MikroE Stepper 11 Click (TB9120AFTG + PCA9538A).

Mirrors the MikroE mikroSDK reference driver, but runs on a PC through an
FT2232H USB bridge (see :mod:`stepper11_ftdi.ftdi_link`).

Register layout on the PCA9538A expander (address 0x70):

- P0..P2 (out): DMODE step resolution pins of the TB9120AFTG
- P3..P4 (out): TORQUE scaling pins (100/70/50/30 %)
- P5 (in): DIAG - anomaly detected (open load, overtemp, overcurrent)
- P6 (in): MO - electrical angle at initial position
- P7 (in): SD - stall (step-out) detected

Note: the absolute phase current is set by the VR1 trimmer on the board;
software only scales it through the torque bits.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from .ftdi_link import FtdiLink, PinConfig
from .pca9538a import PCA9538A

#: name -> (DMODE bits for P0..P2, microsteps per full step)
RESOLUTIONS: Dict[str, tuple] = {
    "full": (0x4, 1),
    "half": (0x2, 2),      # half step A (fixed current)
    "half_b": (0x1, 2),    # half step B
    "1/4": (0x6, 4),
    "1/8": (0x5, 8),
    "1/16": (0x3, 16),
    "1/32": (0x7, 32),
}

#: torque percent -> bits for P3..P4
TORQUES: Dict[int, int] = {100: 0x0, 70: 0x1, 50: 0x2, 30: 0x3}

_RESOLUTION_MASK = 0x07
_TORQUE_MASK = 0x18
_EXPANDER_CONFIG = 0xE0  # P7..P5 inputs (SD, MO, DIAG), P4..P0 outputs


class Stepper11:
    """Stepper 11 Click: signed step moves, speed, resolution, torque, diagnostics."""

    I2C_ADDRESS = 0x70

    #: pulse rate (Hz) above which the batched MPSSE mode is used
    BATCH_PULSE_RATE = 400.0

    #: ramp start speed (full steps/s) when move_steps is called with accel
    START_RATE = 50.0

    def __init__(
        self,
        link: FtdiLink,
        address: int = I2C_ADDRESS,
        pins: Optional[PinConfig] = None,
        steps_per_rev: int = 200,
        auto_config: bool = True,
        gpio_link: Optional[FtdiLink] = None,
    ):
        """:param gpio_link: link carrying the CLK/DIR/EN/RST pins, when they
        live on a different FT2232H channel than the I2C bus (e.g. FTDI Click
        on a Feather shield: I2C on channel A, BC0-BC2 GPIOs on channel B).
        Defaults to the same link as the I2C bus."""
        self._i2c_link = link
        self._link = gpio_link or link
        self._pins = pins or PinConfig()
        self._expander = PCA9538A(link.get_i2c_port(address))
        self._steps_per_rev = steps_per_rev
        self._microsteps = 1
        self._resolution = "full"

        pins_cfg = self._pins
        self._en_link = self._i2c_link if pins_cfg.en_on_i2c_link else self._link
        if self._en_link is not self._link and pins_cfg.en_pin is not None:
            self._link.setup_gpio(pins_cfg.output_mask(include_en=False),
                                  pins_cfg.input_mask())
            self._en_link.setup_gpio(1 << pins_cfg.en_pin)
        else:
            self._link.setup_gpio(pins_cfg.output_mask(), pins_cfg.input_mask())

        if auto_config:
            self.default_config()

    # -------------------------------------------------------- configuration

    def default_config(self) -> None:
        """Same defaults as the MikroE reference driver."""
        self._expander.set_directions(_EXPANDER_CONFIG)
        self._link.set_pin(self._pins.rst_pin, False)
        self.enable()
        self.set_resolution("full")

    def set_resolution(self, resolution: str) -> None:
        """Set microstep resolution: full, half, half_b, 1/4, 1/8, 1/16, 1/32."""
        try:
            bits, factor = RESOLUTIONS[resolution]
        except KeyError:
            raise ValueError(
                f"unknown resolution {resolution!r}, expected one of {list(RESOLUTIONS)}"
            ) from None
        self._expander.update_register(PCA9538A.REG_OUTPUT, _RESOLUTION_MASK, bits)
        self._microsteps = factor
        self._resolution = resolution

    def set_torque(self, percent: int) -> None:
        """Scale the VR1-trimmed phase current: 100, 70, 50 or 30 (%)."""
        try:
            bits = TORQUES[int(percent)]
        except (KeyError, ValueError):
            raise ValueError(
                f"unknown torque {percent!r}, expected one of {list(TORQUES)}"
            ) from None
        self._expander.update_register(PCA9538A.REG_OUTPUT, _TORQUE_MASK, bits << 3)

    @property
    def resolution(self) -> str:
        return self._resolution

    @property
    def microsteps(self) -> int:
        """Microstep pulses per full motor step at the current resolution."""
        return self._microsteps

    # ------------------------------------------------------------- GPIO ops

    def enable(self) -> None:
        """Power up the driver stage (motor gets holding torque)."""
        if self._pins.en_pin is not None:
            self._en_link.set_pin(self._pins.en_pin, True)

    def disable(self) -> None:
        """Power down the driver stage (motor free-wheels)."""
        if self._pins.en_pin is not None:
            self._en_link.set_pin(self._pins.en_pin, False)

    def set_direction(self, clockwise: bool) -> None:
        """Set rotation direction; polarity corrected via PinConfig.dir_inverted."""
        level = bool(clockwise) ^ self._pins.dir_inverted
        self._link.set_pin(self._pins.dir_pin, level)

    def reset_angle(self, pulse_s: float = 0.01) -> None:
        """Pulse RST to re-initialize the TB9120 electrical angle counter."""
        self._link.set_pin(self._pins.rst_pin, True)
        time.sleep(pulse_s)
        self._link.set_pin(self._pins.rst_pin, False)

    def read_int(self) -> Optional[bool]:
        """State of the mikroBUS INT line, or None if not wired."""
        if self._pins.int_pin is None:
            return None
        return self._link.read_pin(self._pins.int_pin)

    # ------------------------------------------------------------ diagnostics

    def diagnostics(self) -> Dict[str, bool]:
        """Read the TB9120 status flags through the expander.

        - diag: anomaly detected (open load / overtemperature / overcurrent)
        - mo:   electrical angle at initial position
        - sd:   stall (step-out) detected

        Empirically verified on hardware (flag_monitor tool): the flags are
        ACTIVE-LOW (open-drain, LED lit = pin low = flag asserted), and MO is
        on expander bit P7 - it toggles with an exact 4-full-step period
        while stepping and matches the blue LED. This differs from the MikroE
        reference driver, which maps P6=MO / P7=SD. DIAG stays P5 per MikroE;
        SD is P6 by elimination.
        """
        inputs = self._expander.read_inputs()
        return {
            "diag": not (inputs & 0x20),
            "sd": not (inputs & 0x40),
            "mo": not (inputs & 0x80),
        }

    # ---------------------------------------------------------------- motion

    def move_steps(self, steps: int, speed: float = 200.0,
                   accel: Optional[float] = None) -> None:
        """Move a signed number of *full* motor steps.

        :param steps: positive = DIR high (CW), negative = DIR low (CCW).
        :param speed: full steps per second. The pulse rate sent to the CLK
                      pin is speed * microsteps for the current resolution.
        :param accel: full steps/s^2 for a trapezoidal speed profile: start
                      at START_RATE, ramp up to `speed`, cruise, ramp down.
                      None or 0 = constant speed from the first pulse (the
                      motor may stall when starting above ~100 steps/s).
        """
        steps = int(steps)
        if steps == 0:
            return
        if speed <= 0:
            raise ValueError("speed must be positive (full steps per second)")

        self.set_direction(steps > 0)
        pulses = abs(steps) * self._microsteps
        target = speed * self._microsteps

        if accel:
            v_start = min(target, self.START_RATE * self._microsteps)
            segments = self._profile_segments(
                pulses, v_start, target, accel * self._microsteps)
        else:
            segments = [(pulses, target)]

        for count, rate in segments:
            self._emit_pulses(count, rate)

    def move_degrees(self, degrees: float, speed: float = 200.0,
                     accel: Optional[float] = None) -> None:
        """Move a signed angle in degrees (uses steps_per_rev, 1.8 deg/step)."""
        steps = round(degrees * self._steps_per_rev / 360.0)
        self.move_steps(steps, speed, accel)

    @staticmethod
    def _profile_segments(pulses: int, v_start: float, v_target: float,
                          accel: float) -> list:
        """Approximate a trapezoidal profile as (pulse_count, rate) segments.

        All quantities in pulse units (pulses, pulses/s, pulses/s^2).
        """
        if v_target <= v_start or accel <= 0:
            return [(pulses, v_target)]

        # Pulses needed to reach the target: d = (v^2 - v0^2) / (2a)
        d_accel = int((v_target ** 2 - v_start ** 2) / (2.0 * accel)) + 1
        d_accel = min(d_accel, pulses // 2)
        if d_accel < 1:
            return [(pulses, v_target)]

        n_slices = min(12, d_accel)
        per_slice = d_accel // n_slices
        ramp_up = []
        done = 0
        while done < d_accel:
            n = min(per_slice, d_accel - done) or (d_accel - done)
            # Velocity at the middle of this slice.
            v_mid = (v_start ** 2 + 2.0 * accel * (done + n / 2.0)) ** 0.5
            ramp_up.append((n, min(v_mid, v_target)))
            done += n

        segments = list(ramp_up)
        cruise = pulses - 2 * d_accel
        if cruise > 0:
            segments.append((cruise, v_target))
        segments.extend(reversed(ramp_up))
        return segments

    def _emit_pulses(self, pulses: int, rate: float) -> None:
        if pulses <= 0:
            return
        if rate >= self.BATCH_PULSE_RATE and self._pins.clk_pin >= 8:
            self._link.pulse_train(self._pins.clk_pin, pulses, rate)
        else:
            self._move_paced(pulses, rate)

    def _move_paced(self, pulses: int, pulse_rate: float) -> None:
        """One USB write per edge, paced against a monotonic clock."""
        clk = self._pins.clk_pin
        half_period = 0.5 / pulse_rate
        next_edge = time.perf_counter()
        for _ in range(pulses):
            self._link.set_pin(clk, True)
            next_edge += half_period
            self._sleep_until(next_edge)
            self._link.set_pin(clk, False)
            next_edge += half_period
            self._sleep_until(next_edge)

    @staticmethod
    def _sleep_until(deadline: float) -> None:
        remaining = deadline - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)
