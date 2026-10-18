# Implementation details

For setup and everyday use, see the [README](README.md). Dated changes are in the [release history](CHANGELOG.md).

## Windows shortcut reliability

All Windows shortcuts still use a low-level Windows keyboard hook. Qt only observes focus changes and delivers queued commands to the GUI; it does not intercept Windows hotkeys.

The hook uses pointer-sized Win32 arguments and return values throughout, so forwarding an unclaimed key preserves its original event pointer on 64-bit Windows. A dedicated native message thread keeps forwarding responsive during GUI work and restart waits. The callback reads a plain focus/ownership snapshot and queues commands; device and IPC requests run outside it. Other apps' keys are forwarded unchanged.

A physical press keeps its original owner until release. Mute toggles once per press; volume and navigation keys can repeat while held. Commands use a bounded queue; an unaccepted new press passes through. Sony defers volume/mute keys while the amplifier's global ownership is valid, regardless of which remote started first. Hook failures are reported while the remote remains usable through its buttons. Focused shortcuts are suspended while a Qt popup is open, including combo dropdowns; pending native shortcut delivery also checks popup state. Show/Hide and focus events refresh ownership when the popup closes.

## Local amplifier routing

The remotes cooperate over a user-local Qt socket. Both apps remain self-contained; neither reads the other's JSON. No additional dependencies are needed.

Set, for example, `"AmplifierTVInput": "hdmi1"` in `SonyRemote.json`. Use the corresponding key from Yamaha Remote's `Inputs` setting, not a `NameOverride`. While the amplifier is reachable, on, and using that input, Sony's volume/mute buttons and focused volume keys send commands directly to it. While routing, the volume number and mute label follow the amplifier's published state, including changes made directly through Yamaha Remote. A matching calibration supplies the TV-scale number; otherwise the number is raw amplifier volume. Its tooltip identifies the source. The TV's own stored volume remains separate.

Standby, another amplifier input, offline state, a disconnected socket, or expired status automatically restores ordinary TV volume control. When Yamaha's global volume hook is actually active, Sony passes its focused volume/mute keys through so Yamaha handles each press. This hotkey ownership applies on every amplifier input, independently of whether Sony's volume buttons route to it. Disabling global hotkeys, losing the connection, or losing valid ownership restores Sony's focused shortcuts. Other shortcuts retain their existing behavior.

Connection attempts are asynchronous and silent when no amplifier app is running. A two-second reconnect timer runs only while disconnected; no periodic IPC state requests are sent. Connected clients receive state updates from existing device polls and state events. A freshness deadline prevents continued use of stale state if the amplifier app stops responding. If Sony starts first, routing becomes available automatically after the amplifier app starts.

A routing change cancels pending TV volume rate-limit targets. An already-running TV request may still finish. Amplifier commands use its existing serialized worker and bounded queue, bypassing Sony's TV/CEC rate limiter. Queue overflow is silently dropped; other rejected commands and acknowledgement failures are reported non-modally without retrying against either device, because a retry could apply a volume step twice. Acknowledgements confirm queue acceptance rather than completed device execution.

The amplifier's reported raw maximum is an API ceiling, not its physical volume limit. With a matching calibration file, Sony displays the observed TV-scale equivalent of amplifier volume, capped to the calibrated TV range. It does not write a converted volume back to the TV. Without a matching mapping it displays the amplifier raw value.

## Calibration sessions

Use current versions of both remotes with `VolumeCalibration.pyw`, available in this repository. The helper is a separate, self-contained PySide6 application and communicates only through generic local IPC endpoints. It does not read either remote's settings or credentials.

During a session, both remotes reuse their existing polling timers at 250 ms, including while hidden. Ordinary controls are disabled and pending volume targets are cleared. Yamaha's Windows hook consumes volume/mute media keys without issuing device commands, so they cannot unmute Windows or change volume during the test. Config-driven restarts wait until the session ends. A socket-owned eight-second lease, renewed by the helper, restores normal polling and controls on release, disconnect, or helper failure. Only the owner can perform calibration operations; API work stays in the existing device workers.

The helper first captures the selected amplifier input through IPC. The input and mapping are now saved in `VolumeCalibration.json` after a successful full sweep and restoration. `AmplifierTVInput` remains a fallback setting; the calibrated input takes precedence only for the matching configured devices.

## Calibration workflow

