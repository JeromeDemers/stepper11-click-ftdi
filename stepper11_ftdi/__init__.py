"""Stepper 11 Click control through an FT2232H USB bridge (no microcontroller)."""

from .ftdi_link import PinConfig
from .pca9538a import PCA9538A
from .stepper11 import Stepper11

__all__ = ["FtdiLink", "D2xxLink", "PinConfig", "PCA9538A", "Stepper11",
           "open_link", "open_links"]
__version__ = "0.2.0"


def __getattr__(name):
    # Lazy imports: FtdiLink needs pyftdi/libusb, D2xxLink needs ftd2xx.
    if name == "FtdiLink":
        from .ftdi_link import FtdiLink
        return FtdiLink
    if name == "D2xxLink":
        from .d2xx_link import D2xxLink
        return D2xxLink
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def open_link(url: str = "ftdi://ftdi:2232h/1", frequency: float = 100_000.0,
              channel: str = None):
    """Open the FT2232H with whichever backend works on this machine.

    Tries the FTDI D2XX driver first (the Windows default), then falls back
    to pyftdi/libusb (requires the WinUSB driver installed with Zadig).

    With D2XX, both channels are probed and the one with live I2C devices is
    preferred (the mikroBUS socket sits on channel A on some adapter boards,
    channel B on others). Pass channel="A" or "B" to skip the probing.
    """
    errors = []
    try:
        from .d2xx_link import D2xxLink
        channels = (channel,) if channel else ("A", "B")
        fallback = None
        for name in channels:
            try:
                link = D2xxLink(name, frequency)
            except Exception as exc:  # noqa: BLE001 - channel busy/absent
                errors.append(f"d2xx channel {name}: {exc}")
                continue
            if channel is not None:
                return link
            try:
                has_devices = any(link.poll_i2c(a) for a in range(0x08, 0x78))
            except Exception:  # noqa: BLE001 - unusable bus on this channel
                link.close()
                continue
            if has_devices:
                if fallback is not None:
                    fallback.close()
                return link
            if fallback is None:
                fallback = link  # keep the first channel that opens
            else:
                link.close()
        if fallback is not None:
            return fallback
    except ImportError as exc:
        errors.append(f"d2xx backend: {exc}")
    try:
        from .ftdi_link import FtdiLink
        return FtdiLink(url, frequency)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pyftdi backend: {exc}")
    raise IOError("Could not open the FT2232H with any backend:\n  " + "\n  ".join(errors))


def open_links(frequency: float = 100_000.0):
    """Open both FT2232H channels: returns (i2c_link, gpio_link).

    On the verified rig the I2C bus (PCA9538A) is on channel A and the
    CLK/DIR/RST GPIOs are on channel B. Works with either driver: FTDI's
    stock D2XX driver (Windows default) or WinUSB/pyftdi (Zadig).
    """
    errors = []
    try:
        from .d2xx_link import D2xxLink
        i2c = D2xxLink("A", frequency)
        try:
            gpio = D2xxLink("B", frequency)
        except Exception:
            i2c.close()
            raise
        return i2c, gpio
    except Exception as exc:  # noqa: BLE001 - try the other backend
        errors.append(f"d2xx backend: {exc}")
    try:
        from .ftdi_link import FtdiLink
        gpio = FtdiLink("ftdi://ftdi:2232h/2", frequency)
        try:
            i2c = FtdiLink("ftdi://ftdi:2232h/1", frequency)
        except Exception:
            gpio.close()
            raise
        return i2c, gpio
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pyftdi backend: {exc}")
    raise IOError("Could not open the FT2232H with any backend:\n  " + "\n  ".join(errors))
