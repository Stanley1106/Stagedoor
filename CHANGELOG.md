# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/Stanley1106/stagedoor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Stanley1106/stagedoor/releases/tag/v0.1.0
