"""Deterministic calibration tests: no devices, sockets, audio APIs, or real waits."""
from collections import Counter
import copy
import heapq
import json
from pathlib import Path
import runpy
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / "VolumeCalibration.pyw"), run_name="calibration_tests")
REMOTE = runpy.run_path(str(ROOT / "SonyRemote.pyw"), run_name="display_tests")
SESSION = HELPER["CalibrationSession"]
GLOBALS = SESSION.run.__globals__


class Clock:
    def __init__(self):
        self.now = 0.0
        self.events = []
        self.sequence = 0

    def monotonic(self):
        return self.now

    def later(self, delay, callback):
        self.sequence += 1
        heapq.heappush(self.events, (self.now + delay, self.sequence, callback))

    def advance(self, seconds):
        target = self.now + seconds
        while self.events and self.events[0][0] <= target:
            self.now, _, callback = heapq.heappop(self.events)
            callback()
        self.now = target


class Devices:
    """CEC moves raw amplifier steps; the TV display has a coarser scale."""
    def __init__(self, clock, *, muted=False, windows_muted=False):
        self.clock = clock
        self.raw = 5
        self.ceiling = 8
        self.tv = self.raw // 2
        self.tv_max = 100
        self.tv_lag = 0.0
        self.input = "tv"
        self.speakers = (True, False)
        self.speaker_outputs = ["a", "b"]
        self.isolation_rejected = False
        self.speaker_restore_rejected = False
        self.muted = muted
        self.windows_muted = windows_muted
        self.original = (self.raw, self.tv, muted, windows_muted)
        self.restoring = False
        self.trace = []
        self.peers = {}
        self.logs = []
        self.behavior = None
        self.reachable = list(range(9))
        self.moves = 0
        self.cleanup_time = None
        self.on_progress = None
        self.renew_rejected = False
        self.drop_ack = None

    def progress(self, text):
        self.logs.append(text)
        if self.on_progress:
            self.on_progress(text)

    def cleanup(self):
        self.restoring = True
        self.cleanup_time = self.clock.monotonic()
        self.trace.append((self.cleanup_time, "cleanup"))

    def move(self, direction):
        self.moves += 1
        self.trace.append((self.clock.monotonic(), "volume_command", direction, self.raw, self.restoring))
        if self.behavior and self.behavior(self, direction):
            return
        self.schedule_move(direction, 0.2)

    def schedule_move(self, direction, delay):
        def apply():
            candidates = [value for value in self.reachable if (value - self.raw) * direction > 0]
            if candidates:
                self.raw = min(candidates) if direction > 0 else max(candidates)
            tv = self.raw // 2
            if self.tv_lag:
                self.clock.later(self.tv_lag, lambda: setattr(self, 'tv', tv))
            else:
                self.tv = tv
            # Common CEC behavior: volume changes unmute the audio device.
            self.muted = False
            self.trace.append((self.clock.monotonic(), "movement", self.raw, self.tv))
        self.clock.later(delay, apply)

    def snapshot(self, role, sample):
        peer = self.peers[role]
        common = {"online": True, "calibration_active": peer.acquired,
                  "sample_id": sample, "mute": self.muted, "device_id": role + "-id"}
        if role == "amplifier":
            common.update(power="on", input=self.input, raw_volume=self.raw, raw_max_volume=self.ceiling,
                          speaker_outputs=self.speaker_outputs, speaker_state=list(self.speakers),
                          speaker_valid_for_ms=6000)
        else:
            common.update(power="active", volume=self.tv, minimum=0, maximum=self.tv_max, relative_volume=True)
        return common

    def peer(self, role):
        peer = FakePeer(self, role)
        self.peers[role] = peer
        return peer

    def mute(self):
        return FakeMute(self)


class FakeMute:
    def __init__(self, devices):
        self.devices = devices
        self.closed = False

    def get(self):
        return self.devices.windows_muted

    def set(self, value):
        self.devices.windows_muted = value
        self.devices.trace.append((self.devices.clock.monotonic(), "windows_mute", value))

    def close(self):
        self.closed = True


