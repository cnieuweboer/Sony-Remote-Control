# Standalone experiments

These scripts need PySide6 (`pip install "PySide6>=6.8"`). They do not import or modify either remote app.

## TV volume settling

Run `TVVolumeSettlePrototype.pyw` beside a copy of `SonyRemote.json`. Close Sony Remote and other software volume controllers; Yamaha Remote is not required. The test works through the TV API with TV speakers or TV-to-amplifier CEC.

1. Click **Read current volume**, then choose a safe target different from the current value.
2. Keep the command interval at your normal rate (copied from `VolumeRateLimit.RateMs`, otherwise 500 ms), and start with a **500 ms verification delay**.
3. Leave correction disabled for the first run. Click **Run test**, then **Save log** when it finishes.
4. Optionally enable correction and repeat. A correction sends only one relative step when verification finds the TV exactly one level short in the original direction; it then checks once more and stops.

The initial loop stops at the target, a target crossing, or five seconds without closer progress. The log includes commands, reported volume/mute/range, HTTP durations and the delayed verification result. There is no background polling, automatic startup command, absolute volume set, mute change or automatic restoration. Stop cancels waits and future requests; an HTTP request already sent cannot be recalled. Check your device volume/mute afterward.

## TV/amplifier CEC handover

`TVAmplifierCECHandoverPrototype.pyw` needs copies of both `SonyRemote.json` and `YamahaRemote.json` beside it. It directly monitors amplifier input changes and tests delayed absolute TV volume commands after switching from `tv` to `usb_dac`; use its measurement toggles and log to compare timings. Close the normal remotes for isolated testing.
