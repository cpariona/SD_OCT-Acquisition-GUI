from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def reconstruct_oct_complex(
    spectra: NDArray[np.uint16],
    *,
    fft_size: int | None = None,
    background: NDArray[np.floating] | None = None,
    remove_dc: bool = True,
    reverse_spectrum: bool = False,
    wavelength_start_nm: float | None = None,
    wavelength_end_nm: float | None = None,
) -> NDArray[np.complex64]:
    """Create a diagnostic complex OCT reconstruction in ``[depth, A-line]``.

    Optional endpoint-based k resampling is approximate. Quantitative OCE phase
    still requires measured spectrometer calibration and dispersion correction.
    """
    raw = np.asarray(spectra)
    if raw.ndim != 2 or raw.shape[1] < 2:
        raise ValueError("Se esperaba una matriz [A-lines, píxeles].")
    work = raw.astype(np.float32, copy=True)
    if background is not None:
        bg = np.asarray(background, dtype=np.float32)
        if bg.shape != (work.shape[1],):
            raise ValueError("El background debe tener un valor por píxel espectral.")
        work -= bg[None, :]
    elif remove_dc:
        # Standard B-scan DC/background suppression: remove the fixed spectral
        # component shared by the lateral A-lines.  Subtracting a scalar from
        # each A-line would only remove its zero-frequency offset and leaves the
        # stationary spectrometer/camera pattern dominating the preview.
        work -= work.mean(axis=0, keepdims=True)
    if reverse_spectrum:
        work = work[:, ::-1]
    if wavelength_start_nm is not None or wavelength_end_nm is not None:
        if wavelength_start_nm is None or wavelength_end_nm is None:
            raise ValueError("Se requieren ambos extremos de longitud de onda para linealizar k.")
        if wavelength_start_nm <= 0 or wavelength_end_nm <= 0 or wavelength_start_nm == wavelength_end_nm:
            raise ValueError("El rango de longitudes de onda debe ser positivo y no nulo.")
        wavelength = np.linspace(
            wavelength_start_nm,
            wavelength_end_nm,
            work.shape[1],
            dtype=np.float64,
        )
        wavenumber = 2.0 * np.pi / wavelength
        order = np.argsort(wavenumber)
        ordered_k = wavenumber[order]
        uniform_k = np.linspace(ordered_k[0], ordered_k[-1], work.shape[1], dtype=np.float64)
        mapped = np.empty_like(work)
        for row_index, row in enumerate(work):
            mapped[row_index] = np.interp(uniform_k, ordered_k, row[order])
        work = mapped
    work *= np.hanning(work.shape[1]).astype(np.float32)[None, :]
    if fft_size is None:
        fft_size = 1 << int(np.ceil(np.log2(work.shape[1])))
    spectrum = np.fft.rfft(work, n=fft_size, axis=1)
    return np.asarray(spectrum[:, 1 : fft_size // 2 + 1].T, dtype=np.complex64)


def reconstruct_oct_db(
    spectra: NDArray[np.uint16],
    *,
    fft_size: int | None = None,
    background: NDArray[np.floating] | None = None,
    remove_dc: bool = True,
) -> NDArray[np.float32]:
    """Create a diagnostic OCT intensity preview from raw spectra."""
    spectrum = reconstruct_oct_complex(
        spectra,
        fft_size=fft_size,
        background=background,
        remove_dc=remove_dc,
    )
    magnitude = np.abs(spectrum)
    db = 20.0 * np.log10(magnitude + 1.0)
    return np.asarray(db, dtype=np.float32)


def preview_complex(
    spectra: NDArray[np.uint16],
    *,
    max_width: int = 700,
    max_height: int = 500,
    remove_dc: bool = True,
    fft_size: int = 8192,
    depth_bins: int = 2048,
    depth_start_bin: int = 1,
    depth_end_bin: int | None = None,
    wavelength_start_nm: float = 1453.0,
    wavelength_end_nm: float = 1292.69,
    reverse_spectrum: bool = True,
) -> tuple[NDArray[np.float32], NDArray[np.float32], NDArray[np.int64], NDArray[np.int64]]:
    """Return a conventional single-domain SD-OCT preview.

    Defaults use provisional 1310-nm wavelength endpoints: the
    detector row is reversed, resampled uniformly in k, Hann-windowed,
    zero-padded to 8192, and restricted to the usable near-depth domain.  Raw
    acquisition data is never altered.
    """
    raw = np.asarray(spectra)
    col_step = max(1, int(np.ceil(raw.shape[0] / max_width)))
    bounded_raw = np.ascontiguousarray(raw[::col_step])
    spectrum = reconstruct_oct_complex(
        bounded_raw,
        remove_dc=remove_dc,
        fft_size=fft_size,
        reverse_spectrum=reverse_spectrum,
        wavelength_start_nm=wavelength_start_nm,
        wavelength_end_nm=wavelength_end_nm,
    )
    if depth_end_bin is None:
        depth_end_bin = min(depth_bins, spectrum.shape[0])
    if not 1 <= depth_start_bin <= depth_end_bin <= spectrum.shape[0]:
        raise ValueError(
            f"Rango Z inválido: use 1 ≤ inicio ≤ fin ≤ {spectrum.shape[0]} bins FFT."
        )
    # rFFT already selects one conjugate half-space. The default visible window
    # is the near 2048 bins; the user may inspect deeper positive-depth bins.
    spectrum = spectrum[depth_start_bin - 1 : depth_end_bin]
    row_step = max(1, int(np.ceil(spectrum.shape[0] / max_height)))
    bounded = spectrum[::row_step]
    intensity_db = np.asarray(20.0 * np.log10(np.abs(bounded) + 1.0), dtype=np.float32)
    phase_rad = np.asarray(np.angle(bounded), dtype=np.float32)
    # The zero-frequency bin was discarded by reconstruct_oct_complex.
    depth_indexes = np.arange(depth_start_bin, depth_end_bin + 1, row_step, dtype=np.int64)
    aline_indexes = np.arange(bounded_raw.shape[0], dtype=np.int64) * col_step
    return intensity_db, phase_rad, depth_indexes, aline_indexes


def normalize_preview(
    image_db: NDArray[np.floating],
    *,
    low_percentile: float = 2.0,
    high_percentile: float = 99.5,
    max_width: int = 700,
    max_height: int = 500,
) -> NDArray[np.uint8]:
    image = np.asarray(image_db, dtype=np.float32)
    if image.ndim != 2 or image.size == 0:
        raise ValueError("La vista previa debe ser una imagen 2-D no vacía.")
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    lo, hi = np.percentile(finite, (low_percentile, high_percentile))
    if hi <= lo:
        hi = lo + 1.0
    scaled = np.clip((image - lo) * (255.0 / (hi - lo)), 0.0, 255.0).astype(np.uint8)
    row_step = max(1, int(np.ceil(scaled.shape[0] / max_height)))
    col_step = max(1, int(np.ceil(scaled.shape[1] / max_width)))
    return np.ascontiguousarray(scaled[::row_step, ::col_step])


def preview_from_raw(
    spectra: NDArray[np.uint16],
    *,
    remove_dc: bool = True,
) -> NDArray[np.uint8]:
    return normalize_preview(reconstruct_oct_db(spectra, remove_dc=remove_dc))
