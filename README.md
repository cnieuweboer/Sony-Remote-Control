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

Set, for example, `"AmplifierTVInput": "hdmi1"` in `SonyRemote.json`. Use the corresponding key from Yamaha Remote's `Inputs` setting, not a `NameOverride`. While the amplifier is reachable, on, and using that input, Sony's volume/mute buttons and focused volume keys send commands directly to it. While routing, the volume number and mute label follow the amplifier's published raw state, including changes made directly through Yamaha Remote. The number's tooltip identifies the amplifier source. The TV's own stored volume remains separate.

Standby, another amplifier input, offline state, a disconnected socket, or expired status automatically restores ordinary TV volume control. When Yamaha's global volume hook is actually active, Sony passes its focused volume/mute keys through so Yamaha handles each press. This hotkey ownership applies on every amplifier input, independently of whether Sony's volume buttons route to it. Disabling global hotkeys, losing the connection, or losing valid ownership restores Sony's focused shortcuts. Other shortcuts retain their existing behavior.

Connection attempts are asynchronous and silent when no amplifier app is running. A two-second reconnect timer runs only while disconnected; no periodic IPC state requests are sent. Connected clients receive state updates from existing device polls and state events. A freshness deadline prevents continued use of stale state if the amplifier app stops responding. If Sony starts first, routing becomes available automatically after the amplifier app starts.

A routing change cancels pending TV volume rate-limit targets. An already-running TV request may still finish. Amplifier commands use its existing serialized worker and bounded queue, bypassing Sony's TV/CEC rate limiter. Rejected commands and acknowledgement failures are reported without retrying against either device, because a retry could apply a volume step twice. Acknowledgements confirm queue acceptance rather than completed device execution.

The amplifier's reported raw maximum is an API ceiling, not its physical volume limit. This release deliberately uses raw amplifier values directly; it does not normalize to the TV's scale or write a converted value to the TV. Calibration, caps, and alternative volume mappings are reserved for the later calibration/helper step.

## Calibration helper — session and restoration stage

Release `2026.10.18.5` adds exclusive calibration sessions. Update **both** remotes before using `VolumeCalibration.pyw`, available in the Sony Remote repository. The helper is a separate, self-contained PySide6 application and communicates only through generic local IPC endpoints. It does not read either remote's settings or credentials.

During a session, both remotes reuse their existing polling timers at 250 ms, including while hidden. Ordinary controls are disabled and pending volume targets are cleared. Yamaha's Windows hook consumes volume/mute media keys without issuing device commands, so they cannot unmute Windows or change volume during the test. Config-driven restarts wait until the session ends. A socket-owned eight-second lease, renewed by the helper, restores normal polling and controls on release, disconnect, or helper failure. Only the owner can perform calibration operations; API work stays in the existing device workers.

The helper first captures the selected amplifier input through IPC. This stage does not change `AmplifierTVInput`, save a volume mapping, or apply a new routing configuration. Existing routing remains unchanged. The next stage will save the verified input, device pairing, usable range, and mapping in the separate calibration JSON.

### Running the first-stage helper

Release `2026.10.18.9` requires **both `SonyRemote.pyw` and `VolumeCalibration.pyw` to be updated**. Yamaha Remote can remain at `2026.10.18.5` or newer. Calibration now sends only TV relative `+1` / `-1` commands; absolute TV target writes are no longer accepted by the calibration endpoint. The helper checks this capability before creating a recovery record or changing mute/volume.

One confirmed amplifier raw step is valid even if the TV's coarser volume number remains unchanged. The helper waits for fresh observations from both devices and for amplifier movement in the requested direction to settle. It retries the same relative direction up to three attempts only when neither reading has moved for five seconds. Any observed movement prevents replay; if only the TV moved, the helper waits for amplifier confirmation rather than sending another command. API/IPC errors, missing fresh observations, and opposite-direction movement stop the operation.

Restoration uses one relative command at a time to reach the saved amplifier raw volume, then checks the saved TV reading and mute states. An explicitly lower safe TV restore level is reached incrementally, without exceeding the saved amplifier volume. Restoration is bounded to 512 steps and 120 seconds per seek, and stops if the raw target lies between reachable steps rather than oscillating. Cancellation waits for an already-issued relative step to finish observation before restoring, preventing a delayed step from arriving after cleanup. Older-record recovery also uses relative stepping and a raw-volume round trip; recovery files remain compatible.

Ordinary Sony TV volume clicks with the rate limiter disabled now send relative steps too. The enabled rate limiter continues stepping incrementally toward its displayed TV target. Yamaha-directed routing is unchanged.

