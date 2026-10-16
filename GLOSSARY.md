# git-p4son

git-p4son keeps a local git repository inside a Perforce workspace, with the sync branch in sync with the depot and
local work turned into Perforce changelists. The repository is driven through git or a colocated jj workspace.

## Language

**Backend**:
The tool, git or jj, whose idea of the change, the base and the branch a command follows.
_Avoid_: VCS, mode

**Change**:
The work a changelist is made from: the commits from the base to the tip.
_Avoid_: git change, selected change, feature

**Tip**:
The commit holding the change's latest state: git's `HEAD`, or jj's working change unless it is empty and new, then
its parent.
_Avoid_: head, selected revision

**Base**:
Where the change starts; the commits after it up to the tip make up the change.
_Avoid_: base branch, parent

**Working change**:
jj's `@`, the change jj snapshots the working copy into. git has no counterpart.
_Avoid_: working copy commit, `@` commit

**Alias**:
A name for a pending changelist, local to the workspace. A branch or bookmark only suggests the name.
_Avoid_: changelist name, branch alias

**Sync branch**:
The branch (git) or bookmark (jj) that sync commits land on, usually `main`.
_Avoid_: main, sync bookmark, depot branch
