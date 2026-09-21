from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
from numpy.typing import NDArray


class _Capture(Protocol):
    def isOpened(self) -> bool: ...
    def read(self) -> tuple[bool, NDArray[np.uint8] | None]: ...
    def release(self) -> None: ...


class USBCameraStream:
    """Best-effort USB video on its own thread; never blocks the NI acquisition."""

    def __init__(self, capture_factory: Callable[[int], _Capture] | None = None) -> None:
        self._capture_factory = capture_factory
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame: NDArray[np.uint8] | None = None
        self._status = "USB desconectada"
        self._index: int | None = None
        self._record_path: Path | None = None
        self._record_writer: object | None = None
        self._record_ready: threading.Event | None = None
        self._record_done = threading.Event()
        self._record_error: str | None = None
        self._record_stop_requested = False
        self._record_frames = 0
        self._last_record_path: Path | None = None
        self._last_record_frames = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._record_path is not None

    @property
    def last_record_error(self) -> str | None:
        with self._lock:
            return self._record_error

    def start_recording(self, path: str | os.PathLike[str], timeout_s: float = 8.0) -> Path:
        """Arm MP4 in the capture thread and wait until its first frame is written."""
        destination = Path(path)
        if destination.suffix.lower() != ".mp4":
            raise ValueError("El video USB debe tener extensión .mp4.")
        if destination.exists():
            raise FileExistsError(f"El video ya existe: {destination}")
        if not self.running:
            raise RuntimeError("La cámara USB no está recibiendo fotogramas.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        ready = threading.Event()
        with self._lock:
            if self._record_path is not None:
                raise RuntimeError("Ya hay una grabación USB activa.")
            self._record_path = destination
            self._record_writer = None
            self._record_ready = ready
            self._record_done.clear()
            self._record_error = None
            self._record_stop_requested = False
            self._record_frames = 0
        if not ready.wait(timeout_s):
            self.stop_recording()
            raise TimeoutError("La cámara USB no escribió un fotograma antes del inicio OCT.")
        with self._lock:
            error = self._record_error
        if error is not None:
            raise RuntimeError(error)
        return destination

    def stop_recording(self, timeout_s: float = 8.0) -> tuple[Path | None, int]:
        with self._lock:
            if self._record_path is None:
                return self._last_record_path, self._last_record_frames
            self._record_stop_requested = True
            done = self._record_done
        if not done.wait(timeout_s):
            raise TimeoutError("No se pudo finalizar el archivo de video USB a tiempo.")
        with self._lock:
            return self._last_record_path, self._last_record_frames

    def _finish_recording(self, error: str | None = None) -> None:
        with self._lock:
            writer = self._record_writer
            path = self._record_path
            frames = self._record_frames
            ready = self._record_ready
            self._record_writer = None
            self._record_path = None
            self._record_ready = None
            self._record_stop_requested = False
            self._record_error = error
            self._last_record_path = path
            self._last_record_frames = frames
        try:
            if writer is not None:
                writer.release()
        except Exception as exc:
            with self._lock:
                self._record_error = f"No se pudo cerrar el video USB: {exc}"
        finally:
            if ready is not None:
                ready.set()
            self._record_done.set()

    def start(self, index: int) -> None:
        if not 0 <= index <= 15:
            raise ValueError("El índice USB debe estar entre 0 y 15.")
        if self.running and self._index == index:
            return
        if not self.stop():
            raise RuntimeError("La cámara USB anterior aún no se ha liberado.")
        self._index = index
        self._stop_event = threading.Event()
        with self._lock:
            self._frame = None
            self._status = f"Abriendo cámara USB {index}…"
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(index, self._stop_event),
            name="octoce-usb-camera",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self._stop_event.set()
        thread.join(timeout_s)
        if thread.is_alive():
            with self._lock:
                self._status = "Esperando liberación de la cámara USB…"
            return False
        self._thread = None
        self._index = None
        with self._lock:
            self._frame = None
            self._status = "USB desconectada"
        return True

    def snapshot(self) -> tuple[NDArray[np.uint8] | None, str]:
        with self._lock:
            frame = self._frame
            self._frame = None
            return frame, self._status

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _capture_loop(self, index: int, stop_event: threading.Event) -> None:
        capture: _Capture | None = None
        try:
            import cv2

            if self._capture_factory is not None:
                capture = self._capture_factory(index)
            else:
                backends = (
                    (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)
                    if os.name == "nt" else (cv2.CAP_ANY,)
                )
                for backend in backends:
                    candidate = cv2.VideoCapture(index, backend)
                    if candidate.isOpened():
                        capture = candidate
                        break
                    candidate.release()
            if capture is None or not capture.isOpened():
                self._set_status(f"No se pudo abrir USB {index}. Pruebe otro índice.")
                return
            failures = 0
            while not stop_event.is_set():
                with self._lock:
                    stop_recording = self._record_stop_requested
                if stop_recording:
                    self._finish_recording()
                ok, frame = capture.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= 20:
                        self._set_status(f"Sin fotogramas de USB {index}. Reintente conectar.")
                        return
                    stop_event.wait(0.05)
                    continue
                failures = 0
                with self._lock:
                    record_path = self._record_path
                    writer = self._record_writer
                if record_path is not None:
                    if writer is None:
                        try:
                            fps = float(capture.get(cv2.CAP_PROP_FPS)) if hasattr(capture, "get") else 0.0
                            if not 1.0 <= fps <= 240.0:
                                fps = 30.0
                            writer = cv2.VideoWriter(
                                str(record_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                fps, (int(frame.shape[1]), int(frame.shape[0])),
                            )
                            if not writer.isOpened():
                                raise RuntimeError("OpenCV no pudo abrir el codificador MP4.")
                            with self._lock:
                                self._record_writer = writer
                        except Exception as exc:
                            self._finish_recording(f"No se pudo iniciar el video USB: {exc}")
                            writer = None
                    if writer is not None:
                        try:
                            writer.write(frame)
                            with self._lock:
                                self._record_frames += 1
                                ready = self._record_ready
                            if ready is not None:
                                ready.set()
                        except Exception as exc:
                            self._finish_recording(f"Fallo escribiendo el video USB: {exc}")
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                with self._lock:
                    self._frame = rgb
                    self._status = f"USB {index} · {rgb.shape[1]}×{rgb.shape[0]}"
        except ImportError:
            self._set_status("Falta OpenCV: instale requirements-usb.txt")
        except Exception as exc:
            self._set_status(f"Error de cámara USB: {exc}")
        finally:
            if self.recording:
                self._finish_recording("La cámara USB dejó de entregar fotogramas.")
            if capture is not None:
                capture.release()
