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
  "NavigationControlsCompact": false,
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
- `NavigationControls` — show directional, OK, Input, Settings, Home, and Back controls.
- `NavigationControlsCompact` — `false` (default) gives Input/Settings and Back/Home their own two-button rows; `true` places them in the corners of the three-by-three navigation grid.
- `InputControls` — show the input selector and favorite input buttons.
- `VolumeControls` — show volume and mute controls.
- `PlaybackControls` — show media playback controls.
- `AmplifierTVInput` — fallback amplifier input ID for the TV (for example `hdmi1`, `optical1`, or `tv`), rather than its display name. Leave empty to use TV controls unless a matching calibration supplies the input.
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
- `DropdownAnimationWorkaround` — use a custom 200 ms dropdown animation to prevent underlying button text showing through on Windows. Set to `false` for the standard Qt dropdown; disabled system animations remain disabled.

SonyRemote automatically adds newly discovered applications and inputs to `Apps` and `Inputs` with default settings. Existing `NameOverride`, `Hidden`, and `Favorite` values are preserved. Existing entries keep their JSON positions; newly discovered entries append in device discovery order. Dropdowns only include currently discovered, non-hidden entries. Favorite buttons remain independent of `Hidden`. Name sorting affects display only and does not reorder the JSON file.

Failed discovery disables only its associated controls and retries automatically; power and volume remain usable when TV status is available.

The app automatically restarts when it detects that `SonyRemote.json` has changed.

Only one instance of SonyRemote can run at a time. Launching it again restores the existing window, including from the system tray.

Regular status polling pauses while the window is hidden or minimized and refreshes immediately when restored. Active requests finish normally; configuration file watching remains enabled.

## Windows shortcut reliability

Windows shortcuts use native key handling to stay responsive, including when both remotes run. Sony yields volume/mute keys whenever Yamaha Remote owns global volume control. While a dropdown is open, arrows and Enter operate the list instead of the TV.

## Direct amplifier volume

With both remotes running, Sony can send volume/mute commands directly to the amplifier, bypassing the slower TV/CEC path. It falls back to TV control when the amplifier is unavailable, in standby, or using another input.

A matching calibration automatically identifies the amplifier's TV input and displays TV-scale volume numbers. Without calibration, set `AmplifierTVInput` to the amplifier's input ID (such as `tv` or `hdmi1`); the displayed volume then follows Yamaha Remote’s raw/dB display setting. Update both remotes together.

## Volume calibration

- **What it does:** Measures the TV/amplifier volume relationship and saves it in `VolumeCalibration.json` beside Sony Remote.
- **Why use it:** Keeps familiar TV volume numbers in Sony Remote while volume commands go directly to the amplifier.
- **Requirements:** A Windows PC, a TV connected to the amplifier through HDMI-CEC, and working network connections from both remotes to their devices.
- **Before starting:** Update both remotes and the helper, turn on the TV and amplifier, and select the amplifier input connected to the TV. Run both remotes and keep `VolumeCalibration.pyw` in the same folder as `SonyRemote.pyw`.
- **Silent source:** Put the TV on a silent input/source, stop all other playback, and keep the same Windows audio output selected. Windows mute alone does not silence TV-generated audio or other amplifier sources.
- **Run:** Open `VolumeCalibration.pyw`, confirm the silent-source checkbox, and choose **Start calibration** or **Replace calibration**. The same button cancels a running sweep; the activity log shows what is happening.
- **During calibration:** Volume can go **VERY HIGH**, and volume changes may unmute the TV/amplifier. Keep the source silent and avoid physical controls until cleanup finishes.
- **After calibration:** The helper attempts to restore the starting settings, including after cancellation or failure. **Check volume and mute on the TV, amplifier, and computer before playing sound**, especially if the helper was interrupted.

Sony loads a successful calibration automatically; no manual `AmplifierTVInput` edit is needed. An **≈** beside the volume marks an estimated value; calibration changes Sony's displayed number without setting the physical TV's volume.

## Navigation

**Input** opens the TV’s input menu and **Settings** opens its settings menu; the separate input dropdown remains available. Use `NavigationControlsCompact` to switch between separate menu rows and a compact three-by-three grid.

With the remote focused, **Insert** opens Input and **Delete** opens Settings; both keep normal editing behavior while Text Input has focus.

## Text input

- **Enter** — Sends the text to the TV.
- **Ctrl+Enter** — Sends the text and automatically activates the TV keyboard action, such as **Search** or **Go**.
- **Shift+Enter** — Adds a new line instead of sending the text.

> **Note:** Ctrl+Enter depends on the TV’s on-screen keyboard having the expected layout.

## More information

- [Implementation details](IMPLEMENTATION.md) — key handling, IPC, calibration, and mapping format.
- [Release history](CHANGELOG.md) — versions and changes.

## Third-party code

SonyRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
