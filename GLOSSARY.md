# git-p4son

git-p4son keeps a local git repository inside a Perforce workspace, with the sync branch in sync with the depot and
local work turned into Perforce changelists.

## Language

**Change**:
The work a changelist is made from: the commits from the base to the tip.
_Avoid_: git change, selected change, feature

**Tip**:
The commit holding the change's latest state: `HEAD`.
_Avoid_: head, selected revision

**Base**:
Where the change starts; the commits after it up to the tip make up the change.
_Avoid_: base branch, parent

**Alias**:
A name for a pending changelist, local to the workspace. A branch only suggests the name.
_Avoid_: changelist name, branch alias

**Sync branch**:
The branch that sync commits land on, usually `main`.
_Avoid_: main, depot branch
