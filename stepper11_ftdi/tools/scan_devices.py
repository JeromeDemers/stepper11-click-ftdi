"""List FTDI devices visible to either backend (D2XX or pyftdi/libusb).

Run:  python -m stepper11_ftdi.tools.scan_devices

The FT2232H shows up as two entries (channel A and channel B); channel B is
the one wired to the mikroBUS socket. At least one backend must see it:

- D2XX backend: works with the stock FTDI driver ("USB Serial Converter A/B").
- pyftdi backend: requires the WinUSB driver installed with Zadig
  (https://zadig.akeo.ie) on both "Dual RS232-HS" interfaces.
"""

from __future__ import annotations

import sys


def scan_d2xx() -> bool:
    try:
        import ftd2xx
    except (ImportError, OSError) as exc:
        print(f"  not available: {exc}")
        return False
    try:
        count = ftd2xx.createDeviceInfoList()
    except ftd2xx.DeviceError as exc:
        print(f"  error: {exc}")
        return False
    if not count:
        print("  no devices found")
        return False
    for index in range(count):
        info = ftd2xx.getDeviceInfoDetail(index, update=False)
        desc = (info.get("description") or b"?").decode(errors="replace")
        serial = (info.get("serial") or b"?").decode(errors="replace")
        print(f"  [{index}] {desc} (serial {serial})")
    return True


def scan_pyftdi() -> bool:
    try:
        from pyftdi.ftdi import Ftdi
    except ImportError as exc:
        print(f"  not available: {exc}")
        return False
    try:
        Ftdi.show_devices()
        return True
    except Exception as exc:  # noqa: BLE001 - backend/permission errors vary
        print(f"  error: {exc}")
        print("  (expected unless the WinUSB driver was installed with Zadig)")
        return False


def main() -> int:
    print("D2XX backend (stock FTDI driver):")
    d2xx_ok = scan_d2xx()
    print("pyftdi backend (libusb/WinUSB):")
    pyftdi_ok = scan_pyftdi()
    if not (d2xx_ok or pyftdi_ok):
        print("\nNo backend can see the FT2232H. Is the adapter plugged in?")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
