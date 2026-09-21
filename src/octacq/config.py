"""Physical configuration, experiment requests and execution resources."""
from dataclasses import dataclass, fields
from math import floor, isfinite
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class ScanRequest:
    alines: int = 512
    bscans: int = 1
    m_repetitions: int = 1
    mode: str = "BM"
    pattern: str = "raster"
    orientation: str = "horizontal"
    x_length_mm: float = 1.0
    y_length_mm: float = 1.0
    center_x_mm: float = 0.0
    center_y_mm: float = 0.0
    sync_points: int = 50
    linear_bidirectional: bool = True
    raster_bidirectional: bool = False
    oce_enabled: bool = False
    bframes_delay_us: float = 0.0

    def __post_init__(self):
        for name in ("alines", "bscans", "m_repetitions", "sync_points"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "sync_points" else 1):
                raise ValueError(f"Invalid {name}")
        for name in ("linear_bidirectional", "raster_bidirectional", "oce_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Invalid {name}")
        if self.mode not in ("BM", "MB") or self.pattern not in (
            "raster", "crosshair", "meridians", "linear", "stationary"
        ) or self.orientation not in ("horizontal", "vertical"):
            raise ValueError("Invalid scan mode, pattern or orientation")
        for name in ("x_length_mm", "y_length_mm", "center_x_mm", "center_y_mm", "bframes_delay_us"):
            if not isfinite(getattr(self, name)):
                raise ValueError(f"Nonfinite {name}")
        if min(self.x_length_mm, self.y_length_mm) < 0:
            raise ValueError("Scan lengths cannot be negative")
        if self.pattern == "stationary" and not self.is_stationary:
            raise ValueError("Stationary requests require both lengths zero")
        if not self.is_stationary:
            if self.alines < 2:
                raise ValueError("Moving scans require at least two A-lines")
            lengths = (self.x_length_mm, self.y_length_mm)
            if self.pattern == "linear":
                lengths = (lengths[self.orientation == "vertical"],)
            if min(lengths) <= 0:
                raise ValueError("Active scan dimensions must be positive")
        if self.frame_lines > 0xFFFFFFFF or self.expected_alines > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("Request exceeds NI ROI or raw-format count capacity")

    @property
    def is_stationary(self):
        return self.x_length_mm == self.y_length_mm == 0

    @property
    def sweeps_per_bscan(self):
        return 2 if self.pattern == "crosshair" else 1

    @property
    def frame_lines(self):
        return self.alines if self.mode == "BM" else self.m_repetitions

    @property
    def frame_count(self):
        return self.bscans * self.sweeps_per_bscan * (self.m_repetitions if self.mode == "BM" else self.alines)

    @property
    def expected_alines(self):
        return self.frame_count * self.frame_lines

    @property
    def axis_order(self):
        middle = ("sweep_xy",) if self.pattern == "crosshair" else ()
        return (("bscan", "m_repetition") + middle + ("aline", "pixel") if self.mode == "BM"
                else ("bscan",) + middle + ("aline", "m_repetition", "pixel"))

    @property
    def logical_shape(self):
        sizes = dict(bscan=self.bscans, m_repetition=self.m_repetitions, sweep_xy=2, aline=self.alines)
        return tuple(sizes[axis] for axis in self.axis_order[:-1])


