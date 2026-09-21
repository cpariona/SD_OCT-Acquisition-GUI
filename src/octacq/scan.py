"""Deterministic geometry and uniform hardware event schedules; no I/O."""
from dataclasses import dataclass
from math import ceil
from typing import Iterator
import numpy as np
from numpy.typing import NDArray
from .config import HardwareProfile, ScanRequest


@dataclass(frozen=True)
class Frame:
    index: int
    bscan: int
    repetition: int | None
    sweep: int
    aline: int | None
    reverse_storage: bool
    oce: bool
    xy_mm: NDArray[np.float64]


@dataclass(frozen=True)
class Block:
    frames: tuple[Frame, ...]
    volts: NDArray[np.float64]


@dataclass(frozen=True)
class Schedule:
    request: ScanRequest
    hardware: HardwareProfile
    period_ticks: int
    camera_delay_s: float
    oce_delay_s: float
    oce_stride: int
    continuous: bool = False

    @property
    def period_s(self):
        return self.period_ticks / self.hardware.effective_line_rate_hz

    @property
    def hold_points(self):
        return self.period_ticks - self.request.sync_points - self.request.frame_lines

    def frames(self) -> Iterator[Frame]:
        p = self.request
        index = 0
        for b in range(p.bscans):
            lines = _lines(p, b)
            if p.mode == "BM":
                for m in range(p.m_repetitions):
                    for sweep, line in enumerate(lines):
                        reverse = ((p.pattern == "linear" and p.linear_bidirectional and
                                    (b * p.m_repetitions + m) % 2 == 1) or
                                   (p.pattern == "raster" and p.raster_bidirectional and b % 2 == 1))
                        active = line[::-1].copy() if reverse else line.copy()
                        yield Frame(index, b, m, sweep, None, reverse,
                                    p.oce_enabled and sweep == 0, active)
                        index += 1
            else:
                for sweep, line in enumerate(lines):
                    reverse = b % 2 == 1 and ((p.pattern == "linear" and p.linear_bidirectional)
                                              or (p.pattern == "raster" and p.raster_bidirectional))
                    for a, point in enumerate(line[::-1] if reverse else line):
                        active = np.repeat(point[None, :], p.m_repetitions, axis=0)
                        yield Frame(index, b, None, sweep, a, False, p.oce_enabled, active)
                        index += 1

    def blocks(self, max_ao_bytes: int) -> Iterator[Block]:
        # Two float64 AO channels. Bound the allocated waveform, not the experiment.
        capacity = max_ao_bytes // (self.period_ticks * 2 * 8)
        if self.oce_stride == 2:
            capacity -= capacity % 2
        if capacity < self.oce_stride:
            raise ValueError("AO memory budget cannot contain one logical event")
        if self.continuous and capacity < self.request.frame_count:
            raise ValueError("Continuous cycle exceeds the AO memory budget")
        capacity = min(capacity, self.request.frame_count)
        previous = np.array((self.hardware.park_x_mm, self.hardware.park_y_mm))
        if self.continuous:
            # A regenerated cycle must join its own last position smoothly.
            for frame in self.frames():
                previous = frame.xy_mm[-1]
        waveform = np.empty((capacity * self.period_ticks, 2), dtype=np.float64)
        frames = []
        scale = np.array((self.hardware.x_v_per_mm, self.hardware.y_v_per_mm))
        for frame in self.frames():
            active = frame.xy_mm
            start = len(frames) * self.period_ticks
            part = waveform[start:start + self.period_ticks]
            sync = self.request.sync_points
            if sync == 0 and not np.allclose(previous, active[0], rtol=0, atol=1e-12):
                raise ValueError("A position jump requires explicit sync points")
            part[:sync] = transition(previous, active[0], sync)
            part[sync:sync + len(active)] = active
            part[sync + len(active):] = active[-1]
            part *= scale
            self.hardware.check_volts(part.min(axis=0))
            self.hardware.check_volts(part.max(axis=0))
            frames.append(frame)
            previous = active[-1]
            if len(frames) == capacity:
                waveform.setflags(write=False)
                yield Block(tuple(frames), waveform)
                frames = []
                waveform = np.empty((capacity * self.period_ticks, 2), dtype=np.float64)
        if frames:
            waveform = waveform[:len(frames) * self.period_ticks]
            waveform.setflags(write=False)
            yield Block(tuple(frames), waveform)


def transition(start, end, count):
    t = np.arange(1, count + 1, dtype=np.float64) / (count + 1)
    smooth = 6 * t**5 - 15 * t**4 + 10 * t**3
    return np.asarray(start)[None, :] + (np.asarray(end) - start)[None, :] * smooth[:, None]


def _lines(p, b):
    u = np.linspace(-1, 1, p.alines)
    x, y = p.center_x_mm, p.center_y_mm
    horizontal = np.column_stack((x + u * p.x_length_mm / 2, np.full_like(u, y)))
    vertical = np.column_stack((np.full_like(u, x), y + u * p.y_length_mm / 2))
    if p.is_stationary:
        return [horizontal] * p.sweeps_per_bscan
    if p.pattern == "crosshair":
        return [horizontal, vertical]
    if p.pattern == "linear":
        return [horizontal if p.orientation == "horizontal" else vertical]
    if p.pattern == "raster":
        if p.bscans > 1:
            horizontal[:, 1] += -p.y_length_mm / 2 + b * p.y_length_mm / (p.bscans - 1)
        return [horizontal]
    angle = np.pi * b / p.bscans
    return [np.column_stack((x + u * p.x_length_mm / 2 * np.cos(angle),
                             y + u * p.y_length_mm / 2 * np.sin(angle)))]


def plan(request: ScanRequest, hardware: HardwareProfile, *, continuous: bool = False) -> Schedule:
    p, h = request, hardware
    if continuous and not ((p.mode == "MB" and p.is_stationary and p.alines == p.bscans == 1)
                          or (p.mode == "BM" and p.pattern == "crosshair" and
                              p.bscans == p.m_repetitions == 1 and not p.oce_enabled)):
        raise ValueError("Continuous acquisition requires stationary MB or BM crosshair without OCE")
    rate = h.effective_line_rate_hz
    camera = p.sync_points / rate + h.camera_phase_offset_us * 1e-6
    oce = camera + p.bframes_delay_us * 1e-6
    if p.oce_enabled and oce < 0:
        raise ValueError("OCE event precedes AO start")
    # Unknown rearm is not guessed. Hardware execution requires a measured value.
    rearm = (h.camera_rearm_us or 0) * 1e-6
    duration = max(camera + p.frame_lines / rate, p.frame_lines / rate + rearm,
                   camera + h.cc1_trigger_width_us * 1e-6)
    if p.oce_enabled:
        duration = max(duration, oce + h.oce_pulse_width_us * 1e-6)
    ticks = max(2, ceil(duration * rate - 1e-9))
    stride = 2 if p.mode == "BM" and p.pattern == "crosshair" else 1
    schedule = Schedule(p, h, ticks, camera, oce, stride, continuous)
    # Validate the geometric envelope without expanding the acquisition.
    for b in range(p.bscans):
        for line in _lines(p, b):
            volts = line * (h.x_v_per_mm, h.y_v_per_mm)
            h.check_volts(volts.min(axis=0))
            h.check_volts(volts.max(axis=0))
    return schedule
