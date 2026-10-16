Fixture: a first-level dir with no config.json and no subdirs at all -- listRecordDirs
must classify it `invalid` (via readRecordDir), never recurse into it.
