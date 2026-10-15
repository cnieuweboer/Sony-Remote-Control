import ctypes
import faulthandler
import hashlib
import html
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

from PySide6.QtCore import QEvent, QFileSystemWatcher, QLockFile, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QGraphicsOpacityEffect, QMainWindow, QMenu, QMessageBox, QPushButton, QStyle, QTextEdit,
    QStyleOptionComboBox, QStylePainter, QStyledItemDelegate,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

APP_NAME = "Sony Remote"
INSTANCE_MUTEX_NAME = "Local\\SonyRemote.SingleInstance"
CONFIG_PATH = Path(__file__).with_name("SonyRemote.json")

NETWORK_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_MS = 2000
CONFIG_RESTART_DEBOUNCE_MS = 250
POPUP_GUARD_DURATION_MS = 220
POPUP_GUARD_FAVORITE_COUNT = 4
TEXT_INPUT_STATE_TIMEOUT_SECONDS = 1.5
TEXT_INPUT_STATE_POLL_SECONDS = 0.05

_instance_mutex_handle = None
_instance_lock_file = None

DEFAULT_CONFIG = {
    "Host": "",
    "PreSharedKey": "",
    "DarkMode": True,
    "Mica": True,
    "TextInputControls": True,
    "InputControls": False,
    "AppControls": True,
    "ChannelControls": True,
    "NavigationControls": True,
    "PlaybackControls": True,
    "VolumeControls": True,
    "Apps": {},
    "Inputs": {},
    "SystemTray": False,
    "CloseToTray": False,
    "DropdownAnimationWorkaround": sys.platform == "win32",
}

BOOLEAN_CONFIG_KEYS = (
    "DarkMode",
    "Mica",
    "TextInputControls",
    "InputControls",
    "AppControls",
    "ChannelControls",
    "NavigationControls",
    "PlaybackControls",
    "VolumeControls",
    "SystemTray",
    "CloseToTray",
    "DropdownAnimationWorkaround",
)

OBSOLETE_CONFIG_KEYS = (
    "PollIntervalMs",
    "AppNameOverrides",
    "HiddenApps",
    "FavoriteApps",
    "InputNameOverrides",
    "HiddenInputs",
    "FavoriteInputs",
)


# ----------------------------
# Single-instance guard
# ----------------------------

def acquire_single_instance():
    global _instance_mutex_handle, _instance_lock_file

    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32

            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.GetLastError.argtypes = []
            kernel32.GetLastError.restype = ctypes.c_ulong
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool

            handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
            if not handle:
                return True

            # CreateMutexW deliberately returns a valid handle even when the
            # named mutex already exists, so GetLastError must be checked
            # immediately after the call.
            ERROR_ALREADY_EXISTS = 183
            last_error = kernel32.GetLastError()
            if last_error == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False

            _instance_mutex_handle = handle
            return True
        except Exception:
            # Do not block startup if the guard itself fails unexpectedly.
            return True

    try:
        # macOS/Linux: use Qt's cross-platform file lock. Include the Unix
        # user ID so separate logged-in users do not block each other.
        user_suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
        lock_path = Path(tempfile.gettempdir()) / f"SonyRemote-{user_suffix}.lock"

        lock_file = QLockFile(str(lock_path))
        lock_file.setStaleLockTime(0)

        if not lock_file.tryLock(0):
            return False

        _instance_lock_file = lock_file
        return True
    except Exception:
        # As on Windows, a failure in the guard itself should not make the
        # remote unusable.
        return True


def release_single_instance():
    global _instance_mutex_handle, _instance_lock_file

    if sys.platform == "win32":
        if _instance_mutex_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(_instance_mutex_handle)
            except Exception:
                pass
            _instance_mutex_handle = None
        return

    if _instance_lock_file is not None:
        try:
            _instance_lock_file.unlock()
        except Exception:
            pass
        _instance_lock_file = None


# ----------------------------
# Windows application identity
# ----------------------------

def set_windows_app_id(app_id):
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# ----------------------------
# Windows Mica integration
# ----------------------------
# Same documented DWM approach used by the Yamaha remote.

class _MicaMargins(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def supports_mica():
    return (
        sys.platform == "win32"
        and sys.getwindowsversion().build >= 22000
    )


def apply_mica(hwnd):
    if not supports_mica() or not hwnd:
        return False

    try:
        hwnd = int(hwnd)
        dwm = ctypes.windll.dwmapi

        # Extend DWM composition over the complete Qt client area.
        margins = _MicaMargins(-1, -1, -1, -1)
        dwm.DwmExtendFrameIntoClientArea(
            ctypes.c_void_p(hwnd),
            ctypes.byref(margins),
        )

        build = sys.getwindowsversion().build
        if build < 22523:
            attribute = 1029
            value = ctypes.c_int(1)
        else:
            # DWMWA_SYSTEMBACKDROP_TYPE = 38
            # DWMSBT_MAINWINDOW = 2 (Mica)
            attribute = 38
            value = ctypes.c_int(2)

        result = dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(attribute),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except Exception:
        return False


def set_dark_titlebar(hwnd):
    if sys.platform != "win32":
        return

    try:
        value = ctypes.c_int(1)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_uint(20),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )

        if result != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(hwnd)),
                ctypes.c_uint(19),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except Exception:
        pass


def _default_config_copy():
    return {
        key: value.copy() if isinstance(value, dict) else value
        for key, value in DEFAULT_CONFIG.items()
    }


