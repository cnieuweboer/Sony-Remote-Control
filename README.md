# Sony-Remote-Control

Small PySide6 desktop remote for Sony BRAVIA TVs using the Sony BRAVIA IP Control API (only tested on XR-55X90J)

![Sony Remote](screenshots/sony_remote.jpg)

## Installation

Place these files in the same folder.

```text
SonyRemote.pyw
SonyRemote.json
```

## Windows

```powershell
py -m pip install PySide6
```

Run `SonyRemote.pyw`.

## macOS

```bash
python3 -m pip install PySide6
python3 SonyRemote.pyw
```

## Linux

```bash
python3 -m pip install PySide6
python3 SonyRemote.pyw
```

## TV setup

On the TV, open:

`Settings > Network and Internet > Home network setup > IP Control`

Set:

- `Authentication` — Normal and Pre-Shared Key.
- `Pre-Shared Key` — choose a key and enter the same value in `SonyRemote.json`.
- `Simple IP Control` — enable this if you want the remote to power on the TV.

## Configuration

```json
{
  "Host": "192.168.1.5",
  "PreSharedKey": "2468",
  "DarkMode": true,
  "Mica": true,
  "TextInputControls": true,
  "ChannelControls": true,
  "AppControls": true,
  "NavigationControls": true,
  "InputControls": false,
  "VolumeControls": true,
  "AmplifierTVInput": "",
  "PlaybackControls": true,
  "DiagnosticsEnabled": false,
  "SortByName": false,
  "Apps": {},
  "Inputs": {},
  "SystemTray": false,
  "CloseToTray": false,
  "DropdownAnimationWorkaround": true
}
```

- `Host` — IP address or hostname of the Sony TV.
- `PreSharedKey` — pre-shared key configured in the TV's IP Control settings.
- `DarkMode` — `true` or `false`.
- `Mica` — Windows-only Mica background. Ignored on macOS and Linux.
- `TextInputControls` — show the Text Input section. Text is sent to the currently active TV software keyboard or text field by pressing Enter.
- `ChannelControls` — show number buttons `0`–`9` and channel `−` / `+` controls.
- `AppControls` — show the application launcher and favorite application buttons.
- `NavigationControls` — show directional, OK, Home, and Back controls.
- `InputControls` — show the input selector and favorite input buttons.
- `VolumeControls` — show volume and mute controls.
- `PlaybackControls` — show media playback controls.
- `AmplifierTVInput` — empty by default, which disables amplifier routing. Set the exact amplifier input ID used by the TV (for example `hdmi1`, `optical1`, or `tv`). Use the input ID, not its display name.
- `DiagnosticsEnabled` — `false` by default. Set to `true` to append unexpected errors and crash diagnostics to `SonyRemote-crash.log` beside the script. Logs contain the app version, exception types, and stack locations; exception messages, source lines, request data, and local variable values are omitted. If the log cannot be opened or written, the app continues without logging.
- `SortByName` — `false` (default) keeps favorites and dropdown entries in JSON insertion order; `true` sorts them alphabetically by `NameOverride`, falling back to `Name`, ignoring case. Apps and inputs are ordered separately.
- `Apps` — settings for applications discovered from the Sony TV.
  - `Name` — name reported by the TV. This is updated automatically.
  - `NameOverride` — optional replacement name shown in the dropdown and on favorite buttons.
  - `Hidden` — hide the application from the dropdown menu. Hidden applications can still be favorites.
  - `Favorite` — show a button for the application directly in the remote.
- `Inputs` — settings for inputs discovered from the Sony TV.
  - `Name` — name reported by the TV. This is updated automatically.
  - `NameOverride` — optional replacement name shown in the dropdown and on favorite buttons.
  - `Hidden` — hide the input from the dropdown menu. Hidden inputs can still be favorites.
  - `Favorite` — show a button for the input directly in the remote.
