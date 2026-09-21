from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import numpy as np
from PIL import Image, ImageTk

from .engine import EngineEvent, EngineState
from .gui import OCTOCEApp
from .usb_camera import USBCameraStream


class OCTOCEUSBApp(OCTOCEApp):
    """Separate GUI variant: live USB view, retaining the OCT alignment zoom."""

    USB_REFRESH_MS = 66

    def __init__(self, root: tk.Tk, *, usb_stream: USBCameraStream | None = None) -> None:
        self._usb_stream = usb_stream or USBCameraStream()
        self._usb_last_frame: np.ndarray | None = None
        self._usb_photo: ImageTk.PhotoImage | None = None
        self._usb_after: str | None = None
        self._video_active_path: Path | None = None
        super().__init__(root)
        self.root.title("OCT / OCE Acquisition · USB")
        target_width = max(1040, min(1520, self.root.winfo_screenwidth() - 40))
        target_height = max(680, min(900, self.root.winfo_screenheight() - 80))
        self.root.geometry(f"{target_width}x{target_height}")
        self._connect_usb()
        self._poll_usb()

    def _build_previews(self, parent: ttk.Frame) -> None:
        super()._build_previews(parent)
        image_area = self.image_canvas.master
        self.usb_panel = ttk.Frame(image_area, style="Card.TFrame", width=250)
        self.usb_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.usb_panel.columnconfigure(0, weight=1)
        self.usb_panel.rowconfigure(1, weight=1)

        controls = ttk.Frame(self.usb_panel, style="Card.TFrame")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(controls, text="Cámara USB", style="Card.TLabel").pack(side="left")
        self.usb_index_var = tk.StringVar(value="0")
        selector = ttk.Combobox(
            controls,
            textvariable=self.usb_index_var,
            values=tuple(str(index) for index in range(6)),
            state="readonly",
            width=3,
        )
        selector.pack(side="left", padx=(7, 4))
        self.usb_selector = selector
        selector.bind("<<ComboboxSelected>>", lambda _event: self._connect_usb())
        self.usb_connect_button = ttk.Button(controls, text="Conectar", command=self._connect_usb)
        self.usb_connect_button.pack(side="left")

        self.record_video_var = tk.BooleanVar(value=False)
        self.record_video_check = ttk.Checkbutton(
            self.usb_panel, text="Grabar video USB al adquirir",
            variable=self.record_video_var,
        )
        self.record_video_check.grid(row=1, column=0, sticky="w", pady=(3, 5))

        self.usb_canvas = tk.Canvas(
            self.usb_panel,
            width=250,
            bg="#07111f",
            highlightthickness=1,
            highlightbackground="#d5dde8",
        )
        self.usb_canvas.grid(row=2, column=0, sticky="nsew")
        self.usb_canvas.bind("<Configure>", lambda _event: self._render_usb())
        self.usb_status_var = tk.StringVar(value="USB desconectada")
        ttk.Label(
            self.usb_panel,
            textvariable=self.usb_status_var,
            style="Muted.TLabel",
            wraplength=240,
        ).grid(row=3, column=0, sticky="w", pady=(5, 0))
        self._render_usb()

    def _connect_usb(self) -> None:
        if self._alignment_active or self._closing or self._usb_stream.recording:
            return
        try:
            index = int(self.usb_index_var.get())
            self._usb_last_frame = None
            self._usb_photo = None
            self._usb_stream.start(index)
            self.usb_status_var.set(f"Abriendo cámara USB {index}…")
            self._render_usb()
        except (RuntimeError, ValueError) as exc:
            self.usb_status_var.set(str(exc))
            self._render_usb()

    def _video_path(self, output: Path | None) -> Path:
        if output is not None:
            candidate = output.with_name(output.stem + "_usb.mp4")
        else:
            folder = Path(self.output_var.get()).parent
            if str(folder) in ("", "."):
                folder = Path.cwd() / "data"
            candidate = folder / ("USB_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4")
        base = candidate.with_suffix("")
        number = 2
        while candidate.exists():
            candidate = base.with_name(base.name + f"_{number}").with_suffix(".mp4")
            number += 1
        return candidate

    def _before_oct_start(self, output: Path | None) -> None:
        if not self.record_video_var.get():
            return
        index = int(self.usb_index_var.get())
        if not self._usb_stream.running:
            self._usb_stream.start(index)
        path = self._video_path(output)
        self._usb_stream.start_recording(path)
        self._video_active_path = path
        self.record_video_check.configure(state="disabled")
        self.usb_selector.configure(state="disabled")
        self.usb_connect_button.configure(state="disabled")
        self._append_log(f"Video USB iniciado antes de OCT: {path}")

    def _stop_usb_video(self) -> None:
        if self._video_active_path is None:
            return
        try:
            path, frames = self._usb_stream.stop_recording()
            self._append_log(f"Video USB finalizado: {path} · {frames} fotogramas.")
            if self._usb_stream.last_record_error:
                self._append_log(f"Advertencia de video USB: {self._usb_stream.last_record_error}")
        except Exception as exc:
            self._append_log(f"Error al finalizar video USB: {exc}")
        finally:
            self._video_active_path = None
            self.record_video_check.configure(state="normal")
            self.usb_selector.configure(state="readonly")
            self.usb_connect_button.configure(state="normal")

    def _on_oct_start_failed(self) -> None:
        self._stop_usb_video()

    def _clear_preview(self, message: str) -> None:
        super()._clear_preview(message)
        if self._alignment_active:
            self.usb_panel.grid_remove()
            self._usb_stream.stop()
        else:
            self.usb_panel.grid()
            if not self._usb_stream.running:
                self._connect_usb()

    def _start_alignment(self) -> None:
        super()._start_alignment()
        # The base action handles validation/confirmation internally. If it
        # returns without starting, keep the USB view available.
        if not self._alignment_active and not self._closing:
            self.usb_panel.grid()
            self._connect_usb()

    def _handle_event(self, event: EngineEvent) -> None:
        super()._handle_event(event)
        if event.kind == "state" and event.payload.get("state") in {
            EngineState.COMPLETED.value, EngineState.STOPPED.value, EngineState.ERROR.value,
        }:
            self._stop_usb_video()
        if (
            event.kind == "state"
            and self._alignment_active
            and event.payload.get("state") in {
                EngineState.COMPLETED.value,
                EngineState.STOPPED.value,
                EngineState.ERROR.value,
            }
            and not self._closing
        ):
            self._alignment_active = False
            self.alignment_zoom_canvas.grid_remove()
            self.usb_panel.grid()
            self._connect_usb()

    def _poll_usb(self) -> None:
        if self._closing or not self.root.winfo_exists():
            return
        frame, status = self._usb_stream.snapshot()
        self.usb_status_var.set(status)
        if frame is not None and not self._alignment_active:
            self._usb_last_frame = frame
            self._render_usb()
        self._usb_after = self.root.after(self.USB_REFRESH_MS, self._poll_usb)

    def _render_usb(self) -> None:
        canvas = self.usb_canvas
        canvas.delete("all")
        frame = self._usb_last_frame
        if frame is None:
            canvas.create_text(
                14, 18, anchor="nw", fill="#9bb2cf", font=("Segoe UI", 10),
                width=max(100, canvas.winfo_width() - 28),
                text=self.usb_status_var.get(),
            )
            return
        height, width = frame.shape[:2]
        canvas_width = max(1, canvas.winfo_width())
        canvas_height = max(1, canvas.winfo_height())
        scale = min(canvas_width / width, canvas_height / height)
        display_width = max(1, int(width * scale))
        display_height = max(1, int(height * scale))
        pil = Image.fromarray(frame, mode="RGB")
        pil = pil.resize((display_width, display_height), Image.Resampling.BILINEAR)
        self._usb_photo = ImageTk.PhotoImage(pil)
        canvas.create_image(
            canvas_width / 2, canvas_height / 2,
            image=self._usb_photo, anchor="center",
        )

    def _on_close(self) -> None:
        super()._on_close()

    def _before_root_destroy(self) -> None:
        self._stop_usb_video()
        self._usb_stream.stop()
        super()._before_root_destroy()
        if self._usb_after is not None:
            self.root.after_cancel(self._usb_after)
            self._usb_after = None


def main() -> None:
    root = tk.Tk()
    OCTOCEUSBApp(root)
    root.mainloop()
