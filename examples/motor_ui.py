"""Control panel for the MikroE Stepper 11 Click.

All motor I/O is done on a background worker thread so the UI stays responsive, and a move can be stopped mid-way.

Run:  python examples/motor_ui.py
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stepper11_ftdi import Stepper11  # noqa: E402
from stepper11_ftdi.stepper11 import RESOLUTIONS  # noqa: E402

# ------------------------------------------------------------------ palette

BG = "#000000"
CARD = "#1C1C1E"
FIELD = "#2C2C2E"
SEG_SEL = "#636366"
TRACK = "#39393D"
TEXT = "#FFFFFF"
SUBTLE = "#98989E"
BLUE = "#0A84FF"
GREEN = "#30D158"
RED = "#FF453A"
ORANGE = "#FF9F0A"

FONT = "Segoe UI"

CONTENT_W = 336          # inner width of every card


def rrect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle via a smoothed polygon."""
    pts = (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)
    return canvas.create_polygon(pts, smooth=True, splinesteps=36, **kw)


def pill(canvas: tk.Canvas, x1, y1, x2, y2, fill):
    """True pill shape: two semicircular caps + a rectangle body."""
    d = y2 - y1
    canvas.create_oval(x1, y1, x1 + d, y2, fill=fill, outline=fill)
    canvas.create_oval(x2 - d, y1, x2, y2, fill=fill, outline=fill)
    canvas.create_rectangle(x1 + d / 2, y1, x2 - d / 2, y2,
                            fill=fill, outline=fill)


# ------------------------------------------------------------ iOS widgets

class Segmented(tk.Canvas):
    """iOS segmented control."""

    def __init__(self, parent, options, initial=0, command=None,
                 width=CONTENT_W, height=32):
        super().__init__(parent, width=width, height=height, bg=CARD,
                         highlightthickness=0, bd=0)
        self._opts = list(options)
        self._on_change = command
        self._pw, self._ph = width, height
        self._index = initial
        self.bind("<Button-1>", self._on_click)
        self._draw()

    @property
    def value(self):
        return self._opts[self._index]

    def select(self, index: int) -> None:
        self._index = max(0, min(index, len(self._opts) - 1))
        self._draw()

    def _on_click(self, event) -> None:
        i = int(event.x / (self._pw / len(self._opts)))
        if i != self._index:
            self.select(i)
            if self._on_change:
                self._on_change(self.value)

    def _draw(self) -> None:
        self.delete("all")
        rrect(self, 0, 0, self._pw, self._ph, 9, fill=FIELD, outline="")
        seg_w = self._pw / len(self._opts)
        x = self._index * seg_w
        rrect(self, x + 2, 2, x + seg_w - 2, self._ph - 2, 8,
              fill=SEG_SEL, outline="")
        for i, opt in enumerate(self._opts):
            color = TEXT if i == self._index else SUBTLE
            self.create_text((i + 0.5) * seg_w, self._ph / 2, text=str(opt),
                             fill=color, font=(FONT, 10, "bold"))


class Switch(tk.Canvas):
    """iOS toggle switch."""

    def __init__(self, parent, initial=False, command=None):
        super().__init__(parent, width=51, height=31, bg=CARD,
                         highlightthickness=0, bd=0)
        self.state = initial
        self._command = command
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def _toggle(self, _event=None) -> None:
        self.state = not self.state
        self._draw()
        if self._command:
            self._command(self.state)

    def _draw(self) -> None:
        self.delete("all")
        color = GREEN if self.state else TRACK
        pill(self, 0, 0, 51, 31, color)
        # 25 px knob travels between 3 and 51-25-3 = 23 (stays inside).
        x = 23 if self.state else 3
        self.create_oval(x, 3, x + 25, 28, fill=TEXT, outline=TEXT)


