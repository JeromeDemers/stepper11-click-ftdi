"""Minimal driver for the NXP PCA9538A 8-bit I2C port expander.

The Stepper 11 Click uses one at address 0x70 to drive the TB9120AFTG mode
pins (P0-P4) and to read its status flags (P5-P7). The class is generic and
reusable for any PCA9538/PCA9538A based board.
"""

from __future__ import annotations

from pyftdi.i2c import I2cPort


class PCA9538A:
    REG_INPUT = 0x00        # read-only, live pin levels
    REG_OUTPUT = 0x01       # output latch for pins configured as outputs
    REG_POLARITY = 0x02     # input polarity inversion
    REG_CONFIG = 0x03       # 1 = input, 0 = output (per bit)

    def __init__(self, port: I2cPort):
        self._port = port

    def read_register(self, reg: int) -> int:
        return self._port.read_from(reg, 1)[0]

    def write_register(self, reg: int, value: int) -> None:
        self._port.write_to(reg, bytes((value & 0xFF,)))

    def update_register(self, reg: int, mask: int, value: int) -> int:
        """Read-modify-write: replace the bits in `mask` with `value`."""
        current = self.read_register(reg)
        updated = (current & ~mask & 0xFF) | (value & mask)
        self.write_register(reg, updated)
        return updated

    def set_directions(self, config: int) -> None:
        """Set the CONFIG register: bit=1 makes the pin an input."""
        self.write_register(self.REG_CONFIG, config)

    def read_inputs(self) -> int:
        return self.read_register(self.REG_INPUT)
