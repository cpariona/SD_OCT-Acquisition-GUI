"""Direct NI-IMAQ camera session and numbered raw ring extraction."""
import ctypes
from math import ceil
from ctypes import POINTER, byref, c_char_p, c_double, c_int32, c_uint32, c_void_p
import numpy as np
from ..config import HardwareProfile, RuntimePolicy

_IMG_BASE = 0x3FF60000
IMG_ATTR_BITSPERPIXEL = _IMG_BASE + 0x0066
IMG_ATTR_BYTESPERPIXEL = _IMG_BASE + 0x0067
IMG_ATTR_ROWPIXELS = _IMG_BASE + 0x00C1
IMG_ATTR_ROI_WIDTH = _IMG_BASE + 0x01A6
IMG_ATTR_ROI_HEIGHT = _IMG_BASE + 0x01A7
IMG_ATTR_LOST_FRAMES = _IMG_BASE + 0x0088
IMG_ATTR_FRAMEWAIT_MSEC = _IMG_BASE + 0x007D


class Camera:
    def __init__(self, profile: HardwareProfile, policy: RuntimePolicy):
        self.profile = profile
        self.policy = policy
        self._dll = None
        self.interface_id = c_uint32()
        self.session_id = c_uint32()
        self.height = 0
        self.next_buffer = 0
        self.lost_buffers = 0
        self.discontinuous_buffers = 0
        self._ring = None

    def connect(self, height: int):
        if self.session_id.value and height == self.height:
            return
        if self.session_id.value:
            self.close()  # ROI changes require rebuilding the session-owned ring.
        if self._dll is None:
            self._dll = ctypes.WinDLL("imaq.dll")
            self._bind()
        h, r = self.profile, self.policy
        try:
            self._check(self._dll.imgInterfaceOpen(h.camera_interface.encode("ascii"), byref(self.interface_id)), "interface open")
            self._check(self._dll.imgSessionOpen(self.interface_id, byref(self.session_id)), "session open")
            self._check(self._dll.imgSessionConfigureROI(self.session_id, 0, 0, height, h.spectral_samples), "ROI")
            self.set_read_timeout(r.frame_timeout_ms + ceil(height * h.cc1_period_us / 1000))
            # The installed ICD resets the sensor on polarity change: apply this first.
            self._set_string("Serial Commands", "ON")
            self._set_string("Trigger Polarity", h.trigger_polarity)
            self._set_string("Operational Setting", h.operational_setting)
            self._set_string("Trigger Mode", h.trigger_mode)
            for name, value in (("CC1 Trigger Width", h.cc1_trigger_width_us), ("CC1 Trigger Period", h.cc1_period_us)):
                self._check(self._dll.imgSetCameraAttributeNumeric(self.session_id, name.encode(), c_double(value)), name)
                actual = c_double()
                self._check(self._dll.imgGetCameraAttributeNumeric(self.session_id, name.encode(), byref(actual)), name)
                if abs(actual.value - value) > 0.051:
                    raise RuntimeError(f"Camera coerced {name}: {actual.value} != {value}")
            for name, expected in (("Trigger Mode", h.trigger_mode), ("Trigger Polarity", h.trigger_polarity)):
                value = ctypes.create_string_buffer(512)
                self._check(self._dll.imgGetCameraAttributeString(self.session_id, name.encode(), value, len(value)), name)
                if value.value.decode().strip() != expected:
                    raise RuntimeError(f"Camera did not confirm {name}")
            # External active-high Trigger Each Buffer (protocol values 0, 0, 3).
            self._check(self._dll.imgSessionTriggerConfigure2(self.session_id, 0, h.external_trigger_line,
                        0, 0xFFFFFFFF, 3), "external buffer trigger")
            self.row_pixels = self._get_u32(IMG_ATTR_ROWPIXELS)
            if (self._get_u32(IMG_ATTR_ROI_WIDTH) != h.spectral_samples or
                self._get_u32(IMG_ATTR_ROI_HEIGHT) != height or
                self._get_u32(IMG_ATTR_BYTESPERPIXEL) != 2 or
                self._get_u32(IMG_ATTR_BITSPERPIXEL) != h.bit_depth or self.row_pixels < h.spectral_samples):
                raise RuntimeError("Camera ROI, stride or pixel format does not match profile")
            self.height = height
            self._ring = (c_void_p * r.ring_buffers)()
            self._check(self._dll.imgRingSetup(self.session_id, r.ring_buffers, self._ring, 0, 1), "ring setup")
            self.next_buffer = 0
            self.lost_buffers = self._get_u32(IMG_ATTR_LOST_FRAMES)
            if self.lost_buffers:
                raise RuntimeError("Camera already reports lost buffers at session start")
            self.discontinuous_buffers = 0
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup:
                error.add_note(f"Camera cleanup: {cleanup}")
            raise

    def _set_string(self, name, value):
        self._check(self._dll.imgSetCameraAttributeString(self.session_id, name.encode(), value.encode()), name)

    def set_read_timeout(self, milliseconds: int):
        if not 1 <= milliseconds < 0xFFFFFFFF:
            raise ValueError("Frame wait exceeds the NI-IMAQ timeout range")
        timeout = c_uint32(milliseconds)
        self._check(self._dll.imgSetAttributeFromVoidPtr(self.session_id, IMG_ATTR_FRAMEWAIT_MSEC,
                                                       byref(timeout)), "frame timeout")

    def _get_u32(self, attribute):
        value = c_uint32()
        self._check(self._dll.imgGetAttribute(self.session_id, attribute, byref(value)), "attribute read")
        return value.value

    def _check(self, code, operation):
        if code < 0:
            description = ctypes.create_string_buffer(512)
            self._dll.imgShowError(code, description)
            raise RuntimeError(f"NI-IMAQ {operation} ({code}): {description.value.decode(errors='replace')}")

    def read(self):
        if not self.session_id.value:
            raise RuntimeError("Camera is disconnected")
        if self.next_buffer >= 0xFFFFFFFE:
            raise RuntimeError("Camera buffer numbering exhausted; reconnect before another acquisition")
        actual, address = c_uint32(), c_void_p()
        self._check(self._dll.imgSessionExamineBuffer2(self.session_id, self.next_buffer,
                                                     byref(actual), byref(address)), "examine buffer")
        try:
            if actual.value != self.next_buffer:
                self.discontinuous_buffers += 1
                raise RuntimeError(f"Discontinuous camera buffer: {actual.value}, expected {self.next_buffer}")
            if not address.value:
                raise RuntimeError("Null camera buffer")
            source = (ctypes.c_uint16 * (self.height * self.row_pixels)).from_address(address.value)
            data = np.ctypeslib.as_array(source).reshape(self.height, self.row_pixels)[:, :self.profile.spectral_samples].copy()
        finally:
            self._check(self._dll.imgSessionReleaseBuffer(self.session_id), "release buffer")
        lost = self._get_u32(IMG_ATTR_LOST_FRAMES)
        if lost != self.lost_buffers:
            self.lost_buffers = lost
            raise RuntimeError(f"NI-IMAQ reports {lost} lost buffers")
        number = self.next_buffer
        self.next_buffer += 1
        return number, data

    def close(self):
        errors = []
        if self.session_id.value:
            for operation, args in ((self._dll.imgSessionStopAcquisition, (self.session_id,)),
                                    (self._dll.imgClose, (self.session_id, 1))):
                try:
                    self._check(operation(*args), operation.__name__)
                except Exception as error:
                    errors.append(error)
            self.session_id = c_uint32()
        if self.interface_id.value:
            try:
                self._check(self._dll.imgClose(self.interface_id, 1), "interface close")
            except Exception as error:
                errors.append(error)
            self.interface_id = c_uint32()
        self._ring = None
        self.height = 0
        if errors:
            raise ExceptionGroup("Camera cleanup failed", errors)

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
