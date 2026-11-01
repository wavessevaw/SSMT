# Interrupted install journal — experimental

The reproduction terminated the installer after moving the old runtime to `runtime.rollback`, before moving `runtime.new` into place. A new installer instance then refused to install because the rollback tree existed. No persistent metadata described which service files or watchdog markers belonged to the old installation.

The installer now writes a versioned JSON journal before changing runtime or service state. It stores file snapshots (including mode/owner and existing symlink representation), prior enablement and whether an old runtime existed. File contents use base64; journal data is parsed as JSON, never executed. Recovery uses a fixed list of installer paths rather than arbitrary paths from the journal. The journal is replaced atomically, its file is fsynced, and its containing directory is fsynced on POSIX.

Before the next Install or Remove operation, an uncommitted journal restores the previous runtime, files, watchdog markers and service enablement. A committed journal only finishes cleanup of obsolete runtime trees. Failed recovery retains the journal and recoverable data. A rollback tree without a journal remains protected and is not automatically deleted. POSIX flock serializes Install/Remove across processes and is released by the OS on process exit.

The tests cover the rename gap, first-install enablement, upgrade enablement with a subsequent rejected retry, repeated interruption during recovery, committed cleanup and malformed journal. A child process uses `os._exit(77)` at the rename gap, exercising a real exit without Python cleanup; the subsequent installation succeeds. The concurrency test runs on POSIX and explicitly skips on Windows, where the filesystem/systemd fixture is only a model. Existing recovery/watchdog assertions are retained.

Local initial result: 7 interruption tests passed, 1 POSIX-lock test explicitly skipped. Of 13 existing upgrade tests, 12 passed and the shell-based watchdog test could not run because Windows has no `sh`. Linux must run all tests, including flock and the real generated shell scripts, without these skips before integration.

## Limits that remain blocking

Follow-up boot guard: before either runtime rename, installation atomically replaces
the `disabled` marker and fsyncs its file and directories. This marker is understood
by the historical boot script. It remains until successful commit cleanup or rollback.
The new boot and profile scripts additionally return without executing the runtime
or applying tuning while an installation journal exists. A Linux shell test checks
both guards and a control run that reaches a fake runtime. This is a stock-boot
fallback, not early-boot automatic restoration of the old runtime. Older profile
scripts and interruption during config restoration still require separate coverage.

Runtime symlinks (including staging/rollback paths) are now rejected before staging
or journal creation. A regression test reproduced the previous behavior: an attempt
could leave a journal which its own recovery then refused. No test requirement was
removed. Local Windows runs explicitly skip POSIX lock and boot-shell execution;
Linux CI must execute both.

Recovery now also runs once during backend startup, before telemetry, migration,
actuators or controller loops are initialized. It restores the previous runtime
without reinstalling the bundle. A normal startup without a journal performs no
installer writes or systemd calls. Recovery errors are recorded and propagated;
controllers do not start over an unresolved journal. Unloading a partially
initialized backend is supported. Local tests cover recovery without reinstall,
no-op startup, failed initialization and lifecycle cleanup.

Recovery is **not automatic in the early-boot service**. This does not prove a safe boot following every interruption, a durable complete runtime after power loss, real systemd behavior or real bind-mount interactions. Runtime/config writes are not all atomic/durably flushed; journal fsync alone is not a power-loss guarantee. The bootstrap and watchdog paths still require independent boot fault injection and hardware verification. A retained or malformed journal can intentionally stop backend initialization and requires investigation; the plugin UI may consequently be unavailable until that failure is resolved.

Do not classify the complete installer blocker as fixed or issue a device-ready candidate on this evidence alone. This branch remains EXPERIMENTAL and must pass the candidate's full regression, including the unchanged historical guards. Rollback is a revert of this branch; Stable tags and main remain unchanged.

## Atomic configuration replacement

Installation now writes boot/profile scripts, the systemd unit and limits through
a same-directory temporary file: write, flush, set ownership/mode, fsync, replace,
then fsync the parent directory on POSIX. Rollback of regular-file snapshots uses
the same writer with the saved metadata. Existing symlink representation is kept;
its resolved target receives the complete replacement.

Injected file-fsync and rename failures preserve the previous complete file and
clean up temporary files. Rollback-flush failure also preserves the current
complete file for a subsequent recovery attempt. A POSIX test verifies symlink,
mode and owner preservation. Local Windows interruption tests: 13 PASS, 3 explicit
POSIX skips. Upgrade tests: 12 PASS, one cannot execute without `sh`; Linux remains
required. These results do not validate sudden power loss: runtime-tree durability,
directory removals and the complete boot sequence still require verification.

The staged runtime is now fsynced after capability setup and before transaction
creation: regular files first, directories bottom-up, without traversing symlink
targets. Each live-runtime directory rename is followed by a parent-directory
fsync. A failed staging flush leaves the previous runtime, service files and
watchdog markers unchanged and creates no recovery journal. A POSIX test observes
actual fsync descriptors and verifies file-before-directory ordering and exclusion
of an external symlink target. Complete sudden-power-loss validation, including
cleanup and recovery ordering, remains outstanding.
