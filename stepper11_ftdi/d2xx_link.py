"""FT2232H access through FTDI's official D2XX driver (Windows default).

Same public API as :class:`stepper11_ftdi.ftdi_link.FtdiLink`, but implemented
with raw MPSSE commands over the `ftd2xx` package, so it works with the stock
FTDI driver ("USB Serial Converter A/B") - no Zadig/WinUSB replacement needed.

I2C is bit-banged by the MPSSE engine on channel B:

- BD0 = SCL, BD1 = SDA out, BD2 = SDA in (BD1/BD2 tied together on the board)
- BD3..BD7 are the low-byte GPIOs (bits 3..7)
- BC0..BC7 are the high-byte GPIOs (bits 8..15)
"""

from __future__ import annotations

import time
from typing import Optional

import ftd2xx
from ftd2xx import defines as ftdef

# MPSSE opcodes
_WRITE_BYTES_NVE = 0x11     # clock bytes out, MSB first, on -ve edge
_WRITE_BITS_NVE = 0x13      # clock bits out, MSB first, on -ve edge
_READ_BYTES_PVE = 0x20      # clock bytes in, MSB first, on +ve edge
_READ_BITS_PVE = 0x22       # clock bits in, MSB first, on +ve edge
_SET_BITS_LOW = 0x80
_READ_BITS_LOW = 0x81
_SET_BITS_HIGH = 0x82
_READ_BITS_HIGH = 0x83
_LOOPBACK_OFF = 0x85
_SET_DIVISOR = 0x86
_SEND_IMMEDIATE = 0x87
_DISABLE_CLK_DIV5 = 0x8A
_ENABLE_3PHASE = 0x8C
_DISABLE_ADAPTIVE = 0x97

# I2C line bits on the MPSSE low byte
_SCL = 0x01                 # BD0
_SDA_OUT = 0x02             # BD1
_I2C_BITS = _SCL | _SDA_OUT

#: mask applied to the byte returned by "clock 1 bit in" to test the slave ACK
_ACK_MASK = 0x01


class I2cNackError(IOError):
    """Slave did not acknowledge (wrong address, jumper not in I2C position...)."""


class D2xxI2cPort:
    """Register-oriented I2C port, mimicking the pyftdi I2cPort subset we use."""

    def __init__(self, link: "D2xxLink", address: int):
        self._link = link
        self._address = address

    def write_to(self, regaddr: int, out: bytes) -> None:
        self._link.i2c_write(self._address, bytes((regaddr,)) + bytes(out))

    def read_from(self, regaddr: int, readlen: int) -> bytes:
        return self._link.i2c_read(self._address, regaddr, readlen)