class FakeSocket:
    def __init__(self, peer):
        self.peer = peer
        self.connected = False
        self.aborted = False

    def waitForReadyRead(self, milliseconds):
        self.peer.devices.clock.advance(milliseconds / 1000)
        return True

    def state(self):
        socket = HELPER["QLocalSocket"]
        return socket.ConnectedState if self.connected else socket.UnconnectedState

    def abort(self):
        self.connected = False
        self.aborted = True
        self.peer.acquired = False


class FakePeer:
    def __init__(self, devices, role):
        self.devices, self.role = devices, role
        self.socket = FakeSocket(self)
        self.acquired = False
        self.results = {}
        self.state = None
        self.state_at = 0
        self.sequence = 0
        self.samples = 0
        self.next_sample = 0
        self.frozen = False

    def connect(self):
        self.socket.connected = True

    def send(self, token, action, args=None):
        if not self.socket.connected:
            raise HELPER["CalibrationError"]("Fake device disconnected.")
        self.sequence += 1
        self.devices.trace.append((self.devices.clock.monotonic(), self.role, action, args))
        ok = True
        if action == "acquire":
            self.acquired = True
        elif action == "renew":
            ok = not self.devices.renew_rejected
        elif action == "adjust_volume":
            assert self.role == "tv" and args["value"] in (-1, 1)
            self.devices.move(args["value"])
        elif action == "set_mute":
            self.devices.muted = args["value"]
        elif action == "set_speakers":
            target = tuple(args['value'])
            ok = not (self.devices.isolation_rejected and target == (False, False))
            ok = ok and not (self.devices.speaker_restore_rejected and self.devices.restoring)
            if ok:
                def apply():
                    self.devices.speakers = target
                    self.devices.trace.append((self.devices.clock.monotonic(), 'speakers', target))
                self.devices.clock.later(0.1, apply)
        # Acquisition flags are updated by the real session's request method.
        if not self.devices.drop_ack or not self.devices.drop_ack(self.role, action):
            self.results[self.sequence] = {"ok": ok, "error": "Fake lease rejected" if not ok else ""}
        return self.sequence

    def read(self):
        now = self.devices.clock.monotonic()
        if not self.frozen and now >= self.next_sample:
            self.samples += 1
            self.state = self.devices.snapshot(self.role, self.samples)
            self.state_at = now
            self.next_sample = now + 0.25

    def fresh(self):
        return self.state is not None and self.devices.clock.monotonic() - self.state_at < 6


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "VolumeCalibration.json"
        self.previous = {"format_version": 2, "tv_device_id": "old-tv", "amplifier_device_id": "old-amp",
                         "amplifier_tv_input": "old-input", "tv_volume_by_amplifier_raw_volume": [0, 1]}
        self.previous_bytes = (json.dumps(self.previous, separators=(",", ":")) + "\n").encode()
        self.path.write_bytes(self.previous_bytes)
        self.clock = Clock()
        self.devices = Devices(self.clock)
        self.cancel = threading.Event()
        self.time_patch = patch.dict(GLOBALS, {"time": self.clock})
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.session = SESSION(self.cancel, self.devices.progress, path=self.path,
                               mute_factory=self.devices.mute, peer_factory=self.devices.peer,
                               silent_source_confirmed=True, cleanup_started=self.devices.cleanup)

    def assert_released(self):
        for peer in self.devices.peers.values():
            self.assertFalse(peer.acquired)
            self.assertTrue(peer.socket.aborted)
        if self.session.windows:
            self.assertTrue(self.session.windows.closed)

    def assert_restored(self):
        d = self.devices
        self.assertEqual((d.raw, d.tv, d.muted, d.windows_muted), d.original)
        self.assertTrue(self.session.restoration_verified)
        self.assert_released()

    def assert_previous_preserved(self):
        self.assertEqual(self.path.read_bytes(), self.previous_bytes)
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def first_upward_behavior(self, effect):
        fired = False
        def behavior(devices, direction):
            nonlocal fired
            if not fired and not devices.restoring and direction == 1:
                fired = True
                effect(devices)
                return True
            return False
        self.devices.behavior = behavior

    def test_full_sweep_saves_only_after_verified_restoration_and_cleanup(self):
        original_write = HELPER["write_mapping"]
        writes = []
        def checked_write(path, mapping):
            self.assert_restored()
            self.assert_previous_preserved()
            writes.append(copy.deepcopy(mapping))
            original_write(path, mapping)
        with patch.dict(GLOBALS, {"write_mapping": checked_write}):
            self.assertIn("mapping saved", self.session.run())
        self.assertEqual(len(writes), 1)
        document = HELPER["load_mapping"](self.path)
        self.assertEqual(document["tv_volume_by_amplifier_raw_volume"], [0, 0, 1, 1, 2, 2, 3, 3, 4])
        self.assertEqual(document["amplifier_tv_input"], "tv")
        self.assertEqual(document["estimated_amplifier_raw_volumes"], [])
        self.assertNotIn("observations", document)
        self.assertEqual([row["raw"] for row in writes[0]["observations"]], list(range(1, 9)))
        self.assertTrue(all(abs(row[2]) == 1 for row in self.devices.trace if row[1] == "volume_command"))
        self.assertFalse(any(row[1:3] == ("amplifier", "set_volume") for row in self.devices.trace))

    def test_original_device_and_windows_mute_are_restored_last(self):
        self.devices.muted = self.devices.windows_muted = True
        self.devices.original = (5, 2, True, True)
        self.session.run()
        self.assert_restored()
        restore_mute = next(i for i, row in enumerate(self.devices.trace) if row[1:3] == ("tv", "set_mute"))
        last_windows = max(i for i, row in enumerate(self.devices.trace) if row[1] == "windows_mute")
        self.assertGreater(last_windows, restore_mute)

    def test_delayed_single_step_is_accepted_without_duplicate_command(self):
        self.first_upward_behavior(lambda devices: devices.schedule_move(1, 6.0))
        self.session.run()
        self.assert_restored()
        self.assertTrue(any("Delayed single amplifier step settled" in text for text in self.devices.logs))
        upward = [row for row in self.devices.trace if row[1] == "volume_command" and row[2] == 1 and not row[4]]
        self.assertGreaterEqual(upward[1][0] - upward[0][0], 6)
        self.assertEqual(len(upward), 7)

    def test_no_response_waits_and_retries_from_accepted_pair(self):
        self.first_upward_behavior(lambda devices: None)
        self.session.run()
        self.assert_restored()
        upward = [row for row in self.devices.trace if row[1] == "volume_command" and row[2] == 1 and not row[4]]
        self.assertEqual([row[3] for row in upward[:2]], [1, 1])
        self.assertGreaterEqual(upward[1][0] - upward[0][0], 10)
        self.assertTrue(any("Previous accepted pair verified; retrying" in text for text in self.devices.logs))

    def test_late_multiple_step_rolls_back_before_retry_and_is_not_measurement(self):
        def two_steps(devices):
            devices.schedule_move(1, 6.0)
            devices.schedule_move(1, 6.1)
        self.first_upward_behavior(two_steps)
        self.session.run()
        self.assert_restored()
        commands = [row for row in self.devices.trace if row[1] == "volume_command"]
        first_up = next(i for i, row in enumerate(commands) if row[2] == 1)
        self.assertEqual([(row[2], row[3]) for row in commands[first_up:first_up + 4]], [(1, 1), (-1, 3), (-1, 2), (1, 1)])
        self.assertEqual([row["raw"] for row in self.session.pending_mapping["observations"]], list(range(1, 9)))

    def test_cancel_finishes_in_flight_step_then_restores_without_saving(self):
        def delayed_cancel(devices):
            devices.schedule_move(1, 2.0)
            self.clock.later(0.1, self.cancel.set)
        self.first_upward_behavior(delayed_cancel)
        with self.assertRaises(HELPER["Cancelled"]):
            self.session.run()
        self.assert_restored()
        self.assert_previous_preserved()
        issued = next(row for row in self.devices.trace if row[1] == "volume_command" and row[2] == 1)
        self.assertGreaterEqual(self.devices.cleanup_time - issued[0], 2)

    def test_cancel_during_cleanup_does_not_interrupt_restoration_or_publish(self):
        cleanup = self.session.cleanup_started
        self.session.cleanup_started = lambda: (cleanup(), self.cancel.set())
        with self.assertRaises(HELPER["Cancelled"]):
            self.session.run()
        self.assert_restored()
        self.assert_previous_preserved()

    def test_restoration_failure_keeps_windows_muted_and_existing_mapping(self):
        self.devices.behavior = lambda devices, direction: devices.restoring
        with self.assertRaisesRegex(HELPER["CalibrationError"], "Original settings could not be verified"):
            self.session.run()
        self.assertFalse(self.session.restoration_verified)
        self.assertTrue(self.devices.windows_muted)
        self.assert_previous_preserved()
        self.assert_released()

    def test_opposite_amplifier_movement_aborts_but_restores_start(self):
        self.first_upward_behavior(lambda devices: devices.schedule_move(-1, 0.2))
        with self.assertRaisesRegex(HELPER["CalibrationError"], "opposite"):
            self.session.run()
        self.assert_restored()
        self.assert_previous_preserved()

    def test_input_change_prevents_restoring_to_different_pairing(self):
        self.first_upward_behavior(lambda devices: self.clock.later(0.2, lambda: setattr(devices, "input", "other")))
        with self.assertRaisesRegex(HELPER["CalibrationError"], "different pairing"):
            self.session.run()
        self.assertTrue(self.devices.windows_muted)
        self.assertFalse(self.session.restoration_verified)
        self.assertFalse(any(row[1] == "volume_command" and row[4] for row in self.devices.trace))
        self.assert_previous_preserved()
        self.assert_released()

    def test_disconnection_during_step_never_publishes(self):
        self.first_upward_behavior(lambda devices: self.clock.later(0.2, lambda: setattr(devices.peers['amplifier'].socket, 'connected', False)))
        with self.assertRaisesRegex(HELPER["CalibrationError"], "disconnected"):
            self.session.run()
        self.assertTrue(self.devices.windows_muted)
        self.assert_previous_preserved()
        self.assert_released()

    def test_lease_rejection_never_publishes(self):
        self.first_upward_behavior(lambda devices: setattr(devices, "renew_rejected", True))
        with self.assertRaisesRegex(HELPER["CalibrationError"], "lease"):
            self.session.run()
        self.assert_previous_preserved()
        self.assert_released()

    def test_silent_source_confirmation_required_before_touching_devices(self):
        self.session.silent_source_confirmed = False
        with self.assertRaisesRegex(HELPER["CalibrationError"], "silent source"):
            self.session.run()
        self.assertIsNone(self.session.windows)
        self.assertFalse(self.devices.peers)
        self.assert_previous_preserved()

    def test_cancel_before_start_does_not_touch_devices(self):
        self.cancel.set()
        with self.assertRaises(HELPER["Cancelled"]):
            self.session.run()
        self.assertIsNone(self.session.windows)
        self.assertFalse(self.devices.peers)
        self.assert_previous_preserved()

    def test_frozen_observations_abort_without_replaying_uncertain_command(self):
        self.first_upward_behavior(lambda devices: setattr(devices.peers['amplifier'], 'frozen', True))
        with self.assertRaisesRegex(HELPER['CalibrationError'], 'fresh volume observations'):
            self.session.run()
        upward = [row for row in self.devices.trace if row[1] == 'volume_command' and row[2] == 1 and not row[4]]
        self.assertEqual(len(upward), 1)
        self.assertTrue(self.devices.windows_muted)
        self.assertFalse(self.session.restoration_verified)
        self.assert_previous_preserved()
        self.assert_released()

    def test_windows_unmute_during_sweep_aborts_and_restores(self):
        self.first_upward_behavior(lambda devices: self.clock.later(0.2, lambda: setattr(devices, 'windows_muted', False)))
        with self.assertRaisesRegex(HELPER['CalibrationError'], 'Windows was unmuted'):
            self.session.run()
        self.assert_restored()
        self.assert_previous_preserved()

    def test_retries_are_bounded_and_baseline_restored_before_giving_up(self):
        self.devices.behavior = lambda devices, direction: not devices.restoring and direction == 1
        with self.assertRaisesRegex(HELPER['CalibrationError'], 'initial attempt and three retries'):
            self.session.run()
        attempts = [row for row in self.devices.trace if row[1] == 'volume_command' and row[2] == 1 and not row[4]]
        self.assertEqual(len(attempts), 4)
        self.assertEqual([row[3] for row in attempts], [1, 1, 1, 1])
        self.assert_restored()
        self.assert_previous_preserved()

    def test_lost_command_ack_is_not_retried_even_if_device_moved(self):
        def lost_ack(devices):
            devices.schedule_move(1, 0.2)
            devices.drop_ack = lambda role, action: role == 'tv' and action == 'adjust_volume' and not devices.restoring
        self.first_upward_behavior(lost_ack)
        with self.assertRaisesRegex(HELPER['CalibrationError'], 'operation timed out; it was not retried'):
            self.session.run()
        attempts = [row for row in self.devices.trace if row[1] == 'volume_command' and row[2] == 1 and not row[4]]
        self.assertEqual(len(attempts), 1)
        self.assert_restored()
        self.assert_previous_preserved()

    def test_atomic_publication_failure_occurs_after_devices_are_restored(self):
        with patch.object(HELPER['os'], 'replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.session.run()
        self.assert_restored()
        self.assert_previous_preserved()

    def test_success_without_previous_mapping_creates_valid_file(self):
        self.path.unlink()
        self.session.run()
        self.assert_restored()
        self.assertEqual(HELPER['load_mapping'](self.path)['tv_volume_by_amplifier_raw_volume'],
                         [0, 0, 1, 1, 2, 2, 3, 3, 4])

    def test_tv_maximum_can_end_sweep_before_amplifier_api_ceiling(self):
        self.devices.tv_max = 3
        self.session.run()
        self.assert_restored()
        self.assertEqual(self.session.pending_mapping['raw_max'], 6)
        self.assertEqual(HELPER['load_mapping'](self.path)['tv_volume_by_amplifier_raw_volume'][-1], 3)

    def test_fast_positioning_and_restore_resynchronize_before_final_steps(self):
        self.devices.raw, self.devices.tv, self.devices.ceiling = 14, 7, 32
        self.devices.reachable = list(range(33))
        self.devices.tv_lag = 0.6
        self.devices.original = (14, 7, False, False)
        self.session.run()
        self.assert_restored()
        self.assertTrue(any('after fast positioning' in text for text in self.devices.logs))
        self.assertTrue(any('before the final restoration approach' in text for text in self.devices.logs))
        self.assertEqual([row['raw'] for row in self.session.pending_mapping['observations']], list(range(1, 33)))
        self.assertEqual(HELPER['load_mapping'](self.path)['tv_volume_by_amplifier_raw_volume'],
                         [raw // 2 for raw in range(33)])

    def test_transient_opposite_tv_display_settles_without_aborting_correct_amplifier_step(self):
        bounced = False
        def bounce(devices, direction):
            nonlocal bounced
            if not bounced and not devices.restoring and direction == 1 and devices.raw == 4:
                bounced = True
                devices.schedule_move(1, 0.2)
                self.clock.later(0.21, lambda: setattr(devices, 'tv', 1))
                self.clock.later(0.8, lambda: setattr(devices, 'tv', 2))
                return True
            return False
        self.devices.behavior = bounce
        self.session.run()
        self.assertTrue(bounced)
        self.assert_restored()
        self.assertEqual(HELPER['load_mapping'](self.path)['tv_volume_by_amplifier_raw_volume'],
                         [raw // 2 for raw in range(9)])

    def test_initial_device_mute_disagreement_does_not_start_sweep(self):
        snapshot = self.devices.snapshot
        def mismatched(role, sample):
            state = snapshot(role, sample)
            if role == 'tv':
                state['mute'] = True
            return state
        self.devices.snapshot = mismatched
        with self.assertRaisesRegex(HELPER['CalibrationError'], 'mute states differ'):
            self.session.run()
        self.assertIsNone(self.session.saved)
        self.assertFalse(any(row[1] in ('volume_command', 'windows_mute') for row in self.devices.trace))
        self.assert_previous_preserved()
        self.assert_released()

    def test_unreachable_gap_is_rechecked_twice_each_way_before_estimation(self):
        self.devices.reachable.remove(3)
        self.session.run()
        self.assert_restored()
        mapping = self.session.pending_mapping
        crossings = mapping['gap_checks']['3']
        self.assertEqual(Counter(row['direction'] for row in crossings), {1: 2, -1: 2})
        self.assertEqual(mapping['lookup'][3]['source'], 'interpolated')
        self.assertEqual(HELPER['load_mapping'](self.path)['estimated_amplifier_raw_volumes'], [3])

    def test_gap_reached_during_recheck_becomes_measured(self):
        skipped = False
        def skip_once(devices, direction):
            nonlocal skipped
            if not skipped and not devices.restoring and devices.raw == 2 and direction == 1:
                skipped = True
                devices.schedule_move(1, 0.2)
                devices.schedule_move(1, 0.2)
                return True
            return False
        self.devices.behavior = skip_once
        self.session.run()
        self.assert_restored()
        self.assertEqual(self.session.pending_mapping['lookup'][3]['source'], 'measured')
        self.assertEqual(HELPER['load_mapping'](self.path)['estimated_amplifier_raw_volumes'], [])

    def test_speakers_off_before_sweep_and_restored_after_volume_before_windows(self):
        message = self.session.run()
        self.assertNotIn('without verified speaker isolation', message)
        trace = self.devices.trace
        off = next(i for i, row in enumerate(trace) if row[1:3] == ('speakers', (False, False)))
        first_volume = next(i for i, row in enumerate(trace) if row[1] == 'volume_command')
        restored = max(i for i, row in enumerate(trace) if row[1:3] == ('speakers', (True, False)))
        last_volume = max(i for i, row in enumerate(trace) if row[1] == 'volume_command')
        windows = max(i for i, row in enumerate(trace) if row[1] == 'windows_mute')
        self.assertLess(off, first_volume)
        self.assertGreater(restored, last_volume)
        self.assertLess(restored, windows)
        self.assertEqual(self.devices.speakers, (True, False))
        self.assert_restored()

    def test_unsupported_isolation_can_continue_with_one_explicit_decision(self):
        self.devices.speaker_outputs = []
        self.session.confirm_isolation = Mock(return_value=True)
        message = self.session.run()
        self.session.confirm_isolation.assert_called_once()
        self.assertIn('without verified speaker isolation', message)
        self.assertFalse(any(row[1:3] == ('amplifier', 'set_speakers') for row in self.devices.trace))
        self.assert_restored()

    def test_declining_isolation_warning_cancels_without_sweeping(self):
        self.devices.speaker_outputs = []
        self.session.confirm_isolation = Mock(return_value=False)
        with self.assertRaises(HELPER['Cancelled']):
            self.session.run()
        self.session.confirm_isolation.assert_called_once()
        self.assertFalse(any(row[1] == 'volume_command' for row in self.devices.trace))
        self.assert_previous_preserved()
        self.assert_restored()

    def test_completed_off_rejection_can_continue_and_restore_original_outputs(self):
        self.devices.isolation_rejected = True
        self.session.confirm_isolation = Mock(return_value=True)
        self.session.run()
        self.session.confirm_isolation.assert_called_once()
        self.assertEqual(self.devices.speakers, (True, False))
        self.assert_restored()

    def test_repeated_isolation_loss_is_warned_only_once(self):
        def behavior(devices, direction):
            if not devices.restoring:
                devices.speakers = (bool(devices.moves % 2), False)
            return False
        self.devices.behavior = behavior
        self.session.confirm_isolation = Mock(return_value=True)
        message = self.session.run()
        self.session.confirm_isolation.assert_called_once()
        self.assertIn('without verified speaker isolation', message)
        self.assertGreater(self.devices.moves, 5)
        self.assertEqual(self.devices.speakers, (True, False))
        self.assert_restored()

    def test_long_isolation_decision_renews_leases_and_pauses_step_deadlines(self):
        def behavior(devices, direction):
            if not devices.restoring:
                devices.speakers = (True, False)
            return False
        def decide(reason, pump):
            for _ in range(500):
                pump()
            return True
        self.devices.behavior = behavior
        self.session.confirm_isolation = Mock(side_effect=decide)
        self.session.run()
        self.session.confirm_isolation.assert_called_once()
        self.assertGreater(self.session._prompt_pause_total, 30)
        self.assertGreater(sum(row[2] == 'renew' for row in self.devices.trace if row[1] in ('tv', 'amplifier')), 15)
        self.assert_restored()

    def test_connection_loss_during_warning_still_aborts(self):
        self.devices.speaker_outputs = []
        def decide(reason, pump):
            self.devices.peers['amplifier'].socket.abort()
            pump()
            return True
        self.session.confirm_isolation = decide
        with self.assertRaises(HELPER['CalibrationError']):
            self.session.run()
        self.assertTrue(self.devices.windows_muted)
        self.assert_previous_preserved()
        self.assert_released()

    def test_uncertain_speaker_command_timeout_is_not_a_continue_case(self):
        self.devices.drop_ack = lambda role, action: role == 'amplifier' and action == 'set_speakers' and not self.devices.restoring
        self.session.confirm_isolation = Mock(return_value=True)
        with self.assertRaises(HELPER['CalibrationError']):
            self.session.run()
        self.session.confirm_isolation.assert_not_called()
        self.assert_previous_preserved()
        self.assert_released()

    def test_volume_restore_failure_never_reenables_speakers(self):
        original = self.session.seek_volume
        def seek(*args, **kwargs):
            if self.session.restoring:
                raise HELPER['CalibrationError']('Restore could not be verified')
            return original(*args, **kwargs)
        self.session.seek_volume = seek
        with self.assertRaises(HELPER['CalibrationError']):
            self.session.run()
        self.assertEqual(self.devices.speakers, (False, False))
        self.assertTrue(self.devices.windows_muted)
        self.assert_previous_preserved()

    def test_speaker_restoration_failure_is_reported_and_windows_stays_muted(self):
        self.devices.speaker_restore_rejected = True
        with self.assertRaises(HELPER['CalibrationError']):
            self.session.run()
        self.assertEqual((self.devices.raw, self.devices.tv), self.devices.original[:2])
        self.assertEqual(self.devices.speakers, (False, False))
        self.assertTrue(self.devices.windows_muted)
        self.assert_previous_preserved()



class MappingTests(unittest.TestCase):
    def mapping(self):
        observations = [{'raw': 1, 'tv': 0}, {'raw': 2, 'tv': 1}, {'raw': 4, 'tv': 3},
                        {'raw': 5, 'tv': 4}, {'raw': 5, 'tv': 5}]
        checks = {'3': [{'direction': direction, 'from': 2 if direction > 0 else 4,
                         'to': 4 if direction > 0 else 2} for direction in (1, -1, 1, -1)]}
        saved = {'input': 'tv', 'tv_id': 'tv-id', 'amplifier_id': 'amp-id'}
        return HELPER['build_canonical_mapping'](observations, checks, saved, 5, 5, 0)

    def test_complete_lookup_boundary_clamp_gap_and_lowest_capped_tv(self):
        mapping = self.mapping()
        self.assertEqual([row['tv'] for row in mapping['lookup']], [0, 0, 1, 2, 3, 4])
        self.assertEqual(mapping['lookup'][0]['source'], 'boundary-clamped')
        self.assertEqual(mapping['lookup'][3]['anchors'], [2, 4])
        self.assertEqual(mapping['tv_max'], 4)
        self.assertEqual(mapping['observed_tv_max'], 5)
        document = HELPER['runtime_document'](mapping)
        self.assertEqual(document['estimated_amplifier_raw_volumes'], [3])
        self.assertEqual(set(document), {'format_version', 'tv_device_id', 'amplifier_device_id',
                                        'amplifier_tv_input', 'tv_volume_by_amplifier_raw_volume',
                                        'estimated_amplifier_raw_volumes'})

    def mapping_with_gaps(self, levels, missing):
        known = [raw for raw in range(len(levels)) if raw not in missing]
        checks = {}
        for raw in missing:
            lower = max(value for value in known if value < raw)
            upper = min(value for value in known if value > raw)
            checks[str(raw)] = [
                {'direction': direction, 'from': lower if direction > 0 else upper,
                 'to': upper if direction > 0 else lower}
                for direction in (1, -1, 1, -1)]
        saved = {'input': 'tv', 'tv_id': 'tv-id', 'amplifier_id': 'amp-id'}
        observations = [{'raw': raw, 'tv': levels[raw]} for raw in known]
        return HELPER['build_canonical_mapping'](
            observations, checks, saved, len(levels) - 1, len(levels) - 1, levels[0])

    def test_wide_gap_uses_one_rule_and_cannot_make_the_lookup_decrease(self):
        levels = [(raw + 1) // 4 for raw in range(41)]
        missing = set(range(12, 18))
        # Previously per-value rule selection produced TV 3, 3, 4, 3, 3, 3
        # inside this gap, causing the complete calibration to be rejected.
        mapping = self.mapping_with_gaps(levels, missing)
        gap = mapping['lookup'][12:18]
        self.assertEqual(len({row['rounding'] for row in gap}), 1)
        self.assertTrue(all(row['anchors'] == [11, 18] for row in gap))
        result = [row['tv'] for row in mapping['lookup']]
        self.assertEqual(result, sorted(result))
        for raw in set(range(len(levels))) - missing:
            self.assertEqual(result[raw], levels[raw])
        document = HELPER['runtime_document'](mapping)
        REMOTE['prepare_volume_mapping'](document)
        self.assertEqual(document['estimated_amplifier_raw_volumes'], sorted(missing))

    def test_separate_gaps_can_still_select_all_three_rounding_methods(self):
        levels = [raw // 4 for raw in range(41)]
        missing = {raw for raw in range(1, 40) if raw % 5 not in (0, 2, 3)}
        mapping = self.mapping_with_gaps(levels, missing)
        self.assertEqual({row['rounding'] for row in mapping['lookup'] if row['raw'] in missing},
                         {'nearest-half-up', 'floor', 'ceil'})
        # Floor still retains the more accurate estimate in the reviewed
        # example: raw 39 is TV 9, whereas nearest-only would yield TV 10.
        self.assertEqual(mapping['lookup'][39]['tv'], 9)
        self.assertEqual(mapping['lookup'][39]['rounding'], 'floor')

    def test_gap_without_middle_observations_defaults_to_nearest(self):
        mapping = self.mapping_with_gaps([0, 0, 0, 1, 1, 2], {1, 2, 3, 4})
        self.assertEqual([row['tv'] for row in mapping['lookup']], [0, 0, 1, 1, 2, 2])
        self.assertEqual({row['rounding'] for row in mapping['lookup'][1:5]}, {'nearest-half-up'})

    def test_incomplete_crossing_evidence_cannot_be_saved_as_interpolation(self):
        for evidence in ([], [{'direction': 1, 'from': 2, 'to': 4}] * 4,
                         [{'direction': d, 'from': 2, 'to': 2} for d in (1, -1, 1, -1)]):
            mapping = self.mapping()
            mapping['gap_checks']['3'] = evidence
            with self.subTest(evidence=evidence):
                with self.assertRaises(ValueError):
                    HELPER['validate_sweep_mapping'](mapping)

    def test_missing_nonmonotonic_or_false_measurements_are_rejected(self):
        for mutation in (lambda m: m['lookup'].pop(2),
                         lambda m: m['lookup'][2].update(tv=4),
                         lambda m: m['lookup'][2].update(tv=0),
                         lambda m: m['lookup'][3].update(anchors=[0, 5]),
                         lambda m: m['lookup'][0].update(tv=1)):
            mapping = self.mapping()
            mutation(mapping)
            with self.assertRaises(ValueError):
                HELPER['validate_sweep_mapping'](mapping)

    def test_display_marks_estimates_and_fractional_values_without_reinterpolation(self):
        mapping = REMOTE['prepare_volume_mapping'](HELPER['runtime_document'](self.mapping()))
        for raw, expected in ((-1, (0, False)), (0, (0, False)), (1, (0, False)),
                              (2, (1, False)), (3, (2, True)), (2.5, (2, True)),
                              (4.2, (3, True)), (100, (4, False))):
            with self.subTest(raw=raw):
                self.assertEqual(REMOTE['mapped_tv_volume'](mapping, raw), expected)

    def test_failed_atomic_replace_preserves_existing_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'VolumeCalibration.json'
            path.write_bytes(b'previous bytes\n')
            with patch.object(HELPER['os'], 'replace', side_effect=OSError('simulated replace failure')):
                with self.assertRaises(OSError):
                    HELPER['write_mapping'](path, self.mapping())
            self.assertEqual(path.read_bytes(), b'previous bytes\n')
            self.assertFalse(path.with_name(path.name + '.tmp').exists())


if __name__ == '__main__':
    unittest.main()
