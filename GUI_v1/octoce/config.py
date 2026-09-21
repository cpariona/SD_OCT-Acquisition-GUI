from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from math import ceil, floor, isfinite
from typing import Any


# PFI13 is a 5 V-logic counter output. The pulse train timing is fixed at 10% duty.
OCE_TRIGGER_DUTY_CYCLE = 0.10


class AcquisitionMode(str, Enum):
    """Acquisition nesting order.

    BM stores [bscan, repetition, aline, pixel].
    MB stores [bscan, aline, repetition, pixel].
    """

    BM = "BM"
    MB = "MB"


class ScanPattern(str, Enum):
    RASTER = "raster"
    CROSSHAIR = "crosshair"
    MERIDIANS = "meridians"
    LINEAR = "linear"


class Orientation(str, Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScanParameters:
    alines: int = 512
    bscans: int = 64
    m_repetitions: int = 1
    sync_points: int = 50
    x_length_mm: float = 5.0
    y_length_mm: float = 5.0
    mode: AcquisitionMode = AcquisitionMode.BM
    pattern: ScanPattern = ScanPattern.RASTER
    orientation: Orientation = Orientation.HORIZONTAL
    center_x_mm: float = 0.0
    center_y_mm: float = 0.0
    raster_bidirectional: bool = False
    bframes_delay_us: float = 0.0

    def validate(self) -> None:
        errors: list[str] = []
        stationary = self.x_length_mm == 0 and self.y_length_mm == 0
        minimum_alines = 1 if stationary else 2
        if self.alines < minimum_alines:
            errors.append(f"La cantidad de A-lines debe ser >= {minimum_alines}.")
        if self.bscans < 1:
            errors.append("La cantidad de B-scans debe ser >= 1.")
        if self.m_repetitions < 1:
            errors.append("M repeticiones debe ser >= 1.")
        if self.sync_points < 0:
            errors.append("El número de puntos sync no puede ser negativo.")
        for label, value in (
            ("Longitud X", self.x_length_mm),
            ("Longitud Y", self.y_length_mm),
            ("Centro X", self.center_x_mm),
            ("Centro Y", self.center_y_mm),
            ("BFramesDelay", self.bframes_delay_us),
        ):
            if not isfinite(value):
                errors.append(f"{label} debe ser un número finito.")
        if self.x_length_mm < 0 or self.y_length_mm < 0:
            errors.append("Las longitudes no pueden ser negativas.")
        if not stationary and self.pattern in (ScanPattern.RASTER, ScanPattern.MERIDIANS):
            if self.x_length_mm <= 0 or self.y_length_mm <= 0:
                errors.append("Raster y meridianos requieren longitudes X e Y mayores que cero.")
        elif not stationary and self.pattern is ScanPattern.LINEAR:
            selected = self.x_length_mm if self.orientation is Orientation.HORIZONTAL else self.y_length_mm
            if selected <= 0:
                axis = "X" if self.orientation is Orientation.HORIZONTAL else "Y"
                errors.append(f"El escaneo lineal {self.orientation.value} requiere longitud {axis} > 0.")
        elif not stationary and self.pattern is ScanPattern.CROSSHAIR:
            if self.x_length_mm <= 0 or self.y_length_mm <= 0:
                errors.append("Crosshair requiere longitudes X e Y mayores que cero.")
        if self.expected_alines > 2_000_000_000:
            errors.append("La adquisición excede 2 000 millones de A-lines; divídala en archivos.")
        if errors:
            raise ConfigurationError("\n".join(errors))

    @property
    def is_stationary(self) -> bool:
        return self.x_length_mm == 0 and self.y_length_mm == 0

    @property
    def expected_alines(self) -> int:
        return self.alines * self.bscans * self.m_repetitions * self.sweeps_per_bscan

    @property
    def sweeps_per_bscan(self) -> int:
        return 2 if self.pattern is ScanPattern.CROSSHAIR else 1

    @property
    def lines_per_segment(self) -> int:
        return self.alines if self.mode is AcquisitionMode.BM else self.m_repetitions

    @property
    def total_segments(self) -> int:
        if self.mode is AcquisitionMode.BM:
            return self.bscans * self.m_repetitions * self.sweeps_per_bscan
        return self.bscans * self.alines * self.sweeps_per_bscan

    @property
    def oce_trigger_segments(self) -> int:
        if self.mode is AcquisitionMode.MB:
            return self.total_segments
        # A crosshair X+Y pair is one logical B-scan, so BM triggers on X only.
        return self.bscans * self.m_repetitions

    @property
    def axis_order(self) -> tuple[str, ...]:
        if self.pattern is ScanPattern.CROSSHAIR:
            if self.mode is AcquisitionMode.BM:
                return ("bscan", "m_repetition", "sweep_xy", "aline", "pixel")
            return ("bscan", "sweep_xy", "aline", "m_repetition", "pixel")
        if self.mode is AcquisitionMode.BM:
            return ("bscan", "m_repetition", "aline", "pixel")
        return ("bscan", "aline", "m_repetition", "pixel")

    @property
    def logical_shape_without_pixels(self) -> tuple[int, ...]:
        if self.pattern is ScanPattern.CROSSHAIR:
            if self.mode is AcquisitionMode.BM:
                return (self.bscans, self.m_repetitions, 2, self.alines)
            return (self.bscans, 2, self.alines, self.m_repetitions)
        if self.mode is AcquisitionMode.BM:
            return (self.bscans, self.m_repetitions, self.alines)
        return (self.bscans, self.alines, self.m_repetitions)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["mode"] = self.mode.value
        result["pattern"] = self.pattern.value
        result["orientation"] = self.orientation.value
        # Explicit marker lets older unidirectional linear .bin files retain
        # their original interpretation in external readers.
        result["linear_bidirectional"] = self.pattern is ScanPattern.LINEAR
        return result


@dataclass(frozen=True, slots=True)
class HardwareConfig:
    daq_device: str = "Dev1"
    camera_interface: str = "img0"
    spectral_samples: int = 2048
    sensor_bit_depth: int = 12
    line_rate_hz: float = 50_000.0
    k_start_nm: float = 1472.76
    k_end_nm: float = 1261.36
    x_v_per_mm: float = 0.40528098
    y_v_per_mm: float = 0.40710430
    ao_min_v: float = -10.0
    ao_max_v: float = 10.0
    max_galvo_abs_v: float = 9.5
    galvo_v_per_degree: float = 0.8
    beam_diameter_mm: float = 0.0
    park_x_mm: float = 0.0
    park_y_mm: float = 0.0
    camera_counter: str = "ctr0"
    oce_counter: str = "ctr1"
    camera_trigger_terminal: str = "/Dev1/PFI12"
    oce_trigger_terminal: str = "/Dev1/PFI13"
    camera_trigger_width_us: float = 5.0
    camera_phase_offset_us: float = 0.0
    oce_enabled: bool = True
    oce_pulse_width_us: float = 10.0
    imaq_ring_buffers: int = 8
    frame_timeout_ms: int = 2_000
    writer_queue_size: int = 8
    preview_rate_hz: float = 10.0
    park_ramp_points: int = 200
    configure_sensor_trigger: bool = True
    require_external_sensor_trigger: bool = True
    sensor_trigger_attribute: str = "Trigger Mode"
    sensor_trigger_value: str = "1:  Fixed Exp"
    external_buffer_trigger_enabled: bool = True
    external_buffer_trigger_line: int = 0

    def validate(self, scan: ScanParameters | None = None) -> None:
        errors: list[str] = []
        valid_line_rate = isfinite(self.line_rate_hz) and 9_600 <= self.line_rate_hz <= 147_000
        if not self.daq_device.strip():
            errors.append("El nombre del dispositivo DAQ no puede estar vacío.")
        if not self.camera_interface.strip():
            errors.append("La interfaz de cámara no puede estar vacía.")
        if self.spectral_samples < 2:
            errors.append("El sensor debe tener al menos 2 píxeles espectrales.")
        if not 1 <= self.sensor_bit_depth <= 16:
            errors.append("La profundidad del sensor debe estar entre 1 y 16 bits.")
        if not isfinite(self.line_rate_hz) or self.line_rate_hz < 9_600:
            errors.append("La frecuencia de la GL2048R debe ser al menos 9,6 klps.")
        elif self.line_rate_hz > 147_000:
            errors.append("La cámara GL2048R no debe configurarse por encima de 147 klps.")
        if (
            not isfinite(self.k_start_nm)
            or not isfinite(self.k_end_nm)
            or not 900.0 <= self.k_start_nm <= 1700.0
            or not 900.0 <= self.k_end_nm <= 1700.0
            or self.k_start_nm == self.k_end_nm
        ):
            errors.append("Inicio y fin para linealización k deben ser distintos y estar entre 900 y 1700 nm.")
        if not (self.ao_min_v < self.ao_max_v):
            errors.append("El rango AO es inválido.")
        if self.max_galvo_abs_v <= 0:
            errors.append("El límite de tensión del galvo debe ser positivo.")
        if self.max_galvo_abs_v > max(abs(self.ao_min_v), abs(self.ao_max_v)):
            errors.append("El límite del galvo excede el rango configurado de la DAQ.")
        if self.galvo_v_per_degree not in (0.5, 0.8, 1.0):
            errors.append("La escala JP7 del GVS002 debe ser 0.5, 0.8 o 1.0 V/°.")
        manual_input_limit = 6.25 if self.galvo_v_per_degree == 0.5 else 10.0
        if self.max_galvo_abs_v > manual_input_limit:
            errors.append(
                f"Con JP7={self.galvo_v_per_degree:g} V/° el límite de entrada manual es "
                f"±{manual_input_limit:g} V."
            )
        if not isfinite(self.beam_diameter_mm) or not 0 <= self.beam_diameter_mm <= 5:
            errors.append("El diámetro del haz en los espejos debe estar entre 0 (desconocido) y 5 mm.")
        if self.x_v_per_mm <= 0 or self.y_v_per_mm <= 0:
            errors.append("Los factores V/mm deben ser positivos.")
        expected_prefix = f"/{self.daq_device}/"
        if not self.camera_trigger_terminal.startswith(expected_prefix):
            errors.append(f"El terminal de cámara debe pertenecer a {self.daq_device}.")
        if not self.oce_trigger_terminal.startswith(expected_prefix):
            errors.append(f"El terminal OCE debe pertenecer a {self.daq_device}.")
        if self.oce_enabled and self.camera_counter.casefold() == self.oce_counter.casefold():
            errors.append("Los triggers de cámara y OCE no pueden usar el mismo contador.")
        if self.oce_enabled and self.camera_trigger_terminal.casefold() == self.oce_trigger_terminal.casefold():
            errors.append("Los triggers de cámara y OCE no pueden usar el mismo terminal.")
        period_us = 1_000_000.0 / self.effective_line_rate_hz if valid_line_rate else 0.0
        if valid_line_rate and not 4.5 <= self.camera_trigger_width_us < min(102.0, period_us):
            errors.append(
                f"El pulso CC1 debe estar entre 4,5 µs y el periodo de A-line "
                f"({period_us:.3f} µs), sin alcanzar el periodo."
            )
        if self.oce_enabled and (
            not isfinite(self.oce_pulse_width_us) or self.oce_pulse_width_us <= 0
        ):
            errors.append("El ancho del trigger OCE debe ser positivo.")
        if self.imaq_ring_buffers < 3:
            errors.append("Use al menos 3 buffers NI-IMAQ para detectar y absorber latencia.")
        if self.frame_timeout_ms < 100:
            errors.append("El timeout de cámara debe ser >= 100 ms.")
        if self.writer_queue_size < 2:
            errors.append("La cola de escritura debe tener al menos 2 bloques.")
        if self.preview_rate_hz <= 0:
            errors.append("La frecuencia de preview debe ser positiva.")
        if self.park_ramp_points < 2:
            errors.append("La rampa de parqueo debe tener al menos 2 puntos.")
        if not self.external_buffer_trigger_enabled:
            errors.append(
                "PFI12 llega al frame grabber: debe habilitarse el trigger externo de cada buffer."
            )
        if not 0 <= self.external_buffer_trigger_line <= 8:
            errors.append("La línea de trigger externo NI-IMAQ debe estar entre 0 y 8.")
        if scan is not None:
            if valid_line_rate:
                sync_duration_us = scan.sync_points / self.effective_line_rate_hz * 1_000_000.0
                if self.camera_phase_offset_us < -sync_duration_us:
                    errors.append("El retardo de cámara adelanta PFI12 hasta la región sync.")
                if (
                    self.oce_enabled
                    and self.camera_phase_offset_us + scan.bframes_delay_us < -sync_duration_us
                ):
                    errors.append("El retardo OCE adelanta PFI13 antes del inicio del segmento.")
            x_lo = (scan.center_x_mm - scan.x_length_mm / 2.0) * self.x_v_per_mm
            x_hi = (scan.center_x_mm + scan.x_length_mm / 2.0) * self.x_v_per_mm
            y_lo = (scan.center_y_mm - scan.y_length_mm / 2.0) * self.y_v_per_mm
            y_hi = (scan.center_y_mm + scan.y_length_mm / 2.0) * self.y_v_per_mm
            peak = max(abs(x_lo), abs(x_hi), abs(y_lo), abs(y_hi))
            if peak > self.max_galvo_abs_v:
                errors.append(
                    f"La trayectoria requiere {peak:.3f} V y excede el límite del galvo "
                    f"({self.max_galvo_abs_v:.3f} V)."
                )
            if min(x_lo, y_lo) < self.ao_min_v or max(x_hi, y_hi) > self.ao_max_v:
                errors.append("La trayectoria excede el rango AO configurado.")
            if self.galvo_v_per_degree > 0:
                x_min_deg, x_max_deg, y_min_deg, y_max_deg = self.manual_angle_limits_deg()
                beam_for_limits = self.beam_diameter_mm or 5.0
                x_min_command = x_lo / self.galvo_v_per_degree
                x_max_command = x_hi / self.galvo_v_per_degree
                y_min_command = y_lo / self.galvo_v_per_degree
                y_max_command = y_hi / self.galvo_v_per_degree
                if x_min_command < x_min_deg or x_max_command > x_max_deg:
                    errors.append(
                        f"El recorrido X ({x_min_command:.2f}° a {x_max_command:.2f}°) excede "
                        f"la tabla GVS002 para haz de {beam_for_limits:g} mm "
                        f"({x_min_deg:g}° a {x_max_deg:g}°)."
                    )
                if y_min_command < y_min_deg or y_max_command > y_max_deg:
                    errors.append(
                        f"El recorrido Y ({y_min_command:.2f}° a {y_max_command:.2f}°) excede "
                        f"la tabla GVS002 para haz de {beam_for_limits:g} mm "
                        f"({y_min_deg:g}° a {y_max_deg:g}°)."
                    )
        park_x, park_y = self.park_volts
        if max(abs(park_x), abs(park_y)) > self.max_galvo_abs_v:
            errors.append("La posición de park excede el límite del galvo.")
        if self.galvo_v_per_degree > 0:
            x_min_deg, x_max_deg, y_min_deg, y_max_deg = self.manual_angle_limits_deg()
            park_x_deg = park_x / self.galvo_v_per_degree
            park_y_deg = park_y / self.galvo_v_per_degree
            if not x_min_deg <= park_x_deg <= x_max_deg:
                errors.append("La posición park X excede la tabla angular del GVS002.")
            if not y_min_deg <= park_y_deg <= y_max_deg:
                errors.append("La posición park Y excede la tabla angular del GVS002.")
        if errors:
            raise ConfigurationError("\n".join(errors))

    @property
    def camera_start_trigger_source(self) -> str:
        return f"/{self.daq_device}/ao/StartTrigger"

    @property
    def cc1_period_us(self) -> float:
        requested_period_us = 1_000_000.0 / self.line_rate_hz
        # The installed GL2048R ICD exposes period in increments of 0.1 µs.
        quantized = floor(requested_period_us * 10.0 + 0.5) / 10.0
        return min(106.0, max(6.8, quantized))

    @property
    def effective_line_rate_hz(self) -> float:
        return 1_000_000.0 / self.cc1_period_us

    @property
    def camera_operational_setting(self) -> str:
        rate = self.effective_line_rate_hz
        if rate <= 79_000:
            return "OPR 2 - 9.6 to 79klps"
        if rate <= 125_000:
            return "OPR 1 - 73 to 125klps"
        return "OPR 0 - 115 to 147klps"

    @property
    def park_volts(self) -> tuple[float, float]:
        return (self.park_x_mm * self.x_v_per_mm, self.park_y_mm * self.y_v_per_mm)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["effective_line_rate_hz"] = self.effective_line_rate_hz
        result["cc1_period_us"] = self.cc1_period_us
        result["camera_operational_setting"] = self.camera_operational_setting
        return result

    def oce_post_hold_points(self, scan: ScanParameters) -> int:
        """AO hold samples needed so a delayed PFI13 pulse cannot be truncated."""
        if not self.oce_enabled:
            return 0
        period_us = 1_000_000.0 / self.effective_line_rate_hz
        nominal_end_us = (scan.sync_points + scan.lines_per_segment) * period_us
        oce_end_us = (
            scan.sync_points * period_us
            + self.camera_phase_offset_us
            + scan.bframes_delay_us
            + self.oce_pulse_width_us
        )
        missing_us = max(0.0, oce_end_us - nominal_end_us)
        return int(ceil(missing_us / period_us))

    def manual_angle_limits_deg(self) -> tuple[float, float, float, float]:
        """Conservative GVS002 limits, selecting the next larger tabulated beam."""
        row = 5 if self.beam_diameter_mm == 0 else min(
            5, max(1, int(ceil(self.beam_diameter_mm)))
        )
        limits = {
            1: (-12.5, 11.0, -12.5, 12.5),
            2: (-11.5, 10.0, -12.5, 12.5),
            3: (-10.0, 9.5, -12.5, 12.5),
            4: (-8.5, 8.5, -12.5, 12.5),
            5: (-8.0, 8.0, -3.0, 12.5),
        }
        return limits[row]

    def safety_warnings(self, scan: ScanParameters) -> tuple[str, ...]:
        """Conservative warnings derived from the GVS002 manual, not hard limits."""
        warnings: list[str] = []
        if self.beam_diameter_mm == 0:
            warnings.append(
                "diámetro de haz desconocido; se aplican conservadoramente los límites GVS002 de 5 mm"
            )
        transition_us = scan.sync_points / self.effective_line_rate_hz * 1_000_000.0
        if transition_us < 300.0:
            warnings.append(
                f"sync={transition_us:.0f} µs es menor que la respuesta de paso pequeña "
                "de 300 µs indicada para el GVS002"
            )
        segment_rate = self.effective_line_rate_hz / (scan.sync_points + scan.lines_per_segment)
        if scan.mode is AcquisitionMode.BM and segment_rate > 175.0:
            warnings.append(
                f"{segment_rate:.1f} barridos/s supera la referencia de 175 Hz para "
                "triangular/sawtooth de recorrido completo del GVS002"
            )
        return tuple(warnings)


def estimate_payload_bytes(scan: ScanParameters, hardware: HardwareConfig) -> int:
    return scan.expected_alines * hardware.spectral_samples * 2


def stationary_alignment_timing(
    hardware: HardwareConfig, alines_per_block: int = 1000
) -> tuple[HardwareConfig, float]:
    """Reserve 5% camera/frame-grabber idle time while keeping MB trigger cadence.

    At 50 klps, 1000 lines consume the entire 20 ms period and NI-IMAQ can
    ignore frame triggers while finishing the preceding buffer.  The alignment
    camera is clocked slightly faster (52.5 klps requested at the default),
    while PFI12/PFI13 remain at the original 50 Hz target.
    """
    if alines_per_block < 1:
        raise ValueError("El bloque de alineación debe contener A-lines.")
    requested_block_rate_hz = hardware.effective_line_rate_hz / alines_per_block
    capture = replace(hardware, line_rate_hz=min(147_000.0, hardware.effective_line_rate_hz * 1.05))
    block_rate_hz = min(
        requested_block_rate_hz,
        capture.effective_line_rate_hz / (alines_per_block * 1.05),
    )
    return capture, block_rate_hz
