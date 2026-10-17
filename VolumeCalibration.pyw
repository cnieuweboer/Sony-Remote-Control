"""Standalone muted CEC preflight and restoration helper. Mapping is stage two."""
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from PySide6.QtCore import QThread, Signal
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

__version__ = "2026.10.18.5"
DATA_PATH = Path(__file__).resolve().with_name("VolumeCalibration.json")
USER_SUFFIX = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
TIMEOUT = 5.0


class CalibrationError(RuntimeError):
    pass


class Cancelled(CalibrationError):
    pass


def load_document(path):
    if not path.exists():
        return {"format_version": 1, "calibration": None, "recovery": None}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("format_version") != 1:
        raise CalibrationError("Unsupported calibration JSON format. Preserve the file for recovery.")
    recovery = document.get("recovery")
    if recovery is not None:
        validate_saved(recovery)
    return document


def validate_saved(saved):
    if not isinstance(saved, dict):
        raise CalibrationError("Invalid recovery record.")
    for key in ("input", "amplifier_id", "tv_id", "windows_device"):
        if not isinstance(saved.get(key), str) or not saved[key]:
            raise CalibrationError(f"Invalid recovery field: {key}.")
    for key in ("windows_mute", "tv_mute", "amplifier_mute"):
        if type(saved.get(key)) is not bool:
            raise CalibrationError(f"Invalid recovery field: {key}.")
    for key in ("tv_volume", "restore_volume"):
        if type(saved.get(key)) is not int or saved[key] < 0:
            raise CalibrationError(f"Invalid recovery field: {key}.")
    for key in ("amplifier_volume", "amplifier_api_ceiling"):
        value = saved.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise CalibrationError(f"Invalid recovery field: {key}.")
    if (saved["restore_volume"] > saved["tv_volume"]
            or saved["amplifier_volume"] >= saved["amplifier_api_ceiling"]
            or saved["tv_mute"] != saved["amplifier_mute"]):
        raise CalibrationError("Inconsistent recovery record.")