class D2xxLink:
    """FT2232H channel B via D2XX: I2C bus + GPIO + batched pulse trains."""

    def __init__(self, channel: str = "B", frequency: float = 100_000.0):
        self._dev = self._open_channel(channel)
        self._low_val = 0    # cached GPIO state, bits 3..7 (BD3..BD7)
        self._low_dir = 0
        self._high_val = 0   # cached GPIO state for BC0..BC7 (bits 8..15 >> 8)
        self._high_dir = 0
        self._cmd_rate: Optional[float] = None
        self._init_mpsse(frequency)

    # ------------------------------------------------------------- open/init

    @staticmethod
    def _open_channel(channel: str):
        count = ftd2xx.createDeviceInfoList()
        if count == 0:
            raise IOError("No FTDI device found (is the adapter plugged in?)")
        suffix = channel.upper().encode()
        for index in range(count):
            info = ftd2xx.getDeviceInfoDetail(index, update=False)
            desc = info.get("description") or b""
            serial = info.get("serial") or b""
            if desc.endswith(b" " + suffix) or serial.endswith(suffix):
                return ftd2xx.open(index)
        # Fallback: FT2232H enumerates its channels in order (A first, B second).
        if count >= 2:
            return ftd2xx.open(0 if suffix == b"A" else 1)
        raise IOError(
            f"FT2232H channel {channel} not found among {count} FTDI device(s)"
        )

    def _init_mpsse(self, frequency: float) -> None:
        dev = self._dev
        dev.resetDevice()
        dev.purge(ftdef.PURGE_RX | ftdef.PURGE_TX)
        dev.setUSBParameters(65536, 65536)
        dev.setChars(0, 0, 0, 0)
        dev.setTimeouts(1000, 1000)
        dev.setLatencyTimer(2)
        dev.setBitMode(0, 0x00)        # reset mode
        dev.setBitMode(0, 0x02)        # MPSSE
        time.sleep(0.05)

        self._sync_mpsse()

        # 60 MHz base clock, no adaptive clocking, 3-phase clocking for I2C.
        # SCL = 60 MHz / ((1 + divisor) * 3)
        divisor = max(0, round(60e6 / (3.0 * frequency)) - 1)
        setup = bytes((
            _DISABLE_CLK_DIV5,
            _DISABLE_ADAPTIVE,
            _ENABLE_3PHASE,
            _SET_DIVISOR, divisor & 0xFF, (divisor >> 8) & 0xFF,
            _LOOPBACK_OFF,
        ))
        dev.write(setup)
        self._write(self._cmd_set_low(_I2C_BITS, _I2C_BITS))  # bus idle: both high
        time.sleep(0.02)

    def _sync_mpsse(self) -> None:
        """Send a bogus opcode; the MPSSE must echo 0xFA + opcode."""
        self._dev.write(b"\xAA")
        deadline = time.perf_counter() + 1.0
        response = b""
        while len(response) < 2 and time.perf_counter() < deadline:
            pending = self._dev.getQueueStatus()
            if pending:
                response += self._dev.read(pending)
        if b"\xFA" not in response:
            raise IOError("MPSSE sync failed (unexpected response to bad opcode)")

    # ------------------------------------------------------------ raw helpers

    def _write(self, data: bytes) -> None:
        self._dev.write(bytes(data))

    def _read_exact(self, count: int, timeout_s: float = 2.0) -> bytes:
        deadline = time.perf_counter() + timeout_s
        buf = b""
        while len(buf) < count:
            pending = self._dev.getQueueStatus()
            if pending:
                buf += self._dev.read(min(pending, count - len(buf)))
            elif time.perf_counter() > deadline:
                raise IOError(
                    f"I2C/MPSSE read timeout ({len(buf)}/{count} bytes) - "
                    "check the adapter's UART/I2C jumper is in the I2C position"
                )
        return buf

    def _cmd_set_low(self, value: int, direction: int) -> bytes:
        """SET_BITS_LOW merging the I2C line state with the cached GPIO bits."""
        v = (value & _I2C_BITS) | (self._low_val & 0xF8)
        d = (direction & _I2C_BITS) | (self._low_dir & 0xF8)
        return bytes((_SET_BITS_LOW, v, d))

    def _cmd_set_high(self) -> bytes:
        return bytes((_SET_BITS_HIGH, self._high_val & 0xFF, self._high_dir & 0xFF))

    # ------------------------------------------------------- I2C primitives

    def _seq_start(self) -> bytes:
        buf = b""
        buf += self._cmd_set_low(_SCL | _SDA_OUT, _I2C_BITS) * 4  # idle high
        buf += self._cmd_set_low(_SCL, _I2C_BITS) * 4             # SDA falls
        buf += self._cmd_set_low(0, _I2C_BITS) * 4                # SCL falls
        return buf

    def _seq_stop(self) -> bytes:
        buf = b""
        buf += self._cmd_set_low(0, _I2C_BITS) * 4                # both low
        buf += self._cmd_set_low(_SCL, _I2C_BITS) * 4             # SCL rises
        buf += self._cmd_set_low(_SCL | _SDA_OUT, _I2C_BITS) * 4  # SDA rises
        return buf

    def _seq_write_byte(self, byte: int) -> bytes:
        """Clock one byte out, then clock the slave ACK bit in (1 read byte)."""
        buf = self._cmd_set_low(0, _I2C_BITS)                 # SCL low, SDA driven
        buf += bytes((_WRITE_BYTES_NVE, 0x00, 0x00, byte & 0xFF))
        buf += self._cmd_set_low(0, _SCL)                     # release SDA for ACK
        buf += bytes((_READ_BITS_PVE, 0x00))
        return buf

    def _seq_read_byte(self, ack: bool) -> bytes:
        """Clock one byte in (1 read byte), then drive the master ACK/NACK bit."""
        buf = self._cmd_set_low(0, _SCL)                      # SDA released
        buf += bytes((_READ_BYTES_PVE, 0x00, 0x00))
        buf += self._cmd_set_low(0, _I2C_BITS)                # SDA driven again
        buf += bytes((_WRITE_BITS_NVE, 0x00, 0x00 if ack else 0xFF))
        return buf

    def _run_transaction(self, commands: bytes, expected: int) -> bytes:
        self._write(commands + bytes((_SEND_IMMEDIATE,)))
        return self._read_exact(expected) if expected else b""

    @staticmethod
    def _check_acks(acks: bytes, context: str) -> None:
        for i, bit in enumerate(acks):
            if bit & _ACK_MASK:
                raise I2cNackError(f"{context}: NACK on byte {i}")

    # -------------------------------------------------------------- I2C API

    def i2c_write(self, address: int, payload: bytes) -> None:
        buf = self._seq_start()
        buf += self._seq_write_byte(address << 1)
        for byte in payload:
            buf += self._seq_write_byte(byte)
        buf += self._seq_stop()
        acks = self._run_transaction(buf, 1 + len(payload))
        self._check_acks(acks, f"I2C write to 0x{address:02X}")

    def i2c_read(self, address: int, regaddr: int, count: int) -> bytes:
        buf = self._seq_start()
        buf += self._seq_write_byte(address << 1)
        buf += self._seq_write_byte(regaddr)
        buf += self._seq_start()                              # repeated start
        buf += self._seq_write_byte((address << 1) | 1)
        for i in range(count):
            buf += self._seq_read_byte(ack=(i < count - 1))
        buf += self._seq_stop()
        raw = self._run_transaction(buf, 3 + count)
        self._check_acks(raw[:3], f"I2C read from 0x{address:02X}")
        return raw[3:]

    def get_i2c_port(self, address: int) -> D2xxI2cPort:
        return D2xxI2cPort(self, address)

    def sda_loop_ok(self) -> bool:
        """Check that SDA-out (BD1) and SDA-in (BD2) are tied together.

        MPSSE I2C needs both pins on the same SDA net; the boards do that
        through the UART/I2C selection jumper. If this returns False, the
        jumper is still in the UART position and no I2C device can answer.
        """
        try:
            self._write(bytes((_SET_BITS_LOW, 0x00, _SDA_OUT)))  # drive BD1 low
            time.sleep(0.002)
            self._write(bytes((_READ_BITS_LOW, _SEND_IMMEDIATE)))
            value = self._read_exact(1)[0]
            return not (value & 0x04)  # BD2 must follow BD1 low
        finally:
            self._write(self._cmd_set_low(_I2C_BITS, _I2C_BITS))  # restore idle

    def poll_i2c(self, address: int) -> bool:
        buf = self._seq_start() + self._seq_write_byte(address << 1) + self._seq_stop()
        ack = self._run_transaction(buf, 1)
        return not (ack[0] & _ACK_MASK)

    # ----------------------------------------------------------------- GPIO

    def setup_gpio(self, outputs: int, inputs: int = 0) -> None:
        if (outputs | inputs) & _I2C_BITS or (outputs | inputs) & 0x04:
            raise ValueError("GPIO bits 0..2 are reserved for the I2C bus")
        self._low_dir = outputs & 0xF8
        self._low_val = 0
        self._high_dir = (outputs >> 8) & 0xFF
        self._high_val = 0
        self._write(self._cmd_set_low(_I2C_BITS, _I2C_BITS) + self._cmd_set_high())

    def set_pin(self, pin: int, state: bool) -> None:
        if pin >= 8:
            bit = 1 << (pin - 8)
            self._high_val = (self._high_val | bit) if state else (self._high_val & ~bit)
            self._write(self._cmd_set_high())
        else:
            bit = 1 << pin
            self._low_val = (self._low_val | bit) if state else (self._low_val & ~bit)
            self._write(self._cmd_set_low(_I2C_BITS, _I2C_BITS))

    def read_pin(self, pin: int) -> bool:
        opcode = _READ_BITS_HIGH if pin >= 8 else _READ_BITS_LOW
        self._write(bytes((opcode, _SEND_IMMEDIATE)))
        value = self._read_exact(1)[0]
        return bool(value & (1 << (pin - 8 if pin >= 8 else pin)))

    # ---------------------------------------------------- batched pulse train

    def pulse_train(self, pin: int, count: int, frequency: float) -> None:
        """Emit `count` pulses on a BCBUS pin at ~`frequency` Hz (see FtdiLink)."""
        if not 8 <= pin <= 15:
            raise ValueError("pulse_train only supports BCBUS pins (bits 8..15)")
        if count <= 0:
            return
        if frequency <= 0:
            raise ValueError("frequency must be positive")

        if self._cmd_rate is None:
            self._cmd_rate = self._measure_command_rate()

        bit = 1 << (pin - 8)
        high = bytes((_SET_BITS_HIGH, (self._high_val | bit) & 0xFF, self._high_dir))
        low = bytes((_SET_BITS_HIGH, self._high_val & ~bit & 0xFF, self._high_dir))
        repeats = max(1, round(self._cmd_rate / (2.0 * frequency)))
        pulse = high * repeats + low * repeats

        pulses_per_chunk = max(1, (1 << 20) // len(pulse))
        sent = 0
        while sent < count:
            n = min(pulses_per_chunk, count - sent)
            self._write(pulse * n)
            sent += n
        # Sync: request a pin read; the response only arrives once the queue drained.
        self._write(bytes((_READ_BITS_HIGH, _SEND_IMMEDIATE)))
        self._read_exact(1, timeout_s=10.0 + count / frequency)

    def _measure_command_rate(self, sample: int = 65536) -> float:
        buf = self._cmd_set_high() * sample
        start = time.perf_counter()
        self._write(buf)
        self._write(bytes((_READ_BITS_HIGH, _SEND_IMMEDIATE)))
        self._read_exact(1, timeout_s=10.0)
        elapsed = time.perf_counter() - start
        return sample / max(elapsed, 1e-6)

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        try:
            self._dev.setBitMode(0, 0x00)
        finally:
            self._dev.close()

    def __enter__(self) -> "D2xxLink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
