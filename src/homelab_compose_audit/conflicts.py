"""Cross-stack conflict detection.

Every check compares *different* stacks: two services inside one compose file are
docker's own problem (and a duplicate YAML key is a parse error), whereas two
independently maintained stacks have nothing stopping them from claiming the
same host port, container name, external network or host directory.
"""

from __future__ import annotations

import os
from itertools import combinations

from .model import Conflict, PortBinding, Stack

#: Stack name used for ports supplied via --occupied-port / --occupied-ports-file.
OCCUPIED_STACK = "(already occupied)"

KIND_PORT = "host_port"
KIND_CONTAINER_NAME = "container_name"
KIND_EXTERNAL_NETWORK = "external_network"
KIND_BIND_MOUNT = "bind_mount"


def _where(stack: Stack | str, service: str | None = None, path: str | None = None) -> dict:
    if isinstance(stack, Stack):
        entry = {"stack": stack.name, "path": stack.path}
    else:
        entry = {"stack": stack, "path": path or ""}
    if service:
        entry["service"] = service
    return entry


def check_host_ports(
    stacks: list[Stack], occupied: list[PortBinding] | None = None
) -> list[Conflict]:
    """Host ports claimed by more than one stack (or already taken on the host)."""
    # (stack_name, service_name, binding) for every published port.
    claims: list[tuple[str, str | None, str, PortBinding]] = []
    for stack in stacks:
        for service in stack.services:
            for binding in service.ports:
                claims.append((stack.name, service.name, stack.path, binding))
    for binding in occupied or []:
        claims.append((OCCUPIED_STACK, None, "", binding))

    conflicts: list[Conflict] = []
    seen: set[tuple] = set()
    for left, right in combinations(claims, 2):
        left_stack, left_service, left_path, left_binding = left
        right_stack, right_service, right_path, right_binding = right
        if left_stack == right_stack:
            continue
        if not left_binding.collides_with(right_binding):
            continue

        key = (
            left_binding.host_port,
            left_binding.protocol,
            tuple(sorted([(left_stack, left_service), (right_stack, right_service)])),
        )
        if key in seen:
            continue
        seen.add(key)

        detail = (
            f"host port {left_binding.host_port}/{left_binding.protocol} is claimed by "
            f"{_claim_text(left_stack, left_service, left_binding)} and "
            f"{_claim_text(right_stack, right_service, right_binding)}"
        )
        conflicts.append(
            Conflict(
                kind=KIND_PORT,
                detail=detail,
                where=[
                    _where(left_stack, left_service, left_path),
                    _where(right_stack, right_service, right_path),
                ],
            )
        )
    return conflicts


def _claim_text(stack: str, service: str | None, binding: PortBinding) -> str:
    if service is None:
        return f"{stack} [{binding.describe()}]"
    return f"{stack}/{service} [{binding.describe()}]"


def check_container_names(stacks: list[Stack]) -> list[Conflict]:
    """The same ``container_name:`` used by two stacks — docker refuses the second."""
    by_name: dict[str, list[tuple[Stack, str]]] = {}
    for stack in stacks:
        for service in stack.services:
            if service.container_name:
                by_name.setdefault(service.container_name, []).append((stack, service.name))

    conflicts: list[Conflict] = []
    for name, users in sorted(by_name.items()):
        distinct_stacks = {stack.name for stack, _ in users}
        if len(distinct_stacks) < 2:
            continue
        where = [_where(stack, service) for stack, service in users]
        listed = ", ".join(f"{stack.name}/{service}" for stack, service in users)
        conflicts.append(
            Conflict(
                kind=KIND_CONTAINER_NAME,
                detail=f"container_name '{name}' is used by {listed}",
                where=where,
            )
        )
    return conflicts


def check_external_networks(stacks: list[Stack]) -> list[Conflict]:
    """The same external network name referenced by two stacks.

    This is reported because it couples otherwise-independent stacks: they share
    one L2 segment and a name that must already exist. It is frequently
    intentional, so the CLI offers ``--allow-shared-network``.
    """
    by_name: dict[str, list[Stack]] = {}
    for stack in stacks:
        for network in stack.external_networks:
            by_name.setdefault(network.name, []).append(stack)

    conflicts: list[Conflict] = []
    for name, users in sorted(by_name.items()):
        distinct = {stack.name for stack in users}
        if len(distinct) < 2:
            continue
        conflicts.append(
            Conflict(
                kind=KIND_EXTERNAL_NETWORK,
                detail=(
                    f"external network '{name}' is shared by "
                    + ", ".join(sorted(distinct))
                ),
                where=[_where(stack) for stack in users],
            )
        )
    return conflicts


def _paths_overlap(left: str, right: str) -> bool:
    """True when two host paths are equal or one contains the other.

    Only compares lexically — the filesystem is never touched, so this works on
    fixtures and on a machine that does not host the stacks.
    """
    if not left or not right:
        return False
    if left == right:
        return True
    # Relative and absolute paths are not comparable without a working directory.
    if os.path.isabs(left) != os.path.isabs(right):
        return False
    left_parts = left.split(os.sep)
    right_parts = right.split(os.sep)
    if len(left_parts) <= len(right_parts):
        shorter, longer = left_parts, right_parts
    else:
        shorter, longer = right_parts, left_parts
    return longer[: len(shorter)] == shorter


def check_bind_mounts(stacks: list[Stack]) -> list[Conflict]:
    """Host paths bind-mounted by two stacks, including nested paths.

    Two stacks mounting the same directory read-only is not flagged: concurrent
    readers are safe, it is the writers that corrupt each other's state.
    """
    claims: list[tuple[Stack, str, object]] = []
    for stack in stacks:
        for service in stack.services:
            for bind in service.binds:
                claims.append((stack, service.name, bind))

    conflicts: list[Conflict] = []
    seen: set[tuple] = set()
    for left, right in combinations(claims, 2):
        left_stack, left_service, left_bind = left
        right_stack, right_service, right_bind = right
        if left_stack.name == right_stack.name:
            continue
        if left_bind.read_only and right_bind.read_only:
            continue
        if not _paths_overlap(left_bind.host_path, right_bind.host_path):
            continue

        key = tuple(
            sorted(
                [
                    (left_stack.name, left_service, left_bind.host_path),
                    (right_stack.name, right_service, right_bind.host_path),
                ]
            )
        )
        if key in seen:
            continue
        seen.add(key)

        if left_bind.host_path == right_bind.host_path:
            detail = (
                f"host path '{left_bind.host_path}' is bind-mounted by "
                f"{left_stack.name}/{left_service} and {right_stack.name}/{right_service}"
            )
        else:
            detail = (
                f"host paths overlap: '{left_bind.host_path}' "
                f"({left_stack.name}/{left_service}) and '{right_bind.host_path}' "
                f"({right_stack.name}/{right_service})"
            )
        conflicts.append(
            Conflict(
                kind=KIND_BIND_MOUNT,
                detail=detail,
                where=[
                    _where(left_stack, left_service),
                    _where(right_stack, right_service),
                ],
            )
        )
    return conflicts


def find_conflicts(
    stacks: list[Stack],
    occupied: list[PortBinding] | None = None,
    allow_shared_network: bool = False,
) -> list[Conflict]:
    """Run every check and return the conflicts in a stable order."""
    conflicts: list[Conflict] = []
    conflicts.extend(check_host_ports(stacks, occupied))
    conflicts.extend(check_container_names(stacks))
    if not allow_shared_network:
        conflicts.extend(check_external_networks(stacks))
    conflicts.extend(check_bind_mounts(stacks))
    return conflicts
