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

### Full calibration sweep and results — 2026.10.18.10

Update **YamahaRemote.pyw, SonyRemote.pyw, and VolumeCalibration.pyw** to `2026.10.18.10`. Keep the helper beside `SonyRemote.pyw` so its separate `VolumeCalibration.json` is in the location Sony watches. Neither remote reads the other remote's settings. Both scripts and the helper remain self-contained.

1. Start both updated remotes and select the amplifier input physically connected to the TV. Start below the amplifier API ceiling, with TV/amplifier mute states initially agreeing.
2. Set the **TV to a silent input/source**, stop all other playback, and keep the same Windows output selected. Volume changes may unmute the TV/amplifier. Windows mute does not silence TV-generated audio or other amplifier sources. A full sweep can reach a very loud range if the source becomes active.
3. Confirm the silent-source checkbox. Choose a reachable lower raw limit (default zero). The default upper limit is the reported API ceiling; optionally select a lower reachable upper limit. Limits constrain measurement; the original volume is restored afterward unless a lower safe TV restore volume is selected.
4. Click **Run full calibration sweep**. The helper waits for three seconds of settled device observations, saves recovery state, mutes Windows, and verifies a relative CEC step. It moves to the lower limit, measures upward one relative step at a time, then measures downward. At the first reported API ceiling it stops increasing immediately; it never explores the unobservable physical range beyond it.
5. After both passes, the helper restores and verifies saved volume/mute states, restores Windows mute last, and only then atomically publishes the mapping. Both remotes' normal polling and controls resume. Full-range calibration may take several minutes; progress appears in the log and activity indicator.

**Run step check only** retains the shorter preflight/restoration workflow and does not replace a mapping. **Cancel and restore**, or closing the helper during work, waits for an issued relative step to settle and performs cleanup. A cancelled/failed sweep or failed restoration leaves the previous mapping intact. Restoration failures retain the recovery record and leave Windows muted where possible. Keep the source silent and use **Recover saved states**; do not delete the JSON to bypass recovery. Older recovery records remain supported.

Relative commands are `+1` / `-1`; an amplifier raw change is valid even when the TV number stays unchanged. Fresh observations from both devices are required. Retries are bounded to three attempts and only occur after neither device moves for five seconds. Movement is then verified and settled separately. Missing observations, API/IPC errors, opposite movement, and unreachable limits stop the run. Each sweep pass is limited to 512 successful steps and fifteen minutes; each positioning/restoration seek has the same bounds. No periodic IPC status requests are added: existing remote device timers provide the observations.

### Mapping file and results

`VolumeCalibration.json` keeps `format_version: 1`, the durable `recovery` record, a `last_session` report, and a `calibration` object. Mapping schema version 1 contains:

- Captured amplifier TV input, configured-TV/amplifier fingerprints, and creation date.
- Observed raw/TV range limits and the amplifier network API ceiling.
- Separate `up` and `down` arrays of exact `{ "tv": ..., "raw": ... }` observations.

Several amplifier values may belong to one TV level. No linear conversion is assumed. A full mapping is published only when both monotonic passes cover the same raw endpoints and restoration succeeds. Individual up/down disagreements or levels seen in one pass remain identifiable observations, rather than being silently merged or treated as exact matches. No partial sweep is automatically activated.

The results table groups observations by TV level, shows the raw ranges/counts from both passes, and labels directional differences or incomplete verification. Hover over a raw-range cell for every observed value. The summary reports conflicts, one-pass observations, gaps in observed TV levels, and the shared range. The table reloads saved results when the helper starts and after a run. It is read-only; the underlying JSON retains the complete observations.

Sony watches this file through its existing event-driven file watcher and loads changes without restarting. It uses the captured input only when both configured-device fingerprints match. With no matching file it keeps `AmplifierTVInput` and the raw amplifier display. Deleting/renaming the calibration file restores that fallback; a malformed edit retains the last valid in-memory mapping and reports the reload error in the volume tooltip (and optional diagnostics).

Mapped volume is capped to the calibrated TV range—for example, a raw ceiling of 154 observed at TV 80 caps the mapped display at 80. Exact observed pairs use their recorded TV value. Unobserved raw values use the nearest recorded observation, and directional conflicts use the latest observed direction; **≈** marks these approximate or ambiguous values. The actual TV's stored volume remains separate. Raw values outside a user-selected narrower range are clamped to its display endpoints. No inferred linear interpolation or physical-TV volume write is performed.

Fingerprints currently identify the configured host endpoints, not hardware serial numbers. Recalibrate after replacing a device, even if its address stays the same. Recovery records also contain the original Windows endpoint ID, mute/volume states, and restore target, never credentials. Only one helper can own the JSON and remote sessions. Abrupt process/computer termination can interrupt restoration; the session leases expire while the durable recovery record permits a later recovery attempt.

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