Helper `2026.10.18.7` waits for at least three seconds of unchanged TV/amplifier readings after acquiring exclusive control, allowing up to fifteen seconds for earlier CEC commands to settle. Fresh IPC data alone does not establish a settled baseline. If readings keep changing, the helper stops before saving recovery data or changing mute/volume. The probe must move amplifier volume in the expected direction.

Older recovery records may contain a TV/amplifier pair captured while CEC was still catching up. **Recover saved states** rechecks the saved TV restore level, verifies a small CEC step and return to that level, and only then replaces the old amplifier reference. This requires a silent source, valid pairing, and observable readings below the API ceiling. Failed checks preserve the journal and keep Windows muted. New records mark the settled baseline and retain exact amplifier-volume restoration checks. Do not delete the JSON to bypass recovery.

Helper `2026.10.18.6` supports TVs and amplifiers that automatically clear mute on volume changes. It no longer pre-mutes those devices or treats their clearing mute as a test failure. Silence must come from the selected source throughout testing and recovery. Recovery JSON from `2026.10.18.5` remains compatible; preserve it and use **Recover saved states** before starting another test. Yamaha can stay at `2026.10.18.5`; the current relative-step helper requires Sony `2026.10.18.9`.

1. Place `VolumeCalibration.pyw` in a writable folder and run it on Windows with the same Python/PySide6 installation as the remotes.
2. Start Yamaha Remote (`2026.10.18.5` or newer) and Sony Remote (`2026.10.18.9` or newer). Select the amplifier input physically connected to the TV and start at a comfortable volume **below** its reported API ceiling. TV and amplifier mute should initially agree. Set the **TV** to a silent input/source and stop all other playback. Windows mute does not silence TV-generated audio or other amplifier sources.
3. Keep the same Windows output selected and do not use physical device controls during the check. Optionally select a safe TV restore volume no higher than the starting TV volume.
4. Confirm **I confirm the TV source is silent and all other playback is stopped**, then click **Run silent-source CEC check**. The helper saves original states before changing anything, mutes Windows only, and tests one TV volume step through CEC. It prefers a downward amplifier step, using an upward step only at amplifier raw zero. The TV number may remain unchanged.
5. Keeping Windows muted, the helper restores TV volume, verifies the corresponding amplifier volume has settled, restores and verifies both original device mute states, and restores Windows mute last. **Cancel and restore**, or closing the helper while running, requests this same cleanup rather than terminating its worker.

Each relative attempt allows five seconds to observe movement, followed by a separate settling check; initial settling and the older-record pairing checks allow fifteen seconds. A result must remain stable across at least three distinct observations from **both** devices, following at least a one-second settling period. Repeated reads of one snapshot do not count. The helper stops on failed final mute restoration, missing volume response, stale/offline devices, a changed amplifier input, or lost session ownership. No sweep or mapping is performed yet.

`VolumeCalibration.json` is generated next to the helper. It is separate from `SonyRemote.json` and contains a versioned `recovery` record plus a `last_session` report. The `calibration` field is reserved for the next-stage mapping; an existing mapping is preserved. Recovery records contain the captured input, configured-device fingerprints, original mute/volume states, Windows endpoint ID, and requested restore volume—never device credentials. Configured-host fingerprints identify the remote configuration, rather than proving a device's hardware serial number.

If restoration cannot be verified, Windows remains muted where possible and the recovery record is retained. Keep the TV on a silent source, restore the original amplifier input and Windows output, start both remotes, confirm the silent-source checkbox, and click **Recover saved states**. A new test is blocked until recovery succeeds. Do not delete or edit the recovery record to bypass this. Ordinary cancellation and exceptions attempt restoration; killing the helper, remote, or computer can interrupt it. The remote leases still release automatically, while the durable JSON permits a later recovery attempt.

The helper's Windows Core Audio integration uses the standard library's COM bindings; no extra package beyond PySide6 is required. Real Windows audio and physical CEC behavior still need testing on your devices. The helper intentionally fails closed when it cannot verify restoration.

## Text input

- **Enter** — Sends the text to the TV.
- **Ctrl+Enter** — Sends the text and automatically activates the TV keyboard action, such as **Search** or **Go**.
- **Shift+Enter** — Adds a new line instead of sending the text.

> **Note:** Ctrl+Enter cannot directly trigger the TV’s **Search**, **Go**, or similar action. It works by automatically navigating the on-screen keyboard to that button, so it depends on the keyboard using the expected layout. With that button in the bottom right, on the fourth row.

## Third-party code

SonyRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
