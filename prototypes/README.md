# Standalone experiments

These scripts need PySide6 (`pip install "PySide6>=6.8"`). They do not import or modify either remote app.

## TV volume settling

Run `TVVolumeSettlePrototype.pyw` beside a copy of `SonyRemote.json`. Close Sony Remote and other software volume controllers; Yamaha Remote is not required. The test works through the TV API with TV speakers or TV-to-amplifier CEC.

1. Click **Read current volume**, then choose a safe target different from the current value.
2. In relative mode, keep the command interval at your normal rate (copied from `VolumeRateLimit.RateMs`, otherwise 500 ms). In absolute mode, start with a **500 ms absolute quiet period**, then compare **800 ms** if needed. Keep the **confirmation delay after a target reading at 500 ms**.
3. Enable **Send absolute target values on every attempt** to test absolute commands, or leave it off for relative commands.
4. Click **Run test**, then **Save log** when it finishes. Each apparent target arrival must pass the delayed verification. If still short, the prototype continues in the original direction and verifies the next target arrival again; the old single-correction option has been removed.

**Send absolute target values on every attempt** switches all volume writes to the chosen target number. The default relative mode sends `+1`/`-1` (API 1.0); absolute mode sends the unsigned target with `ui: on` (API 1.2, as in the earlier absolute-volume experiment). Try targets above and below the current volume to see whether CEC moves toward them incrementally or the TV jumps directly. In absolute mode the relative command interval is ignored: after each command completes, the prototype sends no further requests during the absolute quiet period, then reads volume. A value still short causes another command; a target reading gets the separate delayed confirmation before success. This replaces the old relative interval plus verification wait after every requested-value echo. Logs identify the mode, both delays, command values, quiet-period timing and reported results.

The loop stops only after a verified target, a target crossing, or five seconds without closer progress. Provisional target readings do not reset the progress timer: repeated requested-value echoes followed by unchanged actual volume still time out. Larger shortfalls can be retried, but the prototype never reverses direction to chase an overshoot. The log includes commands, reported volume/mute/range, HTTP durations and the delayed verification result. There is no background polling, automatic startup command, mute change or automatic restoration. Stop cancels waits and future requests; an HTTP request already sent cannot be recalled. Check your device volume/mute afterward.

## TV/amplifier CEC handover

`TVAmplifierCECHandoverPrototype.pyw` needs copies of both `SonyRemote.json` and `YamahaRemote.json` beside it. It directly monitors amplifier input changes and tests delayed absolute TV volume commands after switching from `tv` to `usb_dac`; use its measurement toggles and log to compare timings. Close the normal remotes for isolated testing.
