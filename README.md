# Sony-Remote-Control

Small PySide6 desktop remote for Sony BRAVIA TVs using the Sony BRAVIA IP Control API (only tested on XR-55X90J)

![Sony Remote](screenshots/sony_remote.jpg)

## Installation

Run `SonyRemote.pyw` from a writable folder. On first launch it creates `SonyRemote.json` and prompts you to fill in the missing connection settings; you can also copy `SonyRemote.example.json` to `SonyRemote.json` before starting.

## Windows

```powershell
py -m pip install "PySide6>=6.8"
```

Run `SonyRemote.pyw`.

For Windows login startup, add `--start-in-tray` to your shortcut target, for example:

```text
"C:\Path\To\pythonw.exe" "C:\Remotes\SonyRemote.pyw" --start-in-tray
```

This starts hidden only when `SystemTray` and `CloseToTray` are both `true` and a tray is available; otherwise the window opens normally. Launching the app again still brings the existing window forward.

For optional Windows-managed global playback keys, install:

```powershell
py -m pip install winrt-Windows.Media winrt-Windows.Media.Interop winrt-Windows.Foundation
```

Then set `"SMTCPlaybackKeys": true` in `SonyRemote.json`. Windows routes Play/Pause, Stop, Next, and Previous between participating apps; the remote can receive them while in the tray.

## macOS

```bash
python3 -m pip install "PySide6>=6.8"
python3 SonyRemote.pyw
```

## Linux

