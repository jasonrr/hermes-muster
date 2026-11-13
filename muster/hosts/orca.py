from pathlib import Path

NAME = "orca"
ROOT = "~/orca/workspaces"  # Orca's default workspace directory: <ROOT>/<clone dir name>/<worktree name>


def worktree_path(clone, branch):
    """Where Orca puts a worktree named after the branch; git's checkout for an existing branch goes there too."""
    return str(Path(ROOT).expanduser() / Path(clone).name / branch.replace("/", "-"))