Keep the helper beside `SonyRemote.pyw` so Sony watches the same `VolumeCalibration.json`.

1. Start both remotes. Select the amplifier input physically connected to the TV and start at a comfortable volume below its API ceiling, with TV/amplifier mute states agreeing.
2. Put the **TV on a silent input/source**, stop all other playback, and keep the same Windows output selected. Volume changes may automatically unmute the TV/amplifier. Windows mute does not silence TV-generated audio or other amplifier sources. The sweep can reach a **VERY LOUD** range if the source becomes active. Do not use physical device controls during calibration.
3. Confirm the silent-source checkbox. The single button offers **Start calibration** when no usable mapping exists, or **Replace calibration** when one is already saved. Replacement asks for confirmation; the existing file stays intact until the new sweep and restoration succeed.
4. While running, that same button becomes **Cancel calibration**. The activity bar signals work without claiming a percentage, and the bottom log explains the current operation. A cancellation waits for any issued command and bounded rollback to finish, then attempts restoration. During cleanup the button is disabled and reads **Restoring original settings…**. Closing the window during work also requests cancellation and waits for cleanup before allowing closure.
5. After success, cancellation, or failure, a warning and the log ask you to **check volume and mute settings on the TV, amplifier, and computer before playing sound**. Confirm the silent-source checkbox again before another sweep.

The helper always runs one complete upward sweep, starting at the TV's reported minimum and stopping at the first amplifier network API ceiling reading or the TV's reported maximum. There is no step-check-only mode, alternative restore volume, configurable sweep limit, or full downward measurement pass. Faster positioning waits for individual amplifier movements above TV minimum + 5; a five-second quiet interval resynchronizes readings before the final verified steps. Every amplifier raw change is useful even when the TV number stays unchanged.

Skipped interior amplifier integers are approached twice from below and twice from above through relative CEC commands. A value reached during rechecking becomes measured; only repeated confirmed crossings permit interpolation. Inconclusive commands are not evidence of an unreachable value. Successful calibration saves a complete lookup from amplifier raw zero through the observed maximum, with a lower-boundary clamp where needed and the lowest TV number associated with the API ceiling. The API ceiling is a network readout/control limit, not the physical amplifier maximum.

Relative commands are `+1` / `-1`. Confirmed movement must settle at the first observed amplifier value. A transient TV-only reading in the opposite direction may catch up; actual opposite amplifier movement stops the run. An inconclusive command pauses new commands for five quiet seconds (up to twenty seconds if readings keep changing). A late single raw step can be accepted without replay. Otherwise the helper restores the last accepted raw/TV pair before retrying, with an initial attempt plus three retries. Larger delayed changes are rolled back, and rollback movements do not count as measurements. Rollback is bounded to sixteen correction commands / three minutes, with no more than four consecutive no-progress commands.

Starting settings are held **only in memory for the current run**. After success, cancellation, or failure, the helper attempts to restore the exact starting amplifier raw volume and matching TV level, then the original mute states, restoring Windows mute last. Restoration uses faster movement while more than five TV levels and five raw units from the target, then resynchronizes and verifies the final approach. If restoration cannot be verified, Windows stays muted where possible and the helper asks you to restore settings manually. A new run always starts from current settled settings; it does not resume or repair an interrupted run.

The previous completed mapping is atomically replaced only after the full sweep and restoration succeed. Failed/cancelled working measurements are discarded. No recovery/draft journal is created or read, and no persistent recovery can block a new sweep. Force-closing the process or shutting down the computer can prevent automatic restoration; check device and computer volume/mute manually before restarting playback. The remotes' socket-owned leases still expire and restore ordinary controls after a disconnected helper.

The upward pass and positioning/restoration seeks are limited to 512 successful steps and fifteen minutes each. Gap verification has a shared limit of 2,048 steps and thirty minutes. No new periodic IPC status requests are added; existing remote device timers provide observations. Calibration requires integer amplifier raw values and an integer API ceiling between 1 and 10,000.

## Mapping file and display

`VolumeCalibration.json` now contains only the operational data Sony needs. Format version 2 has six fields:

```json
{
  "format_version": 2,
  "tv_device_id": "configured-TV fingerprint",
  "amplifier_device_id": "configured-amplifier fingerprint",
  "amplifier_tv_input": "tv",
  "tv_volume_by_amplifier_raw_volume": [0, 0, 1, 1, 2, 2, 3],
  "estimated_amplifier_raw_volumes": []
}
```