- `SystemTray` — show a system tray icon when the desktop supports it. If unavailable, the app runs as an ordinary window.
- `CloseToTray` — clicking the window close button hides the app to the tray instead of exiting. If no tray is available, closing the window exits normally.
- `DropdownAnimationWorkaround` — suppress a Qt/Windows repaint glitch where controls underneath an opening dropdown can briefly show through the menu animation. Usually only needed on Windows.

SonyRemote automatically adds newly discovered applications and inputs to `Apps` and `Inputs` with default settings. Existing `NameOverride`, `Hidden`, and `Favorite` values are preserved. Existing entries keep their JSON positions; newly discovered entries append in device discovery order. Dropdowns only include currently discovered, non-hidden entries. Favorite buttons remain independent of `Hidden`. Name sorting affects display only and does not reorder the JSON file.

The app automatically restarts when it detects that `SonyRemote.json` has changed.

Only one instance of SonyRemote can run at a time.

Regular status polling pauses while the window is hidden or minimized and refreshes immediately when restored. Active requests finish normally; configuration file watching remains enabled.

## Windows shortcut reliability

Version `2026.10.18.3` fixes the native hook chain used when both remotes run. All Windows shortcuts still use a low-level Windows keyboard hook. Qt only observes focus changes and delivers queued commands to the GUI; it does not intercept Windows hotkeys.

The hook uses pointer-sized Win32 arguments and return values throughout, so forwarding an unclaimed key preserves its original event pointer on 64-bit Windows. A dedicated native message thread keeps forwarding responsive during GUI work and restart waits. The callback reads a plain focus/ownership snapshot and queues commands; device and IPC requests run outside it. Other apps' keys are forwarded unchanged.

A physical press keeps its original owner until release. Mute toggles once per press; volume and navigation keys can repeat while held. Commands use a bounded queue; an unaccepted new press passes through. Sony defers volume/mute keys while the amplifier's global ownership is valid, regardless of which remote started first. Hook failures are reported while the remote remains usable through its buttons.

## Local amplifier routing

Version `2026.10.18.2` and newer can cooperate with Yamaha Remote `2026.10.18.2` and newer over a user-local Qt socket. Both apps remain self-contained; neither reads the other's JSON. No additional dependencies are needed.

Set, for example, `"AmplifierTVInput": "hdmi1"` in `SonyRemote.json`. Use the corresponding key from Yamaha Remote's `Inputs` setting, not a `NameOverride`. While the amplifier is reachable, on, and using that input, Sony's volume/mute buttons and focused volume keys send commands directly to it. While routing, the volume number and mute label follow the amplifier's published state, including changes made directly through Yamaha Remote. A matching calibration supplies the TV-scale number; otherwise the number is raw amplifier volume. Its tooltip identifies the source. The TV's own stored volume remains separate.

Standby, another amplifier input, offline state, a disconnected socket, or expired status automatically restores ordinary TV volume control. When Yamaha's global volume hook is actually active, Sony passes its focused volume/mute keys through so Yamaha handles each press. This hotkey ownership applies on every amplifier input, independently of whether Sony's volume buttons route to it. Disabling global hotkeys, losing the connection, or losing valid ownership restores Sony's focused shortcuts. Other shortcuts retain their existing behavior.

Connection attempts are asynchronous and silent when no amplifier app is running. A two-second reconnect timer runs only while disconnected; no periodic IPC state requests are sent. Connected clients receive state updates from existing device polls and state events. A freshness deadline prevents continued use of stale state if the amplifier app stops responding. If Sony starts first, routing becomes available automatically after the amplifier app starts.

A routing change cancels pending TV volume rate-limit targets. An already-running TV request may still finish. Amplifier commands use its existing serialized worker and bounded queue, bypassing Sony's TV/CEC rate limiter. Rejected commands and acknowledgement failures are reported without retrying against either device, because a retry could apply a volume step twice. Acknowledgements confirm queue acceptance rather than completed device execution.

