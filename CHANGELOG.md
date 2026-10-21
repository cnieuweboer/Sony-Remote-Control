# Release history

Release identifiers below come from the corresponding commits. Some releases updated only the calibration helper; the remote and helper can therefore have different version numbers. Historical behavior may have been superseded by later releases.

## 2026-10-21

- 2026.10.21.1 — Expand dropdown popups one Qt logical pixel on each side while leaving combobox buttons unchanged. Use the expanded geometry throughout the live opening animation.

## 2026-10-20

- 2026.10.20.7 — Clear state-protocol feedback on fresh valid state and ack-protocol feedback on a valid pending acknowledgement, without replaying commands. Keep unattributed errors until an accepted command. Detect missing Qt theme support before acquiring the instance lock, display an explicit upgrade message, and remove unused imports.

- Calibration helper 2026.10.20.6 — Cancel unanswered speaker-isolation decisions after five minutes while maintaining lease heartbeats; close the prompt through restoration/completion and log timeout explicitly. Distinguish missing amplifier-remote speaker information, unsupported controls, and unverifiable current state. Preserve one decision per sweep, mapping safety, and single-pass restoration. Sony Remote remains 2026.10.20.5.

- 2026.10.20.5 — With Windows Mica, restore custom button/combobox/text-input fills and outlines, hover/pressed/disabled styling, section borders, and matching dropdown colors. Use opaque control fills to keep the backdrop out of controls; other configurations retain Qt-native colors.

- 2026.10.20.4 — Give the power/standby button an opaque dark/light background only with Windows Mica, including hover/pressed states. Other native controls retain Qt-managed styling.

- 2026.10.20.3 — Restore compact control padding so native playback-button size hints do not widen Sony layouts. Restore previous dark/light section fills only with Windows Mica; retain Qt-managed control colors and explicit JSON theme selection.

- 2026.10.20.2 — Request light/dark appearance through Qt 6.8+ platform integration. Remove fixed control colors and the manual dark title-bar override; retain layout, typography, Mica support, and live dropdown animation. Windows appearance and live desktop-theme changes require visual verification.

- 2026.10.20.1 — Preserve Qt window geometry and visible/minimized/tray state across configuration restarts. Startup-in-tray arguments apply only to the initial launch; disabling tray support makes a hidden replacement visible.

- Calibration helper 2026.10.20.3 — Verify amplifier speaker isolation before the sweep, offer one explicit Continue/Cancel choice if unavailable or lost, and keep leases alive during the decision. Restore volume and mute before speaker outputs and Windows mute; preserve the previous mapping on failed cleanup. Add fake-device and Qt coverage. Sony Remote remains 2026.10.19.20.

## 2026-10-19

- Sony Remote 2026.10.19.20 — Add optional Windows SMTC routing for Play, Pause, Stop, Next, and Previous with `SMTCPlaybackKeys` (default false). Disable their focused interception in this mode, preserve volume/amplifier routing, and reuse the existing polling timer while hidden. Add lazy optional imports, clear startup warnings, Qt-thread callback delivery, serialized commands, safe session cleanup/restart recovery, and fake-SDK integration tests.

- 2026.10.19.19 — Restore the native Qt dropdown arrow in a borderless, transparent right-hand area. Keep labels centered across the full combobox and retain the live-view popup animation; no icon files are required.

- 2026.10.19.15 — Make tray startup tests independent of the checkout folder name and describe dropdown flicker as a popup fill/frame timing mismatch. Clear connection-loss feedback on fresh valid amplifier state using explicit error categories; preserve timeout/rejection feedback until an accepted command and never replay uncertain commands.

- Sony Remote 2026.10.19.14 — With volume rate limiting disabled, schedule the follow-up status poll only after the volume-command queue drains. Suppress delayed follow-ups superseded by new volume input or active volume work, while retaining final target reconciliation and ordinary periodic polling. Add deterministic queue/delay regression tests.

- 2026.10.19.13 — Replace tracked device settings with a sanitized `SonyRemote.example.json`; ignore local settings, crash logs, and temporary JSON files. Make hook lifecycle tests independent of the checkout folder name. Resolve the configuration path consistently with the calibration helper, including symlink launches.

