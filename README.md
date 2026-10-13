# locdiff

Diff two ArcGIS locator `.loc` files and fail when the rebuild lost a matching setting. The
setting it watches by default is `supportsOptionalZone`, which ArcGIS Pro shows as "Match with
no zones".

A team rebuilt its address locators with a newer ArcGIS Pro release, ran its test addresses
through the new builds, and saw the same matches as before. The services were pointed at the
new builds. Then a different kind of search started to fail. An address typed with no city
returned no candidate at all on the server, although the same text still matched in Pro on the
desktop.

Nothing in the rebuild reported an error. The rebuild had written a fresh `.loc` file, and the
old one carried `supportsOptionalZone = true`, a setting somebody had changed by hand years
before. The new `.loc` had no such line. Every test that had a city in it still passed, because
the lost setting only matters when the city is missing.

This was observed on one site, on one ArcGIS Server release. I have not reproduced it anywhere
else, so read it as one observation, not as a rule about every release.

`locdiff` reads both files, lists every setting the rebuild dropped, added or changed, and exits
1 when a watched setting was lost. With `--apply`, it copies the old value into the new `.loc`
after it writes a numbered backup.

```
$ python locdiff.py --self-test
locdiff self-test: temp files only, no network, no credentials
--------------------------------------------------------------------
PASS  a rebuild that loses supportsOptionalZone = true is DROPPED  <-- pinned defect
PASS  and the gate fails on it
PASS  and the finding names the OLD line and value
PASS  the same text on both sides is SAME and passes
PASS  true in OLD and false in NEW is CHANGED  <-- pinned defect
PASS  false to true is CHANGED too: the gate does not pick a direction
PASS  dropping false, the documented default, is DROPPED-DEFAULT
PASS  and does not fail the gate, because nothing changes
...
PASS  a --watch key that neither file sets is UNKNOWN and fails  <-- pinned defect
...
PASS  a case-only respelling is the same key, not a drop  <-- pinned defect
PASS  and the respelling is reported
PASS  and it is not also listed as only OLD and only NEW  <-- pinned defect
PASS  the values True and TRUE are one boolean  <-- pinned defect
...
PASS  a repeated key keeps every line, so losing one of two is seen  <-- pinned defect
PASS  and the same repeated lines compare equal
...
PASS  a watched key set twice with two values in OLD is AMBIGUOUS  <-- pinned defect
...
PASS  the prefix --ap is refused with exit 64, not expanded  <-- pinned defect
...
PASS  and both files are byte for byte unchanged  <-- pinned defect
PASS  and no backup or other file was created
PASS  --apply restores the key and the re-check exits 0
PASS  the NEW file gains exactly the one line, in CRLF
PASS  the backup holds NEW as it was  <-- pinned defect
PASS  the OLD file is never written
...
PASS  a zip with key = value bytes inside, such as a .loz, exits 2 and is never written  <-- pinned defect
...
PASS  a hard link to OLD is the same file, never a pass  <-- pinned defect
...
PASS  a rename that fails leaves no temp file and no backup behind  <-- pinned defect
PASS  --apply restores every lost key, not only the first  <-- pinned defect
...
PASS  importing the module runs nothing
PASS  check() and raises() really do record a failure  <-- pinned defect
...
--------------------------------------------------------------------
135 assertions, 0 failed
```

The full run prints all 135 assertions. The `...` lines show where this block is cut.

## The existing alternatives

**A plain text diff** (`diff`, `fc`, `git diff --no-index`) is the first tool to reach for, and
it does one thing better than this tool: it shows every changed byte and hides nothing. On a
rebuilt locator, though, it also shows the rebuild time stamps, which change on every build. It
shows `FALSE` against `false` as a change. It shows `supportsOptionalZone` against
`SupportsOptionalZone` as one line removed and one line added. It has no idea which lines change
matching, and it cannot fail a scheduled job on one key.

**The Locator Properties dialog in ArcGIS Pro** is the authoritative view of one locator. It
shows one locator at a time, though, and it compares nothing.

A search of GitHub (`gh search repos locdiff`, `"locator diff"` and `supportsOptionalZone`, on
2026-10-09) found no tool that diffs two `.loc` files. Two unrelated projects share the name: an
R package on CRAN called `locdiff` (a local diffusion exponent), and a repository called
`LocDiff` with no description and no README. Neither reads a locator.

## What Esri documents

Each behaviour this tool relies on, and where it comes from:

