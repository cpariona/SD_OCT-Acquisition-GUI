from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from ..config import HardwareConfig, ScanParameters
from ..scan import ScanSegment


class BackendError(RuntimeError):
    pass


class FrameIntegrityError(BackendError):
    pass


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    requested_buffer: int
    copied_buffer: int
    physical_ring_index: int
    lost_buffers_total: int
    elapsed_s: float


class AcquisitionBackend(Protocol):
    name: str

    def open(self, scan: ScanParameters, hardware: HardwareConfig) -> None: ...

    def acquire_segment(
        self,
        segment: ScanSegment,
        destination: NDArray[np.uint16],
        stop_event: Event,
    ) -> AcquisitionResult: ...

    def close(self, *, abort: bool = False) -> None: ...
