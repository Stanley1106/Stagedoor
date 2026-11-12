# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-11-12

### Added

- Version history per deck at `/publish/history/<id>`: versions grouped by
  day, marked Current and Public, with Restore and a one-click Undo.
  `history` and `restore` commands do the same from a shell.
- `slides-autosave`: commits `slides/` to git about 30 seconds after edits
  stop, whether they come from the inspector, the agent panel or an editor.
  The installer sets it up.
- Every publish is committed and recorded as the deck's public version.

### Changed

- Unpublish and restore confirm in a dialog styled like open-slide's, not the
  browser's `confirm()`.
- README: scope and non-goals; the installer is described as a reference
  deployment.

## [0.2.0] - 2026-11-12

### Added

- Published decks are live: they republish automatically about a minute
  after edits stop (`STAGEDOOR_SYNC_DELAY`). A failed sync keeps the last
  good build and waits for the next edit before retrying.
- Pause and Resume per deck, Sync Now, and `pause`, `resume` and `sync`
  commands.

### Changed

- Decks published with 0.1.0 become live on upgrade.

## [0.1.0] - 2026-11-12

### Added

- Publisher that snapshots decks and builds a public site containing only
  published ones, with atomic release swaps and rollback on failed builds.
- Web UI at `/publish` with per-deck status (draft, published, unpublished
  changes, deleted in editor), publish/unpublish actions and the time each
  deck was last published. Styled after open-slide's own interface.
- `status`, `publish`, `unpublish` and `rebuild` commands.
- `deploy/install.sh` for Ubuntu/Debian, systemd units, a Caddyfile with
  `/<deck>` short links, and Proxmox network examples.

[Unreleased]: https://github.com/Stanley1106/stagedoor/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Stanley1106/stagedoor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Stanley1106/stagedoor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Stanley1106/stagedoor/releases/tag/v0.1.0
