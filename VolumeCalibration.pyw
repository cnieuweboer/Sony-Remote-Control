"""Standalone silent-source CEC calibration sweep and restoration helper."""
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
    QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QProgressBar, QTextEdit, QVBoxLayout, QWidget,
)

__version__ = "2026.10.18.10"
DATA_PATH = Path(__file__).resolve().with_name("VolumeCalibration.json")
USER_SUFFIX = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
TIMEOUT = 5.0
TV_VOLUME_ATTEMPTS = 3
TV_VOLUME_CONFIRM_SECONDS = 5.0
MAX_VOLUME_STEPS = 512
VOLUME_RESTORE_SECONDS = 900.0
SWEEP_SECONDS = 900.0


class CalibrationError(RuntimeError):
    pass


class Cancelled(CalibrationError):
    pass


def validate_mapping(value):
    """Validate observed pairs; never infer a linear scale from endpoints."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1):
        raise ValueError("Unsupported volume mapping format.")
    for key in ("input", "tv_id", "amplifier_id", "created_at"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"Missing mapping field: {key}.")
    for key in ("raw_min", "raw_max", "api_ceiling"):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError(f"Invalid mapping field: {key}.")
    if not 0 <= value["raw_min"] < value["raw_max"] <= value["api_ceiling"]:
        raise ValueError("Invalid mapped amplifier range.")
    for key in ("tv_min", "tv_max"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError("Invalid mapped TV range.")
    if value["tv_min"] > value["tv_max"]:
        raise ValueError("Invalid mapped TV range.")
    for direction in ("up", "down"):
        rows = value.get(direction)
        if not isinstance(rows, list) or not 2 <= len(rows) <= 513:
            raise ValueError("Invalid mapping observations.")
        previous = None
        for row in rows:
            if not isinstance(row, dict) or type(row.get("tv")) is not int:
                raise ValueError("Invalid observed TV level.")
            raw = row.get("raw")
            if (type(raw) not in (int, float) or not math.isfinite(raw)
                    or not value["raw_min"] <= raw <= value["raw_max"]
                    or not value["tv_min"] <= row["tv"] <= value["tv_max"]):
                raise ValueError("Observed volume is outside the mapped range.")
            if previous is not None:
                if ((raw - previous["raw"]) * (1 if direction == "up" else -1) <= 0
                        or (row["tv"] - previous["tv"]) * (1 if direction == "up" else -1) < 0):
                    raise ValueError("Mapping observations are not monotonic.")
            previous = row
        ends = (rows[0]["raw"], rows[-1]["raw"])
        expected = (value["raw_min"], value["raw_max"]) if direction == "up" else (value["raw_max"], value["raw_min"])
        if ends != expected:
            raise ValueError("Mapping passes do not cover the same endpoints.")
    all_tv = [row["tv"] for direction in ("up", "down") for row in value[direction]]
    if min(all_tv) != value["tv_min"] or max(all_tv) != value["tv_max"]:
        raise ValueError("Mapping TV limits do not match observations.")
    return value


def mapping_report(mapping):
    validate_mapping(mapping)
    up = {row["raw"]: row["tv"] for row in mapping["up"]}
    down = {row["raw"]: row["tv"] for row in mapping["down"]}
    differences = sorted(raw for raw in up.keys() & down.keys() if up[raw] != down[raw])
    unverified = sorted(up.keys() ^ down.keys())
    rows = []
    for tv in sorted(set(up.values()) | set(down.values())):
        rising = sorted(raw for raw, level in up.items() if level == tv)
        falling = sorted(raw for raw, level in down.items() if level == tv)
        status = "Verified" if rising == falling else "Direction differs / incomplete"
        rows.append((tv, rising, falling, status))
    gaps = [(a, b) for a, b in zip([row[0] for row in rows], [row[0] for row in rows][1:]) if b - a > 1]
    return {"rows": rows, "differences": differences, "unverified_raw": unverified,
            "tv_gaps": gaps, "verified": not differences and not unverified}


def mapped_tv_volume(mapping, raw, direction="up"):
    rows = sorted(mapping[direction], key=lambda row: row["raw"])
    raw = max(mapping["raw_min"], min(mapping["raw_max"], raw))
    row = min(rows, key=lambda row: (abs(row["raw"] - raw), row["raw"]))
    return row["tv"], row["raw"] != raw


def load_document(path):
    if not path.exists():
        return {"format_version": 1, "calibration": None, "recovery": None}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise CalibrationError("Calibration JSON exceeds the 2 MiB limit.")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or type(document.get("format_version")) is not int
            or document["format_version"] != 1):
        raise CalibrationError("Unsupported calibration JSON format. Preserve the file for recovery.")
    recovery = document.get("recovery")
    if recovery is not None:
        validate_saved(recovery)
    return document


def validate_saved(saved):
    if not isinstance(saved, dict):
        raise CalibrationError("Invalid recovery record.")
    if "baseline_verified" in saved and type(saved["baseline_verified"]) is not bool:
        raise CalibrationError("Invalid recovery baseline marker.")
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
                 peer_factory=Peer, restore_volume=None, recovery=False, silent_source_confirmed=False, sweep=False, raw_lower=0, raw_upper=None):
        self.cancel, self.progress, self.path = cancel, progress, path
        self.mute_factory, self.peer_factory = mute_factory, peer_factory
        self.restore_volume, self.recovery = restore_volume, recovery
        self.silent_source_confirmed = silent_source_confirmed
        self.sweep = sweep
        self.raw_lower, self.raw_upper = raw_lower, raw_upper
        self.pending_mapping = None
        self.token = uuid.uuid4().hex
        self.peers = {}
        self.next_renew = 0
        self.renewals = []
        self.restoring = False
        self.in_request = False
        self.in_step = False
        self.saved = None
        self.windows = None
        self.document = None

    def pump(self):
        if self.cancel.is_set() and not self.restoring and not self.in_request and not self.in_step:
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

    def guard(self, require_windows_mute=False):
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
        if require_windows_mute and not self.windows.get():
            raise CalibrationError("Windows was unmuted during the test.")
        return a, t

    def wait(self, predicate, description, *, windows_muted=False, settle=True,
             stable_for=0.5, min_elapsed=1.0, timeout=TIMEOUT):
        self.progress(description)
        start = time.monotonic()
        deadline = start + timeout
        stable = 0
        stable_since = start
        last_pair = None
        last_values = None
        while time.monotonic() < deadline:
            self.pump()
            if not all(peer.fresh() for peer in self.peers.values()):
                stable, last_pair, last_values = 0, None, None
                stable_since = time.monotonic()
                continue
            a, t = self.guard(require_windows_mute=windows_muted)
            pair = (a["sample_id"], t["sample_id"])
            values = (a.get("raw_volume"), a.get("mute"), t.get("volume"), t.get("mute"))
            if predicate(a, t):
                if not settle:
                    return a, t
                # Count fresh observations from BOTH devices. Start the quiet
                # period again whenever either device's value changes.
                if values != last_values:
                    stable = 0
                    stable_since = time.monotonic()
                    last_pair, last_values = None, values
                if last_pair is None or (pair[0] != last_pair[0] and pair[1] != last_pair[1]):
                    stable += 1
                    last_pair = pair
                now = time.monotonic()
                if stable >= 3 and now - start >= min_elapsed and now - stable_since >= stable_for:
                    return a, t
            else:
                stable, last_pair, last_values = 0, None, None
                stable_since = time.monotonic()
        detail = ""
        if all(peer.state for peer in self.peers.values()):
            detail = (f" (TV {self.peers['tv'].state.get('volume')}, "
                      f"amplifier raw {self.peers['amplifier'].state.get('raw_volume')})")
        raise CalibrationError(description + " timed out" + detail + ". No uncertain reading was accepted.")

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
                      "amplifier_api_ceiling": a["raw_max_volume"], "baseline_verified": True}
        self.document["recovery"] = saved
        write_document(self.path, self.document)
        # No mute/volume mutations are allowed before the journal is durable.
        self.saved = saved
        self.progress(f"Captured amplifier TV input: {a['input']}. Recovery state saved.")

    def relative_step(self, direction):
        # Cancellation waits for an issued step to be observed and settled;
        # otherwise restoration could mistake its pre-step reading for success.
        self.in_step = True
        try:
            result = self._relative_step(direction)
        finally:
            self.in_step = False
        if self.cancel.is_set() and not self.restoring:
            raise Cancelled("Cancelled. Restoring saved states.")
        return result

    def _relative_step(self, direction):
        """One relative TV command, with retries only for confirmed no movement."""
        if type(direction) is not int or direction not in (-1, 1):
            raise CalibrationError("Volume direction must be +1 or -1.")
        for attempt in range(1, TV_VOLUME_ATTEMPTS + 1):
            a, t = self.guard(require_windows_mute=True)
            raw, tv_volume = a["raw_volume"], t["volume"]
            if not self.number(raw) or not 0 <= raw <= a["raw_max_volume"]:
                raise CalibrationError("Amplifier volume is outside its observable range.")
            if (direction > 0 and raw >= a["raw_max_volume"]) or (direction < 0 and raw <= 0):
                raise CalibrationError("Cannot step beyond the amplifier's observable range.")
            samples = (a["sample_id"], t["sample_id"])
            self.request("tv", "adjust_volume", direction)
            deadline = time.monotonic() + TV_VOLUME_CONFIRM_SECONDS
            fresh = False
            moved = False
            while time.monotonic() < deadline:
                self.pump()
                a, t = self.guard(require_windows_mute=True)
                if a["sample_id"] == samples[0] or t["sample_id"] == samples[1]:
                    continue
                fresh = True
                amp_delta, tv_delta = a["raw_volume"] - raw, t["volume"] - tv_volume
                if amp_delta * direction < 0 or tv_delta * direction < 0:
                    raise CalibrationError("Volume moved opposite to the requested direction.")
                if amp_delta or tv_delta:
                    moved = True
                    break
            if moved:
                # Any movement forbids replay. A finer amplifier step is valid
                # even when the coarser TV number remains unchanged.
                return self.wait(lambda a, t: (a["raw_volume"] - raw) * direction > 0,
                                 "Waiting for the relative amplifier step to settle", windows_muted=True)
            if not fresh:
                raise CalibrationError("No fresh volume observations arrived; relative command was not retried.")
            if self.cancel.is_set() and not self.restoring:
                raise Cancelled("Cancelled. Restoring saved states.")
            if attempt < TV_VOLUME_ATTEMPTS:
                self.progress(f"Neither device moved; retrying relative {direction:+d} ({attempt + 1}/{TV_VOLUME_ATTEMPTS}).")
        raise CalibrationError("Neither TV nor amplifier moved after three relative attempts.")

    def seek_volume(self, *, raw_target=None, tv_target=None, raw_limit=None):
        """Bounded incremental restoration; never oscillate around an unreachable raw target."""
        start = time.monotonic()
        for step in range(MAX_VOLUME_STEPS + 1):
            a, t = self.guard(require_windows_mute=True)
            raw = a["raw_volume"]
            if raw_target is not None:
                reached = raw == raw_target
                direction = 1 if raw < raw_target else -1
            else:
                reached = t["volume"] == tv_target and (raw_limit is None or raw <= raw_limit)
                direction = 1 if t["volume"] < tv_target else -1
            if reached:
                return a, t
            if step == MAX_VOLUME_STEPS or time.monotonic() - start >= VOLUME_RESTORE_SECONDS:
                raise CalibrationError("Incremental volume restoration exceeded its step/time limit.")
            before = raw
            self.relative_step(direction)
            after = self.guard(require_windows_mute=True)[0]["raw_volume"]
            if raw_target is not None and (before - raw_target) * (after - raw_target) < 0:
                raise CalibrationError("The saved raw volume lies between reachable steps; restoration stopped without oscillating.")
        raise CalibrationError("Volume restoration did not complete.")

    def prepare_and_probe(self):
        self.capture()
        self.windows.set(True)
        a, _ = self.guard(require_windows_mute=True)
        self.relative_step(-1 if a["raw_volume"] > 0 else 1)
        self.progress("Relative CEC step confirmed, including changes within one TV display level. No sweep was performed.")

    def repair_legacy_baseline(self):
        self.progress("Rechecking the CEC pairing in the older recovery record using relative steps.")
        target = self.saved["restore_volume"]
        self.seek_volume(tv_target=target)
        a, _ = self.wait(lambda a, t: t["volume"] == target,
                         "Settling the saved TV recovery level", windows_muted=True,
                         stable_for=3.0, min_elapsed=3.0, timeout=15.0)
        paired_raw = a["raw_volume"]
        if not 0 <= paired_raw < min(self.saved["amplifier_api_ceiling"], a["raw_max_volume"]):
            raise CalibrationError("Recovery pairing is outside the observable range.")
        self.relative_step(-1 if paired_raw > 0 else 1)
        self.seek_volume(raw_target=paired_raw)
        self.wait(lambda a, t: t["volume"] == target and a["raw_volume"] == paired_raw,
                  "Verifying the recovered TV/amplifier pair", windows_muted=True,
                  stable_for=3.0, min_elapsed=3.0, timeout=15.0)
        updated = dict(self.saved, amplifier_volume=paired_raw, baseline_verified=True)
        self.document["recovery"] = updated
        write_document(self.path, self.document)
        self.saved = updated

    def sweep_limits(self):
        ceiling = self.peers["amplifier"].state["raw_max_volume"]
        lower = self.raw_lower
        upper = ceiling if self.raw_upper is None else self.raw_upper
        if (not self.number(lower) or not self.number(upper)
                or not 0 <= lower < upper <= ceiling):
            raise CalibrationError("Sweep limits must satisfy 0 <= lower < upper <= amplifier API ceiling.")
        return lower, upper, ceiling

    def calibration_sweep(self):
        lower, upper, ceiling = self.sweep_limits()
        self.progress(f"Moving to lower raw limit {lower}; the source must remain silent throughout the sweep.")
        self.seek_volume(raw_target=lower)
        def observation():
            amp, tv = self.guard(require_windows_mute=True)
            return {"tv": tv["volume"], "raw": amp["raw_volume"]}
        upward = [observation()]
        start = time.monotonic()
        while upward[-1]["raw"] < upper:
            if len(upward) > MAX_VOLUME_STEPS or time.monotonic() - start >= SWEEP_SECONDS:
                raise CalibrationError("Upward sweep exceeded its step/time limit.")
            self.relative_step(1)
            row = observation()
            if row["raw"] > upper:
                raise CalibrationError("A volume step crossed the requested upper limit. Choose a reachable limit.")
            upward.append(row)
            self.progress(f"Upward {len(upward) - 1}: amplifier raw {row['raw']}, TV {row['tv']}.")
            # No more upward commands after the first observed ceiling reading.
        downward = [observation()]
        start = time.monotonic()
        while downward[-1]["raw"] > lower:
            if len(downward) > MAX_VOLUME_STEPS or time.monotonic() - start >= SWEEP_SECONDS:
                raise CalibrationError("Downward sweep exceeded its step/time limit.")
            self.relative_step(-1)
            row = observation()
            if row["raw"] < lower:
                raise CalibrationError("A volume step crossed the requested lower limit. Choose a reachable limit.")
            downward.append(row)
            self.progress(f"Downward {len(downward) - 1}: amplifier raw {row['raw']}, TV {row['tv']}.")
        all_rows = upward + downward
        mapping = {"schema_version": 1,
                   "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "input": self.saved["input"], "tv_id": self.saved["tv_id"],
                   "amplifier_id": self.saved["amplifier_id"], "api_ceiling": ceiling,
                   "raw_min": lower, "raw_max": upward[-1]["raw"],
                   "tv_min": min(row["tv"] for row in all_rows),
                   "tv_max": max(row["tv"] for row in all_rows),
                   "up": upward, "down": downward}
        report = mapping_report(mapping)
        self.progress("Both passes complete. " + ("Observed pairs agree." if report["verified"]
                      else "Directional differences were found; the results will identify unverified observations."))
        if self.cancel.is_set():
            raise Cancelled("Cancelled. Restoring saved states; the previous mapping will be preserved.")
        self.pending_mapping = mapping

    def restore(self):
        self.restoring = True
        self.progress("Restoring saved states; cancellation is paused until cleanup finishes.")
        self.windows.set(True)
        if self.windows.device_id != self.saved["windows_device"]:
            raise CalibrationError("Windows output device changed. Restore the original output before recovery.")
        # Verify identity and input BEFORE issuing any device mutations.
        self.guard()
        if self.recovery and not self.saved.get("baseline_verified", False):
            self.repair_legacy_baseline()
        target = self.saved["restore_volume"]
        if target == self.saved["tv_volume"]:
            self.seek_volume(raw_target=self.saved["amplifier_volume"])
        else:
            self.seek_volume(tv_target=target, raw_limit=self.saved["amplifier_volume"])
        def restored(a, t):
            return (t["volume"] == target and
                    (a["raw_volume"] == self.saved["amplifier_volume"] if target == self.saved["tv_volume"]
                     else a["raw_volume"] <= self.saved["amplifier_volume"]))
        self.wait(restored, "Verifying restored TV and amplifier volume", windows_muted=True)
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
        outcome = "Silent-source CEC check completed; saved states restored."
        failure = None
        try:
            if not self.silent_source_confirmed:
                raise CalibrationError("Confirm that the TV is on a silent input/source and all other playback is stopped before testing or recovery.")
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
            self.wait(lambda a, t: True, "Waiting for settled TV/amplifier status after earlier CEC commands",
                      stable_for=3.0, min_elapsed=3.0, timeout=15.0)
            if self.peers["tv"].state.get("relative_volume") is not True:
                raise CalibrationError("Update Sony Remote to 2026.10.18.9 or newer for relative calibration commands.")
            if self.recovery:
                self.saved = existing
            else:
                if self.sweep:
                    self.sweep_limits()
                self.prepare_and_probe()
                if self.sweep:
                    self.calibration_sweep()
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
                    failure = CalibrationError(f"{str(failure or 'Session ended').rstrip('. ')}. Restoration could not be verified: {str(error).rstrip('. ')}. "
                                               "Windows has been left muted where possible. Keep the TV on a silent source, select the original amplifier input and Windows output, "
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
        if self.pending_mapping is not None and not self.cancel.is_set():
            self.document["calibration"] = self.pending_mapping
            write_document(self.path, self.document)
            return "Sweep completed, saved states restored, and mapping saved."
        return "Saved states recovered and verified." if self.recovery else outcome


class SessionWorker(QThread):
    progress = Signal(str)
    completed = Signal(bool, str)

    def __init__(self, restore_volume=None, recovery=False, parent=None, sweep=False, raw_lower=0, raw_upper=None):
        super().__init__(parent)
        self.cancel = threading.Event()
        self.restore_volume, self.recovery = restore_volume, recovery
        self.sweep, self.raw_lower, self.raw_upper = sweep, raw_lower, raw_upper

    def run(self):
        try:
            message = CalibrationSession(self.cancel, self.progress.emit, restore_volume=self.restore_volume,
                                         recovery=self.recovery, silent_source_confirmed=True, sweep=self.sweep,
                                         raw_lower=self.raw_lower, raw_upper=self.raw_upper).run()
            self.completed.emit(True, message)
        except Exception as error:
            self.completed.emit(False, str(error))


class CalibrationWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self._recovery_notice_shown = False
        self.setWindowTitle(f"Volume matching helper — {__version__}")
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "Start both updated remotes. Set the amplifier to the input connected to the TV, "
            "and start at a comfortable volume below its API ceiling. Select a TV input/source that plays NO sound "
            "and stop all other playback. Windows mute does not silence the TV or other amplifier sources.\n\n"
            "Volume changes can automatically unmute the TV and amplifier. Windows alone is muted. "
            "The full sweep goes from the lower limit to the amplifier API ceiling (or a lower chosen limit) and back, "
            "then restores your saved states. The upper range can be VERY LOUD if the source becomes active. "
            "Do not use physical device controls during the test.")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.silent_source = QCheckBox("I confirm the TV source is silent and all other playback is stopped")
        self.silent_source.toggled.connect(lambda _: self.refresh())
        layout.addWidget(self.silent_source)
        self.safe_restore = QCheckBox("Use a lower safe TV restore volume")
        self.restore_value = QSpinBox()
        self.restore_value.setRange(0, 1000)
        self.restore_value.setEnabled(False)
        self.safe_restore.toggled.connect(self.restore_value.setEnabled)
        layout.addWidget(self.safe_restore)
        layout.addWidget(self.restore_value)
        self.raw_lower = QDoubleSpinBox()
        self.raw_lower.setRange(0, 100000)
        self.raw_lower.setDecimals(3)
        self.raw_lower.setPrefix("Lower raw limit: ")
        self.limit_upper = QCheckBox("Use a lower upper limit instead of the amplifier API ceiling")
        self.raw_upper = QDoubleSpinBox()
        self.raw_upper.setRange(0, 100000)
        self.raw_upper.setDecimals(3)
        self.raw_upper.setEnabled(False)
        self.limit_upper.toggled.connect(self.raw_upper.setEnabled)
        for widget in (self.raw_lower, self.limit_upper, self.raw_upper):
            layout.addWidget(widget)
        location = QLabel(f"Mapping and recovery file: {DATA_PATH}\nKeep this file beside SonyRemote.pyw to use it automatically.")
        location.setWordWrap(True)
        layout.addWidget(location)
        buttons = QHBoxLayout()
        self.start = QPushButton("Run full calibration sweep")
        self.preflight = QPushButton("Run step check only")
        self.preflight.clicked.connect(lambda: self.begin(False, sweep=False))
        self.recover = QPushButton("Recover saved states")
        self.cancel_button = QPushButton("Cancel and restore")
        self.cancel_button.setEnabled(False)
        self.start.clicked.connect(lambda: self.begin(False, sweep=True))
        self.recover.clicked.connect(lambda: self.begin(True))
        self.cancel_button.clicked.connect(self.cancel)
        for button in (self.start, self.preflight, self.recover, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.activity = QProgressBar()
        self.activity.setRange(0, 1)
        self.activity.setValue(0)
        layout.addWidget(self.activity)
        self.result_summary = QLabel("No mapping loaded.")
        self.result_summary.setWordWrap(True)
        layout.addWidget(self.result_summary)
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(["TV level", "Upward raw values", "Downward raw values", "Verification"])
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.results)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        self.resize(900, 850)
        self.refresh()
        self.load_results()

    def refresh(self):
        try:
            pending = bool(load_document(DATA_PATH).get("recovery"))
            self.start.setEnabled(not pending and sys.platform == "win32" and self.silent_source.isChecked())
            self.preflight.setEnabled(self.start.isEnabled())
            self.recover.setEnabled(pending and sys.platform == "win32" and self.silent_source.isChecked())
            if pending and not self._recovery_notice_shown:
                self.log.append("A saved session needs recovery before another test can start.")
            self._recovery_notice_shown = pending
            if sys.platform != "win32":
                self.log.append("Windows is required for this stage's system mute control.")
        except Exception as error:
            self.start.setEnabled(False)
            self.preflight.setEnabled(False)
            self.recover.setEnabled(False)
            self.log.append(str(error))

    def load_results(self):
        try:
            mapping = load_document(DATA_PATH).get("calibration")
            if mapping is None:
                self.results.setRowCount(0)
                self.result_summary.setText("No saved mapping. A cancelled or failed sweep preserves the previous mapping.")
                return
            report = mapping_report(mapping)
            self.results.setRowCount(len(report["rows"]))
            for index, (tv, up, down, status) in enumerate(report["rows"]):
                for column, value in enumerate((tv, ", ".join(map(str, up)), ", ".join(map(str, down)), status)):
                    if column in (1, 2):
                        observations = up if column == 1 else down
                        text = (f"{observations[0]}–{observations[-1]} ({len(observations)} observations)"
                                if observations else "Not observed")
                        item = QTableWidgetItem(text)
                        item.setToolTip(str(value))
                    else:
                        item = QTableWidgetItem(str(value))
                    self.results.setItem(index, column, item)
            self.results.resizeColumnsToContents()
            self.result_summary.setText(
                f"Input {mapping['input']}; raw {mapping['raw_min']}–{mapping['raw_max']}; "
                f"TV {mapping['tv_min']}–{mapping['tv_max']}. "
                f"{'Observed pairs verified' if report['verified'] else 'Direction differences / unverified observations'}; "
                f"{len(report['differences'])} conflicting raw values, {len(report['unverified_raw'])} values seen in one pass, "
                f"{len(report['tv_gaps'])} gaps in observed TV levels. "
                "The reported API ceiling is a control limit, not the physical maximum.")
        except Exception as error:
            self.results.setRowCount(0)
            self.result_summary.setText(f"Cannot display mapping: {error}")

    def begin(self, recovery, sweep=False):
        if not self.silent_source.isChecked() or self.worker is not None:
            return
        self.silent_source.setEnabled(False)
        self.start.setEnabled(False)
        self.preflight.setEnabled(False)
        self.recover.setEnabled(False)
        self.activity.setRange(0, 0)
        for widget in (self.raw_lower, self.limit_upper, self.raw_upper):
            widget.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.safe_restore.setEnabled(False)
        self.restore_value.setEnabled(False)
        volume = self.restore_value.value() if self.safe_restore.isChecked() else None
        self.worker = SessionWorker(volume, recovery, self, sweep=sweep,
                                    raw_lower=self.raw_lower.value(),
                                    raw_upper=self.raw_upper.value() if self.limit_upper.isChecked() else None)
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
        self.activity.setRange(0, 1)
        self.activity.setValue(1)
        self.raw_lower.setEnabled(True)
        self.limit_upper.setEnabled(True)
        self.raw_upper.setEnabled(self.limit_upper.isChecked())
        self.silent_source.setEnabled(True)
        self.safe_restore.setEnabled(True)
        self.restore_value.setEnabled(self.safe_restore.isChecked())
        self.refresh()
        self.load_results()

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
