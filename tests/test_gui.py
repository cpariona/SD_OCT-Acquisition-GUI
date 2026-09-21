from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import patch
from octacq.acquisition import Acquisition
from octacq.config import load_config
from octacq.ui.app import App


class GuiTests(unittest.TestCase):
    def test_request_controls_and_safe_startup(self):
        h, r = load_config(Path(__file__).resolve().parents[1] / "config/system.toml")
        root = tk.Tk()
        root.withdraw()
        engine = Acquisition(h, r)
        try:
            app = App(root, engine)
            root.update_idletasks()
            self.assertFalse(engine.connected)
            self.assertFalse(engine.active)
            self.assertEqual(str(app.start_button["state"]), "disabled")
            app.values["mode"].set("MB")
            app.values["m_repetitions"].set("7")
            self.assertEqual(app.request().m_repetitions, 7)
            self.assertEqual(app.request().mode, "MB")
            app.values["alines"].set("invalid")
            with patch("octacq.ui.app.messagebox.showerror") as error:
                app.connect()
                error.assert_called_once()
            self.assertFalse(engine.active)
            root.after_cancel(app.timer)
        finally:
            root.destroy()
