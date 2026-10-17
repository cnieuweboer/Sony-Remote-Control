import ctypes
from datetime import datetime
import faulthandler
import hashlib
import html
import ipaddress
import json
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
from PySide6.QtCore import QEvent, QFileSystemWatcher, QLockFile, QObject, QProcess, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QGraphicsOpacityEffect, QMainWindow, QMenu, QMessageBox, QPushButton, QStyle, QTextEdit,
    QStyleOptionComboBox, QStylePainter, QStyledItemDelegate,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)


# ----------------------------
# Application constants
# ----------------------------


APP_NAME = "Sony Remote"


__version__ = "2026.10.17.7"


CONFIG_PATH = Path(__file__).with_name("SonyRemote.json")


INSTANCE_MUTEX_NAME = "Local\\SonyRemote.SingleInstance"


NETWORK_TIMEOUT_SECONDS = 2.0


POLL_INTERVAL_MS = 2000


CONFIG_RESTART_DEBOUNCE_MS = 250


POPUP_GUARD_DURATION_MS = 220


POPUP_GUARD_FAVORITE_COUNT = 4


TEXT_INPUT_STATE_TIMEOUT_SECONDS = 1.5


TEXT_INPUT_STATE_POLL_SECONDS = 0.05


# ----------------------------
# Configuration
# ----------------------------


DEFAULT_CONFIG = {
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
    "PlaybackControls": True,
    "VolumeControls": True,
    "VolumeRateLimit": {
        "Active": True,
        "RateMs": 500,
    },
    "DiagnosticsEnabled": False,
    "SortByName": False,
    "Apps": {},
    "Inputs": {},
    "SystemTray": False,
    "CloseToTray": False,
    "DropdownAnimationWorkaround": sys.platform == "win32",
}


BOOLEAN_CONFIG_KEYS = (
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
    "PlaybackControls",
    "VolumeControls",
    "SystemTray",
    "CloseToTray",
    "DropdownAnimationWorkaround",
)


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

        normalized[input_id] = {
            "Name": name.strip(),
            "NameOverride": name_override.strip(),
            "Hidden": hidden,
            "Favorite": favorite,
        }

    return normalized


def _entry_display_name(settings):
    return settings["NameOverride"] or settings["Name"]


def _ordered_entries(entries, sort_by_name):
    items = list(entries.items())
    if sort_by_name:
        items.sort(key=lambda item: _entry_display_name(item[1]).casefold())
    return items


def _default_config_copy():
    return {
        key: value.copy() if isinstance(value, (dict, list)) else value
        for key, value in DEFAULT_CONFIG.items()
    }


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


def validate_config(raw_config, *, allow_incomplete=False):
    if not isinstance(raw_config, dict):
        raise ConfigError("The configuration root must be a JSON object.")
    unknown = set(raw_config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ConfigError("Unknown configuration setting(s): " + ", ".join(sorted(unknown)) + ".")

    config = _default_config_copy()
    config.update(raw_config)
    config["Host"] = _validate_host(config["Host"])
    if not isinstance(config["PreSharedKey"], str):
        raise ConfigError('"PreSharedKey" must be a string.')
    for key in BOOLEAN_CONFIG_KEYS:
        config[key] = _require_bool(config, key, DEFAULT_CONFIG[key])
    if config["CloseToTray"] and not config["SystemTray"]:
        raise ConfigError('"CloseToTray" can only be true when "SystemTray" is also true.')
    for key in ("Apps", "Inputs"):
        config[key] = _validate_entries(config[key], key)

    rate_limit = config["VolumeRateLimit"]
    if not isinstance(rate_limit, dict):
        raise ConfigError('"VolumeRateLimit" must be an object.')
    if set(rate_limit) - {"Active", "RateMs"}:
        raise ConfigError('"VolumeRateLimit" has unknown fields.')
    normalized_rate_limit = DEFAULT_CONFIG["VolumeRateLimit"].copy()
    normalized_rate_limit.update(rate_limit)
    if not isinstance(normalized_rate_limit["Active"], bool):
        raise ConfigError('"VolumeRateLimit.Active" must be true or false.')
    rate_ms = normalized_rate_limit["RateMs"]
    if isinstance(rate_ms, bool) or not isinstance(rate_ms, int) or rate_ms < 1:
        raise ConfigError('"VolumeRateLimit.RateMs" must be a positive integer.')
    config["VolumeRateLimit"] = normalized_rate_limit

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
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            raw_config = json.load(f)
    except json.JSONDecodeError as ex:
        raise ConfigError(
            f"Invalid JSON syntax in {CONFIG_PATH.name}: line {ex.lineno}, "
            f"column {ex.colno}: {ex.msg}"
        ) from ex
    except UnicodeError as ex:
        raise ConfigError(f"{CONFIG_PATH.name} must use UTF-8 encoding.") from ex
    except OSError as ex:
        raise ConfigError(f"Unable to read {CONFIG_PATH.name}: {ex}") from ex
    return validate_config(raw_config, allow_incomplete=True)


def save_config(config):
    temp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, CONFIG_PATH)
    except OSError as ex:
        raise ConfigError(f"Unable to update {CONFIG_PATH.name}: {ex}") from ex
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _file_sha256(path):
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return None