def load_config():
    exists = CONFIG_PATH.exists()
    if exists:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("SonyRemote.json must contain a JSON object.")
    else:
        user = {}

    config = _default_config_copy()
    config.update(user)
    changed = not exists

    for key in DEFAULT_CONFIG:
        if key not in user:
            changed = True

    for key in OBSOLETE_CONFIG_KEYS:
        if key in config:
            config.pop(key, None)
            changed = True

    for key in ("Apps", "Inputs"):
        if not isinstance(config.get(key), dict):
            config[key] = {}
            changed = True

    for key in BOOLEAN_CONFIG_KEYS:
        if not isinstance(config.get(key), bool):
            config[key] = DEFAULT_CONFIG[key]
            changed = True

    for key in ("Host", "PreSharedKey"):
        value = config.get(key, "")
        if not isinstance(value, str):
            config[key] = str(value)
            changed = True

    if changed:
        save_config(config)

    return config


def save_config(config):
    temp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, CONFIG_PATH)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


class SonyApi:
    def __init__(self, host, psk, timeout=2.0):
        self.psk = psk
        self.timeout = timeout
        self.base_url = f"http://{host}"
        self._request_id = 0
        self._lock = threading.Lock()
        self.remote_codes = {}

    def _next_id(self):
        with self._lock:
            self._request_id += 1
            return self._request_id

    def rpc(self, service, method, params=None, version="1.0"):
        payload = {
            "method": method,
            "params": [] if params is None else params,
            "id": self._next_id(),
            "version": version,
        }
        request = urllib.request.Request(
            f"{self.base_url}/sony/{service}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Auth-PSK": self.psk,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if "error" in result:
            raise RuntimeError(f"{method}: Sony error {result['error']}")
        return result

    def get_power(self):
        return self.rpc("system", "getPowerStatus")["result"][0]["status"]

    def set_text_form(self, text):
        return self.rpc(
            "appControl",
            "setTextForm",
            [str(text)],
            version="1.0",
        )

    def is_text_input_active(self):
        entries = self.rpc(
            "appControl",
            "getApplicationStatusList",
            version="1.0",
        )["result"][0]

        for entry in entries:
            if entry.get("name") == "textInput":
                return entry.get("status") == "on"

        return False

    def wait_for_text_input_state(self, active, timeout=TEXT_INPUT_STATE_TIMEOUT_SECONDS):
        deadline = time.monotonic() + timeout

        while True:
            if self.is_text_input_active() == active:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(TEXT_INPUT_STATE_POLL_SECONDS)

    def set_power(self, on):
        return self.rpc("system", "setPowerStatus", [{"status": bool(on)}])

    def get_volume(self):
        entries = self.rpc("audio", "getVolumeInformation")["result"][0]
        for entry in entries:
            if entry.get("target") == "speaker":
                return entry
        return entries[0] if entries else None

    def set_volume(self, value):
        return self.rpc(
            "audio", "setAudioVolume",
            [{"target": "speaker", "volume": str(int(value))}],
        )

    def set_mute(self, muted):
        return self.rpc("audio", "setAudioMute", [{"status": bool(muted)}])

    def get_apps(self):
        return self.rpc("appControl", "getApplicationList")["result"][0]

    def launch_app(self, uri):
        return self.rpc("appControl", "setActiveApp", [{"uri": uri}])

    def get_inputs(self):
        return self.rpc(
            "avContent", "getCurrentExternalInputsStatus", version="1.1"
        )["result"][0]

    def set_input(self, uri):
        return self.rpc("avContent", "setPlayContent", [{"uri": uri}])

    def load_remote_codes(self):
        entries = self.rpc("system", "getRemoteControllerInfo")["result"][1]
        self.remote_codes = {
            item["name"]: item["value"]
            for item in entries
            if item.get("name") and item.get("value")
        }

    def ircc(self, name):
        code = self.remote_codes.get(name)
        if not code:
            raise RuntimeError(f"Remote command '{name}' is unavailable.")
        body = f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">
      <IRCCCode>{escape(code)}</IRCCCode>
    </u:X_SendIRCC>
  </s:Body>
</s:Envelope>'''.encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/sony/ircc",
            data=body,
            method="POST",
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
                "X-Auth-PSK": self.psk,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()


class TvTextEdit(QTextEdit):
    submitRequested = Signal()
    submitAndConfirmRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setPlaceholderText("Type text for the TV")

        # Two visible lines, including the editor frame/padding.
        line_height = self.fontMetrics().lineSpacing()
        self.setFixedHeight(line_height * 2 + 24)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            modifiers = event.modifiers()

            if modifiers & Qt.ShiftModifier:
                super().keyPressEvent(event)
                return

            if modifiers & Qt.ControlModifier:
                self.submitAndConfirmRequested.emit()
            else:
                self.submitRequested.emit()
            return

        super().keyPressEvent(event)


class CenteredComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_guard_enabled = False
        self._popup_guard_widgets = []
        self._guarded_widgets = []

        self._popup_guard_timer = QTimer(self)
        self._popup_guard_timer.setSingleShot(True)
        self._popup_guard_timer.setInterval(POPUP_GUARD_DURATION_MS)
        self._popup_guard_timer.timeout.connect(self._restore_popup_guard)

    def configure_popup_guard(self, widgets, enabled):
        self._restore_popup_guard()
        self._popup_guard_enabled = bool(enabled)
        self._popup_guard_widgets = [widget for widget in widgets if widget is not None]

    def _restore_popup_guard(self):
        self._popup_guard_timer.stop()
        for widget in self._guarded_widgets:
            try:
                widget.setGraphicsEffect(None)
                widget.update()
            except RuntimeError:
                pass
        self._guarded_widgets.clear()

    def showPopup(self):
        self._restore_popup_guard()
        super().showPopup()

        if not self._popup_guard_enabled or not self._popup_guard_widgets:
            return

        popup = self.view().window()
        popup_rect = popup.geometry()

        for widget in self._popup_guard_widgets:
            if not widget.isVisible():
                continue

            top_left = widget.mapToGlobal(widget.rect().topLeft())
            widget_rect = widget.rect()
            widget_rect.moveTopLeft(top_left)

            if popup_rect.intersects(widget_rect):
                effect = QGraphicsOpacityEffect(widget)
                effect.setOpacity(0.0)
                widget.setGraphicsEffect(effect)
                self._guarded_widgets.append(widget)

        if self._guarded_widgets:
            self._popup_guard_timer.start()

    def hidePopup(self):
        super().hidePopup()
        self._restore_popup_guard()

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)

        painter.drawComplexControl(QStyle.CC_ComboBox, option)

        rect = self.style().subControlRect(
            QStyle.CC_ComboBox,
            option,
            QStyle.SC_ComboBoxEditField,
            self,
        )

        text = self.currentText()
        if self.currentIndex() < 0:
            text = self.placeholderText()

        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(option.palette.color(group, QPalette.Text))
        painter.drawText(rect, Qt.AlignCenter, text)


class CenteredComboDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignCenter


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.fn())
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class SonyRemote(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.mica_enabled = bool(config["Mica"] and supports_mica())
        self.api = SonyApi(config["Host"], config["PreSharedKey"], timeout=NETWORK_TIMEOUT_SECONDS)
        self.pool = QThreadPool(self)
        self._workers = set()
        self.poll_running = False
        self.initialization_running = False
        self.initialized = False
        self.last_power = None
        self.last_volume = None
        self._really_close = False
        self._shutting_down = False

        self._build_ui()
        self._build_favorites_from_config()
        self._configure_combo_popup_guards()
        self._apply_style()
        self._setup_tray()

        self._config_digest = self._get_config_digest()
        self._config_restart_pending = False
        self._setup_config_watcher()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self.poll_status)

        self.set_controls_enabled(False, power_enabled=False)
        QTimer.singleShot(0, self.initialize_tv)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        if self.mica_enabled:
            central.setAttribute(Qt.WA_TranslucentBackground)
        self.setCentralWidget(central)
        self.root = QVBoxLayout(central)
        self.root.setContentsMargins(12, 12, 12, 12)
        self.root.setSpacing(9)

        self.power_button = QPushButton("Connecting...")
        self.power_button.setMinimumHeight(38)
        self.power_button.clicked.connect(self.toggle_power)
        self.root.addWidget(self.power_button)

        if self.config["TextInputControls"]:
            self._build_text_input_controls()
        if self.config["ChannelControls"]:
            self._build_channel_controls()
        if self.config["AppControls"]:
            self._build_app_controls()
        if self.config["NavigationControls"]:
            self._build_navigation_controls()
        if self.config["InputControls"]:
            self._build_input_controls()
        if self.config["VolumeControls"]:
            self._build_volume_controls()
        if self.config["PlaybackControls"]:
            self._build_playback_controls()

    def _section(self, title):
        frame = QFrame()
        frame.setObjectName("section")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 8, 9, 9)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        self.root.addWidget(frame)
        return layout

    def _build_text_input_controls(self):
        layout = self._section("Text Input")

        self.text_input = TvTextEdit()
        self.text_input.submitRequested.connect(self.send_text_input)
        self.text_input.submitAndConfirmRequested.connect(
            self.send_text_input_and_confirm
        )
        layout.addWidget(self.text_input)

    def _build_channel_controls(self):
        grid = QGridLayout()
        grid.setSpacing(6)
        for column in range(3):
            grid.setColumnStretch(column, 1)

        self.channel_buttons = {}

        entries = (
            ("1", 0, 0, "Num1"),
            ("2", 0, 1, "Num2"),
            ("3", 0, 2, "Num3"),
            ("4", 1, 0, "Num4"),
            ("5", 1, 1, "Num5"),
            ("6", 1, 2, "Num6"),
            ("7", 2, 0, "Num7"),
            ("8", 2, 1, "Num8"),
            ("9", 2, 2, "Num9"),
            ("−", 3, 0, "ChannelDown"),
            ("0", 3, 1, "Num0"),
            ("+", 3, 2, "ChannelUp"),
        )

        for label, row, column, command in entries:
            button = QPushButton(label)
            button.setMinimumHeight(36)
            button.clicked.connect(
                lambda checked=False, c=command: self.send_ircc(c)
            )
            self.channel_buttons[command] = button
            grid.addWidget(button, row, column)

        self.root.addLayout(grid)

    def _build_app_controls(self):
        layout = self._section("Apps")
        self.app_combo = CenteredComboBox()
        self.app_combo.view().setAutoFillBackground(True)
        self.app_combo.view().viewport().setAutoFillBackground(True)
        self.app_combo.setItemDelegate(CenteredComboDelegate(self.app_combo))
        self.app_combo.activated.connect(self.launch_selected_app)
        layout.addWidget(self.app_combo)
        self.app_favorites_layout = QGridLayout()
        self.app_favorites_layout.setSpacing(6)
        self.app_favorites_layout.setColumnStretch(0, 1)
        self.app_favorites_layout.setColumnStretch(1, 1)
        layout.addLayout(self.app_favorites_layout)

    def _build_input_controls(self):
        layout = self._section("Inputs")
        self.input_combo = CenteredComboBox()
        self.input_combo.view().setAutoFillBackground(True)
        self.input_combo.view().viewport().setAutoFillBackground(True)
        self.input_combo.setItemDelegate(CenteredComboDelegate(self.input_combo))
        self.input_combo.activated.connect(self.select_input)
        layout.addWidget(self.input_combo)
        self.input_favorites_layout = QGridLayout()
        self.input_favorites_layout.setSpacing(6)
        self.input_favorites_layout.setColumnStretch(0, 1)
        self.input_favorites_layout.setColumnStretch(1, 1)
        layout.addLayout(self.input_favorites_layout)

    def _build_navigation_controls(self):
        layout = self._section("Navigation")
        grid = QGridLayout()
        grid.setSpacing(6)
        for column in range(3):
            grid.setColumnStretch(column, 1)

        for text, row, col, command in (
            ("↑", 0, 1, "Up"), ("←", 1, 0, "Left"),
            ("OK", 1, 1, "Confirm"), ("→", 1, 2, "Right"),
            ("↓", 2, 1, "Down"),
        ):
            button = QPushButton(text)
            button.setMinimumHeight(36)
            button.clicked.connect(lambda checked=False, c=command: self.send_ircc(c))
            grid.addWidget(button, row, col)
        layout.addLayout(grid)
        row = QHBoxLayout()
        back = QPushButton("Back")
        home = QPushButton("Home")
        back.clicked.connect(lambda: self.send_ircc("Return"))
        home.clicked.connect(lambda: self.send_ircc("Home"))
        row.addWidget(back)
        row.addWidget(home)
        layout.addLayout(row)

    def _build_playback_controls(self):
        layout = self._section("Playback")
        row = QHBoxLayout()
        row.setSpacing(6)
        for text, command in (
            ("Prev", "Prev"), ("Rew", "Rewind"),
            ("Play / Pause", "PlayPause"), ("Fwd", "Forward"),
            ("Next", "Next"),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, c=command: self.send_ircc(c))
            row.addWidget(button)
        layout.addLayout(row)

    def _build_volume_controls(self):
        layout = self._section("Volume")
        row = QGridLayout()
        row.setSpacing(6)
        for column in range(3):
            row.setColumnStretch(column, 1)

        self.volume_down = QPushButton("−")
        self.volume_value = QLabel("—")
        self.volume_value.setObjectName("volumeValue")
        self.volume_value.setAlignment(Qt.AlignCenter)
        self.volume_up = QPushButton("+")
        self.volume_down.clicked.connect(lambda: self.adjust_volume(-1))
        self.volume_up.clicked.connect(lambda: self.adjust_volume(1))

        row.addWidget(self.volume_down, 0, 0)
        row.addWidget(self.volume_value, 0, 1)
        row.addWidget(self.volume_up, 0, 2)
        layout.addLayout(row)

        self.mute_button = QPushButton("Mute")
        self.mute_button.clicked.connect(self.toggle_mute)
        layout.addWidget(self.mute_button)

    def _fit_and_lock_window(self):
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _apply_style(self):
        palette = self.palette()

        if self.config["DarkMode"]:
            if self.mica_enabled:
                palette.setColor(QPalette.Window, QColor(0, 0, 0, 0))
            else:
                palette.setColor(QPalette.Window, QColor(32, 32, 32))

            palette.setColor(QPalette.WindowText, QColor(235, 235, 235))
            self.setPalette(palette)

            self.setStyleSheet("""
                QMainWindow, QWidget#central {
                    background: transparent;
                }
                QWidget {
                    color: #f0f0f0;
                    font-size: 9pt;
                }
                QFrame#section {
                    background: rgba(38, 38, 38, 185);
                    border: 1px solid rgba(120, 120, 120, 110);
                    border-radius: 8px;
                }
                QLabel {
                    background: transparent;
                }
                QLabel#sectionTitle {
                    color: #d6d6d6;
                    font-weight: 600;
                }
                QLabel#volumeValue {
                    font-size: 13pt;
                    font-weight: 600;
                }
                QPushButton, QComboBox, QTextEdit {
                    background-color: rgba(48, 48, 48, 185);
                    color: rgb(235, 235, 235);
                    border: 1px solid rgba(120, 120, 120, 150);
                    border-radius: 7px;
                    padding: 5px 7px;
                    min-height: 22px;
                }
                QComboBox::drop-down {
                    border: none;
                    width: 0px;
                }
                QComboBox::down-arrow {
                    image: none;
                    width: 0px;
                    height: 0px;
                }
                QPushButton:hover, QComboBox:hover {
                    background-color: rgba(68, 68, 68, 205);
                }
                QPushButton:pressed {
                    background-color: rgba(38, 38, 38, 220);
                }
                QPushButton:disabled, QComboBox:disabled, QTextEdit:disabled {
                    color: rgba(235, 235, 235, 100);
                    background-color: rgba(48, 48, 48, 100);
                }
                QComboBox QAbstractItemView {
                    background-color: rgb(40, 40, 40);
                    color: rgb(235, 235, 235);
                    selection-background-color: rgb(80, 110, 170);
                    border: 1px solid rgb(120, 120, 120);
                    text-align: center;
                }
            """)
        else:
            if self.mica_enabled:
                palette.setColor(QPalette.Window, QColor(0, 0, 0, 0))
            self.setPalette(palette)

            self.setStyleSheet("""
                QMainWindow, QWidget#central {
                    background: transparent;
                }
                QWidget {
                    font-size: 9pt;
                }
                QFrame#section {
                    background: rgba(255,255,255,190);
                    border: 1px solid rgba(0,0,0,25);
                    border-radius: 8px;
                }
                QLabel {
                    background: transparent;
                }
                QLabel#sectionTitle {
                    font-weight: 600;
                }
                QLabel#volumeValue {
                    font-size: 13pt;
                    font-weight: 600;
                }
                QPushButton, QComboBox, QTextEdit {
                    background: rgba(255,255,255,215);
                    border: 1px solid rgba(0,0,0,35);
                    border-radius: 7px;
                    padding: 5px 7px;
                    min-height: 22px;
                }
                QComboBox::drop-down {
                    border: none;
                    width: 0px;
                }
                QComboBox::down-arrow {
                    image: none;
                    width: 0px;
                    height: 0px;
                }
                QComboBox QAbstractItemView {
                    background-color: rgb(250, 250, 250);
                    color: rgb(20, 20, 20);
                    selection-background-color: rgb(210, 225, 250);
                    border: 1px solid rgb(180, 180, 180);
                    text-align: center;
                }
                QPushButton:hover, QComboBox:hover {
                    background: rgba(245,245,245,235);
                }
            """)

    def showEvent(self, event):
        super().showEvent(event)

        if self.mica_enabled:
            apply_mica(int(self.winId()))

        if self.config["DarkMode"] and sys.platform == "win32":
            set_dark_titlebar(int(self.winId()))

    def run_worker(self, fn, on_result=None, on_error=None, on_finished=None):
        if self._shutting_down:
            return

        worker = Worker(fn)
        self._workers.add(worker)

        if on_result:
            worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error or self.show_error)
        if on_finished:
            worker.signals.finished.connect(on_finished)

        def release_worker():
            self._workers.discard(worker)

        worker.signals.finished.connect(release_worker)
        self.pool.start(worker)

    def _build_favorites_from_config(self):
        if hasattr(self, "app_favorites_layout"):
            app_items = []
            for uri, settings in self.config.get("Apps", {}).items():
                if not isinstance(settings, dict):
                    continue
                if settings.get("Hidden", False) or not settings.get("Favorite", False):
                    continue

                name = str(settings.get("Name", "")).strip()
                override = str(settings.get("NameOverride", "")).strip()
                label = override or name or uri
                app_items.append((label, uri))

            app_items.sort(key=lambda item: item[0].casefold())

            for index, (label, uri) in enumerate(app_items):
                button = QPushButton(label)
                button.clicked.connect(
                    lambda checked=False, value=uri: self.launch_app_by_uri(value)
                )
                row, column = divmod(index, 2)
                self.app_favorites_layout.addWidget(button, row, column)

        if hasattr(self, "input_favorites_layout"):
            input_items = []
            for uri, settings in self.config.get("Inputs", {}).items():
                if not isinstance(settings, dict):
                    continue
                if settings.get("Hidden", False) or not settings.get("Favorite", False):
                    continue

                name = str(settings.get("Name", "")).strip()
                override = str(settings.get("NameOverride", "")).strip()
                label = override or name or uri
                input_items.append((label, uri))

            input_items.sort(key=lambda item: item[0].casefold())

            for index, (label, uri) in enumerate(input_items):
                button = QPushButton(label)
                button.clicked.connect(
                    lambda checked=False, value=uri: self.set_input_by_uri(value)
                )
                row, column = divmod(index, 2)
                self.input_favorites_layout.addWidget(button, row, column)

    def _configure_combo_popup_guards(self):
        # Only guard the first four favorite buttons. They sit directly below
        # the dropdown and spend the longest time underneath its opening
        # animation; guarding lower controls is visually more noticeable.
        app_favorites = []
        if hasattr(self, "app_favorites_layout"):
            for index in range(self.app_favorites_layout.count()):
                item = self.app_favorites_layout.itemAt(index)
                widget = item.widget()
                if isinstance(widget, QPushButton):
                    app_favorites.append(widget)

        input_favorites = []
        if hasattr(self, "input_favorites_layout"):
            for index in range(self.input_favorites_layout.count()):
                item = self.input_favorites_layout.itemAt(index)
                widget = item.widget()
                if isinstance(widget, QPushButton):
                    input_favorites.append(widget)

        enabled = bool(self.config.get("DropdownAnimationWorkaround", False))

        if hasattr(self, "app_combo"):
            self.app_combo.configure_popup_guard(
                app_favorites[:POPUP_GUARD_FAVORITE_COUNT],
                enabled,
            )
        if hasattr(self, "input_combo"):
            self.input_combo.configure_popup_guard(
                input_favorites[:POPUP_GUARD_FAVORITE_COUNT],
                enabled,
            )

    def _get_config_digest(self):
        try:
            return hashlib.sha256(CONFIG_PATH.read_bytes()).digest()
        except Exception:
            return None

    def _setup_config_watcher(self):
        self.config_watcher = QFileSystemWatcher(self)

        if CONFIG_PATH.exists():
            self.config_watcher.addPath(str(CONFIG_PATH))

        parent = str(CONFIG_PATH.parent)
        if parent not in self.config_watcher.directories():
            self.config_watcher.addPath(parent)

        self.config_watcher.fileChanged.connect(self._config_path_changed)
        self.config_watcher.directoryChanged.connect(self._config_path_changed)

        self.config_restart_timer = QTimer(self)
        self.config_restart_timer.setSingleShot(True)
        self.config_restart_timer.setInterval(CONFIG_RESTART_DEBOUNCE_MS)
        self.config_restart_timer.timeout.connect(self._check_external_config_change)

    def _config_path_changed(self, _path):
        if self._shutting_down or self._config_restart_pending:
            return

        # Many editors save by replacing the file, which removes a direct
        # QFileSystemWatcher file watch. The directory watch lets us re-arm it.
        config_path = str(CONFIG_PATH)
        if CONFIG_PATH.exists() and config_path not in self.config_watcher.files():
            self.config_watcher.addPath(config_path)

        self.config_restart_timer.start()

    def _check_external_config_change(self):
        if self._shutting_down or self._config_restart_pending:
            return

        digest = self._get_config_digest()
        if digest is None or digest == self._config_digest:
            return

        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                candidate = json.load(f)
            if not isinstance(candidate, dict):
                return
        except Exception:
            return

        self._config_restart_pending = True
        self._restart_for_config_change()

    def _restart_for_config_change(self):
        # Complete in-flight work first. Then release the single-instance
        # guard before starting the replacement process.
        self._shutdown()
        release_single_instance()

        try:
            script_path = Path(__file__).resolve()
            if getattr(sys, "frozen", False):
                command = [sys.executable, *sys.argv[1:]]
            else:
                command = [sys.executable, str(script_path), *sys.argv[1:]]

            subprocess.Popen(
                command,
                cwd=str(script_path.parent),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                APP_NAME,
                f"Could not restart after SonyRemote.json changed:\n\n{exc}",
            )

        QApplication.quit()

    def _save_config_without_restart(self):
        save_config(self.config)
        self._config_digest = self._get_config_digest()

        # Re-arm the file watch if a save implementation ever replaces it.
        config_path = str(CONFIG_PATH)
        if CONFIG_PATH.exists() and config_path not in self.config_watcher.files():
            self.config_watcher.addPath(config_path)

    def _update_channel_button_availability(self, enabled):
        if not hasattr(self, "channel_buttons"):
            return

        remote_codes = getattr(self.api, "remote_codes", {})
        for command, button in self.channel_buttons.items():
            button.setEnabled(bool(enabled and command in remote_codes))

    def set_controls_enabled(
        self,
        enabled,
        power_enabled=None,
        app_enabled=None,
    ):
        if power_enabled is None:
            power_enabled = enabled
        if app_enabled is None:
            app_enabled = enabled

        self.power_button.setEnabled(power_enabled)

        for widget in (
            getattr(self, "text_input", None),
            getattr(self, "input_combo", None),
            getattr(self, "mute_button", None),
            getattr(self, "volume_down", None),
            getattr(self, "volume_up", None),
        ):
            if widget is not None:
                widget.setEnabled(enabled)

        # Start from the general state for all ordinary buttons.
        for button in self.findChildren(QPushButton):
            if button is self.power_button:
                continue
            button.setEnabled(enabled)

        # App launching works while the TV is in standby and can wake it, so
        # App controls have their own availability state.
        if hasattr(self, "app_combo"):
            self.app_combo.setEnabled(app_enabled)

        if hasattr(self, "app_favorites_layout"):
            for index in range(self.app_favorites_layout.count()):
                item = self.app_favorites_layout.itemAt(index)
                button = item.widget()
                if isinstance(button, QPushButton):
                    button.setEnabled(app_enabled)

        # Channel commands vary between Sony models/regions. Keep unsupported
        # commands disabled even when the rest of the connected UI is active.
        self._update_channel_button_availability(enabled)

    def initialize_tv(self):
        if self._shutting_down or self.initialization_running:
            return

        self.initialization_running = True

        def initialize():
            data = {}

            # Only discover IRCC codes when the enabled UI actually needs them.
            if (
                self.config["TextInputControls"]
                or self.config["ChannelControls"]
                or self.config["NavigationControls"]
                or self.config["PlaybackControls"]
            ):
                self.api.load_remote_codes()

            if self.config["AppControls"]:
                data["apps"] = self.api.get_apps()
            if self.config["InputControls"]:
                data["inputs"] = self.api.get_inputs()
            return data

        def done(data):
            config_changed = False

            if "apps" in data:
                config_changed |= self.sync_app_config(data["apps"])
                self.populate_apps(data["apps"])

            if "inputs" in data:
                config_changed |= self.sync_input_config(data["inputs"])
                self.populate_inputs(data["inputs"])

            if config_changed:
                try:
                    self._save_config_without_restart()
                except Exception as exc:
                    self.show_error(f"Could not update SonyRemote.json:\n\n{exc}")

            self.initialized = True
            self.set_controls_enabled(
                False,
                power_enabled=True,
                app_enabled=True,
            )
            self._update_polling_state(refresh=True)

        def failed(_message):
            self.initialized = False
            self.power_button.setText("TV unavailable")
            self.set_controls_enabled(False, power_enabled=False)
            self._update_polling_state()

        def finished():
            self.initialization_running = False

        self.run_worker(initialize, done, failed, finished)

    def sync_app_config(self, entries):
        changed = False
        apps_config = self.config.setdefault("Apps", {})

        for entry in entries:
            uri = str(entry.get("uri", "")).strip()
            name = html.unescape(str(entry.get("title", ""))).strip()
            if not uri or not name:
                continue

            item = apps_config.get(uri)
            if not isinstance(item, dict):
                item = {}
                apps_config[uri] = item
                changed = True

            if item.get("Name") != name:
                item["Name"] = name
                changed = True
            if "NameOverride" not in item:
                item["NameOverride"] = ""
                changed = True
            if "Hidden" not in item:
                item["Hidden"] = False
                changed = True
            if "Favorite" not in item:
                item["Favorite"] = False
                changed = True

        return changed

    def sync_input_config(self, entries):
        changed = False
        inputs_config = self.config.setdefault("Inputs", {})

        for entry in entries:
            uri = str(entry.get("uri", "")).strip()
            name = html.unescape(str(entry.get("title", ""))).strip()
            if not uri or not name:
                continue

            item = inputs_config.get(uri)
            if not isinstance(item, dict):
                item = {}
                inputs_config[uri] = item
                changed = True

            if item.get("Name") != name:
                item["Name"] = name
                changed = True
            if "NameOverride" not in item:
                item["NameOverride"] = ""
                changed = True
            if "Hidden" not in item:
                item["Hidden"] = False
                changed = True
            if "Favorite" not in item:
                item["Favorite"] = False
                changed = True

        return changed

    def populate_apps(self, entries):
        apps_config = self.config.get("Apps", {})
        apps = []

        for entry in entries:
            uri = str(entry.get("uri", "")).strip()
            tv_name = html.unescape(str(entry.get("title", ""))).strip()
            if not uri or not tv_name:
                continue

            settings = apps_config.get(uri, {})
            if bool(settings.get("Hidden", False)):
                continue

            name = str(settings.get("Name", tv_name)).strip() or tv_name
            override = str(settings.get("NameOverride", "")).strip()
            label = override or name
            favorite = bool(settings.get("Favorite", False))

            apps.append((label, uri, favorite))

        apps.sort(key=lambda item: item[0].casefold())

        self.app_combo.blockSignals(True)
        self.app_combo.clear()
        self.app_combo.setPlaceholderText("Select app")
        for label, uri, _favorite in apps:
            self.app_combo.addItem(label, uri)
            self.app_combo.setItemData(
                self.app_combo.count() - 1, Qt.AlignCenter, Qt.TextAlignmentRole
            )
        self.app_combo.setCurrentIndex(-1)
        self.app_combo.blockSignals(False)

    def populate_inputs(self, entries):
        inputs_config = self.config.get("Inputs", {})
        inputs = []
        active_uri = None

        for entry in entries:
            uri = str(entry.get("uri", "")).strip()
            tv_name = html.unescape(str(entry.get("title", ""))).strip()
            if not uri or not tv_name:
                continue

            settings = inputs_config.get(uri, {})
            if bool(settings.get("Hidden", False)):
                continue

            name = str(settings.get("Name", tv_name)).strip() or tv_name
            override = str(settings.get("NameOverride", "")).strip()
            label = override or name
            favorite = bool(settings.get("Favorite", False))

            inputs.append((label, uri, favorite))

            if str(entry.get("status", "")).lower() == "true":
                active_uri = uri

        inputs.sort(key=lambda item: item[0].casefold())

        self.input_combo.blockSignals(True)
        self.input_combo.clear()
        self.input_combo.setPlaceholderText("Select input")

        active_index = -1
        for label, uri, _favorite in inputs:
            self.input_combo.addItem(label, uri)
            self.input_combo.setItemData(
                self.input_combo.count() - 1, Qt.AlignCenter, Qt.TextAlignmentRole
            )
            if uri == active_uri:
                active_index = self.input_combo.count() - 1

        self.input_combo.setCurrentIndex(active_index)
        self.input_combo.blockSignals(False)

    def poll_status(self):
        if self.poll_running:
            return
        self.poll_running = True

        def query():
            power = self.api.get_power()
            data = {"power": power}
            if power == "active" and self.config["VolumeControls"]:
                data["volume"] = self.api.get_volume()
            if power == "active" and self.config["InputControls"]:
                try:
                    data["inputs"] = self.api.get_inputs()
                except Exception:
                    pass
            return data

        def done(data):
            self.last_power = data.get("power")

            if not self.initialized:
                self.power_button.setText("Connecting...")
                self.set_controls_enabled(False, power_enabled=False)
                QTimer.singleShot(0, self.initialize_tv)
                return

            is_active = self.last_power == "active"
            self.set_controls_enabled(
                is_active,
                power_enabled=True,
                app_enabled=True,
            )
            self.update_power_ui(self.last_power)
            if data.get("volume"):
                self.last_volume = data["volume"]
                self.update_volume_ui(self.last_volume)
            if "inputs" in data:
                self.update_active_input(data["inputs"])

        def failed(_message):
            self.last_power = None
            self.power_button.setText("TV unavailable")
            self.set_controls_enabled(False, power_enabled=False)
            if self.config["VolumeControls"]:
                self.volume_value.setText("—")

        self.run_worker(query, done, failed, lambda: setattr(self, "poll_running", False))

    def update_power_ui(self, status):
        if status == "active":
            self.power_button.setText("Standby")
        else:
            self.power_button.setText("Power On")

    def update_volume_ui(self, volume):
        self.volume_value.setText(str(volume.get("volume", "—")))
        self.mute_button.setText("Unmute" if volume.get("mute") else "Mute")

    def update_active_input(self, entries):
        if not self.config["InputControls"]:
            return
        for entry in entries:
            if str(entry.get("status", "")).lower() == "true":
                index = self.input_combo.findData(entry.get("uri"))
                if index >= 0:
                    self.input_combo.blockSignals(True)
                    self.input_combo.setCurrentIndex(index)
                    self.input_combo.blockSignals(False)
                break

    def send_text_input(self):
        if not hasattr(self, "text_input"):
            return

        text = self.text_input.toPlainText()
        if not text:
            return

        self.run_worker(
            lambda: self.api.set_text_form(text),
            on_error=lambda _message: self.show_error(
                "Could not send text to the TV.\n\n"
                "Make sure a TV software keyboard or text field is currently active."
            ),
        )

    def send_text_input_and_confirm(self):
        if not hasattr(self, "text_input"):
            return

        text = self.text_input.toPlainText()
        if not text:
            return

        def send_and_confirm():
            # Reset Gboard's selection to its initial key before using the
            # navigation macro. If the keyboard is already open, Return closes
            # it while leaving the text field focused.
            if self.api.is_text_input_active():
                self.api.ircc("Return")
                if not self.api.wait_for_text_input_state(False):
                    raise RuntimeError("Software keyboard did not close.")

            # Confirm on the still-focused text field reopens the software
            # keyboard. Wait for Sony's status API rather than relying on a
            # fixed UI delay.
            self.api.ircc("Confirm")
            if not self.api.wait_for_text_input_state(True):
                raise RuntimeError("Software keyboard did not become active.")

            # setTextForm now doubles as the final readiness check.
            self.api.set_text_form(text)

            # With a freshly opened Gboard keyboard the active key starts at Q.
            # Left wraps to the right-side action column, then three Down
            # presses reach the contextual Search/Go/etc. button.
            self.api.ircc("Left")
            self.api.ircc("Down")
            self.api.ircc("Down")
            self.api.ircc("Down")
            self.api.ircc("Confirm")

        self.run_worker(
            send_and_confirm,
            on_error=lambda _message: self.show_error(
                "Could not send or submit the text on the TV.\n\n"
                "Make sure a TV software keyboard or text field is currently active."
            ),
        )

    def toggle_power(self):
        turn_on = self.last_power != "active"
        self.power_button.setEnabled(False)
        self.run_worker(
            lambda: self.api.set_power(turn_on),
            on_finished=lambda: (self.power_button.setEnabled(True), QTimer.singleShot(300, self.poll_status)),
        )

    def launch_selected_app(self, index):
        uri = self.app_combo.itemData(index)
        if uri:
            self.launch_app_by_uri(uri)
            self.app_combo.setCurrentIndex(-1)

    def launch_app_by_uri(self, uri):
        self.run_worker(lambda: self.api.launch_app(uri), on_finished=lambda: QTimer.singleShot(300, self.poll_status))

    def select_input(self, index):
        uri = self.input_combo.itemData(index)
        if uri:
            self.set_input_by_uri(uri)

    def set_input_by_uri(self, uri):
        self.run_worker(lambda: self.api.set_input(uri), on_finished=lambda: QTimer.singleShot(300, self.poll_status))

    def send_ircc(self, command):
        self.run_worker(lambda: self.api.ircc(command))

    def adjust_volume(self, amount):
        if not self.last_volume:
            return
        current = int(self.last_volume.get("volume", 0))
        minimum = int(self.last_volume.get("minVolume", 0))
        maximum = int(self.last_volume.get("maxVolume", 100))
        target = max(minimum, min(maximum, current + amount))
        self.volume_value.setText(str(target))
        self.run_worker(lambda: self.api.set_volume(target), on_finished=lambda: QTimer.singleShot(200, self.poll_status))

    def toggle_mute(self):
        if not self.last_volume:
            return
        target = not bool(self.last_volume.get("mute"))
        self.run_worker(lambda: self.api.set_mute(target), on_finished=lambda: QTimer.singleShot(200, self.poll_status))

    def _update_polling_state(self, refresh=False):
        if self._shutting_down or not hasattr(self, "poll_timer"):
            return

        should_poll = self.isVisible() and not self.isMinimized()

        if should_poll:
            if refresh:
                self.poll_status()
            if not self.poll_timer.isActive():
                self.poll_timer.start()
        else:
            self.poll_timer.stop()

    def _setup_tray(self):
        self.tray = None
        if not self.config["SystemTray"] or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Sony Remote")

        menu = QMenu()
        show_action = QAction("Show", self)
        quit_action = QAction("Exit", self)
        show_action.triggered.connect(self.show_from_tray)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._update_polling_state(refresh=True)

    def _shutdown(self):
        if self._shutting_down:
            return

        self._shutting_down = True
        self._really_close = True

        # Stop anything that can schedule more background work.
        self.poll_timer.stop()
        if hasattr(self, "config_restart_timer"):
            self.config_restart_timer.stop()
        if hasattr(self, "config_watcher"):
            self.config_watcher.blockSignals(True)

        # Remove the tray icon before Qt exits.
        if self.tray:
            self.tray.hide()

        # Prevent queued jobs from starting, then let already-running jobs
        # finish cleanly before QObject/QRunnable state is torn down.
        self.pool.clear()
        self.pool.waitForDone()
        self._workers.clear()

    def quit_from_tray(self):
        self._shutdown()
        QApplication.quit()

    def changeEvent(self, event):
        super().changeEvent(event)

        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(
                0,
                lambda: self._update_polling_state(
                    refresh=self.isVisible() and not self.isMinimized()
                ),
            )

    def closeEvent(self, event):
        if self.tray and self.config["CloseToTray"] and not self._really_close:
            event.ignore()
            self.hide()
            self._update_polling_state()
            return

        self._shutdown()
        event.accept()
        QApplication.quit()

    def show_error(self, message):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(message)
        box.setStandardButtons(QMessageBox.Ok)
        box.setDefaultButton(QMessageBox.Ok)

        button_box = box.findChild(QDialogButtonBox)
        if button_box is not None:
            # Use the normal platform button placement instead of centering.
            button_box.setCenterButtons(False)

        box.exec()


def main():
    if not acquire_single_instance():
        return 0

    set_windows_app_id("Sony.Remote")

    log_path = Path(__file__).with_name("SonyRemote-crash.log")
    crash_file = open(log_path, "a", encoding="utf-8")
    try:
        faulthandler.enable(crash_file)
    except Exception:
        pass

    def exception_hook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb, file=crash_file)
        crash_file.flush()
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_hook

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    # Use the same native-looking monitor/computer icon for the whole
    # application, including the Windows taskbar.
    app.setWindowIcon(app.style().standardIcon(QStyle.SP_ComputerIcon))
    try:
        config = load_config()
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"Could not load SonyRemote.json:\n\n{exc}")
        return 1
    window = SonyRemote(config)
    app.setQuitOnLastWindowClosed(window.tray is None)
    app.aboutToQuit.connect(window._shutdown)
    window._fit_and_lock_window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
