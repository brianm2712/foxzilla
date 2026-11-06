"""
Media pickup watcher.  v2

Watches an SFTP drop folder for new movies/shows, waits for the upload to
go quiet, organizes it, hands it to the Jellyfin library roots and triggers a
rescan.

v2 exists because v1 lost episodes and duplicated files. The handoff is now a
three-phase transaction and the source is never touched until the destination
has been proven correct:

  Phase 1  MANIFEST   walk the incoming item; record every file's relative
                      path, size and SHA-256. This is the contract for what
                      must arrive, and it is what makes a missing episode a
                      detectable condition rather than an invisible one.
  Phase 2  STAGE      copy (never move) into a staging dir on the destination
                      filesystem, hashing as we write, and check every file
                      against the manifest. Reorganising happens here, where
                      it cannot touch the source.
  Phase 3  COMMIT     staging is on the destination filesystem, so each file
                      lands by atomic rename. Reconcile the placed files
                      against the manifest, and only then remove the source.

Invariants, all of which v1 broke:
  - Exactly one run at a time (flock). v1 raced itself every 5 minutes.
  - Nothing is ever deleted to make room for something else. Leftovers go to
    _leftovers/, conflicts go to quarantine. v1 called shutil.rmtree() on the
    source of a multi-movie pack.
  - A file already at the destination is compared by hash, not renamed to
    "name (1).mkv". Identical means skip, different means quarantine. The
    "(1)" suffix was the duplication the user kept seeing.
  - A failure anywhere leaves the source intact and the run retryable.

Run on a timer (cron, systemd, or the bundled container).
"""
