import ctypes
import copy
from collections import deque
from datetime import datetime, timezone
import faulthandler
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import sys
import tempfile
import time
import threading
import traceback
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path
from xml.sax.saxutils import escape
from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import qVersion, QStandardPaths, QByteArray, QEasingCurve, QElapsedTimer, QRect, QEvent, QFileSystemWatcher, QLockFile, QObject, QProcess, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QKeySequence, QPalette, QStyleHints
from PySide6.QtWidgets import (
    QAbstractSpinBox, QLineEdit, QPlainTextEdit, QApplication, QComboBox, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLayout, QMainWindow, QMenu, QMessageBox, QPushButton, QProxyStyle, QStyle, QTextEdit,
    QStyleOptionButton, QSizePolicy, QStyleOptionComboBox, QStylePainter, QStyledItemDelegate,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)


# Device requests connect directly; never forward LAN traffic or credentials to a proxy.
DEVICE_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ----------------------------
# Local amplifier protocol (newline-delimited JSON, version 1)
# ----------------------------


IPC_USER_SUFFIX = hashlib.sha256(str(Path.home()).encode("utf-8")).hexdigest()[:16]


def ipc_server_name(role, *, channel=None):
    channel = f"{role}.{channel}" if channel else role
    return f"remote-control.{channel}.v1.{IPC_USER_SUFFIX}"


IPC_SERVER_NAME = ipc_server_name("amplifier")
IPC_MAX_FRAME = 4096
IPC_MAX_STATE_VALIDITY_MS = 6000  # Protocol v1 wire limit.


def _ipc_encode(message):
    return (json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _ipc_frames(socket, buffer):
    messages = []
    while socket.bytesAvailable():
        # Parse between bounded reads so a burst of valid frames is allowed,
        # without allowing an oversized complete or partial frame.
        chunk = bytes(socket.read(IPC_MAX_FRAME + 1))
        if not chunk:
            break
        buffer.extend(chunk)
        while b"\n" in buffer:
            frame, _, tail = buffer.partition(b"\n")
            buffer[:] = tail
            if len(frame) > IPC_MAX_FRAME:
                raise ValueError("Local protocol frame too large")
            try:
                message = json.loads(frame)
            except RecursionError as error:
                raise ValueError("Local protocol nesting too deep") from error
            if (not isinstance(message, dict) or type(message.get("version")) is not int
                    or message["version"] != 1):
                raise ValueError("Unsupported local protocol message")
            messages.append(message)
        if len(buffer) > IPC_MAX_FRAME:
            raise ValueError("Local protocol frame too large")
    return messages


class AmplifierClient(QObject):
    state_changed = Signal(object)
    command_failed = Signal(str, str)
    command_succeeded = Signal()
    command_acknowledged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = QLocalSocket(self)
        self.socket.setReadBufferSize(IPC_MAX_FRAME + 1)
        self.buffer = bytearray()
        self.state = None
        self.state_deadline = 0
        self.sequence = 0
        self.pending = {}
        self.stopped = True
        self.focused_volume = None
        self._sent_focused_volume = None
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.setInterval(2000)
        self.retry.timeout.connect(self._connect)
        self.lease = QTimer(self)
        self.lease.setSingleShot(True)
        self.lease.timeout.connect(self._expire)
        self.socket.readyRead.connect(self._read)
        self.socket.disconnected.connect(self._lost)
        self.socket.errorOccurred.connect(lambda _error: self._lost())

    def set_focused_volume(self, window, bindings, input_id, observed_at):
        self.focused_volume = (window, tuple(bindings), input_id, observed_at)
        self._send_focused_volume()

    def _send_focused_volume(self):
        context = self.focused_volume
        if (context is None or context == self._sent_focused_volume
                or not self.state_is_fresh or not self.state.get("focused_volume_override_supported", False)
                or self.socket.state() != QLocalSocket.ConnectedState):
            return
        window, bindings, input_id, observed_at = context
        valid_for = remaining_validity_ms(time.monotonic() - observed_at, STATUS_MAX_AGE_SECONDS)
        payload = _ipc_encode({"version": 1, "type": "focused_volume", "window": window,
                               "shortcuts": bindings, "input": input_id, "valid_for_ms": valid_for})
        if self.socket.bytesToWrite() > IPC_MAX_FRAME * 4:
            self.socket.abort()  # Do not keep a permission with an undeliverable withdrawal.
            return
        if self.socket.write(payload) == len(payload):
            self._sent_focused_volume = context
        else:
            self.socket.abort()

    def start(self):
        self.stopped = False
        self._connect()

    def _connect(self):
        if not self.stopped and self.socket.state() == QLocalSocket.UnconnectedState:
            self.socket.connectToServer(IPC_SERVER_NAME)

    @property
    def state_is_fresh(self):
        return self.state is not None and time.monotonic() < self.state_deadline

    def _expire(self):
        self._sent_focused_volume = None
        self.state_deadline = 0
        self.state = None
        self.state_changed.emit(None)

    def _lost(self):
        self.lease.stop()
        self.buffer.clear()
        self._expire()
        for timer in self.pending.values():
            timer.stop()
            timer.deleteLater()
        had_pending = bool(self.pending)
        self.pending.clear()
        if had_pending and not self.stopped:
            self.command_failed.emit("Amplifier connection lost. Command was not retried.", "connection")
        if not self.stopped and not self.retry.isActive():
            self.retry.start()

    def _read(self):
        message_type = None
        try:
            for message in _ipc_frames(self.socket, self.buffer):
                message_type = message.get("type")
                if message_type == "state":
                    self._validate_state(message)
                    self.state = message
                    self.state_deadline = time.monotonic() + message["valid_for_ms"] / 1000
                    self.lease.start(max(1, message["valid_for_ms"]))
                    self.state_changed.emit(message)
                    self._send_focused_volume()
                elif message_type == "ack":
                    if type(message.get("id")) is not int or type(message.get("accepted")) is not bool:
                        raise ValueError("Invalid command acknowledgement")
                    reason = message.get("reason", "")
                    if reason not in ("", "queue_full", "state_changed"):
                        raise ValueError("Invalid command rejection reason")
                    timer = self.pending.pop(message["id"], None)
                    if timer is not None:
                        timer.stop()
                        timer.deleteLater()
                        self.command_acknowledged.emit()
                        if message["accepted"]:
                            self.command_succeeded.emit()
                        elif reason != "queue_full":
                            detail = " because its state changed" if reason == "state_changed" else ""
                            self.command_failed.emit(f"Amplifier command rejected{detail}. Command was not retried.", "rejection")
                else:
                    raise ValueError("Unknown local message")
        except (ValueError, UnicodeError, TypeError, OverflowError) as error:
            record_exception(error, "Amplifier protocol", deduplicate=True)
            self.socket.abort()
            self._lost()
            category = ("protocol_state" if message_type == "state" else
                        "protocol_ack" if message_type == "ack" else "protocol")
            self.command_failed.emit("Incompatible amplifier protocol. Update both remotes together. Commands were not retried.", category)

    @staticmethod
    def _validate_state(message):
        required = {"online", "power", "input", "raw_volume", "raw_max_volume", "mute",
                    "volume_display", "volume_unit",
                    "global_hotkeys_enabled", "global_hotkeys_active", "valid_for_ms"}
        if (not required <= message.keys() or type(message.get("valid_for_ms")) is not int
                or not 0 <= message["valid_for_ms"] <= IPC_MAX_STATE_VALIDITY_MS
                or any(type(message.get(key)) is not bool for key in
                       ("online", "global_hotkeys_enabled", "global_hotkeys_active"))
                or message.get("power") not in (None, "on", "standby")
                or not isinstance(message.get("input"), (str, type(None)))
                or not isinstance(message.get("mute"), (bool, type(None)))):
            raise ValueError("Invalid amplifier state")
        if (not isinstance(message["volume_display"], str)
                or not 1 <= len(message["volume_display"]) <= 128
                or message["volume_unit"] not in ("raw", "dB")):
            raise ValueError("Invalid amplifier volume display")
        bindings = message.get("global_shortcuts", [])
        if (not isinstance(bindings, list) or len(bindings) > 128
                or any(type(code) is not int or not 0 < code <= 0x7FFFFFFF for code in bindings)
                or len(set(bindings)) != len(bindings)):
            raise ValueError("Invalid global shortcut ownership")
        if type(message.get("focused_volume_override_supported", False)) is not bool:
            raise ValueError("Invalid focused volume capability")
        for key in ("raw_volume", "raw_max_volume"):
            value = message.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("Invalid raw volume")

    def send_command(self, command, input_id):
        # Never replay a non-idempotent volume command or fall back on ambiguity.
        if self.stopped or not self.state_is_fresh:
            return False
        if len(self.pending) >= 8:
            return None  # Backpressure: silently drop, without retry or TV fallback.
        if self.socket.state() != QLocalSocket.ConnectedState:
            return False
        self.sequence += 1
        request_id = self.sequence
        payload = _ipc_encode({"version": 1, "type": "command", "id": request_id,
                               "command": command, "input": input_id})
        if self.socket.bytesToWrite() > IPC_MAX_FRAME * 4:
            return None  # Bound outgoing traffic during a burst.
        if self.socket.write(payload) != len(payload):
            return False
        timer = QTimer(self)
        timer.setSingleShot(True)
        def expired():
            self.pending.pop(request_id, None)
            timer.deleteLater()
            self.command_failed.emit("Amplifier command acknowledgement timed out. Command was not retried.", "timeout")
        timer.timeout.connect(expired)
        self.pending[request_id] = timer
        timer.start(2500)
        return True

    def stop(self):
        self.stopped = True
        self.retry.stop()
        self.socket.abort()
        self._lost()


# ----------------------------
# Exclusive calibration sessions (generic role-local protocol)
# ----------------------------


class ActivationServer(QObject):
    """User-local window activation, separate from device commands and leases."""

    def __init__(self, role, activate, parent=None, *, dump=None):
        super().__init__(parent)
        self.role, self.activate = role, activate
        self.dump = dump
        self.handled = set()
        self.name = ipc_server_name(role, channel="activation")
        self.clients = {}
        self.lock = QLockFile(str(Path(tempfile.gettempdir()) / (self.name + ".lock")))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.setMaxPendingConnections(4)
        self.available = self.lock.tryLock(0)
        if self.available:
            QLocalServer.removeServer(self.name)
            self.available = self.server.listen(self.name)
            if not self.available:
                self.lock.unlock()
        self.server.newConnection.connect(self._accept)

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if not self.available or len(self.clients) >= 4:
                socket.abort()
                socket.deleteLater()
                continue
            self.clients[socket] = bytearray()
            socket.setReadBufferSize(IPC_MAX_FRAME + 1)
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._lost(socket))
            timer = QTimer(socket)
            timer.setObjectName("request_deadline")
            timer.setSingleShot(True)
            timer.timeout.connect(lambda socket=socket: socket.abort())
            timer.start(1000)
            socket.write(_ipc_encode({"version": 1, "type": "instance", "role": self.role,
                                      "process_id": os.getpid()}))

    def _read(self, socket):
        try:
            messages = _ipc_frames(socket, self.clients[socket])
            if not messages:
                return
            if not self.available or len(messages) != 1 or socket in self.handled:
                raise ValueError("Invalid instance request")
            self.handled.add(socket)
            if messages[0] == {"version": 1, "type": "activate"}:
                ok = bool(self.activate())
                socket.write(_ipc_encode({"version": 1, "type": "activated", "ok": ok}))
                socket.disconnectFromServer()
            elif (messages[0] in ({"version": 1, "type": "dump_state"},
                                  {"version": 1, "type": "dump_state_full"}) and self.dump is not None):
                socket.findChild(QTimer, "request_deadline").start(5000)
                def completed(result):
                    if socket in self.clients:
                        socket.write(_ipc_encode(dict(result, version=1, type="state_dump")))
                        socket.disconnectFromServer()
                if messages[0]["type"] == "dump_state_full":
                    self.dump(completed, full=True)
                else:
                    self.dump(completed)
            else:
                raise ValueError("Unsupported instance request")
        except (ValueError, UnicodeError, KeyError):
            socket.abort()

    def _lost(self, socket):
        if socket in self.clients:
            del self.clients[socket]
            self.handled.discard(socket)
            socket.deleteLater()

    def close(self):
        self.available = False
        for socket in list(self.clients):
            socket.abort()
        self.server.close()
        self.lock.unlock()


def _allow_foreground_activation(process_id):
    if sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
            user32.AllowSetForegroundWindow.restype = ctypes.c_int
            user32.AllowSetForegroundWindow(process_id)
        except (OSError, AttributeError):
            pass  # Window restoration still works if foreground permission is denied.


def _request_existing_instance(role, request_type, timeout_seconds):
    """Bound startup retries and the complete handshake to one deadline."""
    socket = QLocalSocket()
    socket.setReadBufferSize(IPC_MAX_FRAME + 1)
    buffer = bytearray()
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def receive(expected):
        while remaining():
            messages = _ipc_frames(socket, buffer)
            if messages:
                if len(messages) != 1 or messages[0].get("type") != expected:
                    return None
                return messages[0]
            if not socket.waitForReadyRead(int(remaining() * 1000)) and not socket.bytesAvailable():
                return None
        return None

    try:
        while remaining():
            socket.connectToServer(ipc_server_name(role, channel="activation"))
            if socket.waitForConnected(int(min(0.15, remaining()) * 1000)):
                break
            socket.abort()
            time.sleep(min(0.05, remaining()))
        else:
            return None
        hello = receive("instance")
        if (not hello or hello.get("role") != role or type(hello.get("process_id")) is not int
                or not 0 < hello["process_id"] <= 0xFFFFFFFF):
            return None
        if request_type == "activate":
            _allow_foreground_activation(hello["process_id"])
        payload = _ipc_encode({"version": 1, "type": request_type})
        if socket.write(payload) != len(payload):
            return None
        if socket.bytesToWrite() and not socket.waitForBytesWritten(int(remaining() * 1000)):
            return None
        return receive("activated" if request_type == "activate" else "state_dump")
    except (ValueError, UnicodeError, OSError, TypeError):
        return None
    finally:
        socket.abort()



def activate_existing_instance(role, timeout_seconds=1.5):
    reply = _request_existing_instance(role, "activate", timeout_seconds)
    return bool(reply and reply.get("ok") is True)


def dump_existing_state(role, *, full=False):
    reply = _request_existing_instance(role, "dump_state_full" if full else "dump_state", 5.0)
    if reply and reply.get("ok") is True and isinstance(reply.get("path"), str):
        message = "Cached state saved to:\n" + reply["path"]
        if reply.get("fallback"):
            message += "\n(The script folder was unavailable; the app data folder was used.)"
        if full:
            message += ("\n\nPrivacy warning: This full report may contain device/network identifiers "
                        "and app inventories. Review it before sharing.")
        ok = True
    else:
        error = reply.get("error", "unavailable") if reply else "unavailable"
        message = ("No state dump was confirmed. Start the remote first, or wait until it is responsive."
                   if error == "unavailable" else "Could not write the cached state dump (" + str(error) + ").")
        ok = False
    if sys.stdout is not None:
        print(message)
    else:
        (QMessageBox.information if ok else QMessageBox.warning)(None, APP_NAME + " - State dump", message)
    return 0 if ok else 1


class CalibrationServer(QObject):
    """A socket-owned, expiring lease. No device I/O in protocol callbacks."""

    def __init__(self, role, snapshot, changed, perform, parent=None):
        super().__init__(parent)
        self.snapshot, self.changed, self.perform = snapshot, changed, perform
        self.owner = None
        self.token = None
        self.busy = False
        self.sockets = {}
        self.name = ipc_server_name(role, channel="calibration")
        self.lock = QLockFile(str(Path(tempfile.gettempdir()) / (self.name + ".lock")))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.setMaxPendingConnections(4)
        self.lease = QTimer(self)
        self.lease.setSingleShot(True)
        self.lease.setInterval(8000)
        self.lease.timeout.connect(self._expire)
        self.available = self.lock.tryLock(0)
        if self.available:
            QLocalServer.removeServer(self.name)
            self.available = self.server.listen(self.name)
            if not self.available:
                self.lock.unlock()
        self.server.newConnection.connect(self._accept)

    @property
    def active(self):
        return self.owner is not None

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self.sockets) >= 4:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(IPC_MAX_FRAME + 1)
            self.sockets[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._lost(socket))
            self._send(socket, self.snapshot())

    def _send(self, socket, message):
        if socket not in self.sockets:
            return
        if socket.bytesToWrite() > IPC_MAX_FRAME * 4:
            socket.abort()
        else:
            socket.write(_ipc_encode(message))

    def _reply(self, socket, request_id, ok, error=""):
        self._send(socket, {"version": 1, "type": "result", "id": request_id,
                            "ok": bool(ok), "error": str(error)[:200]})

    def _read(self, socket):
        try:
            for message in _ipc_frames(socket, self.sockets[socket]):
                request_id, action, token = (message.get(key) for key in ("id", "action", "token"))
                if (message.get("type") != "calibration" or type(request_id) is not int
                        or not isinstance(action, str) or not isinstance(token, str)
                        or not 16 <= len(token) <= 80 or not isinstance(message.get("args", {}), dict)):
                    raise ValueError("Invalid calibration request")
                if action == "acquire":
                    if self.active:
                        self._reply(socket, request_id, False, "Another calibration session is active.")
                        continue
                    self.owner, self.token = socket, token
                    self.lease.start()
                    self.changed(True)
                elif socket is not self.owner or token != self.token:
                    self._reply(socket, request_id, False, "Calibration session is not owned by this connection.")
                    continue
                if action == "renew":
                    self.lease.start()
                    self._reply(socket, request_id, True)
                    continue
                if action == "release":
                    if self.busy:
                        self._reply(socket, request_id, False, "A device operation is still running.")
                    else:
                        self._expire()
                        self._reply(socket, request_id, True)
                    continue
                if self.busy:
                    self._reply(socket, request_id, False, "A device operation is still running.")
                    continue
                self.busy = True
                def done(ok, error="", socket=socket, request_id=request_id, token=token, action=action):
                    if socket is self.owner and token == self.token:
                        self.busy = False
                        self._reply(socket, request_id, ok, error)
                        if action == "acquire" and not ok:
                            self._expire()
                self.perform(action, message.get("args", {}), done)
        except (ValueError, UnicodeError, KeyError):
            socket.abort()

    def publish(self):
        state = self.snapshot()
        for socket in list(self.sockets):
            self._send(socket, state)

    def _expire(self):
        was_active = self.active
        self.owner = self.token = None
        self.busy = False
        self.lease.stop()
        if was_active:
            self.changed(False)

    def _lost(self, socket):
        if socket is self.owner:
            self._expire()
        if socket in self.sockets:
            del self.sockets[socket]
            socket.deleteLater()

    def close(self):
        self._expire()
        for socket in list(self.sockets):
            socket.abort()
        self.server.close()
        self.lock.unlock()


# ----------------------------
# Application constants
# ----------------------------


APP_NAME = "Sony Remote"


__version__ = "2026.10.29.1"


CONFIG_PATH = Path(__file__).resolve().with_name("SonyRemote.json")


INSTANCE_MUTEX_NAME = "Local\\SonyRemote.SingleInstance"


NETWORK_TIMEOUT_SECONDS = 2.0


POLL_INTERVAL_MS = 2000


STATUS_MAX_AGE_SECONDS = 6.0


MAX_PENDING_COMMANDS = 16
VOLUME_NO_PROGRESS_SECONDS = 5.0


CONFIG_RESTART_DEBOUNCE_MS = 250


POPUP_ANIMATION_DURATION_MS = 200


TEXT_INPUT_STATE_TIMEOUT_SECONDS = 1.5


TEXT_INPUT_STATE_POLL_SECONDS = 0.05


# ----------------------------
# Configuration
# ----------------------------


DEFAULT_SHORTCUTS = {
    "VolumeUp": ["Volume Up"],
    "VolumeDown": ["Volume Down"],
    "Mute": ["Volume Mute"],
    "FocusTextInput": ["Shift+Return", "Shift+Enter", "Shift+Num+Enter"],
    "Left": ["Left", "Num+Left"],
    "Right": ["Right", "Num+Right"],
    "Up": ["Up", "Num+Up"],
    "Down": ["Down", "Num+Down"],
    "Confirm": ["Return", "Enter", "Num+Enter"],
    "Return": ["Backspace"],
    "Home": ["Home", "Num+Home"],
    "TvInput": ["Ins", "Num+Ins"],
    "Settings": ["Del", "Num+Del"],
    "ChannelUp": ["Num++"],
    "ChannelDown": ["Num+-"],
    "PlayPause": ["Toggle Media Play/Pause"],
    "Play": ["Media Play"],
    "Pause": ["Media Pause"],
    "Stop": ["Media Stop"],
    "Next": ["Media Next"],
    "Previous": ["Media Previous"],
    "Num0": ["Num+0"],
    "Num1": ["Num+1"],
    "Num2": ["Num+2"],
    "Num3": ["Num+3"],
    "Num4": ["Num+4"],
    "Num5": ["Num+5"],
    "Num6": ["Num+6"],
    "Num7": ["Num+7"],
    "Num8": ["Num+8"],
    "Num9": ["Num+9"],
}


