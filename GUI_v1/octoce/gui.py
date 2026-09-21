from __future__ import annotations

import queue
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import numpy as np
from PIL import Image, ImageTk

from .backends.ni_hardware import NIHardwareBackend
from .backends.simulated import SimulatedBackend
from .config import (
    AcquisitionMode,
    ConfigurationError,
    HardwareConfig,
    OCE_TRIGGER_DUTY_CYCLE,
    Orientation,
    ScanParameters,
    ScanPattern,
    estimate_payload_bytes,
    stationary_alignment_timing,
)
from .engine import AcquisitionEngine, EngineEvent, EngineState
from .processing import normalize_preview, preview_complex


PATTERN_LABELS = {
    "Raster": ScanPattern.RASTER,
    "Crosshair": ScanPattern.CROSSHAIR,
    "Meridianos (polar)": ScanPattern.MERIDIANS,
    "Lineal": ScanPattern.LINEAR,
}
ORIENTATION_LABELS = {
    "Horizontal": Orientation.HORIZONTAL,
    "Vertical": Orientation.VERTICAL,
}
MODE_LABELS = {
    "BM-mode  (B → M → A)": AcquisitionMode.BM,
    "MB-mode  (B → A → M)": AcquisitionMode.MB,
}
COLORMAPS = ("Inferno", "Viridis", "Magma", "Turbo", "Grises")
_COLORMAP_ANCHORS: dict[str, tuple[tuple[float, tuple[int, int, int]], ...]] = {
    "Inferno": (
        (0.00, (0, 0, 4)), (0.22, (66, 10, 104)), (0.45, (147, 38, 103)),
        (0.68, (221, 81, 58)), (0.86, (252, 165, 10)), (1.00, (252, 255, 164)),
    ),
    "Viridis": (
        (0.00, (68, 1, 84)), (0.25, (59, 82, 139)), (0.50, (33, 145, 140)),
        (0.75, (94, 201, 98)), (1.00, (253, 231, 37)),
    ),
    "Magma": (
        (0.00, (0, 0, 4)), (0.25, (81, 18, 124)), (0.50, (183, 55, 121)),
        (0.75, (252, 137, 97)), (1.00, (252, 253, 191)),
    ),
    "Turbo": (
        (0.00, (48, 18, 59)), (0.20, (50, 104, 230)), (0.40, (30, 190, 164)),
        (0.60, (174, 236, 52)), (0.80, (249, 126, 21)), (1.00, (122, 4, 3)),
    ),
    "Grises": ((0.00, (0, 0, 0)), (1.00, (255, 255, 255))),
}


class _ScrollablePanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, width: int | None = None):
        super().__init__(parent, style="Card.TFrame")
        self.canvas = tk.Canvas(
            self,
            bg="#ffffff",
            highlightthickness=0,
            width=width,
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas, style="Card.TFrame", padding=16)
        self._window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _sync_scroll_region(self, _event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self._window_id, width=event.width)

    def _wheel(self, event: tk.Event[Any]) -> None:
        self.canvas.yview_scroll(-int(event.delta / 120), "units")


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TiB"


