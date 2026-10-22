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
   - **Running:** a live process of its script exists (`pgrep -fl <the script's path in this checkout>`). Wait
     for it to end, checking about every 30 seconds with whatever waiting your tools allow, then judge again.
     If its log shows no new output for 15 minutes, stop waiting and ask the human.
   - **Failed:** no live process and no marker, but a lock or log shows it ran. Read the end of the log, then
     run the setup once yourself.
   - **Never run:** no marker, lock, log or process. Check once more about 30 seconds later, since a
     session-start hook may not have begun yet, then run it once yourself.

   When you run it yourself, give the command a timeout long enough for it (up to 10 minutes), or run it in
   the background with its output in a log you watch. A call that timed out is not a failure and not a reason
   to start it again: judge its state as above.
3. Confirm it succeeded (exit status 0, and the marker if there is one) before you rely on it.
4. Setup must leave no tracked change. Note `git status --porcelain` before you run it. Afterwards, restore
   with `git checkout -- <file>` each tracked file it changed that was clean before, such as a lockfile. If it
   changed a file you had already edited, ask the human.

This setup inside your worktree is not an activation step: the Activation rule against installs does not
apply to it. If setup fails, that is setup trouble, not a finding about your task. Do not report it as a
product bug, and do not work around it with an install of your own. Ask the human with your ask tool, giving
the command, its exit status and the end of its log.
