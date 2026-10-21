"""Development-only regressions; neither application imports this file."""
import copy
import hashlib
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / "SonyRemote.pyw"), run_name="test_remote")
CALIBRATOR = runpy.run_path(str(ROOT / "VolumeCalibration.pyw"), run_name="test_calibrator")


class ConfigurationTests(unittest.TestCase):
    def test_optional_defaults_and_boolean_validation(self):
        defaults = REMOTE["DEFAULT_CONFIG"]
        result = REMOTE["validate_config"]({}, allow_incomplete=True)
        self.assertEqual(result, defaults)
        self.assertEqual(set(REMOTE["BOOLEAN_CONFIG_KEYS"]),
                         {key for key, value in defaults.items() if type(value) is bool})
        for key in REMOTE["BOOLEAN_CONFIG_KEYS"]:
            for invalid in (0, 1, "true", None):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaises(REMOTE["ConfigError"]):
                        REMOTE["validate_config"]({key: invalid}, allow_incomplete=True)

    def test_unknown_settings_and_missing_required_settings(self):
        with self.assertRaisesRegex(REMOTE["ConfigError"], "Unknown configuration"):
            REMOTE["validate_config"]({"UnknownSetting": False}, allow_incomplete=True)
        with self.assertRaises(REMOTE["ConfigError"]):
            REMOTE["validate_config"]({})


class DiscoveryTests(unittest.TestCase):
    def test_both_collections_preserve_customization_and_insertion_order(self):
        sync = REMOTE["SonyRemote"]._sync_discovered_config
        for collection in ("Apps", "Inputs"):
            with self.subTest(collection=collection):
                existing = {"Name": "Old", "NameOverride": "Custom", "Hidden": True, "Favorite": True}
                remote = SimpleNamespace(config={collection: {"existing": existing, "unseen": {"Name": "Unseen"}}})
                entries = [{"uri": "new", "title": "New &amp; useful"},
                           {"uri": "existing", "title": "Updated"},
                           {"uri": "", "title": "Ignore"}, {"uri": "ignore", "title": ""}]
                self.assertTrue(sync(remote, collection, entries))
                values = remote.config[collection]
                self.assertEqual(list(values), ["existing", "unseen", "new"])
                self.assertEqual(existing, {"Name": "Updated", "NameOverride": "Custom", "Hidden": True, "Favorite": True, "Removed": False})
                self.assertEqual(values["new"], {"Name": "New & useful", "NameOverride": "", "Hidden": False, "Favorite": False, "Removed": False})
                self.assertFalse(sync(remote, collection, entries))


class CalibrationGuardTests(unittest.TestCase):
    def test_either_session_blocks_commands(self):
        guard = REMOTE["SonyRemote"]._calibration_blocked
        for local in (False, True):
            for peer in (None, {}, {"calibration_active": False}, {"calibration_active": True}):
                with self.subTest(local=local, peer=peer):
                    remote = SimpleNamespace(calibration_active=local, amplifier_state=peer)
                    expected = local or bool(peer and peer.get("calibration_active"))
                    self.assertEqual(guard(remote), expected)

    def test_all_ordinary_action_entry_points_obey_guard(self):
        remote = SimpleNamespace(_calibration_blocked=lambda: True)
        cls = REMOTE["SonyRemote"]
        for name, args in (("toggle_power", ()), ("toggle_mute", ()), ("adjust_volume", (1,)),
                           ("launch_app", ("app",)), ("set_input", ("input",)),
                           ("send_ircc", ("Down",)), ("send_text_input", ()),
                           ("send_text_input_and_confirm", ())):
            with self.subTest(action=name):
                # Any access beyond the guard fails on this deliberately minimal object.
                self.assertIsNone(getattr(cls, name)(remote, *args))