The lookup array's **index is the amplifier raw volume**, and its value is the corresponding TV volume. In the example, amplifier raw 0 and 1 both map to TV 0, raw 2 and 3 map to TV 1, and raw 6 maps to TV 3. Amplifier mapping bounds are therefore zero through `array length - 1`; TV mapping bounds are the first and last array values. Neither describes the devices' full physical range. There are no duplicated min/max fields, API ceiling, timestamps, sweep observations, gap evidence, interpolation anchors, or rounding methods in this file.

`estimated_amplifier_raw_volumes` identifies lookup entries that Sony should mark **≈**. It may be empty or omitted when there are no estimates. Boundary-clamped values below the first measurement display normally and need no additional metadata. Measured and boundary-clamped origins are intentionally collapsed into ordinary mapped values in the saved results.

The helper performs full measurement, gap verification, and interpolation checks during the sweep and holds working evidence only in memory. It saves the compact lookup after successful restoration; measurement evidence and starting settings are not retained permanently.

Sony validates only supported format, nonempty device identities/input, a bounded dense lookup of nonnegative integer TV values in nondecreasing order, and valid unique estimate indices. Every amplifier integer between the derived bounds has an entry by construction. Device-pair matching is checked against the running remotes before the mapping is used. Sony does not repeat the calibration experiment's verification or calculate interpolation.

The helper shows whether a completed calibration exists and summarizes its mapped range. Detailed working progress appears in the activity log; the former results table and recovery controls are removed.

**Supported format:** only format version 2 is accepted. Older formats have no migration adapter; run a new calibration to replace them.

Sony watches the runtime file through its existing event-driven file watcher and loads changes without restarting. It uses the captured input only when both configured-device fingerprints match. With no matching file it keeps `AmplifierTVInput` and the raw amplifier display. Deleting/renaming the runtime file restores that fallback; a malformed edit retains the last valid in-memory mapping and records the reload error through optional diagnostics.

Mapped display is independent of volume direction. Raw values outside the calibrated range clamp to its display endpoints; for example, amplifier raw 154 measured at TV 79 gives a maximum mapped TV display of 79. **≈** marks saved estimates (or an unexpected fractional raw reading). Mapping changes only Sony Remote's displayed volume; it does not write a matching volume to the physical TV.

Fingerprints currently identify the configured host endpoints, not hardware serial numbers. Recalibrate after replacing a device, even if its address stays the same. Only one helper can own the JSON and remote sessions. Neither mapping nor working measurements contain credentials. Abrupt process/computer termination can interrupt restoration; session leases expire automatically, and settings must be checked manually.

The helper uses PySide6 plus standard-library Windows Core Audio COM bindings. Simulated-device and IPC checks do not replace testing physical CEC and Windows audio behavior on your devices.

## TV keyboard action

Ctrl+Enter navigates the on-screen keyboard to its action button. It expects that button in the bottom right, on the fourth row.

## Amplifier volume display

State includes `volume_display` (the exact text from the existing amplifier formatter, including any dB suffix) and `volume_unit` (`raw` or `dB`). Sony uses these only without a matching calibration; calibrated display and calibration still use `raw_volume`. Sony validates the display fields without converting dB. Update both remotes together for these required fields.

## IPC command backpressure

Acknowledgements include a generic `reason`: empty on acceptance, `queue_full` for dropped overflow, or `state_changed` for rejected eligibility/input. Sony silently drops queue-full commands, including local pending/write-buffer overflow. Other failures appear in the power button tooltip prefixed “Amplifier control”, without changing TV state or opening a dialog. Commands are never replayed or redirected to the TV after an uncertain result. Update both remotes together.

## Device HTTP transport

Device requests use a dedicated urllib opener with `ProxyHandler({})`, bypassing system and environment proxies. This keeps LAN device traffic and TV authentication headers off configured proxies without modifying urllib’s global opener. Existing request timeouts and HTTP/connection exception handling are unchanged.

## TV command ordering

All TV-changing operations use a dedicated single-thread pool, with at most 16 queued/running operations. Accepted commands execute in submission order; excess commands are dropped quietly. Text submission and its navigation sequence remain one operation. Polling/discovery use the separate background pool, so command ordering does not imply TV UI completion or prevent transient stale status readings. Calibration waits for both pools to become idle, and restart/shutdown clear queued work in both pools before waiting for active requests. Failed restarts reuse the drained pools with fresh worker bookkeeping.