- Sony Remote 2026.10.19.12 — Accept version-1 amplifier acknowledgements without the optional rejection reason. Report malformed protocol messages through deduplicated optional diagnostics and persistent non-modal feedback. Keep amplifier errors across TV polls/discovery until a pending command is acknowledged as accepted; retain silent queue-full drops and no replay. Add matching wire fixtures and producer/consumer regression tests in both repositories.

- Sony Remote 2026.10.19.10 — Replace the dropdown screenshot overlay with a live-list animation. Preserve scrolling and keyboard navigation while opening, suspend internal layout resizing until completion, and retain native placement, interruption cleanup, and the existing workaround setting.

- 2026.10.19.9 — Add `--start-in-tray`, gated by both tray settings and actual tray availability. Start hidden without a visible-window flash, keep ordinary fallback/second-launch activation and restart arguments, and add startup regression tests.

- Sony Remote 2026.10.19.8 — Preserve pending TV volume targets across background polling and unrelated amplifier state updates, including queued commands without rate limiting. Ignore polls superseded by newer volume input/completion. Add immediate volume-button presses with repeat after 500 ms at 100 ms intervals; retain rate limiting/routing and add GUI regression tests.

- Sony Remote 2026.10.19.7 — Replace favorite-button hiding with a captured-content animation inside the real Qt dropdown, using the existing workaround toggle. Preserve Qt popup placement and disabled-animation preferences; restore live interaction/geometry after interruption and add dropdown regression tests.

- Helper 2026.10.19.5 — Select one rounding method per gap between measured anchors. Retain all three methods and nearest-half-up tie-breaking while preventing per-value rule switches from making an interpolated gap decrease. Add mapping regression tests; remote versions and JSON format are unchanged.

- 2026.10.19.3 — Preserve newer TV volume targets when an older rate-limiter completion reaches the GUI. Stop only when the current target matches the observed volume; add deterministic volume-key timing regression tests.

- 2026.10.19.2 — Preserve unapplied JSON edits when saving discovered inputs/applications. Compare the accepted file digest immediately before replacement and track the exact saved bytes so later edits still trigger validation. Add discovery-save regression tests.

- 2026.10.19.1 — Skip native keyboard-hook installation if shutdown was requested during startup, and avoid reporting installation after shutdown is observed. Add deterministic startup/stop lifecycle tests; worker shutdown behavior is unchanged.

## 2026-10-18

- 2026.10.18.35 — Second launches restore the existing window through a separate user-local activation endpoint, including from tray/minimized state. Add bounded startup retries, Windows foreground permission, restart recovery, and cross-process regression tests.

- 2026.10.18.34 — Discover remote codes, applications, and inputs independently; keep core controls available and retry failed discovery through existing polling. Add focused graceful-failure regression tests.

- 2026.10.18.33 — Drain IPC reads with frame bounds, use cached Win32 mutex errors, and hash/validate the same configuration bytes. Sony mapping reload also uses one snapshot; move the helper QLockFile import. Add focused reliability regression tests.

- 2026.10.18.32 — Add 31 deterministic calibration and mapping regression tests. Calibration behavior and file format are unchanged; only the helper version label changes. Both remotes remain at 2026.10.18.31.

- 2026.10.18.31 — Clean up local configuration/state helpers and endpoint naming, reduce Sony duplication, and add development-only regression tests. Scripts remain self-contained; IPC names and calibration file format are unchanged.

- 2026.10.18.30 — Suspend focused TV shortcuts while dropdowns or other popups are open, preserving list navigation and selection.

- 2026.10.18.29 — Serialize TV-changing commands on a bounded single-thread pool while polling stays separate; include both pools in calibration and lifecycle handling.

- 2026.10.18.28 — Bypass system/environment proxies for device requests using a dedicated direct-connection opener.

- 2026.10.18.27 — Reject invalid numeric IP-like hosts during configuration validation; correct the Sony sample address.

- 2026.10.18.26 — Silently drop amplifier IPC queue overflow and report other IPC failures non-modally. Update both remotes together.