SHORTCUT_ACTIONS = {
    "VolumeUp": "volume_up",
    "VolumeDown": "volume_down",
    "Mute": "mute",
    "FocusTextInput": "focus_text_input",
    "Left": "Left",
    "Right": "Right",
    "Up": "Up",
    "Down": "Down",
    "Confirm": "Confirm",
    "Return": "Return",
    "Home": "Home",
    "TvInput": "TvInput",
    "Settings": "Settings",
    "ChannelUp": "ChannelUp",
    "ChannelDown": "ChannelDown",
    "PlayPause": "PlayPause",
    "Play": "Play",
    "Pause": "Pause",
    "Stop": "Stop",
    "Next": "Next",
    "Previous": "Prev",
    "Num0": "Num0",
    "Num1": "Num1",
    "Num2": "Num2",
    "Num3": "Num3",
    "Num4": "Num4",
    "Num5": "Num5",
    "Num6": "Num6",
    "Num7": "Num7",
    "Num8": "Num8",
    "Num9": "Num9",
}


DEFAULT_CONFIG = {
    "Shortcuts": DEFAULT_SHORTCUTS,
    "Host": "",
    "PreSharedKey": "",
    "DarkMode": True,
    "Mica": True,
    "TextInputControls": True,
    "TextInputSubmit": True,
    "TextInputSubmitSequence": [
        "Left",
        "Down",
        "Down",
        "Down",
        "Confirm",
    ],
    "InputControls": False,
    "AppControls": True,
    "ChannelControls": True,
    "NavigationControls": True,
    "NavigationControlsCompact": False,
    "PlaybackControls": True,
    "VolumeControls": True,
    "AmplifierTVInput": "",
    "DefaultTvVolume": {"Volume": None, "DelayMs": 1000, "RepeatCount": 0, "RepeatIntervalMs": 200},
    "VolumeRateLimit": {
        "Active": True,
        "RateMs": 600,
        "VerificationDelayMs": 500,
    },
    "DiagnosticsEnabled": False,
    "SMTCPlaybackKeys": False,
    "SortByName": False,
    "Apps": {},
    "Inputs": {},
    "SystemTray": False,
    "CloseToTray": False,
    "DropdownAnimationWorkaround": sys.platform == "win32",
}


BOOLEAN_CONFIG_KEYS = (
    "SMTCPlaybackKeys",
    "DiagnosticsEnabled",
    "SortByName",
    "DarkMode",
    "Mica",
    "TextInputControls",
    "TextInputSubmit",
    "InputControls",
    "AppControls",
    "ChannelControls",
    "NavigationControls",
    "NavigationControlsCompact",
    "PlaybackControls",
    "VolumeControls",
    "SystemTray",
    "CloseToTray",
    "DropdownAnimationWorkaround",
)


def validate_runtime_mapping(document):
    """Validate only the completed lookup and its device-routing information."""
    if (not isinstance(document, dict) or type(document.get("format_version")) is not int
            or document["format_version"] != 2):
        raise ValueError("Unsupported volume calibration document.")
    for key in ("tv_device_id", "amplifier_device_id", "amplifier_tv_input"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise ValueError(f"Invalid calibration field: {key}.")
    levels = document.get("tv_volume_by_amplifier_raw_volume")
    if not isinstance(levels, list) or not 2 <= len(levels) <= 10001:
        raise ValueError("Lookup must cover amplifier raw zero through its calibrated maximum.")
    previous = 0
    for level in levels:
        if type(level) is not int or level < previous:
            raise ValueError("Mapped TV volumes must be nonnegative, integer, and nondecreasing.")
        previous = level
    estimated = document.get("estimated_amplifier_raw_volumes", [])
    if (not isinstance(estimated, list)
            or any(type(raw) is not int or not 0 <= raw < len(levels) for raw in estimated)
            or len(set(estimated)) != len(estimated)):
        raise ValueError("Invalid estimated amplifier raw volumes.")
    return document


def prepare_volume_mapping(document):
    """Build Sony's display lookup from a validated runtime document."""
    validate_runtime_mapping(document)
    levels = document["tv_volume_by_amplifier_raw_volume"]
    estimated = set(document.get("estimated_amplifier_raw_volumes", []))
    lookup = []
    start = 0
    while start < len(levels):
        end = start + 1
        while end < len(levels) and levels[end] == levels[start]:
            end += 1
        count = end - start
        for raw in range(start, end):
            # Round i/N to tenths without floating-point ties, and never
            # round a member of TV bucket T into the next integer T+1.
            fraction = min(9, (20 * (raw - start) + count) // (2 * count))
            lookup.append({"display_tenths": 10 * levels[raw] + fraction,
                           "source": "interpolated" if raw in estimated else "mapped"})
        start = end
    return {"input": document["amplifier_tv_input"],
            "tv_id": document["tv_device_id"], "amplifier_id": document["amplifier_device_id"],
            "raw_max": len(levels) - 1,
            "lookup": lookup}


def mapped_tv_volume(mapping, raw):
    bounded = max(0, min(mapping["raw_max"], raw))
    index = int(math.floor(bounded + 0.5))
    entry = mapping["lookup"][index]
    tenths = entry["display_tenths"]
    display = tenths // 10 if tenths % 10 == 0 else tenths / 10
    return display, bounded != index or entry["source"] == "interpolated"


class ConfigError(ValueError):
    pass


def _require_bool(config, key, default):
    value = config.get(key, default)

    if not isinstance(value, bool):
        raise ConfigError(
            f'"{key}" must be true or false.'
        )

    return value


def _validate_host(value):
    if not isinstance(value, str):
        raise ConfigError(
            '"Host" must be a string.'
        )

    host = value.strip()

    if not host:
        return host

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if len(host) > 253:
        raise ConfigError(
            '"Host" is too long.'
        )

    labels = host.rstrip(".").split(".")
    if all(label.isdigit() for label in labels):
        raise ConfigError(
            '"Host" looks like an IP address but is not a valid one.'
        )

    hostname_label = re.compile(
        r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
    )

    if not labels or any(
        not hostname_label.fullmatch(label)
        for label in labels
    ):
        raise ConfigError(
            '"Host" must be a valid IP address or hostname.'
        )

    return host


def _validate_entries(value, collection):
    if not isinstance(value, dict):
        raise ConfigError(
            f'"{collection}" must be an object.'
        )

    normalized = {}

    for input_id, settings in value.items():
        if not isinstance(input_id, str) or not input_id.strip():
            raise ConfigError(
                f'"{collection}" IDs must be non-empty strings.'
            )

        input_id = input_id.strip()

        if not isinstance(settings, dict):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} must be an object.'
            )

        unknown_keys = set(settings) - {
            "Name",
            "NameOverride",
            "Hidden",
            "Favorite",
            "Removed",
        }

        if unknown_keys:
            names = ", ".join(sorted(unknown_keys))
            raise ConfigError(
                f'"{collection}" entry {input_id!r} has unknown '
                f'field(s): {names}.'
            )

        name = settings.get("Name", input_id)
        name_override = settings.get("NameOverride", "")
        hidden = settings.get("Hidden", False)
        favorite = settings.get("Favorite", False)
        removed = settings.get("Removed", False)

        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f'"{collection}" entry {input_id!r} must have a '
                'non-empty string "Name".'
            )

        if not isinstance(name_override, str):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} "NameOverride" '
                'must be a string.'
            )

        if not isinstance(hidden, bool):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} "Hidden" '
                'must be true or false.'
            )

        if not isinstance(favorite, bool):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} "Favorite" '
                'must be true or false.'
            )

        if not isinstance(removed, bool):
            raise ConfigError(f'"{collection}" entry {input_id!r} "Removed" must be true or false.')

        normalized[input_id] = {
            "Name": name.strip(),
            "NameOverride": name_override.strip(),
            "Hidden": hidden,
            "Favorite": favorite,
            "Removed": removed,
        }

    return normalized


def _entry_display_name(settings):
    return settings["NameOverride"] or settings["Name"]


def _ordered_entries(entries, sort_by_name):
    items = list(entries.items())
    if sort_by_name:
        items.sort(key=lambda item: _entry_display_name(item[1]).casefold())
    return items


MOUSE_BINDING_LABELS = {
    "MouseMiddle": "Middle mouse button", "MouseBack": "Mouse back button",
    "MouseForward": "Mouse forward button", "WheelUp": "Mouse wheel up",
    "WheelDown": "Mouse wheel down", "WheelLeft": "Mouse wheel left",
    "WheelRight": "Mouse wheel right",
}


def _mouse_binding(text):
    return next((name for name in MOUSE_BINDING_LABELS if name.casefold() == text.strip().casefold()), None)


def _binding_label(text):
    mouse = _mouse_binding(text)
    return MOUSE_BINDING_LABELS[mouse] if mouse else QKeySequence.fromString(
        text, QKeySequence.PortableText).toString(QKeySequence.NativeText)


class MouseShortcuts:
    """Local Qt mouse gestures, sharing the remote's existing action dispatch."""
    def __init__(self, window, bindings, dispatch):
        self.window, self.bindings, self.dispatch = window, bindings, dispatch
        self.pressed = set()
        self.remainders = {"x": 0, "y": 0}

    def reset(self):
        self.pressed.clear()
        self.remainders = {"x": 0, "y": 0}

    def handle(self, watched, event):
        kind = event.type()
        if kind in (QEvent.WindowDeactivate, QEvent.Hide) and watched is self.window:
            self.reset()
        if kind not in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick,
                        QEvent.MouseButtonRelease, QEvent.Wheel):
            return False
        if kind == QEvent.MouseButtonRelease:
            if event.button() in self.pressed:
                self.pressed.remove(event.button())
                event.accept()
                return True
            return False
        window = self.window
        eligible = (not window._shutting_down and window.isActiveWindow()
                    and window.isVisible() and not window.isMinimized() and window.isEnabled()
                    and QApplication.activePopupWidget() is None
                    and QApplication.activeModalWidget() is None
                    and isinstance(watched, QWidget) and watched.window() is window
                    and watched.isEnabled())
        widget = watched if isinstance(watched, QWidget) else None
        while eligible and widget is not None:
            if isinstance(widget, (QComboBox, QTextEdit, QPlainTextEdit, QLineEdit, QAbstractSpinBox)):
                eligible = False
            widget = widget.parentWidget()
        if not eligible or event.modifiers() != Qt.NoModifier:
            self.remainders = {"x": 0, "y": 0}
            return False
        if kind != QEvent.Wheel:
            name = {Qt.MiddleButton: "MouseMiddle", Qt.BackButton: "MouseBack",
                    Qt.ForwardButton: "MouseForward"}.get(event.button())
            command = self.bindings.get(name)
            if command is None:
                return False
            if event.button() in self.pressed or self.dispatch(command):
                self.pressed.add(event.button())
                event.accept()
                return True
            return False
        if event.phase() in (Qt.ScrollBegin, Qt.ScrollEnd):
            self.remainders = {"x": 0, "y": 0}
        delta = event.angleDelta()
        axis = "x" if abs(delta.x()) > abs(delta.y()) else "y"
        amount = delta.x() if axis == "x" else delta.y()
        if not amount:
            return False  # Pixel-only touchpad gestures keep their normal behavior.
        name = ("WheelRight" if amount > 0 else "WheelLeft") if axis == "x" else (
            "WheelUp" if amount > 0 else "WheelDown")
        command = self.bindings.get(name)
        if command is None:
            self.remainders[axis] = 0
            return False
        previous = self.remainders[axis]
        if previous * amount < 0:
            previous = 0
        total = previous + amount
        steps = abs(total) // 120
        handled = False
        for _ in range(steps):
            if not self.dispatch(command):
                self.remainders[axis] = 0
                if handled:
                    event.accept()
                return handled
            handled = True
        self.remainders[axis] = (1 if total > 0 else -1) * (abs(total) % 120)
        # Consume bound partial deltas too, preventing parent propagation/counting twice.
        event.accept()
        return True


def _validate_shortcuts(raw):
    if not isinstance(raw, dict):
        raise ConfigError('"Shortcuts" must be an object.')
    if any(not isinstance(action, str) or not action or action != action.strip() for action in raw):
        raise ConfigError("Shortcut action names must be non-empty strings without surrounding whitespace.")
    normalized, owners = {}, {}
    for action in dict.fromkeys((*DEFAULT_SHORTCUTS, *raw)):
        bindings = raw.get(action, DEFAULT_SHORTCUTS.get(action, []))
        if bindings == "":
            bindings = []
        if not isinstance(bindings, list):
            raise ConfigError(f'Shortcuts.{action} must be a list of key strings, or empty to disable.')
        normalized[action] = []
        for text in bindings:
            if not isinstance(text, str) or not text.strip():
                raise ConfigError(f'Shortcuts.{action} contains an empty or invalid key.')
            mouse = _mouse_binding(text)
            if mouse:
                code, canonical = mouse, mouse
            else:
                sequence = QKeySequence.fromString(text.strip(), QKeySequence.PortableText)
                if (sequence.count() != 1 or sequence[0].key() in (Qt.Key_unknown, 0)
                        or not sequence.toString(QKeySequence.PortableText)):
                    raise ConfigError(f'Shortcuts.{action}: invalid single-key combination {text!r}.')
                code = sequence[0].toCombined()
                canonical = sequence.toString(QKeySequence.PortableText)
            if code in owners:
                raise ConfigError(f'Shortcut {canonical!r} is assigned more than once '
                                  f'({owners[code]} and {action}).')
            owners[code] = action
            normalized[action].append(canonical)
    return normalized


def _compile_shortcuts(config):
    return {QKeySequence.fromString(text, QKeySequence.PortableText)[0].toCombined():
            SHORTCUT_ACTIONS.get(action, "ircc:" + action)
            for action, bindings in config["Shortcuts"].items() for text in bindings if not _mouse_binding(text)}


def _compile_mouse_shortcuts(config):
    return {_mouse_binding(text): SHORTCUT_ACTIONS.get(action, "ircc:" + action)
            for action, bindings in config["Shortcuts"].items() for text in bindings if _mouse_binding(text)}


def _ownership_code(code):
    key = code & 0x01FFFFFF
    if key == Qt.Key_Enter.value:
        code &= ~Qt.KeypadModifier.value
    if 0x21 <= key <= 0x7E and not chr(key).isalnum():
        code &= ~Qt.ShiftModifier.value
    return code


def _binding_owned(bindings, code):
    return any(_ownership_code(binding) == _ownership_code(code) for binding in bindings)


def _shortcut_code(event):
    return event.key() | event.modifiers().value


def _focused_shortcut_command(bindings, event):
    code = _shortcut_code(event)
    command = bindings.get(code)
    # Printable punctuation already encodes Shift on many layouts (e.g. '+').
    # Prefer an explicitly modified binding, then accept the literal symbol.
    if (command is None and event.modifiers() & Qt.ShiftModifier
            and 0x21 <= event.key() <= 0x7E and not chr(event.key()).isalnum()):
        command = bindings.get(code & ~Qt.ShiftModifier.value)
    return command


def _shortcut_tip(config, command):
    action = next((name for name, value in SHORTCUT_ACTIONS.items() if value == command), None)
    labels = [_binding_label(text)
              for text in config["Shortcuts"].get(action, [])]
    return "Shortcut: " + " / ".join(labels) if labels else ""


def _default_config_copy():
    return copy.deepcopy(DEFAULT_CONFIG)


def missing_config_settings(config):
    return [key for key in ("Host", "PreSharedKey") if not config[key]]


def show_missing_config_settings(config):
    missing = missing_config_settings(config)
    if missing:
        QMessageBox.information(
            None, "Sony Remote - Configuration Required",
            f"Add the following required settings in {CONFIG_PATH.resolve()}: "
            + ", ".join(missing)
            + ".\nDevice communication is disabled until you save a valid configuration.",
        )


def _add_missing_settings(config, defaults=None):
    """Fill absent settings in place; preserve explicit values and dynamic entries."""
    defaults = DEFAULT_CONFIG if defaults is None else defaults
    changed = False
    for key, default in defaults.items():
        if key not in config:
            config[key] = copy.deepcopy(default)
            changed = True
        elif (key not in ("Apps", "Inputs") and isinstance(default, dict)
              and isinstance(config[key], dict)):
            changed = _add_missing_settings(config[key], default) or changed
    return changed


def validate_config(raw_config, *, allow_incomplete=False):
    if not isinstance(raw_config, dict):
        raise ConfigError("The configuration root must be a JSON object.")
    unknown = set(raw_config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ConfigError("Unknown configuration setting(s): " + ", ".join(sorted(unknown)) + ".")

    config = copy.deepcopy(raw_config)
    _add_missing_settings(config)
    config["Shortcuts"] = _validate_shortcuts(config["Shortcuts"])
    config["Host"] = _validate_host(config["Host"])
    if not isinstance(config["PreSharedKey"], str):
        raise ConfigError('"PreSharedKey" must be a string.')
    for key in BOOLEAN_CONFIG_KEYS:
        config[key] = _require_bool(config, key, DEFAULT_CONFIG[key])
    if config["CloseToTray"] and not config["SystemTray"]:
        raise ConfigError('"CloseToTray" can only be true when "SystemTray" is also true.')
    for key in ("Apps", "Inputs"):
        config[key] = _validate_entries(config[key], key)

    if not isinstance(config["AmplifierTVInput"], str):
        raise ConfigError('"AmplifierTVInput" must be a string (the amplifier input ID, or empty to disable routing).')
    config["AmplifierTVInput"] = config["AmplifierTVInput"].strip()

    default = config["DefaultTvVolume"]
    if not isinstance(default, dict) or set(default) - set(DEFAULT_CONFIG["DefaultTvVolume"]):
        raise ConfigError('"DefaultTvVolume" must be an object with Volume, DelayMs, RepeatCount and RepeatIntervalMs.')
    if default["Volume"] is not None and (type(default["Volume"]) is not int or default["Volume"] < 0):
        raise ConfigError('"DefaultTvVolume.Volume" must be a non-negative integer or null to disable.')
    for key in ("DelayMs", "RepeatCount", "RepeatIntervalMs"):
        if type(default[key]) is not int or default[key] < 0:
            raise ConfigError(f'"DefaultTvVolume.{key}" must be a non-negative integer.')
    if default["RepeatIntervalMs"] < 1:
        raise ConfigError('"DefaultTvVolume.RepeatIntervalMs" must be a positive integer.')
    config["DefaultTvVolume"] = default

    rate_limit = config["VolumeRateLimit"]
    if not isinstance(rate_limit, dict):
        raise ConfigError('"VolumeRateLimit" must be an object.')
    if set(rate_limit) - set(DEFAULT_CONFIG["VolumeRateLimit"]):
        raise ConfigError('"VolumeRateLimit" has unknown fields.')
    if not isinstance(rate_limit["Active"], bool):
        raise ConfigError('"VolumeRateLimit.Active" must be true or false.')
    for key in ("RateMs", "VerificationDelayMs"):
        if type(rate_limit[key]) is not int or rate_limit[key] < 1:
            raise ConfigError(f'"VolumeRateLimit.{key}" must be a positive integer.')

    sequence = config["TextInputSubmitSequence"]
    if (not isinstance(sequence, list) or not sequence
            or not all(isinstance(command, str) and command.strip() for command in sequence)):
        raise ConfigError('"TextInputSubmitSequence" must be a non-empty list of non-empty strings.')
    config["TextInputSubmitSequence"] = [command.strip() for command in sequence]
    if not allow_incomplete:
        missing = missing_config_settings(config)
        if missing:
            raise ConfigError("Required settings must not be empty: " + ", ".join(missing) + ".")
    return config


def load_config():
    if not CONFIG_PATH.exists():
        config = _default_config_copy()
        save_config(config)
        return validate_config(config, allow_incomplete=True)
    try:
        content = CONFIG_PATH.read_bytes()
        raw_config = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as ex:
        raise ConfigError(
            f"Invalid JSON syntax in {CONFIG_PATH.name}: line {ex.lineno}, "
            f"column {ex.colno}: {ex.msg}"
        ) from ex
    except UnicodeError as ex:
        raise ConfigError(f"{CONFIG_PATH.name} must use UTF-8 encoding.") from ex
    except OSError as ex:
        raise ConfigError(f"Unable to read {CONFIG_PATH.name}: {ex}") from ex
    # Convert the former disabled sentinel when upgrading an existing file.
    tv_default = raw_config.get("DefaultTvVolume") if isinstance(raw_config, dict) else None
    migrated_disabled = isinstance(tv_default, dict) and tv_default.get("Volume") == ""
    if migrated_disabled:
        tv_default["Volume"] = None
    migrated_repeat = isinstance(tv_default, dict) and "RepeatMs" in tv_default
    if migrated_repeat:
        duration = tv_default.pop("RepeatMs")
        if type(duration) is not int or duration < 0:
            raise ConfigError('"DefaultTvVolume.RepeatMs" must be a non-negative integer before migration.')
        # Old sends used 200 ms slots, excluding the duration's endpoint.
        tv_default.setdefault("RepeatCount", max(0, (duration - 1) // 200))
    config = validate_config(raw_config, allow_incomplete=True)
    updated = copy.deepcopy(raw_config)
    changed = _add_missing_settings(updated) or migrated_disabled or migrated_repeat
    # Discovered entry fields remain the responsibility of their own validators.
    for collection in ("Apps", "Inputs"):
        if any("Removed" not in item for item in raw_config.get(collection, {}).values()):
            updated[collection] = config[collection]
            changed = True
    if changed:
        save_config(updated, expected_digest=hashlib.sha256(content).digest())
    return config


def save_config(config, *, expected_digest=None):
    content = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    temp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    try:
        temp_path.write_bytes(content)
        # Discovery must not replace unapplied edits, including invalid JSON.
        # Check after preparing the replacement to minimize the edit/save race.
        if expected_digest is not None and _file_sha256(CONFIG_PATH) != expected_digest:
            return None
        os.replace(temp_path, CONFIG_PATH)
        return hashlib.sha256(content).digest()
    except OSError as ex:
        raise ConfigError(f"Unable to update {CONFIG_PATH.name}: {ex}") from ex
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _file_snapshot(path):
    try:
        content = path.read_bytes()
    except OSError:
        return None, None
    return content, hashlib.sha256(content).digest()


def _file_sha256(path):
    return _file_snapshot(path)[1]


# ----------------------------
# Platform integration
# ----------------------------


class TooltipDelayStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return 1000
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:
            # Moving between controls must still require a full hover delay.
            return 0
        return super().styleHint(hint, option, widget, returnData)





_instance_mutex_handle = None
_instance_mutex_api = None


_instance_lock_file = None


_NATIVE_VOLUME_KEYS = {0xAF: "volume_up", 0xAE: "volume_down", 0xAD: "mute"}


_QT_PLAYBACK_KEYS = frozenset((Qt.Key_MediaTogglePlayPause, Qt.Key_MediaPlay,
                               Qt.Key_MediaPause, Qt.Key_MediaStop,
                               Qt.Key_MediaNext, Qt.Key_MediaPrevious))
_RETAINED_VOLUME_HOOKS = []


_NATIVE_QT_VOLUME_KEYS = {0xAF: Qt.Key_VolumeUp.value, 0xAE: Qt.Key_VolumeDown.value,
                          0xAD: Qt.Key_VolumeMute.value}


def _native_shortcut_modifiers(user32):
    return sum(mask for keys, mask in (((0x10,), 0x02000000), ((0x11,), 0x04000000),
                                      ((0x12,), 0x08000000), ((0x5B, 0x5C), 0x10000000))
               if any(user32.GetAsyncKeyState(key) & 0x8000 for key in keys))


class _VolumeKeyState:
    """Keep a physical press with its original owner until release."""

    def __init__(self):
        self.pressed = {}
        self.commands = {}

    def clear(self):
        self.pressed.clear()
        self.commands.clear()

    def press(self, key, command, accept):
        if key in self.pressed:
            owned = self.pressed[key]
            original = self.commands.get(key, command)
            if owned and original not in ("mute", "PlayPause"):
                accept(original)
            return owned
        owned = bool(accept(command))
        self.pressed[key] = owned
        if owned:
            self.commands[key] = command
        return owned

    def release(self, key):
        self.commands.pop(key, None)
        return self.pressed.pop(key, False)


class _LowLevelKey(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32), ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _NativePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _NativeMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32), ("pt", _NativePoint),
        ("lPrivate", ctypes.c_uint32),
    ]


_HookProc = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
)


