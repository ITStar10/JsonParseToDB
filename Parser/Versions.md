# Versions

All notable changes to the Parser are listed here, newest first.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
version numbers follow [Semantic Versioning](https://semver.org/):
MAJOR.MINOR.PATCH, where before 1.0.0 a breaking change raises MINOR and a fix raises PATCH.

## [0.2.0] - 2026-11-23

### Changed

- **Breaking:** renamed the column `useOrders` to `userOrders` (JSON key and database column).
  Databases created by 0.1.0 use the old name and can't be appended to; create a new database from the JSON files.

## [0.1.0] - 2026-11-18

### Added

- Parse a folder of JSON files into one SQLite table with fixed column names.
