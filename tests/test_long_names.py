"""Long display names retain their data without changing favorite column widths."""
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
IS_TV = (ROOT / 'SonyRemote.pyw').exists()
if IS_TV:
    import test_graceful_failures as fixture
else:
    import test_volume_controls as fixture

LONG_NAME = 'Beginning ' + 'a very long display name ' * 30 + ' Ending'


class LongNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (fixture.DiscoveryTests if IS_TV else fixture.VolumeControlTests).setUpClass()

    def setUp(self):
        if IS_TV:
            self.fixture = fixture.DiscoveryTests()
            self.fixture.setUp()
            self.fixture.settle_volume_controls()
            self.window = self.fixture.window
        else:
            self.fixture = fixture.VolumeControlTests()
            self.window = self.fixture.window()
        self.addCleanup(self.fixture.doCleanups)
        self.globals = type(self.window)._build_favorites_from_config.__globals__

    def favorites(self, name):
        entry = lambda label: dict(Name=label, NameOverride='', Hidden=False, Favorite=True)
        if IS_TV:
            self.window.config['Inputs'] = {'first': entry('TV'), 'second': entry('Other')}
            self.window.config['Inputs']['first']['NameOverride'] = name
            self.window.config['Apps'] = {'app1': entry(name), 'app2': entry('App')}
        else:
            self.window.input_settings.clear()
            self.window.input_settings.update({'first': entry('TV'), 'second': entry('Other')})
            self.window.input_settings['first']['NameOverride'] = name
        self.window._build_favorites_from_config()
        self.window._fit_and_lock_window()
        self.fixture.app.processEvents()
        layout = self.window.input_favorites_layout
        return [layout.itemAt(i).widget() for i in range(layout.count())]

    def painted_labels(self, widget):
        labels = []
        original = self.globals['QStylePainter']
        class Painter(original):
            def drawText(self, rect, flags, text):
                labels.append(text)
                return super().drawText(rect, flags, text)
            def drawControl(self, element, option):
                labels.append(option.text)
                return super().drawControl(element, option)
        with patch.dict(self.globals, QStylePainter=Painter):
            widget.grab()
        return labels

    def test_long_overrides_do_not_resize_favorites_or_window(self):
        short = self.favorites('TV')
        width = self.window.width()
        widths = [button.width() for button in short]
        long = self.favorites(LONG_NAME)
        self.assertEqual(self.window.width(), width)
        self.assertEqual([button.width() for button in long], widths)
        self.assertLessEqual(abs(long[0].width() - long[1].width()), 1)
        self.assertEqual(long[0].text(), LONG_NAME)
        if IS_TV:
            apps = self.window.app_favorites_layout
            self.assertLessEqual(abs(apps.itemAt(0).widget().width() - apps.itemAt(1).widget().width()), 1)

    def test_closed_combo_and_favorite_show_prefix_and_trailing_ellipsis(self):
        favorite = self.favorites(LONG_NAME)[0]
        combo = self.window.input_combo
        combo.clear()
        combo.addItem(LONG_NAME, 'first')
        combo.setCurrentIndex(0)
        self.fixture.app.processEvents()
        button_text = self.painted_labels(favorite)[-1]
        combo_text = self.painted_labels(combo)[-1]
        for text in (button_text, combo_text):
            self.assertTrue(text.startswith('Beginning'), text)
            self.assertTrue(text.endswith(('…', '...')), text)
            self.assertNotIn('Ending', text)
        self.assertGreater(len(combo_text), len(button_text))
        self.assertEqual(combo.currentText(), LONG_NAME)
        self.assertEqual(combo.currentData(), 'first')

    def test_short_favorite_label_is_unchanged(self):
        button = self.favorites('TV')[0]
        self.assertEqual(self.painted_labels(button)[-1], 'TV')

    def test_reused_favorite_preserves_click_target(self):
        with patch.object(self.window, 'set_input') as select:
            original = self.favorites('TV')[0]
            reused = self.favorites(LONG_NAME)[0]
            self.assertIs(reused, original)
            reused.setEnabled(True)
            reused.click()
            select.assert_called_once_with('first')