- 2026.10.18.25 — Require one second of hovering before showing tooltips, including when moving between controls.

- 2026.10.18.24 — Show keyboard shortcuts in button hover tooltips.

- 2026.10.18.23 — Add Insert for Input and Delete for Settings to focused keyboard shortcuts, preserving text editing.

- 2026.10.18.22 — Add NavigationControlsCompact to choose between separate menu rows (default) and a full three-by-three navigation grid.

- 2026.10.18.21 — Match Input and Settings button sizes to the navigation grid.

- 2026.10.18.20 — Add compact Input and Settings buttons beside Up in the navigation area, using commands advertised by the TV.

- 2026.10.18.19 — Share Yamaha’s formatted volume and unit over IPC; Sony mirrors them when no calibration matches. Update both remotes together.

- 2026.10.18.18 — Simplify volume tooltips to TV volume, calibrated TV scale, or amplifier raw volume. Calibration reload errors remain available through optional diagnostics.

- [2026.10.18.17](https://github.com/cnieuweboer/Sony-Remote-Control/commit/237c0ee) — Simplify calibration to a single-run one-button helper in 2026.10.18.17.
- [2026.10.18.16](https://github.com/cnieuweboer/Sony-Remote-Control/commit/d90d2e1) — Save compact operational volume lookup and simplify Sony validation in 2026.10.18.16.
- [2026.10.18.15](https://github.com/cnieuweboer/Sony-Remote-Control/commit/7a949b4) — Remove unused calibration remnants from Sony Remote 2026.10.18.15.
- [2026.10.18.14](https://github.com/cnieuweboer/Sony-Remote-Control/commit/e36ce03) — Use faster movement for calibration restoration in 2026.10.18.14.
- [2026.10.18.13](https://github.com/cnieuweboer/Sony-Remote-Control/commit/3041b65) — Tolerate delayed TV readings during calibration positioning in 2026.10.18.13.
- [2026.10.18.12](https://github.com/cnieuweboer/Sony-Remote-Control/commit/f68ecae) — Recover uncertain CEC steps before retrying calibration in 2026.10.18.12.
- [2026.10.18.11](https://github.com/cnieuweboer/Sony-Remote-Control/commit/0aa4f99) — Build canonical volume lookup with verified gap interpolation in 2026.10.18.11.
- [2026.10.18.10](https://github.com/cnieuweboer/Sony-Remote-Control/commit/40dcb5f) — Add volume sweeps, mapping results and calibrated routing in version 2026.10.18.10.
- [2026.10.18.9](https://github.com/cnieuweboer/Sony-Remote-Control/commit/ffc3b9a) — Use relative TV steps and raw amplifier restoration in version 2026.10.18.9.
- [2026.10.18.8](https://github.com/cnieuweboer/Sony-Remote-Control/commit/4e086f8) — Verify and retry absolute TV volume targets in helper 2026.10.18.8.
- [2026.10.18.7](https://github.com/cnieuweboer/Sony-Remote-Control/commit/87f9094) — Settle calibration baselines and repair older recovery pairs in helper 2026.10.18.7.
- [2026.10.18.6](https://github.com/cnieuweboer/Sony-Remote-Control/commit/98a4a75) — Use confirmed silent sources for calibration helper version 2026.10.18.6.
- [2026.10.18.5](https://github.com/cnieuweboer/Sony-Remote-Control/commit/b742877) — Add muted calibration preflight and safe restoration helper for version 2026.10.18.5.
- [2026.10.18.3](https://github.com/cnieuweboer/Sony-Remote-Control/commit/02da9ca) — Fix Windows hook forwarding and responsiveness in version 2026.10.18.3.
- [2026.10.18.2](https://github.com/cnieuweboer/Sony-Remote-Control/commit/d0e25e7) — Add local amplifier IPC and volume routing for version 2026.10.18.2.

## 2026-10-17

- [2026.10.17.7](https://github.com/cnieuweboer/Sony-Remote-Control/commit/278c48b) — Align remote structure and behavior for version 2026.10.17.7.

See the [full commit history](https://github.com/cnieuweboer/Sony-Remote-Control/commits/main/) for earlier changes and documentation updates.