```bash
python3 -m pip install "PySide6>=6.8"
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
  "VolumeRateLimit": {"Active": true, "RateMs": 600, "VerificationDelayMs": 500},
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
- `DarkMode` — Requests Qt’s native dark (`true`) or light (`false`) appearance independently of the desktop theme. Requires PySide6 6.8 or newer.
- `Mica` — Windows-only Mica background. Ignored on macOS and Linux.
- `TextInputControls` — show the Text Input section. Text is sent to the currently active TV software keyboard or text field by pressing Enter.
- `ChannelControls` — show number buttons `0`–`9` and channel `−` / `+` controls.
- `AppControls` — show the application launcher and favorite application buttons.
- `NavigationControls` — show directional, OK, Input, Settings, Home, and Back controls.
- `NavigationControlsCompact` — `false` (default) gives Input/Settings and Back/Home their own two-button rows; `true` places them in the corners of the three-by-three navigation grid.
- `InputControls` — show the input selector and favorite input buttons.
- `VolumeControls` — show volume and mute controls.
- `VolumeRateLimit` — when active, TV volume changes use absolute targets, waiting `RateMs` after each command and an extra `VerificationDelayMs` to confirm a target reading. Defaults are 600/500 ms; disabled uses direct relative commands, and amplifier IPC routing is unaffected.
- `DefaultTvVolume` — optionally set TV speaker volume after the amplifier leaves its configured TV input: `{"Volume": 10, "DelayMs": 1000, "RepeatCount": 0, "RepeatIntervalMs": 200}`. Set `Volume` to `null` to disable it; `RepeatCount` is the number of additional sends (zero sends once), and `RepeatIntervalMs` is the wait after each completed command before the next send. Choose a delay long enough for CEC to release the amplifier; changing volume or mute cancels the sequence.
- `PlaybackControls` — show media playback controls.
- `SMTCPlaybackKeys` — `false` by default. On Windows, use SMTC for global playback keys and disable focused interception of physical playback keys; requires the optional packages above. Participation pauses when the TV is unavailable or during calibration. Active amplifier global bindings take priority over conflicting playback keys.
- `AmplifierTVInput` — fallback amplifier input ID for the TV (for example `hdmi1`, `optical1`, or `tv`), rather than its display name. Leave empty to use TV controls unless a matching calibration supplies the input.
- `Shortcuts` — focused action-to-key lists; see the keyboard shortcut section below.
- `DiagnosticsEnabled` — `false` by default. Set to `true` to append unexpected errors and crash diagnostics to `SonyRemote-crash.log` beside the script. Logs contain the app version, exception types, and stack locations; exception messages, source lines, request data, and local variable values are omitted. If the log cannot be opened or written, the app continues without logging.
- `SortByName` — `false` (default) keeps favorites and dropdown entries in JSON insertion order; `true` sorts them alphabetically by `NameOverride`, falling back to `Name`, ignoring case. Apps and inputs are ordered separately.
- `Apps` — settings for applications discovered from the Sony TV.
  - `Name` — name reported by the TV. This is updated automatically.
  - `NameOverride` — optional replacement name shown in the dropdown and on favorite buttons. Long closed labels end with an ellipsis; favorite buttons keep equal widths.
  - `Hidden` — hide the application from the dropdown menu. Hidden applications can still be favorites.
  - `Favorite` — show a button for the application directly in the remote.
  - `Removed` — maintained automatically; confirmed missing entries disappear from dropdowns and favorites, then return with their preferences when rediscovered.
- `Inputs` — settings for inputs discovered from the Sony TV.
  - `Name` — name reported by the TV. This is updated automatically.
  - `NameOverride` — optional replacement name shown in the dropdown and on favorite buttons.
  - `Hidden` — hide the input from the dropdown menu. Hidden inputs can still be favorites.
  - `Favorite` — show a button for the input directly in the remote.
  - `Removed` — maintained automatically; confirmed missing entries disappear from dropdowns and favorites, then return with their preferences when rediscovered.
- `SystemTray` — show a system tray icon when the desktop supports it. If unavailable, the app runs as an ordinary window.
- `CloseToTray` — clicking the window close button hides the app to the tray instead of exiting. If no tray is available, closing the window exits normally.
- `DropdownAnimationWorkaround` — use a custom 200 ms dropdown animation to avoid dropdown flicker on Windows. Set to `false` for the standard Qt dropdown; disabled system animations remain disabled.

SonyRemote automatically adds newly discovered applications and inputs to `Apps` and `Inputs` with default settings. Existing `NameOverride`, `Hidden`, and `Favorite` values are preserved. Existing entries keep their JSON positions; newly discovered entries append in device discovery order. Dropdowns exclude `Hidden` and `Removed` entries; favorites exclude `Removed` entries. Name sorting affects display only and does not reorder the JSON file. Availability is refreshed while the device is on and the window visible; missing entries need a second successful check after five seconds, and the UI resizes without restarting.

Failed discovery disables only its associated controls and retries automatically; power and volume remain usable when TV status is available.

## Keyboard shortcuts

Edit `Shortcuts` to replace focused keyboard bindings; each action accepts a list such as `["Ctrl+Alt+Up", "F8"]`. Missing actions are added with defaults on startup, while `[]` or `""` disables that action's focused shortcut; button tooltips show the configured bindings.

Additional TV commands can be added by their exact advertised name, for example `"Guide": ["F1"]`. Find available names in `--dump-state-full` under `polling_responses` → `system/getRemoteControllerInfo`; commands not advertised by the TV stay inactive and are listed in dump diagnostics.

For supported key names and shortcut syntax, see [KEYBOARD_SHORTCUTS](KEYBOARD_SHORTCUTS.md). Mouse button and wheel names can also be added to the same `Shortcuts` lists; see [mouse bindings](KEYBOARD_SHORTCUTS.md#mouse-bindings).

## Direct amplifier volume

With both remotes running, the Sony remote can send volume/mute commands directly to the amplifier, bypassing the slower TV/CEC path. It falls back to TV control when the amplifier is unavailable, in standby, or using another input.

A matching calibration automatically identifies the amplifier's TV input and displays TV-scale volume numbers. Without calibration, set `AmplifierTVInput` to the amplifier's input ID (such as `tv` or `hdmi1`); the displayed volume then follows Yamaha Remote’s raw/dB display setting. Update both remotes together.

## Volume calibration

- **What it does:** Measures the TV/amplifier volume relationship and saves it in `VolumeCalibration.json` beside Sony Remote.
- **Why use it:** Keeps familiar TV volume numbers in Sony Remote while volume commands go directly to the amplifier.
- **Requirements:** A Windows PC, a TV connected to the amplifier through HDMI-CEC, and working network connections from both remotes to their devices.
- **Before starting:** Update both remotes and the helper, turn on the TV and amplifier, and select the amplifier input connected to the TV. Run both remotes and keep `VolumeCalibration.pyw` in the same folder as `SonyRemote.pyw`.
- **Silent source:** Put the TV on a silent input/source, stop all other playback, and keep the same Windows audio output selected. Windows mute alone does not silence TV-generated audio or other amplifier sources.
- **Run:** Open `VolumeCalibration.pyw`, confirm the silent-source checkbox, and choose **Start calibration** or **Replace calibration**. The same button cancels a running sweep; the activity log shows what is happening.
- **Speaker isolation:** The helper tries to disable amplifier speakers and verifies the result. If unavailable or lost, choose once whether to continue without isolation; an unanswered prompt cancels after five minutes. A silent source is still required.
- **During calibration:** Volume can go **VERY HIGH**, and volume changes may unmute the TV/amplifier. Keep the source silent and avoid physical controls until cleanup finishes.
- **After calibration:** The helper attempts to restore the starting settings, including after cancellation or failure. **Check speaker outputs, volume, and mute on the TV, amplifier, and computer before playing sound**, especially if the helper was interrupted.

Sony loads a successful calibration automatically; no manual `AmplifierTVInput` edit is needed. Calibrated volume shows decimal steps within each TV-number group (for example **20**, **20.5**). An **≈** beside the volume marks an estimated value; calibration changes Sony's displayed number without setting the physical TV's volume.

## Navigation

**Input** opens the TV’s input menu and **Settings** opens its settings menu; the separate input dropdown remains available. Use `NavigationControlsCompact` to switch between separate menu rows and a compact three-by-three grid.

With the remote focused, **Insert** opens Input and **Delete** opens Settings; both keep normal editing behavior while Text Input has focus.

## Text input

- **Escape** — Leaves Text Input without sending or clearing anything.
- **Enter** — Sends the text to the TV.
- **Ctrl+Enter** — Sends the text and automatically activates the TV keyboard action, such as **Search** or **Go**.
- **Shift+Enter** — Focuses Text Input from the remote; inside the field, adds a new line. Configure or disable this through `Shortcuts.FocusTextInput`.

Accepted submissions return focus to the remote for keyboard navigation and keep your text. Empty text is sent too, allowing the TV field to be cleared if the TV supports it.

> **Note:** Ctrl+Enter depends on the TV’s on-screen keyboard having the expected layout.

## More information

- [Implementation details](IMPLEMENTATION.md) — key handling, IPC, calibration, and mapping format.
- [Release history](CHANGELOG.md) — versions and changes.

## Third-party code

SonyRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.


Amplifier global shortcuts take priority over overlapping SMTC playback keys and focused shortcuts. Sony follows the amplifier's published ownership automatically. With Sony focused and the TV on, matching volume shortcuts control the TV when the amplifier uses another known input; global control remains active outside Sony.

### Cached state report

Run `python SonyRemote.pyw --dump-state` while the remote is running to save a timestamped diagnostic JSON beside the script (or in the app-data diagnostics folder if needed). This leaves the window as it is and makes no device queries; the result message gives the file path. Credentials and text-entry contents are omitted, but review reports before sharing.

Use `--dump-state-full` instead to include the last successful decoded response for each polling request, with its timestamp and age. It still makes no fresh queries; full reports may include device/network identifiers and app inventories, so review them before sharing.

Both reports also include session-only polling counters for successes, empty inventories, failures and recoveries; these reset when the remote restarts.
