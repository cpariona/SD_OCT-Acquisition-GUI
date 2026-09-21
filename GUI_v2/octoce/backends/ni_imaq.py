from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_char_p, c_double, c_int32, c_uint32, c_void_p

import numpy as np
from numpy.typing import NDArray

from .base import BackendError, FrameIntegrityError


_IMG_BASE = 0x3FF60000
IMG_ATTR_BITSPERPIXEL = _IMG_BASE + 0x0066
IMG_ATTR_BYTESPERPIXEL = _IMG_BASE + 0x0067
IMG_ATTR_ROWPIXELS = _IMG_BASE + 0x00C1
IMG_ATTR_ROI_WIDTH = _IMG_BASE + 0x01A6
IMG_ATTR_ROI_HEIGHT = _IMG_BASE + 0x01A7
IMG_ATTR_LOST_FRAMES = _IMG_BASE + 0x0088
IMG_ATTR_FRAMEWAIT_MSEC = _IMG_BASE + 0x007D

IMG_SIGNAL_EXTERNAL = 0
IMG_TRIG_POLAR_ACTIVEH = 0
IMG_TRIG_ACTION_BUFFER = 3


class ImaqError(BackendError):
    def __init__(self, operation: str, code: int, description: str):
        super().__init__(f"NI-IMAQ {operation} falló ({code}): {description}")
        self.operation = operation
        self.code = code
        self.description = description