class Slider(tk.Canvas):
    """iOS slider with live value callback."""

    def __init__(self, parent, lo, hi, initial, command=None,
                 width=CONTENT_W, step=1):
        super().__init__(parent, width=width, height=28, bg=CARD,
                         highlightthickness=0, bd=0)
        self._lo, self._hi, self._step = lo, hi, step
        self._pw = width
        self._command = command
        self.value = initial
        self.bind("<Button-1>", self._on_drag)
        self.bind("<B1-Motion>", self._on_drag)
        self._draw()

    def _on_drag(self, event) -> None:
        frac = min(1.0, max(0.0, (event.x - 14) / (self._pw - 28)))
        raw = self._lo + frac * (self._hi - self._lo)
        self.value = int(round(raw / self._step) * self._step)
        self._draw()
        if self._command:
            self._command(self.value)

    def _draw(self) -> None:
        self.delete("all")
        y = 14
        frac = (self.value - self._lo) / (self._hi - self._lo)
        knob_x = 14 + frac * (self._pw - 28)
        pill(self, 12, y - 2, self._pw - 12, y + 2, TRACK)
        if knob_x > 17:
            pill(self, 12, y - 2, knob_x, y + 2, BLUE)
        self.create_oval(knob_x - 12, y - 12, knob_x + 12, y + 12,
                         fill=TEXT, outline=TEXT)


class PillButton(tk.Canvas):
    """Large rounded action button."""

    def __init__(self, parent, text, color=BLUE, command=None,
                 width=CONTENT_W, height=48, font_size=15):
        super().__init__(parent, width=width, height=height, bg=CARD,
                         highlightthickness=0, bd=0)
        self._pw, self._ph = width, height
        self._color, self._text = color, text
        self._font_size = font_size
        self._command = command
        self._enabled = True
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def config_pill(self, text=None, color=None, enabled=None) -> None:
        if text is not None:
            self._text = text
        if color is not None:
            self._color = color
        if enabled is not None:
            self._enabled = enabled
        self._draw()

    def _on_click(self, _event) -> None:
        if self._enabled and self._command:
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        fill = self._color if self._enabled else TRACK
        text_color = TEXT if self._enabled else SUBTLE
        rrect(self, 0, 0, self._pw, self._ph, self._ph // 2 - 4,
              fill=fill, outline="")
        self.create_text(self._pw / 2, self._ph / 2, text=self._text,
                         fill=text_color, font=(FONT, self._font_size, "bold"))


class FlagPill(tk.Canvas):
    """Small status pill: gray when clear, colored when set."""

    def __init__(self, parent, label, color, width=104):
        super().__init__(parent, width=width, height=30, bg=CARD,
                         highlightthickness=0, bd=0)
        self._label, self._color, self._pw = label, color, width
        self.set(None)

    def set(self, state) -> None:
        self.delete("all")
        fill = self._color if state else FIELD
        text = TEXT if state else SUBTLE
        pill(self, 0, 0, self._pw, 30, fill)
        self.create_text(self._pw / 2, 15, text=self._label, fill=text,
                         font=(FONT, 10, "bold"))


# ------------------------------------------------------------------- cards

CARD_W = 368


class Tooltip:
    """Dark hover tooltip, shown after a short delay below the widget."""

    def __init__(self, widget, text: str, delay_ms: int = 350):
        self._widget = widget
        self._text = text
        self._delay = delay_ms
        self._tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button-1>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self._widget.after(self._delay, self._show)

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self._widget.winfo_rootx()
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        tip = tk.Toplevel(self._widget)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        tip.configure(bg="#3A3A3C")
        tk.Label(tip, text=self._text, bg="#3A3A3C", fg=TEXT,
                 font=(FONT, 9), justify="left", wraplength=280,
                 padx=10, pady=8).pack()
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _cancel(self) -> None:
        if self._after is not None:
            self._widget.after_cancel(self._after)
            self._after = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def make_card(parent, title=None):
    """Rounded card: canvas background + embedded content frame.

    The layout is finalized once, after all content widgets have been added
    (deferred with after_idle), because the window is fixed-width.
    """
    canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0,
                       width=CARD_W, height=10)
    canvas.pack(padx=16, pady=(0, 12))
    inner = tk.Frame(canvas, bg=CARD)

    def finalize():
        inner.update_idletasks()
        height = inner.winfo_reqheight() + 24
        canvas.configure(height=height)
        canvas.delete("all")
        rrect(canvas, 0, 0, CARD_W, height, 14, fill=CARD, outline="")
        canvas.create_window(12, 12, window=inner, anchor="nw")

    canvas.after_idle(finalize)
    inner.refresh_card = finalize      # call after adding/removing content

    if title:
        tk.Label(inner, text=title.upper(), bg=CARD, fg=SUBTLE,
                 font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 6))
    return inner