def write_document(path, document):
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class WindowsMute:
    """Core Audio COM, on the helper worker thread; no third-party dependency."""

    def __init__(self):
        if sys.platform != "win32":
            raise CalibrationError("This stage requires Windows Core Audio mute control.")
        self.ole = ctypes.OleDLL("ole32")
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole.CoInitializeEx.restype = ctypes.c_int32
        self.ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.ole.CoCreateInstance.restype = ctypes.c_int32
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoUninitialize.argtypes = []
        self.enumerator = ctypes.c_void_p()
        self.device = ctypes.c_void_p()
        self.endpoint = ctypes.c_void_p()
        self.initialized = False
        try:
            self._check(self.ole.CoInitializeEx(None, 2))
            self.initialized = True
            clsid = self._guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
            iid = self._guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
            self._check(self.ole.CoCreateInstance(ctypes.byref(clsid), None, 23,
                                                 ctypes.byref(iid), ctypes.byref(self.enumerator)))
            self._check(self._call(self.enumerator, 4, [ctypes.c_int, ctypes.c_int,
                         ctypes.POINTER(ctypes.c_void_p)], 0, 0, ctypes.byref(self.device)))
            pointer = ctypes.c_void_p()
            self._check(self._call(self.device, 5, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(pointer)))
            try:
                self.device_id = ctypes.wstring_at(pointer)
            finally:
                self.ole.CoTaskMemFree(pointer)
            iid = self._guid("5CDF2C82-841E-4546-9722-0CF74078229A")
            self._check(self._call(self.device, 3, [ctypes.c_void_p, ctypes.c_uint32,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                         ctypes.byref(iid), 23, None, ctypes.byref(self.endpoint)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _guid(value):
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)

    @staticmethod
    def _check(result):
        if result < 0:
            raise CalibrationError(f"Windows audio operation failed (HRESULT 0x{result & 0xffffffff:08x}).")

    @staticmethod
    def _call(pointer, index, arguments, *values):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *arguments)(table[index])
        return function(pointer, *values)

    def get(self):
        value = ctypes.c_int()
        self._check(self._call(self.endpoint, 15, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(value)))
        return bool(value.value)

    def set(self, value):
        self._check(self._call(self.endpoint, 14, [ctypes.c_int, ctypes.c_void_p], int(value), None))
        if self.get() != value:
            raise CalibrationError("Windows mute did not reach the requested state.")

    def close(self):
        for name in ("endpoint", "device", "enumerator"):
            pointer = getattr(self, name, None)
            if pointer:
                # IUnknown::Release returns ULONG rather than HRESULT.
                self._call(pointer, 2, [])
                setattr(self, name, ctypes.c_void_p())
        if self.initialized:
            self.ole.CoUninitialize()
            self.initialized = False


class Peer:
    def __init__(self, role):
        self.role = role
        self.socket = QLocalSocket()
        self.state = None
        self.state_at = 0
        self.buffer = bytearray()
        self.results = {}
        self.sequence = 0
        self.acquired = False
        self.socket.setReadBufferSize(4097)

    def connect(self):
        self.socket.connectToServer(f"remote-control.{self.role}.calibration.v1.{USER_SUFFIX}")
        if not self.socket.waitForConnected(2000):
            raise CalibrationError(f"Start an updated {self.role} remote before running calibration.")

    def send(self, token, action, args=None):
        if self.socket.state() != QLocalSocket.ConnectedState:
            raise CalibrationError(f"The {self.role} connection was lost.")
        self.sequence += 1
        message = {"version": 1, "type": "calibration", "id": self.sequence,
                   "token": token, "action": action, "args": args or {}}
        payload = (json.dumps(message, allow_nan=False) + "\n").encode()
        if len(payload) > 4096 or self.socket.write(payload) != len(payload):
            raise CalibrationError(f"Could not send the {self.role} request.")
        self.socket.flush()
        return self.sequence

    def read(self):
        while self.socket.bytesAvailable():
            self.buffer.extend(bytes(self.socket.read(4097)))
            while b"\n" in self.buffer:
                frame, _, tail = self.buffer.partition(b"\n")
                self.buffer[:] = tail
                if len(frame) > 4096:
                    raise CalibrationError("Oversized calibration response.")
                message = json.loads(frame)
                if not isinstance(message, dict) or type(message.get("version")) is not int or message["version"] != 1:
                    raise CalibrationError("Unsupported calibration response.")
                if message.get("type") == "state":
                    if (message.get("role") != self.role or type(message.get("sample_id")) is not int
                            or type(message.get("valid_for_ms")) is not int
                            or not 0 <= message["valid_for_ms"] <= 6000):
                        raise CalibrationError("Invalid device state.")
                    self.state, self.state_at = message, time.monotonic()
                elif message.get("type") == "result":
                    if type(message.get("id")) is not int or type(message.get("ok")) is not bool:
                        raise CalibrationError("Invalid calibration result.")
                    self.results[message["id"]] = message
                    if len(self.results) > 32:
                        raise CalibrationError("Too many unanswered calibration results.")
                else:
                    raise CalibrationError("Unexpected calibration response.")
            if len(self.buffer) > 4096:
                raise CalibrationError("Oversized calibration response.")

    def fresh(self):
        return bool(self.state and time.monotonic() - self.state_at < self.state["valid_for_ms"] / 1000)


class CalibrationSession:
    def __init__(self, cancel, progress, path=DATA_PATH, mute_factory=WindowsMute,
                 peer_factory=Peer, restore_volume=None, recovery=False):
        self.cancel, self.progress, self.path = cancel, progress, path
        self.mute_factory, self.peer_factory = mute_factory, peer_factory
        self.restore_volume, self.recovery = restore_volume, recovery
        self.token = uuid.uuid4().hex
        self.peers = {}
        self.next_renew = 0
        self.renewals = []
        self.restoring = False
        self.in_request = False
        self.saved = None
        self.windows = None
        self.document = None

    def pump(self):
        if self.cancel.is_set() and not self.restoring and not self.in_request:
            raise Cancelled("Cancelled. Restoring saved states.")
        now = time.monotonic()
        if now >= self.next_renew:
            self.next_renew = now + 2
            for peer in self.peers.values():
                if peer.acquired:
                    self.renewals.append((peer, peer.send(self.token, "renew"), now))
        for peer in self.peers.values():
            peer.socket.waitForReadyRead(40)
            peer.read()
            if peer.socket.state() != QLocalSocket.ConnectedState:
                raise CalibrationError(f"The {peer.role} remote disconnected.")
        pending = []
        for peer, request_id, sent_at in self.renewals:
            result = peer.results.pop(request_id, None)
            if result is not None:
                if not result["ok"]:
                    raise CalibrationError(f"The {peer.role} calibration lease was lost.")
            elif now - sent_at > 4:
                raise CalibrationError(f"The {peer.role} calibration lease stopped responding.")
            else:
                pending.append((peer, request_id, sent_at))
        self.renewals = pending

    def request(self, role, action, value=None):
        peer = self.peers[role]
        request_id = peer.send(self.token, action, {} if value is None else {"value": value})
        deadline = time.monotonic() + 8
        # Finish a bounded, already-issued mutation before honouring cancel;
        # restoration must not race a still-running device request.
        self.in_request = True
        try:
            while request_id not in peer.results:
                self.pump()
                if time.monotonic() >= deadline:
                    raise CalibrationError(f"The {role} {action} operation timed out; it was not retried.")
        finally:
            self.in_request = False
        result = peer.results.pop(request_id)
        if not result["ok"]:
            raise CalibrationError(result.get("error") or f"The {role} request failed.")
        if action == "acquire":
            peer.acquired = True
        elif action == "release":
            peer.acquired = False

    def guard(self, require_mute=False):
        amp, tv = (self.peers[role] for role in ("amplifier", "tv"))
        if not amp.fresh() or not tv.fresh():
            raise CalibrationError("Device state became stale.")
        a, t = amp.state, tv.state
        if not a.get("online") or a.get("power") != "on" or not t.get("online") or t.get("power") != "active":
            raise CalibrationError("Both devices must remain online and powered on.")
        if not a.get("calibration_active") or not t.get("calibration_active"):
            raise CalibrationError("Exclusive calibration ownership was lost.")
        if self.saved:
            if (a.get("input") != self.saved["input"]
                    or a.get("device_id") != self.saved["amplifier_id"]
                    or t.get("device_id") != self.saved["tv_id"]):
                raise CalibrationError("The amplifier input or configured device changed. Restore the original pairing before recovery.")
        if require_mute and (a.get("mute") is not True or t.get("mute") is not True):
            raise CalibrationError("Mute was lost during the muted test.")
        if require_mute and not self.windows.get():
            raise CalibrationError("Windows was unmuted during the test.")
        return a, t

    def wait(self, predicate, description, *, muted=False, settle=True):
        self.progress(description)
        start = time.monotonic()
        deadline = start + TIMEOUT
        stable = 0
        last_pair = None
        last_values = None
        while time.monotonic() < deadline:
            self.pump()
            if not all(peer.fresh() for peer in self.peers.values()):
                continue
            a, t = self.guard(require_mute=muted)
            pair = (a["sample_id"], t["sample_id"])
            values = (a.get("raw_volume"), a.get("mute"), t.get("volume"), t.get("mute"))
            if predicate(a, t):
                if not settle:
                    return a, t
                if time.monotonic() - start >= 1 and pair != last_pair:
                    # Require new observations from BOTH devices, not repeated
                    # socket broadcasts or repeated reads of the same snapshot.
                    if last_pair is None or (pair[0] != last_pair[0] and pair[1] != last_pair[1]):
                        stable = stable + 1 if values == last_values else 1
                        last_pair, last_values = pair, values
                    if stable >= 3:
                        return a, t
            else:
                stable, last_pair, last_values = 0, None, None
        raise CalibrationError(description + " timed out. No uncertain reading was accepted.")

    @staticmethod
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def capture(self):
        a, t = self.guard()
        if (not isinstance(a.get("input"), str) or not a["input"]
                or not self.number(a.get("raw_volume")) or not self.number(a.get("raw_max_volume"))
                or not 0 <= a["raw_volume"] < a["raw_max_volume"]
                or any(type(t.get(key)) is not int for key in ("volume", "minimum", "maximum"))
                or not t["minimum"] <= t["volume"] <= t["maximum"]
                or type(a.get("mute")) is not bool or type(t.get("mute")) is not bool):
            raise CalibrationError("Start below the amplifier API ceiling, with valid TV volume limits and mute states.")
        if a["mute"] != t["mute"]:
            raise CalibrationError("TV and amplifier mute states differ. Align them before starting.")
        restore = t["volume"] if self.restore_volume is None else self.restore_volume
        if type(restore) is not int or not t["minimum"] <= restore <= t["volume"]:
            raise CalibrationError("The safe restore volume must lie between the TV minimum and starting volume.")
        saved = {"input": a["input"], "amplifier_id": a["device_id"], "tv_id": t["device_id"],
                      "windows_device": self.windows.device_id, "windows_mute": self.windows.get(),
                      "tv_mute": t["mute"], "tv_volume": t["volume"], "restore_volume": restore,
                      "amplifier_mute": a["mute"], "amplifier_volume": a["raw_volume"],
                      "amplifier_api_ceiling": a["raw_max_volume"]}
        self.document["recovery"] = saved
        write_document(self.path, self.document)
        # No mute/volume mutations are allowed before the journal is durable.
        self.saved = saved
        self.progress(f"Captured amplifier TV input: {a['input']}. Recovery state saved.")

    def prepare_and_probe(self):
        self.capture()
        self.windows.set(True)
        self.request("tv", "set_mute", True)
        self.wait(lambda a, t: a["mute"] is True and t["mute"] is True,
                  "Verifying TV and amplifier mute")
        if not self.windows.get():
            raise CalibrationError("Windows mute could not be verified.")
        a, t = self.guard(require_mute=True)
        target = t["volume"] - 1 if t["volume"] > t["minimum"] else t["volume"] + 1
        if target > t["maximum"]:
            raise CalibrationError("The TV exposes no usable volume step.")
        original_raw = a["raw_volume"]
        self.request("tv", "set_volume", target)
        self.wait(lambda a, t: t["volume"] == target and a["raw_volume"] != original_raw,
                  "Verifying a single muted TV/CEC volume step", muted=True)
        self.progress("Muted CEC check passed. No calibration sweep was performed.")

    def restore(self):
        self.restoring = True
        self.progress("Restoring saved states; cancellation is paused until cleanup finishes.")
        self.windows.set(True)
        if self.windows.device_id != self.saved["windows_device"]:
            raise CalibrationError("Windows output device changed. Restore the original output before recovery.")
        # Verify identity and input BEFORE issuing any device mutations.
        self.guard()
        self.request("tv", "set_mute", True)
        self.wait(lambda a, t: a["mute"] is True and t["mute"] is True,
                  "Verifying mute before volume restoration")
        before_a, before_t = self.guard(require_mute=True)
        before_raw, before_tv = before_a["raw_volume"], before_t["volume"]
        target = self.saved["restore_volume"]
        self.request("tv", "set_volume", target)
        def restored(a, t):
            if t["volume"] != target:
                return False
            if target == self.saved["tv_volume"]:
                return a["raw_volume"] == self.saved["amplifier_volume"]
            return (a["raw_volume"] <= self.saved["amplifier_volume"]
                    and (target == before_tv or a["raw_volume"] != before_raw))
        self.wait(restored, "Verifying restored TV and amplifier volume", muted=True)
        self.request("tv", "set_mute", self.saved["tv_mute"])
        self.wait(lambda a, t: a["mute"] == self.saved["amplifier_mute"] and t["mute"] == self.saved["tv_mute"],
                  "Verifying restored TV and amplifier mute")
        self.windows.set(self.saved["windows_mute"])
        self.document["recovery"] = None
        self.document["last_session"] = {"completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                         "input": self.saved["input"], "tv_id": self.saved["tv_id"],
                                         "amplifier_id": self.saved["amplifier_id"], "restoration_verified": True}
        write_document(self.path, self.document)
        self.progress("Volume and mute restoration verified. Windows mute restored last.")

    def run(self):
        outcome = "Muted CEC check completed; original states restored."
        failure = None
        try:
            self.document = load_document(self.path)
            existing = self.document.get("recovery")
            if existing and not self.recovery:
                raise CalibrationError("A previous session needs recovery. Use Recover saved states first.")
            if self.recovery and not existing:
                raise CalibrationError("There is no saved session to recover.")
            self.windows = self.mute_factory()
            for role in ("amplifier", "tv"):
                peer = self.peer_factory(role)
                self.peers[role] = peer
                peer.connect()
                self.request(role, "acquire")
            self.wait(lambda a, t: True, "Waiting for fresh, exclusive device status", settle=False)
            if self.recovery:
                self.saved = existing
            else:
                self.prepare_and_probe()
        except Exception as error:
            failure = error
        finally:
            if self.saved is not None and self.windows is not None:
                try:
                    self.restore()
                except Exception as error:
                    # Keep the durable journal. Never restore Windows unmute
                    # after an uncertain device restoration.
                    try:
                        self.windows.set(True)
                    except Exception:
                        pass
                    failure = CalibrationError(f"{failure or 'Session ended'}. Restoration could not be verified: {error}. "
                                               "Windows has been left muted where possible. Keep devices muted, select the original input/output, "
                                               "start both remotes, and use Recover saved states.")
            self.restoring = True
            for role, peer in list(self.peers.items()):
                try:
                    if peer.acquired:
                        self.request(role, "release")
                except Exception:
                    pass  # The disconnected lease also expires automatically.
            for peer in self.peers.values():
                peer.socket.abort()
            if self.windows is not None:
                self.windows.close()
        if failure is not None:
            raise failure
        return "Saved states recovered and verified." if self.recovery else outcome


class SessionWorker(QThread):
    progress = Signal(str)
    completed = Signal(bool, str)

    def __init__(self, restore_volume=None, recovery=False, parent=None):
        super().__init__(parent)
        self.cancel = threading.Event()
        self.restore_volume, self.recovery = restore_volume, recovery

    def run(self):
        try:
            message = CalibrationSession(self.cancel, self.progress.emit, restore_volume=self.restore_volume,
                                         recovery=self.recovery).run()
            self.completed.emit(True, message)
        except Exception as error:
            self.completed.emit(False, str(error))


class CalibrationWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.setWindowTitle(f"Volume matching helper — {__version__}")
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Start both updated remotes. Set the amplifier to the input connected to the TV, "
            "and start at a comfortable volume below its API ceiling. Stop other audio playback.\n\n"
            "This first stage mutes Windows and the TV, verifies amplifier mute, tests one CEC volume step, "
            "then restores and verifies the saved states. It does not create a volume mapping yet. "
            "Do not use physical device controls during the test.")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.safe_restore = QCheckBox("Use a lower safe TV restore volume")
        self.restore_value = QSpinBox()
        self.restore_value.setRange(0, 1000)
        self.restore_value.setEnabled(False)
        self.safe_restore.toggled.connect(self.restore_value.setEnabled)
        layout.addWidget(self.safe_restore)
        layout.addWidget(self.restore_value)
        buttons = QHBoxLayout()
        self.start = QPushButton("Run muted CEC check")
        self.recover = QPushButton("Recover saved states")
        self.cancel_button = QPushButton("Cancel and restore")
        self.cancel_button.setEnabled(False)
        self.start.clicked.connect(lambda: self.begin(False))
        self.recover.clicked.connect(lambda: self.begin(True))
        self.cancel_button.clicked.connect(self.cancel)
        for button in (self.start, self.recover, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        self.resize(650, 460)
        self.refresh()

    def refresh(self):
        try:
            pending = bool(load_document(DATA_PATH).get("recovery"))
            self.start.setEnabled(not pending and sys.platform == "win32")
            self.recover.setEnabled(pending and sys.platform == "win32")
            if pending:
                self.log.append("A saved session needs recovery before another test can start.")
            if sys.platform != "win32":
                self.log.append("Windows is required for this stage's system mute control.")
        except Exception as error:
            self.start.setEnabled(False)
            self.recover.setEnabled(False)
            self.log.append(str(error))

    def begin(self, recovery):
        self.start.setEnabled(False)
        self.recover.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.safe_restore.setEnabled(False)
        self.restore_value.setEnabled(False)
        volume = self.restore_value.value() if self.safe_restore.isChecked() else None
        self.worker = SessionWorker(volume, recovery, self)
        self.worker.progress.connect(self.log.append)
        self.worker.completed.connect(self.completed)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def cancel(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.log.append("Cancellation requested; waiting for restoration.")

    def completed(self, ok, message):
        self.log.append(message)
        if not ok:
            QMessageBox.warning(self, "Volume matching helper", message)

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.cancel_button.setEnabled(False)
        self.safe_restore.setEnabled(True)
        self.restore_value.setEnabled(self.safe_restore.isChecked())
        self.refresh()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.cancel()
            self.log.append("Close again after restoration completes.")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    # A second helper may inspect recovery state, but only one can own it or
    # alter devices. Lock the journal as well as relying on remote leases.
    from PySide6.QtCore import QLockFile
    lock = QLockFile(str(DATA_PATH) + ".lock")
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.warning(None, "Volume matching helper", "Another helper is already using this calibration JSON.")
        return 1
    window = CalibrationWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
