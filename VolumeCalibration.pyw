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

__version__ = "2026.10.18.16"
DATA_PATH = Path(__file__).resolve().with_name("VolumeCalibration.json")
USER_SUFFIX = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
TIMEOUT = 5.0
TV_VOLUME_ATTEMPTS = 4
CEC_QUIET_SECONDS = 5.0
CEC_RECOVERY_STEPS = 16
CEC_RECOVERY_SECONDS = 180.0
TV_VOLUME_CONFIRM_SECONDS = 5.0
MAX_VOLUME_STEPS = 512
VOLUME_RESTORE_SECONDS = 900.0
SWEEP_SECONDS = 900.0


class CalibrationError(RuntimeError):
    pass


class StepUncertain(CalibrationError):
    """Fresh status could not confirm and settle an issued relative step."""


class Cancelled(CalibrationError):
    pass


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
    estimated = set(estimated)
    return {"input": document["amplifier_tv_input"],
            "tv_id": document["tv_device_id"], "amplifier_id": document["amplifier_device_id"],
            "raw_min": 0, "raw_max": len(levels) - 1, "tv_min": levels[0], "tv_max": levels[-1],
            "lookup": [{"raw": raw, "tv": level, "source": "interpolated" if raw in estimated else "mapped"}
                       for raw, level in enumerate(levels)]}


def runtime_document(mapping):
    """Strip measurement evidence; array index is the amplifier raw value."""
    entries = mapping.get("lookup")
    if (not isinstance(entries, list) or not 2 <= len(entries) <= 10001
            or mapping.get("raw_min") != 0 or mapping.get("raw_max") != len(entries) - 1):
        raise ValueError("Invalid complete amplifier lookup range.")
    for raw, entry in enumerate(entries):
        if (not isinstance(entry, dict) or type(entry.get("raw")) is not int or entry["raw"] != raw
                or entry.get("source") not in ("measured", "boundary-clamped", "interpolated", "mapped")):
            raise ValueError("Lookup must contain exactly one entry for every amplifier raw value.")
    document = {"format_version": 2, "tv_device_id": mapping.get("tv_id"),
                "amplifier_device_id": mapping.get("amplifier_id"), "amplifier_tv_input": mapping.get("input"),
                "tv_volume_by_amplifier_raw_volume": [entry.get("tv") for entry in entries],
                "estimated_amplifier_raw_volumes": [raw for raw, entry in enumerate(entries)
                                                    if entry["source"] == "interpolated"]}
    return document


