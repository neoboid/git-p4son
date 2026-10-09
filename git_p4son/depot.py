"""Depot root resolution: reading it from config and expanding $(workspace)."""

from dataclasses import dataclass

from .config import load_config
from .log import log
from .perforce import P4ClientSpec, get_client_spec

# Placeholder allowed in a stored depot root, substituted with the live
# Perforce client (workspace) name each time the root is used. Storing e.g.
# root = "//$(workspace)/Engine" keeps the config working after the workspace
# is renamed, at the cost of one client-name lookup per command.
WORKSPACE_PLACEHOLDER = '$(workspace)'


def get_depot_root(workspace_dir: str) -> str | None:
    """Get the depot root from config, or None if not configured."""
    config = load_config(workspace_dir)
    return config.get('depot', {}).get('root')


def expand_depot_root(depot_root: str, workspace_name: str) -> str:
    """Substitute the live workspace name for the $(workspace) placeholder."""
    return depot_root.replace(WORKSPACE_PLACEHOLDER, workspace_name)


@dataclass
class ResolvedDepot:
    """The configured depot root with any placeholder expanded, plus its client spec."""
    depot_root: str
    client_spec: P4ClientSpec | None


def resolve_depot_root(workspace_dir: str) -> ResolvedDepot | None:
    """Resolve the configured depot root, or None (with an error logged)."""
    log.heading('Finding depot root')
    depot_root = get_depot_root(workspace_dir)
    if not depot_root:
        log.error('No depot root configured. Run "git p4son init" first.')
        return None

    client_spec = get_client_spec(workspace_dir)
    if WORKSPACE_PLACEHOLDER in depot_root and not client_spec:
        log.error('Cannot resolve $(workspace) in depot root: not inside a '
                  'Perforce workspace')
        return None
    if client_spec:
        depot_root = expand_depot_root(depot_root, client_spec.name)
    log.success(depot_root)
    return ResolvedDepot(depot_root=depot_root, client_spec=client_spec)
