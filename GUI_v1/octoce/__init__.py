"""OCT/OCE acquisition application.

The public API intentionally keeps acquisition parameters, scan planning and
storage independent from the GUI and NI drivers.  This lets the complete data
path be exercised in simulation before the galvanometers are enabled.
"""

from .config import AcquisitionMode, HardwareConfig, Orientation, ScanParameters, ScanPattern

__all__ = [
    "AcquisitionMode",
    "HardwareConfig",
    "Orientation",
    "ScanParameters",
    "ScanPattern",
]

__version__ = "0.1.0"