@dataclass(frozen=True)
class HardwareProfile:
    camera_model: str
    frame_grabber_model: str
    galvo_model: str
    daq_device: str
    ao_x: str
    ao_y: str
    camera_counter: str
    oce_counter: str
    camera_trigger_terminal: str
    oce_trigger_terminal: str
    ao_min_v: float
    ao_max_v: float
    camera_interface: str
    external_trigger_line: int
    spectral_samples: int
    bit_depth: int
    requested_line_rate_hz: float
    cc1_trigger_width_us: float
    camera_phase_offset_us: float
    trigger_mode: str
    trigger_polarity: str
    wavelength_start_nm: float
    wavelength_end_nm: float
    x_v_per_mm: float
    y_v_per_mm: float
    max_abs_v: float
    park_x_mm: float
    park_y_mm: float
    v_per_degree: float
    oce_pulse_width_us: float
    camera_rearm_us: float | None = None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, (float, int)) and not isfinite(value):
                raise ValueError(f"Nonfinite {field.name}")
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"Empty {field.name}")
        if not 9600 <= self.requested_line_rate_hz <= 147000:
            raise ValueError("Line rate outside camera operating range")
        if not 4.5 <= self.cc1_trigger_width_us < min(102, self.cc1_period_us):
            raise ValueError("Invalid CC1 width")
        if not 0 <= self.camera_phase_offset_us < self.cc1_period_us:
            raise ValueError("Camera phase must stay within the active A-line tick")
        if min(self.x_v_per_mm, self.y_v_per_mm, self.max_abs_v, self.oce_pulse_width_us) <= 0:
            raise ValueError("Calibration, voltage limits and pulse width must be positive")
        if not self.ao_min_v <= -self.max_abs_v < self.max_abs_v <= self.ao_max_v:
            raise ValueError("Galvo limit exceeds AO range")
        if self.v_per_degree not in (0.5, 0.8, 1.0):
            raise ValueError("Invalid GVS002 JP7 scale")
        if self.v_per_degree == 0.5 and self.max_abs_v > 6.25:
            raise ValueError("JP7 setting restricts the electrical input")
        if self.camera_counter == self.oce_counter or self.ao_x == self.ao_y:
            raise ValueError("Channels must be distinct")
        if self.camera_trigger_terminal == self.oce_trigger_terminal:
            raise ValueError("Trigger terminals must be distinct")
        if any(not terminal.startswith(f"/{self.daq_device}/") for terminal in
               (self.camera_trigger_terminal, self.oce_trigger_terminal)):
            raise ValueError("Trigger route belongs to another device")
        if type(self.spectral_samples) is not int or self.spectral_samples < 1:
            raise ValueError("Invalid sensor width")
        if type(self.bit_depth) is not int or not 1 <= self.bit_depth <= 16:
            raise ValueError("Invalid sensor bit depth")
        if type(self.external_trigger_line) is not int or not 0 <= self.external_trigger_line <= 8:
            raise ValueError("Invalid external trigger line")
        if self.camera_rearm_us is not None and self.camera_rearm_us < 0:
            raise ValueError("Camera rearm cannot be negative")
        if min(self.wavelength_start_nm, self.wavelength_end_nm) <= 0 or self.wavelength_start_nm == self.wavelength_end_nm:
            raise ValueError("Spectrometer endpoints must be positive and distinct")
        self.check_volts(self.park_volts)

    @property
    def cc1_period_us(self):
        return floor(1e7 / self.requested_line_rate_hz + 0.5) / 10

    @property
    def effective_line_rate_hz(self):
        return 1e6 / self.cc1_period_us

    @property
    def operational_setting(self):
        rate = self.effective_line_rate_hz
        return ("OPR 2 - 9.6 to 79klps" if rate <= 79000 else
                "OPR 1 - 73 to 125klps" if rate <= 125000 else "OPR 0 - 115 to 147klps")

    @property
    def park_volts(self):
        return self.park_x_mm * self.x_v_per_mm, self.park_y_mm * self.y_v_per_mm

    @property
    def ao_start_trigger(self):
        return f"/{self.daq_device}/ao/StartTrigger"

    def check_volts(self, xy):
        # Conservative 5 mm beam envelope from the existing GVS002 contract.
        for value, angular in zip(xy, ((-8.0, 8.0), (-3.0, 12.5))):
            if not isfinite(value) or not max(self.ao_min_v, -self.max_abs_v,
                    angular[0] * self.v_per_degree) <= value <= min(
                    self.ao_max_v, self.max_abs_v, angular[1] * self.v_per_degree):
                raise ValueError("Trajectory exceeds electrical/galvo envelope")


@dataclass(frozen=True)
class RuntimePolicy:
    ring_buffers: int
    frame_timeout_ms: int
    writer_queue_blocks: int
    park_ramp_points: int
    ao_buffer_bytes: int

    def __post_init__(self):
        for name, minimum in (("ring_buffers", 3), ("frame_timeout_ms", 100),
                              ("writer_queue_blocks", 2), ("park_ramp_points", 2), ("ao_buffer_bytes", 32)):
            if type(getattr(self, name)) is not int or getattr(self, name) < minimum:
                raise ValueError(f"Invalid {name}")


def load_config(path: str | Path) -> tuple[HardwareProfile, RuntimePolicy]:
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    daq, grabber, camera = data["daq"], data["frame_grabber"], data["camera"]
    values = {key: value for section in (daq, camera, data["galvo"], data["spectrometer"])
              for key, value in section.items() if key not in ("model", "device")}
    values.update(daq_device=daq["device"], camera_interface=grabber["interface"],
                  camera_model=camera["model"], frame_grabber_model=grabber["model"], galvo_model=data["galvo"]["model"],
                  external_trigger_line=grabber["external_trigger_line"],
                  oce_pulse_width_us=data["oce"]["trigger_pulse_width_us"])
    return HardwareProfile(**values), RuntimePolicy(
        ring_buffers=grabber["ring_buffers"], frame_timeout_ms=grabber["frame_timeout_ms"], **data["runtime"])
