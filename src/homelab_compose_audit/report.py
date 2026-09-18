"""Render an AuditResult as JSON, markdown or plain text."""

from __future__ import annotations

import json

from .model import AuditResult

KIND_TITLES = {
    "host_port": "Host port collisions",
    "container_name": "Duplicate container names",
    "external_network": "Shared external networks",
    "bind_mount": "Overlapping bind mounts",
}

KIND_ORDER = ["host_port", "container_name", "external_network", "bind_mount"]


def to_json(result: AuditResult) -> str:
    payload = {
        "ok": result.ok,
        "summary": {
            "stacks": len(result.stacks),
            "services": sum(len(stack.services) for stack in result.stacks),
            "conflicts": len(result.conflicts),
            "warnings": len(result.warnings),
        },
        "stacks": [
            {"name": stack.name, "path": stack.path, "services": len(stack.services)}
            for stack in result.stacks
        ],
        "conflicts": [conflict.to_dict() for conflict in result.conflicts],
        "warnings": list(result.warnings),
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _grouped(result: AuditResult) -> list[tuple[str, list]]:
    groups: dict[str, list] = {}
    for conflict in result.conflicts:
        groups.setdefault(conflict.kind, []).append(conflict)
    ordered = [(kind, groups[kind]) for kind in KIND_ORDER if kind in groups]
    ordered.extend((kind, items) for kind, items in groups.items() if kind not in KIND_ORDER)
    return ordered


def to_markdown(result: AuditResult) -> str:
    lines = ["# docker-compose audit", ""]
    services = sum(len(stack.services) for stack in result.stacks)
    lines.append(
        f"Checked **{len(result.stacks)}** stack(s), **{services}** service(s) — "
        f"**{len(result.conflicts)}** conflict(s), **{len(result.warnings)}** warning(s)."
    )
    lines.append("")

    if result.stacks:
        lines.append("| Stack | File | Services |")
        lines.append("| --- | --- | --- |")
        for stack in result.stacks:
            lines.append(f"| `{stack.name}` | `{stack.path}` | {len(stack.services)} |")
        lines.append("")

    if not result.conflicts:
        lines.append("No conflicts found.")
        lines.append("")
    else:
        for kind, items in _grouped(result):
            lines.append(f"## {KIND_TITLES.get(kind, kind)}")
            lines.append("")
            for conflict in items:
                lines.append(f"- {conflict.detail}")
            lines.append("")

    if result.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def to_text(result: AuditResult) -> str:
    lines = []
    services = sum(len(stack.services) for stack in result.stacks)
    lines.append(
        f"Checked {len(result.stacks)} stack(s), {services} service(s): "
        f"{len(result.conflicts)} conflict(s), {len(result.warnings)} warning(s)."
    )

    if result.conflicts:
        for kind, items in _grouped(result):
            lines.append("")
            lines.append(f"{KIND_TITLES.get(kind, kind)}:")
            for conflict in items:
                lines.append(f"  - {conflict.detail}")
    else:
        lines.append("No conflicts found.")

    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in result.warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines) + "\n"


def render(result: AuditResult, fmt: str) -> str:
    if fmt == "json":
        return to_json(result)
    if fmt == "markdown":
        return to_markdown(result)
    return to_text(result)