def _load_volume_hook_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = (
        (user32.SetWindowsHookExW, [ctypes.c_int, _HookProc, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p),
        (user32.UnhookWindowsHookEx, [ctypes.c_void_p], ctypes.c_int),
        (user32.CallNextHookEx, [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t),
        (user32.GetForegroundWindow, [], ctypes.c_void_p),
        (user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_int16),
        (user32.PeekMessageW, [ctypes.POINTER(_NativeMessage), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        (user32.GetMessageW, [ctypes.POINTER(_NativeMessage), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        (user32.PostThreadMessageW, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int),
        (kernel32.GetModuleHandleW, [ctypes.c_wchar_p], ctypes.c_void_p),
        (kernel32.GetCurrentThreadId, [], ctypes.c_uint32),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return user32, kernel32


class _WindowsVolumeHook(QObject):
    changed = Signal(bool, object)

    def __init__(self, accept, parent=None, *, resolve=None):
        super().__init__(parent)
        self._accept = accept
        self._resolve = resolve
        self._keys = _VolumeKeyState()
        self._hook = None
        self._callback = None
        self._user32 = None
        self._thread_id = None
        self._thread = None
        self._stopping = threading.Event()
        self._faulted = False

    @property
    def installed(self):
        return bool(self._hook and not self._faulted and not self._stopping.is_set())

    def start(self):
        # A separate native message loop keeps the hook responsive even while
        # the GUI waits for workers during restart/shutdown. No Qt UI calls or
        # network/file operations run inside the native callback.
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="RemoteVolumeKeys", daemon=True)
        self._thread.start()

    def _notify(self, installed, error=None):
        try:
            self.changed.emit(installed, error)
        except RuntimeError:
            pass  # The Qt parent may already be closing.

    def _run(self):
        error = None
        try:
            self._user32, kernel32 = _load_volume_hook_api()
            message = _NativeMessage()
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._thread_id = kernel32.GetCurrentThreadId()
            # stop() can arrive before the native thread has finished startup.
            if self._stopping.is_set():
                return
            # Do not seize keys that were already held when the app started.
            self._keys.clear()
            self._keys.pressed = {
                key: False for key in _NATIVE_VOLUME_KEYS
                if self._user32.GetAsyncKeyState(key) & 0x8000
            }
            self._callback = _HookProc(self._handle_key)
            self._hook = self._user32.SetWindowsHookExW(
                13, self._callback, kernel32.GetModuleHandleW(None), 0
            )
            if not self._hook:
                raise ctypes.WinError()
            if not self._stopping.is_set():
                self._notify(True)
            while not self._stopping.is_set():
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError()
        except Exception as exc:
            error = exc
            self._faulted = True
        finally:
            if self._hook:
                try:
                    if not self._user32.UnhookWindowsHookEx(self._hook):
                        raise ctypes.WinError()
                    self._hook = None
                except Exception as exc:
                    error = error or exc
                    self._faulted = True
            # Keep the C callback alive if native unregistration failed.
            if self._hook is None:
                self._callback = None
            elif self not in _RETAINED_VOLUME_HOOKS:
                _RETAINED_VOLUME_HOOKS.append(self)
            self._thread_id = None
            self._notify(False, error)

    def _handle_key(self, code, message, address):
        key = None
        if code == 0 and message in (0x100, 0x101, 0x104, 0x105):
            try:
                data = ctypes.cast(address, ctypes.POINTER(_LowLevelKey)).contents
                key = data.vkCode
                command = _NATIVE_VOLUME_KEYS.get(key)
                if command is not None and self._resolve is not None:
                    command = self._keys.commands.get(key) or self._resolve(
                        key, _native_shortcut_modifiers(self._user32))
                # Remappers can generate injected volume-key events. They are
                # safe to handle too: this remote never synthesizes key events.
                if command or key in self._keys.pressed:
                    if message in (0x101, 0x105):
                        owned = self._keys.release(data.vkCode)
                    else:
                        foreground = self._user32.GetForegroundWindow()
                        owned = self._keys.press(
                            data.vkCode, command,
                            lambda value: (
                                not self._faulted and not self._stopping.is_set()
                                and self._accept(value, foreground)
                            ),
                        )
                    if owned:
                        return 1
            except Exception as error:
                if not self._faulted:
                    self._faulted = True
                    self._notify(False, error)
                if self._keys.pressed.get(key, False):
                    return 1
        try:
            return self._user32.CallNextHookEx(self._hook, code, message, address)
        except Exception as error:
            if not self._faulted:
                self._faulted = True
                self._notify(False, error)
            return 0  # Never let a Python exception escape a native callback.

    def stop(self):
        self._stopping.set()
        if self._thread is not None and self._thread.is_alive() and self._thread_id and self._user32:
            try:
                if not self._user32.PostThreadMessageW(self._thread_id, 0x12, 0, 0):  # WM_QUIT
                    raise ctypes.WinError()
            except Exception as error:
                record_exception(error, "Hotkey shutdown")
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
        if self._thread is not None and self._thread.is_alive():
            # Never free a live C callback. Disable interception even if the
            # native message thread cannot be stopped normally.
            try:
                if self._hook and self._user32.UnhookWindowsHookEx(self._hook):
                    self._hook = None
            except Exception as error:
                record_exception(error, "Hotkey shutdown")
            if self._hook and self not in _RETAINED_VOLUME_HOOKS:
                _RETAINED_VOLUME_HOOKS.append(self)
            record_exception(RuntimeError("Volume hook thread did not stop."), "Hotkey shutdown")




def acquire_single_instance():
    global _instance_mutex_handle, _instance_mutex_api, _instance_lock_file

    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
        last_error = ctypes.get_last_error()
        if not handle:
            raise OSError(last_error, "CreateMutexW failed.")
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _instance_mutex_handle = handle
        _instance_mutex_api = kernel32
        return True

    # Include the user ID so separate logged-in users do not block each other.
    user_suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
    lock_path = Path(tempfile.gettempdir()) / f"SonyRemote-{user_suffix}.lock"
    lock_file = QLockFile(str(lock_path))
    lock_file.setStaleLockTime(0)
    if not lock_file.tryLock(0):
        if lock_file.error() != QLockFile.LockFailedError:
            raise OSError("Could not create the single-instance lock file.")
        return False
    _instance_lock_file = lock_file
    return True


def release_single_instance():
    global _instance_mutex_handle, _instance_mutex_api, _instance_lock_file

    if sys.platform == "win32":
        if _instance_mutex_handle:
            try:
                _instance_mutex_api.CloseHandle(_instance_mutex_handle)
            except Exception:
                pass
            _instance_mutex_handle = None
            _instance_mutex_api = None
        return

    if _instance_lock_file is not None:
        try:
            _instance_lock_file.unlock()
        except Exception:
            pass
        _instance_lock_file = None


def set_app_user_model_id(app_id):
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# Windows Mica integration inspired by win32mica (MIT); see THIRD_PARTY_LICENSES.txt.


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



def get_app_icon():
    return QApplication.instance().style().standardIcon(QStyle.SP_ComputerIcon)


# ----------------------------
# Request errors and optional diagnostics
# ----------------------------


class DeviceConnectionError(RuntimeError):
    """The device could not be reached."""


class DeviceResponseError(RuntimeError):
    """The device rejected a request or returned an invalid response."""


_diagnostics = None


class Diagnostics:
    """Optional, best-effort logs without messages, source lines, or locals."""

    def __init__(self):
        self._stream = None
        self._lock = threading.RLock()
        self._seen = set()
        self._owns_fault_handler = False
        self._old_sys_hook = sys.excepthook
        self._old_thread_hook = threading.excepthook
        self._sys_hook = self._exception_hook
        self._thread_hook = self._thread_exception_hook
        try:
            self._stream = Path(__file__).with_name(
                Path(__file__).stem + "-crash.log"
            ).open("a", encoding="utf-8")
        except OSError:
            return
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        try:
            if not faulthandler.is_enabled():
                faulthandler.enable(self._stream)
                self._owns_fault_handler = True
        except (OSError, RuntimeError, ValueError):
            pass

    def _format_exception(self, error, context):
        # Never stringify exceptions: messages and chained causes may contain
        # credentials, request payloads, or text entered by the user.
        lines = [f"{context}: {type(error).__name__}"]
        visited = set()
        while error is not None and id(error) not in visited:
            visited.add(id(error))
            lines.append(f"Exception type: {type(error).__name__}")
            for frame, line in traceback.walk_tb(error.__traceback__):
                lines.append(
                    f"  {Path(frame.f_code.co_filename).name}:{line} in {frame.f_code.co_name}"
                )
            error = error.__cause__ or (
                None if error.__suppress_context__ else error.__context__
            )
        return "\n".join(lines) + "\n"

    def record(self, error, context, *, deduplicate=False):
        with self._lock:
            if self._stream is None:
                return
            text = self._format_exception(error, context)
            if deduplicate and text in self._seen:
                return
            if deduplicate:
                self._seen.add(text)
            try:
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                self._stream.write(f"\n[{timestamp}] {Path(__file__).stem} {__version__}\n{text}")
                self._stream.flush()
            except (OSError, ValueError):
                self.close()

    def _exception_hook(self, exc_type, error, tb):
        self.record(error, "Unhandled exception")
        try:
            sys.stderr.write(self._format_exception(error, "Unhandled exception"))
        except (OSError, ValueError, AttributeError):
            pass

    def _thread_exception_hook(self, args):
        self._exception_hook(args.exc_type, args.exc_value, args.exc_traceback)

    def close(self):
        with self._lock:
            if sys.excepthook is self._sys_hook:
                sys.excepthook = self._old_sys_hook
            if threading.excepthook is self._thread_hook:
                threading.excepthook = self._old_thread_hook
            if self._owns_fault_handler:
                try:
                    faulthandler.disable()
                except (OSError, RuntimeError, ValueError):
                    pass
                self._owns_fault_handler = False
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def configure_diagnostics(config):
    global _diagnostics
    close_diagnostics()
    if config["DiagnosticsEnabled"]:
        _diagnostics = Diagnostics()


def close_diagnostics():
    global _diagnostics
    if _diagnostics is not None:
        _diagnostics.close()
        _diagnostics = None


def record_exception(error, context, *, deduplicate=False):
    if _diagnostics is not None:
        _diagnostics.record(error, context, deduplicate=deduplicate)


def record_unexpected_exception(error, context, *, background=False):
    if not isinstance(error, (DeviceConnectionError, DeviceResponseError)):
        record_exception(error, context, deduplicate=background)


# ----------------------------
# Device API
# ----------------------------


class SonyConnectionError(DeviceConnectionError):
    """Raised when the Sony TV cannot be reached."""


class SonyApi:
    def __init__(self, host, psk, timeout=2.0):
        self.psk = psk
        self.polling_responses = PollingResponses(('system/getPowerStatus', 'audio/getVolumeInformation', 'appControl/getApplicationList', 'avContent/getCurrentExternalInputsStatus', 'system/getRemoteControllerInfo', 'appControl/getApplicationStatusList'))
        self.timeout = timeout
        # IPv6 literals need brackets when used in a URL.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            url_host = host
        else:
            url_host = f"[{host.replace('%', '%25')}]" if address.version == 6 else host
        self.base_url = f"http://{url_host}"
        self._request_id = 0
        self._lock = threading.Lock()
        self.remote_codes = {}

    def _next_id(self):
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _read_response(self, request):
        try:
            with DEVICE_HTTP_OPENER.open(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as ex:
            raise DeviceResponseError(f"Sony returned HTTP {ex.code}.") from ex
        except (URLError, TimeoutError, OSError) as ex:
            raise SonyConnectionError("Unable to reach the Sony TV.") from ex

    def rpc(self, service, method, params=None, version="1.0"):
        request_type = f"{service}/{method}"
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
        try:
            result = json.loads(self._read_response(request).decode("utf-8"))
        except DeviceConnectionError as ex:
            self.polling_responses.failed(request_type, _connection_failure_category(ex))
            raise
        except DeviceResponseError:
            self.polling_responses.failed(request_type, "http_errors")
            raise
        except (UnicodeError, json.JSONDecodeError) as ex:
            self.polling_responses.failed(request_type, "malformed_responses")
            raise DeviceResponseError("Sony returned invalid JSON.") from ex
        if not isinstance(result, dict):
            self.polling_responses.failed(request_type, "malformed_responses")
            raise DeviceResponseError("Sony response must be a JSON object.")
        if "error" in result:
            error = result["error"]
            code = error[0] if isinstance(error, list) and error and isinstance(error[0], int) else "unknown"
            self.polling_responses.failed(request_type, "device_rejections")
            raise DeviceResponseError(f"Sony rejected {method} (error code {code}).")
        if not isinstance(result.get("result"), list):
            self.polling_responses.failed(request_type, "malformed_responses")
            raise DeviceResponseError("Sony response is missing a result list.")
        try:
            self._validate_poll_response(request_type, result["result"])
        except DeviceResponseError:
            self.polling_responses.failed(request_type, "malformed_responses")
            raise
        self.polling_responses.record(request_type, result)
        return result

    @staticmethod
    def _validate_poll_response(request_type, result):
        if request_type == "system/getPowerStatus":
            if (not result or not isinstance(result[0], dict)
                    or result[0].get("status") not in ("active", "standby")):
                raise DeviceResponseError("Sony returned an invalid power status.")
        elif request_type in ("appControl/getApplicationList", "avContent/getCurrentExternalInputsStatus"):
            if not result:
                raise DeviceResponseError("Sony response is missing its inventory list.")
            _inventory_ids(result[0], "uri", "title")
        elif request_type == "audio/getVolumeInformation":
            if not result or not isinstance(result[0], list):
                raise DeviceResponseError("Sony returned an invalid volume list.")
            for entry in result[0]:
                if (not isinstance(entry, dict) or not isinstance(entry.get("target"), str)
                        or type(entry.get("volume")) is not int or type(entry.get("mute")) is not bool
                        or any(key in entry and type(entry[key]) is not int for key in ("minVolume", "maxVolume"))
                        or not entry.get("minVolume", -math.inf) <= entry["volume"] <= entry.get("maxVolume", math.inf)):
                    raise DeviceResponseError("Sony returned invalid volume or mute information.")
        elif request_type == "system/getRemoteControllerInfo":
            if (len(result) < 2 or not isinstance(result[1], list)
                    or any(not isinstance(item, dict)
                           or any(not isinstance(item.get(key), str) or not item[key].strip()
                                  for key in ("name", "value")) for item in result[1])):
                raise DeviceResponseError("Sony returned invalid remote-control commands.")
        elif request_type == "appControl/getApplicationStatusList":
            if (not result or not isinstance(result[0], list)
                    or any(not isinstance(item, dict)
                           or any(not isinstance(item.get(key), str) for key in ("name", "status"))
                           for item in result[0])):
                raise DeviceResponseError("Sony returned invalid application status information.")

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
        return self.rpc("audio", "setAudioVolume",
                        [{"target": "speaker", "volume": str(value), "ui": "on"}], version="1.2")

    def adjust_volume(self, amount):
        amount = int(amount)
        if amount == 0:
            return None
        value = f"{amount:+d}"
        return self.rpc(
            "audio", "setAudioVolume",
            [{"target": "speaker", "volume": value}],
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

    def resolve_remote_command(self, name):
        if name == "Settings":
            for candidate in ("QuickSettings", "Settings", "ActionMenu"):
                if candidate in self.remote_codes:
                    return candidate
        if name == "Confirm" and name not in self.remote_codes:
            if "DpadCenter" in self.remote_codes:
                return "DpadCenter"
        return name

    def validate_remote_commands(self, commands):
        unavailable = [
            name
            for name in commands
            if self.resolve_remote_command(name) not in self.remote_codes
        ]
        if unavailable:
            joined = ", ".join(repr(name) for name in unavailable)
            raise DeviceResponseError(
                f"TextInputSubmitSequence contains unavailable remote command(s): {joined}"
            )

    def ircc(self, name):
        resolved_name = self.resolve_remote_command(name)
        code = self.remote_codes.get(resolved_name)
        if not code:
            raise DeviceResponseError(f"Remote command '{name}' is unavailable.")
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
        self._read_response(request)


# ----------------------------
# Background workers
# ----------------------------


# ----------------------------
# Cached diagnostics (no device requests)
# ----------------------------


def _state_age(observed_at, now):
    return round(max(0.0, now - observed_at), 3) if observed_at else None


def _connection_failure_category(error):
    while error is not None:
        if isinstance(error, TimeoutError):
            return "timeouts"
        if isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
            return "timeouts"
        error = error.__cause__
    return "connection_failures"


class PollingResponses:
    """Latest successful decoded response per allowlisted read; worker-thread safe."""

    def __init__(self, request_types):
        self._responses = dict.fromkeys(request_types)
        self._counters = {key: dict.fromkeys(("successes", "empty_inventories", "failures",
            "malformed_responses", "connection_failures", "timeouts", "http_errors",
            "device_rejections", "recoveries", "consecutive_failures"), 0) for key in request_types}
        self._lock = threading.Lock()

    def record(self, request_type, response):
        if request_type not in self._responses:
            return  # Never retain command responses, parameters, or headers.
        entry = {"at_utc": datetime.now(timezone.utc).isoformat(),
                 "observed_at": time.monotonic(), "response": copy.deepcopy(response)}
        with self._lock:
            self._responses[request_type] = entry
            counters = self._counters[request_type]
            counters["successes"] += 1
            if counters["consecutive_failures"]:
                counters["recoveries"] += 1
            counters["consecutive_failures"] = 0
            if request_type in ("appControl/getApplicationList", "avContent/getCurrentExternalInputsStatus"):
                empty = response.get("result") == [[]]
            else:
                empty = False
            if empty:
                counters["empty_inventories"] += 1

    def failed(self, request_type, category):
        if request_type not in self._counters:
            return
        with self._lock:
            counters = self._counters[request_type]
            counters["failures"] += 1
            counters["consecutive_failures"] += 1
            counters[category] += 1
            if category == "timeouts":
                counters["connection_failures"] += 1

    def counters(self):
        with self._lock:
            return copy.deepcopy(self._counters)

    def snapshot(self, now):
        with self._lock:
            result = copy.deepcopy(self._responses)
        for entry in result.values():
            if entry is not None:
                entry["age_seconds"] = _state_age(entry.pop("observed_at"), now)
        return result


class RequestHistory:
    """Bounded GUI-thread outcome summaries; never retain exceptions or payloads."""

    def __init__(self):
        self.groups = {}

    def record(self, group, error=None):
        if group not in ("status", "command", "apps", "inputs", "speakers", "background"):
            group = "background"
        outcome = "failure" if error is not None else "success"
        record = self.groups.setdefault(group, {"successes": 0, "failures": 0,
                                                "recoveries": 0, "consecutive_failures": 0})
        if error is None:
            record["successes"] += 1
            record["recoveries"] += bool(record["consecutive_failures"])
            record["consecutive_failures"] = 0
        else:
            record["failures"] += 1
            record["consecutive_failures"] += 1
        record[outcome] = {"at_utc": datetime.now(timezone.utc).isoformat(),
                           "observed_at": time.monotonic()}
        if error is not None:
            record[outcome]["exception_type"] = type(error).__name__
        record["latest"] = outcome

    def snapshot(self, now):
        result = copy.deepcopy(self.groups)
        for record in result.values():
            for outcome in ("success", "failure"):
                if outcome in record:
                    observed = record[outcome].pop("observed_at")
                    record[outcome]["age_seconds"] = _state_age(observed, now)
        return result


def write_state_dump(snapshot, directories):
    """Write a complete, exclusive report; fall back without overwriting files."""
    payload = json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    filename = f"{Path(__file__).stem}-state-{stamp}-{os.urandom(4).hex()}.json"
    last_error = None
    for index, directory in enumerate(directories):
        path = Path(directory) / filename
        created = False
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                created = True
                stream.write(payload)
            return {"ok": True, "path": str(path.resolve()), "fallback": index > 0}
        except OSError as error:
            last_error = error
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
    raise last_error or OSError("No diagnostic output directory is available")


class StateDumper(QObject):
    """Capture on the GUI thread; one file write at a time on a separate pool."""

    def __init__(self, parent, snapshot):
        super().__init__(parent)
        self.snapshot = snapshot
        self.worker = None

    def request(self, done, *, full=False):
        if self.worker is not None:
            done({"ok": False, "error": "busy"})
            return
        try:
            snapshot = copy.deepcopy(self.snapshot(full=True) if full else self.snapshot())
            directories = [CONFIG_PATH.parent]
            fallback = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
            if fallback:
                directories.append(Path(fallback) / "diagnostics")
            worker = Worker(lambda: write_state_dump(snapshot, directories))
        except Exception as error:
            done({"ok": False, "error": type(error).__name__})
            return
        self.worker = worker
        worker.signals.result.connect(done)
        worker.signals.error.connect(lambda error: done({"ok": False, "error": type(error).__name__}))
        worker.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(worker)

    def _finished(self):
        self.worker = None


def _cached_common(window, role, now):
    polling = window.polling
    scheduled = {}
    for group, value in polling._scheduled.items():
        deadline = value[0] if isinstance(value, tuple) else value
        scheduled[group] = round(max(0.0, deadline - now), 3)
    return {
        "report_format_version": 1, "mode": "cached", "fresh_queries_performed": False,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "app": {"name": APP_NAME, "version": __version__, "role": role,
                "python_version": sys.version.split()[0], "qt_version": qVersion(),
                "pyside_version": PYSIDE_VERSION, "platform": sys.platform},
        "window": {"visible": window.isVisible(), "minimized": window.isMinimized(),
                   "active": window.isActiveWindow(), "tray_available": window.tray is not None},
        "lifecycle": {"shutting_down": window._shutting_down, "restart_failed": window._restart_failed,
                      "invalid_config_edit": window._invalid_config_digest is not None,
                      "missing_config_settings": missing_config_settings(window.config)},
        "calibration": {"active": window.calibration_active, "operation_running": window._calibration_operation},
        "polling": {"paused": polling.paused, "periodic_active": polling.periodic.isActive(),
                    "periodic_interval_ms": polling.periodic.interval(),
                    "poll_running": window.poll_running, "scheduled_in_seconds": scheduled,
                    "observed_age_seconds": {key: _state_age(value, now) for key, value in polling._observed.items()},
                    "active_device_workers": len(window._workers)},
        "requests": window.request_history.snapshot(now),
        "discovery": {"paused": window.discovery.paused,
                      "last_inventory_age_seconds": {key: _state_age(value, now) for key, value in window.discovery.checked.items()},
                      "pending_removal_counts": {key: len(value) for key, value in window.discovery.pending.items()}},
        "privacy": {"omitted": ["credentials", "device_addresses", "text_input", "exception_messages",
                                 "request_payloads", "app_and_input_inventories", "configuration_file"]},
    }


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function())
        except Exception as exc:
            self.signals.error.emit(exc)
        finally:
            self.signals.finished.emit()


# ----------------------------
# Optional Windows playback media-key routing
# ----------------------------


_PLAYBACK_KEY_COMMANDS = frozenset(("PlayPause", "Play", "Pause", "Stop", "Next", "Prev"))
_SMTC_BUTTON_COMMANDS = {"PLAY": "Play", "PAUSE": "Pause", "STOP": "Stop",
                         "NEXT": "Next", "PREVIOUS": "Prev"}


def _load_smtc_api():
    # Optional dependencies must never be imported for the ordinary key path.
    from winrt.windows.media import MediaPlaybackStatus
    from winrt.windows.media.interop import get_for_window
    return MediaPlaybackStatus, get_for_window


# Physical playback keys associated with each Windows session action. Global
# amplifier ownership wins; the TV session withdraws only overlapping actions.
_SMTC_COMMAND_KEYS = {
    "Play": (Qt.Key_MediaPlay.value, Qt.Key_MediaTogglePlayPause.value),
    "Pause": (Qt.Key_MediaPause.value, Qt.Key_MediaTogglePlayPause.value),
    "Stop": (Qt.Key_MediaStop.value,), "Next": (Qt.Key_MediaNext.value,),
    "Prev": (Qt.Key_MediaPrevious.value,),
}


class SMTCPlayback(QObject):
    """Window-local WinRT adapter; callbacks cross to Qt without doing work."""

    button_pressed = Signal(str)
    failed = Signal(object)
    _button_event = Signal(int, str)
    _error_event = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.controls = None
        self._token = None
        self._generation = 0
        self._settings = None
        self._status_api = None
        self._status = None
        self._button_event.connect(self._deliver_button, Qt.QueuedConnection)
        self._error_event.connect(self._deliver_error, Qt.QueuedConnection)

    def start(self, hwnd):
        self.stop()
        self._status_api, get_for_window = _load_smtc_api()
        self.controls = get_for_window(hwnd)
        try:
            self.controls.is_enabled = False
            self.controls.is_fast_forward_enabled = False
            self.controls.is_rewind_enabled = False
            self.controls.is_record_enabled = False
            self._status = self._status_api.PLAYING
            self.controls.playback_status = self._status
            generation = self._generation
            def received(_sender, args):
                try:
                    command = _SMTC_BUTTON_COMMANDS.get(args.button.name)
                    if command is not None:
                        self._button_event.emit(generation, command)
                except Exception as error:
                    try:
                        self._error_event.emit(generation, error)
                    except RuntimeError:
                        pass  # A retired callback can race QObject destruction.
            self._token = self.controls.add_button_pressed(received)
        except Exception:
            self.stop()
            raise

    def update(self, commands):
        settings = frozenset(commands)
        if self.controls is None or settings == self._settings:
            return
        try:
            for command, name in (("Play", "play"), ("Pause", "pause"), ("Stop", "stop"),
                                  ("Next", "next"), ("Prev", "previous")):
                setattr(self.controls, f"is_{name}_enabled", command in settings)
            self.controls.is_enabled = bool(settings)
            self._settings = settings
        except Exception as error:
            self.stop()
            self.failed.emit(error)

    def note_command(self, command):
        if self.controls is None or command not in ("Play", "Pause", "PlayPause", "Stop"):
            return
        if command == "PlayPause":
            command = "Pause" if self._status == self._status_api.PLAYING else "Play"
        status = {"Play": self._status_api.PLAYING, "Pause": self._status_api.PAUSED,
                  "Stop": self._status_api.STOPPED}[command]
        try:
            # Optimistic routing hint, not confirmation of the TV's playback.
            self.controls.playback_status = status
            self._status = status
        except Exception as error:
            self.stop()
            self.failed.emit(error)

    def _deliver_button(self, generation, command):
        if generation == self._generation and command in (self._settings or ()):
            self.button_pressed.emit(command)

    def _deliver_error(self, generation, error):
        if generation == self._generation and self.controls is not None:
            self.stop()
            self.failed.emit(error)

    def stop(self):
        self._generation += 1  # Ignore events already queued by a retired session.
        controls, token = self.controls, self._token
        self.controls = None
        self._token = self._settings = None
        if controls is not None:
            try:
                controls.is_enabled = False
            except Exception as error:
                record_exception(error, "SMTC disable", deduplicate=True)
            if token is not None:
                try:
                    controls.remove_button_pressed(token)
                except Exception as error:
                    record_exception(error, "SMTC unsubscribe", deduplicate=True)


# ----------------------------
# Specialized widgets
# ----------------------------


class TvTextEdit(QTextEdit):
    focusRemoteRequested = Signal(int)
    submitRequested = Signal()
    submitAndConfirmRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.submission_key = None
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setPlaceholderText("Type text for the TV")

        # Two visible lines, including the editor frame/padding.
        line_height = self.fontMetrics().lineSpacing()
        self.setFixedHeight(line_height * 2 + 24)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.accept()
            self.focusRemoteRequested.emit(event.key())
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            modifiers = event.modifiers()

            if modifiers & Qt.ShiftModifier:
                super().keyPressEvent(event)
                return

            self.submission_key = event.key()
            try:
                if modifiers & Qt.ControlModifier:
                    self.submitAndConfirmRequested.emit()
                else:
                    self.submitRequested.emit()
            finally:
                self.submission_key = None
            return

        super().keyPressEvent(event)


class WindowDrag(QObject):
    """Start native moves only for actual drags, preserving ordinary releases."""
    def __init__(self, widget, *, background=False):
        super().__init__(widget)
        self._origin = None
        self._background = background
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Hide, QEvent.WindowDeactivate):
            self._origin = None
        elif kind == QEvent.MouseButtonPress:
            if (event.button() == Qt.LeftButton and event.buttons() == Qt.LeftButton
                    and (not self._background or watched.childAt(event.position().toPoint()) is None)):
                self._origin = event.globalPosition().toPoint()
                # Preserve the window's existing click-to-leave-text-editing behavior.
        elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._origin = None
        elif kind == QEvent.MouseMove and self._origin is not None:
            if not event.buttons() & Qt.LeftButton:
                self._origin = None
            elif (event.globalPosition().toPoint() - self._origin).manhattanLength() >= QApplication.startDragDistance():
                self._origin = None
                handle = watched.window().windowHandle()
                if handle is not None and handle.startSystemMove():
                    event.accept()
                    return True
        return False


class FavoriteButton(QPushButton):
    """Keep favorite columns equal; elide only the painted label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumHeight(34)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SE_PushButtonContents, option, self)
        option.text = self.fontMetrics().elidedText(
            option.text, Qt.ElideRight, max(0, contents.width()), Qt.TextShowMnemonic)
        painter.drawControl(QStyle.CE_PushButton, option)


class CenteredComboBox(QComboBox):
    def __init__(self, parent=None, *, animate_popup=False, clear_on_close=False):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._animate_popup = animate_popup
        self._clear_on_close = clear_on_close
        self._popup = None
        self._popup_view = None
        self._final_rect = None
        self._view_rect = None
        self._popup_layout = None
        self._layout_was_enabled = False
        self._opens_downward = True
        self._elapsed = QElapsedTimer()
        self._easing = QEasingCurve(QEasingCurve.OutCubic)
        self._popup_timer = QTimer(self)
        self._popup_timer.setTimerType(Qt.PreciseTimer)
        self._popup_timer.setInterval(8)
        self._popup_timer.timeout.connect(self._animation_tick)

    def _widen_popup(self):
        popup = self.view().window()
        if popup.isVisible():
            if self._clear_on_close:
                popup.installEventFilter(self)
            popup.setGeometry(popup.geometry().adjusted(-1, 0, 1, 0))
            if popup.layout() is not None:
                popup.layout().activate()

    def showPopup(self):
        if not self._animate_popup:
            super().showPopup()
            self._widen_popup()
            return
        if self.view().window().isVisible():
            self.hidePopup()
            return

        app = QApplication.instance()
        effect = Qt.UI_AnimateCombo
        animate = app.isEffectEnabled(effect)
        # Suppress Qt's rolling window, then animate only the real popup.
        # Restore the preference immediately, including when opening fails.
        app.setEffectEnabled(effect, False)
        try:
            super().showPopup()
        finally:
            app.setEffectEnabled(effect, animate)
        self._widen_popup()
        if not animate:
            return  # Respect the user's/system's disabled UI animations.

        view = self.view()
        popup = view.window()
        final_rect = popup.geometry()
        if not popup.isVisible() or final_rect.height() <= 2:
            return

        # Keep Qt's vertical placement, including popups that overlap the combo,
        # and the one-pixel expansion on each side. Never relocate it above/below.
        self._opens_downward = final_rect.center().y() >= self.mapToGlobal(self.rect().center()).y()
        self._popup, self._popup_view = popup, view
        self._final_rect = QRect(final_rect)
        self._view_rect = QRect(view.geometry())
        self._popup_layout = popup.layout()
        if self._popup_layout is not None:
            self._layout_was_enabled = self._popup_layout.isEnabled()
            # Deferred layout passes must not shrink the live view between
            # animation frames and change its scrollbar geometry.
            self._popup_layout.setEnabled(False)
        popup.installEventFilter(self)
        self._elapsed.start()
        self._animation_tick()
        popup.raise_()
        self._popup_timer.start()

    def _animation_tick(self):
        if self._popup is None:
            return
        linear = min(1.0, self._elapsed.elapsed() / POPUP_ANIMATION_DURATION_MS)
        progress = self._easing.valueForProgress(linear)
        # Scroll controls can impose a taller minimum on long popups.
        minimum = min(self._final_rect.height(), max(2, self._popup.minimumHeight()))
        height = round(minimum + (self._final_rect.height() - minimum) * progress)
        rect = QRect(self._final_rect)
        rect.setHeight(height)
        if not self._opens_downward:
            rect.moveBottom(self._final_rect.bottom())
        self._popup.setGeometry(rect)
        view_rect = QRect(self._view_rect)
        if self._opens_downward:
            view_rect.translate(0, height - self._final_rect.height())
        self._popup_view.setGeometry(view_rect)
        if linear >= 1:
            self._finish_popup_animation(focus_view=True)

    def _finish_popup_animation(self, *, focus_view=False):
        self._popup_timer.stop()
        popup, view = self._popup, self._popup_view
        if popup is None:
            return
        # Clear the active marker before restoring widgets: restoration itself
        # can generate Qt show/hide/focus events.
        self._popup = None
        if not self._clear_on_close:
            popup.removeEventFilter(self)
        popup.setGeometry(self._final_rect)
        view.setGeometry(self._view_rect)
        view.show()
        view.raise_()
        view.update()
        view.viewport().update()
        if self._popup_layout is not None:
            self._popup_layout.setEnabled(self._layout_was_enabled)
        self._popup_layout = None
        if focus_view and popup.isVisible():
            view.setFocus(Qt.PopupFocusReason)
        self._popup_view = None
        self._final_rect = self._view_rect = None

    def _clear_closed_selection(self):
        if not self.view().window().isVisible():
            self.setCurrentIndex(-1)
            self.view().selectionModel().clear()

    def eventFilter(self, watched, event):
        if (self._clear_on_close and watched is self.view().window()
                and event.type() in (QEvent.Hide, QEvent.Close)):
            # Qt may emit activated after hiding; let that handler consume its index first.
            QTimer.singleShot(0, self._clear_closed_selection)
        if self._popup is not None:
            if watched is self._popup and event.type() in (QEvent.Hide, QEvent.Close):
                self._finish_popup_animation()
        return super().eventFilter(watched, event)

    def hidePopup(self):
        self._finish_popup_animation()
        super().hidePopup()

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)

        # Let the styled control paint its frame without a separate arrow button.
        option.subControls &= ~QStyle.SC_ComboBoxArrow
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        arrow = QStyleOptionComboBox(option)
        arrow_rect = self.style().subControlRect(
            QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, self)
        arrow.rect = QRect(0, 0, 12, 12)
        arrow.rect.moveCenter(arrow_rect.center())
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        arrow.palette.setColor(QPalette.ButtonText, option.palette.color(group, QPalette.Text))
        QApplication.style().drawPrimitive(QStyle.PE_IndicatorArrowDown, arrow, painter, self)

        rect = self.style().subControlRect(
            QStyle.CC_ComboBox,
            option,
            QStyle.SC_ComboBoxEditField,
            self,
        )

        # Reserve equal space on both sides so the native arrow does not
        # shift the label away from the center of the whole button.
        inset = max(rect.left(), self.width() - rect.right() - 1)
        rect.setLeft(inset)
        rect.setRight(self.width() - inset - 1)

        text = self.currentText()
        if self.currentIndex() < 0:
            text = self.placeholderText()

        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(option.palette.color(group, QPalette.Text))
        text = self.fontMetrics().elidedText(text, Qt.ElideRight, max(0, rect.width()))
        painter.drawText(rect, Qt.AlignCenter, text)


class CenteredComboDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignCenter


# ----------------------------
# Main window
# ----------------------------


def remaining_validity_ms(age_seconds, max_age_seconds):
    """Convert a bounded remaining status lifetime to the IPC wire unit."""
    return int(max(0.0, min(max_age_seconds, max_age_seconds - age_seconds)) * 1000)


# Discovery inventories retain preferences while absent entries are confirmed.
DISCOVERY_INTERVAL_SECONDS = 30.0
REMOVAL_CONFIRM_SECONDS = 5.0


def _inventory_ids(entries, id_key, name_key):
    if not isinstance(entries, list):
        raise DeviceResponseError("Discovery did not return a complete inventory list.")
    ids = []
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get(id_key), str)
                or not entry[id_key].strip() or not isinstance(entry.get(name_key), str)
                or not entry[name_key].strip()):
            raise DeviceResponseError("Discovery returned an invalid inventory entry.")
        ids.append(entry[id_key].strip())
    if len(set(ids)) != len(ids):
        raise DeviceResponseError("Discovery returned duplicate inventory IDs.")
    return set(ids)


class DiscoveryTracker(QObject):
    """Confirm absences with fresh inventories; reuse the ordinary status cadence."""

    def __init__(self, parent, collections, refresh, allowed):
        super().__init__(parent)
        self.collections, self.refresh, self.allowed = collections, refresh, allowed
        self.pending = {}
        self.checked = {}
        self.retry_after = {}
        self.paused = False
        self.verification = QTimer(self)
        self.verification.setSingleShot(True)
        self.verification.timeout.connect(self.tick)

    def observe(self, collection, present, settings, *, allow_missing, observed_at=None):
        now = time.monotonic() if observed_at is None else observed_at
        missing = self.pending.setdefault(collection, {})
        changed = False
        for key, item in settings.items():
            if "Removed" not in item:
                item["Removed"] = False
                changed = True
            if key in present:
                missing.pop(key, None)
                if item["Removed"]:
                    item["Removed"] = False
                    changed = True
            elif allow_missing and not item["Removed"]:
                first = missing.setdefault(key, now)
                if now - first >= REMOVAL_CONFIRM_SECONDS:
                    item["Removed"] = True
                    missing.pop(key, None)
                    changed = True
        if allow_missing or not any(key not in present and not item["Removed"]
                                    for key, item in settings.items()):
            self.checked[collection] = now
        else:
            missing.clear()  # Standby/unknown power cannot establish an absence.
        self._arm()
        return changed

    def tick(self):
        if self.paused:
            return
        now = time.monotonic()
        for collection in self.collections:
            if not self.allowed(collection):
                continue
            pending = self.pending.get(collection, {})
            due = min(pending.values()) + REMOVAL_CONFIRM_SECONDS if pending else float("inf")
            if (now >= self.retry_after.get(collection, 0)
                    and (now >= due or now - self.checked.get(collection, 0) >= DISCOVERY_INTERVAL_SECONDS)):
                self.retry_after[collection] = now + REMOVAL_CONFIRM_SECONDS
                self.refresh(collection)
        self._arm()

    def _arm(self):
        self.verification.stop()
        if self.paused:
            return
        now = time.monotonic()
        deadlines = [max(min(missing.values()) + REMOVAL_CONFIRM_SECONDS,
                         self.retry_after.get(collection, 0))
                     for collection, missing in self.pending.items()
                     if missing and self.allowed(collection)]
        if deadlines:
            self.verification.start(max(1, int(max(0, min(deadlines) - now) * 1000) + 1))

    def pause(self):
        self.paused = True
        self.verification.stop()
        self.pending.clear()
        self.checked.clear()
        self.retry_after.clear()

    def resume(self):
        self.paused = False
        self._arm()


class PollingCoordinator(QObject):
    """Own ordinary polling policy and coalesced refresh scheduling.

    Device queries and their results stay in the window. Volume-rate steps are
    command feedback, not ordinary polling, and retain their independent timer.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.paused = False
        self._scheduled = {}
        self._commands = {}
        self._observed = {}
        self.periodic = QTimer(self)
        self.periodic.timeout.connect(self._periodic_due)
        self.refresh = QTimer(self)
        self.refresh.setSingleShot(True)
        self.refresh.timeout.connect(self._dispatch)

    def allowed(self):
        w = self.window
        return (not self.paused and not w._shutting_down
                and not w._calibration_operation
                and not missing_config_settings(w.config)
                and (w.calibration_active or w._smtc_background_polling()
                     or (w.isVisible() and not w.isMinimized())))

    def reads_volume(self):
        w = self.window
        return (not (w._volume_rate_loop_running or w._volume_rate_step_running
                     or w._pending_volume_commands or self._commands.get("volume"))
                and (w.calibration_active or not w._amplifier_routed)
                and (w.config["VolumeControls"] or w.calibration_active))

    def reads_inputs(self):
        w = self.window
        return (self.allowed() and not w.calibration_active and not w._input_status_running
                and "inputs" in w._discovery_ready and "inputs" not in w._discovery_pending)

    def update(self, refresh=False):
        interval = 250 if self.window.calibration_active else POLL_INTERVAL_MS
        if self.periodic.interval() != interval:
            self.periodic.setInterval(interval)
        if not self.allowed():
            self.periodic.stop()
            self.refresh.stop()
            return
        if not self.periodic.isActive():
            self.periodic.start()
        if refresh:
            self.request()
        elif self.pending:
            self._arm()

    GROUPS = frozenset(("power", "volume", "inputs"))
    FRESHNESS_SECONDS = POLL_INTERVAL_MS / 1000 / 2  # Half the normal interval.

    @property
    def pending(self):
        return bool(self._scheduled)

    def invalidate(self, groups=None):
        for group in self.GROUPS if groups is None else groups:
            self._observed.pop(group, None)

    def observed(self, group, observed_at):
        self._observed[group] = max(self._observed.get(group, 0), observed_at)

    def hold(self, groups):
        # Cover command queue time as well as the device-settling delay.
        for group in groups:
            self._observed.pop(group, None)
            self._commands[group] = self._commands.get(group, 0) + 1

    def release(self, groups):
        for group in groups:
            self._commands[group] = max(0, self._commands.get(group, 0) - 1)

    def periodic_groups(self):
        if self.window.calibration_active:
            return self.GROUPS
        now = time.monotonic()
        groups = {group for group in self.GROUPS
                  if group not in self._scheduled and not self._commands.get(group)
                  and now - self._observed.get(group, -math.inf) >= self.FRESHNESS_SECONDS}
        if not self.reads_volume():
            groups.discard("volume")
        if not self.reads_inputs():
            groups.discard("inputs")
        return frozenset(groups)

    def request(self, delay_ms=0, condition=None, groups=None, *, replace=False):
        deadline = time.monotonic() + max(0, delay_ms) / 1000
        for group in self.GROUPS if groups is None else groups:
            self._observed.pop(group, None)
            old = self._scheduled.get(group)
            if replace or old is None or old[1] is not None:
                self._scheduled[group] = (deadline, condition)
            elif condition is None:
                self._scheduled[group] = (min(old[0], deadline), None)
            else:
                # Retain the unconditional read, but allow a newer volume
                # completion its settling delay before that read is issued.
                self._scheduled[group] = (max(old[0], deadline), None)
        if delay_ms <= 0:
            self._dispatch()
        else:
            self._arm()

    def _ready_groups(self):
        return {group for group in self._scheduled if not self._commands.get(group)
                and not (group == "inputs" and (self.window._input_status_running
                                                or "inputs" in self.window._discovery_pending))}

    def _arm(self):
        self.refresh.stop()
        groups = self._ready_groups()
        if groups and self.allowed() and not self.window.poll_running:
            deadline = min(self._scheduled[group][0] for group in groups)
            self.refresh.start(max(0, math.ceil((deadline - time.monotonic()) * 1000)))

    def _dispatch(self):
        if not self.allowed() or self.window.poll_running:
            return
        groups = set()
        now = time.monotonic()
        for group in self._ready_groups():
            deadline, condition = self._scheduled[group]
            if deadline > now:
                continue
            del self._scheduled[group]
            if condition is None or condition():
                groups.add(group)
        if groups:
            self.window.poll_status(groups=groups)
        self._arm()

    def _periodic_due(self):
        # Preserve the fixed cadence. Pending/fresh groups do not postpone
        # unrelated reads, and periodic ticks never queue behind active polls.
        if self.allowed() and not self.window.poll_running:
            groups = self.periodic_groups()
            if groups:
                self.window.poll_status(groups=groups)

    def finished(self):
        self._arm()

    def pause(self):
        self.paused = True
        self._scheduled.clear()
        self._commands.clear()
        self.periodic.stop()
        self.refresh.stop()

    def resume(self):
        self.paused = False
        self.update(refresh=True)


class TvVolumeDefault(QObject):
    """One input-handover sequence; queued work rechecks its cancellation token."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.token = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.send)
        self.remaining = 0

    def cancel(self):
        self.token += 1
        self.timer.stop()
        self.remaining = 0

    def allowed(self):
        w = self.window
        state = w.amplifier_state or {}
        return (not w._shutting_down and not w._calibration_blocked()
                and w._tv_controls_available() and w.amplifier_client.state_is_fresh
                and state.get("online") and state.get("power") == "on"
                and state.get("valid_for_ms", 0) > 0 and state.get("input")
                and state["input"] != w._amplifier_tv_input())

    def start(self):
        self.cancel()
        setting = self.window.config["DefaultTvVolume"]
        if setting["Volume"] is not None and self.allowed():
            self.remaining = setting["RepeatCount"] + 1
            self.timer.start(setting["DelayMs"])

    def send(self):
        if not self.allowed() or self.remaining <= 0:
            self.cancel()
            return
        w = self.window
        setting = w.config["DefaultTvVolume"]
        token = self.token
        target = setting["Volume"]
        sent = False
        def command():
            nonlocal sent
            if token != self.token or not self.allowed():
                return
            info = w.api.get_volume()
            if token != self.token or not self.allowed():
                return
            minimum, maximum = info.get("minVolume"), info.get("maxVolume")
            if (type(minimum) is not int or type(maximum) is not int
                    or not minimum <= target <= maximum):
                raise ValueError("DefaultTvVolume.Volume is outside the TV's reported volume range.")
            w.api.set_volume(target)
            sent = True
        def finished():
            if token != self.token:
                return
            if not sent:
                self.cancel()
                return
            self.remaining -= 1
            if self.allowed() and self.remaining > 0:
                self.timer.start(setting["RepeatIntervalMs"])
            else:
                self.cancel()
                w._request_poll(200, groups={"volume"})
        def failed(error):
            self.cancel()
            w.handle_request_error(error, background=True)
        if not w.run_command(command, on_error=failed, on_finished=finished):
            self.cancel()


def cancel_tv_volume_default(window):
    controller = getattr(window, "tv_volume_default", None)
    if controller is not None:
        controller.cancel()


class SonyRemote(QMainWindow):
    _native_volume_ready = Signal()

    # ----------------------------
    # Initialization
    # ----------------------------

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._shortcut_bindings = _compile_shortcuts(config)
        self._mouse_shortcuts = MouseShortcuts(self, _compile_mouse_shortcuts(config), self._accept_mouse_shortcut)
        self.calibration_active = False
        self.volume_mapping = None
        self._mapping_digest = None
        self._calibration_operation = False
        self._status_sequence = 0
        self._status_observed_at = 0
        self._volume_observed_at = 0
        self.calibration_server = None
        self.activation_server = None
        self.mica_enabled = bool(config["Mica"] and supports_mica())
        self.api = SonyApi(config["Host"], config["PreSharedKey"], timeout=NETWORK_TIMEOUT_SECONDS)
        self.pool = QThreadPool(self)
        self.command_pool = QThreadPool(self)
        self.command_pool.setMaxThreadCount(1)
        self._workers = set()
        self.request_history = RequestHistory()
        self.state_dumper = StateDumper(self, self._cached_state)
        self._command_workers = set()
        self.poll_running = False
        self._input_status_running = False
        self.initialized = False
        self._discovery_ready = set()
        self._discovery_pending = set()
        self._discovery_errors = {}
        self.discovery = DiscoveryTracker(self, ("Apps", "Inputs"), self._refresh_discovery, self._can_discover)
        self.tray = None
        self._really_close = False
        self._shutting_down = False
        self._shutdown_complete = False
        self._invalid_config_digest = None
        self._worker_generation = 0
        self._restart_failed = False
        self._config_digest = self._get_config_digest()
        self._config_restart_pending = False

        self.last_power = None
        # None: ordinary control; infinity: standby command pending; otherwise
        # require a power read started after the command completed.
        self._standby_confirmation_after = None
        self.last_volume = None
        self._desired_volume = None
        self._volume_rate_loop_running = False
        self._volume_rate_step_running = False
        self._volume_rate_progress = None
        self._volume_rate_phase = "read"
        self._volume_rate_due = 0.0
        self._volume_rate_candidate = None
        self._pending_volume_commands = 0
        self._volume_generation = 0
        self._volume_route_epoch = 0

        self.tv_volume_default = TvVolumeDefault(self)
        self.amplifier_state = None
        self._amplifier_routed = False
        self._smtc = None
        self._smtc_failed = False
        self._smtc_warning_shown = False
        self._amplifier_error = ""
        self._amplifier_error_category = ""
        self.amplifier_client = AmplifierClient(self)
        self.amplifier_client.state_changed.connect(self._amplifier_state_changed)
        self.amplifier_client.command_failed.connect(self._show_amplifier_error)
        self.amplifier_client.command_succeeded.connect(self._clear_amplifier_error)
        self.amplifier_client.command_acknowledged.connect(self._amplifier_acknowledged)
        self._build_ui()
        self._window_drags = [WindowDrag(self.centralWidget(), background=True)]
        if config["VolumeControls"]:
            self._window_drags.append(WindowDrag(self.volume_value))
        self._apply_style()
        self._setup_timers()
        self._setup_config_watcher()
        self._reload_volume_mapping()
        self._setup_tray()
        self._setup_shortcuts()
        self._setup_calibration()
        self._setup_activation()
        self._setup_smtc()
        self.set_controls_enabled(False)
        self.amplifier_client.start()
        QTimer.singleShot(0, self.initialize)

    def _setup_calibration(self):
        if self.calibration_server is not None:
            self.calibration_server.deleteLater()
        self.calibration_server = CalibrationServer(
            "tv", self._calibration_snapshot, self._calibration_changed,
            self._calibration_perform, self,
        )

    def _calibration_snapshot(self):
        volume = self.last_volume or {}
        observed_at = self._status_observed_at
        if self.last_power == "active":
            observed_at = min(observed_at, self._volume_observed_at)
        age = time.monotonic() - observed_at
        state = {"version": 1, "type": "state", "role": "tv",
                 "online": self.last_power is not None, "power": self.last_power,
                 "volume": volume.get("volume"), "mute": volume.get("mute"),
                 "minimum": volume.get("minVolume"), "maximum": volume.get("maxVolume"),
                 "sample_id": self._status_sequence, "relative_volume": True,
                 "valid_for_ms": remaining_validity_ms(age, STATUS_MAX_AGE_SECONDS)}
        state.update(calibration_active=self.calibration_active,
                     device_id=hashlib.sha256(self.config["Host"].casefold().encode()).hexdigest())
        return state

    def _calibration_changed(self, active):
        cancel_tv_volume_default(self)
        self.calibration_active = active
        self.setEnabled(not active)
        self._desired_volume = None
        self._volume_rate_loop_running = False
        self.volume_rate_timer.stop()
        self._refresh_playback_routing()
        self._update_polling_state(refresh=True)

    def _calibration_perform(self, action, args, done):
        if self._shutting_down or missing_config_settings(self.config):
            done(False, "Device configuration is missing or the app is shutting down.")
            return
        if action not in ("acquire", "set_mute", "adjust_volume"):
            done(False, "Unsupported calibration operation.")
            return
        if action == "set_mute" and type(args.get("value")) is not bool:
            done(False, "Mute must be true or false.")
            return
        if action == "adjust_volume" and (type(args.get("value")) is not int or args["value"] not in (-1, 1)):
            done(False, "Calibration volume steps must be +1 or -1.")
            return
        self._calibration_operation = True
        token = self.calibration_server.token
        deadline = time.monotonic() + 5
        def finish(ok, error=""):
            self._calibration_operation = False
            done(ok, error)
            self._update_polling_state(refresh=True)
        def ready():
            if self._shutting_down or self.calibration_server.token != token:
                finish(False, "Calibration session ended.")
                return
            if (self._workers or self.pool.activeThreadCount()
                    or self.command_pool.activeThreadCount()):
                if time.monotonic() >= deadline:
                    finish(False, "Device requests did not finish in time.")
                else:
                    QTimer.singleShot(25, ready)
                return
            if action == "acquire":
                finish(True)
                return
            function = (lambda: self.api.set_mute(args["value"])) if action == "set_mute" else (lambda: self.api.adjust_volume(args["value"]))
            if not self.run_command(function, on_result=lambda _: finish(True),
                                    on_error=lambda error: finish(False, f"{type(error).__name__}: {error}")):
                finish(False, "Device command queue is unavailable.")
        ready()

    def _setup_timers(self):
        self.polling = PollingCoordinator(self)
        self.poll_timer = self.polling.periodic

        self.volume_rate_timer = QTimer(self)
        self.volume_rate_timer.setSingleShot(True)
        self.volume_rate_timer.timeout.connect(self._volume_rate_step)

    def _playback_key_mode(self):
        if not self.config.get("SMTCPlaybackKeys", False):
            return "focused"
        if sys.platform != "win32" or self._smtc_failed:
            return "unavailable"
        return "smtc"

    def _setup_smtc(self):
        if not self.config["SMTCPlaybackKeys"]:
            return
        self._smtc_failed = False
        try:
            if sys.platform != "win32":
                raise OSError("SMTC playback keys require Windows.")
            if self._smtc is None:
                self._smtc = SMTCPlayback(self)
                self._smtc.button_pressed.connect(self._handle_smtc_button)
                self._smtc.failed.connect(self._smtc_failure)
            # winId creates an HWND without showing a start-in-tray window.
            self._smtc.start(int(self.winId()))
            self._refresh_playback_routing()
        except Exception as error:
            self._smtc_failure(error)

    def _smtc_failure(self, error):
        self._smtc_failed = True
        if self._smtc is not None:
            self._smtc.stop()
        record_exception(error, "SMTC playback keys", deduplicate=True)
        self._refresh_playback_routing()
        self._update_polling_state()
        if not self._smtc_warning_shown:
            self._smtc_warning_shown = True
            QTimer.singleShot(0, self._show_smtc_warning)

    def _show_smtc_warning(self):
        if self._shutting_down:
            return
        details = ("This option requires Windows." if sys.platform != "win32" else
                   "Install the optional packages and restart:\n\n"
                   "pip install winrt-Windows.Media winrt-Windows.Media.Interop winrt-Windows.Foundation")
        QMessageBox.warning(self, APP_NAME,
                            "SMTC playback keys are unavailable.\n\n" + details +
                            "\n\nPlayback media keys are not intercepted in this mode. "
                            "The remote's on-screen buttons remain available. "
                            "Set SMTCPlaybackKeys to false to restore focused playback keys.")

    def _smtc_commands(self):
        if (self._playback_key_mode() != "smtc" or self._shutting_down
                or self._calibration_blocked() or not self._tv_controls_available()
                or QApplication.activeModalWidget() is not None
                or "remote_codes" not in self._discovery_ready):
            return frozenset()
        return frozenset(command for command in _SMTC_BUTTON_COMMANDS.values()
                         if self.api.resolve_remote_command(command) in self.api.remote_codes
                         and not any(self._amplifier_owns_hotkeys(key) for key in _SMTC_COMMAND_KEYS[command]))

    def _refresh_playback_routing(self):
        if self._smtc is not None:
            self._smtc.update(self._smtc_commands())
        self._publish_native_volume_state()

    def _smtc_background_polling(self):
        return self._playback_key_mode() == "smtc" and self._smtc is not None

    def _handle_smtc_button(self, command):
        # Availability may have changed since Windows queued this event.
        if command in self._smtc_commands():
            self.send_ircc(command, background=True)

    def _setup_shortcuts(self):
        self._popup_shortcut_keys = set()
        self._shortcut_presses = {}
        self._shortcut_refresh_pending = False
        self._native_volume_hook = None
        self._native_volume_lock = threading.Lock()
        self._native_volume_state = (0, True, 0)
        self._native_volume_queue = deque()
        self._native_volume_drain_pending = False
        self._native_volume_ready.connect(self._drain_native_volume, Qt.QueuedConnection)
        QApplication.instance().installEventFilter(self)
        self._publish_native_volume_state()
        if sys.platform == "win32":
            self._install_windows_volume_hook()

    # ----------------------------
    # UI construction and styling
    # ----------------------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        if self.mica_enabled:
            central.setAttribute(Qt.WA_TranslucentBackground)
        self.setCentralWidget(central)
        self.main_layout = QVBoxLayout(central)
        self.main_layout.setContentsMargins(12, 12, 12, 12)
        self.main_layout.setSpacing(9)

        self.power_button = QPushButton("Connecting...")
        self.power_button.setMinimumHeight(38)
        self.power_button.clicked.connect(self.toggle_power)
        self.main_layout.addWidget(self.power_button)

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
        self._build_favorites_from_config()

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
        self.main_layout.addWidget(frame)
        return layout

    def _build_text_input_controls(self):
        layout = self._section("Text Input")

        self.text_input = TvTextEdit()
        self.text_input.setToolTip(_shortcut_tip(self.config, "focus_text_input"))
        self.text_input.focusRemoteRequested.connect(self._leave_text_input)
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
            button.setToolTip(_shortcut_tip(self.config, command))
            button.setProperty("remoteCommand", command)
            self.channel_buttons[command] = button
            grid.addWidget(button, row, column)

        self.main_layout.addLayout(grid)

    def _build_app_controls(self):
        layout = self._section("Apps")
        self.app_combo = CenteredComboBox(animate_popup=self.config["DropdownAnimationWorkaround"], clear_on_close=True)
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

    def _build_navigation_controls(self):
        layout = self._section("Navigation")
        compact = self.config["NavigationControlsCompact"]

        def make_button(text, command):
            button = QPushButton(text)
            button.setMinimumHeight(36)
            button.clicked.connect(lambda checked=False: self.send_ircc(command))
            button.setToolTip(_shortcut_tip(self.config, command))
            button.setProperty("remoteCommand", command)
            return button

        def add_menu_row(entries):
            row = QHBoxLayout()
            row.setSpacing(6)
            for text, command in entries:
                row.addWidget(make_button(text, command), 1)
            layout.addLayout(row)

        if not compact:
            add_menu_row((("Input", "TvInput"), ("Settings", "Settings")))

        grid = QGridLayout()
        grid.setSpacing(6)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        entries = [
            ("↑", 0, 1, "Up"), ("←", 1, 0, "Left"),
            ("OK", 1, 1, "Confirm"), ("→", 1, 2, "Right"),
            ("↓", 2, 1, "Down"),
        ]
        if compact:
            entries.extend((
                ("Input", 0, 0, "TvInput"), ("Settings", 0, 2, "Settings"),
                ("Back", 2, 0, "Return"), ("Home", 2, 2, "Home"),
            ))
        for text, row, column, command in entries:
            grid.addWidget(make_button(text, command), row, column)
        layout.addLayout(grid)

        if not compact:
            add_menu_row((("Back", "Return"), ("Home", "Home")))

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
        self.volume_down.setToolTip(_shortcut_tip(self.config, "volume_down"))
        self.volume_up.setToolTip(_shortcut_tip(self.config, "volume_up"))
        for button, amount in ((self.volume_down, -1), (self.volume_up, 1)):
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(500)
            button.setAutoRepeatInterval(100)
            # pressed fires immediately and on auto-repeat; release adds no step.
            button.pressed.connect(lambda amount=amount: self.adjust_volume(amount))

        row.addWidget(self.volume_down, 0, 0)
        row.addWidget(self.volume_value, 0, 1)
        row.addWidget(self.volume_up, 0, 2)
        layout.addLayout(row)

        self.mute_button = QPushButton("Mute")
        self.mute_button.setToolTip(_shortcut_tip(self.config, "mute"))
        self.mute_button.clicked.connect(self.toggle_mute)
        layout.addWidget(self.mute_button)

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
            button.setProperty("remoteCommand", command)
            button.setToolTip(_shortcut_tip(self.config, command))
            row.addWidget(button)
        layout.addLayout(row)

    def _build_favorites_from_config(self):
        collections = (
            ("Apps", "app_favorites_layout", self.launch_app),
            ("Inputs", "input_favorites_layout", self.set_input),
        )
        for collection, layout_name, action in collections:
            if not hasattr(self, layout_name):
                continue
            layout = getattr(self, layout_name)
            buttons = {}
            while layout.count():
                widget = layout.takeAt(0).widget()
                if widget is not None:
                    buttons[widget.property("remote_id")] = widget
            favorites = [
                (uri, settings)
                for uri, settings in _ordered_entries(
                    self.config[collection], self.config["SortByName"]
                )
                if settings["Favorite"] and not settings.get("Removed", False)
            ]
            for index, (uri, settings) in enumerate(favorites):
                button = buttons.pop(uri, None)
                if button is None:
                    button = FavoriteButton()
                    button.setProperty("remote_id", uri)
                    button.clicked.connect(
                        lambda checked=False, value=uri, callback=action: callback(value)
                    )
                button.setText(_entry_display_name(settings))
                row, column = divmod(index, 2)
                layout.addWidget(button, row, column)
                button.show()
            for button in buttons.values():
                button.hide()
                button.deleteLater()

    def _build_input_controls(self):
        layout = self._section("Inputs")
        self.input_combo = CenteredComboBox(animate_popup=self.config["DropdownAnimationWorkaround"])
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

    def _fit_and_lock_window(self):
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        # Recalculate nested section layouts before locking the new size.
        for layout in reversed(self.findChildren(QLayout)):
            layout.invalidate()
            layout.activate()
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def _apply_style(self):
        # Leave colors and control rendering to the platform's Qt style.
        # Geometry/typography and the optional Windows Mica section surfaces are customized.
        for kind in (QPushButton, QComboBox):
            for control in self.findChildren(kind):
                control.setMinimumHeight(max(34, control.minimumHeight()))
        background = "transparent" if self.mica_enabled else "palette(window)"
        # mica_enabled is only true on supported Windows versions.
        section_fill = ""
        mica_controls = ""
        if self.mica_enabled:
            dark = self.config["DarkMode"]
            surface = "rgba(38,38,38,185)" if dark else "rgba(255,255,255,190)"
            section_border = "rgba(120,120,120,110)" if dark else "rgba(0,0,0,25)"
            section_fill = f"background: {surface}; border-color: {section_border};"
            normal, hover, pressed = (("48,48,48", "68,68,68", "38,38,38") if dark else
                                      ("255,255,255", "245,245,245", "235,235,235"))
            text = "235,235,235" if dark else "20,20,20"
            border = "rgba(120,120,120,150)" if dark else "rgba(0,0,0,35)"
            menu = "40,40,40" if dark else "250,250,250"
            menu_border = "120,120,120" if dark else "180,180,180"
            selection = "80,110,170" if dark else "210,225,250"
            # Opaque control surfaces prevent native Mica treatment showing through.
            mica_controls = f"""
                QPushButton, QComboBox, QTextEdit {{
                    background-color: rgb({normal});
                    color: rgb({text});
                    border: 1px solid {border};
                    border-radius: 7px;
                    padding: 5px 7px;
                }}
                QComboBox::drop-down {{
                    border: none; background: transparent; width: 20px;
                }}
                QPushButton:hover, QComboBox:hover {{ background-color: rgb({hover}); }}
                QPushButton:pressed {{ background-color: rgb({pressed}); }}
                QPushButton:disabled, QComboBox:disabled, QTextEdit:disabled {{
                    background-color: rgb({normal});
                    color: rgba({text},100);
                }}
                QComboBox QAbstractItemView {{
                    background-color: rgb({menu});
                    color: rgb({text});
                    selection-background-color: rgb({selection});
                    selection-color: rgb({text});
                    border: 1px solid rgb({menu_border});
                }}
            """
        self.setStyleSheet("""
            ROOT_SELECTOR { background: BACKGROUND; }
            QWidget { font-size: 9pt; }
            QFrame#section {
                border: 1px solid palette(mid);
                border-radius: 8px;
                SECTION_FILL
            }
            QPushButton, QComboBox { padding: 5px 7px; }
            QLabel { background: transparent; }
            QLabel#sectionTitle { font-weight: 600; }
            QLabel#volumeValue { font-size: 13pt; font-weight: 600; }
            MICA_CONTROLS
        """.replace("ROOT_SELECTOR", "QMainWindow, QWidget#central").replace("BACKGROUND", background).replace("SECTION_FILL", section_fill).replace("MICA_CONTROLS", mica_controls))

    # ----------------------------
    # Background work
    # ----------------------------

    def run_command(self, function, on_result=None, on_error=None, on_finished=None,
                    *, refresh_groups=None, refresh_delay_ms=200):
        if refresh_groups is None:
            return self.run_worker(function, on_result, on_error, on_finished, command=True)
        groups = frozenset(refresh_groups)
        self.polling.hold(groups)
        def finished():
            self.polling.release(groups)
            # Start settling time after this command, including failures whose
            # device-side outcome is uncertain. Never retry the command itself.
            self.polling.request(refresh_delay_ms, groups=groups, replace=True)
            if on_finished:
                on_finished()
        accepted = self.run_worker(function, on_result, on_error, finished, command=True)
        if not accepted:
            self.polling.release(groups)
            self.polling.finished()
        return accepted

    def run_worker(self, function, on_result=None, on_error=None, on_finished=None, *, command=False, request_group="background"):
        if self._shutting_down or missing_config_settings(self.config):
            return False
        if command and len(self._command_workers) >= MAX_PENDING_COMMANDS:
            # Keep key repeats from creating an unbounded delayed command ramp.
            return False
        if command:
            request_group = "command"
        worker = Worker(function)
        self._workers.add(worker)
        if command:
            self._command_workers.add(worker)
        generation = self._worker_generation

        def deliver_result(result):
            if not self._shutting_down and generation == self._worker_generation:
                self.request_history.record(request_group)
                if on_result:
                    on_result(result)

        def deliver_error(error):
            if not self._shutting_down and generation == self._worker_generation:
                self.request_history.record(request_group, error)
                (on_error or self.handle_request_error)(error)

        def finished():
            self._workers.discard(worker)
            self._command_workers.discard(worker)
            if not self._shutting_down and generation == self._worker_generation and on_finished:
                on_finished()

        worker.signals.result.connect(deliver_result)
        worker.signals.error.connect(deliver_error)
        worker.signals.finished.connect(finished)
        (self.command_pool if command else self.pool).start(worker)
        return True

    # ----------------------------
    # Discovery and polling
    # ----------------------------

    def initialize(self):
        if self._shutting_down or self.calibration_active:
            return
        if missing_config_settings(self.config):
            self.power_button.setText("Configure device")
            return

        tasks = []
        if ((self.config["TextInputControls"] and self.config["TextInputSubmit"])
                or self.config["ChannelControls"] or self.config["NavigationControls"]
                or self.config["PlaybackControls"]):
            tasks.append("remote_codes")
        if self.config["AppControls"]:
            tasks.append("apps")
        if self.config["InputControls"]:
            tasks.append("inputs")

        for feature in tasks:
            if feature not in self._discovery_ready:
                self._start_discovery(feature)
        self._update_polling_state()

    def _can_discover(self, collection):
        return bool(not self._shutting_down and not self.calibration_active
                    and not self._calibration_operation and self.last_power == "active"
                    and time.monotonic() - self._status_observed_at < STATUS_MAX_AGE_SECONDS
                    and self.config["AppControls" if collection == "Apps" else "InputControls"]
                    and self.isVisible() and not self.isMinimized() and self.polling.allowed())

    def _refresh_discovery(self, collection):
        if self._can_discover(collection):
            self._start_discovery("apps" if collection == "Apps" else "inputs")

    def _start_discovery(self, feature):
        if (feature in self._discovery_pending or self._shutting_down or self.calibration_active
                or feature == "inputs" and self._input_status_running):
            return
        function = getattr(self.api, {"apps": "get_apps", "inputs": "get_inputs",
                                     "remote_codes": "load_remote_codes"}[feature])
        self._discovery_pending.add(feature)
        def discover():
            return function(), time.monotonic()
        accepted = self.run_worker(
            discover,
            on_result=lambda result: self._discovery_succeeded(feature, *result),
            on_error=lambda error: self._discovery_failed(feature, error),
            on_finished=lambda: self._discovery_finished(feature), request_group=feature,
        )
        if not accepted:
            self._discovery_pending.discard(feature)

    def _discovery_finished(self, feature):
        self._discovery_pending.discard(feature)
        if feature == "inputs":
            self.polling.finished()

    def _discovery_succeeded(self, feature, result, observed_at=None):
        if feature in ("apps", "inputs"):
            collection = "Apps" if feature == "apps" else "Inputs"
            try:
                present = _inventory_ids(result, "uri", "title")
                changed = self._sync_discovered_config(collection, result)
                changed = self.discovery.observe(
                    collection, present, self.config[collection],
                    allow_missing=self._can_discover(collection), observed_at=observed_at) or changed
                if changed or feature not in self._discovery_ready:
                    combo = self.app_combo if feature == "apps" else self.input_combo
                    combo.hidePopup()
                    if feature == "apps":
                        self.populate_apps()
                    else:
                        self.populate_inputs(result)
                    self._build_favorites_from_config()
                    self._fit_and_lock_window()
                elif feature == "inputs":
                    self.update_active_input(result)
            except Exception as error:
                self._discovery_failed(feature, error)
                return
            if changed:
                try:
                    self._save_config_without_restart()
                except Exception as error:
                    record_exception(error, "Configuration save")
                    self.show_error(f"Could not update SonyRemote.json:\n\n{error}")
        if feature == "inputs" and observed_at is not None:
            self.polling.observed("inputs", observed_at)
        self._discovery_ready.add(feature)
        self._discovery_errors.pop(feature, None)
        self.set_controls_enabled(self.last_power == "active",
                                  power_enabled=self.last_power is not None)
        self._update_discovery_feedback()

    def _discovery_failed(self, feature, error):
        record_unexpected_exception(error, f"Optional {feature} discovery", background=True)
        if feature not in ("apps", "inputs"):
            self._discovery_ready.discard(feature)
        self._discovery_errors[feature] = (str(error) if isinstance(error, (DeviceConnectionError, DeviceResponseError))
                                           else f"Unexpected discovery error ({type(error).__name__}).")
        self.set_controls_enabled(self.last_power == "active",
                                  power_enabled=self.last_power is not None)
        self._update_discovery_feedback()

    def _update_discovery_feedback(self):
        labels = {"remote_codes": "Remote buttons", "apps": "Apps", "inputs": "Inputs"}
        messages = [f"{labels[feature]} unavailable; retrying. {message}"
                    for feature, message in self._discovery_errors.items()]
        if self._amplifier_error:
            messages.append("Amplifier control: " + self._amplifier_error)
        self.power_button.setToolTip("\n".join(messages))
        for feature, name in (("apps", "app_combo"), ("inputs", "input_combo")):
            widget = getattr(self, name, None)
            if widget is not None:
                error = self._discovery_errors.get(feature)
                widget.setToolTip(f"{labels[feature]} unavailable; retrying. {error}" if error else "")

    def _sync_discovered_config(self, collection, entries):
        changed = False
        settings = self.config.setdefault(collection, {})

        for entry in entries:
            uri = str(entry.get("uri", "")).strip()
            name = html.unescape(str(entry.get("title", ""))).strip()
            if not uri or not name:
                continue

            item = settings.get(uri)
            if not isinstance(item, dict):
                item = {}
                settings[uri] = item
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
            if "Removed" not in item:
                item["Removed"] = False
                changed = True

        return changed

    def populate_apps(self):
        apps_config = self.config.get("Apps", {})
        apps = []

        for uri, settings in _ordered_entries(apps_config, self.config["SortByName"]):
            if not settings["Hidden"] and not settings.get("Removed", False):
                apps.append((_entry_display_name(settings), uri))

        self.app_combo.blockSignals(True)
        self.app_combo.clear()
        self.app_combo.setPlaceholderText("Select app")
        for label, uri in apps:
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

        available = {
            str(entry.get("uri", "")).strip(): entry
            for entry in entries
            if str(entry.get("uri", "")).strip()
            and html.unescape(str(entry.get("title", ""))).strip()
        }
        for uri, settings in _ordered_entries(inputs_config, self.config["SortByName"]):
            if settings["Hidden"] or settings.get("Removed", False):
                continue
            inputs.append((_entry_display_name(settings), uri))
            if str(available.get(uri, {}).get("status", "")).lower() == "true":
                active_uri = uri

        self.input_combo.blockSignals(True)
        self.input_combo.clear()
        self.input_combo.setPlaceholderText("Select input")

        active_index = -1
        for label, uri in inputs:
            self.input_combo.addItem(label, uri)
            self.input_combo.setItemData(
                self.input_combo.count() - 1, Qt.AlignCenter, Qt.TextAlignmentRole
            )
            if uri == active_uri:
                active_index = self.input_combo.count() - 1

        self.input_combo.setCurrentIndex(active_index)
        self.input_combo.blockSignals(False)

    def _accept_volume_status(self, info, observed_at):
        if not info or observed_at < self._volume_observed_at:
            return False
        self.last_volume = info
        self._volume_observed_at = observed_at
        self.polling.observed("volume", observed_at)
        self._publish_native_volume_state()
        return True

    def poll_status(self, groups=None):
        if not self.polling.allowed():
            return
        if self.poll_running:
            return
        groups = self.polling.GROUPS if groups is None else frozenset(groups)
        if not groups & {"power", "volume"}:
            if "inputs" in groups and self.last_power == "active" and self.config["InputControls"]:
                self._poll_input_status()
            return
        self.poll_running = True
        volume_request = (self._volume_generation, self._desired_volume)
        volume_busy = bool(self._volume_rate_loop_running or self._pending_volume_commands)
        read_volume = "volume" in groups and self.polling.reads_volume()
        cached_power = self.last_power
        def query():
            read_power = "power" in groups or cached_power is None
            power_started_at = time.monotonic() if read_power else None
            power = self.api.get_power() if read_power else cached_power
            data = {"power": power, "observed_at": time.monotonic() if read_power else None,
                    "power_started_at": power_started_at}
            # Recheck after the power request: a limiter may have started
            # while it was in flight. Never delay the limiter behind this poll.
            if (power == "active" and read_volume
                    and self._standby_confirmation_after is None
                    and self.polling.reads_volume()):
                data["volume"] = self.api.get_volume()
                data["volume_observed_at"] = time.monotonic()
            return data

        def done(data):
            # Power remains useful even if this poll's volume was superseded.
            self._status_sequence += 1
            if data["observed_at"] is not None:
                self._status_observed_at = data["observed_at"]
                self.polling.observed("power", data["observed_at"])
            self._update_discovery_feedback()
            self.last_power = data.get("power")
            if (self._standby_confirmation_after is not None
                    and data["power_started_at"] is not None
                    and data["power_started_at"] >= self._standby_confirmation_after):
                self._standby_confirmation_after = None

            self.initialized = True
            # Retry only missing discovery, independently of successful status.
            QTimer.singleShot(0, self.initialize)
            is_active = self.last_power == "active"
            self.set_controls_enabled(
                is_active,
                power_enabled=True,
            )
            self.update_power_ui(self.last_power)
            if not is_active:
                cancel_tv_volume_default(self)
                self._desired_volume = None
                self._volume_rate_loop_running = False
                self.volume_rate_timer.stop()
            elif (volume_request == (self._volume_generation, self._desired_volume)
                    and not (self._volume_rate_loop_running or self._volume_rate_step_running)
                    and (self.calibration_active or not self._amplifier_routed)
                    and self._accept_volume_status(data.get("volume"), data.get("volume_observed_at", 0))):
                if not (volume_busy or self._pending_volume_commands):
                    self._desired_volume = None
                self._update_volume_controls()
            if self.config["VolumeControls"]:
                self.update_volume_ui(self.last_volume or {})
            if self.calibration_server is not None:
                self.calibration_server.publish()
            if is_active and "inputs" in groups and self.config["InputControls"]:
                self._poll_input_status()

        def failed(error):
            self.handle_request_error(error, background=True)

        self.run_worker(query, done, failed, self._status_poll_finished, request_group="status")

    def _status_poll_finished(self):
        self.poll_running = False
        self.discovery.tick()
        self.polling.finished()

    def _poll_input_status(self):
        if not self.polling.reads_inputs():
            return
        self._input_status_running = True
        def query():
            entries = self.api.get_inputs()
            return entries, time.monotonic()
        def done(result):
            entries, observed_at = result
            self._discovery_succeeded("inputs", entries, observed_at)
        def finished():
            self._input_status_running = False
            self.polling.finished()
        accepted = self.run_worker(
            query, done,
            lambda error: record_unexpected_exception(error, "Optional input status", background=True),
            finished, request_group="inputs",
        )
        if not accepted:
            self._input_status_running = False

    def _update_polling_state(self, refresh=False):
        if hasattr(self, "polling"):
            self.polling.update(refresh=refresh)

    def _request_poll(self, delay_ms=0, condition=None, groups=None):
        self.polling.request(delay_ms, condition, groups)

    # ----------------------------
    # State and request errors
    # ----------------------------

    def set_controls_enabled(
        self,
        enabled,
        power_enabled=None,
    ):
        if power_enabled is None:
            power_enabled = enabled

        enabled = bool(enabled and self._tv_controls_available())
        self.power_button.setEnabled(power_enabled and self._standby_confirmation_after is None)

        for widget in (
            getattr(self, "text_input", None),
            getattr(self, "input_combo", None),
            getattr(self, "mute_button", None),
            getattr(self, "volume_down", None),
            getattr(self, "volume_up", None),
        ):
            if widget is not None:
                widget.setEnabled(enabled)

        for button in self.centralWidget().findChildren(QPushButton):
            if button is self.power_button or button.window() is not self:
                continue
            command = button.property("remoteCommand")
            supported = (command is None or ("remote_codes" in self._discovery_ready
                         and self.api.resolve_remote_command(command) in self.api.remote_codes))
            button.setEnabled(bool(enabled and supported))

        apps_enabled = bool(enabled and "apps" in self._discovery_ready)
        inputs_enabled = bool(enabled and "inputs" in self._discovery_ready)
        if hasattr(self, "app_combo"):
            self.app_combo.setEnabled(apps_enabled)
        if hasattr(self, "input_combo"):
            self.input_combo.setEnabled(inputs_enabled)
        for name, available in (("app_favorites_layout", apps_enabled),
                                ("input_favorites_layout", inputs_enabled)):
            layout = getattr(self, name, None)
            if layout is not None:
                for index in range(layout.count()):
                    button = layout.itemAt(index).widget()
                    if isinstance(button, QPushButton):
                        button.setEnabled(available)
        self._update_volume_controls()
        self._refresh_playback_routing()

    def handle_request_error(self, error, *, background=False, hint=None):
        record_unexpected_exception(
            error, "Background request" if background else "Command", background=background
        )
        if isinstance(error, DeviceConnectionError):
            self.set_offline()
        elif background:
            self.set_error_status()
        if isinstance(error, (DeviceConnectionError, DeviceResponseError)):
            message = str(error)
        else:
            message = f"Unexpected request error ({type(error).__name__})."
        self.power_button.setToolTip(message)
        if not background:
            if hint:
                message += "\n\n" + hint
            self.show_error(message)

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

    def set_offline(self):
        self.polling.invalidate()
        self.last_power = None
        self.last_volume = None
        self._volume_observed_at = 0
        self._desired_volume = None
        self._volume_rate_loop_running = False
        self.volume_rate_timer.stop()
        self.power_button.setText("Offline")
        self.set_controls_enabled(False, power_enabled=False)
        if self.config["VolumeControls"]:
            self.update_volume_ui({})

    def set_error_status(self):
        self.set_offline()
        self.power_button.setText("Error")

    def update_power_ui(self, status):
        if status == "active":
            self.power_button.setText("Standby")
        else:
            self.power_button.setText("Power On")

    def update_volume_ui(self, volume):
        if self.last_power != "active":
            self.volume_value.setText("—")
            self.volume_value.setToolTip("TV volume")
            self.mute_button.setText("Mute")
            return
        prefix = ""
        if self._use_amplifier():
            raw = self.amplifier_state["raw_volume"]
            mapping = self._matched_volume_mapping()
            if mapping:
                display, approximate = mapped_tv_volume(mapping, raw)
                prefix = "≈" if approximate else ""
                self.volume_value.setToolTip("Calibrated TV scale")
            else:
                display = self.amplifier_state["volume_display"]
                self.volume_value.setToolTip(
                    "Amplifier volume (dB)" if self.amplifier_state["volume_unit"] == "dB"
                    else "Amplifier raw volume")
            volume = {"volume": display, "mute": self.amplifier_state["mute"]}
        else:
            self.volume_value.setToolTip("TV volume")
            if self._desired_volume is not None:
                # Every repaint must preserve the user's pending TV target,
                # including unrelated amplifier IPC updates. Keep mute factual.
                volume = dict(volume, volume=self._desired_volume)
        self.volume_value.setText(prefix + str(volume.get("volume", "—")))
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

    # ----------------------------
    # Device actions and shortcuts
    # ----------------------------

    def _calibration_blocked(self):
        return bool(self.calibration_active or (self.amplifier_state or {}).get("calibration_active", False))

    def _tv_controls_available(self):
        return (not self._shutting_down and self.last_power == "active"
                and self._standby_confirmation_after is None)

    def toggle_power(self):
        cancel_tv_volume_default(self)
        if self._calibration_blocked():
            return
        if self._standby_confirmation_after is not None:
            return
        turn_on = self.last_power != "active"
        if not turn_on:
            self._standby_confirmation_after = math.inf
            # Cancel intent before queueing standby: old reads/errors and queued
            # relative commands must not continue after the power command.
            self._volume_route_epoch += 1
            self._volume_generation += 1
            self._desired_volume = None
            self._volume_rate_loop_running = False
            self._volume_rate_progress = None
            self._volume_rate_phase = "read"
            self._volume_rate_candidate = None
            self._volume_rate_due = 0.0
            self.volume_rate_timer.stop()
            if self.config["VolumeControls"]:
                self.update_volume_ui(self.last_volume or {})
        self.set_controls_enabled(self.last_power == "active", power_enabled=False)
        def succeeded(_result):
            if not turn_on:
                self._standby_confirmation_after = time.monotonic()
        def failed(error):
            self._standby_confirmation_after = None
            self.set_controls_enabled(self.last_power == "active",
                                      power_enabled=self.last_power is not None)
            self.handle_request_error(error)
        def finished():
            self.set_controls_enabled(self.last_power == "active",
                                      power_enabled=self.last_power is not None)
        accepted = self.run_command(
            lambda: self.api.set_power(turn_on),
            on_result=succeeded, on_error=failed, on_finished=finished,
            refresh_groups=self.polling.GROUPS, refresh_delay_ms=300,
        )
        if not accepted:
            self._standby_confirmation_after = None
            finished()

    def _matched_volume_mapping(self):
        mapping = self.volume_mapping
        state = self.amplifier_state or {}
        tv_id = hashlib.sha256(self.config["Host"].casefold().encode()).hexdigest()
        if mapping and mapping["tv_id"] == tv_id and mapping["amplifier_id"] == state.get("device_id"):
            return mapping
        return None

    def _amplifier_tv_input(self):
        mapping = self._matched_volume_mapping()
        return mapping["input"] if mapping else self.config["AmplifierTVInput"]

    def _reload_volume_mapping(self):
        path = CONFIG_PATH.with_name("VolumeCalibration.json")
        content, digest = _file_snapshot(path)
        if digest == self._mapping_digest:
            return
        self._mapping_digest = digest
        if digest is None:
            self.volume_mapping = None
        else:
            try:
                if len(content) > 2 * 1024 * 1024:
                    raise ValueError("Volume calibration JSON exceeds the 2 MiB limit.")
                document = json.loads(content.decode("utf-8"))
                self.volume_mapping = prepare_volume_mapping(document)
            except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
                # Keep the last valid mapping after a bad edit. On first load
                # the configured input and raw-volume display remain available.
                record_exception(error, "Volume calibration JSON")
                return
        self._amplifier_state_changed(self.amplifier_state)

    def _use_amplifier(self):
        state = self.amplifier_state
        return bool(not self._shutting_down and state and self.amplifier_client.state_is_fresh
                    and self._amplifier_tv_input()
                    and state["online"] and state["power"] == "on" and state["valid_for_ms"] > 0
                    and state["input"] == self._amplifier_tv_input()
                    and state["raw_volume"] is not None and type(state["mute"]) is bool)

    def _focused_tv_volume_bindings(self):
        if (self._shutting_down or self._calibration_blocked() or not self.initialized
                or not self._tv_controls_available() or not self.last_volume or self._use_amplifier()
                or not self.isActiveWindow() or self.isMinimized() or not self.isVisible()
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None or not self._amplifier_tv_input()):
            return []
        native_ready = (sys.platform != "win32" or self._native_volume_hook is not None
                        and self._native_volume_hook.installed)
        editing = hasattr(self, "text_input") and self.text_input.hasFocus()
        return [code for code, action in self._shortcut_bindings.items()
                if action in ("volume_up", "volume_down", "mute")
                and not (code & 0x01FFFFFF in _QT_PLAYBACK_KEYS and self._playback_key_mode() != "focused")
                and (code & 0x01FFFFFF not in _NATIVE_QT_VOLUME_KEYS.values() or native_ready)
                and (not editing or code & 0x01FFFFFF in _NATIVE_QT_VOLUME_KEYS.values())][:128]

    def _focused_tv_override_bindings(self):
        state = self.amplifier_state or {}
        if (not self.amplifier_client.state_is_fresh
                or not state.get("focused_volume_override_supported", False)
                or not state.get("input") or state["input"] == self._amplifier_tv_input()
                or time.monotonic() - self._status_observed_at >= STATUS_MAX_AGE_SECONDS):
            return []
        return self._focused_tv_volume_bindings()

    def _amplifier_owns_hotkeys(self, code=None, *, focused=False):
        # Global ownership is authoritative. A capable amplifier may yield matching
        # keys to the prepared foreground TV-volume handler.
        active = bool(not self._shutting_down and self.amplifier_state
                    and self.amplifier_client.state_is_fresh
                    and self.amplifier_state["online"] and self.amplifier_state["power"] == "on"
                    and self.amplifier_state["valid_for_ms"] > 0
                    and self.amplifier_state["global_hotkeys_active"])
        if not active or code is None:
            return active
        if focused and _binding_owned(self._focused_tv_override_bindings(), code):
            return False
        if "global_shortcuts" not in self.amplifier_state:
            # Older producers own the three physical volume keys.
            return code & 0x01FFFFFF in _NATIVE_QT_VOLUME_KEYS.values()
        return _binding_owned(self.amplifier_state["global_shortcuts"], code)

    def _amplifier_state_changed(self, state):
        previous = self.amplifier_state or {}
        confirmed_handover = bool(self._amplifier_routed and state
            and state.get("input") and state["input"] != previous.get("input")
            and previous.get("input") == self._amplifier_tv_input())
        self.amplifier_state = state
        controller = getattr(self, "tv_volume_default", None)
        if controller is not None:
            if not controller.allowed():
                controller.cancel()
            elif confirmed_handover:
                controller.start()
        if (self._amplifier_error_category in ("connection", "protocol_state") and state
                and state["valid_for_ms"] > 0 and self.amplifier_client.state_is_fresh):
            self._clear_amplifier_error()
        routed = self._use_amplifier()
        falling_back = self._amplifier_routed and not routed
        if routed != self._amplifier_routed:
            self._volume_route_epoch += 1
            self._volume_generation += 1
            self._desired_volume = None
            self._volume_rate_loop_running = False
            self.volume_rate_timer.stop()
        self._amplifier_routed = routed
        if falling_back and not self.calibration_active:
            # The physical TV may have changed while we displayed amplifier
            # values. Do not act on its old cached volume before the refresh.
            self.last_volume = None
            self._volume_observed_at = 0

        self._refresh_playback_routing()
        self._update_volume_controls()
        if self.config["VolumeControls"]:
            self.update_volume_ui(self.last_volume or {})
        if (falling_back and not self.calibration_active and self.config["VolumeControls"]
                and self.last_power == "active"):
            self._request_poll(groups={"volume"})

    def _update_volume_controls(self):
        if self.config["VolumeControls"]:
            enabled = (self._tv_controls_available() and self.initialized
                       and (self._use_amplifier() or bool(self.last_volume)))
            for widget in (self.volume_up, self.volume_down, self.mute_button):
                widget.setEnabled(enabled)
            if self.last_power != "active":
                self.update_volume_ui({})

    def _amplifier_acknowledged(self):
        if self._amplifier_error_category == "protocol_ack":
            self._clear_amplifier_error()

    def _show_amplifier_error(self, message, category="command"):
        # Match background feedback without changing the TV state or opening a dialog.
        self._amplifier_error = message
        self._amplifier_error_category = category
        self._update_discovery_feedback()

    def _clear_amplifier_error(self):
        self._amplifier_error = ""
        self._amplifier_error_category = ""
        self._update_discovery_feedback()

    def _route_volume_command(self, command):
        if not self._use_amplifier():
            return False
        if self.amplifier_client.send_command(command, self._amplifier_tv_input()) is False:
            self._show_amplifier_error("Command could not be sent. Command was not sent to the TV or retried.")
        return True

    def toggle_mute(self):
        cancel_tv_volume_default(self)
        if self._calibration_blocked():
            return
        if not self._tv_controls_available():
            return
        if self._route_volume_command("mute"):
            return
        if not self.last_volume:
            return
        target = not bool(self.last_volume.get("mute"))
        self.run_command(lambda: self.api.set_mute(target), refresh_groups={"volume"})

    def adjust_volume(self, amount):
        cancel_tv_volume_default(self)
        if self._calibration_blocked():
            return
        if not self._tv_controls_available():
            return
        if self._route_volume_command("volume_up" if amount > 0 else "volume_down"):
            return
        if not self.last_volume:
            return

        minimum = int(self.last_volume.get("minVolume", 0))
        maximum = int(self.last_volume.get("maxVolume", 100))
        current = (
            self._desired_volume
            if self._desired_volume is not None
            else int(self.last_volume.get("volume", 0))
        )
        target = max(minimum, min(maximum, current + int(amount)))
        if target == current and self.config["VolumeRateLimit"]["Active"]:
            return

        self._volume_generation += 1
        self._desired_volume = target
        self.volume_value.setText(str(target))

        rate_limit = self.config["VolumeRateLimit"]
        if not rate_limit["Active"]:
            self._pending_volume_commands += 1
            def finished():
                self._pending_volume_commands -= 1
                if self._pending_volume_commands or epoch != self._volume_route_epoch:
                    return
                generation = self._volume_generation
                def volume_is_idle():
                    # A new press may arrive during the device-settling delay.
                    if (generation == self._volume_generation
                            and not self._pending_volume_commands
                            and not self._volume_rate_loop_running):
                        return True
                    return False
                self._request_poll(200, volume_is_idle, groups={"volume"})
            epoch = self._volume_route_epoch
            accepted = self.run_command(
                lambda: None if self._amplifier_routed or epoch != self._volume_route_epoch else self.api.adjust_volume(1 if amount > 0 else -1),
                on_finished=finished,
            )
            if not accepted:
                self._pending_volume_commands -= 1
                self._desired_volume = current if self._pending_volume_commands else None
                self.update_volume_ui(self.last_volume)
            return

        if not self._volume_rate_loop_running:
            self._volume_rate_phase = "read"
            self._volume_rate_due = 0.0
            self._volume_rate_candidate = None
            self._volume_rate_progress = None
            self._volume_rate_loop_running = True
            self._schedule_volume_rate_step(0)

    def _schedule_volume_rate_step(self, delay_ms=None):
        if self._shutting_down or not self._volume_rate_loop_running:
            return
        if delay_ms is None:
            delay_ms = int(self.config["VolumeRateLimit"]["RateMs"])
        self.volume_rate_timer.start(max(0, int(delay_ms)))

    def _volume_rate_step(self):
        if (self._shutting_down or self._use_amplifier()
                or not self._volume_rate_loop_running or self._volume_rate_step_running
                or self._desired_volume is None):
            return
        if self._calibration_blocked() or self.last_power != "active":
            self._volume_rate_loop_running = False
            self._desired_volume = None
            self.volume_rate_timer.stop()
            return
        self._volume_rate_step_running = True
        epoch = self._volume_route_epoch
        generation, target = self._volume_generation, self._desired_volume
        phase = self._volume_rate_phase
        candidate = self._volume_rate_candidate
        due = self._volume_rate_due

        def current_intent():
            return (not self._shutting_down and not self._calibration_blocked()
                    and self.last_power == "active" and not self._use_amplifier()
                    and self._volume_rate_loop_running
                    and epoch == self._volume_route_epoch
                    and generation == self._volume_generation)

        def step():
            # Qt timers may fire slightly early. Enforce silence at the API boundary.
            if time.monotonic() < due:
                return {"deferred": due}
            if not current_intent():
                return {"superseded": True}
            info = self.api.get_volume()
            observed_at = time.monotonic()
            result = {"info": info, "observed_at": observed_at}
            if not current_intent():
                return dict(result, superseded=True)
            current = int(info["volume"])
            if candidate is not None and candidate[0] != generation and current == candidate[1]:
                # A newer target must not infer its direction from the previous
                # command's requested-value echo. Confirm that baseline first.
                return dict(result, phase="verify", candidate=(generation, current),
                            due=observed_at + self.config["VolumeRateLimit"]["VerificationDelayMs"] / 1000)
            if current == target:
                # A target echo is provisional and never advances progress tracking.
                if phase == "verify" and candidate == (generation, target):
                    return dict(result, done=True)
                return dict(result, phase="verify",
                            due=observed_at + self.config["VolumeRateLimit"]["VerificationDelayMs"] / 1000)
            progress = self._volume_rate_progress
            if progress is not None and progress[:2] == (epoch, generation):
                _, _, direction, best, progressed_at = progress
                crossed = direction * (current - target) > 0
                if direction * (current - best) > 0:
                    best, progressed_at = current, observed_at
                if crossed or observed_at - progressed_at >= VOLUME_NO_PROGRESS_SECONDS:
                    return dict(result, done=True)
            else:
                direction = 1 if current < target else -1
                best, progressed_at = current, observed_at
            self._volume_rate_progress = (epoch, generation, direction, best, progressed_at)
            if not current_intent():
                return dict(result, superseded=True)
            self.api.set_volume(target)
            # Quiet time begins after the actual command finishes, not at queueing.
            return dict(result, phase="read", command_sent=True,
                        due=time.monotonic() + self.config["VolumeRateLimit"]["RateMs"] / 1000)

        def schedule(ready_at=0.0):
            self._volume_rate_due = ready_at
            self._schedule_volume_rate_step(max(0, math.ceil((ready_at - time.monotonic()) * 1000)))

        def done(result):
            self._volume_rate_step_running = False
            if epoch != self._volume_route_epoch:
                if self._volume_rate_loop_running:
                    schedule(result.get("due", 0.0) if result.get("command_sent") else 0.0)
                return
            if self._shutting_down or self._use_amplifier() or not self._volume_rate_loop_running:
                return
            if self._calibration_blocked() or self.last_power != "active":
                self._volume_rate_loop_running = False
                self._desired_volume = None
                self.volume_rate_timer.stop()
                return
            if "deferred" in result:
                schedule(result["deferred"])
                return
            accepted_observation = True
            if result.get("info") is not None:
                accepted_observation = self._accept_volume_status(result["info"], result["observed_at"])
                self._status_sequence += 1
            newer_intent = (generation != self._volume_generation or result.get("superseded")
                            or not accepted_observation)
            finished = result.get("done") and not newer_intent
            if finished:
                self._volume_rate_loop_running = False
                self._desired_volume = None
                self._volume_rate_progress = None
                self._volume_rate_phase = "read"
                self._volume_rate_candidate = None
                self._volume_rate_due = 0.0
            elif newer_intent:
                self._volume_rate_phase = "read"
                self._volume_rate_candidate = (generation, target) if result.get("command_sent") else candidate
            else:
                self._volume_rate_phase = result["phase"]
                self._volume_rate_candidate = (result.get("candidate", (generation, target))
                    if result["phase"] == "verify" or result.get("command_sent") else None)
            if self.config["VolumeControls"]:
                self.update_volume_ui(self.last_volume or {})
            if self.calibration_server is not None:
                self.calibration_server.publish()
            if finished:
                self._request_poll(200, groups={"volume"})
            else:
                # A newer press may skip an old verification, but never a command's quiet time.
                ready_at = result.get("due", 0.0) if result.get("command_sent") or not newer_intent else 0.0
                if not accepted_observation:
                    ready_at = max(ready_at, time.monotonic() + self.config["VolumeRateLimit"]["RateMs"] / 1000)
                schedule(ready_at)

        def failed(error):
            self._volume_rate_step_running = False
            if self._shutting_down or not self._volume_rate_loop_running:
                return
            if epoch != self._volume_route_epoch or generation != self._volume_generation:
                if self._volume_rate_loop_running:
                    self._volume_rate_phase = "read"
                    self._volume_rate_candidate = None
                    schedule(time.monotonic() + self.config["VolumeRateLimit"]["RateMs"] / 1000)
                return
            self._volume_rate_loop_running = False
            self._desired_volume = None
            self._volume_rate_progress = None
            self.handle_request_error(error)
            self._request_poll(200, groups={"volume"})

        if not self.run_command(step, on_result=done, on_error=failed):
            self._volume_rate_step_running = False
            self._schedule_volume_rate_step()

    def launch_selected_app(self, index):
        uri = self.app_combo.itemData(index)
        if uri:
            self.launch_app(uri)
            self.app_combo.setCurrentIndex(-1)

    def launch_app(self, uri):
        if self._calibration_blocked() or not self._tv_controls_available():
            return
        self.run_command(lambda: self.api.launch_app(uri),
                         refresh_groups=self.polling.GROUPS, refresh_delay_ms=300)

    def select_input(self, index):
        uri = self.input_combo.itemData(index)
        if uri:
            self.set_input(uri)

    def set_input(self, uri):
        if self._calibration_blocked() or not self._tv_controls_available():
            return
        self.run_command(lambda: self.api.set_input(uri),
                         refresh_groups=self.polling.GROUPS, refresh_delay_ms=300)

    def send_ircc(self, command, *, background=False):
        if self._calibration_blocked() or not self._tv_controls_available():
            return
        accepted = self.run_command(
            lambda: self.api.ircc(command),
            on_error=(lambda error: self.handle_request_error(error, background=True)) if background else None,
        )
        if accepted and self._smtc is not None:
            self._smtc.note_command(command)
        return accepted

    def send_text_input(self):
        if self._calibration_blocked() or not self._tv_controls_available():
            return
        if not hasattr(self, "text_input"):
            return

        text = self.text_input.toPlainText()
        accepted = self.run_command(
            lambda: self.api.set_text_form(text),
            on_error=lambda error: self.handle_request_error(
                error, hint="Make sure a TV software keyboard or text field is currently active."
            ),
        )
        if accepted:
            self._leave_text_input(self.text_input.submission_key)

    def send_text_input_and_confirm(self):
        if self._calibration_blocked() or not self._tv_controls_available():
            return
        if not hasattr(self, "text_input"):
            return

        text = self.text_input.toPlainText()

        def send_and_confirm():
            # When automated submit is disabled, Ctrl+Enter falls back to the
            # same send-only behavior as Enter.
            if not self.config["TextInputSubmit"]:
                self.api.set_text_form(text)
                return

            submit_sequence = self.config["TextInputSubmitSequence"]
            self.api.validate_remote_commands(["Return", "Confirm", *submit_sequence])

            # Reset the keyboard selection before using the configurable
            # submit sequence. If the keyboard is already open, Return closes
            # it while leaving the text field focused.
            if self.api.is_text_input_active():
                self.api.ircc("Return")
                if not self.api.wait_for_text_input_state(False):
                    raise DeviceResponseError("Software keyboard did not close.")

            # Confirm on the still-focused text field reopens the software
            # keyboard. Wait for Sony's status API rather than relying on a
            # fixed UI delay.
            self.api.ircc("Confirm")
            if not self.api.wait_for_text_input_state(True):
                raise DeviceResponseError("Software keyboard did not become active.")

            # setTextForm now doubles as the final readiness check.
            self.api.set_text_form(text)

            for command in submit_sequence:
                self.api.ircc(command)

        accepted = self.run_command(
            send_and_confirm,
            on_error=lambda error: self.handle_request_error(
                error, hint="Make sure a TV software keyboard or text field is currently active."
            ),
        )
        if accepted:
            self._leave_text_input(self.text_input.submission_key)

    def _leave_text_input(self, key=None):
        # Retain the editing key through release after focus moves, preventing
        # held Enter/Escape from becoming a button or TV navigation command.
        if key is not None:
            self._shortcut_presses[key] = "text_edit"
        self.power_button.setFocus(Qt.OtherFocusReason)

    def _dispatch_shortcut(self, command, *, custom=False):
        command = command.removeprefix("ircc:")
        if not custom and command in _PLAYBACK_KEY_COMMANDS and self._playback_key_mode() != "focused":
            return False
        if "remote_codes" not in self._discovery_ready:
            return False
        resolved = self.api.resolve_remote_command(command)
        if resolved not in self.api.remote_codes:
            return False

        self.send_ircc(command)
        return True

    def _install_windows_volume_hook(self):
        if self._native_volume_hook is not None:
            return
        self._native_volume_hook = _WindowsVolumeHook(self._accept_native_volume, self, resolve=self._resolve_native_volume)
        self._native_volume_hook.changed.connect(self._volume_hook_changed)
        try:
            self._native_volume_hook.start()
        except Exception as error:
            QTimer.singleShot(0, lambda error=error: self._volume_hook_changed(False, error))

    def _volume_hook_changed(self, installed, error):
        if not self._shutting_down:
            self._publish_native_volume_state()
        if error is not None and not self._shutting_down:
            record_exception(error, "Windows volume hook")
            self.show_error("Windows volume-key interception is unavailable. Use the remote's volume buttons.")

    def _publish_native_volume_state(self):
        if not hasattr(self, "_native_volume_lock"):
            return
        window = int(self.winId()) if self.isVisible() and not self.isMinimized() else 0
        blocked = (self._shutting_down or self._calibration_blocked()
                   or not self._tv_controls_available()
                   or QApplication.activeModalWidget() is not None
                   or QApplication.activePopupWidget() is not None)
        owner_deadline = self.amplifier_client.state_deadline if self._amplifier_owns_hotkeys() else 0
        owned_codes = (self.amplifier_state.get("global_shortcuts")
                                    if self.amplifier_state else [])
        override_codes = self._focused_tv_override_bindings()
        override_deadline = self._status_observed_at + STATUS_MAX_AGE_SECONDS
        bound_actions = {
            command for code, command in self._shortcut_bindings.items()
            if (code & 0x01FFFFFF) in _NATIVE_QT_VOLUME_KEYS.values()
            and (not command.startswith("ircc:") or ("remote_codes" in self._discovery_ready
                 and command[5:] in self.api.remote_codes))
            and (command != "focus_text_input" or self._focused_shortcut_allowed(command, custom=True))
            and (command in ("volume_up", "volume_down", "mute")
                 or not (hasattr(self, "text_input") and self.text_input.hasFocus()))}
        with self._native_volume_lock:
            self._native_tv_override = (override_deadline, override_codes)
            self._native_owned_codes = owned_codes
            self._native_bound_actions = bound_actions
            self._native_volume_state = (window, blocked, owner_deadline)
            if blocked or not window:
                self._native_volume_queue.clear()
        candidates = self._focused_tv_volume_bindings()
        self.amplifier_client.set_focused_volume(
            window if candidates else 0, candidates, self._amplifier_tv_input(), self._status_observed_at)

    def _resolve_native_volume(self, key, modifiers):
        code = _NATIVE_QT_VOLUME_KEYS[key] | modifiers
        with self._native_volume_lock:
            if time.monotonic() < self._native_volume_state[2]:
                if self._native_owned_codes is None or _binding_owned(self._native_owned_codes, code):
                    deadline, granted = self._native_tv_override
                    if time.monotonic() >= deadline or not _binding_owned(granted, code):
                        return None
        return self._shortcut_bindings.get(code)

    def _accept_native_volume(self, command, foreground):
        # Native-thread callback: plain state and queued signal only; no widget/device/IPC calls.
        with self._native_volume_lock:
            window, blocked, _ = self._native_volume_state
            if (command not in self._native_bound_actions or blocked
                    or not window or foreground != window):
                return False
            if len(self._native_volume_queue) >= 32:
                return True  # Drop an excess step, never leak an owned key to Windows.
            self._native_volume_queue.append(command)
            if not self._native_volume_drain_pending:
                self._native_volume_drain_pending = True
                self._native_volume_ready.emit()
            return True

    def _drain_native_volume(self):
        with self._native_volume_lock:
            commands = list(self._native_volume_queue)
            self._native_volume_queue.clear()
            self._native_volume_drain_pending = False
        if (self._shutting_down or self._calibration_blocked() or not self.isActiveWindow()
                or not self._tv_controls_available()
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None):
            return
        for command in commands:
            # Ownership was decided when this physical event was intercepted.
            self._run_focused_shortcut(command, custom=True)

    def _refresh_shortcut_context(self):
        self._shortcut_refresh_pending = False
        self._refresh_playback_routing()

    def _focused_shortcut_allowed(self, command, *, custom=False):
        if (self._shutting_down or self._calibration_blocked()
                or not self._tv_controls_available()
                or not self.isActiveWindow() or self.isMinimized()
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None):
            return False
        if command == "focus_text_input":
            editor = getattr(self, "text_input", None)
            return bool(editor is not None and editor.isEnabled() and not editor.hasFocus())
        if command in ("volume_up", "volume_down", "mute"):
            return True
        if hasattr(self, "text_input") and self.text_input.hasFocus():
            return False
        return (not (not custom and command in _PLAYBACK_KEY_COMMANDS and self._playback_key_mode() != "focused")
                and "remote_codes" in self._discovery_ready
                and self.api.resolve_remote_command(command.removeprefix("ircc:")) in self.api.remote_codes)

    def _accept_mouse_shortcut(self, command):
        if (command in ("volume_up", "volume_down", "mute")
                and (not self.initialized or self.last_power != "active"
                     or not (self._use_amplifier() or self.last_volume))):
            return False
        if not self._focused_shortcut_allowed(command, custom=True):
            return False
        self._run_focused_shortcut(command, custom=True)
        return True

    def _run_focused_shortcut(self, command, *, custom=False):
        if command == "focus_text_input":
            self.text_input.setFocus(Qt.ShortcutFocusReason)
        elif command == "volume_up":
            self.adjust_volume(1)
        elif command == "volume_down":
            self.adjust_volume(-1)
        elif command == "mute":
            self.toggle_mute()
        else:
            self._dispatch_shortcut(command, custom=custom)

    def _qt_shortcut_command(self, event):
        key = event.key()
        if self._amplifier_owns_hotkeys(_shortcut_code(event), focused=True):
            return None
        if (key in _QT_PLAYBACK_KEYS and self._playback_key_mode() != "focused"):
            return None  # SMTC owns physical transport keys, independent of bindings.
        if (hasattr(self, "text_input") and self.text_input.hasFocus()
                and key not in _NATIVE_QT_VOLUME_KEYS.values()):
            return None
        return _focused_shortcut_command(self._shortcut_bindings, event)

    # ----------------------------
    # Configuration watching and restart
    # ----------------------------

    def _get_config_digest(self):
        return _file_sha256(CONFIG_PATH)

    def _setup_config_watcher(self):
        self.config_watcher = QFileSystemWatcher(self)
        self._watch_config_paths()

        self.config_watcher.fileChanged.connect(self._config_path_changed)
        self.config_watcher.directoryChanged.connect(self._config_path_changed)

        self.config_restart_timer = QTimer(self)
        self.config_restart_timer.setSingleShot(True)
        self.config_restart_timer.setInterval(CONFIG_RESTART_DEBOUNCE_MS)
        self.config_restart_timer.timeout.connect(self._check_external_config_change)

    def _watch_config_paths(self):
        config_path = str(CONFIG_PATH)
        if CONFIG_PATH.exists() and config_path not in self.config_watcher.files():
            self.config_watcher.addPath(config_path)
        mapping_path = str(CONFIG_PATH.with_name("VolumeCalibration.json"))
        if Path(mapping_path).exists() and mapping_path not in self.config_watcher.files():
            self.config_watcher.addPath(mapping_path)
        parent = str(CONFIG_PATH.parent)
        if parent not in self.config_watcher.directories():
            self.config_watcher.addPath(parent)

    def _config_path_changed(self, _path):
        if self._shutting_down or self._config_restart_pending:
            return

        # Many editors save by replacing the file, which removes a direct
        # QFileSystemWatcher file watch. The directory watch lets us re-arm it.
        self._watch_config_paths()
        self.config_restart_timer.start()

    def _check_external_config_change(self):
        if self.calibration_active:
            self.config_restart_timer.start(1000)
            return
        if self._shutting_down or self._config_restart_pending:
            return

        self._reload_volume_mapping()
        content, digest = _file_snapshot(CONFIG_PATH)
        if digest is None or digest == self._config_digest:
            return

        try:
            candidate = json.loads(content.decode("utf-8"))
            validate_config(candidate)
        except (OSError, ValueError) as ex:
            if digest != self._invalid_config_digest:
                self._invalid_config_digest = digest
                QMessageBox.warning(
                    self, "Sony Remote - Configuration Error",
                    f"The configuration change was not applied:\n{ex}\n\n"
                    "Correct the JSON settings and save again. The current instance remains running.",
                )
            return

        self._invalid_config_digest = None
        self._restart_for_config_change()

    def _save_config_without_restart(self):
        # A failed restart or unknown baseline must never authorize a save.
        if self._restart_failed or self._config_digest is None:
            return
        digest = save_config(self.config, expected_digest=self._config_digest)
        if digest is not None:
            # Use our own bytes; a later user edit must remain detectable.
            self._config_digest = digest
        self._watch_config_paths()

    def _restart_for_config_change(self):
        if self.calibration_active:
            self._config_restart_pending = False
            self.config_restart_timer.start(1000)
            return
        if self._shutting_down or self._config_restart_pending:
            return
        self._config_restart_pending = True
        active_timers = self._pause_background_work()
        release_single_instance()
        try:
            launch_replacement(self)
        except Exception as exc:
            try:
                if not acquire_single_instance():
                    raise RuntimeError("Another instance may already be running.")
            except Exception as lock_error:
                QMessageBox.critical(
                    self, "Sony Remote - Restart Error",
                    f"Could not restart: {exc}\nCould not restore the instance lock: "
                    f"{lock_error}\nThis instance will close.",
                )
                self._shutdown()
                QApplication.quit()
                return
            self._config_digest = self._get_config_digest()
            self._restart_failed = True
            self._resume_background_work(active_timers)
            QMessageBox.warning(
                self, "Sony Remote - Restart Error",
                f"Could not restart: {exc}\n\nThe current instance is still running "
                "with its previous settings. Edit and save the JSON again to retry.",
            )
            return
        self._shutdown()
        QApplication.quit()

    # ----------------------------
    # Tray and shutdown lifecycle
    # ----------------------------

    def _cached_state(self, *, full=False):
        now = time.monotonic()
        report = _cached_common(self, "tv", now)
        report["polling_counters"] = self.api.polling_responses.counters()
        if full:
            report["mode"] = "cached_full"
            report["polling_responses"] = self.api.polling_responses.snapshot(now)
            report["privacy"]["warning"] = "Full device responses may contain device/network identifiers and app inventories. Review before sharing."
            report["privacy"]["omitted"] = ["credentials", "request_headers", "submitted_text", "request_parameters", "exception_messages", "configuration_file"]
        state = self.amplifier_state or {}
        routed = self._use_amplifier()
        tv_input = self._amplifier_tv_input()
        if routed:
            reason = "amplifier_ready_on_tv_input"
        elif not tv_input:
            reason = "tv_input_not_configured"
        elif not self.amplifier_client.state_is_fresh:
            reason = "no_fresh_amplifier_state"
        elif not state.get("online"):
            reason = "amplifier_offline"
        elif state.get("power") != "on":
            reason = "amplifier_not_on"
        elif state.get("input") != tv_input:
            reason = "amplifier_on_other_input"
        else:
            reason = "amplifier_audio_state_incomplete"
        volume = self.last_volume or {}
        report["device"] = {"power": self.last_power,
            "standby_pending": self._standby_confirmation_after is not None,
            "status_age_seconds": _state_age(self._status_observed_at, now),
            "volume_age_seconds": _state_age(self._volume_observed_at, now),
            "audio": {key: volume.get(key) for key in ("volume", "mute", "minVolume", "maxVolume")}}
        report["routing"] = {"volume_destination": "amplifier" if routed else "tv", "reason": reason,
            "tv_controls_available": self.initialized and self._tv_controls_available() and bool(routed or self.last_volume),
            "tv_input_configured": bool(tv_input), "amplifier_input_matches_tv": bool(tv_input and state.get("input") == tv_input),
            "calibration_matches": self._matched_volume_mapping() is not None}
        with self._native_volume_lock:
            native_queued = len(self._native_volume_queue)
        extra_commands = [action for action, keys in self.config["Shortcuts"].items()
                          if action not in DEFAULT_SHORTCUTS and keys]
        report["hotkeys"] = {"tv_commands_discovered": "remote_codes" in self._discovery_ready,
            "unavailable_tv_commands": ([action for action in extra_commands if action not in self.api.remote_codes]
                                        if "remote_codes" in self._discovery_ready else None),
            "focused_configured": copy.deepcopy(self.config["Shortcuts"]),
            "native_hook_installed": bool(self._native_volume_hook and self._native_volume_hook.installed),
            "native_queued": native_queued, "amplifier_global_owner": self._amplifier_owns_hotkeys(),
            "focused_tv_override_bindings": list(self._focused_tv_override_bindings()),
            "owned_focused_press_count": sum(bool(value) for value in self._shortcut_presses.values())}
        report["ipc"] = {"connected": self.amplifier_client.socket.state() == QLocalSocket.ConnectedState,
            "socket_state": self.amplifier_client.socket.state().name,
            "peer_state_fresh": self.amplifier_client.state_is_fresh,
            "peer_lease_remaining_seconds": round(max(0, self.amplifier_client.state_deadline-now),3),
            "pending_commands": len(self.amplifier_client.pending),
            "last_error_category": self._amplifier_error_category,
            "peer": {key: state.get(key) for key in ("online", "power", "input", "raw_volume", "raw_max_volume", "mute",
                "global_hotkeys_enabled", "global_hotkeys_active", "global_shortcuts", "calibration_active",
                "focused_volume_override_supported")}}
        smtc = self._smtc
        report["smtc"] = {"configured": self.config["SMTCPlaybackKeys"], "mode": self._playback_key_mode(),
            "initialization_failed": self._smtc_failed, "session_created": bool(smtc and smtc.controls is not None),
            "advertised_actions": sorted(smtc._settings or ()) if smtc else [],
            "eligible_actions": sorted(self._smtc_commands()),
            "optimistic_playback_status": getattr(smtc._status, "name", None) if smtc else None}
        report["volume_commands"] = {"target": self._desired_volume,
            "rate_limit": copy.deepcopy(self.config["VolumeRateLimit"]), "limiter_running": self._volume_rate_loop_running,
            "step_running": self._volume_rate_step_running, "pending_count": self._pending_volume_commands,
            "active_command_workers": len(self._command_workers),
            "rate_timer_active": self.volume_rate_timer.isActive(),
            "phase": self._volume_rate_phase,
            "next_read_in_seconds": round(max(0, self._volume_rate_due - now), 3)}
        report["polling"]["held_groups"] = dict(self.polling._commands)
        return report

    def _setup_activation(self):
        if self.activation_server is not None:
            self.activation_server.deleteLater()
        self.activation_server = ActivationServer(
            "tv", self._activate_existing_window, self, dump=self.state_dumper.request,
        )
        if not self.activation_server.available:
            record_exception(RuntimeError("Local activation endpoint unavailable"), "Window activation")

    def _activate_existing_window(self):
        if self._shutting_down:
            return False
        self.show_from_tray()
        dialog = QApplication.activeModalWidget()
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
        return True

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

    def quit_from_tray(self):
        self._shutdown()
        QApplication.quit()

    def _pause_background_work(self):
        cancel_tv_volume_default(self)
        self._shutting_down = True
        self.discovery.pause()
        self.polling.pause()
        self._shortcut_presses.clear()
        self._popup_shortcut_keys.clear()
        if self._smtc is not None:
            self._smtc.stop()
        if self.activation_server is not None:
            self.activation_server.close()
        if self.calibration_server is not None:
            self.calibration_server.close()
        self._refresh_playback_routing()
        self.amplifier_client.stop()
        self._worker_generation += 1
        timers = (self.poll_timer, self.volume_rate_timer, self.config_restart_timer)
        active_timers = [timer for timer in timers if timer.isActive()]
        for timer in timers:
            timer.stop()
        self.config_watcher.blockSignals(True)
        self.pool.clear()
        self.command_pool.clear()
        self.pool.waitForDone()
        self.command_pool.waitForDone()
        self._workers.clear()
        self._command_workers.clear()
        self._pending_volume_commands = 0
        if self._standby_confirmation_after is not None:
            # Pausing discards worker callbacks. A failed restart must still
            # reconcile a completed power request through a fresh status read.
            self._standby_confirmation_after = time.monotonic()
        self.poll_running = False
        self._input_status_running = False
        self._discovery_pending.clear()
        self._volume_rate_step_running = False
        return active_timers

    def _resume_background_work(self, active_timers):
        self._shutting_down = False
        self.discovery.resume()
        self._setup_activation()
        self._setup_calibration()
        self._setup_smtc()
        self.amplifier_client.start()
        self._config_restart_pending = False
        self.config_watcher.blockSignals(False)
        self._config_path_changed(str(CONFIG_PATH))
        for timer in active_timers:
            timer.start()
        if missing_config_settings(self.config):
            return
        self.set_controls_enabled(
            self.last_power == "active", power_enabled=self.last_power is not None,
        )
        if self._volume_rate_loop_running and self._desired_volume is not None:
            self._schedule_volume_rate_step(0)
        if not self.initialized:
            self.initialize()
        self.polling.resume()

    def _shutdown(self):
        if self._shutdown_complete:
            return
        self._pause_background_work()
        self._shutdown_complete = True
        self._really_close = True
        self._volume_rate_loop_running = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self._native_volume_hook is not None:
            self._native_volume_hook.stop()
        if self.tray:
            self.tray.hide()
        release_single_instance()

    # ----------------------------
    # Qt events
    # ----------------------------

    def showEvent(self, event):
        super().showEvent(event)

        if self.mica_enabled:
            apply_mica(int(self.winId()))

        self._update_polling_state(refresh=True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_polling_state()

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
        if (self.tray and self.config["CloseToTray"] and not self._really_close
                and QSystemTrayIcon.isSystemTrayAvailable()):
            event.ignore()
            self.hide()
            self._update_polling_state()
            return

        self._shutdown()
        event.accept()
        QApplication.quit()

    def mousePressEvent(self, event):
        if hasattr(self, "text_input") and self.text_input.hasFocus():
            self.text_input.clearFocus()
        super().mousePressEvent(event)

    def eventFilter(self, watched, event):
        if hasattr(self, "_mouse_shortcuts") and self._mouse_shortcuts.handle(watched, event):
            return True
        event_type = event.type()
        if hasattr(self, "_shortcut_presses") and event_type in (
                QEvent.Show, QEvent.Hide, QEvent.WindowActivate, QEvent.WindowDeactivate,
                QEvent.FocusIn, QEvent.FocusOut):
            if watched is self and event_type in (QEvent.Hide, QEvent.WindowDeactivate):
                self._shortcut_presses.clear()
                self._popup_shortcut_keys.clear()
            if not self._shortcut_refresh_pending:
                self._shortcut_refresh_pending = True
                QTimer.singleShot(0, self._refresh_shortcut_context)
        if event_type not in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease):
            return super().eventFilter(watched, event)
        if not hasattr(self, "_shortcut_presses"):
            return super().eventFilter(watched, event)

        key = event.key()
        if sys.platform == "win32" and key in (Qt.Key_VolumeUp, Qt.Key_VolumeDown, Qt.Key_VolumeMute):
            # Only the native hook handles focused Windows volume keys. Do not
            # dispatch again through Qt, even if the hook could not be installed.
            return super().eventFilter(watched, event)
        if event_type == QEvent.KeyRelease:
            if event.isAutoRepeat():
                owned = self._shortcut_presses.get(key)
            else:
                self._popup_shortcut_keys.discard(key)
                owned = self._shortcut_presses.pop(key, None)
            if owned:
                event.accept()
                return True
            return super().eventFilter(watched, event)

        if QApplication.activePopupWidget() is not None:
            self._popup_shortcut_keys.add(key)
            return super().eventFilter(watched, event)
        # Enter may be forwarded after closing a popup. It still belongs to
        # the popup until the physical release, including auto-repeat events.
        if key in self._popup_shortcut_keys:
            return super().eventFilter(watched, event)

        command = self._qt_shortcut_command(event)
        custom = key not in _QT_PLAYBACK_KEYS
        previous = self._shortcut_presses.get(key)
        if event_type == QEvent.ShortcutOverride:
            owned = (previous if key in self._shortcut_presses else
                     command if not event.isAutoRepeat() and command is not None
                     and self._focused_shortcut_allowed(command, custom=custom) else None)
            if owned:
                event.accept()
                return True
            return super().eventFilter(watched, event)

        if key in self._shortcut_presses:
            owned = previous
            if (owned and command is not None and owned not in ("mute", "PlayPause", "text_edit")
                    and self._focused_shortcut_allowed(owned, custom=custom)):
                self._run_focused_shortcut(owned, custom=custom)
        else:
            # Never acquire a held key halfway through its press, after focus
            # or amplifier/SMTC ownership changed.
            owned = command if (not event.isAutoRepeat() and command is not None
                                and self._focused_shortcut_allowed(command, custom=custom)) else None
            if command is not None:
                self._shortcut_presses[key] = owned
            if owned:
                self._run_focused_shortcut(owned, custom=custom)
        if owned:
            event.accept()
            return True
        return super().eventFilter(watched, event)


# ----------------------------
# Application entry point
# ----------------------------


def restart_window_arguments(window):
    # One-shot process arguments, never persisted to the user's configuration.
    state = "tray" if not window.isVisible() else "minimized" if window.isMinimized() else "visible"
    return [f"--restart-window-state={state}",
            "--restart-window-geometry=" + bytes(window.saveGeometry()).hex()]


def show_startup_window(window, config):
    state = next((arg.split("=", 1)[1] for arg in sys.argv[1:]
                  if arg.startswith("--restart-window-state=")), None)
    geometry = next((arg.split("=", 1)[1] for arg in sys.argv[1:]
                     if arg.startswith("--restart-window-geometry=")), None)
    if geometry:
        try:
            # Qt also adjusts saved geometry when the original screen is unavailable.
            window.restoreGeometry(QByteArray(bytes.fromhex(geometry)))
        except ValueError:
            pass
    tray_available = (config["SystemTray"] and config["CloseToTray"]
                      and window.tray is not None and QSystemTrayIcon.isSystemTrayAvailable())
    if tray_available and (state == "tray" or
                           (state is None and "--start-in-tray" in sys.argv[1:])):
        window._update_polling_state()
    elif state == "minimized":
        window.showMinimized()
    else:
        window.show()


def launch_replacement(window=None):
    script_path = Path(__file__).resolve()
    arguments = [arg for arg in sys.argv[1:]
                 if not arg.startswith(("--restart-window-state=", "--restart-window-geometry="))]
    if window is not None:
        arguments.extend(restart_window_arguments(window))
    if not getattr(sys, "frozen", False):
        arguments.insert(0, str(script_path))
    result = QProcess.startDetached(sys.executable, arguments, str(script_path.parent))
    started = result[0] if isinstance(result, tuple) else bool(result)
    if not started:
        raise OSError("The replacement process could not be started.")


def check_qt_theme_support():
    if hasattr(QStyleHints, "setColorScheme"):
        return True
    command = 'py' if sys.platform == "win32" else 'python3'
    QMessageBox.critical(
        None, APP_NAME,
        "PySide6 6.8 or newer is required for light/dark appearance.\n\n"
        f'Upgrade with:\n{command} -m pip install --upgrade "PySide6>=6.8"')
    return False


def configure_app_theme(config):
    # Qt 6.8+ forwards this override to supported platform theme backends.
    QApplication.styleHints().setColorScheme(
        Qt.ColorScheme.Dark if config["DarkMode"] else Qt.ColorScheme.Light)


def _run_application():
    app = QApplication(sys.argv)
    if "--dump-state-full" in sys.argv[1:]:
        return dump_existing_state("tv", full=True)
    if "--dump-state" in sys.argv[1:]:
        return dump_existing_state("tv")
    if not check_qt_theme_support():
        return 1
    app.setStyle(TooltipDelayStyle())
    try:
        acquired = acquire_single_instance()
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"Could not acquire the instance lock:\n{exc}")
        return 1

    if not acquired:
        if activate_existing_instance("tv"):
            return 0
        QMessageBox.warning(
            None, APP_NAME,
            "The remote is already running, but its window could not be brought forward. "
            "Check the system tray, or wait for startup/shutdown to finish and try again.",
        )
        return 1

    set_app_user_model_id("Sony.Remote")

    app.setApplicationName(APP_NAME)

    # Use the same native-looking monitor/computer icon for the whole
    # application, including the Windows taskbar.
    app.setWindowIcon(get_app_icon())
    try:
        config = load_config()
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"Could not load SonyRemote.json:\n\n{exc}")
        return 1
    configure_app_theme(config)
    configure_diagnostics(config)
    window = SonyRemote(config)
    show_missing_config_settings(config)
    app.setQuitOnLastWindowClosed(window.tray is None)
    app.aboutToQuit.connect(window._shutdown)
    # Refresh Qt palette roles without changing the explicit JSON light/dark selection.
    app.paletteChanged.connect(lambda palette: window._apply_style())
    window._fit_and_lock_window()
    show_startup_window(window, config)
    try:
        return app.exec()
    finally:
        window._shutdown()


def main():
    try:
        return _run_application()
    except Exception as error:
        record_exception(error, "Application")
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None, "Sony Remote", f"Unexpected application error ({type(error).__name__})."
            )
        return 1
    finally:
        release_single_instance()
        close_diagnostics()


if __name__ == "__main__":
    raise SystemExit(main())
