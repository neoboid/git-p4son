"""Depot root resolution: reading it from config and expanding $(workspace)."""

from dataclasses import dataclass

from .common import normalize_workspace_path
from .config import load_config
from .log import log
from .perforce import P4ClientSpec, get_client_spec, p4_depot_root_exists

# Substituted with the client name on each use, so the config survives a workspace rename.
WORKSPACE_PLACEHOLDER = '$(workspace)'
WORKSPACE_DEPOT_ROOT = f'//{WORKSPACE_PLACEHOLDER}'


def get_depot_root(workspace_dir: str) -> str | None:
    """Get the depot root from config, or None if not configured."""
    config = load_config(workspace_dir)
    return config.get('depot', {}).get('root')


def subdirectory_depot_root(directory: str, workspace_root: str) -> str | None:
    """Return the $(workspace)-based depot root for directory, or None at or outside the workspace root."""
    rel = normalize_workspace_path(directory, workspace_root)
    if rel in (None, '.'):
        return None
    return f'{WORKSPACE_DEPOT_ROOT}/{rel}'


def expand_depot_root(depot_root: str, workspace_name: str) -> str:
    """Substitute the live workspace name for the $(workspace) placeholder."""
    return depot_root.replace(WORKSPACE_PLACEHOLDER, workspace_name)


def check_depot_root(depot_root: str, workspace_name: str, workspace_dir: str) -> bool:
    """Report whether p4 accepts the expanded depot root, logging an error if not."""
    resolved = expand_depot_root(depot_root, workspace_name)
    if p4_depot_root_exists(resolved, workspace_dir):
        return True
    log.error(f'Depot root {resolved}/... is not valid')
    return False


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