## Development checks

Install PySide6, then run from the repository root:

```sh
python -m unittest discover -s tests -v
```

These development-only tests do not require real devices and are not imported by the applications. Each script remains independently runnable without the tests or a shared module. Endpoint naming is centralized locally in `ipc_server_name`; the user-local socket names and wire protocol are unchanged.

Local cleanup separates runtime-document validation (`validate_runtime_mapping`) from display preparation (`prepare_volume_mapping`). The remote and helper validate the same document structure; only Sony builds its internal display lookup. `_calibration_blocked` centralizes ordinary-command guards, and `_sync_discovered_config` preserves discovered Apps/Inputs settings and JSON order through one implementation.

### Calibration regression coverage

`tests/test_calibration.py` runs the real `CalibrationSession` with injected fake peers, fake Windows mute, and a virtual monotonic clock. Device snapshots arrive at simulated 250 ms intervals; delays and timeouts advance simulated time without sleeping or contacting devices. Temporary directories isolate every mapping write.

| Scenarios | Checked outcome |
| --- | --- |
| Complete sweep; TV maximum or amplifier API ceiling | Dense lookup, correct stopping point, original settings restored before publication |
| Fine amplifier steps within one TV level; delayed/bouncing TV display | Useful raw values retained and TV-scale output preserved |
| Six-second CEC delay; ignored command; late multiple steps | No duplicate late step, quiet wait before retry, verified rollback before replay |
| Repeated no-response; lost command acknowledgement | Four-attempt bound for inconclusive steps; no replay after acknowledgement uncertainty |
| Cancel before starting, during a step, or during cleanup | Issued steps finish safely; cleanup completes; existing mapping stays unchanged |
| Stale observations, disconnection, lease rejection, changed input | No new mapping; unsafe restoration is refused and Windows remains muted where possible |
| Windows unmuted during sweep; original device/Windows mute states | Abort and restore where possible; original Windows mute restored last |
| Skipped raw values | Measured when reached on recheck; interpolation requires two verified crossings from each direction |
| Mapping boundaries and capped readouts | Lower-boundary clamp, complete monotonic lookup, lowest TV value at the API cap, estimate markers |
| Failed restoration or atomic file replacement | Existing mapping bytes remain unchanged; temporary file removed |

These tests do not exercise real Windows Core Audio, physical CEC timing, or device firmware. No calibration behavior changes were needed for this coverage release.

## Small reliability checks

IPC frame readers drain available socket data in bounded chunks, preserving incomplete frames and enforcing the 4,096-byte limit per frame rather than per burst. Configuration-change checks hash and parse the same byte snapshot. Sony applies the same rule to calibration-file reloads, retaining its last valid mapping after a malformed edit.

Windows mutex creation uses `WinDLL(..., use_last_error=True)` and the ctypes cached last-error value immediately after `CreateMutexW`. Handle release retains pointer-sized signatures. Yamaha’s published freshness uses its existing hotkey age constant; no protocol or freshness-policy change is introduced.

`tests/test_reliability.py` covers fragmented UTF-8, multi-read bursts, frame-size boundaries, invalid frames, changing/unreadable configuration files, and mocked Win32 mutex success/existing/error paths. Native Windows mutex behavior still requires Windows testing.

## Independent discovery and availability

Successful power/status polling establishes core availability independently of remote-code, application, and input discovery. Each enabled discovery feature has separate ready/pending/error state and its own background worker. Successful discoveries are cached; the existing status cycle retries only failed or unfinished discovery, without a new periodic timer. Active-input refresh uses a separate bounded-in-flight worker so it cannot hold up core status.

Buttons, dropdowns, favorites, and keyboard shortcuts use the availability of their required feature. IRCC buttons also check whether their command is advertised. Applications can remain available in standby. Discovery failures are non-modal and appear in the power/dropdown tooltips; unavailable sections recover automatically after successful retry. Text entry and direct volume/power operations do not depend on remote-code discovery. Calibration pauses discovery retries, and restart/shutdown clear pending-discovery bookkeeping after draining workers.

`tests/test_graceful_failures.py` uses offscreen Qt windows and fake devices to check isolated and combined discovery failures, delayed discovery/input refresh, feature recovery, standby, connection loss, and restart recovery. Tests disable IPC endpoints and native keyboard-hook installation.

