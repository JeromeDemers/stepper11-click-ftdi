"""FT2232H access layer: one I2C bus plus GPIO pins on a single MPSSE channel.

The Click USB Adapter (MIKROE-1433) and the FTDI Click both expose the mikroBUS
socket on FT2232H *channel B*:

- BD0 = SCL, BD1+BD2 = SDA (with the board's UART/I2C jumper in the I2C position)
- BD3..BD7 (GPIO bits 3..7) and BC0..BC7 (GPIO bits 8..15) are free GPIOs

pyftdi's I2cController reserves the three lowest pins for I2C and hands out the
remaining 13 pins as a "wide" GPIO port; bit numbers used throughout this module
are absolute FT2232H channel-B bit positions (bit 3 = BD3, bit 8 = BC0, ...).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from pyftdi.i2c import I2cController, I2cPort

#: MPSSE opcode: set the high byte (BC0..BC7) of the MPSSE port.
_SET_BITS_HIGH = 0x82


@dataclass
class PinConfig:
    """GPIO bit assignments between the FT2232H and the Stepper 11 Click.

    Defaults are the mapping verified on the FTDI Click + Feather Click Shield
    rig (jumper wires A2->D7, A4->A1, A3->D8 across the Feather headers):
    the FTDI Click's BC0/BC1/BC2 pins are FT2232H *channel B* GPIO bits 8/9/10,
    while the I2C bus to the expander runs on channel A. Pass the channel-B
    link as `gpio_link` to Stepper11 in that setup.

    en_pin is None because EN is pulled up on the click board (driver always
    enabled); set it if you wire EN to a controllable pin.
    """

    dir_pin: int = 9            # BC1 -> wire A4->A1 -> mikroBUS AN  -> DIR
    rst_pin: int = 8            # BC0 -> wire A2->D7 -> mikroBUS RST -> RST
    clk_pin: int = 10           # BC2 -> wire A3->D8 -> mikroBUS PWM -> CLK
    int_pin: Optional[int] = None   # not wired on this rig
    en_pin: Optional[int] = None    # EN pulled high on the click board

    #: EN sits on the I2C link instead of the GPIO link. On the FTDI Click
    #: the mikroBUS CS pin (-> wire A5->D5 -> Stepper EN) is an FT2232H
    #: *channel A* pin, while CLK/DIR/RST are channel B - so with the EN wire
    #: installed, set en_pin (find_pins reports it) and this flag.
    en_on_i2c_link: bool = False

    #: Invert the DIR pin so that "clockwise" matches the real shaft motion.
    #: Verified True for the 17HS08-1004 wired B+/B-/A-/A+ on this rig
    #: (swap if your motor coils are connected differently).
    dir_inverted: bool = True

    def output_mask(self, include_en: bool = True) -> int:
        mask = (1 << self.dir_pin) | (1 << self.rst_pin) | (1 << self.clk_pin)
        if include_en and self.en_pin is not None:
            mask |= 1 << self.en_pin
        return mask

    def input_mask(self) -> int:
        return 0 if self.int_pin is None else (1 << self.int_pin)


class FtdiLink:
    """One FT2232H MPSSE channel shared between an I2C bus and GPIO pins.

    The same I2C bus can host other devices later (ADS7128 shunt ADC,
    Counter Click...), so keep a single FtdiLink per channel and create
    one I2C port per peripheral with :meth:`get_i2c_port`.
    """

    def __init__(self, url: str = "ftdi://ftdi:2232h/2", frequency: float = 100_000.0):
        """Open the MPSSE channel. Default URL is FT2232H channel B."""
        self._i2c = I2cController()
        self._i2c.configure(url, frequency=frequency)
        self._gpio = self._i2c.get_gpio()
        self._gpio_value = 0        # cached state of the output pins
        self._cmd_rate: Optional[float] = None  # measured MPSSE commands/second

    # ------------------------------------------------------------------ I2C

    def get_i2c_port(self, address: int) -> I2cPort:
        """Return an I2C port bound to a 7-bit slave address."""
        return self._i2c.get_port(address)

    def poll_i2c(self, address: int) -> bool:
        """Return True if a device ACKs at the given address."""
        return self._i2c.poll(address)

    # ----------------------------------------------------------------- GPIO

    def setup_gpio(self, outputs: int, inputs: int = 0) -> None:
        """Configure GPIO directions. Masks use absolute bit positions (3..15)."""
        self._gpio.set_direction(outputs | inputs, outputs)
        self._gpio_value = 0
        self._gpio.write(0)

    def set_pin(self, pin: int, state: bool) -> None:
        """Drive one output pin high or low."""
        if state:
            self._gpio_value |= 1 << pin
        else:
            self._gpio_value &= ~(1 << pin)
        self._gpio.write(self._gpio_value)

    def read_pin(self, pin: int) -> bool:
        """Read the live state of one input pin."""
        return bool(self._gpio.read(with_output=True) & (1 << pin))

    # ---------------------------------------------------- batched pulse train

    def pulse_train(self, pin: int, count: int, frequency: float) -> None:
        """Emit `count` clean pulses on a BCBUS pin at ~`frequency` Hz.

        Instead of one USB transfer per edge, this streams raw MPSSE
        "set high byte" commands. The chip executes queued commands
        back-to-back at a rate measured once per session, so the pulse
        frequency is set by repeating each level `repeats` times.
        Only works for pins on the high byte (bits 8..15), where the step
        clock lives on both supported adapter boards.
        """
        if not 8 <= pin <= 15:
            raise ValueError("pulse_train only supports BCBUS pins (bits 8..15)")
        if count <= 0:
            return
        if frequency <= 0:
            raise ValueError("frequency must be positive")

        if self._cmd_rate is None:
            self._cmd_rate = self._measure_command_rate()

        bit = 1 << (pin - 8)
        value = (self._gpio_value >> 8) & 0xFF
        direction = (self._gpio.direction >> 8) & 0xFF

        repeats = max(1, round(self._cmd_rate / (2.0 * frequency)))
        high = bytes((_SET_BITS_HIGH, value | bit, direction)) * repeats
        low = bytes((_SET_BITS_HIGH, value & ~bit & 0xFF, direction)) * repeats
        pulse = high + low

        # Stream in chunks to bound memory; ~1 MiB max per buffer.
        pulses_per_chunk = max(1, (1 << 20) // len(pulse))
        sent = 0
        while sent < count:
            n = min(pulses_per_chunk, count - sent)
            self._i2c.ftdi.write_data(pulse * n)
            sent += n
        # Round-trip read to block until every queued command has executed,
        # and to leave pyftdi's view of the port consistent (pulse ends low).
        self._gpio.read(with_output=True)

    def _measure_command_rate(self, sample: int = 65536) -> float:
        """Measure how many MPSSE set-bit commands per second the link executes."""
        value = (self._gpio_value >> 8) & 0xFF
        direction = (self._gpio.direction >> 8) & 0xFF
        # Same command cost as a real toggle, but keeps the pin levels unchanged.
        buf = bytes((_SET_BITS_HIGH, value, direction)) * sample
        start = time.perf_counter()
        self._i2c.ftdi.write_data(buf)
        self._gpio.read(with_output=True)  # sync: returns after the queue drains
        elapsed = time.perf_counter() - start
        return sample / max(elapsed, 1e-6)

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._i2c.close()

    def __enter__(self) -> "FtdiLink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
