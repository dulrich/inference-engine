"""Owner modules for `scripts/sync-openwebui-capabilities.py`.

One synchronization domain, four seams -- deliberately not a framework:

  * `types`        -- the shapes that cross a seam (router entries, Open WebUI rows,
                      visibility entries, policy, desired rows, retirement partition).
  * `declarations` -- repo-owned inputs: visibility, arming/retirement policy, effort ladder.
                      Every loader is fail-closed; none degrades to a permissive default.
  * `planner`      -- pure decisions: create/update/toggle/retire, managed-field merges.
                      No I/O of any kind lives here.
  * `client`       -- Open WebUI/router endpoint shapes, auth, timeouts, read-after-write.
  * `runner`       -- per-row execution and operator output, composing planner + client.

The entry script keeps `parse_args` and `main` (the CLI coordinator) and stays the stable
invocation path: `python3 scripts/sync-openwebui-capabilities.py ...`.
"""