class NIIMAQCamera:
    """Minimal 64-bit NI-IMAQ ring wrapper for Camera Link frame grabbers.

    It follows NI's installed ExtractBuffers example: request cumulative buffer
    numbers, verify the actual number, copy while held, then release immediately.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise BackendError("NI-IMAQ solo está disponible en Windows.")
        try:
            self._dll = ctypes.WinDLL("imaq.dll")
        except OSError as exc:
            raise BackendError(
                "No se encontró imaq.dll. Instale NI Vision Acquisition Software de 64 bits."
            ) from exc
        self._bind()
        self.interface_id = c_uint32(0)
        self.session_id = c_uint32(0)
        self._ring_buffers: ctypes.Array[c_void_p] | None = None
        self._opened = False
        self.width = 0
        self.height = 0
        self.row_pixels = 0
        self.bytes_per_pixel = 0
        self.bits_per_pixel = 0
        self.next_buffer_number = 0

    def _bind(self) -> None:
        dll = self._dll
        dll.imgInterfaceOpen.argtypes = [c_char_p, POINTER(c_uint32)]
        dll.imgInterfaceOpen.restype = c_int32
        dll.imgSessionOpen.argtypes = [c_uint32, POINTER(c_uint32)]
        dll.imgSessionOpen.restype = c_int32
        dll.imgClose.argtypes = [c_uint32, c_uint32]
        dll.imgClose.restype = c_int32
        dll.imgSessionConfigureROI.argtypes = [c_uint32, c_uint32, c_uint32, c_uint32, c_uint32]
        dll.imgSessionConfigureROI.restype = c_int32
        dll.imgGetAttribute.argtypes = [c_uint32, c_uint32, c_void_p]
        dll.imgGetAttribute.restype = c_int32
        dll.imgSetAttributeFromVoidPtr.argtypes = [c_uint32, c_uint32, c_void_p]
        dll.imgSetAttributeFromVoidPtr.restype = c_int32
        dll.imgRingSetup.argtypes = [c_uint32, c_uint32, POINTER(c_void_p), c_uint32, c_uint32]
        dll.imgRingSetup.restype = c_int32
        dll.imgSessionStopAcquisition.argtypes = [c_uint32]
        dll.imgSessionStopAcquisition.restype = c_int32
        dll.imgSessionExamineBuffer2.argtypes = [
            c_uint32,
            c_uint32,
            POINTER(c_uint32),
            POINTER(c_void_p),
        ]
        dll.imgSessionExamineBuffer2.restype = c_int32
        dll.imgSessionReleaseBuffer.argtypes = [c_uint32]
        dll.imgSessionReleaseBuffer.restype = c_int32
        dll.imgSessionTriggerConfigure2.argtypes = [
            c_uint32,
            c_uint32,
            c_uint32,
            c_uint32,
            c_uint32,
            c_uint32,
        ]
        dll.imgSessionTriggerConfigure2.restype = c_int32
        dll.imgSetCameraAttributeString.argtypes = [c_uint32, c_char_p, c_char_p]
        dll.imgSetCameraAttributeString.restype = c_int32
        dll.imgSetCameraAttributeNumeric.argtypes = [c_uint32, c_char_p, c_double]
        dll.imgSetCameraAttributeNumeric.restype = c_int32
        dll.imgGetCameraAttributeString.argtypes = [c_uint32, c_char_p, c_char_p, c_uint32]
        dll.imgGetCameraAttributeString.restype = c_int32
        dll.imgGetCameraAttributeNumeric.argtypes = [c_uint32, c_char_p, POINTER(c_double)]
        dll.imgGetCameraAttributeNumeric.restype = c_int32
        dll.imgShowError.argtypes = [c_int32, c_char_p]
        dll.imgShowError.restype = c_int32

    def _description(self, code: int) -> str:
        text = ctypes.create_string_buffer(512)
        try:
            self._dll.imgShowError(c_int32(code), text)
            return text.value.decode("utf-8", errors="replace") or "Error desconocido"
        except Exception:
            return "Error desconocido"

    def _check(self, code: int, operation: str) -> None:
        if int(code) < 0:
            raise ImaqError(operation, int(code), self._description(int(code)))

    def _get_u32(self, attribute: int) -> int:
        value = c_uint32(0)
        self._check(
            self._dll.imgGetAttribute(self.session_id, c_uint32(attribute), byref(value)),
            f"imgGetAttribute(0x{attribute:08X})",
        )
        return int(value.value)

    def open(
        self,
        *,
        interface: str,
        width: int,
        height: int,
        ring_buffers: int,
        timeout_ms: int,
        configure_sensor_trigger: bool,
        require_external_sensor_trigger: bool,
        sensor_trigger_attribute: str,
        sensor_trigger_value: str,
        camera_operational_setting: str,
        cc1_trigger_width_us: float,
        cc1_period_us: float,
        external_buffer_trigger_enabled: bool,
        external_buffer_trigger_line: int,
    ) -> None:
        if self._opened:
            raise BackendError("La sesión NI-IMAQ ya está abierta.")
        try:
            self._check(
                self._dll.imgInterfaceOpen(interface.encode("ascii"), byref(self.interface_id)),
                "imgInterfaceOpen",
            )
            self._check(
                self._dll.imgSessionOpen(self.interface_id, byref(self.session_id)),
                "imgSessionOpen",
            )
            self._opened = True
            self._check(
                self._dll.imgSessionConfigureROI(self.session_id, 0, 0, height, width),
                "imgSessionConfigureROI",
            )
            timeout_value = c_uint32(timeout_ms)
            self._check(
                self._dll.imgSetAttributeFromVoidPtr(
                    self.session_id,
                    IMG_ATTR_FRAMEWAIT_MSEC,
                    byref(timeout_value),
                ),
                "configurar timeout de frame",
            )
            if configure_sensor_trigger:
                self.set_camera_attribute("Serial Commands", "ON")
                # Trigger Polarity performs a camera reset in the installed ICD,
                # so all remaining serial settings are deliberately applied after it.
                self.set_camera_attribute("Trigger Polarity", "CC1 High")
                self.set_camera_attribute("Operational Setting", camera_operational_setting)
                self.set_camera_attribute(sensor_trigger_attribute, sensor_trigger_value)
                self.set_camera_attribute_numeric("CC1 Trigger Width", cc1_trigger_width_us)
                self.set_camera_attribute_numeric("CC1 Trigger Period", cc1_period_us)
            if require_external_sensor_trigger:
                trigger_mode = self.get_camera_attribute(sensor_trigger_attribute)
                if "internal" in trigger_mode.casefold() or trigger_mode.lstrip().startswith("0:"):
                    raise BackendError(
                        f"El sensor reporta Trigger Mode={trigger_mode!r}. "
                        "Configure exposición externa antes de habilitar los galvos."
                    )
                actual_period = self.get_camera_attribute_numeric("CC1 Trigger Period")
                actual_width = self.get_camera_attribute_numeric("CC1 Trigger Width")
                if abs(actual_period - cc1_period_us) > 0.051:
                    raise BackendError(
                        f"La cámara reporta periodo CC1={actual_period:.3f} µs; "
                        f"se solicitaron {cc1_period_us:.3f} µs."
                    )
                if abs(actual_width - cc1_trigger_width_us) > 0.051:
                    raise BackendError(
                        f"La cámara reporta ancho CC1={actual_width:.3f} µs; "
                        f"se solicitaron {cc1_trigger_width_us:.3f} µs."
                    )
            if external_buffer_trigger_enabled:
                self._check(
                    self._dll.imgSessionTriggerConfigure2(
                        self.session_id,
                        IMG_SIGNAL_EXTERNAL,
                        external_buffer_trigger_line,
                        IMG_TRIG_POLAR_ACTIVEH,
                        timeout_ms,
                        IMG_TRIG_ACTION_BUFFER,
                    ),
                    "configurar trigger externo de buffer",
                )
            self.width = self._get_u32(IMG_ATTR_ROI_WIDTH)
            self.height = self._get_u32(IMG_ATTR_ROI_HEIGHT)
            self.row_pixels = self._get_u32(IMG_ATTR_ROWPIXELS)
            self.bytes_per_pixel = self._get_u32(IMG_ATTR_BYTESPERPIXEL)
            self.bits_per_pixel = self._get_u32(IMG_ATTR_BITSPERPIXEL)
            if self.width != width or self.height != height:
                raise BackendError(
                    f"NI-IMAQ coercionó el ROI a {self.width}×{self.height}; "
                    f"se solicitó {width}×{height}."
                )
            if self.bytes_per_pixel != 2:
                raise BackendError(
                    f"La cámara devuelve {self.bytes_per_pixel} bytes/píxel; este backend requiere uint16."
                )
            if self.row_pixels < self.width:
                raise BackendError(
                    f"NI-IMAQ reporta un stride de {self.row_pixels} píxeles para un ROI de "
                    f"{self.width}; no es posible copiar el frame de forma segura."
                )
            self._ring_buffers = (c_void_p * ring_buffers)()
            self._check(
                self._dll.imgRingSetup(
                    self.session_id,
                    ring_buffers,
                    self._ring_buffers,
                    0,
                    1,
                ),
                "imgRingSetup",
            )
            self.next_buffer_number = 0
        except Exception:
            self.close(abort=True)
            raise

    def get_camera_attribute(self, name: str) -> str:
        value = ctypes.create_string_buffer(512)
        self._check(
            self._dll.imgGetCameraAttributeString(
                self.session_id,
                name.encode("ascii"),
                value,
                len(value),
            ),
            f"leer atributo de cámara {name!r}",
        )
        return value.value.decode("utf-8", errors="replace")

    def set_camera_attribute(self, name: str, value: str) -> None:
        self._check(
            self._dll.imgSetCameraAttributeString(
                self.session_id,
                name.encode("ascii"),
                value.encode("ascii"),
            ),
            f"escribir atributo de cámara {name!r}",
        )

    def get_camera_attribute_numeric(self, name: str) -> float:
        value = c_double(0.0)
        self._check(
            self._dll.imgGetCameraAttributeNumeric(
                self.session_id,
                name.encode("ascii"),
                byref(value),
            ),
            f"leer atributo numérico de cámara {name!r}",
        )
        return float(value.value)

    def set_camera_attribute_numeric(self, name: str, value: float) -> None:
        self._check(
            self._dll.imgSetCameraAttributeNumeric(
                self.session_id,
                name.encode("ascii"),
                c_double(value),
            ),
            f"escribir atributo numérico de cámara {name!r}",
        )

    def read_into(self, destination: NDArray[np.uint16]) -> tuple[int, int, int]:
        if not self._opened:
            raise BackendError("La sesión NI-IMAQ no está abierta.")
        if destination.shape != (self.height, self.width) or destination.dtype != np.uint16:
            raise BackendError(
                f"Buffer destino inválido: {destination.shape}/{destination.dtype}; "
                f"esperado ({self.height}, {self.width})/uint16."
            )
        requested = self.next_buffer_number
        actual = c_uint32(0)
        address = c_void_p()
        held = False
        try:
            self._check(
                self._dll.imgSessionExamineBuffer2(
                    self.session_id,
                    requested,
                    byref(actual),
                    byref(address),
                ),
                "imgSessionExamineBuffer2",
            )
            held = True
            actual_number = int(actual.value)
            if actual_number != requested:
                raise FrameIntegrityError(
                    f"Discontinuidad NI-IMAQ: solicitado {requested}, recibido {actual_number}."
                )
            if not address.value:
                raise BackendError("NI-IMAQ devolvió un puntero de buffer nulo.")
            pixel_count = self.height * self.row_pixels
            source_type = ctypes.c_uint16 * pixel_count
            source = np.ctypeslib.as_array(source_type.from_address(address.value)).reshape(
                self.height, self.row_pixels
            )
            np.copyto(destination, source[:, : self.width], casting="no")
        finally:
            if held:
                self._check(self._dll.imgSessionReleaseBuffer(self.session_id), "imgSessionReleaseBuffer")
        self.next_buffer_number = requested + 1
        lost = self._get_u32(IMG_ATTR_LOST_FRAMES)
        return requested, int(actual.value), lost

    def close(self, *, abort: bool = False) -> None:
        if self.session_id.value:
            if self._opened:
                try:
                    self._dll.imgSessionStopAcquisition(self.session_id)
                except Exception:
                    pass
            try:
                self._dll.imgClose(self.session_id, 1)
            except Exception:
                pass
            self.session_id = c_uint32(0)
        if self.interface_id.value:
            try:
                self._dll.imgClose(self.interface_id, 1)
            except Exception:
                pass
            self.interface_id = c_uint32(0)
        self._ring_buffers = None
        self._opened = False
