# Implementation details

For setup and everyday use, see the [README](README.md). Dated changes are in the [release history](CHANGELOG.md).

## Focused shortcut handling

Navigation and focused playback shortcuts use an application-wide Qt event filter on every platform. Windows volume/mute use a dedicated low-level hook; other platforms use the Qt filter for volume too. It handles mapped keys before buttons and comboboxes, preserves text editing and popup navigation, and retains physical-press ownership until release. Sony yields focused volume/mute to valid amplifier global ownership; optional Windows SMTC remains a separate playback path. See [Cross-platform focused shortcuts](#cross-platform-focused-shortcuts-202610213) for event ordering and regression coverage.

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

`tests/test_graceful_failures.py` uses offscreen Qt windows and fake devices to check isolated and combined discovery failures, delayed discovery/input refresh, feature recovery, standby, connection loss, and restart recovery. Tests disable production IPC endpoints while retaining Qt shortcut handling.

## Second-launch activation

Window activation uses a separate user-only endpoint: `remote-control.tv.activation.v1.<user-suffix>` or `remote-control.amplifier.activation.v1.<user-suffix>`. It has its own role lock and accepts only window-activation requests, independently of device commands and calibration leases. `ipc_server_name(role, channel=...)` builds the state, calibration, and activation names locally in each self-contained script; existing state/calibration names and file formats are unchanged.

After failing to acquire the single-instance lock, a new process contacts the existing instance without loading device configuration or starting a backend. The handshake supplies the existing process ID; Windows clients call `AllowSetForegroundWindow` before requesting activation. The existing process restores its window and raises any active modal dialog, then acknowledges. OS rules still determine actual foreground focus. Calibration-disabled controls stay disabled, and activation only requests the normal status refresh associated with showing the window.

Connection retries and the handshake share a 1.5-second startup deadline. An unavailable/unresponsive endpoint produces a clear already-running message instead of a silent exit. The server allows four connected clients, bounds frames, and disconnects idle clients after one second. Activation closes before restart/shutdown draining and is recreated if restart fails; it adds no periodic device/IPC polling.

`tests/test_activation.py` launches real second Python processes against isolated local endpoints and private instance locks. It covers hidden/minimized windows, modal dialogs, calibration-disabled state, concurrent launches, delayed startup, malformed/partial requests, idle timeouts, role mismatch, duplicate endpoints, and endpoint recovery. Windows foreground-permission calls are mocked; actual desktop focus behavior requires platform testing.

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

## Dropdown animation workaround

Closed comboboxes retain Qt's native dropdown arrow. The styled arrow area is transparent and borderless, avoiding a separate inset button; text uses symmetric space reservation to remain centered across the whole control. The arrow follows the active Qt/platform style, including disabled states, with no image assets.

The Windows popup fill can lag a frame behind its outline and shadow during native animation, making underlying text appear to flicker. The live-view animation addresses that rendering mismatch.

`DropdownAnimationWorkaround` enables a 200 ms OutCubic animation inside the real Qt popup. Sony keeps the actual list and its scrollbars live at their final size, temporarily suspends the popup layout, and grows the native popup frame using elapsed time with an 8 ms precise timer. The full-size list slides within the clipping popup; keyboard navigation and wheel scrolling continue during opening. Cleanup restores the original layout-enabled state, including after native dismissal. Underlying favorites and navigation buttons remain untouched. Qt's animation preference is restored immediately after opening; if combo animations are disabled, the popup appears without the custom animation. Disabling the workaround retains the ordinary Qt popup.

The final popup rectangle comes directly from Qt, including screen-constrained placement for long lists. Upward/downward growth stays inside that rectangle. Finishing or interrupting the animation restores the native geometry and layout, removes the event filter, and stops the timer. Navigation and scrolling remain live throughout opening; selecting or dismissing the popup performs normal cleanup. The centered text/delegate, native frame/shadow, selection signals, and active-popup identity used by shortcut blocking are retained. The implementation is adapted from the supplied live-view comparison prototype and remains self-contained.

`tests/test_dropdown_animation.py` exercises selection, early keyboard input, Escape and native dismissal, repeated opening, owner close/reopen, long-list navigation and both placement directions, disabled animations/workaround, empty lists, and preference restoration on failure. Offscreen tests verify Qt behavior and geometry; the Windows-specific flicker fix and native shadow/animation appearance require desktop verification.

## TV target display and held volume buttons

All TV volume repaints use the pending requested target until it is completed or reconciled, including repaints caused by unrelated amplifier IPC updates. Background polls capture the volume input generation/target and whether volume work was pending: superseded poll results are ignored without renewing status freshness, and polls started during pending work cannot clear the target. Actual volume remains separate from the display target; matching polls can still refresh mute. Without rate limiting, queued/running volume commands are counted and a subsequent idle poll reconciles the display. Rejected queue submissions roll back the unused target, and lifecycle draining resets pending counts.

The volume buttons use Qt's built-in auto-repeat: an immediate step on press, a 500 ms hold delay, then a step every 100 ms. Release adds no extra step and stops repetition; an existing rate-limited target continues to completion. The same action path retains TV rate limiting, amplifier routing, and calibration blocking. `tests/test_graceful_failures.py` covers target repaints, late polls, unlimited command completion/rejection, both hold directions, short clicks, disabling held buttons, and reaching a target after release.

## Starting in the system tray

`--start-in-tray` leaves the newly constructed window hidden when both `SystemTray` and `CloseToTray` are true, an icon was created, and the tray is still available. It never shows/minimizes the main window first. Initialization, configuration feedback, IPC, and visibility-aware polling retain their existing paths; Yamaha global control or connected TV clients can still keep hidden-window polling active. Missing/disabled tray support falls back to an ordinary visible window.

The switch is checked after single-instance activation, so a second launch restores the existing window regardless of the flag. Configuration-driven replacement already forwards command-line arguments and retains the switch. `tests/test_tray_startup.py` checks the visibility/fallback gates, startup feedback, and argument forwarding without live tray icons or devices; `tests/test_activation.py` also exercises a real second process with the switch.

## Amplifier protocol contract checks

Protocol version 1 acknowledgements have integer `id`, boolean `accepted`, and an optional `reason` (`""`, `queue_full`, or `state_changed`). Missing reasons are treated as empty; rejection without an explanation receives generic feedback. Queue-full drops stay silent and commands are never replayed on uncertain delivery. Sony records invalid messages in optional diagnostics with duplicate suppression and displays brand-neutral incompatibility feedback.

Amplifier feedback is stored separately from discovery feedback and survives TV polling. Connection-loss feedback clears when a fresh valid amplifier state arrives again. Timeout, rejection, and other command feedback remain until a pending amplifier command receives an accepted acknowledgement; receiving state does not resolve uncertain command delivery. Error categories are explicit and do not depend on message wording. Acceptance means queued by the amplifier remote, not confirmation that the physical device completed the command.

Both repositories keep identical `tests/fixtures/amplifier_protocol_v1.json` wire examples. Tests compare Yamaha's actual snapshots and acknowledgement production with those examples and feed the same messages through Sony's actual consumer. Fixtures and tests are development-only; the scripts remain self-contained. Changes to the wire contract should update and verify both copies together.

## Local configuration files

`SonyRemote.example.json` is the tracked connection-settings template, with an empty host and pre-shared key. `SonyRemote.json` is local and ignored by Git; the script generates defaults and prompts for missing settings when it is absent. Crash logs and temporary JSON files are also ignored. Existing local configuration bytes are preserved when removing the file from Git tracking; no Git history is rewritten.

## Volume follow-up polling

With volume rate limiting disabled, only the final pending volume-command completion schedules a status refresh after 200 ms. The callback checks the volume input generation and that volume work is still idle before polling, so a newer press suppresses an obsolete follow-up even if its command has already finished. The latest idle completion still reconciles the target with the TV, and regular periodic polling is unchanged.

## Optional Windows SMTC playback keys

`SMTCPlaybackKeys` defaults to false. Only when enabled on Windows does `SMTCPlayback` lazily import PyWinRT and bind `get_for_window(int(winId()))`; creating the handle does not show the window, so registration also works with `--start-in-tray`. Optional packages are `winrt-Windows.Media`, `winrt-Windows.Media.Interop`, and `winrt-Windows.Foundation` (their runtime dependency is resolved by pip).

`_playback_key_mode()` selects focused, SMTC, or unavailable. The Qt shortcut eligibility check and final focused dispatch both exclude Play/Pause, Play, Pause, Stop, Next, and Previous whenever SMTC is selected, including when its initialization fails. There is no automatic focused-shortcut fallback. Volume/mute ownership, amplifier IPC, and volume calibration routing are separate and unchanged. On-screen playback controls continue using the ordinary command path; `PlaybackControls` controls their visibility independently of the key-routing option.

`_refresh_playback_routing()` advertises only discovered explicit TV commands while the TV is active, calibration is inactive, and no modal dialog blocks interaction. Missing explicit Play/Pause commands are not replaced with blind toggles. Only the existing status timer is kept active while hidden/minimized when the SMTC backend is available; standby/offline/discovery recovery uses the existing state updates. No new device or IPC polling timer is added.

WinRT callbacks only read the button and emit a queued Qt signal. The GUI rechecks availability and sends playback commands through the same bounded, serialized TV command pool as the on-screen buttons. SMTC command errors use non-modal background feedback. Playing/Paused/Stopped are optimistic routing hints updated when commands are queued, including on-screen Play/Pause; they do not claim to reflect independently observed TV playback. Windows chooses which participating media session receives a media key.

Pausing for restart/shutdown disables the SMTC session and removes the event subscription before waiting for workers. Cleanup attempts unsubscribe even if disabling fails. Event generations discard callbacks queued before a session was retired; restart-launch failure re-registers the existing window. Missing dependencies or SDK errors disable participation, retain other controls, and show one warning per process with installation/fallback instructions; optional diagnostics retain exception types without request contents.

`tests/test_smtc_playback.py` uses a fake WinRT API with real Qt event delivery and the device-free TV fixture. It covers supported buttons including Stop, callback thread handoff, optimistic hints, stale event rejection, dependency/registration/cleanup failures, focused-key exclusion, hidden polling, unavailable TVs, calibration, hidden startup, and restart recovery. Windows media-session arbitration still needs desktop verification; it cannot be exercised by offscreen Linux tests.


## Calibration speaker isolation

Calibration-only amplifier snapshots include generic `speaker_outputs`, confirmed `speaker_state`, and `speaker_valid_for_ms` facts. Freshness is tracked separately from main status; optimistic selector targets are never published as confirmed speaker isolation. Ordinary amplifier state messages and mapping JSON are unchanged.

The exclusive calibration lease permits a serialized `set_speakers` operation. It uses the existing disable-first API verification and publishes the confirmed result before acknowledging success. Uncertain commands are not replayed. Unsupported controls, an explicit rejection, or failure to verify isolation offers one Cancel-default Continue/Cancel decision per sweep. Continuing applies for the entire session; later speaker changes are logged without more dialogs. Connection, lease, identity, and volume failures remain fatal.

The helper keeps Windows muted and all sources must remain silent: speaker OFF does not guarantee isolation of TV speakers, headphones, or other outputs. While the decision is open, its worker keeps reading state and renewing leases; user decision time is excluded from volume-step deadlines. Cleanup restores and verifies original volumes and mute before intentionally restoring original speakers, then verifies the combined state and restores Windows mute last. A volume-restoration failure does not re-enable speakers; failed cleanup leaves Windows muted where possible and preserves the previous mapping. Original speaker settings live only in session memory, with no recovery journal or resume workflow.

Fake-device session tests cover unsupported isolation, rejection, repeated loss with one decision, long decisions with live lease renewal, communication failure, cancellation, and restoration failures. Qt tests cover the warning and worker handoff; amplifier tests cover confirmed snapshots and the leased command. Physical speaker switching during a full sweep still requires device verification.


## Window state across configuration restarts

Configuration replacements receive one-shot internal arguments containing Qt's saved window geometry and current visible/minimized/tray state. Prior restart arguments are replaced rather than accumulated, and unrelated startup arguments are preserved. Qt restores screen geometry without platform-specific window-manager code; a hidden window opens visibly if tray support was disabled or is unavailable. Initial launches still honor `--start-in-tray`. Focus is left to the operating system; no forced activation is requested. Window size remains governed by the current control layout.


## Native Qt light/dark appearance

The application requests `QStyleHints.setColorScheme()` before constructing its window, using `DarkMode` to select Dark or Light. This requires PySide6/Qt 6.8 or newer. The platform style and application palette remain Qt-managed, including system-theme notifications. The application palette-change signal reapplies layout styling so the stylesheet layer cannot retain resolved colors from the old palette; no Fusion style or fixed light/dark color palette is installed. The previous DWM dark-title-bar override is removed so Qt owns native theme selection. Stylesheets retain font sizing, section outlines using the palette's mid role, and optional transparent Mica surfaces, without overriding button, text, selection, hover, or disabled colors. The existing centered combobox painting and live popup animation remain in use. Platform support varies; the initial target is Windows, with native appearance and live Windows theme switching checked manually.


### Native theme follow-up

`DarkMode` explicitly selects the app scheme; switching the desktop scheme does not replace that selection. The palette-change callback only refreshes Qt-supplied colors cached by stylesheet processing. Compact 5 px vertical/7 px horizontal padding restores the former control sizing without overriding native control colors. When Windows Mica is active, section backgrounds again use the previous translucent dark/light fills; otherwise sections inherit the platform surface.

With Windows Mica active, the power/standby button also uses an opaque dark/light fill and matching hover/pressed fills to prevent the backdrop showing through. This override is scoped to its object name; other buttons remain native.


With Windows Mica active, all buttons, comboboxes, and text input now share the former dark/light control colors and borders, using opaque fills to prevent backdrop effects within controls. Hover, pressed, disabled, and dropdown selection states are explicitly styled; dropdown surfaces use the previous 40/250 grayscale colors. This replaces the narrower power-button override. Non-Mica configurations keep Qt-managed control palettes.


### Calibration isolation decision deadline

The helper allows five minutes of real monotonic time for an isolation decision, with Cancel still the default. The worker continues pumping peer state and renewing leases while waiting; the prompt duration is excluded from sweep/volume-step deadlines. Expiry logs “Speaker isolation decision timed out” and raises cancellation. A timeout during the sweep enters the existing single restoration pass and closes the dialog; one during restoration ends cleanup as unverified, closes the dialog on completion, preserves the mapping, and leaves Windows muted where possible. No restoration retry or second isolation prompt is introduced.

Initial warnings distinguish missing speaker-support fields (suggest updating the amplifier remote), an explicit empty capability list (no controllable outputs), and unavailable/stale/invalid current state. Existing lost-isolation, command-rejection, and verification-timeout explanations remain distinct. Malformed information is not described as merely an older remote. Dialog/worker tests exercise the actual Qt timeout-close handoff both before and during cleanup; fake-device tests cover reason selection, cancellation restoration, and cleanup-time expiry.


### Early Qt compatibility check

Immediately after creating QApplication, startup checks for `QStyleHints.setColorScheme` before acquiring the instance lock or reading device configuration. If unavailable, it displays the PySide6 6.8+ requirement and upgrade command, then exits without starting device work. Feature detection avoids version-string parsing and silent theme fallback.

### Protocol feedback recovery

The amplifier consumer distinguishes errors validating state messages, errors validating acknowledgements, and unattributed framing/version/unknown-message errors. A fresh valid state clears only connection and state-protocol feedback. A valid acknowledgement for a pending command clears ack-protocol feedback; a rejection can then replace it with command-rejection feedback, while queue-full remains silent. Unattributed errors retain the existing accepted-command recovery rule. Expired state and unsolicited acknowledgements cannot prove recovery. No recovery path resends or reroutes commands.

Dropdown popups expand one Qt logical pixel left and right after native placement, without changing combobox button geometry. The expanded popup and live view geometry are captured before the custom animation starts and retained at completion; the expansion also applies with the workaround or UI animations disabled.

The Apps combobox alone enables clear-on-close: a popup hide/close schedules clearing both its selected index and the view selection/current index after activation delivery. Native dismissal is observed even after animation ends or when animation is disabled. A queued clear skips a popup already reopened; input/speaker selectors retain their device-backed state.


## Cross-platform focused shortcuts (2026.10.21.3)

Sony uses its application-wide Qt event filter for navigation and focused playback on every platform. The Windows volume-only exception added in 2026.10.21.6 is described below. The filter handles ShortcutOverride, KeyPress, and KeyRelease before focused buttons/comboboxes can navigate or click. Key maps include navigation, Insert/Delete, keypad digits/channel keys, volume/mute, and explicit Play/Pause/Stop/Next/Previous. Ctrl/Alt/Meta combinations are left to normal application/desktop handling.

A physical press records its initial owner until release. Navigation and volume repeats dispatch while their current context remains eligible; Mute and Play/Pause dispatch once per press. Synthetic repeat releases do not release ownership, and auto-repeat cannot acquire a key whose initial press belonged elsewhere. Owned events are consumed even if routing eligibility changes, preventing a held press from falling through to another Qt handler. Focus loss, hiding, and lifecycle pause clear press state.

Open popups retain navigation and the entire Enter press, including delivery after dismissal until release. Text input retains non-volume editing keys. Inactive/minimized windows, modal dialogs, shutdown, and local or amplifier calibration block dispatch. Unknown device commands are not claimed. Amplifier global ownership releases only overlapping focused key combinations; optional SMTC selection excludes focused playback keys even when its backend is unavailable. SMTC remains Windows-specific; amplifier global bindings have higher priority.

Palette/GUI events refresh SMTC availability and Windows volume ownership on the Qt thread. The Windows volume-only hook has its own bounded queue and lifecycle tests; Yamaha's separate global-volume hook handles its configured bindings.

`tests/test_focused_shortcuts.py` uses real Qt application-filter delivery to buttons, comboboxes, text input, popups, and a competing QShortcut. It covers press/release and repeat ownership, popup Enter forwarding, media commands, rapid volume input, SMTC/amplifier ownership, calibration/modal/focus guards, and the Windows/non-Windows dispatch branches. Offscreen Linux tests cannot establish Windows OS-level media-key suppression or physical keyboard-wheel behavior; these require desktop verification, including both remotes running together.


### Focused Windows volume fallback (2026.10.21.6)

Qt media-key registration and WM_APPCOMMAND suppression did not prevent Windows volume changes on the tested desktop. Both approaches are removed. Sony restores its previously working low-level keyboard-hook implementation, narrowed to the three volume virtual keys (Up/Down/Mute). Navigation and playback continue through the Qt application event filter; Windows volume events are explicitly excluded from that filter even if hook startup fails, avoiding double dispatch. On hook failure, the UI reports the issue and on-screen volume controls remain usable.

The native callback consumes keys only for Sony's foreground window when popup/modal/calibration/shutdown guards allow it and valid amplifier global ownership does not apply. It reads a plain lock-protected snapshot; device calls and IPC stay on the GUI/worker paths. An accepted press remains consumed through release and auto-repeat, mute fires once per press, and already-held/unclaimed keys remain forwarded. A full 32-entry native queue drops excess steps silently instead of letting them alter Windows volume. The GUI drain rechecks focus and calibration/popup/modal state, then uses the ordinary volume/mute methods.

Pause/restart blocks interception and clears pending work while the hook keeps its native message loop responsive. Restart-launch recovery republishes ownership; final shutdown stops the hook using the existing bounded join and callback-retention safeguards. There is no Sony global-volume mode. A focused volume hook on Windows is the intentional platform exception; SMTC playback and Yamaha global volume remain separate.

`tests/test_native_volume.py` covers the actual native callback's return values for both key edges, 100 rapid presses, repeat ownership, volume-only allowlisting, queue overflow, foreground/lease ownership, Qt-volume exclusion, and pause/recovery. `tests/test_hook_lifecycle.py` restores startup/stop race coverage with a fake Win32 API and real threads. Qt focused-shortcut and SMTC tests remain; actual Windows suppression still requires desktop verification.


## Fractional calibrated volume display

The saved runtime mapping still contains integer TV values for each amplifier raw value. During display preparation, Sony groups consecutive raw values with the same TV integer T and assigns raw position i within a group of N values the representative display T+i/N. Each bucket starts at its integer; singleton buckets remain integers. Fractions are rounded half-up to tenths using integer arithmetic and capped at .9 to prevent large buckets rounding into T+1. The grouping uses only the exact bucket, independent of neighboring TV values or gaps. This is display preparation, not a change to the calibration sweep or measured integer mapping.

Prepared entries retain their integer `tv` and estimate source and add an in-memory `display_tenths`. Raw lookup clamping uses the prepared endpoint display, including the final fractional bucket. Whole display values omit .0; existing estimated-raw and fractional-raw approximation markers and the “Calibrated TV scale” tooltip remain unchanged. Uncalibrated amplifier raw/dB display, native TV values, commands, IPC, and persisted JSON are unchanged; existing mappings work without recalibration.


### TV power gates the volume UI

Volume/mute button availability requires an initialized, active TV and a running remote, independently of amplifier routing eligibility. Standby/offline updates immediately repaint the volume placeholder and “Mute”; amplifier IPC updates and cached mute/volume values cannot overwrite that inactive display. Once a fresh TV status reports active, ordinary amplifier/TV routing supplies the displayed value and mute state again. Device state tracking and routing ownership remain separate from this UI presentation rule.


## Rate-limited volume read ownership

While the TV volume rate-limit loop or one of its steps is active, normal status cycles skip `getVolumeInformation` but continue power and input polling. They check ownership both when scheduled and after the power request, avoiding an audio read when rate limiting starts during that request. The limiter is never delayed by an ordinary poll or by a recent-read cooldown; a volume request already in flight may finish.

Normal poll callbacks always process their power result. Audio is accepted only if the request generation/target still matches, the limiter no longer owns reads, and its observation time is not older than the last accepted volume response. Limiter reads update cached volume/mute and their observation timestamp, repaint through the existing target-preserving UI path, and publish cached state without additional device reads. Late results from cancelled/routed-away loops are ignored. Completion/failure resumes normal audio polling through the existing follow-up; standby cancels the limiter, and in-flight steps retain ownership until their callback finishes.

`_status_observed_at` records the actual power-response time and `_volume_observed_at` the actual audio-response time, captured before any subsequent command. An active-TV calibration snapshot uses the older observation for its combined freshness. Limiter reads cannot refresh old power facts, nor can power-only polls refresh old audio facts. The mapping, IPC schema, limiter interval and relative-command behavior remain unchanged.

Tests cover audio-read counts during ownership, continued power/input polling, limiter startup during a power read, no waiting behind an existing poll, target/mute presentation, stale callback delivery, cancellation/failure/resumption, and combined freshness.


## TV audio reads during amplifier routing

Ordinary status cycles omit `getVolumeInformation` while amplifier routing is active. Power/input polling continues, and calibration explicitly bypasses this optimization to capture and verify physical TV volume/mute. Routing is rechecked after the power request; generation changes prevent a response from before a routing transition from overwriting current presentation. Power-only cycles still repaint the amplifier display when the TV wakes.

On amplifier disconnect, expired state, standby, input change, or a mapping change that ends routing, the remote invalidates cached physical TV audio and requests an immediate normal status refresh for an active TV. If a status poll is already running, a pending flag schedules the refresh at its completion without waiting for the periodic timer. Hidden windows retain the flag until normal visibility/SMTC polling permits a refresh. No new polling timer is added.

The volume placeholder and disabled controls remain until a fresh TV audio response is accepted, preventing commands based on stale cached volume; mute also returns to its ordinary TV value. Routing back to the amplifier or lifecycle pause clears the pending request. A failed refresh follows existing offline/error handling and normal retries. Tests cover read counts, disconnect/input-change fallback, an existing in-flight poll, routing changes during power/audio reads, hidden fallback, calibration, and TV wake display.


## Sony polling coordinator

`PollingCoordinator` in `SonyRemote.pyw` owns ordinary status scheduling. Its `update()` applies visibility, SMTC background mode, configuration, and calibration eligibility and selects the 2000/250 ms periodic cadence. Reapplying an unchanged interval does not reset the periodic timer.

Power, volume/mute (one combined API response), and inputs have separate freshness timestamps and pending refresh records. On each ordinary tick, `periodic_groups()` excludes groups with a queued command, scheduled refresh, or response newer than half the normal interval (one second). Other groups still run. This uses the existing periodic timer, not individual group timers or deadline-driven periodic rescheduling; ordinary gaps can approach three seconds around an event refresh. Explicit refreshes and 250 ms calibration polling bypass freshness.

`request(delay_ms, condition, groups)` immediately reserves affected groups, invalidates their previous freshness, and schedules/coalesces their follow-up with one single-shot timer. Commands reserve their groups before entering the serialized command queue, then start the settling delay after completion, including failures with uncertain device-side outcomes. Rejected queues release the reservation. Volume batching/limiting uses its existing active-work flags and a volume-only reconciliation request; mute is also volume-only. Power, input selection, and app launch retain their full status follow-up because changing sources or waking the TV can affect audio as well. Discovery input responses also contribute actual observation timestamps.

A successful response records the time immediately after its API read, not the time a refresh was scheduled or a later GUI callback ran. Failed/cancelled follow-ups do not mark data fresh and leave periodic retries available. Offline transitions invalidate all freshness. Pending reads wait behind an active status cycle; input refreshes additionally wait behind existing input status/discovery work. `finished()` releases deferred work. Superseded conditional volume refreshes cannot cancel unconditional verification, and newer command completions receive their settling delay.

`pause()` stops periodic/delayed timers and discards pending command/refresh reservations; restart recovery uses `resume()` to re-evaluate current state and request fresh status. The window's `poll_status(groups)` still performs device reads and handles results; `reads_volume()` and `reads_inputs()` centralize read eligibility. The volume limiter retains its independent command/feedback timer and never waits on the ordinary coordinator. Discovery retries and calibration command/lease coordination remain separate from recurring status polling. This remains one self-contained script with no new runtime file.


## Time units and live timer coverage

Python-side deadlines, ages, retry sleeps, and timeout budgets use seconds. Convert at Qt calls and IPC fields that require milliseconds; existing documented JSON millisecond settings retain their units. The activation helper accepts `timeout_seconds` (default 1.5) and converts its remaining budget only at Qt socket waits. Polling freshness remains explicitly half the normal interval: `POLL_INTERVAL_MS / 1000 / 2`.

`STATUS_MAX_AGE_SECONDS` is the six-second runtime observation-age policy. `remaining_validity_ms(age_seconds, max_age_seconds)` clamps expired/future observations and converts the remaining duration to whole wire milliseconds. `IPC_MAX_STATE_VALIDITY_MS` separately names the protocol-v1 validation cap; changing runtime policy does not silently change the accepted wire format.

The real-window test in `test_graceful_failures.py` temporarily shortens the periodic interval and freshness window, schedules a volume refresh between ticks, and uses real Qt timers and worker dispatch. It checks pending suppression on the first tick, recent-response suppression on the next, eventual periodic volume resumption, and continued power/input reads. No manual periodic dispatch or mocked `poll_status()` is used. Time-unit tests cover validity boundaries and the activation retry budget; existing real-socket activation tests cover successful handshakes.


## Long display names

Closed combobox labels use Qt font-metric right elision within the existing centered edit rectangle. Popup view/delegate rendering and animation are unchanged. Combobox horizontal size policy ignores content length so long names do not enlarge the layout.

`FavoriteButton` preserves the complete QPushButton text and existing click targets, but uses an ignored horizontal size hint to keep the two stretched grid columns equal. Its native style option receives a right-elided label sized to the style's content rectangle, preserving platform styling, padding, focus, hover, and disabled states. No fixed character limit or JSON truncation is introduced; the wider combobox naturally displays more characters. Offscreen tests check actual painted labels, unchanged short labels, stable window/column widths, retained data, and reused click targets.


## Configurable focused shortcuts

`Shortcuts` maps action names to lists of Qt `QKeySequence.PortableText` single-key combinations. For example, `"VolumeUp": ["Ctrl+Alt+Up", "F8"]` replaces its existing focused bindings, and `"Mute": []` disables the focused mute shortcut. An empty string is also accepted as disabled and normalized to an empty list. Full defaults are in the example JSON. Missing sections/actions are merged and persisted on startup after full validation, using the original file digest to protect concurrent edits. Invalid edits are rejected before restart, leaving the current instance running.

Useful key names: `A`, `F8`, `Left`, `Up`, `Return`, `Enter`, `Ins`, `Del`, `Backspace`, `Num+0`, `Num++`, and `Num+-`. Media names are `Volume Up`, `Volume Down`, `Volume Mute`, `Media Play`, `Media Pause`, `Toggle Media Play/Pause`, `Media Stop`, `Media Next`, and `Media Previous`. Combine modifiers with `+`, for example `Ctrl+Alt+Up`; use separate list entries for alternatives, not comma-separated multi-stroke sequences. These names are parsed by Qt, not numeric Windows virtual-key codes; see [Qt's key-sequence documentation](https://doc.qt.io/qt-6/qkeysequence.html#detailed-description).

Validation rejects unknown actions, malformed values, unknown keys, multi-stroke sequences, and duplicate combinations across or within actions. Keys are matched with modifiers and keypad identity; shifted printable punctuation can match its literal symbol when no explicit Shift binding exists. Button tooltips use the configured sequences in Qt's native display notation, and disabled actions have no shortcut tooltip. Default dictionaries are deep-copied so nested binding lists cannot leak between instances.

Qt application filters retain popup/text-editing guards, press ownership, and toggle repeat suppression. Sony’s Windows hook watches physical volume keys; Yamaha’s also watches configured global bindings. Both resolve focused physical volume actions from the compiled configuration, and preserve press/release suppression, overflow dropping, and the action captured on keydown even if modifiers change. The callback reads plain compiled mappings and GUI-published state; it performs no Qt, device, or file I/O. Platform key-code translation remains separate from focused user bindings. No shared runtime module or new dependency is required.

Sony exposes navigation, channel digits/up/down, focused transport, and volume/mute actions. `Previous` maps to the TV's previous-track action; `Return` means Back, `Confirm` means OK, and `TvInput` means the physical Input key. Default numpad Enter/navigation aliases are explicit JSON entries. Physical transport keys remain reserved to SMTC when enabled; custom focused combinations still dispatch transport actions. Sony yields only key combinations advertised as actively owned by the amplifier.


## Configurable global volume ownership

The amplifier's `GlobalShortcuts` uses the same Qt PortableText syntax as `Shortcuts`, but accepts only `VolumeUp`, `VolumeDown`, and `Mute`. Each action defaults to an empty list; missing entries are filled and persisted, and `[]` or `""` disables that global action. The retired `GlobalVolumeHotkeys` setting is removed on save; an old `true` value becomes the three standard volume media bindings when no explicit global profile exists. Explicit bindings always win.

Windows global bindings support letters/digits, printable keyboard-layout characters, F1–F24, common navigation/editing keys, keypad keys, physical volume keys and Next/Previous/Stop/Toggle Media Play/Pause, with Shift/Ctrl/Alt/Meta modifiers. Unsupported keys, multi-stroke sequences, duplicate combinations and native aliases are rejected before restart. Cross-profile overlap is intentional. Native VK/modifier mappings are compiled on the GUI thread; the hook performs only plain-state lookups, bounded queueing and queued signals. Held presses keep their captured action through repeat and release; mute does not repeat, and queue overflow is consumed without leaking to Windows. Startup does not acquire already-held keys. Calibration still reserves physical volume keys for mute safety.

IPC state adds optional `global_shortcuts`, a bounded list of combined Qt key/modifier integers for currently active bindings. These are generic protocol values, not another app's JSON settings. `global_hotkeys_enabled` is derived from configured bindings; `global_hotkeys_active` additionally requires an installed hook, fresh on/online status, and no blocking popup/modal. Standby, offline status, stale state, shutdown or unavailable hooks release ordinary global ownership. Existing polling stays active while global control is configured, including in the tray; no IPC polling is added.

Priority is **amplifier global bindings → SMTC playback media keys → focused shortcuts**, per overlapping physical combination. The amplifier publishes facts without depending on the TV's SMTC setting. The TV yields only overlapping focused bindings and withdraws affected SMTC actions while ownership is active; owning Toggle Media Play/Pause withdraws both Play and Pause. Custom focused playback bindings on physical volume keys remain independent of SMTC. State updates and lease expiry restore eligibility. Older amplifier messages without the list retain their physical-volume-key ownership behavior; update both remotes for per-binding support.

SMTC remains a toggle for Windows-managed playback actions, not a configurable keyboard backend. Global volume bindings are Windows-only. On other platforms the focused Qt path remains available. Test suites cover configuration, per-key priority, native repeats/releases/overflow, expiry and availability; an optional cross-repository local-socket test uses both real protocol implementations when both checkouts are present. Physical keyboard suppression and Windows SMTC arbitration still need desktop verification.