- **The setting and its default.** Esri's bug report
  [BUG-000127758](https://support.esri.com/en-us/bug/clipping-a-new-locator-in-arcgis-pro-with-supportsoptio-bug-000127758)
  says: "The "Match with no zones" property (AKA supportsOptionalZones) is for use with locators
  that are in small areas like cities which have no zones in the data. When this property is set
  to true, any search can be done without a zone. If the property is set to false, which is the
  default value, the locator requires a zone when searching. If no zones are used when building
  the locator, this property will be set to true when the locator is built." That default is why
  a dropped `false` is `DROPPED-DEFAULT` and passes, and a dropped `true` fails.
- **Esri's advice against it.** The ArcGIS Pro help topic
  [Tips for improving geocoding quality](https://doc.esri.com/en/arcgis-pro/latest/help/data/geocoding/tips-for-improving-geocoding-quality.html)
  calls "Match with no zones" "a legacy setting for locators that were built without
  administrative zones". It recommends that you do not build locators without administrative
  zones, because it "can result in poor geocoding quality when matching addresses that have the
  same street name". Read that before you restore the setting with `--apply`. This tool reports
  that the setting changed. It does not say which value is right for your locator.
- **The file format.** Esri does not publish a specification for the `.loc` file. The format
  this tool reads comes from Esri's own sample locator in
  [Esri/data-collection-dotnet](https://github.com/Esri/data-collection-dotnet/blob/main/src/DataCollection.Shared/Resources/AddressLocator.loc):
  one `key = value` per line, `;` comment lines, `BatchPresortInputs` set on three lines in a
  row, `RebuildStartTime` and `RebuildStartTimeAscii` build stamps, and a
  `LocatorWorkspaceString` value that itself holds `=`. That sample spells the key
  `supportsOptionalZone`.
- **Two spellings.** A public city locator
  ([CityofSyracuse/Syracuse-GeocodingLocators](https://github.com/CityofSyracuse/Syracuse-GeocodingLocators/blob/main/SyrStreets.loc))
  spells the same key `SupportsOptionalZone`, with a capital S, writes disabled settings as
  `; CategoriesFilter =`, and writes booleans as both `FALSE` and `true`. Esri's sample spells it
  `supportsOptionalZone`. Esri does not document whether the reader folds key case. Because public
  files use both spellings, this tool treats them as one key and prints the respelling, so that a
  human can check it. Esri's bug text writes `supportsOptionalZones` with an s. That is a
  different name, which this tool does not match.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
credentials. A `.loc` file is plain text, so any machine that can see the files can run it.

The self-test prints 135 assertions on each interpreter it was run on: Windows Python 3.13.2,
Ubuntu Python 3.12.3, and CPython 3.9.25 on both hosts. The output was identical line for line
on Windows 3.13 and Ubuntu 3.12.

```
git clone https://github.com/uhsear/locdiff.git
```

## Usage

Point it at the `.loc` of the build that worked (OLD), then the `.loc` of the rebuild (NEW). A
path without `.loc` reads the `.loc` beside it.

```
python locdiff.py --self-test
python locdiff.py old/Streets.loc new/Streets.loc
python locdiff.py old/Streets.loc new/Streets
python locdiff.py old/Streets.loc new/Streets.loc --watch supportsOptionalZone --watch ComprehensiveZoneMatch
python locdiff.py old/Streets.loc new/Streets.loc --ignore Description
python locdiff.py old/Streets.loc new/Streets.loc --apply
```

| Flag | Default | What it does |
|---|---|---|
| `OLD` | none | The `.loc` of the build that worked. Never written. |
| `NEW` | none | The `.loc` of the rebuild. Written only with `--apply`. |
| `--watch KEY` | `supportsOptionalZone` | A key whose loss fails the run. Repeat it. Given at all, it replaces the default, so name `supportsOptionalZone` again to keep it. A key that neither file sets is `UNKNOWN` and fails, so a misspelling cannot pass. |
| `--ignore KEY` | none | A key to leave out of the other differences, on top of the two build stamps. Repeat it. |
| `--apply` | off | Copy OLD's value of each dropped or changed watched key into NEW, after a backup. |
| `--self-test` | off | Run the assertions and exit. |

Every flag must be spelled in full. `--ap` is refused with exit 64, not read as `--apply`. A file
may come before or after a flag, on Python 3.9 as on 3.13.

## What it reports

These runs use synthetic `.loc` files, `old/Streets.loc` and `new/Streets.loc`. The repo ships no
fixtures, so you cannot rerun these exact outputs from it. The self-test builds its own files and
covers the same behaviours. The rebuild dropped two settings, and it wrote one boolean in a
different case.

```
$ python locdiff.py old/Streets.loc new/Streets.loc
locdiff: OLD old/Streets.loc (11 settings), NEW new/Streets.loc (9 settings)
watched keys:
  DROPPED          supportsOptionalZone  OLD line 11 = true, absent from NEW
other differences: 1
  only OLD  ComprehensiveZoneMatch = true
ignored (build stamps and --ignore): RebuildStartTime, RebuildStartTimeAscii

VERDICT: supportsOptionalZone lost or unclear in NEW. Searches that relied on a lost setting may stop matching.

Check only. NEW was not written. Re-run with --apply to restore 1 key(s) from OLD.
```

The exit code is 1. `SuggestPartialSubaddresses = FALSE` in OLD and `= false` in NEW is not
listed, because a boolean's case is folded. The two build stamps differ and are not listed
either.

Each watched key gets one verdict:

| Verdict | Meaning | Fails |
|---|---|---|
| `SAME` | One value on both sides. | no |
| `ABSENT` | Neither file sets the default key. With `--watch`, this is `UNKNOWN`. | no |
| `ADDED` | Only NEW sets it. | no |
| `DROPPED-DEFAULT` | Only OLD sets it, to the value Esri documents as the default. Nothing changes. | no |
| `DROPPED` | Only OLD sets it. | yes |
| `CHANGED` | Both set it, to different values. Either direction fails: the tool does not pick one. | yes |
| `AMBIGUOUS` | One file sets it on two lines with two values. Which line wins is not documented. | yes |
| `UNKNOWN` | Named with `--watch`, and neither file sets it. Often a misspelling. | yes |

A misspelled key fails, and the report points at the likely cause:

```
$ python locdiff.py old/Streets.loc new/Streets.loc --watch supportsOptionalZones
locdiff: OLD old/Streets.loc (3 settings), NEW new/Streets.loc (2 settings)
watched keys:
  UNKNOWN          supportsOptionalZones  set by neither file. Check the spelling: the name must match exactly, apart from case.
other differences: 1
  only OLD  supportsOptionalZone = true
ignored (build stamps and --ignore): RebuildStartTime

VERDICT: supportsOptionalZones lost or unclear in NEW. Searches that relied on a lost setting may stop matching.
```

The exit code is 1. This run used a three-line synthetic file, not the 11-setting one above.

Only `supportsOptionalZone` has a documented default. Another watched key dropped at `false` is
`DROPPED`, because the tool cannot know what absent means for it.

Every other difference is listed as `only OLD`, `only NEW` or `differs`, and none of them fails
the run. A key that repeats, such as `BatchPresortInputs`, is compared as the whole list of its
lines, so a rebuild that loses one of its presort fields is listed.

A key that only changed case is listed under `respelled` and is not a drop:

```
$ python locdiff.py old/Streets.loc new/Respelled.loc
locdiff: OLD old/Streets.loc (11 settings), NEW new/Respelled.loc (11 settings)
watched keys:
  SAME             supportsOptionalZone  true on both sides
respelled (case folded, so not a drop; whether the reader folds case is not documented):
  supportsOptionalZone -> SupportsOptionalZone
other differences: 0
ignored (build stamps and --ignore): RebuildStartTime, RebuildStartTimeAscii

VERDICT: no watched key was lost.
```

## The restore

`--apply` copies OLD's value of each `DROPPED` or `CHANGED` watched key into NEW:

```
$ python locdiff.py old/Streets.loc new/Streets.loc --apply
...
wrote new/Streets.loc (backup new/Streets.loc.bak.1): supportsOptionalZone = true
re-check:
  SAME             supportsOptionalZone  true on both sides
```

The exit code is the re-check's, here 0. What the write does, and what it does not do:

- It writes `NEW.bak.N` first, with the first free `N`. An earlier backup is never overwritten.
- It changes only the lines it sets. A line NEW already has is rewritten in place, in NEW's
  spelling and line ending. A missing key is appended, in the file's own line ending, CRLF or LF.
  Every other byte is kept, including a UTF-8 BOM and a file that is not UTF-8.
- It writes a temporary file beside NEW, then renames it over NEW. If either step fails, it
  removes the temporary file and the backup, so a retry leaves nothing behind.
- It never writes OLD.
- It refuses the whole write when a watched key is `AMBIGUOUS`, because there is no one value to
  restore. The exit code is 1 and nothing is written.
- It refuses before the disk is touched when NEW's encoding cannot hold the value. The exit code
  is 2.
- With nothing to restore, it writes nothing, not even a backup.

The tool does not rebuild the locator, restart a service or republish anything. Whether a
running geocode service reads the changed `.loc` is outside what this tool checks. If a service
reads a copy of the locator instead of the file you changed, the copy keeps the old setting.
[svcsource](https://github.com/uhsear/svcsource) reports which kind each service is.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No watched key was lost. After `--apply`, the re-check passed. |
| 1 | A watched key was dropped, changed, ambiguous, or named with `--watch` and set by neither file. |
| 2 | A file could not be read or written, it is binary, or it holds no `key = value` line. |
| 64 | Usage error: an unknown flag, one file, or one file given as both OLD and NEW. |

A binary file exits 2 and names the `.loc` to pass instead. That covers a `.loz` (a zip) and a
UTF-16 text file, because both hold NUL bytes or start with the zip signature. A `.loz` is never
written, even with `--apply`. The same file given twice exits 64, because that diff could only
pass. Two names for one file, such as a symlink or a hard link, count as the same file.

## Limits

- It reads the `.loc` text file only. It refuses the `.loz` and does not open the `.loc.xml`. It
  does not query a geocode service, so it opens no socket.
- It reads settings, not results. It cannot tell you that an address stopped matching. It tells
  you that a setting which controls matching is different. Geocode a set of addresses with no
  city through both builds to see the effect.
- The file format comes from Esri's sample files, not from a published specification. A key
  must start with a letter and hold only letters, digits, `_` and `.`. Anything else is skipped.
- Key case is folded. Esri does not document whether the reader folds case, so a respelling is
  printed for a human to check.
- Only `supportsOptionalZone` has a documented default. Add another key with `--watch`, and its
  drop always fails.
- The evidence is one observation on one site and one server release. The tool flags the
  change. It does not prove that a change breaks your searches.
- A file that is not valid UTF-8 is read as Latin-1, so a non-ASCII value prints as an escape.
  The bytes on disk are kept exactly.
- A file with a UTF-8 BOM and also one byte that is not valid UTF-8 is read as Latin-1, and the
  BOM then spoils its first line. A watched key on that line reads as absent. Keep the two apart.
- A UTF-16 file exits 2 as binary. Save a `.loc` as UTF-8 or ANSI.
- Line endings must be LF or CRLF. A file with only CR line endings is read as one line, so its
  settings are not seen.
- The default key, when neither file sets it, is `ABSENT` and passes. A pair of files that never
  had the setting, or the wrong pair, reads as clean. Read the `ABSENT` line. A key named with
  `--watch` does not behave this way: it is `UNKNOWN` and fails.
- The tool cannot tell which file is older. If you swap OLD and NEW, a dropped setting reads as
  `ADDED` and the run passes.
- It matches the key name exactly, apart from case. Esri's bug text writes `supportsOptionalZones`
  with an s. A file that uses that name needs `--watch supportsOptionalZones`.
- Only `RebuildStartTime` and `RebuildStartTimeAscii` are build stamps. Another key a rebuild
  rewrites, such as `BuildDate`, can appear under other differences. Add it with `--ignore`.
- A value is printed as written, with no redaction. If a `.loc` holds a connection string with a
  password, the report and any log of it show the password. Do not publish that output.
- NEW is read once. If it changes between the read and the `--apply` write, for example while a
  rebuild still runs, the write replaces that change.
- `--apply` replaces the file, so check its permissions and owner afterwards. Its mode bits, owner
  and Windows ACLs are not kept, and links to it are not followed: a symlink at NEW becomes a
  regular file.
- The re-check after `--apply` cannot fail on any input, because the write sets the exact value
  the plan read. It stays in the code as a guard.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [svcsource](https://github.com/uhsear/svcsource) - report what is behind every service on an
  ArcGIS Server site, including whether each geocode service reads its locator in place or a
  copy. Run it before `--apply`, so that you change the file the service reads.
- [geocodesift](https://github.com/uhsear/geocodesift) - audit a geocoded batch instead of
  believing its match rate. It sees the effect in the results; this tool sees the cause in the
  settings.
- [svcguard](https://github.com/uhsear/svcguard) - make sure a service restarts around risky
  maintenance, such as a locator swap, even on failure.