class RuntimeMappingTests(unittest.TestCase):
    def document(self):
        return {"format_version": 2, "tv_device_id": "tv-id", "amplifier_device_id": "amp-id",
                "amplifier_tv_input": "tv", "tv_volume_by_amplifier_raw_volume": [0, 0, 1, 2],
                "estimated_amplifier_raw_volumes": [2]}

    def test_validation_returns_document_without_mutation(self):
        for module in (REMOTE, CALIBRATOR):
            for estimates in (True, False):
                document = self.document()
                if not estimates:
                    document.pop("estimated_amplifier_raw_volumes")
                before = copy.deepcopy(document)
                self.assertIs(module["validate_runtime_mapping"](document), document)
                self.assertEqual(document, before)

    def test_validators_reject_same_malformed_documents(self):
        cases = [("format_version", 1), ("format_version", True), ("tv_device_id", ""),
                 ("amplifier_device_id", None), ("amplifier_tv_input", " "),
                 ("tv_volume_by_amplifier_raw_volume", [0]),
                 ("tv_volume_by_amplifier_raw_volume", [0, -1]),
                 ("tv_volume_by_amplifier_raw_volume", [0, True]),
                 ("tv_volume_by_amplifier_raw_volume", [0, 1.0]),
                 ("tv_volume_by_amplifier_raw_volume", [0, 2, 1]),
                 ("tv_volume_by_amplifier_raw_volume", [0] * 10002),
                 ("estimated_amplifier_raw_volumes", [2, 2]),
                 ("estimated_amplifier_raw_volumes", [4]),
                 ("estimated_amplifier_raw_volumes", [True])]
        for key, invalid in cases:
            document = self.document()
            document[key] = invalid
            for module in (REMOTE, CALIBRATOR):
                with self.subTest(key=key, module=module["__name__"]):
                    with self.assertRaises(ValueError):
                        module["validate_runtime_mapping"](document)

    def test_decimal_buckets_follow_exact_group_size(self):
        for size, expected in ((1, [20]), (2, [20, 20.5]), (3, [20, 20.3, 20.7]),
                               (4, [20, 20.3, 20.5, 20.8])):
            with self.subTest(size=size):
                document = self.document()
                levels = [19] + [20] * size + [24, 24]
                document['tv_volume_by_amplifier_raw_volume'] = levels
                document['estimated_amplifier_raw_volumes'] = []
                mapping = REMOTE['prepare_volume_mapping'](document)
                values = [REMOTE['mapped_tv_volume'](mapping, raw)[0] for raw in range(1, size + 1)]
                self.assertEqual(values, expected)
                # A missing adjacent TV value does not change this bucket.
                self.assertEqual(REMOTE['mapped_tv_volume'](mapping, size + 2)[0], 24.5)
                self.assertEqual(document['tv_volume_by_amplifier_raw_volume'], levels)

    def test_large_buckets_never_round_into_next_tv_integer(self):
        document = self.document()
        document['tv_volume_by_amplifier_raw_volume'] = [20] * 101 + [21]
        document['estimated_amplifier_raw_volumes'] = []
        mapping = REMOTE['prepare_volume_mapping'](document)
        values = [REMOTE['mapped_tv_volume'](mapping, raw)[0] for raw in range(101)]
        self.assertEqual(values[0], 20)
        self.assertEqual(values[-1], 20.9)
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(20 <= value < 21 for value in values))
        self.assertEqual(REMOTE['mapped_tv_volume'](mapping, 101), (21, False))

    def test_decimal_clamping_preserves_final_bucket_and_estimate_flags(self):
        document = self.document()
        document['tv_volume_by_amplifier_raw_volume'] = [0, 0, 20, 20, 20]
        document['estimated_amplifier_raw_volumes'] = [3]
        mapping = REMOTE['prepare_volume_mapping'](document)
        self.assertEqual(REMOTE['mapped_tv_volume'](mapping, -1), (0, False))
        self.assertEqual(REMOTE['mapped_tv_volume'](mapping, 3), (20.3, True))
        self.assertEqual(REMOTE['mapped_tv_volume'](mapping, 4), (20.7, False))
        self.assertEqual(REMOTE['mapped_tv_volume'](mapping, 100), (20.7, False))
        self.assertEqual(str(REMOTE['mapped_tv_volume'](mapping, 2)[0]), '20')

    def test_display_preparation_preserves_scale_and_estimates(self):
        document = self.document()
        mapping = REMOTE["prepare_volume_mapping"](document)
        self.assertEqual((mapping["raw_min"], mapping["raw_max"], mapping["tv_min"], mapping["tv_max"]), (0, 3, 0, 2))
        self.assertEqual((mapping["tv_id"], mapping["amplifier_id"], mapping["input"]), ("tv-id", "amp-id", "tv"))
        for raw, expected in ((0, (0, False)), (1, (0.5, False)), (2, (1, True)), (3, (2, False))):
            self.assertEqual(REMOTE["mapped_tv_volume"](mapping, raw), expected)
        with self.assertRaises(ValueError):
            REMOTE["prepare_volume_mapping"]({})


class EndpointTests(unittest.TestCase):
    def test_remote_and_helper_use_generic_identical_endpoint_names(self):
        suffix = hashlib.sha256(str(Path.home()).encode("utf-8")).hexdigest()[:16]
        for role in ("tv", "amplifier"):
            for calibration in (False, True):
                expected = f"remote-control.{role}{'.calibration' if calibration else ''}.v1.{suffix}"
                for module in (REMOTE, CALIBRATOR):
                    self.assertEqual(module["ipc_server_name"](role, channel="calibration" if calibration else None), expected)
        self.assertEqual(REMOTE["IPC_SERVER_NAME"], REMOTE["ipc_server_name"]("amplifier"))


if __name__ == "__main__":
    unittest.main()