def _duration(seconds: float | None) -> str:
    if seconds is None or not np.isfinite(seconds):
        return "—"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class OCTOCEApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("OCT / OCE Acquisition")
        self.root.geometry("1360x860")
        self.root.minsize(1040, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.event_queue: queue.Queue[EngineEvent] = queue.Queue()
        self.engine = AcquisitionEngine(self.event_queue.put)
        self.hardware = HardwareConfig()
        self._closing = False
        self._trajectory_after: str | None = None
        self._events_after: str | None = None
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._preview_photo_secondary: ImageTk.PhotoImage | None = None
        self._alignment_zoom_photo: ImageTk.PhotoImage | None = None
        self._preview_db: np.ndarray | None = None
        self._preview_phase: np.ndarray | None = None
        self._secondary_db: np.ndarray | None = None
        self._secondary_phase: np.ndarray | None = None
        self._secondary_source_spectra: np.ndarray | None = None
        self._secondary_aline_indexes = np.empty(0, dtype=np.int64)
        self._preview_depth_indexes = np.empty(0, dtype=np.int64)
        self._preview_aline_indexes = np.empty(0, dtype=np.int64)
        self._preview_source_spectra: np.ndarray | None = None
        self._preview_sweep_index: int | None = None
        self._active_scan: ScanParameters | None = None
        self._active_hardware: HardwareConfig | None = None
        self._image_bounds: tuple[float, float, float, float] | None = None
        self._image_bounds_secondary: tuple[float, float, float, float] | None = None
        self._alignment_active = False
        self._crosshair_loop_active = False
        self._last_depth_range = (1, 2048)
        self._last_db_limits = (30.0, 90.0)
        self._depth_after: str | None = None
        self._status_color = "#718096"

        self._build_style()
        self._build_variables()
        self._build_ui()
        self._bind_plan_updates()
        self._refresh_plan()
        self._events_after = self.root.after(50, self._poll_events)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(bg="#eef2f7")
        style.configure("TFrame", background="#eef2f7")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Header.TLabel", background="#10233f", foreground="#ffffff", font=("Segoe UI", 19, "bold"))
        style.configure("Subheader.TLabel", background="#10233f", foreground="#b8c8df", font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#14243c", font=("Segoe UI", 11, "bold"))
        style.configure("Card.TLabel", background="#ffffff", foreground="#25364e", font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6a788b", font=("Segoe UI", 8))
        style.configure("Metric.TLabel", background="#ffffff", foreground="#10233f", font=("Consolas", 9))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(16, 9))
        style.configure("Danger.TButton", font=("Segoe UI", 10, "bold"), padding=(16, 9))
        style.configure("TButton", font=("Segoe UI", 9), padding=(9, 6))
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=4)
        style.configure("Horizontal.TProgressbar", troughcolor="#dfe6ee", background="#2477d4")

    def _build_variables(self) -> None:
        self.mode_var = tk.StringVar(value=next(k for k, v in MODE_LABELS.items() if v is AcquisitionMode.BM))
        self.pattern_var = tk.StringVar(value="Raster")
        self.orientation_var = tk.StringVar(value="Horizontal")
        self.alines_var = tk.StringVar(value="512")
        self.bscans_var = tk.StringVar(value="64")
        self.repetitions_var = tk.StringVar(value="1")
        self.sync_var = tk.StringVar(value="50")
        self.bframes_delay_var = tk.StringVar(value="0.0")
        self.x_length_var = tk.StringVar(value="5.0")
        self.y_length_var = tk.StringVar(value="5.0")
        self.k_start_var = tk.StringVar(value=str(self.hardware.k_start_nm))
        self.k_end_var = tk.StringVar(value=str(self.hardware.k_end_nm))
        self.backend_var = tk.StringVar(value="Simulación")
        self.save_var = tk.BooleanVar(value=True)
        default_name = datetime.now().strftime("OCTOCE_%Y%m%d_%H%M%S.bin")
        self.output_var = tk.StringVar(value=str(Path.cwd() / "data" / default_name))
        self.summary_var = tk.StringVar(value="")
        self.validation_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Listo")
        self.progress_text_var = tk.StringVar(value="0 / 0 A-lines")
        self.diagnostics_var = tk.StringVar(value="Buffer —  ·  Perdidos 0  ·  Cola 0")
        self.eta_var = tk.StringVar(value="ETA —  ·  0.00 MiB/s")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.colormap_var = tk.StringVar(value="Grises")
        self.remove_dc_var = tk.BooleanVar(value=True)
        self.black_percentile_var = tk.DoubleVar(value=2.0)
        self.white_percentile_var = tk.DoubleVar(value=99.5)
        self.black_text_var = tk.StringVar(value="2.0 %")
        self.white_text_var = tk.StringVar(value="99.5 %")
        self.display_db_var = tk.BooleanVar(value=False)
        self.black_db_var = tk.StringVar(value="30")
        self.white_db_var = tk.StringVar(value="90")
        self.display_limits_status_var = tk.StringVar(value="")
        self.depth_start_var = tk.StringVar(value="1")
        self.depth_end_var = tk.StringVar(value="2048")
        self.depth_limits_status_var = tk.StringVar(value="Z visible: 1–2048 / 4096 bins")
        self.z_cursor_var = tk.DoubleVar(value=0.0)
        self.lateral_cursor_var = tk.DoubleVar(value=0.0)
        self.z_cursor_text_var = tk.StringVar(value="Z: —")
        self.lateral_cursor_text_var = tk.StringVar(value="X/Y: —")
        self.phase_title_var = tk.StringVar(value="Fase relativa en profundidad seleccionada")
        self.phase_auto_var = tk.BooleanVar(value=False)
        self.phase_min_var = tk.StringVar(value="-15")
        self.phase_max_var = tk.StringVar(value="15")

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#10233f", height=92)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_group = tk.Frame(header, bg="#10233f")
        title_group.pack(side="left", padx=24, pady=(13, 11))
        ttk.Label(title_group, text="OCT / OCE Acquisition", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_group,
            text="PCIe-6323 · PCIe-1433 · sincronización por hardware",
            style="Subheader.TLabel",
        ).pack(anchor="w")
        status_frame = tk.Frame(header, bg="#10233f")
        status_frame.pack(side="right", padx=24)
        self.status_dot = tk.Canvas(status_frame, width=14, height=14, bg="#10233f", highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_dot_id = self.status_dot.create_oval(2, 2, 12, 12, fill=self._status_color, outline="")
        tk.Label(
            status_frame,
            textvariable=self.status_var,
            bg="#10233f",
            fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        pane = ttk.Panedwindow(outer, orient="horizontal")
        pane.pack(fill="both", expand=True)

        controls_shell = _ScrollablePanel(pane, width=370)
        previews = ttk.Frame(pane)
        pane.add(controls_shell, weight=0)
        pane.add(previews, weight=1)
        self._build_controls(controls_shell.inner)
        self._build_previews(previews)

    def _build_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Plan de adquisición", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        parent.columnconfigure(1, weight=1)
        row = 1

        def combo(label: str, variable: tk.StringVar, values: list[str]) -> ttk.Combobox:
            nonlocal row
            ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=24)
            widget.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
            row += 1
            return widget

        def entry(label: str, variable: tk.StringVar, suffix: str = "") -> ttk.Entry:
            nonlocal row
            ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            holder = ttk.Frame(parent, style="Card.TFrame")
            holder.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
            holder.columnconfigure(0, weight=1)
            widget = ttk.Entry(holder, textvariable=variable, width=15)
            widget.grid(row=0, column=0, sticky="ew")
            if suffix:
                ttk.Label(holder, text=suffix, style="Muted.TLabel").grid(row=0, column=1, padx=(6, 0))
            row += 1
            return widget

        self.mode_combo = combo("Modo", self.mode_var, list(MODE_LABELS))
        self.pattern_combo = combo("Patrón", self.pattern_var, list(PATTERN_LABELS))
        self.orientation_combo = combo("Orientación lineal", self.orientation_var, list(ORIENTATION_LABELS))
        entry("Cantidad de A-lines", self.alines_var)
        entry("Cantidad de B-scans", self.bscans_var)
        entry("M repeticiones", self.repetitions_var)
        entry("Puntos sync", self.sync_var)
        entry("BFramesDelay", self.bframes_delay_var, "µs")
        entry("Longitud X", self.x_length_var, "mm")
        entry("Longitud Y", self.y_length_var, "mm")
        entry("Inicio λ para k", self.k_start_var, "nm")
        entry("Fin λ para k", self.k_end_var, "nm")

        ttk.Separator(parent).grid(row=row, column=0, columnspan=2, sticky="ew", pady=12)
        row += 1
        combo("Ejecución", self.backend_var, ["Simulación", "Hardware NI"])
        ttk.Checkbutton(
            parent,
            text="Guardar datos crudos (.bin), sin preview",
            variable=self.save_var,
            command=self._save_changed,
        ).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(6, 4)
        )
        row += 1
        path_holder = ttk.Frame(parent, style="Card.TFrame")
        path_holder.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        path_holder.columnconfigure(0, weight=1)
        ttk.Entry(path_holder, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(path_holder, text="…", width=4, command=self._browse_output).grid(row=0, column=1, padx=(6, 0))
        row += 1

        ttk.Button(parent, text="Configuración de hardware…", command=self._open_hardware_dialog).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4)
        )
        row += 1
        info = ttk.Frame(parent, style="Card.TFrame", padding=(0, 8))
        info.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(
            info,
            textvariable=self.summary_var,
            style="Metric.TLabel",
            justify="left",
            wraplength=325,
        ).pack(anchor="w")
        ttk.Label(
            info,
            textvariable=self.validation_var,
            style="Muted.TLabel",
            wraplength=325,
            justify="left",
        ).pack(anchor="w", pady=(5, 0))
        row += 1
        ttk.Label(
            parent,
            text=(
                "BM: repite el B-scan completo (B→M→A). MB: adquiere M veces cada posición "
                "antes de mover (B→A→M). BFramesDelay desplaza PFI13 respecto al primer PFI12."
            ),
            style="Muted.TLabel",
            wraplength=330,
            justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 12))
        row += 1

        buttons = ttk.Frame(parent, style="Card.TFrame")
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        buttons.columnconfigure((0, 1), weight=1)
        self.start_button = ttk.Button(
            buttons, text="Iniciar adquisición", style="Primary.TButton", command=self._start
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = ttk.Button(
            buttons, text="Detener", style="Danger.TButton", command=self._stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.align_button = ttk.Button(
            parent,
            text="Alineación continua · centro (MB × 1000)",
            command=self._start_alignment,
        )
        self.align_button.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.crosshair_loop_button = ttk.Button(
            parent,
            text="Crosshair continuo · BM · 500 X + 500 Y · 10×10 mm",
            command=self._start_crosshair_loop,
        )
        self.crosshair_loop_button.grid(
            row=row + 2, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )

    def _build_previews(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)
        preview_card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        preview_card.grid(row=0, column=0, sticky="nsew", padx=(14, 0), pady=(0, 8))
        preview_card.columnconfigure(0, weight=1)
        preview_card.rowconfigure(2, weight=1)
        title_line = ttk.Frame(preview_card, style="Card.TFrame")
        title_line.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(title_line, text="Vista OCT en vivo", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(
            title_line,
            text="Preview diagnóstico · raw siempre preservado",
            style="Muted.TLabel",
        ).pack(side="right")

        display_controls = ttk.Frame(preview_card, style="Card.TFrame")
        display_controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        display_controls.columnconfigure(1, weight=1)
        display_controls.columnconfigure(4, weight=1)
        ttk.Label(display_controls, text="Colormap", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        cmap_combo = ttk.Combobox(
            display_controls,
            textvariable=self.colormap_var,
            values=COLORMAPS,
            state="readonly",
            width=10,
        )
        cmap_combo.grid(row=0, column=1, sticky="w", padx=(6, 18), pady=(0, 5))
        cmap_combo.bind("<<ComboboxSelected>>", self._display_settings_changed)
        ttk.Checkbutton(
            display_controls,
            text="Remover DC",
            variable=self.remove_dc_var,
            command=self._dc_removal_changed,
        ).grid(row=0, column=2, columnspan=4, sticky="w", padx=(0, 18), pady=(0, 5))
        ttk.Label(display_controls, text="Negro", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Scale(
            display_controls,
            from_=0.0,
            to=30.0,
            variable=self.black_percentile_var,
            command=self._display_settings_changed,
        ).grid(row=1, column=1, sticky="ew", padx=(6, 5))
        ttk.Label(display_controls, textvariable=self.black_text_var, style="Muted.TLabel", width=7).grid(
            row=1, column=2, sticky="w"
        )
        ttk.Label(display_controls, text="Blanco", style="Card.TLabel").grid(
            row=1, column=3, sticky="w", padx=(10, 0)
        )
        ttk.Scale(
            display_controls,
            from_=70.0,
            to=100.0,
            variable=self.white_percentile_var,
            command=self._display_settings_changed,
        ).grid(row=1, column=4, sticky="ew", padx=(6, 5))
        ttk.Label(display_controls, textvariable=self.white_text_var, style="Muted.TLabel", width=7).grid(
            row=1, column=5, sticky="w"
        )
        ttk.Checkbutton(
            display_controls,
            text="Límites en dB",
            variable=self.display_db_var,
            command=self._display_settings_changed,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(display_controls, text="Negro dB", style="Card.TLabel").grid(row=2, column=2, sticky="e")
        ttk.Entry(display_controls, textvariable=self.black_db_var, width=8).grid(
            row=2, column=3, sticky="w", padx=(5, 12)
        )
        ttk.Label(display_controls, text="Blanco dB", style="Card.TLabel").grid(row=2, column=4, sticky="e")
        ttk.Entry(display_controls, textvariable=self.white_db_var, width=8).grid(
            row=2, column=5, sticky="w", padx=(5, 0)
        )
        ttk.Label(
            display_controls,
            textvariable=self.display_limits_status_var,
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=6, sticky="w")
        self.black_db_var.trace_add("write", lambda *_args: self._display_settings_changed())
        self.white_db_var.trace_add("write", lambda *_args: self._display_settings_changed())
        image_area = ttk.Frame(preview_card, style="Card.TFrame")
        image_area.grid(row=2, column=0, sticky="nsew")
        image_area.columnconfigure(0, weight=1)
        image_area.rowconfigure(0, weight=1)
        self.image_canvas = tk.Canvas(
            image_area,
            bg="#07111f",
            highlightthickness=1,
            highlightbackground="#d5dde8",
        )
        self.image_canvas.grid(row=0, column=0, sticky="nsew")
        self.image_canvas.bind("<Button-1>", self._select_preview_point)
        self.image_canvas.bind("<Configure>", lambda _event: self._render_preview())
        self.image_canvas.create_text(
            24,
            24,
            anchor="nw",
            fill="#7f93ad",
            font=("Segoe UI", 11),
            text="La reconstrucción aparecerá al iniciar la adquisición.",
            tags="placeholder",
        )
        self.alignment_zoom_canvas = tk.Canvas(
            image_area,
            width=250,
            bg="#07111f",
            highlightthickness=1,
            highlightbackground="#d5dde8",
        )
        self.alignment_zoom_canvas.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.alignment_zoom_canvas.grid_remove()
        self.alignment_zoom_canvas.bind("<Configure>", lambda _event: self._render_alignment_zoom())

        cursor_controls = ttk.Frame(preview_card, style="Card.TFrame")
        cursor_controls.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        cursor_controls.columnconfigure(1, weight=1)
        cursor_controls.columnconfigure(4, weight=1)
        ttk.Label(cursor_controls, textvariable=self.z_cursor_text_var, style="Card.TLabel", width=12).grid(
            row=0, column=0, sticky="w"
        )
        self.z_cursor_scale = ttk.Scale(
            cursor_controls,
            from_=0,
            to=1,
            variable=self.z_cursor_var,
            command=self._cursor_changed,
        )
        self.z_cursor_scale.grid(row=0, column=1, sticky="ew", padx=(5, 16))
        ttk.Label(cursor_controls, text="Profundidad FFT", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 18)
        )
        ttk.Label(
            cursor_controls,
            textvariable=self.lateral_cursor_text_var,
            style="Card.TLabel",
            width=16,
        ).grid(row=0, column=3, sticky="w")
        self.lateral_cursor_scale = ttk.Scale(
            cursor_controls,
            from_=0,
            to=1,
            variable=self.lateral_cursor_var,
            command=self._cursor_changed,
        )
        self.lateral_cursor_scale.grid(row=0, column=4, sticky="ew", padx=(5, 0))
        ttk.Label(cursor_controls, text="Z inicio", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(5, 0)
        )
        ttk.Entry(cursor_controls, textvariable=self.depth_start_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        ttk.Label(cursor_controls, text="Z fin", style="Card.TLabel").grid(
            row=1, column=2, sticky="w", pady=(5, 0)
        )
        ttk.Entry(cursor_controls, textvariable=self.depth_end_var, width=8).grid(
            row=1, column=3, sticky="w", pady=(5, 0)
        )
        ttk.Label(cursor_controls, textvariable=self.depth_limits_status_var, style="Muted.TLabel").grid(
            row=1, column=4, sticky="w", padx=(5, 0), pady=(5, 0)
        )
        self.depth_start_var.trace_add("write", lambda *_args: self._depth_range_changed())
        self.depth_end_var.trace_add("write", lambda *_args: self._depth_range_changed())

        lower = ttk.Panedwindow(parent, orient="horizontal")
        lower.grid(row=1, column=0, sticky="nsew", padx=(14, 0), pady=(8, 0))
        phase_card = ttk.Frame(lower, style="Card.TFrame", padding=12)
        diagnostic_card = ttk.Frame(lower, style="Card.TFrame", padding=12)
        lower.add(phase_card, weight=3)
        lower.add(diagnostic_card, weight=2)
        phase_card.columnconfigure(0, weight=1)
        phase_card.columnconfigure(1, weight=0)
        phase_card.rowconfigure(1, weight=1)
        ttk.Label(phase_card, textvariable=self.phase_title_var, style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.phase_canvas = tk.Canvas(
            phase_card,
            height=90,
            bg="#fbfcfe",
            highlightthickness=1,
            highlightbackground="#d5dde8",
        )
        self.phase_canvas.grid(row=1, column=0, sticky="nsew")
        self.phase_canvas.bind("<Configure>", lambda _event: self._draw_phase_profile())
        self.cross_canvas = tk.Canvas(
            phase_card,
            width=170,
            bg="#07111f",
            highlightthickness=1,
            highlightbackground="#d5dde8",
        )
        self.cross_canvas.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        self.cross_canvas.grid_remove()
        self.cross_canvas.bind("<Configure>", lambda _event: self._draw_cross_map())
        self.phase_canvas.create_text(
            18,
            18,
            anchor="nw",
            fill="#68788d",
            font=("Segoe UI", 9),
            text="La fase aparecerá con el primer B-scan.",
        )
        phase_limits = ttk.Frame(phase_card, style="Card.TFrame")
        phase_limits.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(
            phase_limits,
            text="Límites Y auto",
            variable=self.phase_auto_var,
            command=self._draw_phase_profile,
        ).pack(side="left")
        ttk.Label(phase_limits, text="Mín", style="Card.TLabel").pack(side="left", padx=(12, 4))
        ttk.Entry(phase_limits, textvariable=self.phase_min_var, width=8).pack(side="left")
        ttk.Label(phase_limits, text="Máx", style="Card.TLabel").pack(side="left", padx=(10, 4))
        ttk.Entry(phase_limits, textvariable=self.phase_max_var, width=8).pack(side="left")
        self.phase_min_var.trace_add("write", lambda *_args: self._draw_phase_profile())
        self.phase_max_var.trace_add("write", lambda *_args: self._draw_phase_profile())

        diagnostic_card.columnconfigure(0, weight=1)
        ttk.Label(diagnostic_card, text="Adquisición e integridad", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )
        ttk.Progressbar(
            diagnostic_card,
            variable=self.progress_var,
            maximum=100.0,
            style="Horizontal.TProgressbar",
        ).grid(row=1, column=0, sticky="ew")
        ttk.Label(diagnostic_card, textvariable=self.progress_text_var, style="Metric.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 2)
        )
        ttk.Label(diagnostic_card, textvariable=self.diagnostics_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=2
        )
        ttk.Label(diagnostic_card, textvariable=self.eta_var, style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", pady=2
        )
        self.log = tk.Text(
            diagnostic_card,
            height=3,
            state="disabled",
            bg="#f5f7fa",
            fg="#3b4a5f",
            relief="flat",
            font=("Consolas", 8),
            wrap="word",
        )
        self.log.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        diagnostic_card.rowconfigure(5, weight=1)

    def _bind_plan_updates(self) -> None:
        variables = (
            self.mode_var,
            self.pattern_var,
            self.orientation_var,
            self.alines_var,
            self.bscans_var,
            self.repetitions_var,
            self.sync_var,
            self.bframes_delay_var,
            self.x_length_var,
            self.y_length_var,
            self.k_start_var,
            self.k_end_var,
        )
        for variable in variables:
            variable.trace_add("write", lambda *_args: self._schedule_plan_refresh())

    def _schedule_plan_refresh(self) -> None:
        if self._trajectory_after is not None:
            self.root.after_cancel(self._trajectory_after)
        self._trajectory_after = self.root.after(180, self._refresh_plan)

    def _configs(self) -> tuple[ScanParameters, HardwareConfig]:
        scan = ScanParameters(
            alines=int(self.alines_var.get()),
            bscans=int(self.bscans_var.get()),
            m_repetitions=int(self.repetitions_var.get()),
            sync_points=int(self.sync_var.get()),
            bframes_delay_us=float(self.bframes_delay_var.get().replace(",", ".")),
            x_length_mm=float(self.x_length_var.get().replace(",", ".")),
            y_length_mm=float(self.y_length_var.get().replace(",", ".")),
            mode=MODE_LABELS[self.mode_var.get()],
            pattern=PATTERN_LABELS[self.pattern_var.get()],
            orientation=ORIENTATION_LABELS[self.orientation_var.get()],
        )
        scan.validate()
        hardware = replace(
            self.hardware,
            k_start_nm=float(self.k_start_var.get().replace(",", ".")),
            k_end_nm=float(self.k_end_var.get().replace(",", ".")),
        )
        hardware.validate(scan)
        return scan, hardware

    def _refresh_plan(self) -> None:
        self._trajectory_after = None
        self.orientation_combo.configure(
            state="readonly" if self.pattern_var.get() == "Lineal" else "disabled"
        )
        try:
            scan, hardware = self._configs()
            size = estimate_payload_bytes(scan, hardware)
            vx = scan.x_length_mm * hardware.x_v_per_mm
            vy = scan.y_length_mm * hardware.y_v_per_mm
            duration_s = (
                scan.expected_alines
                + scan.total_segments * scan.sync_points
                + scan.oce_trigger_segments * hardware.oce_post_hold_points(scan)
            ) / hardware.effective_line_rate_hz
            active_sweep_ms = scan.lines_per_segment / hardware.effective_line_rate_hz * 1000.0
            self.summary_var.set(
                f"{scan.expected_alines:,} A-lines  ·  {_human_bytes(size)}\n"
                f"Vpp X/Y: {vx:.4f} / {vy:.4f} V  ·  "
                f"{hardware.effective_line_rate_hz / 1000:.3f} klps efectivos  ·  "
                f"barrido activo {active_sweep_ms:.2f} ms  ·  mínimo {_duration(duration_s)}"
            )
            warnings = hardware.safety_warnings(scan)
            scan_note = (
                "Lineal bidireccional: el sentido alterna entre barridos. "
                if scan.pattern is ScanPattern.LINEAR and not scan.is_stationary else ""
            )
            if warnings:
                self.validation_var.set(
                    "Plan válido · " + scan_note + "Advertencia: " + " · ".join(warnings)
                )
            else:
                self.validation_var.set(
                    "Plan válido. " + scan_note
                    + "Los puntos sync mueven AO sin trigger PFI12."
                )
            if not self.engine.is_active:
                self.start_button.configure(state="normal")
        except (ValueError, KeyError, ConfigurationError) as exc:
            self.summary_var.set("Plan incompleto")
            self.validation_var.set(str(exc))
            self.start_button.configure(state="disabled")

    def _browse_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Guardar adquisición OCT/OCE",
            defaultextension=".bin",
            filetypes=(("OCT/OCE binario", "*.bin"), ("Todos", "*.*")),
            initialfile=Path(self.output_var.get()).name,
            initialdir=Path(self.output_var.get()).parent,
        )
        if selected:
            self.output_var.set(selected)

    def _save_changed(self) -> None:
        if self.save_var.get() and not self.engine.is_active:
            self._clear_preview("Guardado crudo activo: reconstrucción y preview deshabilitados.")

    def _clear_preview(self, message: str) -> None:
        self._preview_db = None
        self._preview_phase = None
        self._preview_source_spectra = None
        self._secondary_db = None
        self._secondary_phase = None
        self._secondary_source_spectra = None
        self._preview_photo = None
        self._preview_photo_secondary = None
        self._alignment_zoom_photo = None
        self.image_canvas.delete("all")
        self.image_canvas.create_text(
            24, 24, anchor="nw", fill="#7f93ad", font=("Segoe UI", 11),
            text=message,
        )
        self.phase_canvas.delete("all")
        self.phase_canvas.create_text(
            18, 18, anchor="nw", fill="#68788d", font=("Segoe UI", 9), text=message,
        )
        self.cross_canvas.grid_remove()
        self.alignment_zoom_canvas.grid_remove()

    def _before_oct_start(self, output: Path | None) -> None:
        """Extension point for optional USB video pre-roll."""

    def _on_oct_start_failed(self) -> None:
        """Release auxiliary capture if OCT did not start."""

    def _before_root_destroy(self) -> None:
        """Release auxiliary devices before closing Tk."""

    def _start(self) -> None:
        try:
            if self.engine.is_active:
                raise RuntimeError("Hay una adquisición en curso; deténgala antes de iniciar otra.")
            scan, hardware = self._configs()
            depth_range = self._parse_depth_range()
            self._last_depth_range = depth_range
            output = Path(self.output_var.get()) if self.save_var.get() else None
            if output is not None:
                if output.suffix.lower() != ".bin":
                    output = output.with_suffix(".bin")
                    self.output_var.set(str(output))
                if output.exists():
                    raise ValueError("El archivo ya existe. Elija un nombre nuevo para evitar sobrescribir datos.")
            real = self.backend_var.get() == "Hardware NI"
            if real:
                message = (
                    "Se habilitarán AO0/AO1 y los triggers PFI12/PFI13.\n\n"
                    f"X: ±{scan.x_length_mm / 2:.3f} mm  ({scan.x_length_mm * hardware.x_v_per_mm:.4f} Vpp)\n"
                    f"Y: ±{scan.y_length_mm / 2:.3f} mm  ({scan.y_length_mm * hardware.y_v_per_mm:.4f} Vpp)\n"
                    f"Frecuencia efectiva: {hardware.effective_line_rate_hz:,.1f} A-lines/s\n"
                    f"OCE: {'habilitado' if hardware.oce_enabled else 'deshabilitado'}\n"
                    f"PFI13: {hardware.oce_pulse_width_us:g} µs alto / "
                    f"{hardware.oce_pulse_width_us * (1 / OCE_TRIGGER_DUTY_CYCLE - 1):g} µs bajo "
                    "(10 % en tren)\n\n"
                    "Confirme que PFI12 llega a la entrada de trigger del frame grabber, que su acción "
                    "NI-IMAQ produce una línea válida por pulso y que los límites eléctricos son seguros."
                )
                if not messagebox.askyesno("Armar hardware NI", message, icon="warning"):
                    return
                backend = NIHardwareBackend()
            else:
                backend = SimulatedBackend()
            self.progress_var.set(0.0)
            self.progress_text_var.set(f"0 / {scan.expected_alines:,} A-lines")
            self._active_scan = scan
            self._active_hardware = hardware
            self._alignment_active = False
            self._crosshair_loop_active = False
            self._clear_preview(
                "Guardando raw: preview deshabilitado."
                if output is not None else "Esperando primer B-scan…"
            )
            self.engine.set_preview_remove_dc(self.remove_dc_var.get())
            self.engine.set_preview_depth_range(*depth_range)
            self._append_log(f"Iniciando {backend.name}; modo {scan.mode.value}, patrón {scan.pattern.value}.")
            if scan.pattern is ScanPattern.LINEAR and not scan.is_stationary:
                cadence = "cada repetición M" if scan.mode is AcquisitionMode.BM else "cada B-scan"
                self._append_log(
                    f"Lineal bidireccional activo: el sentido físico alterna {cadence}; "
                    "no se invierte el eje temporal M."
                )
            self._before_oct_start(output)
            self.engine.start(scan, hardware, output_path=output, backend=backend)
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        except Exception as exc:
            self._on_oct_start_failed()
            messagebox.showerror("No se puede iniciar", str(exc))

    def _start_alignment(self) -> None:
        try:
            if self.engine.is_active:
                raise RuntimeError("Detenga la adquisición actual antes de alinear.")
            depth_range = self._parse_depth_range()
            self._last_depth_range = depth_range
            scan = ScanParameters(
                alines=1,
                bscans=1,
                m_repetitions=1000,
                sync_points=0,
                x_length_mm=0.0,
                y_length_mm=0.0,
                mode=AcquisitionMode.MB,
                pattern=ScanPattern.LINEAR,
            )
            hardware = replace(
                self.hardware,
                k_start_nm=float(self.k_start_var.get().replace(",", ".")),
                k_end_nm=float(self.k_end_var.get().replace(",", ".")),
            )
            if self.backend_var.get() == "Hardware NI":
                hardware, alignment_rate_hz = stationary_alignment_timing(hardware, 1000)
            else:
                alignment_rate_hz = hardware.effective_line_rate_hz / 1000
            scan.validate()
            hardware.validate(scan)
            real = self.backend_var.get() == "Hardware NI"
            if real:
                if not hardware.oce_enabled:
                    raise RuntimeError(
                        "Active 'Habilitar salida OCE en PFI13' en Hardware para usar "
                        "la alineación continua con trigger."
                    )
                if not messagebox.askyesno(
                    "Alineación continua",
                    "AO0/AO1 se mantendrán en 0 V (centro). Se adquirirán grupos MB de "
                    "1000 A-lines indefinidamente, sin guardar ni puntos sync. PFI12 y "
                    f"PFI13 se temporizarán por hardware cada {1000 / alignment_rate_hz:.2f} ms; PFI13 "
                    f"tendrá 10 % de ciclo útil. La cámara usará {hardware.effective_line_rate_hz / 1000:.3f} klps "
                    "para terminar cada frame antes del siguiente trigger. Se repetirá hasta "
                    "pulsar Detener. Use Detener para parquear y salir. ¿Iniciar?",
                    icon="warning",
                ):
                    return
                backend = NIHardwareBackend(
                    continuous_alignment=True, alignment_block_rate_hz=alignment_rate_hz
                )
            else:
                backend = SimulatedBackend()
            self._alignment_active = True
            self._crosshair_loop_active = False
            self._active_scan = scan
            self._active_hardware = hardware
            self._clear_preview("Alineación: esperando 1000 A-lines del centro…")
            self.alignment_zoom_canvas.grid()
            self.alignment_zoom_canvas.create_text(
                12, 12, anchor="nw", fill="#d7e9ff",
                font=("Segoe UI", 9), text="Zoom ±10 px @ Z · esperando datos…",
            )
            self.remove_dc_var.set(False)
            self.engine.set_preview_remove_dc(False)
            self.engine.set_preview_depth_range(*depth_range)
            self.progress_var.set(0.0)
            self.progress_text_var.set("Alineación continua · 0 A-lines")
            self._append_log(
                "Alineación continua MB en (0,0), M=1000, sin archivo; "
                f"PFI12/PFI13 a {alignment_rate_hz:.3f} Hz por hardware; "
                f"cámara a {hardware.effective_line_rate_hz / 1000:.3f} klps para dejar margen entre frames; "
                "PFI13 10 % de ciclo útil; "
                "DC desactivado para conservar la señal estacionaria."
            )
            self._before_oct_start(None)
            self.engine.start(scan, hardware, output_path=None, backend=backend, continuous=True)
            self.start_button.configure(state="disabled")
            self.align_button.configure(state="disabled")
            self.crosshair_loop_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        except Exception as exc:
            self._alignment_active = False
            self._on_oct_start_failed()
            messagebox.showerror("No se puede alinear", str(exc))

    def _start_crosshair_loop(self) -> None:
        try:
            if self.engine.is_active:
                raise RuntimeError("Detenga la adquisición actual antes de iniciar el loop.")
            depth_range = self._parse_depth_range()
            self._last_depth_range = depth_range
            scan = ScanParameters(
                alines=500,
                bscans=1,
                m_repetitions=1,
                sync_points=50,
                x_length_mm=10.0,
                y_length_mm=10.0,
                mode=AcquisitionMode.BM,
                pattern=ScanPattern.CROSSHAIR,
            )
            hardware = replace(
                self.hardware,
                k_start_nm=float(self.k_start_var.get().replace(",", ".")),
                k_end_nm=float(self.k_end_var.get().replace(",", ".")),
                oce_enabled=False,
            )
            scan.validate()
            hardware.validate(scan)
            if self.backend_var.get() == "Hardware NI":
                if not messagebox.askyesno(
                    "Crosshair continuo",
                    "Se repetirá indefinidamente un B-scan lógico de 500 A-lines en X "
                    "y 500 en Y sobre 10×10 mm, BM con M=1, sin guardar ni PFI13/OCE. "
                    "Use Detener para parquear los galvos. ¿Iniciar?",
                    icon="warning",
                ):
                    return
                backend = NIHardwareBackend()
            else:
                backend = SimulatedBackend()
            self._alignment_active = False
            self._crosshair_loop_active = True
            self._active_scan = scan
            self._active_hardware = hardware
            self._clear_preview("Crosshair continuo: esperando sweeps X e Y…")
            self.progress_var.set(0.0)
            self.progress_text_var.set("Crosshair continuo · 0 A-lines")
            self.engine.set_preview_remove_dc(self.remove_dc_var.get())
            self.engine.set_preview_depth_range(*depth_range)
            self._append_log(
                "Crosshair continuo BM: 500 X + 500 Y, 10×10 mm, M=1, sin archivo ni OCE."
            )
            self._before_oct_start(None)
            self.engine.start(scan, hardware, output_path=None, backend=backend, continuous=True)
            self.start_button.configure(state="disabled")
            self.align_button.configure(state="disabled")
            self.crosshair_loop_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        except Exception as exc:
            self._crosshair_loop_active = False
            self._on_oct_start_failed()
            messagebox.showerror("No se puede iniciar crosshair continuo", str(exc))

    def _stop(self) -> None:
        self.engine.stop()
        self.stop_button.configure(state="disabled")
        self._append_log("Solicitud de parada enviada; se finalizará el bloque actual de forma segura.")

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_dot.itemconfigure(self.status_dot_id, fill=color)

    def _poll_events(self) -> None:
        self._events_after = None
        try:
            while True:
                event = self.event_queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        if self._closing and not self.engine.is_active:
            self._before_root_destroy()
            self._cancel_after_callbacks()
            self.root.destroy()
            return
        self._events_after = self.root.after(50, self._poll_events)

    def _cancel_after_callbacks(self) -> None:
        for name in ("_events_after", "_trajectory_after", "_depth_after"):
            callback_id = getattr(self, name)
            if callback_id is not None:
                try:
                    self.root.after_cancel(callback_id)
                except tk.TclError:
                    pass
                setattr(self, name, None)

    def _handle_event(self, event: EngineEvent) -> None:
        p = event.payload
        if event.kind == "state":
            state = p["state"]
            colors = {
                EngineState.ARMING.value: "#d89527",
                EngineState.RUNNING.value: "#1da56d",
                EngineState.STOPPING.value: "#d89527",
                EngineState.COMPLETED.value: "#2477d4",
                EngineState.STOPPED.value: "#718096",
                EngineState.ERROR.value: "#d34f4f",
            }
            labels = {
                EngineState.ARMING.value: "Armando",
                EngineState.RUNNING.value: "Adquiriendo",
                EngineState.STOPPING.value: "Deteniendo",
                EngineState.COMPLETED.value: "Completado",
                EngineState.STOPPED.value: "Detenido",
                EngineState.ERROR.value: "Error",
            }
            self._set_status(labels.get(state, state), colors.get(state, "#718096"))
            active = state in {
                EngineState.ARMING.value,
                EngineState.RUNNING.value,
                EngineState.STOPPING.value,
            }
            self.start_button.configure(state="disabled" if active else "normal")
            self.align_button.configure(state="disabled" if active else "normal")
            self.crosshair_loop_button.configure(state="disabled" if active else "normal")
            self.stop_button.configure(
                state="normal" if state in {EngineState.ARMING.value, EngineState.RUNNING.value} else "disabled"
            )
            if state == EngineState.COMPLETED.value:
                self._append_log(f"Adquisición completa: {p.get('output') or 'sin archivo'}.")
            elif state in {EngineState.STOPPED.value, EngineState.ERROR.value}:
                self._append_log(p.get("reason") or labels.get(state, state))
            if state in {
                EngineState.COMPLETED.value,
                EngineState.STOPPED.value,
                EngineState.ERROR.value,
            }:
                elapsed = p.get("elapsed_s")
                if elapsed is not None:
                    timing_text = (
                        f"Tiempo de adquisición: {float(elapsed):.3f} s "
                        f"({_duration(float(elapsed))})."
                    )
                    self._append_log(timing_text)
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        f"Tiempo de adquisicion: {float(elapsed):.3f} s",
                        flush=True,
                    )
                self._crosshair_loop_active = False
        elif event.kind == "progress":
            acquired = int(p["acquired_alines"])
            expected = p.get("expected_alines")
            if expected is None:
                self.progress_var.set(0.0)
                label = "Crosshair continuo" if self._crosshair_loop_active else "Alineación continua"
                self.progress_text_var.set(f"{label} · {acquired:,} A-lines")
            else:
                expected = int(expected)
                self.progress_var.set(100.0 * acquired / expected)
                self.progress_text_var.set(f"{acquired:,} / {expected:,} A-lines")
            self.diagnostics_var.set(
                f"Buffer {p['requested_buffer']}→{p['copied_buffer']}  ·  Ring {p['ring_index']}  ·  "
                f"Perdidos {p['lost_buffers']}  ·  Cola {p['queue_size']}"
            )
            timing_label = (
                f"Tiempo {_duration(p.get('elapsed_s'))}"
                if expected is None else f"ETA {_duration(p.get('eta_s'))}"
            )
            self.eta_var.set(f"{timing_label}  ·  {p['throughput_mib_s']:.2f} MiB/s")
        elif event.kind == "preview":
            self._show_preview(
                np.asarray(p["intensity_db"], dtype=np.float32),
                np.asarray(p["phase_rad"], dtype=np.float32),
                np.asarray(p["depth_indexes"], dtype=np.int64),
                np.asarray(p["aline_indexes"], dtype=np.int64),
                p.get("sweep_index"),
                np.asarray(p["source_spectra"], dtype=np.uint16),
                bool(p.get("dc_removed", True)),
                secondary_intensity_db=p.get("secondary_intensity_db"),
                secondary_phase_rad=p.get("secondary_phase_rad"),
                secondary_aline_indexes=p.get("secondary_aline_indexes"),
                secondary_source_spectra=p.get("secondary_source_spectra"),
                depth_start_bin=p.get("depth_start_bin"),
                depth_end_bin=p.get("depth_end_bin"),
            )
        elif event.kind in {"error", "consumer_error", "preview_warning"}:
            self._append_log(f"{event.kind}: {p.get('message', '')}")

    def _show_preview(
        self,
        intensity_db: np.ndarray,
        phase_rad: np.ndarray,
        depth_indexes: np.ndarray,
        aline_indexes: np.ndarray,
        sweep_index: int | None,
        source_spectra: np.ndarray | None = None,
        dc_removed: bool = True,
        *,
        secondary_intensity_db: np.ndarray | None = None,
        secondary_phase_rad: np.ndarray | None = None,
        secondary_aline_indexes: np.ndarray | None = None,
        secondary_source_spectra: np.ndarray | None = None,
        depth_start_bin: int | None = None,
        depth_end_bin: int | None = None,
    ) -> None:
        first = self._preview_db is None
        previous_sweep = self._preview_sweep_index
        self._preview_db = intensity_db
        self._preview_phase = phase_rad
        self._preview_depth_indexes = depth_indexes
        self._preview_aline_indexes = aline_indexes
        self._preview_source_spectra = source_spectra
        self._preview_sweep_index = None if sweep_index is None else int(sweep_index)
        self._secondary_db = None if secondary_intensity_db is None else np.asarray(secondary_intensity_db)
        self._secondary_phase = None if secondary_phase_rad is None else np.asarray(secondary_phase_rad)
        self._secondary_aline_indexes = (
            np.empty(0, dtype=np.int64) if secondary_aline_indexes is None
            else np.asarray(secondary_aline_indexes, dtype=np.int64)
        )
        self._secondary_source_spectra = secondary_source_spectra
        if self._secondary_db is not None:
            self._preview_sweep_index = previous_sweep if previous_sweep == 1 and not first else 0
            self.cross_canvas.grid()
        else:
            self.cross_canvas.grid_remove()
        stale_depth = (
            depth_start_bin is not None
            and depth_end_bin is not None
            and (int(depth_start_bin), int(depth_end_bin)) != self._last_depth_range
        )
        if source_spectra is not None and (dc_removed != self.remove_dc_var.get() or stale_depth):
            self._reprocess_cached_preview(redraw=False)
        assert self._preview_db is not None
        rows, columns = self._preview_db.shape
        self.z_cursor_scale.configure(to=max(0, rows - 1))
        self.lateral_cursor_scale.configure(to=max(0, columns - 1))
        if first:
            self.z_cursor_var.set(max(0, rows // 3))
            self.lateral_cursor_var.set(max(0, columns // 2))
        else:
            self.z_cursor_var.set(min(self.z_cursor_var.get(), max(0, rows - 1)))
            self.lateral_cursor_var.set(min(self.lateral_cursor_var.get(), max(0, columns - 1)))
        self._render_preview()
        self._draw_phase_profile()

    def _dc_removal_changed(self) -> None:
        enabled = self.remove_dc_var.get()
        self.engine.set_preview_remove_dc(enabled)
        self._reprocess_cached_preview()
        self._append_log(
            f"Remoción DC del preview {'activada' if enabled else 'desactivada'}; "
            "los datos crudos no cambian."
        )

    def _reprocess_cached_preview(self, *, redraw: bool = True) -> None:
        if self._preview_source_spectra is None:
            return
        old_z = int(np.clip(round(self.z_cursor_var.get()), 0, max(0, self._preview_depth_indexes.size - 1)))
        old_bin = int(self._preview_depth_indexes[old_z]) if self._preview_depth_indexes.size else self._last_depth_range[0]
        hardware = self._active_hardware or self.hardware
        arguments = {
            "remove_dc": self.remove_dc_var.get(),
            "wavelength_start_nm": hardware.k_start_nm,
            "wavelength_end_nm": hardware.k_end_nm,
            "depth_start_bin": self._last_depth_range[0],
            "depth_end_bin": self._last_depth_range[1],
        }
        self._preview_db, self._preview_phase, self._preview_depth_indexes, _ = preview_complex(
            self._preview_source_spectra, **arguments
        )
        if self._secondary_source_spectra is not None:
            self._secondary_db, self._secondary_phase, _depths, _ = preview_complex(
                self._secondary_source_spectra, **arguments
            )
        rows, columns = self._preview_db.shape
        self.z_cursor_scale.configure(to=max(0, rows - 1))
        self.lateral_cursor_scale.configure(to=max(0, columns - 1))
        nearest = int(np.argmin(np.abs(self._preview_depth_indexes - old_bin)))
        self.z_cursor_var.set(nearest)
        self.lateral_cursor_var.set(min(self.lateral_cursor_var.get(), max(0, columns - 1)))
        if redraw:
            self._render_preview()
            self._draw_phase_profile()

    def _parse_depth_range(self) -> tuple[int, int]:
        try:
            start = int(self.depth_start_var.get())
            end = int(self.depth_end_var.get())
        except ValueError as exc:
            raise ValueError("El rango Z debe usar bins enteros.") from exc
        if not 1 <= start <= end <= 4096:
            raise ValueError("Rango Z inválido: 1 ≤ inicio ≤ fin ≤ 4096 bins FFT.")
        return start, end

    def _depth_range_changed(self) -> None:
        if self._depth_after is not None:
            self.root.after_cancel(self._depth_after)
        self._depth_after = self.root.after(250, self._apply_depth_range)

    def _apply_depth_range(self) -> None:
        pending = self._depth_after
        self._depth_after = None
        if pending is not None:
            self.root.after_cancel(pending)
        try:
            bounds = self._parse_depth_range()
        except ValueError as exc:
            self.depth_limits_status_var.set(str(exc))
            return
        suffix = " · zona lejana puede incluir espejo" if bounds[1] > 2048 else ""
        self.depth_limits_status_var.set(
            f"Z visible: {bounds[0]}–{bounds[1]} / 4096 bins{suffix}"
        )
        if bounds == self._last_depth_range:
            return
        self._last_depth_range = bounds
        self.engine.set_preview_depth_range(*bounds)
        self._reprocess_cached_preview()

    def _display_settings_changed(self, _event: object | None = None) -> None:
        low = float(self.black_percentile_var.get())
        high = float(self.white_percentile_var.get())
        if low >= high - 0.5:
            if low >= 70.0:
                low = high - 0.5
                self.black_percentile_var.set(low)
            else:
                high = low + 0.5
                self.white_percentile_var.set(high)
        self.black_text_var.set(f"{low:.1f} %")
        self.white_text_var.set(f"{high:.1f} %")
        if self.display_db_var.get():
            try:
                black_db = float(self.black_db_var.get().replace(",", "."))
                white_db = float(self.white_db_var.get().replace(",", "."))
                if not np.isfinite(black_db) or not np.isfinite(white_db) or black_db >= white_db:
                    raise ValueError
                self._last_db_limits = (black_db, white_db)
                self.display_limits_status_var.set(
                    f"Escala fija {black_db:g} a {white_db:g} dB"
                )
            except ValueError:
                self.display_limits_status_var.set("dB inválidos: Negro debe ser menor que Blanco")
        else:
            self.display_limits_status_var.set("")
        self._render_preview()

    def _normalize_display(
        self, image_db: np.ndarray, *, max_width: int = 700, max_height: int = 500
    ) -> np.ndarray:
        if not self.display_db_var.get():
            return normalize_preview(
                image_db,
                low_percentile=float(self.black_percentile_var.get()),
                high_percentile=float(self.white_percentile_var.get()),
                max_width=max_width,
                max_height=max_height,
            )
        image = np.asarray(image_db, dtype=np.float32)
        black_db, white_db = self._last_db_limits
        scaled = np.clip((image - black_db) * (255.0 / (white_db - black_db)), 0, 255)
        scaled = np.nan_to_num(scaled, nan=0.0, posinf=255.0, neginf=0.0).astype(np.uint8)
        row_step = max(1, int(np.ceil(scaled.shape[0] / max_height)))
        col_step = max(1, int(np.ceil(scaled.shape[1] / max_width)))
        return np.ascontiguousarray(scaled[::row_step, ::col_step])

    def _cursor_changed(self, _value: object | None = None) -> None:
        self._render_preview()
        self._draw_phase_profile()

    def _render_preview(self) -> None:
        if self._preview_db is None:
            return
        canvas = self.image_canvas
        width = max(canvas.winfo_width(), 100)
        height = max(canvas.winfo_height(), 100)
        canvas.delete("all")
        dual = self._secondary_db is not None
        datasets = [(0, self._preview_db)]
        if dual:
            datasets.append((1, self._secondary_db))
        margin, gap, title_h = 16, 12, 25
        panel_w = (width - 2 * margin - (gap if dual else 0)) / len(datasets)
        panel_h = max(20, height - 2 * margin - title_h)
        self._image_bounds = None
        self._image_bounds_secondary = None
        for panel, db in datasets:
            if db is None:
                continue
            image = self._normalize_display(
                db,
                max_width=max(700, db.shape[1]),
                max_height=max(500, db.shape[0]),
            )
            pil = Image.fromarray(self._apply_colormap(image, self.colormap_var.get()), mode="RGB")
            # Display scaling only: X and Z have different physical units, and
            # the B-scan should use the space reclaimed from the lower plots.
            image_w = max(1, int(panel_w))
            image_h = max(1, int(panel_h))
            pil = pil.resize((image_w, image_h), Image.Resampling.BILINEAR)
            photo = ImageTk.PhotoImage(pil)
            if panel == 0:
                self._preview_photo = photo
            else:
                self._preview_photo_secondary = photo
            left = margin + panel * (panel_w + gap)
            x0 = left + (panel_w - image_w) / 2
            y0 = margin + title_h + (panel_h - image_h) / 2
            bounds = (x0, y0, float(image_w), float(image_h))
            if panel == 0:
                self._image_bounds = bounds
            else:
                self._image_bounds_secondary = bounds
            canvas.create_text(
                left + 3, margin + 1, anchor="nw", fill="#d7e9ff",
                font=("Segoe UI", 10, "bold"),
                text=("X–Z" if panel == 0 else "Y–Z") if dual else "B-scan OCT",
            )
            canvas.create_image(x0, y0, image=photo, anchor="nw")
            rows, columns = db.shape
            z_index = int(np.clip(round(self.z_cursor_var.get()), 0, rows - 1))
            selected = self._preview_sweep_index in (panel, None)
            lateral_index = int(np.clip(round(self.lateral_cursor_var.get()), 0, columns - 1))
            guide_y = y0 + (z_index + 0.5) / rows * image_h
            canvas.create_line(x0, guide_y, x0 + image_w, guide_y, fill="#39e6ff", width=1.5)
            if selected:
                guide_x = x0 + (lateral_index + 0.5) / columns * image_w
                canvas.create_line(guide_x, y0, guide_x, y0 + image_h, fill="#ffdf4d", width=1.5)
                z_label, lateral_label = self._cursor_labels(z_index, lateral_index)
                canvas.create_text(
                    x0 + 12, y0 + 10, anchor="nw", fill="#ffffff",
                    font=("Segoe UI", 9, "bold"),
                    text=f"{z_label}  ·  {lateral_label}",
                )
        self._draw_cross_map()
        self._render_alignment_zoom()

    def _render_alignment_zoom(self) -> None:
        if not self._alignment_active or self._preview_db is None:
            return
        canvas = self.alignment_zoom_canvas
        width = max(canvas.winfo_width(), 80)
        height = max(canvas.winfo_height(), 80)
        canvas.delete("all")
        rows, columns = self._preview_db.shape
        z = int(np.clip(round(self.z_cursor_var.get()), 0, rows - 1))
        # Exactly 20 preview-depth pixels, with the cursor at offset 10.
        start = z - 10
        stop = z + 10
        valid_start, valid_stop = max(0, start), min(rows, stop)
        normalized = self._normalize_display(
            self._preview_db,
            max_width=max(700, columns),
            max_height=max(500, rows),
        )
        strip = np.zeros((20, normalized.shape[1]), dtype=np.uint8)
        strip[valid_start - start:valid_stop - start] = normalized[valid_start:valid_stop]
        colored = self._apply_colormap(strip, self.colormap_var.get())
        pil = Image.fromarray(colored, mode="RGB")
        image_w = max(1, width - 16)
        image_h = max(1, height - 48)
        pil = pil.resize((image_w, image_h), Image.Resampling.NEAREST)
        self._alignment_zoom_photo = ImageTk.PhotoImage(pil)
        canvas.create_text(
            8, 8, anchor="nw", fill="#d7e9ff", font=("Segoe UI", 9, "bold"),
            text=f"Zoom 20 px · Z bin {int(self._preview_depth_indexes[z])}",
        )
        canvas.create_image(8, 32, image=self._alignment_zoom_photo, anchor="nw")
        cursor_y = 32 + (10.5 / 20.0) * image_h
        canvas.create_line(8, cursor_y, 8 + image_w, cursor_y, fill="#39e6ff", width=1.5)

    def _select_preview_point(self, event: tk.Event[Any]) -> None:
        if self._preview_db is None or self._image_bounds is None:
            return
        bounds = self._image_bounds
        db = self._preview_db
        sweep = 0
        if self._secondary_db is not None and self._image_bounds_secondary is not None:
            x1, y1, w1, h1 = self._image_bounds_secondary
            if x1 <= event.x <= x1 + w1 and y1 <= event.y <= y1 + h1:
                bounds, db, sweep = self._image_bounds_secondary, self._secondary_db, 1
        x0, y0, width, height = bounds
        if not (x0 <= event.x <= x0 + width and y0 <= event.y <= y0 + height):
            return
        self._preview_sweep_index = sweep if self._secondary_db is not None else None
        rows, columns = db.shape
        lateral = int(np.clip((event.x - x0) / width * columns, 0, columns - 1))
        depth = int(np.clip((event.y - y0) / height * rows, 0, rows - 1))
        self.lateral_cursor_var.set(lateral)
        self.z_cursor_var.set(depth)
        self._cursor_changed()

    def _lateral_axis(self) -> tuple[str, float]:
        scan = self._active_scan
        if scan is None:
            return "X/Y", 0.0
        if scan.pattern is ScanPattern.CROSSHAIR:
            if self._preview_sweep_index == 1:
                return "Y", scan.y_length_mm
            return "X", scan.x_length_mm
        if scan.pattern is ScanPattern.LINEAR and scan.orientation is Orientation.VERTICAL:
            return "Y", scan.y_length_mm
        if scan.pattern is ScanPattern.MERIDIANS:
            return "S", max(scan.x_length_mm, scan.y_length_mm)
        return "X", scan.x_length_mm

    def _cursor_labels(self, z_index: int, lateral_index: int) -> tuple[str, str]:
        selected_indexes = (
            self._secondary_aline_indexes
            if self._preview_sweep_index == 1 and self._secondary_db is not None
            else self._preview_aline_indexes
        )
        z_original = (
            int(self._preview_depth_indexes[z_index])
            if z_index < self._preview_depth_indexes.size
            else z_index
        )
        aline_original = int(selected_indexes[lateral_index]) if lateral_index < selected_indexes.size else lateral_index
        if self._alignment_active:
            z_label = f"Z bin {z_original}"
            lateral_label = f"Centro (0,0) · M{aline_original + 1}"
            self.z_cursor_text_var.set(z_label)
            self.lateral_cursor_text_var.set(lateral_label)
            return z_label, lateral_label
        axis, length_mm = self._lateral_axis()
        total = max(2, self._active_scan.alines if self._active_scan is not None else self._preview_db.shape[1])
        position_mm = ((aline_original / (total - 1)) - 0.5) * length_mm
        z_label = f"Z bin {z_original}"
        lateral_label = f"{axis} {position_mm:+.3f} mm · A{aline_original + 1}"
        self.z_cursor_text_var.set(z_label)
        self.lateral_cursor_text_var.set(lateral_label)
        return z_label, lateral_label

    def _draw_phase_profile(self) -> None:
        canvas = self.phase_canvas
        selected_phase = (
            self._secondary_phase
            if self._preview_sweep_index == 1 and self._secondary_phase is not None
            else self._preview_phase
        )
        if selected_phase is None or selected_phase.size == 0:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 160)
        height = max(canvas.winfo_height(), 120)
        rows, columns = selected_phase.shape
        z_index = int(np.clip(round(self.z_cursor_var.get()), 0, rows - 1))
        lateral_index = int(np.clip(round(self.lateral_cursor_var.get()), 0, columns - 1))
        phase = np.unwrap(selected_phase[z_index].astype(np.float64))
        phase -= phase[lateral_index]
        finite = phase[np.isfinite(phase)]
        margin_left, margin_right, margin_top, margin_bottom = 48, 16, 18, 34
        plot_width = max(1, width - margin_left - margin_right)
        plot_height = max(1, height - margin_top - margin_bottom)
        if not self.phase_auto_var.get():
            try:
                lo = float(self.phase_min_var.get().replace(",", "."))
                hi = float(self.phase_max_var.get().replace(",", "."))
                if not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi:
                    raise ValueError
            except ValueError:
                canvas.create_text(16, 16, anchor="nw", fill="#c43d3d", text="Límites de fase inválidos: Mín < Máx")
                return
        elif finite.size:
            lo, hi = np.percentile(finite, (1.0, 99.0))
            lo = min(float(lo), 0.0)
            hi = max(float(hi), 0.0)
        else:
            lo, hi = -1.0, 1.0
        if hi - lo < 1e-6:
            lo -= 0.5
            hi += 0.5

        def project(index: int, value: float) -> tuple[float, float]:
            x = margin_left + index / max(1, columns - 1) * plot_width
            y = margin_top + (hi - value) / (hi - lo) * plot_height
            return x, y

        if lo <= 0.0 <= hi:
            zero_y = project(0, 0.0)[1]
            canvas.create_line(margin_left, zero_y, width - margin_right, zero_y, fill="#cfd8e5", dash=(3, 3))
        cursor_x = project(lateral_index, 0.0)[0]
        canvas.create_line(cursor_x, margin_top, cursor_x, height - margin_bottom, fill="#e6b800", width=1.5)
        coords: list[float] = []
        for index, value in enumerate(phase):
            x, y = project(index, float(np.clip(value, lo, hi)))
            coords.extend((x, y))
        if len(coords) >= 4:
            canvas.create_line(*coords, fill="#2477d4", width=1.6, smooth=False)
        canvas.create_line(margin_left, margin_top, margin_left, height - margin_bottom, fill="#8593a6")
        canvas.create_line(margin_left, height - margin_bottom, width - margin_right, height - margin_bottom, fill="#8593a6")
        canvas.create_text(8, margin_top, anchor="nw", fill="#5b697c", font=("Segoe UI", 8), text=f"{hi:.2f}")
        canvas.create_text(8, height - margin_bottom, anchor="sw", fill="#5b697c", font=("Segoe UI", 8), text=f"{lo:.2f}")
        axis, _length = self._lateral_axis()
        if self._alignment_active:
            axis = "M / repetición"
        canvas.create_text(
            margin_left + plot_width / 2,
            height - 8,
            anchor="s",
            fill="#5b697c",
            font=("Segoe UI", 8),
            text=f"{axis} / posición lateral   ·   fase relativa (rad)",
        )
        z_label, lateral_label = self._cursor_labels(z_index, lateral_index)
        self.phase_title_var.set(f"Fase relativa @ {z_label} · referencia {lateral_label}")

    def _draw_cross_map(self) -> None:
        if self._preview_db is None or self._secondary_db is None:
            return
        canvas = self.cross_canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 100)
        canvas.create_text(12, 9, anchor="nw", fill="#d7e9ff", font=("Segoe UI", 9, "bold"), text="Cruz X/Y @ Z")
        z = int(np.clip(round(self.z_cursor_var.get()), 0, min(self._preview_db.shape[0], self._secondary_db.shape[0]) - 1))
        x_intensity = self._normalize_display(self._preview_db[z:z + 1])[0]
        y_intensity = self._normalize_display(self._secondary_db[z:z + 1])[0]
        x_colors = self._apply_colormap(x_intensity[None, :], self.colormap_var.get())[0]
        y_colors = self._apply_colormap(y_intensity[None, :], self.colormap_var.get())[0]
        cx, cy = width / 2, height / 2 + 12
        radius = max(12, min(width / 2 - 15, height / 2 - 30))
        for i, rgb in enumerate(x_colors):
            x = cx - radius + i / max(1, len(x_intensity) - 1) * (2 * radius)
            color = "#%02x%02x%02x" % tuple(rgb)
            canvas.create_line(x, cy - 2, x, cy + 2, fill=color, width=max(1, 2 * radius / len(x_intensity)))
        for i, rgb in enumerate(y_colors):
            y = cy + radius - i / max(1, len(y_intensity) - 1) * (2 * radius)
            color = "#%02x%02x%02x" % tuple(rgb)
            canvas.create_line(cx - 2, y, cx + 2, y, fill=color, width=max(1, 2 * radius / len(y_intensity)))
        canvas.create_text(cx, cy + radius + 5, anchor="n", fill="#a7bfd9", font=("Segoe UI", 8), text="X horizontal · Y vertical")

    @staticmethod
    def _apply_colormap(image: np.ndarray, name: str) -> np.ndarray:
        anchors = _COLORMAP_ANCHORS.get(name, _COLORMAP_ANCHORS["Inferno"])
        positions = np.asarray([anchor[0] for anchor in anchors], dtype=np.float32)
        colors = np.asarray([anchor[1] for anchor in anchors], dtype=np.float32)
        value = image.astype(np.float32) / 255.0
        channels = [np.interp(value, positions, colors[:, channel]) for channel in range(3)]
        return np.clip(np.stack(channels, axis=-1), 0, 255).astype(np.uint8)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{stamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_hardware_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Configuración de hardware")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("650x730")
        dialog.minsize(560, 540)
        dialog.configure(bg="#ffffff")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        shell = _ScrollablePanel(dialog)
        shell.grid(row=0, column=0, sticky="nsew")
        body = shell.inner
        ttk.Label(body, text="Hardware y temporización", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        body.columnconfigure(1, weight=1)
        specs = [
            ("daq_device", "Dispositivo DAQ", str),
            ("camera_interface", "Interfaz NI-IMAQ", str),
            ("spectral_samples", "Píxeles por A-line", int),
            ("sensor_bit_depth", "Bits válidos", int),
            ("line_rate_hz", "Frecuencia solicitada (A-lines/s)", float),
            ("x_v_per_mm", "Factor X (V/mm)", float),
            ("y_v_per_mm", "Factor Y (V/mm)", float),
            ("max_galvo_abs_v", "Límite galvo |V|", float),
            ("galvo_v_per_degree", "JP7 galvo (V/°)", float),
            ("beam_diameter_mm", "Diámetro haz en espejos (mm; 0=?)", float),
            ("camera_trigger_terminal", "Trigger cámara", str),
            ("camera_trigger_width_us", "Ancho PFI12 / CC1 (µs)", float),
            ("camera_phase_offset_us", "Retardo cámara (µs)", float),
            ("oce_trigger_terminal", "Trigger OCE", str),
            ("oce_pulse_width_us", "Ancho OCE (µs; duty 10 %)", float),
            ("imaq_ring_buffers", "Buffers NI-IMAQ", int),
            ("frame_timeout_ms", "Timeout frame (ms)", int),
            ("writer_queue_size", "Bloques en cola", int),
            ("preview_rate_hz", "Preview máximo (Hz)", float),
            ("park_ramp_points", "Puntos rampa a park", int),
            ("external_buffer_trigger_line", "Línea External NI-IMAQ", int),
        ]
        variables: dict[str, tk.StringVar] = {}
        converters: dict[str, type] = {}
        for row, (name, label, converter) in enumerate(specs, start=1):
            ttk.Label(body, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=3)
            variable = tk.StringVar(value=str(getattr(self.hardware, name)))
            ttk.Entry(body, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(15, 0), pady=3)
            variables[name] = variable
            converters[name] = converter
        bool_row = len(specs) + 1
        oce_var = tk.BooleanVar(value=self.hardware.oce_enabled)
        sensor_var = tk.BooleanVar(value=self.hardware.configure_sensor_trigger)
        require_sensor_var = tk.BooleanVar(value=self.hardware.require_external_sensor_trigger)
        ext_buffer_var = tk.BooleanVar(value=self.hardware.external_buffer_trigger_enabled)
        ttk.Checkbutton(body, text="Habilitar salida OCE en PFI13", variable=oce_var).grid(
            row=bool_row, column=0, columnspan=2, sticky="w", pady=(8, 2)
        )
        ttk.Checkbutton(
            body,
            text="Configurar GL2048R: OPR, Fixed Exp y periodo/ancho CC1",
            variable=sensor_var,
        ).grid(row=bool_row + 1, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(
            body,
            text="PFI12 inicia cada buffer NI-IMAQ (External trigger)",
            variable=ext_buffer_var,
        ).grid(row=bool_row + 2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(
            body,
            text="Bloquear AO si el sensor reporta trigger interno",
            variable=require_sensor_var,
        ).grid(row=bool_row + 3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(
            body,
            text="Los factores confirmados (0,2364067 y 0,1941748 V/mm) quedan registrados en cada archivo. "
            "JP7 se inicia en 0,8 V/° (valor de fábrica del manual). Introduzca el diámetro real del haz "
            "para ajustar los límites; mientras sea 0 se usa la fila conservadora de 5 mm.",
            style="Muted.TLabel",
            wraplength=560,
            justify="left",
        ).grid(row=bool_row + 4, column=0, columnspan=2, sticky="w", pady=(8, 10))

        def apply() -> None:
            try:
                changes: dict[str, Any] = {}
                for name, variable in variables.items():
                    converter = converters[name]
                    raw = variable.get().replace(",", ".") if converter is float else variable.get()
                    changes[name] = converter(raw)
                changes.update(
                    oce_enabled=oce_var.get(),
                    configure_sensor_trigger=sensor_var.get(),
                    require_external_sensor_trigger=require_sensor_var.get(),
                    external_buffer_trigger_enabled=ext_buffer_var.get(),
                )
                candidate = replace(self.hardware, **changes)
                scan, _ = self._configs()
                candidate.validate(scan)
                self.hardware = candidate
                dialog.destroy()
                self._refresh_plan()
                self._append_log("Configuración de hardware actualizada.")
            except Exception as exc:
                messagebox.showerror("Configuración inválida", str(exc), parent=dialog)

        actions = ttk.Frame(dialog, style="Card.TFrame", padding=(18, 10, 18, 14))
        actions.grid(row=1, column=0, sticky="ew")
        ttk.Button(actions, text="Cancelar", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Aplicar", style="Primary.TButton", command=apply).pack(
            side="right", padx=(0, 8)
        )

    def _on_close(self) -> None:
        if self.engine.is_active:
            if not messagebox.askyesno(
                "Cerrar aplicación",
                "Hay una adquisición activa. ¿Desea detenerla, parquear los galvos y cerrar?",
                icon="warning",
            ):
                return
            self._closing = True
            self.engine.stop()
        else:
            self._before_root_destroy()
            self._cancel_after_callbacks()
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    OCTOCEApp(root)
    root.mainloop()
