Muster does not install dependencies for you. When you start, this checkout's setup may be finished, still
running in the background (a post-checkout or session-start hook may have started it), or not run at all.
Before the first test, build or command that needs dependencies, and only if your task needs one:

1. Find the repository's setup and whatever runs it: its agent instructions, README, contributing guide and
   setup scripts, its post-checkout hook (`git rev-parse --git-path hooks/post-checkout`), and any
   SessionStart hook in your Claude Code settings. Together they name the setup command, its completion
   marker, and the lock and log of a run in the background. Use only the commands they define. Never invent
   an install command, and never install what your task does not need.
2. Judge its state, and never start a second run of it:
   - **Done:** its completion marker exists, or its documented check passes. Use it.
   - **Running:** its lock, or a live process of its script, exists. Wait for it to end: `sleep 30` between
     short checks, then judge again. If its log shows no new output for 15 minutes, stop waiting and ask the
     human.
   - **Failed:** it ended without its marker, or its log shows an error. Read the end of the log, then run the
     setup once yourself, in the foreground.
   - **Never run:** run it once, in the foreground.
3. Confirm it succeeded (exit status 0, and the marker if there is one) before you rely on it.
4. Setup must leave no tracked change: if it rewrote a tracked file such as a lockfile, restore that file with
   `git checkout -- <file>`.

If setup fails, that is setup trouble, not a finding about your task. Do not report it as a product bug, and do
not work around it with an install of your own. Ask the human with your ask tool, giving the command, its exit
status and the end of its log.
