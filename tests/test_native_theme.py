"""Native color-scheme requests and inheritance without device access."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QComboBox, QHBoxLayout

ROOT = Path(__file__).resolve().parents[1]
BRAND = "Sony" if (ROOT / "SonyRemote.pyw").exists() else "Yamaha"
REMOTE = runpy.run_path(str(ROOT / f"{BRAND}Remote.pyw"), run_name="native_theme_tests")


class NativeThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_both_schemes_are_requested_through_qt(self):
        app = Mock()
        hints = app.styleHints.return_value
        with patch.dict(REMOTE["configure_app_theme"].__globals__, QApplication=app):
            REMOTE["configure_app_theme"]({"DarkMode": True})
            hints.setColorScheme.assert_called_with(Qt.ColorScheme.Dark)
            REMOTE["configure_app_theme"]({"DarkMode": False})
            hints.setColorScheme.assert_called_with(Qt.ColorScheme.Light)

    def test_controls_follow_palette_changes_without_fixed_color_overrides(self):
        original = self.app.palette()
        self.addCleanup(self.app.setPalette, original)
        window = QWidget()
        window.mica_enabled = False
        window.setObjectName("remoteRoot")
        button, combo = QPushButton("Volume", window), QComboBox(window)
        REMOTE[f"{BRAND}Remote"]._apply_style(window)
        refresh = lambda palette: REMOTE[f"{BRAND}Remote"]._apply_style(window)
        self.app.paletteChanged.connect(refresh)
        self.addCleanup(self.app.paletteChanged.disconnect, refresh)
        window.show()
        self.addCleanup(window.close)
        for background, foreground in (("#eeeeee", "#111111"), ("#222222", "#eeeeee")):
            palette = QPalette()
            for role in (QPalette.Window, QPalette.Button, QPalette.Base):
                palette.setColor(role, QColor(background))
            for role in (QPalette.WindowText, QPalette.ButtonText, QPalette.Text):
                palette.setColor(role, QColor(foreground))
            self.app.setPalette(palette)
            window.ensurePolished()
            self.app.processEvents()
            for control in (button, combo):
                self.assertEqual(control.palette().color(QPalette.ButtonText), QColor(foreground))
                self.assertEqual(control.palette().color(QPalette.Button), QColor(background))
        self.assertNotIn("rgba(", window.styleSheet())
        self.assertNotIn("color:", window.styleSheet())
        window.deleteLater()

    def test_compact_playback_row_matches_previous_padding_size(self):
        window = QWidget()
        window.mica_enabled = False
        row = QHBoxLayout(window)
        row.setSpacing(6)
        buttons = [QPushButton(text) for text in ("Prev", "Rew", "Play / Pause", "Fwd", "Next")]
        for button in buttons:
            row.addWidget(button)
        # The former stylesheet made text/padding determine width, rather than
        # accepting the platform's generic minimum width for every push button.
        window.setStyleSheet("QWidget {font-size:9pt;} QPushButton {padding:5px 7px; border:1px solid gray;}")
        window.ensurePolished()
        previous = window.sizeHint().width()
        REMOTE[f"{BRAND}Remote"]._apply_style(window)
        window.ensurePolished()
        self.assertLessEqual(window.sizeHint().width(), previous + 10)
        self.assertLess(buttons[0].sizeHint().width(), buttons[2].sizeHint().width())
        window.deleteLater()

    def test_mica_restores_only_section_fill_and_root_surface(self):
        window = QWidget()
        window.mica_enabled = True
        window.config = {"DarkMode": True}
        REMOTE[f"{BRAND}Remote"]._apply_style(window)
        self.assertIn("background: transparent", window.styleSheet())
        self.assertIn("background: rgba(38, 38, 38, 185)", window.styleSheet())
        window.config["DarkMode"] = False
        REMOTE[f"{BRAND}Remote"]._apply_style(window)
        self.assertIn("background: rgba(255,255,255,190)", window.styleSheet())
        self.assertNotIn("set_dark_titlebar", REMOTE)
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
