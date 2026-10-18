# Release history

Release identifiers below come from the corresponding commits. Some releases updated only the calibration helper; the remote and helper can therefore have different version numbers. Historical behavior may have been superseded by later releases.

## 2026-10-18

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
