from __future__ import annotations

import os
import tkinter as tk
import unittest
from dataclasses import replace
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from octoce.config import AcquisitionMode, ScanParameters, ScanPattern
from octoce.engine import EngineEvent, EngineState
from octoce.gui import OCTOCEApp
from octoce.processing import preview_complex


@unittest.skipUnless(os.name == "nt", "Smoke visual de Tkinter para Windows")
class GuiSmokeTests(unittest.TestCase):
    def test_continuous_mb_alignment_arms_one_oce_pulse_per_block(self) -> None:
        root = tk.Tk()
        try:
            app = OCTOCEApp(root)
            app.backend_var.set("Hardware NI")
            with patch("octoce.gui.messagebox.askyesno", return_value=True) as confirm, patch.object(
                app.engine, "start"
            ) as start:
                app._start_alignment()
            scan, hardware = start.call_args.args[:2]
            self.assertEqual((scan.mode, scan.alines, scan.m_repetitions),
                             (AcquisitionMode.MB, 1, 1000))
            self.assertEqual(scan.sync_points, 0)
            self.assertEqual((scan.x_length_mm, scan.y_length_mm), (0.0, 0.0))
            self.assertEqual(scan.oce_trigger_segments, 1)
            self.assertTrue(hardware.oce_enabled)
            self.assertGreater(hardware.effective_line_rate_hz, 50_000.0)
            self.assertAlmostEqual(start.call_args.kwargs["backend"]._alignment_block_rate_hz, 50.0)
            self.assertTrue(start.call_args.kwargs["continuous"])
            self.assertIsNone(start.call_args.kwargs["output_path"])
            self.assertIn("PFI13", confirm.call_args.args[1])
        finally:
            root.update_idletasks()
            root.destroy()

    def test_continuous_mb_alignment_requires_oce_enabled_on_real_hardware(self) -> None:
        root = tk.Tk()
        try:
            app = OCTOCEApp(root)
            app.backend_var.set("Hardware NI")
            app.hardware = replace(app.hardware, oce_enabled=False)
            with patch("octoce.gui.messagebox.showerror") as error, patch.object(
                app.engine, "start"
            ) as start:
                app._start_alignment()
            start.assert_not_called()
            self.assertIn("Habilitar salida OCE", error.call_args.args[1])
        finally:
            root.update_idletasks()
            root.destroy()

    def test_main_and_hardware_dialog_fit_with_scroll(self) -> None:
        root = tk.Tk()
        try:
            root.geometry("1040x680")
            app = OCTOCEApp(root)
            root.update_idletasks()
            scrollbars = [w for w in self._walk(root) if isinstance(w, ttk.Scrollbar)]
            self.assertGreaterEqual(len(scrollbars), 1)
            self.assertTrue(app.start_button.winfo_exists())
            self.assertTrue(app.phase_canvas.winfo_exists())
            self.assertTrue(app.z_cursor_scale.winfo_exists())
            self.assertTrue(app.lateral_cursor_scale.winfo_exists())
            self.assertEqual(app.colormap_var.get(), "Grises")
            self.assertTrue(app.remove_dc_var.get())
            self.assertFalse(app.phase_auto_var.get())
            self.assertEqual((app.phase_min_var.get(), app.phase_max_var.get()), ("-15", "15"))

            app._open_hardware_dialog()
            root.update_idletasks()
            dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            self.assertEqual(len(dialogs), 1)
            dialog = dialogs[0]
            buttons = [w for w in self._walk(dialog) if isinstance(w, ttk.Button)]
            labels = {w.cget("text") for w in buttons}
            self.assertIn("Aplicar", labels)
            self.assertIn("Cancelar", labels)
            for button in buttons:
                if button.cget("text") in {"Aplicar", "Cancelar"}:
                    self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())
            dialog.destroy()
        finally:
            root.update_idletasks()
            root.destroy()

    def test_preview_controls_render_intensity_guides_and_phase(self) -> None:
        root = tk.Tk()
        try:
            root.geometry("1040x680")
            app = OCTOCEApp(root)
            app._active_scan = ScanParameters(alines=32, bscans=1)
            depth, lateral = 24, 32
            z = np.linspace(0.0, 1.0, depth, dtype=np.float32)[:, None]
            x = np.linspace(0.0, 1.0, lateral, dtype=np.float32)[None, :]
            intensity = 20.0 + 50.0 * (z + x)
            phase = np.angle(np.exp(1j * (8.0 * x + 2.0 * z))).astype(np.float32)
            app._show_preview(
                intensity,
                phase,
                np.arange(depth, dtype=np.int64),
                np.arange(lateral, dtype=np.int64),
                None,
            )
            root.update_idletasks()
            app._render_preview()
            app._draw_phase_profile()
            self.assertGreaterEqual(len(app.image_canvas.find_all()), 4)
            self.assertGreaterEqual(len(app.phase_canvas.find_all()), 5)
            self.assertIn("Z bin", app.z_cursor_text_var.get())
            self.assertIn("fase", app.phase_canvas.itemcget(app.phase_canvas.find_all()[-1], "text").lower())
            source = np.tile(np.arange(64, dtype=np.uint16), (lateral, 1)) + 1000
            app._preview_source_spectra = source
            app.remove_dc_var.set(False)
            app._dc_removal_changed()
            self.assertFalse(app.engine.preview_remove_dc)
            app.remove_dc_var.set(True)
            app._dc_removal_changed()
            self.assertTrue(app.engine.preview_remove_dc)
        finally:
            root.destroy()

    def test_crosshair_shows_both_sweeps_and_selects_y_phase(self) -> None:
        root = tk.Tk()
        try:
            app = OCTOCEApp(root)
            app._active_scan = ScanParameters(alines=32, bscans=1, pattern=ScanPattern.CROSSHAIR)
            depth, lateral = 24, 32
            x_data = np.arange(depth * lateral, dtype=np.float32).reshape(depth, lateral)
            y_data = x_data[:, ::-1].copy()
            x_phase = np.zeros_like(x_data)
            y_phase = np.tile(np.linspace(-1.0, 1.0, lateral, dtype=np.float32), (depth, 1))
            app._show_preview(
                x_data, x_phase, np.arange(depth), np.arange(lateral), 0,
                secondary_intensity_db=y_data,
                secondary_phase_rad=y_phase,
                secondary_aline_indexes=np.arange(lateral),
            )
            root.update_idletasks()
            app._render_preview()
            self.assertTrue(app.cross_canvas.grid_info())
            self.assertIsNotNone(app._image_bounds_secondary)
            x0, y0, width, height = app._image_bounds_secondary
            app._select_preview_point(SimpleNamespace(x=int(x0 + width / 2), y=int(y0 + height / 2)))
            self.assertEqual(app._preview_sweep_index, 1)
            self.assertIn("Y ", app.lateral_cursor_text_var.get())
            app.phase_auto_var.set(False)
            app.phase_min_var.set("-0.5")
            app.phase_max_var.set("0.5")
            app._draw_phase_profile()
            texts = [app.phase_canvas.itemcget(item, "text") for item in app.phase_canvas.find_all()
                     if app.phase_canvas.type(item) == "text"]
            self.assertIn("0.50", texts)
            self.assertIn("-0.50", texts)
        finally:
            root.destroy()

    def test_alignment_zoom_tracks_selected_z(self) -> None:
        root = tk.Tk()
        try:
            app = OCTOCEApp(root)
            app._alignment_active = True
            app.alignment_zoom_canvas.grid()
            depth, lateral = 40, 32
            image = np.arange(depth * lateral, dtype=np.float32).reshape(depth, lateral)
            app._show_preview(
                image, np.zeros_like(image), np.arange(1, depth + 1),
                np.arange(lateral), None,
            )
            root.update_idletasks()
            app.z_cursor_var.set(15)
            app._render_alignment_zoom()
            items = app.alignment_zoom_canvas.find_all()
            labels = [app.alignment_zoom_canvas.itemcget(item, "text") for item in items
                      if app.alignment_zoom_canvas.type(item) == "text"]
            self.assertTrue(any("Z bin 16" in label for label in labels))
            self.assertTrue(any(app.alignment_zoom_canvas.type(item) == "image" for item in items))
        finally:
            root.destroy()

    def test_db_depth_range_loop_controls_and_elapsed_log(self) -> None:
        root = tk.Tk()
        try:
            app = OCTOCEApp(root)
            self.assertIn("MB", app.align_button.cget("text"))
            self.assertIn("500 X + 500 Y", app.crosshair_loop_button.cget("text"))
            app.display_db_var.set(True)
            app.black_db_var.set("20")
            app.white_db_var.set("40")
            app._display_settings_changed()
            scaled = app._normalize_display(np.asarray([[20.0, 30.0, 40.0]], dtype=np.float32))
            self.assertEqual(scaled.tolist(), [[0, 127, 255]])

            raw = np.tile(np.arange(64, dtype=np.uint16), (10, 1)) + 1000
            intensity, phase, depths, alines = preview_complex(raw)
            app._show_preview(intensity, phase, depths, alines, None, source_spectra=raw)
            app.depth_start_var.set("100")
            app.depth_end_var.set("119")
            app._apply_depth_range()
            self.assertEqual(app.engine.preview_depth_range, (100, 119))
            self.assertEqual((app._preview_depth_indexes[0], app._preview_depth_indexes[-1]), (100, 119))

            with patch.object(app.engine, "start") as start:
                app._start_crosshair_loop()
            scan, hardware = start.call_args.args[:2]
            self.assertEqual((scan.alines, scan.expected_alines), (500, 1000))
            self.assertEqual((scan.mode, scan.pattern, scan.m_repetitions),
                             (AcquisitionMode.BM, ScanPattern.CROSSHAIR, 1))
            self.assertEqual((scan.x_length_mm, scan.y_length_mm), (10.0, 10.0))
            self.assertFalse(hardware.oce_enabled)
            self.assertTrue(start.call_args.kwargs["continuous"])
            self.assertIsNone(start.call_args.kwargs["output_path"])
            app._handle_event(EngineEvent("state", {"state": EngineState.STOPPED.value,
                                                    "reason": "Detenido", "elapsed_s": 1.25}))
            self.assertIn("Tiempo de adquisición: 1.250 s", app.log.get("1.0", "end"))
        finally:
            root.update_idletasks()
            root.destroy()

    def _walk(self, widget: tk.Misc):
        for child in widget.winfo_children():
            yield child
            yield from self._walk(child)


if __name__ == "__main__":
    unittest.main()
