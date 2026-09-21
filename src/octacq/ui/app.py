"""Tk acquisition controls; hardware operations run through Acquisition only."""
import argparse
from dataclasses import replace
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from ..acquisition import Acquisition
from ..config import ScanRequest, load_config


class App:
    def __init__(self, root: tk.Tk, acquisition: Acquisition):
        self.root, self.acquisition = root, acquisition
        self.closing = False
        self.disconnect_requested = False
        self.root.title("SD-OCT / OCE acquisition")
        self.root.minsize(680, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        container = ttk.Frame(root, padding=12)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)
        defaults = ScanRequest()
        self.values = {}
        self.inputs = []
        choices = {"mode": ("BM", "MB"), "pattern": ("raster", "crosshair", "meridians", "linear", "stationary"),
                   "orientation": ("horizontal", "vertical")}
        names = [("mode", "Orden"), ("pattern", "Patrón"), ("orientation", "Orientación lineal"),
                 ("alines", "A-lines / posiciones"), ("bscans", "B-scans"), ("m_repetitions", "Repeticiones M"),
                 ("x_length_mm", "Recorrido X (mm)"), ("y_length_mm", "Recorrido Y (mm)"),
                 ("center_x_mm", "Centro X (mm)"), ("center_y_mm", "Centro Y (mm)"),
                 ("sync_points", "Puntos de transición"), ("bframes_delay_us", "Delay OCE (µs, provisional)")]
        for row, (name, label) in enumerate(names):
            ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", pady=2)
            variable = tk.StringVar(root, value=str(getattr(defaults, name)))
            self.values[name] = variable
            widget = (ttk.Combobox(container, textvariable=variable, values=choices[name], state="readonly")
                      if name in choices else ttk.Entry(container, textvariable=variable))
            widget.grid(row=row, column=1, sticky="ew", pady=2)
            self.inputs.append((widget, "readonly" if name in choices else "normal"))
        row = len(names)
        for name, label in (("linear_bidirectional", "Lineal bidireccional"),
                            ("raster_bidirectional", "Raster bidireccional"), ("oce_enabled", "Habilitar OCE")):
            variable = tk.BooleanVar(root, value=getattr(defaults, name))
            self.values[name] = variable
            widget = ttk.Checkbutton(container, text=label, variable=variable)
            widget.grid(row=row, column=0, columnspan=2, sticky="w")
            self.inputs.append((widget, "normal"))
            row += 1
        self.output = tk.StringVar(root)
        ttk.Label(container, text="Archivo raw .bin").grid(row=row, column=0, sticky="w")
        path_box = ttk.Frame(container)
        path_box.grid(row=row, column=1, sticky="ew")
        entry = ttk.Entry(path_box, textvariable=self.output)
        entry.pack(side="left", fill="x", expand=True)
        self.inputs.append((entry, "normal"))
        browse = ttk.Button(path_box, text="Elegir…", command=self.choose_output)
        browse.pack(side="right")
        self.inputs.append((browse, "normal"))
        row += 1
        buttons = ttk.Frame(container)
        buttons.grid(row=row, column=0, columnspan=2, pady=10)
        self.connect_button = ttk.Button(buttons, text="Conectar", command=self.connect)
        self.disconnect_button = ttk.Button(buttons, text="Desconectar", command=lambda: self.call(self.acquisition.disconnect))
        self.start_button = ttk.Button(buttons, text="Adquirir y guardar", command=self.start)
        self.stop_button = ttk.Button(buttons, text="Detener", command=self.acquisition.stop)
        for button in (self.connect_button, self.disconnect_button, self.start_button, self.stop_button):
            button.pack(side="left", padx=3)
        row += 1
        loops = ttk.Frame(container)
        loops.grid(row=row, column=0, columnspan=2)
        self.align_button = ttk.Button(loops, text="Alineación estacionaria", command=lambda: self.start("stationary"))
        self.crosshair_button = ttk.Button(loops, text="Crosshair continuo sin OCE", command=lambda: self.start("crosshair"))
        self.align_button.pack(side="left", padx=3)
        self.crosshair_button.pack(side="left", padx=3)
        row += 1
        ttk.Label(container, text="Continuo: sin archivo. El procesamiento y las imágenes OCT se obtienen en MATLAB.",
                  wraplength=640).grid(row=row, column=0, columnspan=2, pady=8)
        row += 1
        self.status = tk.StringVar(root, "Desconectado")
        ttk.Label(container, textvariable=self.status, wraplength=640).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self.progress = ttk.Progressbar(container, maximum=100)
        self.progress.grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        self.log = tk.Text(container, height=5, wrap="word", state="disabled")
        self.log.grid(row=row, column=0, columnspan=2, sticky="nsew")
        container.rowconfigure(row, weight=1)
        self._refresh()
        self.timer = root.after(100, self.poll)

    def request(self):
        values = {name: variable.get() for name, variable in self.values.items()}
        for name in ("alines", "bscans", "m_repetitions", "sync_points"):
            values[name] = int(values[name])
        for name in ("x_length_mm", "y_length_mm", "center_x_mm", "center_y_mm", "bframes_delay_us"):
            values[name] = float(values[name].replace(",", "."))
        return ScanRequest(**values)

    def call(self, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except Exception as error:
            messagebox.showerror("Adquisición", str(error), parent=self.root)
        self._refresh()

    def choose_output(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".bin", filetypes=[("Raw OCT", "*.bin")])
        if path:
            self.output.set(path)

    def connect(self):
        try:
            self.acquisition.connect(self.request())
        except Exception as error:
            messagebox.showerror("No se puede conectar", str(error), parent=self.root)
        self._refresh()

    def start(self, continuous=None):
        try:
            request = self.request()
            path = self.output.get().strip()
            if continuous == "stationary":
                request = replace(request, mode="MB", pattern="stationary", alines=1, bscans=1,
                                  x_length_mm=0, y_length_mm=0)
            elif continuous == "crosshair":
                request = replace(request, mode="BM", pattern="crosshair", bscans=1, m_repetitions=1, oce_enabled=False)
            elif not path:
                raise ValueError("Seleccione un archivo de salida")
            self.acquisition.start(request, None if continuous else Path(path), continuous=bool(continuous))
        except Exception as error:
            messagebox.showerror("No se puede adquirir", str(error), parent=self.root)
        self._refresh()

    def _refresh(self):
        active = self.acquisition.active or self.closing
        connected = self.acquisition.connected
        for widget, state in self.inputs:
            widget.configure(state="disabled" if active else state)
        self.connect_button.configure(state="disabled" if active or connected else "normal")
        self.disconnect_button.configure(state="normal" if connected and not active else "disabled")
        for button in (self.start_button, self.align_button, self.crosshair_button):
            button.configure(state="normal" if connected and not active else "disabled")
        self.stop_button.configure(state="normal" if self.acquisition.state in ("arming", "running") else "disabled")

    def poll(self):
        while True:
            try:
                event = self.acquisition.events.get_nowait()
            except queue.Empty:
                break
            text = f"{event.state}: {event.confirmed_alines} A-lines confirmadas"
            if event.message:
                text += f" — {event.message}"
            self.status.set(text)
            if event.expected_alines:
                self.progress["value"] = 100 * event.confirmed_alines / event.expected_alines
            if event.kind != "progress":
                self.log.configure(state="normal")
                self.log.insert("end", text + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        if self.closing and not self.acquisition.active:
            if not self.disconnect_requested:
                self.disconnect_requested = True
                self.acquisition.disconnect()
            else:
                if self.acquisition.state == "error":
                    self.closing = False
                    self.disconnect_requested = False
                    messagebox.showerror("Cierre incompleto", self.status.get(), parent=self.root)
                    self._refresh()
                    self.timer = self.root.after(100, self.poll)
                    return
                self.root.destroy()
                return
        self._refresh()
        self.timer = self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        self.acquisition.stop()
        self._refresh()


def main():
    parser = argparse.ArgumentParser(description="SD-OCT/OCE acquisition")
    parser.add_argument("--config", type=Path, default=Path("config/system.toml"))
    args = parser.parse_args()
    hardware, runtime = load_config(args.config)
    root = tk.Tk()
    App(root, Acquisition(hardware, runtime))
    root.mainloop()


if __name__ == "__main__":
    main()
