# CHDK Release Mirror

An automated mirror of [CHDK](https://chdk.fandom.com/wiki/CHDK) release builds and their matching source.

**This is not the official CHDK distribution.** It is an unofficial mirror, maintained to give the
CHDK forum's camera index stable, self-hosted download links instead of hotlinking upstream.
For the canonical downloads, go to [build.chdk.photos](https://build.chdk.photos/).

## What's here

One GitHub Release per upstream stable revision, tagged `<version>-<revision>` (for example `1.6.1-6355`):

- the per-camera build archives, mirrored byte-for-byte from the official autobuild
- a source snapshot of the exact SVN revision those builds came from (`svn export`)
- `cameras.json` — the upstream build metadata, rewritten to point at the mirrored files

Stable releases appear roughly once a year. Development (trunk) builds are not mirrored.

## Upstream

| | |
|---|---|
| Project | [CHDK](https://chdk.fandom.com/wiki/CHDK) |
| Source | `https://subversion.assembla.com/svn/chdk` (Subversion) |
| Official builds | [build.chdk.photos](https://build.chdk.photos/) |
| Build metadata | `https://build.chdk.photos/builds/release/meta/build_info.json` |

## Verifying

Every mirrored archive is checked against the SHA-256 hash published in the upstream
`build_info.json` before it is released here, and those hashes are kept with each release so you
can repeat the check yourself.

## Credits

CHDK is the work of the CHDK project and its contributors, over more than fifteen years.

- The autobuild service at **build.chdk.photos**, and much of the current CHDK source, is
  maintained by **reyalP**. This mirror exists only because that service publishes clean,
  machine-readable build metadata.
- The long-running **mighty-hoernsche** autobuild has served the community for years and asks
  that its files are not linked directly — please respect that and use its pages.

Nothing here is original work beyond the sync tooling. All credit for CHDK belongs upstream.

## Licence

CHDK is licensed under the **GNU General Public License, version 2**. Mirrored files keep their
original licence, and each release ships the corresponding source alongside the builds, as GPL v2
requires. CHDK bundles Lua (MIT) and uBasic (BSD), which keep their own terms.

The sync workflow in this repository is MIT.

## Disclaimer

Not affiliated with, endorsed by, or connected to the CHDK project or Canon Inc.
CHDK modifies camera behaviour and is used at your own risk.