The amplifier's reported raw maximum is an API ceiling, not its physical volume limit. With a matching calibration file, Sony displays the observed TV-scale equivalent of amplifier volume, capped to the calibrated TV range. It does not write a converted volume back to the TV. Without a matching mapping it displays the amplifier raw value.

## Calibration helper — session and restoration stage

Release `2026.10.18.5` adds exclusive calibration sessions. Update **both** remotes before using `VolumeCalibration.pyw`, available in the Sony Remote repository. The helper is a separate, self-contained PySide6 application and communicates only through generic local IPC endpoints. It does not read either remote's settings or credentials.

During a session, both remotes reuse their existing polling timers at 250 ms, including while hidden. Ordinary controls are disabled and pending volume targets are cleared. Yamaha's Windows hook consumes volume/mute media keys without issuing device commands, so they cannot unmute Windows or change volume during the test. Config-driven restarts wait until the session ends. A socket-owned eight-second lease, renewed by the helper, restores normal polling and controls on release, disconnect, or helper failure. Only the owner can perform calibration operations; API work stays in the existing device workers.

The helper first captures the selected amplifier input through IPC. The input and mapping are now saved in `VolumeCalibration.json` after a successful full sweep and restoration. `AmplifierTVInput` remains a fallback setting; the calibrated input takes precedence only for the matching configured devices.

### Volume calibration helper — 2026.10.18.17

Update **VolumeCalibration.pyw** to `2026.10.18.17`. Sony Remote stays at `2026.10.18.16` and Yamaha Remote at `2026.10.18.15`. The compact runtime JSON format is unchanged, so an existing completed mapping from release 16 remains usable. Keep the helper beside `SonyRemote.pyw` so Sony watches the same `VolumeCalibration.json`.

1. Start both remotes. Select the amplifier input physically connected to the TV and start at a comfortable volume below its API ceiling, with TV/amplifier mute states agreeing.
2. Put the **TV on a silent input/source**, stop all other playback, and keep the same Windows output selected. Volume changes may automatically unmute the TV/amplifier. Windows mute does not silence TV-generated audio or other amplifier sources. The sweep can reach a **VERY LOUD** range if the source becomes active. Do not use physical device controls during calibration.
3. Confirm the silent-source checkbox. The single button offers **Start calibration** when no usable mapping exists, or **Replace calibration** when one is already saved. Replacement asks for confirmation; the existing file stays intact until the new sweep and restoration succeed.
4. While running, that same button becomes **Cancel calibration**. The activity bar signals work without claiming a percentage, and the bottom log explains the current operation. A cancellation waits for any issued command and bounded rollback to finish, then attempts restoration. During cleanup the button is disabled and reads **Restoring original settings…**. Closing the window during work also requests cancellation and waits for cleanup before allowing closure.
5. After success, cancellation, or failure, a warning and the log ask you to **check volume and mute settings on the TV, amplifier, and computer before playing sound**. Confirm the silent-source checkbox again before another sweep.

The helper always runs one complete upward sweep, starting at the TV's reported minimum and stopping at the first amplifier network API ceiling reading or the TV's reported maximum. There is no step-check-only mode, alternative restore volume, configurable sweep limit, or full downward measurement pass. Faster positioning waits for individual amplifier movements above TV minimum + 5; a five-second quiet interval resynchronizes readings before the final verified steps. Every amplifier raw change is useful even when the TV number stays unchanged.

Skipped interior amplifier integers are approached twice from below and twice from above through relative CEC commands. A value reached during rechecking becomes measured; only repeated confirmed crossings permit interpolation. Inconclusive commands are not evidence of an unreachable value. Successful calibration saves a complete lookup from amplifier raw zero through the observed maximum, with a lower-boundary clamp where needed and the lowest TV number associated with the API ceiling. The API ceiling is a network readout/control limit, not the physical amplifier maximum.