def row(parent, label_text, widget, sub=None):
    """Label on the left, widget on the right."""
    r = tk.Frame(parent, bg=CARD)
    r.pack(fill="x", pady=4)
    box = tk.Frame(r, bg=CARD)
    box.pack(side="left")
    tk.Label(box, text=label_text, bg=CARD, fg=TEXT,
             font=(FONT, 12)).pack(anchor="w")
    if sub:
        tk.Label(box, text=sub, bg=CARD, fg=SUBTLE,
                 font=(FONT, 9)).pack(anchor="w")
    widget.pack(in_=r, side="right")
    # The widget was created before this row frame, so it sits below it in
    # the stacking order and would be painted over (invisible) without this.
    # (tk.Misc.lift, because Canvas.lift means "raise canvas item" instead.)
    tk.Misc.lift(widget)
    return r


# --------------------------------------------------------------------- app

RES_LABELS = {"full": "Full", "half": "1/2", "1/4": "1/4", "1/8": "1/8",
              "1/16": "1/16", "1/32": "1/32"}
RES_FROM_LABEL = {v: k for k, v in RES_LABELS.items()}


class MotorApp:
    I2C_URL = "ftdi://ftdi:2232h/1"     # channel A: I2C to the expander
    GPIO_URL = "ftdi://ftdi:2232h/2"    # channel B: CLK/DIR/RST

    def __init__(self, root: tk.Tk):
        self.root = root
        self.jobs: "queue.Queue" = queue.Queue()
        self.stop_flag = threading.Event()

        self.motor = None
        self._links = []
        self.connected = False
        self.moving = False
        self.cycle = 0                  # loop cycle counter (0 = not looping)
        self.progress = None            # (done, total)
        self.flags = None               # diagnostics dict
        self.error = None
        self._last_refresh = 0.0
        self._connect_pending = True    # first attempt queued below
        self._last_connect = 0.0
        self._applied_torque = None     # last torque written to the driver

        self._build_ui()
        threading.Thread(target=self._worker, daemon=True).start()
        self.jobs.put(self._job_connect)
        self.root.after(120, self._tick)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = self.root
        root.title("Stepper 11 Click")
        root.configure(bg=BG)
        root.resizable(False, False)
        # Height is set by _fit_window() once the cards have laid out;
        # never hardcode it (cards kept getting cut off at the bottom).
        root.after(80, self._fit_window)

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=20, pady=(14, 10))
        tk.Label(header, text="Stepper 11 Click", bg=BG, fg=TEXT,
                 font=(FONT, 22, "bold")).pack(side="left")
        self.status_lbl = tk.Label(header, text="● connecting…", bg=BG,
                                   fg=ORANGE, font=(FONT, 10, "bold"))
        self.status_lbl.pack(side="right", pady=6)

        # -- Motion ------------------------------------------------------
        c = make_card(root, "Motion")

        steps_row = tk.Frame(c, bg=CARD)
        steps_row.pack(fill="x", pady=2)
        tk.Label(steps_row, text="Steps", bg=CARD, fg=TEXT,
                 font=(FONT, 12)).pack(side="left")
        self.steps_var = tk.StringVar(value="500")
        tk.Entry(steps_row, textvariable=self.steps_var, width=7,
                 bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                 justify="center", font=(FONT, 14, "bold")
                 ).pack(side="right", ipady=4)

        chips = tk.Frame(c, bg=CARD)
        chips.pack(fill="x", pady=(2, 6))
        for n in (50, 200, 500, 1000):
            b = PillButton(chips, str(n), color=FIELD, width=76, height=26,
                           font_size=10,
                           command=lambda v=n: self.steps_var.set(str(v)))
            b.pack(side="left", padx=(0, 8))

        self.direction = Segmented(c, ["⟲  CCW", "CW  ⟳"], initial=1)
        self.direction.pack(pady=(2, 8))

        self.go_btn = PillButton(c, "GO", color=BLUE, command=self._on_go)
        self.go_btn.pack(pady=(2, 4))
        self.progress_lbl = tk.Label(c, text=" ", bg=CARD, fg=SUBTLE,
                                     font=(FONT, 10))
        self.progress_lbl.pack()

        jog = tk.Frame(c, bg=CARD)
        jog.pack(pady=(2, 2))
        PillButton(jog, "−10", color=FIELD, width=120, height=34, font_size=12,
                   command=lambda: self._jog(-10)).pack(side="left", padx=8)
        PillButton(jog, "+10", color=FIELD, width=120, height=34, font_size=12,
                   command=lambda: self._jog(10)).pack(side="left", padx=8)

        self.loop_switch = Switch(c, initial=False)
        row(c, "Loop back & forth", self.loop_switch,
            sub="repeat the move in both directions until STOP")
        Tooltip(self.loop_switch,
                "When on, GO alternates the move: +steps, short pause, "
                "-steps, pause, ... until you press STOP. The direction "
                "selector picks which way each cycle starts.\n\n"
                "While the motor runs (loop or single move), speed, "
                "resolution, torque and direction changes apply live - "
                "unless the acceleration ramp is on.")

        # -- Speed -------------------------------------------------------
        c = make_card(root, "Speed")
        self.speed_lbl = tk.Label(c, bg=CARD, fg=TEXT, font=(FONT, 12, "bold"))
        self.speed_slider = Slider(c, 10, 300, 100,
                                   command=lambda v: self._speed_text(v))
        row(c, "Speed", self.speed_lbl,
            sub="full steps per second - live while moving")
        self.speed_slider.pack(pady=(0, 4))
        self._speed_text(self.speed_slider.value)

        self.ramp_switch = Switch(c, initial=False,
                                  command=lambda s: self._ramp_vis(s))
        row(c, "Acceleration ramp", self.ramp_switch,
            sub="trapezoidal profile, avoids stalls")
        self.accel_lbl = tk.Label(c, bg=CARD, fg=TEXT, font=(FONT, 12, "bold"))
        self.accel_slider = Slider(c, 50, 600, 200, step=25,
                                   command=lambda v: self._accel_text(v))
        self.accel_row = row(c, "Ramp rate", self.accel_lbl, sub="steps/s²")
        self.accel_slider.pack(pady=(0, 2))
        self._accel_text(self.accel_slider.value)
        self._ramp_vis(False)

        # -- Motor setup ---------------------------------------------------
        c = make_card(root, "Motor setup")
        tk.Label(c, text="Microstep resolution", bg=CARD, fg=TEXT,
                 font=(FONT, 12)).pack(anchor="w")
        self.res_seg = Segmented(c, [RES_LABELS[r] for r in
                                     ("full", "half", "1/4", "1/8",
                                      "1/16", "1/32")], initial=3)
        self.res_seg.pack(pady=(4, 8))
        tk.Label(c, text="Torque (current scale)", bg=CARD, fg=TEXT,
                 font=(FONT, 12)).pack(anchor="w")
        self.torque_seg = Segmented(c, ["100%", "70%", "50%", "30%"])
        self.torque_seg.pack(pady=(4, 2))

        # -- Status ---------------------------------------------------------
        c = make_card(root, "Driver status")
        pills = tk.Frame(c, bg=CARD)
        pills.pack(pady=(0, 4))
        self.pill_diag = FlagPill(pills, "DIAG", ORANGE)
        self.pill_mo = FlagPill(pills, "MO", BLUE)
        self.pill_sd = FlagPill(pills, "SD  stall", RED)
        for i, p in enumerate((self.pill_diag, self.pill_mo, self.pill_sd)):
            p.pack(side="left", padx=(0 if i == 0 else 12, 0))
        Tooltip(self.pill_diag,
                "DIAG - Diagnostic: anomaly detected by the TB9120 driver - "
                "open motor coil (load), over-temperature, or over-current.\n\n"
                "Mirrors the red DIAG LED on the click board.")
        Tooltip(self.pill_mo,
                "MO - Motor Origin: the electrical angle is at its initial "
                "position. Blinks once every 4 full steps while the motor "
                "turns, then freezes in whatever state the move ended.\n\n"
                "Mirrors the blue MO LED on the click board.")
        Tooltip(self.pill_sd,
                "SD - Stall Detection: the rotor lost steps (step-out). "
                "Detection threshold is set by the VR2 trimmer on the "
                "board.\n\nMirrors the SD LED on the click board.")
        self.idle_switch = Switch(c, initial=True)
        row(c, "Low idle current", self.idle_switch,
            sub="torque 30% while idle (EN not wired)")
        self.error_lbl = tk.Label(c, text=" ", bg=CARD, fg=RED,
                                  font=(FONT, 9), wraplength=CONTENT_W,
                                  justify="left")
        self.error_lbl.pack(anchor="w")

    def _fit_window(self) -> None:
        """Size the window to exactly fit all cards (capped to the screen)."""
        self.root.update_idletasks()
        height = self.root.winfo_reqheight()
        max_height = self.root.winfo_screenheight() - 90
        self.root.geometry(f"400x{min(height, max_height)}")

    def _speed_text(self, v) -> None:
        self.speed_lbl.config(text=f"{v} st/s")

    def _accel_text(self, v) -> None:
        self.accel_lbl.config(text=f"{v}")

    def _ramp_vis(self, on: bool) -> None:
        if on:
            self.accel_row.pack(fill="x", pady=4)
            self.accel_slider.pack(pady=(0, 2))
        else:
            self.accel_row.pack_forget()
            self.accel_slider.pack_forget()
        card = self.accel_row.master
        if hasattr(card, "refresh_card"):
            card.after_idle(card.refresh_card)
        self.root.after_idle(self._fit_window)

    # -------------------------------------------------------------- events

    def _steps_value(self) -> int:
        try:
            return abs(int(float(self.steps_var.get())))
        except ValueError:
            return 0

    def _on_go(self) -> None:
        if self.moving:
            self.stop_flag.set()
            return
        steps = self._steps_value()
        if not steps or not self.connected:
            return
        if self.direction.value.startswith("⟲"):
            steps = -steps
        self._queue_move(steps)

    def _jog(self, steps: int) -> None:
        if not self.moving and self.connected:
            self._queue_move(steps, allow_loop=False)

    def _queue_move(self, steps: int, allow_loop: bool = True) -> None:
        low_idle = self.idle_switch.state
        loop = allow_loop and self.loop_switch.state
        live_dir = allow_loop           # jog buttons keep their fixed sign
        self.moving = True              # optimistic, refined by the worker
        self.jobs.put(lambda: self._job_move(steps, low_idle, loop, live_dir))

    def _ui_sign(self) -> int:
        """Current direction selector as a sign (live-readable)."""
        return -1 if self.direction.value.startswith("⟲") else 1

    # -------------------------------------------------------------- worker

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self.error = None
                job()
            except Exception as exc:  # noqa: BLE001 - shown in the UI
                self.error = str(exc)
                self.moving = False

    def _job_connect(self) -> None:
        try:
            from stepper11_ftdi import open_links
            i2c, gpio = open_links()
            self._links.extend((i2c, gpio))
            self.motor = Stepper11(i2c, gpio_link=gpio)
            self.connected = True
            self._job_refresh()
        finally:
            self._connect_pending = False

    def _job_refresh(self) -> None:
        if self.motor is not None and not self.moving:
            self.flags = self.motor.diagnostics()

    def _apply_live_settings(self) -> None:
        """Push resolution/torque changes to the driver mid-move (over I2C)."""
        motor = self.motor
        resolution = RES_FROM_LABEL[self.res_seg.value]
        if resolution != motor.resolution:
            motor.set_resolution(resolution)
        torque = int(self.torque_seg.value.rstrip("%"))
        if torque != self._applied_torque:
            motor.set_torque(torque)
            self._applied_torque = torque

    def _run_leg(self, magnitude, parity, live_dir=True) -> None:
        """Execute one move of `magnitude` full steps.

        Runs in ~0.25 s chunks so STOP stays responsive and so speed,
        resolution, torque and direction changes apply while moving.
        `parity` flips the leg for loop mode; jog moves pass live_dir=False
        to keep their fixed sign. The ramp switch is re-read here, at every
        leg start, so toggling it mid-loop applies on the next leg.
        """
        motor = self.motor
        accel = (float(self.accel_slider.value)
                 if self.ramp_switch.state else None)
        total = abs(magnitude)
        done = 0
        self.progress = (0, total)
        try:
            if accel:
                # Ramped moves run as one precomputed profile (no live tuning).
                speed = float(self.speed_slider.value)
                sign = (self._ui_sign() if live_dir else 1) * parity
                motor.move_steps(sign * total, speed, accel)
                done = total
            else:
                while done < total and not self.stop_flag.is_set():
                    self._apply_live_settings()
                    speed = float(self.speed_slider.value)
                    sign = (self._ui_sign() if live_dir else 1) * parity
                    chunk = max(1, min(50, int(speed * 0.25)))
                    n = min(chunk, total - done)
                    motor.move_steps(sign * n, speed)
                    done += n
                    self.progress = (done, total)
        finally:
            self.progress = (done, total)

    def _job_move(self, steps, low_idle, loop=False, live_dir=True) -> None:
        motor = self.motor
        self.moving = True
        self.stop_flag.clear()
        self.cycle = 0
        self._applied_torque = None
        self._apply_live_settings()
        magnitude = abs(steps)
        launch_sign = 1 if steps > 0 else -1
        try:
            if loop:
                while not self.stop_flag.is_set():
                    self.cycle += 1
                    self._run_leg(magnitude, 1)
                    if self.stop_flag.is_set():
                        break
                    time.sleep(0.3)          # settle before reversing
                    self._run_leg(magnitude, -1)
                    time.sleep(0.3)
            else:
                parity = 1 if live_dir else launch_sign
                self._run_leg(magnitude, parity, live_dir)
        finally:
            if low_idle:
                motor.set_torque(30)
                self._applied_torque = None
            self.flags = motor.diagnostics()
            self.moving = False

    # ----------------------------------------------------------------- tick

    def _tick(self) -> None:
        now = time.monotonic()
        if self.connected:
            self.status_lbl.config(text="● connected", fg=GREEN)
        elif self._connect_pending:
            self.status_lbl.config(text="● connecting…", fg=ORANGE)
        else:
            self.status_lbl.config(text="● not connected - retrying", fg=RED)
            if now - self._last_connect > 3.0:
                self._last_connect = now
                self._connect_pending = True
                self.jobs.put(self._job_connect)

        if self.moving:
            self.go_btn.config_pill(text="STOP", color=RED, enabled=True)
            if self.progress:
                done, total = self.progress
                prefix = f"cycle {self.cycle}  ·  " if self.cycle else ""
                self.progress_lbl.config(
                    text=f"{prefix}moving…  {done} / {total} steps")
        else:
            self.go_btn.config_pill(text="GO", color=BLUE,
                                    enabled=self.connected)
            if self.progress:
                done, total = self.progress
                if self.cycle:
                    self.progress_lbl.config(
                        text=f"loop stopped after {self.cycle} cycle(s)")
                else:
                    stopped = " (stopped)" if done < total else ""
                    self.progress_lbl.config(
                        text=f"last move: {done} steps{stopped}")

        if self.flags:
            self.pill_diag.set(self.flags.get("diag"))
            self.pill_mo.set(self.flags.get("mo"))
            self.pill_sd.set(self.flags.get("sd"))

        self.error_lbl.config(text=self.error or " ")

        if self.connected and not self.moving and now - self._last_refresh > 0.5:
            self._last_refresh = now
            self.jobs.put(self._job_refresh)

        self.root.after(120, self._tick)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true",
                        help="open the window and close it automatically")
    args = parser.parse_args()

    root = tk.Tk()
    MotorApp(root)
    if args.selftest:
        root.after(3000, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