def validate_sweep_mapping(value):
    """Verify measurement evidence before publishing a completed lookup."""
    if not isinstance(value, dict):
        raise ValueError("Invalid sweep measurements.")
    for key in ("input", "tv_id", "amplifier_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"Invalid mapping field: {key}.")
    for key in ("raw_max", "tv_min", "tv_max", "observed_min_raw", "observed_tv_max", "api_ceiling"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError(f"Invalid integer mapping field: {key}.")
    if (type(value.get("raw_min")) is not int or value["raw_min"] != 0 or not 0 <= value["observed_min_raw"] < value["raw_max"] <= min(value["api_ceiling"], 10000)
            or value["tv_min"] > value["tv_max"]):
        raise ValueError("Invalid canonical mapping bounds.")
    observations = value.get("observations")
    checks = value.get("gap_checks")
    entries = value.get("lookup")
    if not isinstance(observations, list) or not 2 <= len(observations) <= 5000 or not isinstance(checks, dict):
        raise ValueError("Invalid canonical observations/evidence.")
    measured = {}
    for row in observations:
        if (not isinstance(row, dict) or type(row.get("raw")) is not int or type(row.get("tv")) is not int
                or not value["observed_min_raw"] <= row["raw"] <= value["raw_max"]
                or not value["tv_min"] <= row["tv"] <= value["observed_tv_max"]):
            raise ValueError("Invalid canonical observation.")
        measured.setdefault(row["raw"], []).append(row["tv"])
    if min(measured) != value["observed_min_raw"] or max(measured) != value["raw_max"]:
        raise ValueError("Canonical observed bounds do not match measurements.")
    if not isinstance(entries, list) or len(entries) != value["raw_max"] + 1:
        raise ValueError("Canonical lookup must cover every integer raw value.")
    previous = value["tv_min"]
    for raw, entry in enumerate(entries):
        if (not isinstance(entry, dict) or type(entry.get("raw")) is not int or entry["raw"] != raw
                or type(entry.get("tv")) is not int or not previous <= entry["tv"] <= value["tv_max"]):
            raise ValueError("Canonical lookup must be complete and monotonic.")
        previous = entry["tv"]
        source = entry.get("source")
        if source == "measured":
            if raw not in measured or entry["tv"] != min(measured[raw]):
                raise ValueError("Measured entry does not match its evidence.")
        elif source == "boundary-clamped":
            if raw >= value["observed_min_raw"] or entry["tv"] != value["tv_min"]:
                raise ValueError("Invalid lower-boundary clamp.")
        elif source == "interpolated":
            evidence = checks.get(str(raw), [])
            for direction in (-1, 1):
                crossings = [e for e in evidence if isinstance(e, dict) and type(e.get("direction")) is int and e["direction"] == direction
                             and type(e.get("from")) is int and type(e.get("to")) is int
                             and e["from"] in measured and e["to"] in measured
                             and (e["from"] - raw) * (e["to"] - raw) < 0
                             and (e["to"] - e["from"]) * direction > 0]
                if len(crossings) < 2:
                    raise ValueError("Interpolation requires two verified crossings from each direction.")
            if raw in measured or raw < value["observed_min_raw"]:
                raise ValueError("A measured/boundary value cannot be interpolated.")
            anchors = entry.get("anchors", [])
            if (not isinstance(anchors, list) or len(anchors) != 2 or not all(type(a) is int for a in anchors)
                    or not anchors[0] < raw < anchors[1] or any(a not in measured for a in anchors)
                    or not min(measured[anchors[0]]) <= entry["tv"] <= min(measured[anchors[1]])):
                raise ValueError("Invalid interpolation anchors.")
        else:
            raise ValueError("Unknown lookup provenance.")
    if entries[0]["tv"] != value["tv_min"] or entries[-1]["tv"] != value["tv_max"]:
        raise ValueError("Canonical TV limits do not match lookup endpoints.")
    return value


def build_canonical_mapping(observations, checks, saved, ceiling, top, tv_min):
    candidates = {}
    for row in observations:
        candidates.setdefault(row["raw"], set()).add(row["tv"])
    known = sorted(candidates)
    first = known[0]
    # Minimum observed TV number is deterministic, and in particular retains
    # the lowest TV level associated with a saturated API readout.
    measured = {raw: min(levels) for raw, levels in candidates.items()}
    rounding = {"nearest-half-up": lambda x: math.floor(x + 0.5),
                "floor": math.floor, "ceil": math.ceil}
    lookup = []
    for raw in range(top + 1):
        if raw in measured:
            entry = {"raw": raw, "tv": measured[raw], "source": "measured"}
            if len(candidates[raw]) > 1:
                entry["observed_tv_values"] = sorted(candidates[raw])
        elif raw < first:
            entry = {"raw": raw, "tv": tv_min, "source": "boundary-clamped"}
        else:
            lower = max(value for value in known if value < raw)
            upper = min(value for value in known if value > raw)
            # Select rounding from nearby measured transitions using held-out
            # middle observations. Ties use nearest-half-up deterministically.
            triples = list(zip(known, known[1:], known[2:]))
            triples.sort(key=lambda triple: abs(triple[1] - raw))
            scores = {}
            for name, rule in rounding.items():
                errors = []
                for a, b, c in triples[:8]:
                    predicted = measured[a] + (measured[c] - measured[a]) * (b - a) / (c - a)
                    errors.append(abs(rule(predicted) - measured[b]))
                scores[name] = sum(errors) / len(errors) if errors else 0
            selected = min(rounding, key=lambda name: scores[name])
            estimate = measured[lower] + (measured[upper] - measured[lower]) * (raw - lower) / (upper - lower)
            tv = max(measured[lower], min(measured[upper], rounding[selected](estimate)))
            entry = {"raw": raw, "tv": tv, "source": "interpolated",
                     "anchors": [lower, upper], "rounding": selected}
        lookup.append(entry)
    mapping = {"input": saved["input"], "tv_id": saved["tv_id"], "amplifier_id": saved["amplifier_id"],
               "api_ceiling": ceiling, "raw_min": 0, "raw_max": top,
               "observed_min_raw": first, "tv_min": tv_min, "tv_max": measured[top],
               "observed_tv_max": max(row["tv"] for row in observations),
               "observations": observations, "gap_checks": checks, "lookup": lookup}
    validate_sweep_mapping(mapping)
    return mapping


def session_path(path):
    return path.with_name(path.stem + ".session.json")


def read_json(path):
    if path.stat().st_size > 2 * 1024 * 1024:
        raise CalibrationError("Calibration JSON exceeds the 2 MiB limit.")
    return json.loads(path.read_text(encoding="utf-8"))


def load_document(path):
    # Keep the current session/recovery workflow out of the runtime lookup.
    # The next workflow release will remove this temporary sidecar mechanism.
    document = {"calibration": None, "recovery": None}
    if path.exists():
        document["calibration"] = validate_runtime_mapping(read_json(path))
    state_path = session_path(path)
    if state_path.exists():
        state = read_json(state_path)
        if not isinstance(state, dict) or type(state.get("format_version")) is not int or state["format_version"] != 1:
            raise CalibrationError("Unsupported calibration session journal. Preserve it for recovery.")
        for key in ("recovery", "draft"):
            document[key] = state.get(key)
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


def atomic_json(path, document):
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_document(path, document):
    mapping = document.get("calibration")
    compact = runtime_document(mapping) if mapping is not None else None
    if compact is not None:
        validate_runtime_mapping(compact)
    state_path = session_path(path)
    state = {"format_version": 1, "recovery": document.get("recovery"), "draft": document.get("draft")}
    pending = bool(state["recovery"] or state["draft"])
    existing = read_json(path) if path.exists() else None
    if compact is not None and existing != compact:
        atomic_json(path, compact)
    if pending:
        atomic_json(state_path, state)
    else:
        state_path.unlink(missing_ok=True)


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
        self.draft = None
        self.restoration_verified = False
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
             stable_for=0.5, min_elapsed=1.0, timeout=TIMEOUT, timeout_error=CalibrationError):
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
        raise timeout_error(description + " timed out" + detail + ". No uncertain reading was accepted.")

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

    @staticmethod
    def volume_pair(a, t):
        return a["raw_volume"], t["volume"]

    def cec_quiet(self, description, baseline=None, direction=None):
        """Observe a full quiet interval without issuing another volume command."""
        def safe(a, t):
            if baseline is not None and (a["raw_volume"] - baseline[0]) * direction < 0:
                raise CalibrationError(
                    f"Amplifier raw volume moved opposite to requested {direction:+d}: "
                    f"raw {baseline[0]} → {a['raw_volume']}, TV {baseline[1]} → {t['volume']}.")
            # TV display updates can lag or bounce while the amplifier moves
            # correctly. Observe them through the quiet interval; the caller
            # still checks the resulting pair before accepting/retrying.

            return True
        return self.wait(safe, description, windows_muted=True,
                         stable_for=CEC_QUIET_SECONDS, min_elapsed=CEC_QUIET_SECONDS,
                         timeout=20.0)

    def relative_step(self, direction, *, quick=False):
        # Once issued, a step (including rollback) must finish before cancel can
        # start session restoration. A queued CEC command cannot be cancelled.
        self.in_step = True
        try:
            result = self._relative_step(direction, quick=quick)
        finally:
            self.in_step = False
        if self.cancel.is_set() and not self.restoring:
            raise Cancelled("Cancelled. Restoring saved states.")
        return result

    def _relative_step(self, direction, *, quick=False):
        if type(direction) is not int or direction not in (-1, 1):
            raise CalibrationError("Volume direction must be +1 or -1.")
        baseline = self.volume_pair(*self.guard(require_windows_mute=True))
        for attempt in range(TV_VOLUME_ATTEMPTS):
            try:
                return self.command_step(direction, baseline, quick=quick)
            except StepUncertain as error:
                self.progress(f"Step was inconclusive: {error}")
                a, t = self.cec_quiet("Pausing commands for five quiet seconds to observe delayed CEC responses",
                                      baseline, direction)
                raw_delta = (a["raw_volume"] - baseline[0]) * direction
                tv_delta = (t["volume"] - baseline[1]) * direction
                # Only an unambiguous late single raw step is accepted here.
                # Larger late changes are rolled back even if one TV display
                # level could represent them; they may include queued commands.
                if raw_delta == 1 and tv_delta in (0, 1):
                    self.progress("Delayed single amplifier step settled; no retry was sent.")
                    return a, t
                self.restore_step_baseline(baseline)
                if self.cancel.is_set() and not self.restoring:
                    raise Cancelled("Cancelled after restoring the previous accepted volume pair.")
                if attempt + 1 < TV_VOLUME_ATTEMPTS:
                    self.progress(f"Previous accepted pair verified; retrying {direction:+d} ({attempt + 1}/3 retries).")
        raise CalibrationError("Relative CEC step remained inconclusive after the initial attempt and three retries; previous accepted pair restored.")

    def command_step(self, direction, baseline, *, quick=False):
        """Issue ONE command; errors/disconnection are never retried here."""
        a, t = self.guard(require_windows_mute=True)
        raw, tv_volume = baseline
        current = a["raw_volume"]
        if not self.number(current) or not 0 <= current <= a["raw_max_volume"]:
            raise CalibrationError("Amplifier volume is outside its observable range.")
        if (direction > 0 and current >= a["raw_max_volume"]) or (direction < 0 and current <= 0):
            raise CalibrationError("Cannot step beyond the amplifier's observable range.")
        samples = (a["sample_id"], t["sample_id"])
        self.request("tv", "adjust_volume", direction)
        deadline = time.monotonic() + TV_VOLUME_CONFIRM_SECONDS
        fresh = False
        candidate = None
        def confirmed(a, t):
            nonlocal candidate
            amp_delta, tv_delta = a["raw_volume"] - raw, t["volume"] - tv_volume
            if amp_delta * direction < 0:
                raise CalibrationError(
                    f"Amplifier raw volume moved opposite to requested {direction:+d}: "
                    f"raw {raw} → {a['raw_volume']}, TV {tv_volume} → {t['volume']}.")
            if amp_delta * direction <= 0:
                return False
            if candidate is None:
                candidate = a["raw_volume"]
            elif a["raw_volume"] != candidate:
                raise StepUncertain("Amplifier continued beyond the first observed step before settling.")
            # Quick positioning records no mapping pairs. In verified steps a
            # contrary TV reading must correct itself and settle, rather than
            # instantly aborting or being accepted as measurement data.
            return quick or tv_delta * direction >= 0
        while time.monotonic() < deadline:
            self.pump()
            a, t = self.guard(require_windows_mute=True)
            if a["sample_id"] == samples[0] or t["sample_id"] == samples[1]:
                continue
            fresh = True
            if confirmed(a, t):
                if quick:
                    # Positioning only: wait for movement, not measurement
                    # settling. One observed command at a time avoids a burst.
                    return a, t
                return self.wait(confirmed, "Waiting for the relative amplifier step to settle",
                                 windows_muted=True, timeout_error=StepUncertain)
        if not fresh:
            raise CalibrationError("No fresh volume observations arrived; relative command was not retried.")
        raise StepUncertain("No confirmed amplifier step arrived within five seconds.")

    def restore_step_baseline(self, baseline):
        """Bounded rollback, never recursive and never a calibration measurement."""
        self.progress(f"Restoring previous accepted pair: amplifier {baseline[0]}, TV {baseline[1]} (not measurement data).")
        started = time.monotonic()
        stalled = 0
        for count in range(CEC_RECOVERY_STEPS + 1):
            a, t = self.guard(require_windows_mute=True)
            current = a["raw_volume"]
            if current == baseline[0]:
                # If already unchanged, the preceding quiet interval is enough.
                if self.volume_pair(a, t) == baseline:
                    return a, t
                return self.wait(lambda a, t: self.volume_pair(a, t) == baseline,
                                 "Verifying the previous accepted TV/amplifier pair before retry",
                                 windows_muted=True, stable_for=CEC_QUIET_SECONDS,
                                 min_elapsed=CEC_QUIET_SECONDS, timeout=20.0)
            if count == CEC_RECOVERY_STEPS or time.monotonic() - started >= CEC_RECOVERY_SECONDS:
                raise CalibrationError("Previous accepted pair could not be restored within the rollback budget.")
            direction = 1 if current < baseline[0] else -1
            pair = self.volume_pair(a, t)
            try:
                self.command_step(direction, pair)
            except StepUncertain:
                pass  # Observe first; never issue an immediate replacement.
            a, t = self.cec_quiet("Waiting five quiet seconds during previous-pair restoration", pair, direction)
            stalled = stalled + 1 if a["raw_volume"] == current else 0
            if stalled >= TV_VOLUME_ATTEMPTS:
                raise CalibrationError("Previous-pair restoration made no progress after four verified attempts.")
            if (current - baseline[0]) * (a["raw_volume"] - baseline[0]) < 0:
                raise CalibrationError("Previous accepted raw volume lies between reachable steps; rollback stopped.")
        raise CalibrationError("Previous accepted pair could not be restored.")

    def position_at_tv_minimum(self, minimum):
        self.progress("Positioning at the TV minimum; full step verification starts within five TV levels of minimum.")
        start = time.monotonic()
        was_quick = False
        for count in range(MAX_VOLUME_STEPS + 1):
            a, t = self.guard(require_windows_mute=True)
            if was_quick and t["volume"] <= minimum + 5:
                self.cec_quiet("Resynchronizing TV/amplifier readings after fast positioning",
                               self.volume_pair(a, t), -1)
                a, t = self.guard(require_windows_mute=True)
                was_quick = False
            if t["volume"] == minimum or a["raw_volume"] == 0:
                # A delayed TV number may still be above minimum when raw
                # volume has reached its lower boundary. Send nothing further.
                return self.wait(lambda a, t: t["volume"] == minimum,
                                 "Verifying the settled TV-minimum baseline", windows_muted=True,
                                 stable_for=3.0, min_elapsed=3.0, timeout=15.0)
            if count == MAX_VOLUME_STEPS or time.monotonic() - start >= VOLUME_RESTORE_SECONDS:
                raise CalibrationError("TV-minimum positioning exceeded its step/time limit.")
            was_quick = t["volume"] > minimum + 5
            self.relative_step(-1, quick=was_quick)

    def seek_volume(self, *, raw_target=None, tv_target=None, raw_limit=None, fast_tv_target=None):
        """Bounded incremental restoration; verify the final approach and never oscillate."""
        start = time.monotonic()
        was_quick = False
        previous_direction = None
        def approach(a, t):
            raw = a["raw_volume"]
            if raw_target is not None:
                reached = raw == raw_target
                direction = 1 if raw < raw_target else -1
            else:
                reached = t["volume"] == tv_target and (raw_limit is None or raw <= raw_limit)
                direction = 1 if t["volume"] < tv_target else -1
            quick = fast_tv_target is not None and abs(t["volume"] - fast_tv_target) > 5
            if raw_target is not None:
                # A coarse/stale TV readout must not keep fast mode active
                # when the exact saved raw target is already close.
                quick = quick and abs(raw - raw_target) > 5
            if raw_limit is not None and direction > 0:
                quick = quick and raw_limit - raw > 5
            return reached, direction, quick
        for step in range(MAX_VOLUME_STEPS + 1):
            a, t = self.guard(require_windows_mute=True)
            reached, direction, quick = approach(a, t)
            if was_quick and (not quick or reached or direction != previous_direction):
                self.cec_quiet("Resynchronizing TV/amplifier readings before the final restoration approach",
                               self.volume_pair(a, t), previous_direction)
                a, t = self.guard(require_windows_mute=True)
                reached, direction, quick = approach(a, t)
                was_quick = False
            if reached:
                return a, t
            if step == MAX_VOLUME_STEPS or time.monotonic() - start >= VOLUME_RESTORE_SECONDS:
                raise CalibrationError("Incremental volume restoration exceeded its step/time limit.")
            if raw_limit is not None and direction > 0 and a["raw_volume"] >= raw_limit:
                # Wait for a delayed TV readout rather than raising the
                # amplifier above the saved safe restore limit.
                return self.wait(lambda a, t: t["volume"] == tv_target and a["raw_volume"] <= raw_limit,
                                 "Waiting for the safe restore TV level to catch up", windows_muted=True)
            before = a["raw_volume"]
            self.relative_step(direction, quick=quick)
            was_quick, previous_direction = quick, direction
            after = self.guard(require_windows_mute=True)[0]["raw_volume"]
            if raw_target is not None and (before - raw_target) * (after - raw_target) < 0:
                raise CalibrationError("The saved raw volume lies between reachable steps; restoration stopped without oscillating.")
        raise CalibrationError("Volume restoration did not complete.")

    def prepare_and_probe(self):
        self.capture()
        self.windows.set(True)
        a, _ = self.guard(require_windows_mute=True)
        self.relative_step(-1 if a["raw_volume"] > 0 else 1)
        self.progress("Relative CEC step confirmed, including changes within one TV display level."
                      + ("" if self.sweep else " No sweep was performed."))

    def sweep_limits(self):
        ceiling = self.peers["amplifier"].state["raw_max_volume"]
        upper = ceiling if self.raw_upper is None else self.raw_upper
        if (not self.number(ceiling) or not float(ceiling).is_integer()
                or not self.number(upper) or not float(upper).is_integer()
                or not 0 < upper <= min(ceiling, 10000) or self.raw_lower != 0):
            raise CalibrationError("Canonical calibration needs integer raw values, an upper limit <= min(API ceiling, 10000), and automatic TV-minimum positioning.")
        return 0, int(upper), int(ceiling)

    def calibration_sweep(self):
        _, upper, ceiling = self.sweep_limits()
        observations = []
        checks = {}
        minimum = self.peers["tv"].state["minimum"]
        self.draft = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "input": self.saved["input"], "tv_id": self.saved["tv_id"],
                      "amplifier_id": self.saved["amplifier_id"], "api_ceiling": ceiling,
                      "upper_limit": upper, "tv_min": minimum, "status": "in-progress",
                      "observations": observations, "gap_checks": checks}
        self.document["draft"] = self.draft
        write_document(self.path, self.document)
        def observe(phase, direction=0):
            a, t = self.guard(require_windows_mute=True)
            raw = a["raw_volume"]
            if not self.number(raw) or not float(raw).is_integer() or not 0 <= raw <= upper:
                raise CalibrationError("A step left the requested integer raw range.")
            row = {"raw": int(raw), "tv": t["volume"], "phase": phase, "direction": direction}
            observations.append(row)
            write_document(self.path, self.document)
            return row
        self.progress("Moving the TV to its reported minimum using relative CEC steps.")
        self.position_at_tv_minimum(minimum)
        first = observe("up")
        if first["raw"] >= upper:
            raise CalibrationError("There is no usable raw range above the TV minimum.")
        upward = [first]
        start = time.monotonic()
        while upward[-1]["raw"] < upper and upward[-1]["tv"] < self.peers["tv"].state["maximum"]:
            if len(upward) > MAX_VOLUME_STEPS or time.monotonic() - start >= SWEEP_SECONDS:
                raise CalibrationError("Upward sweep exceeded its step/time limit.")
            self.relative_step(1)
            row = observe("up", 1)
            upward.append(row)
            self.progress(f"Upward: amplifier {row['raw']} → TV {row['tv']}.")
        top = upward[-1]["raw"]
        self.progress("Upward measurement pass complete; checking only missing raw values, then restoring saved states.")
        # Cross each missing integer twice from BOTH directions. A target found
        # during positioning is measured too; any command error is inconclusive,
        # never evidence that the raw value is unreachable.
        gap_start = time.monotonic()
        gap_steps = 0
        def step(direction):
            nonlocal gap_steps
            if gap_steps >= 2048 or time.monotonic() - gap_start >= 1800:
                raise CalibrationError("Gap verification exceeded its 2048-step/30-minute budget.")
            current = self.guard(require_windows_mute=True)[0]["raw_volume"]
            if (direction > 0 and current >= top) or (direction < 0 and current <= 0):
                raise CalibrationError("Cannot verify a gap without leaving the observed range.")
            self.relative_step(direction)
            gap_steps += 1
            return observe("gap", direction)
        def measured(raw):
            return any(row["raw"] == raw for row in observations)
        while True:
            missing = [raw for raw in range(min(row["raw"] for row in observations) + 1, top)
                       if not measured(raw) and str(raw) not in checks]
            if not missing:
                break
            target = missing[0]
            self.progress(f"Verifying skipped amplifier value {target} from both directions.")
            evidence = []
            for direction in (1, -1, 1, -1):
                current = self.guard(require_windows_mute=True)[0]["raw_volume"]
                while (current >= target if direction > 0 else current <= target):
                    row = step(-direction)
                    current = row["raw"]
                    if measured(target):
                        break
                if measured(target):
                    break
                while (current < target if direction > 0 else current > target):
                    before = int(current)
                    current = step(direction)["raw"]
                    if current == target:
                        break
                    if (before - target) * (current - target) < 0:
                        evidence.append({"direction": direction, "from": before, "to": current})
                if measured(target):
                    break
            checks[str(target)] = evidence
            write_document(self.path, self.document)
        mapping = build_canonical_mapping(observations, checks, self.saved, ceiling, top, minimum)
        counts = {source: sum(entry["source"] == source for entry in mapping["lookup"])
                  for source in ("measured", "boundary-clamped", "interpolated")}
        self.progress("Canonical lookup complete: " + ", ".join(f"{count} {source}" for source, count in counts.items()))
        if self.cancel.is_set():
            raise Cancelled("Cancelled. Restoring saved states; the previous mapping will be preserved.")
        self.pending_mapping = mapping
        self.draft["status"] = "awaiting-restoration"
        self.draft["mapping"] = mapping
        write_document(self.path, self.document)

    def restore(self):
        self.restoring = True
        self.progress("Restoring saved states; cancellation is paused until cleanup finishes.")
        self.windows.set(True)
        if self.windows.device_id != self.saved["windows_device"]:
            raise CalibrationError("Windows output device changed. Restore the original output before recovery.")
        # Verify identity and input BEFORE issuing any device mutations.
        self.guard()
        self.cec_quiet("Waiting for pending CEC activity to settle before session restoration")
        target = self.saved["restore_volume"]
        if target == self.saved["tv_volume"]:
            self.seek_volume(raw_target=self.saved["amplifier_volume"], fast_tv_target=target)
        else:
            self.seek_volume(tv_target=target, raw_limit=self.saved["amplifier_volume"], fast_tv_target=target)
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
        write_document(self.path, self.document)
        self.restoration_verified = True
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
        if self.draft is not None:
            self.draft["status"] = ("cancelled" if isinstance(failure, Cancelled) or self.cancel.is_set()
                                    else "failed" if failure is not None else "awaiting-activation")
            self.draft["restoration_verified"] = self.restoration_verified
            if failure is not None:
                self.draft["error"] = str(failure)
            try:
                write_document(self.path, self.document)
            except Exception as error:
                failure = CalibrationError(f"{failure or 'Session finished'}. Could not update inactive draft: {error}")
        if failure is not None:
            raise failure
        if self.pending_mapping is not None and not self.cancel.is_set():
            self.document["calibration"] = self.pending_mapping
            self.document.pop("draft", None)
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
            "The sweep starts at the TV minimum, goes to the API ceiling (or a lower chosen limit) and back, "
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
        self.raw_lower.setPrefix("Automatic TV-minimum baseline: ")
        self.raw_lower.setVisible(False)
        self.limit_upper = QCheckBox("Use a lower upper limit instead of the amplifier API ceiling")
        self.raw_upper = QDoubleSpinBox()
        self.raw_upper.setRange(0, 10000)
        self.raw_upper.setDecimals(0)
        self.raw_upper.setEnabled(False)
        self.limit_upper.toggled.connect(self.raw_upper.setEnabled)
        for widget in (self.limit_upper, self.raw_upper):
            layout.addWidget(widget)
        location = QLabel(f"Runtime mapping: {DATA_PATH}\nTemporary session journal: {session_path(DATA_PATH)}\nKeep the runtime mapping beside SonyRemote.pyw.")
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
            document = load_document(DATA_PATH)
            mapping = document.get("calibration")
            draft = document.get("draft")
            draft_note = ""
            if isinstance(draft, dict):
                rows = draft.get("observations", [])
                draft_note = (f" Inactive draft: {len(rows)} observations; {draft.get('status', 'unfinished')}. "
                              "It is not used by Sony; start a new sweep after any required recovery.")
            if mapping is None:
                self.results.setHorizontalHeaderLabels(["Amplifier raw", "TV level", "Phase", "Status"])
                self.results.setRowCount(len(rows) if draft_note else 0)
                if draft_note:
                    for index, row in enumerate(rows):
                        for column, value in enumerate((row['raw'], row['tv'], row['phase'], "Inactive draft")):
                            self.results.setItem(index, column, QTableWidgetItem(str(value)))
                    self.results.resizeColumnsToContents()
                self.result_summary.setText("No active saved mapping." + draft_note)
                return
            entries = mapping["lookup"]
            estimated = sum(entry["source"] == "interpolated" for entry in entries)
            self.results.setHorizontalHeaderLabels(["Amplifier raw", "TV level", "Origin", "Status"])
            self.results.setRowCount(len(entries))
            for index, entry in enumerate(entries):
                values = (entry["raw"], entry["tv"], "Estimated" if entry["source"] == "interpolated" else "Mapped", "Completed lookup")
                for column, value in enumerate(values):
                    self.results.setItem(index, column, QTableWidgetItem(str(value)))
            self.results.resizeColumnsToContents()
            self.result_summary.setText(
                f"Input {mapping['input']}; amplifier raw 0–{mapping['raw_max']} → TV {mapping['tv_min']}–{mapping['tv_max']}. "
                f"{len(entries)} mapped values, {estimated} estimates. Measurement evidence is not stored in the runtime file."
                + draft_note)
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
