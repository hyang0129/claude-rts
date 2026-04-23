"""One-shot data migration scripts for breaking on-disk schema changes.

Each module here owns one migration. Migrations are invoked explicitly through
``python -m claude_rts --migrate-...`` CLI flags — never automatically at
startup. The server's startup path may *probe* for unmigrated state and refuse
to boot with a clear error pointing at the right CLI flag.
"""
