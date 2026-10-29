"""Clicks must not enter native window movement; actual drags still can."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import test_graceful_failures as fixture


class WindowDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        self.f = fixture.DiscoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.start_and_settle()
        self.w = self.f.window
        self.background = self.w.centralWidget()
        self.w.activateWindow()
        self.f.app.processEvents()
        self.move_patch = patch.object(QWindow, 'startSystemMove', return_value=True)
        self.move = self.move_patch.start()
        self.addCleanup(self.move_patch.stop)

    def send_move(self, widget, point, buttons=Qt.LeftButton):
        event = QMouseEvent(QEvent.MouseMove, QPointF(point), QPointF(widget.mapToGlobal(point)),
                            Qt.NoButton, buttons, Qt.NoModifier)
        QApplication.sendEvent(widget, event)

    def test_clicks_on_both_drag_surfaces_preserve_button_hover(self):
        for widget, point in ((self.background, QPoint(1, 1)),
                              (self.w.volume_value, self.w.volume_value.rect().center())):
            with self.subTest(surface=type(widget).__name__):
                QTest.mouseClick(widget, Qt.LeftButton, pos=point)
                self.move.assert_not_called()
                self.w.volume_up.setEnabled(True)
                QTest.mouseMove(self.w.volume_up, self.w.volume_up.rect().center())
                self.f.app.processEvents()
                self.assertTrue(self.w.volume_up.underMouse())
                self.assertTrue(self.w.volume_up.toolTip())

    def test_only_movement_beyond_threshold_starts_a_drag(self):
        for widget, origin in ((self.background, QPoint(1, 1)),
                               (self.w.volume_value, self.w.volume_value.rect().center())):
            with self.subTest(surface=type(widget).__name__):
                self.move.reset_mock()
                QTest.mousePress(widget, Qt.LeftButton, pos=origin)
                self.move.assert_not_called()
                distance = QApplication.startDragDistance()
                self.send_move(widget, origin + QPoint(distance - 1, 0))
                self.move.assert_not_called()
                self.send_move(widget, origin + QPoint(distance, 0))
                self.move.assert_called_once()
                self.send_move(widget, origin + QPoint(distance + 2, 0))
                self.move.assert_called_once()
                QTest.mouseRelease(widget, Qt.LeftButton, pos=origin)

    def test_release_and_focus_loss_cancel_pending_drags(self):
        label = self.w.volume_value
        origin = label.rect().center()
        for ending in ('release', 'deactivate', 'hide'):
            with self.subTest(ending=ending):
                QTest.mousePress(label, Qt.LeftButton, pos=origin)
                if ending == 'release':
                    QTest.mouseRelease(label, Qt.LeftButton, pos=origin)
                else:
                    QApplication.sendEvent(label, QEvent(QEvent.WindowDeactivate if ending == 'deactivate' else QEvent.Hide))
                self.send_move(label, origin + QPoint(QApplication.startDragDistance(), 0))
                self.move.assert_not_called()
                QTest.mouseRelease(label, Qt.LeftButton, pos=origin)

    def test_buttons_and_group_background_do_not_start_window_drags(self):
        for widget in (self.w.volume_up, self.w.volume_value.parentWidget()):
            origin = widget.rect().center()
            QTest.mousePress(widget, Qt.LeftButton, pos=origin)
            self.send_move(widget, origin + QPoint(QApplication.startDragDistance(), 0))
            QTest.mouseRelease(widget, Qt.LeftButton, pos=origin)
        self.move.assert_not_called()

    def test_drag_surface_clicks_leave_text_editing(self):
        for widget in (self.background, self.w.volume_value):
            self.w.text_input.setFocus()
            self.f.app.processEvents()
            self.assertTrue(self.w.text_input.hasFocus())
            QTest.mouseClick(widget, Qt.LeftButton, pos=QPoint(1, 1))
            self.assertFalse(self.w.text_input.hasFocus())
            self.move.assert_not_called()

    def test_background_dragging_without_volume_controls(self):
        cls = fixture.REMOTE['SonyRemote']
        with patch.object(cls, 'initialize', lambda window: None), \
             patch.object(cls, '_setup_calibration', lambda window: None), \
             patch.object(cls, '_setup_activation', lambda window: None), \
             patch.object(cls, '_install_windows_volume_hook', lambda window: None), \
             patch.object(fixture.REMOTE['AmplifierClient'], 'start', lambda client: None):
            window = cls(dict(self.w.config, VolumeControls=False))
        try:
            window.show()
            self.f.app.processEvents()
            self.assertFalse(hasattr(window, 'volume_value'))
            background = window.centralWidget()
            origin = QPoint(1, 1)
            QTest.mousePress(background, Qt.LeftButton, pos=origin)
            self.send_move(background, origin + QPoint(QApplication.startDragDistance(), 0))
            self.move.assert_called_once()
            QTest.mouseRelease(background, Qt.LeftButton, pos=origin)
        finally:
            window._shutdown()
            window.hide()
            window.deleteLater()
            self.f.app.processEvents()

    def test_right_click_does_not_arm_a_drag(self):
        label = self.w.volume_value
        origin = label.rect().center()
        QTest.mousePress(label, Qt.RightButton, pos=origin)
        self.send_move(label, origin + QPoint(QApplication.startDragDistance(), 0), Qt.RightButton)
        QTest.mouseRelease(label, Qt.RightButton, pos=origin)
        self.move.assert_not_called()


if __name__ == '__main__':
    unittest.main()
