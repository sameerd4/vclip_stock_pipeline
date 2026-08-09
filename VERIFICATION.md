# Verification

The release was checked at three levels.

## Automated regression tests

```bash
PYTHONPATH=src python3 -m pytest -q
```

Result:

```text
12 passed
```

Covered scenarios include:

- exact sibling SRT precedence over duplicate archive matches;
- four unrelated original projects reorganized into three location/date events;
- same-session natural and graded project families kept distinct;
- accepted and rejected candidate persistence;
- retimed clip rejection;
- stable candidate IDs across Stockify reruns;
- human trim and deletion reconciliation;
- partial reviewed XML scope safety;
- compilation-only layout reconciliation;
- unknown embedded ID handling;
- custom-metadata fallback through exact individual project names;
- reviewed compilation timecode persistence;
- export matching and public/internal package metadata;
- partial-package status;
- material duration-mismatch blocking.

## Command-line smoke test

A synthetic Final Cut XML was processed through the actual CLI entry point:

```text
stockify → reconcile → package → db status
```

Observed result:

```text
3 inferred shoot sessions
4 generated source-project compilations
7 accepted candidates
1 rejected retimed candidate
7 reconciled candidates
4 package folders
```

The test included:

- Capitol Hill, Seattle on December 9;
- Downtown Seattle on May 2;
- South Lake Union on May 9;
- separate Natural and Graded SLU project families;
- one human trim recorded without falsely marking unchanged graded effects as modified.

## Distribution check

A wheel was built with:

```bash
python3 -m pip wheel . --no-build-isolation --no-deps
```

The wheel was installed into a clean virtual environment, where these commands succeeded:

```text
vclip --version
vclip db status
```

FCPXML is an interchange format rather than a stable public automation API. Import generated XML into a test/new Final Cut library before applying the workflow to a large archive.