# ----------------------------
# Platform integration
# ----------------------------


_instance_mutex_handle = None


_instance_lock_file = None


if sys.platform == "win32":
    _user32 = ctypes.windll.user32

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104

    VK_BACK = 0x08
    VK_RETURN = 0x0D
    VK_HOME = 0x24
    VK_LEFT = 0x25
    VK_UP = 0x26
    VK_RIGHT = 0x27
    VK_DOWN = 0x28

    VK_NUMPAD0 = 0x60
    VK_NUMPAD1 = 0x61
    VK_NUMPAD2 = 0x62
    VK_NUMPAD3 = 0x63
    VK_NUMPAD4 = 0x64
    VK_NUMPAD5 = 0x65
    VK_NUMPAD6 = 0x66
    VK_NUMPAD7 = 0x67
    VK_NUMPAD8 = 0x68
    VK_NUMPAD9 = 0x69
    VK_ADD = 0x6B
    VK_SUBTRACT = 0x6D

    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_STOP = 0xB2
    VK_MEDIA_PLAY_PAUSE = 0xB3

    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF

    _LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_ulong),
            ("scanCode", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_void_p),
        ]
else:
    _user32 = None
    _LowLevelKeyboardProc = None
    _KBDLLHOOKSTRUCT = None


def acquire_single_instance():
    global _instance_mutex_handle, _instance_lock_file

    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
        last_error = kernel32.GetLastError()
        if not handle:
            raise OSError(last_error, "CreateMutexW failed.")
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _instance_mutex_handle = handle
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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as ex:
            raise DeviceResponseError(f"Sony returned HTTP {ex.code}.") from ex
        except (URLError, TimeoutError, OSError) as ex:
            raise SonyConnectionError("Unable to reach the Sony TV.") from ex

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
        try:
            result = json.loads(self._read_response(request).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as ex:
            raise DeviceResponseError("Sony returned invalid JSON.") from ex
        if not isinstance(result, dict):
            raise DeviceResponseError("Sony response must be a JSON object.")
        if "error" in result:
            error = result["error"]
            code = error[0] if isinstance(error, list) and error and isinstance(error[0], int) else "unknown"
            raise DeviceResponseError(f"Sony rejected {method} (error code {code}).")
        if not isinstance(result.get("result"), list):
            raise DeviceResponseError("Sony response is missing a result list.")
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
# Specialized widgets
# ----------------------------


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


# ----------------------------
# Main window
# ----------------------------


class SonyRemote(QMainWindow):

    # ----------------------------
    # Initialization
    # ----------------------------

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.mica_enabled = bool(config["Mica"] and supports_mica())
        self.api = SonyApi(config["Host"], config["PreSharedKey"], timeout=NETWORK_TIMEOUT_SECONDS)
        self.pool = QThreadPool(self)
        self._workers = set()
        self.poll_running = False
        self.initialized = False
        self.tray = None
        self._really_close = False
        self._shutting_down = False
        self._worker_generation = 0
        self._restart_failed = False
        self._config_digest = self._get_config_digest()
        self._config_restart_pending = False

        self.initialization_running = False
        self.last_power = None
        self.last_volume = None
        self._desired_volume = None
        self._volume_rate_loop_running = False
        self._volume_rate_step_running = False

        self._build_ui()
        self._apply_style()
        self._setup_timers()
        self._setup_config_watcher()
        self._setup_tray()
        self._setup_shortcuts()
        self.set_controls_enabled(False)
        QTimer.singleShot(0, self.initialize)

    def _setup_timers(self):
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self.poll_status)

        self.volume_rate_timer = QTimer(self)
        self.volume_rate_timer.setSingleShot(True)
        self.volume_rate_timer.timeout.connect(self._volume_rate_step)

    def _setup_shortcuts(self):
        # On Windows the native low-level keyboard hook captures shortcuts
        # reliably. Other platforms use Qt's event filter as a fallback.
        if sys.platform != "win32":
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)

        self._keyboard_hook = None
        self._keyboard_hook_proc = None
        if sys.platform == "win32":
            self._install_windows_keyboard_hook()

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
        self._configure_combo_popup_guards()

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

        self.main_layout.addLayout(grid)

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
                if settings["Favorite"]
            ]
            for index, (uri, settings) in enumerate(favorites):
                button = buttons.pop(uri, None)
                if button is None:
                    button = QPushButton()
                    button.setProperty("remote_id", uri)
                    button.clicked.connect(
                        lambda checked=False, value=uri, callback=action: callback(value)
                    )
                button.setText(_entry_display_name(settings))
                row, column = divmod(index, 2)
                layout.addWidget(button, row, column)
            for button in buttons.values():
                button.hide()
                button.deleteLater()

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

    # ----------------------------
    # Background work
    # ----------------------------

    def run_worker(self, function, on_result=None, on_error=None, on_finished=None):
        if self._shutting_down or missing_config_settings(self.config):
            return
        worker = Worker(function)
        self._workers.add(worker)
        generation = self._worker_generation

        def deliver_result(result):
            if not self._shutting_down and generation == self._worker_generation and on_result:
                on_result(result)

        def deliver_error(error):
            if not self._shutting_down and generation == self._worker_generation:
                (on_error or self.handle_request_error)(error)

        def finished():
            self._workers.discard(worker)
            if not self._shutting_down and generation == self._worker_generation and on_finished:
                on_finished()

        worker.signals.result.connect(deliver_result)
        worker.signals.error.connect(deliver_error)
        worker.signals.finished.connect(finished)
        self.pool.start(worker)

    # ----------------------------
    # Discovery and polling
    # ----------------------------

    def initialize(self):
        if self._shutting_down or self.initialization_running:
            return
        if missing_config_settings(self.config):
            self.power_button.setText("Configure device")
            return

        self.initialization_running = True

        def initialize():
            data = {}

            # Only discover IRCC codes when the enabled UI actually needs them.
            if (
                (self.config["TextInputControls"] and self.config["TextInputSubmit"])
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
                    record_exception(exc, "Configuration save")
                    self.show_error(f"Could not update SonyRemote.json:\n\n{exc}")

            self._build_favorites_from_config()
            self._configure_combo_popup_guards()
            self._fit_and_lock_window()
            self.initialized = True
            self.set_controls_enabled(
                False,
                power_enabled=True,
                app_enabled=True,
            )
            self._update_polling_state(refresh=True)

        def failed(error):
            self.initialized = False
            self.handle_request_error(error, background=True)
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

        available = {
            str(entry.get("uri", "")).strip()
            for entry in entries
            if str(entry.get("uri", "")).strip()
            and html.unescape(str(entry.get("title", ""))).strip()
        }
        for uri, settings in _ordered_entries(apps_config, self.config["SortByName"]):
            if uri in available and not settings["Hidden"]:
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
            if uri not in available or settings["Hidden"]:
                continue
            inputs.append((_entry_display_name(settings), uri))
            if str(available[uri].get("status", "")).lower() == "true":
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

    def poll_status(self):
        if (self._shutting_down or missing_config_settings(self.config)
                or not self.isVisible() or self.isMinimized()):
            return
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
                except Exception as error:
                    record_unexpected_exception(error, "Optional input status", background=True)
            return data

        def done(data):
            self.power_button.setToolTip("")
            self.last_power = data.get("power")

            if not self.initialized:
                self.power_button.setText("Connecting...")
                self.set_controls_enabled(False, power_enabled=False)
                QTimer.singleShot(0, self.initialize)
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
                if not self._volume_rate_loop_running:
                    self._desired_volume = None
                    self.update_volume_ui(self.last_volume)
            if "inputs" in data:
                self.update_active_input(data["inputs"])

        def failed(error):
            self.handle_request_error(error, background=True)

        self.run_worker(query, done, failed, lambda: setattr(self, "poll_running", False))

    def _update_polling_state(self, refresh=False):
        if self._shutting_down or not hasattr(self, "poll_timer"):
            return

        should_poll = (
            self.isVisible() and not self.isMinimized()
            and not missing_config_settings(self.config)
        )

        if should_poll:
            if refresh:
                self.poll_status()
            if not self.poll_timer.isActive():
                self.poll_timer.start()
        else:
            self.poll_timer.stop()

    # ----------------------------
    # State and request errors
    # ----------------------------

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

    def _update_channel_button_availability(self, enabled):
        if not hasattr(self, "channel_buttons"):
            return

        remote_codes = getattr(self.api, "remote_codes", {})
        for command, button in self.channel_buttons.items():
            button.setEnabled(bool(enabled and command in remote_codes))

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
        self.last_power = None
        self.last_volume = None
        self._desired_volume = None
        self._volume_rate_loop_running = False
        self.volume_rate_timer.stop()
        self.power_button.setText("Offline")
        self.set_controls_enabled(False, power_enabled=False, app_enabled=False)
        if self.config["VolumeControls"]:
            self.volume_value.setText("—")

    def set_error_status(self):
        self.set_offline()
        self.power_button.setText("Error")

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

    # ----------------------------
    # Device actions and shortcuts
    # ----------------------------

    def toggle_power(self):
        turn_on = self.last_power != "active"
        self.power_button.setEnabled(False)
        self.run_worker(
            lambda: self.api.set_power(turn_on),
            on_finished=lambda: (
                self.power_button.setEnabled(self.last_power is not None),
                QTimer.singleShot(300, self.poll_status),
            ),
        )

    def toggle_mute(self):
        if not self.last_volume:
            return
        target = not bool(self.last_volume.get("mute"))
        self.run_worker(lambda: self.api.set_mute(target), on_finished=lambda: QTimer.singleShot(200, self.poll_status))

    def adjust_volume(self, amount):
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
        if target == current:
            return

        self._desired_volume = target
        self.volume_value.setText(str(target))

        rate_limit = self.config["VolumeRateLimit"]
        if not rate_limit["Active"]:
            self.run_worker(
                lambda: self.api.set_volume(target),
                on_finished=lambda: QTimer.singleShot(200, self.poll_status),
            )
            return

        if not self._volume_rate_loop_running:
            self._volume_rate_loop_running = True
            self._schedule_volume_rate_step(0)

    def _schedule_volume_rate_step(self, delay_ms=None):
        if self._shutting_down or not self._volume_rate_loop_running:
            return
        if delay_ms is None:
            delay_ms = int(self.config["VolumeRateLimit"]["RateMs"])
        self.volume_rate_timer.start(max(0, int(delay_ms)))

    def _volume_rate_step(self):
        if (
            self._shutting_down
            or not self._volume_rate_loop_running
            or self._volume_rate_step_running
            or self._desired_volume is None
        ):
            return

        self._volume_rate_step_running = True

        def step():
            info = self.api.get_volume()
            current = int(info.get("volume", 0))
            target = self._desired_volume
            if target is None or current == target:
                return {"done": True, "info": info}
            direction = 1 if current < target else -1
            self.api.adjust_volume(direction)
            return {"done": False, "info": info}

        def done(result):
            self._volume_rate_step_running = False
            info = result.get("info")
            if info:
                self.last_volume = info
            if result.get("done"):
                self._volume_rate_loop_running = False
                self._desired_volume = None
                if self.last_volume:
                    self.update_volume_ui(self.last_volume)
                QTimer.singleShot(200, self.poll_status)
                return
            self._schedule_volume_rate_step()

        def failed(error):
            self._volume_rate_step_running = False
            self._volume_rate_loop_running = False
            self._desired_volume = None
            self.handle_request_error(error)
            QTimer.singleShot(200, self.poll_status)

        self.run_worker(step, on_result=done, on_error=failed)

    def launch_selected_app(self, index):
        uri = self.app_combo.itemData(index)
        if uri:
            self.launch_app(uri)
            self.app_combo.setCurrentIndex(-1)

    def launch_app(self, uri):
        self.run_worker(lambda: self.api.launch_app(uri), on_finished=lambda: QTimer.singleShot(300, self.poll_status))

    def select_input(self, index):
        uri = self.input_combo.itemData(index)
        if uri:
            self.set_input(uri)

    def set_input(self, uri):
        self.run_worker(lambda: self.api.set_input(uri), on_finished=lambda: QTimer.singleShot(300, self.poll_status))

    def send_ircc(self, command):
        self.run_worker(lambda: self.api.ircc(command))

    def send_text_input(self):
        if not hasattr(self, "text_input"):
            return

        text = self.text_input.toPlainText()
        if not text:
            return

        self.run_worker(
            lambda: self.api.set_text_form(text),
            on_error=lambda error: self.handle_request_error(
                error, hint="Make sure a TV software keyboard or text field is currently active."
            ),
        )

    def send_text_input_and_confirm(self):
        if not hasattr(self, "text_input"):
            return

        text = self.text_input.toPlainText()
        if not text:
            return

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

        self.run_worker(
            send_and_confirm,
            on_error=lambda error: self.handle_request_error(
                error, hint="Make sure a TV software keyboard or text field is currently active."
            ),
        )

    def _dispatch_shortcut(self, command):
        resolved = self.api.resolve_remote_command(command)
        if resolved not in self.api.remote_codes:
            return False

        self.send_ircc(command)
        return True

    def _install_windows_keyboard_hook(self):
        if sys.platform != "win32" or self._keyboard_hook:
            return

        self._keyboard_hook_proc = _LowLevelKeyboardProc(
            self._windows_keyboard_hook
        )
        self._keyboard_hook = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._keyboard_hook_proc,
            None,
            0,
        )
        if not self._keyboard_hook:
            self._keyboard_hook_proc = None
            raise ctypes.WinError()

    def _remove_windows_keyboard_hook(self):
        if sys.platform == "win32" and self._keyboard_hook:
            _user32.UnhookWindowsHookEx(self._keyboard_hook)
            self._keyboard_hook = None
            self._keyboard_hook_proc = None

    def _windows_keyboard_hook(self, n_code, w_param, l_param):
        if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
            data = ctypes.cast(
                l_param,
                ctypes.POINTER(_KBDLLHOOKSTRUCT),
            ).contents

            if _user32.GetForegroundWindow() == int(self.winId()):
                vk = data.vkCode

                # Keep ordinary typing/editing untouched while the text box
                # owns focus. Volume keys remain remote controls, matching the
                # previous Windows behavior.
                text_input_focused = (
                    hasattr(self, "text_input") and self.text_input.hasFocus()
                )

                if vk == VK_VOLUME_UP:
                    self.adjust_volume(1)
                    return 1

                if vk == VK_VOLUME_DOWN:
                    self.adjust_volume(-1)
                    return 1

                if vk == VK_VOLUME_MUTE:
                    self.toggle_mute()
                    return 1

                if not text_input_focused:
                    key_map = {
                        VK_LEFT: "Left",
                        VK_RIGHT: "Right",
                        VK_UP: "Up",
                        VK_DOWN: "Down",
                        VK_RETURN: "Confirm",
                        VK_BACK: "Return",
                        VK_HOME: "Home",
                        VK_NUMPAD0: "Num0",
                        VK_NUMPAD1: "Num1",
                        VK_NUMPAD2: "Num2",
                        VK_NUMPAD3: "Num3",
                        VK_NUMPAD4: "Num4",
                        VK_NUMPAD5: "Num5",
                        VK_NUMPAD6: "Num6",
                        VK_NUMPAD7: "Num7",
                        VK_NUMPAD8: "Num8",
                        VK_NUMPAD9: "Num9",
                        VK_ADD: "ChannelUp",
                        VK_SUBTRACT: "ChannelDown",
                        VK_MEDIA_PLAY_PAUSE: "PlayPause",
                        VK_MEDIA_STOP: "Stop",
                        VK_MEDIA_NEXT_TRACK: "Next",
                        VK_MEDIA_PREV_TRACK: "Prev",
                    }
                    command = key_map.get(vk)
                    if command is not None and self._dispatch_shortcut(command):
                        return 1

        return _user32.CallNextHookEx(
            self._keyboard_hook,
            n_code,
            w_param,
            l_param,
        )

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
        if self._shutting_down or self._config_restart_pending:
            return

        digest = self._get_config_digest()
        if digest is None or digest == self._config_digest:
            return

        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                candidate = json.load(f)
            validate_config(candidate)
        except (OSError, ValueError) as ex:
            if digest != getattr(self, "_invalid_config_digest", None):
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
        # Preserve unapplied user edits after a failed restart.
        if self._restart_failed:
            return
        save_config(self.config)
        self._config_digest = self._get_config_digest()

        # Re-arm the file watch after atomic replacement.
        self._watch_config_paths()

    def _restart_for_config_change(self):
        if self._shutting_down or self._config_restart_pending:
            return
        self._config_restart_pending = True
        active_timers = self._pause_background_work()
        release_single_instance()
        try:
            launch_replacement()
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
        self._shutting_down = True
        self._worker_generation += 1
        timers = (self.poll_timer, self.volume_rate_timer, self.config_restart_timer)
        active_timers = [timer for timer in timers if timer.isActive()]
        for timer in timers:
            timer.stop()
        self.config_watcher.blockSignals(True)
        self.pool.clear()
        self.pool.waitForDone()
        self._workers.clear()
        self.poll_running = False
        self.initialization_running = False
        self._volume_rate_step_running = False
        return active_timers

    def _resume_background_work(self, active_timers):
        self._shutting_down = False
        self._config_restart_pending = False
        self.config_watcher.blockSignals(False)
        self._config_path_changed(str(CONFIG_PATH))
        for timer in active_timers:
            timer.start()
        if missing_config_settings(self.config):
            return
        self.set_controls_enabled(
            self.last_power == "active", power_enabled=self.initialized,
            app_enabled=self.initialized,
        )
        if self._volume_rate_loop_running and self._desired_volume is not None:
            self._schedule_volume_rate_step(0)
        if not self.initialized:
            self.initialize()
        self._update_polling_state(refresh=True)

    def _shutdown(self):
        if getattr(self, "_shutdown_complete", False):
            return
        self._pause_background_work()
        self._shutdown_complete = True
        self._really_close = True
        self._volume_rate_loop_running = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._remove_windows_keyboard_hook()
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

        if self.config["DarkMode"] and sys.platform == "win32":
            set_dark_titlebar(int(self.winId()))
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
        if sys.platform == "win32":
            return super().eventFilter(watched, event)

        if event.type() != QEvent.KeyPress or not self.isActiveWindow():
            return super().eventFilter(watched, event)

        # Keep normal editing behavior while Text Input owns keyboard focus.
        if hasattr(self, "text_input") and self.text_input.hasFocus():
            return super().eventFilter(watched, event)

        key = event.key()
        modifiers = event.modifiers()
        command = None

        if modifiers & Qt.KeypadModifier:
            keypad_map = {
                Qt.Key_0: "Num0",
                Qt.Key_1: "Num1",
                Qt.Key_2: "Num2",
                Qt.Key_3: "Num3",
                Qt.Key_4: "Num4",
                Qt.Key_5: "Num5",
                Qt.Key_6: "Num6",
                Qt.Key_7: "Num7",
                Qt.Key_8: "Num8",
                Qt.Key_9: "Num9",
                Qt.Key_Plus: "ChannelUp",
                Qt.Key_Minus: "ChannelDown",
            }
            command = keypad_map.get(key)

        if command is None:
            key_map = {
                Qt.Key_Left: "Left",
                Qt.Key_Right: "Right",
                Qt.Key_Up: "Up",
                Qt.Key_Down: "Down",
                Qt.Key_Return: "Confirm",
                Qt.Key_Enter: "Confirm",
                Qt.Key_Backspace: "Return",
                Qt.Key_Home: "Home",
                Qt.Key_MediaTogglePlayPause: "PlayPause",
                Qt.Key_MediaPlay: "Play",
                Qt.Key_MediaPause: "Pause",
                Qt.Key_MediaStop: "Stop",
                Qt.Key_MediaNext: "Next",
                Qt.Key_MediaPrevious: "Prev",
            }
            command = key_map.get(key)

        if command is not None and self._dispatch_shortcut(command):
            event.accept()
            return True

        return super().eventFilter(watched, event)


# ----------------------------
# Application entry points
# ----------------------------


def launch_replacement():
    script_path = Path(__file__).resolve()
    arguments = list(sys.argv[1:])
    if not getattr(sys, "frozen", False):
        arguments.insert(0, str(script_path))
    result = QProcess.startDetached(sys.executable, arguments, str(script_path.parent))
    started = result[0] if isinstance(result, tuple) else bool(result)
    if not started:
        raise OSError("The replacement process could not be started.")


def _run_application():
    app = QApplication(sys.argv)
    try:
        if not acquire_single_instance():
            return 0
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"Could not acquire the instance lock:\n{exc}")
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
    configure_diagnostics(config)
    window = SonyRemote(config)
    show_missing_config_settings(config)
    app.setQuitOnLastWindowClosed(window.tray is None)
    app.aboutToQuit.connect(window._shutdown)
    window._fit_and_lock_window()
    window.show()
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
