"""Popup lifecycle checks; Windows rendering still needs desktop testing."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'SonyRemote.pyw'), run_name='dropdown_tests')


class DropdownAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        effect = self.app.isEffectEnabled(Qt.UI_AnimateCombo)
        self.addCleanup(lambda: self.app.setEffectEnabled(Qt.UI_AnimateCombo, effect))
        self.root = QWidget()
        self.root.setStyleSheet('QComboBox QAbstractItemView { background: rgb(40,40,40); color: white; }')
        layout = QVBoxLayout(self.root)
        self.combo = REMOTE['CenteredComboBox'](animate_popup=True)
        self.combo.setItemDelegate(REMOTE['CenteredComboDelegate'](self.combo))
        self.combo.addItems([f'Entry {i}' for i in range(20)])
        self.combo.setFixedWidth(240)
        layout.addWidget(self.combo)
        self.button = QPushButton('Underlying navigation/favorite button')
        layout.addWidget(self.button)
        self.root.move(100, 100)
        self.root.show()
        self.app.processEvents()
        self.addCleanup(self.cleanup)
        # Headless Qt disables effects regardless of the preference. Simulate
        # its query; native opening still runs with Qt's animation suppressed.
        query = patch.object(QApplication, 'isEffectEnabled', return_value=True)
        query.start()
        self.addCleanup(query.stop)
        self.activated = QSignalSpy(self.combo.activated)

    def cleanup(self):
        self.combo.hidePopup()
        self.root.close()
        self.root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def native_geometry(self):
        self.app.setEffectEnabled(Qt.UI_AnimateCombo, False)
        QComboBox.showPopup(self.combo)
        # Capture Qt's chosen client geometry at the same point as the
        # implementation, before platform-specific frame adjustments.
        geometry = QRect(self.combo.view().window().geometry())
        self.combo.hidePopup()
        return geometry

    def assert_clean(self):
        self.assertIsNone(self.combo._popup)
        self.assertIsNone(self.combo._popup_layout)
        self.assertIsNone(self.combo._popup_view)
        self.assertFalse(self.combo._popup_timer.isActive())

    def finish(self):
        QTest.qWait(REMOTE['POPUP_ANIMATION_DURATION_MS'] + 60)
        self.assert_clean()

    def test_animation_restores_live_popup_without_hiding_underlying_buttons(self):
        expected = self.native_geometry()
        self.combo.showPopup()
        popup = self.combo.view().window()
        self.assertIs(self.app.activePopupWidget(), popup)
        self.assertIsNotNone(self.combo._popup_layout)
        self.assertTrue(self.combo.view().isVisible())
        self.assertTrue(self.button.isVisible())
        self.assertIsNone(self.button.graphicsEffect())
        self.finish()
        self.assertEqual(popup.geometry(), expected)
        self.assertTrue(self.combo.view().isVisible())
        self.assertTrue(self.combo.view().hasFocus())
        self.assertIs(self.app.activePopupWidget(), popup)
        self.assertEqual(self.activated.count(), 0)

    def test_mouse_selection_activates_once_and_preserves_item_data(self):
        self.combo.setItemData(3, 'app:third')
        self.combo.showPopup()
        self.finish()
        view = self.combo.view()
        point = view.visualRect(self.combo.model().index(3, 0)).center()
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=point)
        self.assertEqual(self.activated.count(), 1)
        self.assertEqual(self.combo.currentData(), 'app:third')
        self.assertFalse(view.window().isVisible())

    def test_keyboard_during_animation_keeps_live_navigation(self):
        self.combo.showPopup()
        QTest.keyClick(self.app.focusWidget(), Qt.Key_Down)
        self.assertTrue(self.combo._popup_timer.isActive())
        self.assertEqual(self.combo.view().currentIndex().row(), 1)
        QTest.keyClick(self.app.focusWidget(), Qt.Key_Return)
        self.assertEqual(self.combo.currentIndex(), 1)
        self.assertEqual(self.activated.count(), 1)
        self.assertFalse(self.combo.view().window().isVisible())

    def test_wheel_and_layout_remain_live_during_animation(self):
        self.combo.clear()
        self.combo.addItems([f'Entry {i}' for i in range(100)])
        self.combo.showPopup()
        view = self.combo.view()
        popup = self.combo._popup
        layout = popup.layout()
        expected_size = self.combo._view_rect.size()
        self.assertFalse(layout.isEnabled())
        bar = view.verticalScrollBar()
        before = bar.value()
        point = view.viewport().rect().center()
        wheel = QWheelEvent(QPointF(point),
                            QPointF(view.viewport().mapToGlobal(point)),
                            QPoint(), QPoint(0, -120), Qt.NoButton,
                            Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(view.viewport(), wheel)
        self.assertGreater(bar.value(), before)
        for _ in range(3):
            QTest.qWait(20)
            self.assertIs(self.combo._popup, popup)
            self.assertEqual(view.size(), expected_size)
        self.finish()
        self.assertTrue(layout.isEnabled())

    def test_escape_cancels_animation_and_allows_reopening(self):
        expected = self.native_geometry()
        self.combo.showPopup()
        QTest.keyClick(self.app.focusWidget(), Qt.Key_Escape)
        self.assert_clean()
        self.assertFalse(self.combo.view().window().isVisible())
        self.assertEqual(self.activated.count(), 0)
        self.combo.showPopup()
        self.finish()
        self.assertEqual(self.combo.view().window().geometry(), expected)

    def test_native_dismissals_restore_geometry_before_reopening(self):
        expected = self.native_geometry()
        for _ in range(4):
            self.combo.showPopup()
            self.assertIsNotNone(self.combo._popup_layout)
            self.combo.view().window().hide()  # OS/Qt can bypass hidePopup().
            self.assert_clean()
            self.assertEqual(self.combo.view().window().geometry(), expected)
        self.combo.showPopup()
        self.finish()
        self.assertEqual(self.combo.view().window().geometry(), expected)
        self.assertEqual(self.activated.count(), 0)

    def test_second_open_request_closes_animation_cleanly(self):
        self.combo.showPopup()
        self.combo.showPopup()
        self.assert_clean()
        self.assertFalse(self.combo.view().window().isVisible())
        self.combo.showPopup()
        self.finish()
        self.assertTrue(self.combo.view().isVisible())

    def test_native_placement_is_preserved_for_upward_downward_and_long_lists(self):
        screen = self.combo.screen().availableGeometry()
        for count, bottom in ((3, False), (20, False), (20, True), (100, True)):
            with self.subTest(count=count, bottom=bottom):
                self.combo.clear()
                self.combo.addItems([f'Entry {i}' for i in range(count)])
                self.root.move(screen.left() + 20,
                               screen.bottom() - self.root.height() - 5 if bottom else screen.top() + 20)
                self.app.processEvents()
                expected = self.native_geometry()
                self.combo.showPopup()
                self.assertEqual(self.combo._final_rect, expected)
                self.assertTrue(expected.contains(self.combo.view().window().geometry()))
                self.finish()
                self.assertEqual(self.combo.view().window().geometry(), expected)
                self.combo.hidePopup()

    def test_long_list_keyboard_navigation_and_selection_still_work(self):
        self.combo.clear()
        self.combo.addItems([f'Entry {i}' for i in range(100)])
        self.combo.showPopup()
        self.finish()
        QTest.keyClick(self.app.focusWidget(), Qt.Key_End)
        self.assertEqual(self.combo.view().currentIndex().row(), 99)
        QTest.keyClick(self.app.focusWidget(), Qt.Key_Return)
        self.assertEqual(self.combo.currentIndex(), 99)
        self.assertEqual(self.activated.count(), 1)

    def test_owner_closing_during_animation_does_not_leave_a_running_timer(self):
        self.combo.showPopup()
        self.root.close()
        self.app.processEvents()
        self.assert_clean()
        self.root.show()
        self.app.processEvents()
        self.combo.showPopup()
        self.finish()
        self.assertTrue(self.combo.view().isVisible())

    def test_disabled_system_animation_does_not_capture_or_animate(self):
        with patch.object(QApplication, 'isEffectEnabled', return_value=False), \
             patch.object(self.combo.view(), 'grab') as capture:
            self.combo.showPopup()
        self.assert_clean()
        capture.assert_not_called()
        self.assertTrue(self.combo.view().isVisible())

    def test_disabled_workaround_keeps_ordinary_qt_popup(self):
        self.combo._animate_popup = False
        with patch.object(self.combo.view(), 'grab') as capture:
            self.combo.showPopup()
        self.assert_clean()
        capture.assert_not_called()
        self.assertTrue(self.combo.view().isVisible())

    def test_effect_preference_is_restored_when_opening_fails(self):
        with patch.object(QComboBox, 'showPopup', side_effect=RuntimeError('opening failed')), \
             patch.object(QApplication, 'setEffectEnabled') as effects:
            with self.assertRaisesRegex(RuntimeError, 'opening failed'):
                self.combo.showPopup()
        self.assertEqual([call.args for call in effects.call_args_list],
                         [(Qt.UI_AnimateCombo, False), (Qt.UI_AnimateCombo, True)])
        self.assert_clean()

    def test_empty_dropdown_does_not_start_animation(self):
        self.combo.clear()
        self.combo.showPopup()
        self.assert_clean()
        self.assertFalse(self.combo.view().window().isVisible())


if __name__ == '__main__':
    unittest.main()