## Second-launch activation

Window activation uses a separate user-only endpoint: `remote-control.tv.activation.v1.<user-suffix>` or `remote-control.amplifier.activation.v1.<user-suffix>`. It has its own role lock and accepts only window-activation requests, independently of device commands and calibration leases. `ipc_server_name(role, channel=...)` builds the state, calibration, and activation names locally in each self-contained script; existing state/calibration names and file formats are unchanged.

After failing to acquire the single-instance lock, a new process contacts the existing instance without loading device configuration or starting a backend. The handshake supplies the existing process ID; Windows clients call `AllowSetForegroundWindow` before requesting activation. The existing process restores its window and raises any active modal dialog, then acknowledges. OS rules still determine actual foreground focus. Calibration-disabled controls stay disabled, and activation only requests the normal status refresh associated with showing the window.

Connection retries and the handshake share a 1.5-second startup deadline. An unavailable/unresponsive endpoint produces a clear already-running message instead of a silent exit. The server allows four connected clients, bounds frames, and disconnects idle clients after one second. Activation closes before restart/shutdown draining and is recreated if restart fails; it adds no periodic device/IPC polling.

`tests/test_activation.py` launches real second Python processes against isolated local endpoints and private instance locks. It covers hidden/minimized windows, modal dialogs, calibration-disabled state, concurrent launches, delayed startup, malformed/partial requests, idle timeouts, role mismatch, duplicate endpoints, and endpoint recovery. Windows foreground-permission calls are mocked; actual desktop focus behavior requires platform testing.

## Hook startup shutdown safeguard

The hook thread checks its existing stop flag after native message-queue setup and before installation. If shutdown arrives during installation, the thread skips the started notification once it observes the flag and immediately follows its existing unhook cleanup path. Running hooks still use `WM_QUIT` to wake the native message loop. Worker draining, join limits, and restart behavior are unchanged.

`tests/test_hook_lifecycle.py` uses real Python threads with a fake Win32 API and event barriers to force shutdown before thread-ID publication and during hook installation. It also checks normal message-loop shutdown, repeated stop calls, and stopping before start. These tests exercise lifecycle ordering without relying on randomly hitting a race; actual Windows API behavior still requires Windows testing.

## Discovery saves and user configuration edits

Automatic discovery writes use the last accepted file digest as their baseline. After preparing a temporary file, they check the current configuration immediately before replacement; changed, missing, or unreadable files are left alone. A skipped save keeps the baseline unchanged so existing file watching can validate the user's edit and restart only when it is valid. Failed-restart and unknown-baseline guards also prevent discovery saves.

Successful saves return the digest of the exact UTF-8 bytes written, rather than re-reading the destination. An edit made after replacement therefore remains detectable. Default-file creation still works without an existing baseline. Both scripts retain their own implementation and atomic replacement; no shared module or additional polling is introduced.

`tests/test_reliability.py` covers ordinary discovery saves, valid and unfinished edits, deletion/read failures, edits during replacement preparation and immediately after replacement, failed-restart protection, failed replacement cleanup, and initial default-file creation. Temporary files isolate every write.

## Volume target completion

The TV volume rate limiter checks a worker's completed reading against the latest requested target before clearing it. A keypress received after the worker finishes but before its GUI callback runs therefore continues through the existing rate-limited relative-step loop. A worker that sent a volume command still requires a later observation to confirm completion. Rate settings and amplifier routing are unchanged.

`tests/test_volume_limiter.py` separates fake-device execution from GUI result delivery to force the lost-key timing. It covers up/down presses, coalesced bursts, a new target during an issued step, returning to the current volume, a TV command that does not move volume, and switching away from TV routing. It performs no real device requests.

## Interpolation rounding within a gap

For each gap between measured amplifier values, the calibration helper scores nearest-half-up, floor, and ceil against up to eight nearby held-out measured transitions, centered on the gap midpoint. It selects once and reuses that method throughout the gap; ties prefer nearest-half-up. Interpolation stays between its measured anchors, so nondecreasing anchors produce nondecreasing estimates. Different gaps can select different methods, and measured values are unchanged.

The selected rules remain temporary sweep evidence, not additional runtime JSON fields. `tests/test_calibration.py` covers the former decreasing wide-gap failure, retention of all three methods across separate gaps, and the nearest default when there are no middle observations. Existing sweep validation, gap verification, and restoration checks remain in place.