Relative commands are `+1` / `-1`. Confirmed movement must settle at the first observed amplifier value. A transient TV-only reading in the opposite direction may catch up; actual opposite amplifier movement stops the run. An inconclusive command pauses new commands for five quiet seconds (up to twenty seconds if readings keep changing). A late single raw step can be accepted without replay. Otherwise the helper restores the last accepted raw/TV pair before retrying, with an initial attempt plus three retries. Larger delayed changes are rolled back, and rollback movements do not count as measurements. Rollback is bounded to sixteen correction commands / three minutes, with no more than four consecutive no-progress commands.

Starting settings are held **only in memory for the current run**. After success, cancellation, or failure, the helper attempts to restore the exact starting amplifier raw volume and matching TV level, then the original mute states, restoring Windows mute last. Restoration uses faster movement while more than five TV levels and five raw units from the target, then resynchronizes and verifies the final approach. If restoration cannot be verified, Windows stays muted where possible and the helper asks you to restore settings manually. A new run always starts from current settled settings; it does not resume or repair an interrupted run.

The previous completed mapping is atomically replaced only after the full sweep and restoration succeed. Failed/cancelled working measurements are discarded. No recovery/draft journal is created or read, and no persistent recovery can block a new sweep. A leftover `VolumeCalibration.session.json` from release 16 is ignored and may be removed. Force-closing the process or shutting down the computer can prevent automatic restoration; check device and computer volume/mute manually before restarting playback. The remotes' socket-owned leases still expire and restore ordinary controls after a disconnected helper.

The upward pass and positioning/restoration seeks are limited to 512 successful steps and fifteen minutes each. Gap verification has a shared limit of 2,048 steps and thirty minutes. No new periodic IPC status requests are added; existing remote device timers provide observations. Calibration requires integer amplifier raw values and an integer API ceiling between 1 and 10,000.

### Mapping file and results

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

**Supported format:** the compact format introduced in release 16 is unchanged and remains usable; do not delete a completed version-2 file just to update the helper. Formats predating release 16 are intentionally unsupported; remove such an older file before running a fresh sweep. There is no migration adapter or legacy directional lookup code. Neither remote needs an update for this helper release, and no separate session journal is used.

Sony watches the runtime file through its existing event-driven file watcher and loads changes without restarting. It uses the captured input only when both configured-device fingerprints match. With no matching file it keeps `AmplifierTVInput` and the raw amplifier display. Deleting/renaming the runtime file restores that fallback; a malformed edit retains the last valid in-memory mapping and reports the reload error in the volume tooltip (and optional diagnostics).

Mapped display is independent of volume direction. Raw values outside the calibrated range clamp to its display endpoints; for example, amplifier raw 154 measured at TV 79 gives a maximum mapped TV display of 79. **≈** marks saved estimates (or an unexpected fractional raw reading). Mapping changes only Sony Remote's displayed volume; it does not write a matching volume to the physical TV.

Fingerprints currently identify the configured host endpoints, not hardware serial numbers. Recalibrate after replacing a device, even if its address stays the same. Only one helper can own the JSON and remote sessions. Neither mapping nor working measurements contain credentials. Abrupt process/computer termination can interrupt restoration; session leases expire automatically, and settings must be checked manually.

The helper uses PySide6 plus standard-library Windows Core Audio COM bindings. Simulated-device and IPC checks do not replace testing physical CEC and Windows audio behavior on your devices.

## Text input

- **Enter** — Sends the text to the TV.
- **Ctrl+Enter** — Sends the text and automatically activates the TV keyboard action, such as **Search** or **Go**.
- **Shift+Enter** — Adds a new line instead of sending the text.

> **Note:** Ctrl+Enter cannot directly trigger the TV’s **Search**, **Go**, or similar action. It works by automatically navigating the on-screen keyboard to that button, so it depends on the keyboard using the expected layout. With that button in the bottom right, on the fourth row.

## Third-party code

SonyRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
