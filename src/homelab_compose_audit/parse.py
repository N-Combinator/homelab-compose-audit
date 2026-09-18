"""Parse docker-compose files with PyYAML only.

No docker daemon is contacted and ``${VAR}`` interpolation is deliberately not
performed (out of scope for v0.1) — an unresolved variable simply yields a port
or path we cannot make sense of, which is skipped rather than guessed at.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .model import BindMount, ExternalNetwork, PortBinding, Service, Stack


class ComposeParseError(Exception):
    """A compose file could not be read or understood."""


def read_text(path: Path) -> str:
    """Read a file as text, tolerating a UTF-8 BOM.

    ``utf-8-sig`` strips a leading BOM when present and behaves exactly like
    ``utf-8`` when it is absent. A genuinely non-UTF-8 file raises
    ``ComposeParseError`` so the caller can record it as a warning instead of
    letting a ``UnicodeDecodeError`` traceback escape or — worse — dropping the
    file silently.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ComposeParseError(
            f"{path}: not valid UTF-8 ({exc.reason}) — skipped"
        ) from exc
    except OSError as exc:
        raise ComposeParseError(f"{path}: cannot read ({exc.strerror or exc}) — skipped") from exc


def _yaml_error_summary(exc: yaml.YAMLError) -> str:
    """Condense a PyYAML error into one line.

    PyYAML's ``str()`` spans several lines and quotes the offending source, which
    makes a warning list unreadable. The problem and its line:column are enough.
    """
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if problem and mark is not None:
        return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"
    if problem:
        return str(problem)
    return str(exc).split("\n", 1)[0]


def load_compose(path: Path) -> dict:
    """Load one compose file into a mapping, or raise ComposeParseError."""
    text = read_text(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        summary = _yaml_error_summary(exc)
        raise ComposeParseError(f"{path}: invalid YAML ({summary}) — skipped") from exc
    if data is None:
        # An empty file is legal YAML but has no stack in it.
        return {}
    if not isinstance(data, dict):
        raise ComposeParseError(
            f"{path}: top level is {type(data).__name__}, expected a mapping — skipped"
        )
    return data


def _services_of(doc: dict) -> dict:
    """Return the services mapping for compose v1, v2 and v3+.

    In v1 the services *are* the top-level keys; v2/v3 nest them under
    ``services``. We distinguish by looking for the v2+ reserved top-level keys.
    """
    services = doc.get("services")
    if isinstance(services, dict):
        return services
    if "version" in doc or "services" in doc or "networks" in doc or "volumes" in doc:
        # v2/v3 shaped document that simply declares no services.
        return {}
    # v1: every top-level mapping whose value looks like a service definition.
    return {k: v for k, v in doc.items() if isinstance(v, dict)}


def _split_proto(value: str) -> tuple[str, str]:
    """Split a trailing ``/tcp`` or ``/udp`` off a port spec."""
    if "/" in value:
        head, _, proto = value.rpartition("/")
        proto = proto.strip().lower()
        if proto in ("tcp", "udp", "sctp"):
            return head, proto
    return value, "tcp"


def _expand_range(spec: str) -> list[int] | None:
    """Expand ``8000-8002`` into [8000, 8001, 8002]; a plain number into [n]."""
    spec = spec.strip()
    if not spec:
        return None
    if "-" in spec:
        low, _, high = spec.partition("-")
        try:
            start, end = int(low), int(high)
        except ValueError:
            return None
        if start > end or end - start > 10_000:
            return None
        return list(range(start, end + 1))
    try:
        return [int(spec)]
    except ValueError:
        # Unresolved ${VAR} or other non-numeric text — out of scope, skip it.
        return None


def _parse_short_port(spec: str) -> list[PortBinding]:
    """Parse the short syntax: ``80``, ``8080:80``, ``127.0.0.1:8080:80``, ranges, ``/udp``."""
    body, protocol = _split_proto(spec.strip())
    if not body:
        return []

    host_ip = "0.0.0.0"
    # IPv6 literals are bracketed: [::1]:8080:80
    if body.startswith("["):
        close = body.find("]")
        if close == -1:
            return []
        host_ip = body[1:close]
        body = body[close + 1 :].lstrip(":")
        parts = body.split(":")
    else:
        parts = body.split(":")
        if len(parts) == 3:
            host_ip = parts[0]
            parts = parts[1:]
        elif len(parts) > 3:
            # An unbracketed IPv6 address; too ambiguous to interpret safely.
            return []

    if len(parts) == 1:
        # "80" — a container port only. Docker picks a random host port, so
        # there is no host-port claim to collide on.
        return []

    host_spec, container_spec = parts[0], parts[1]
    host_ports = _expand_range(host_spec)
    if host_ports is None:
        return []
    container_ports = _expand_range(container_spec) or []

    bindings = []
    for index, host_port in enumerate(host_ports):
        container_port = (
            container_ports[index] if index < len(container_ports) else None
        )
        bindings.append(
            PortBinding(
                host_port=host_port,
                protocol=protocol,
                host_ip=host_ip or "0.0.0.0",
                container_port=container_port,
            )
        )
    return bindings


def _parse_long_port(entry: dict) -> list[PortBinding]:
    """Parse the long syntax: ``{published: 8080, target: 80, protocol: tcp}``."""
    published = entry.get("published")
    if published is None:
        # ``target`` without ``published`` publishes on an ephemeral host port.
        return []
    if entry.get("mode") == "host" and published is None:
        return []

    protocol = str(entry.get("protocol") or "tcp").strip().lower()
    host_ip = str(entry.get("host_ip") or "0.0.0.0")
    # ``published`` may be an int, a "8080" string, or a "8000-8002" range string.
    host_ports = _expand_range(str(published))
    if host_ports is None:
        return []

    target = entry.get("target")
    container_port = None
    if target is not None:
        targets = _expand_range(str(target))
        container_port = targets[0] if targets else None

    return [
        PortBinding(
            host_port=host_port,
            protocol=protocol,
            host_ip=host_ip,
            container_port=container_port,
        )
    for host_port in host_ports
    ]


def parse_ports(raw) -> list[PortBinding]:
    """Parse a service's ``ports:`` list in either syntax."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]

    bindings: list[PortBinding] = []
    for entry in raw:
        if isinstance(entry, dict):
            bindings.extend(_parse_long_port(entry))
        elif isinstance(entry, bool):
            continue
        elif isinstance(entry, int):
            # YAML turns an unquoted 8080:80 into a sexagesimal int; a bare
            # `- 8080` is a container port only, so neither claims a host port.
            continue
        elif isinstance(entry, str):
            bindings.extend(_parse_short_port(entry))
    return bindings


def parse_expose(raw) -> list[int]:
    """Parse ``expose:`` — container ports, needed for the network_mode: host case."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    ports: list[int] = []
    for entry in raw:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            ports.append(entry)
        elif isinstance(entry, str):
            body, _ = _split_proto(entry)
            expanded = _expand_range(body)
            if expanded:
                ports.extend(expanded)
    return ports


def _is_bind_source(source: str) -> bool:
    """True when a volume source is a host path rather than a named volume.

    Compose treats a source containing a path separator or starting with ``.``
    or ``~`` as a bind mount; anything else is a named volume.
    """
    return source.startswith(("/", "./", "../", "~", ".\\")) or "/" in source


def parse_volumes(raw) -> list[BindMount]:
    """Parse a service's ``volumes:`` list, keeping only host bind mounts."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]

    binds: list[BindMount] = []
    for entry in raw:
        if isinstance(entry, dict):
            if entry.get("type") not in (None, "bind"):
                continue
            source = entry.get("source")
            if not isinstance(source, str) or not source:
                continue
            if entry.get("type") is None and not _is_bind_source(source):
                continue
            binds.append(
                BindMount(
                    host_path=normalise_host_path(source),
                    container_path=_as_str(entry.get("target")),
                    read_only=bool(entry.get("read_only")),
                )
            )
        elif isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) < 2:
                # A lone path is an anonymous volume, not a bind mount.
                continue
            source, target = parts[0], parts[1]
            if not _is_bind_source(source):
                continue
            read_only = len(parts) > 2 and "ro" in parts[2].split(",")
            binds.append(
                BindMount(
                    host_path=normalise_host_path(source),
                    container_path=target or None,
                    read_only=read_only,
                )
            )
    return binds


def normalise_host_path(path: str) -> str:
    """Normalise a host path for comparison, without touching the filesystem.

    Relative paths are kept relative (we cannot know the compose file's runtime
    working directory), but ``.``/``..`` segments and duplicate slashes are
    collapsed so ``./data`` and ``data/`` compare equal.
    """
    text = path.strip()
    if not text:
        return text
    expanded = os.path.expanduser(text)
    normalised = os.path.normpath(expanded)
    return normalised


def _as_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


def parse_external_networks(doc: dict) -> list[ExternalNetwork]:
    """Collect networks declared ``external: true``.

    Both the v3.5+ form (``external: true`` + optional ``name:``) and the older
    ``external: {name: ...}`` form are recognised.
    """
    networks = doc.get("networks")
    if not isinstance(networks, dict):
        return []

    result: list[ExternalNetwork] = []
    for key, value in networks.items():
        if not isinstance(key, str):
            continue
        if value is None:
            continue
        if not isinstance(value, dict):
            continue
        external = value.get("external")
        name: str | None = None
        if external is True:
            name = _as_str(value.get("name")) or key
        elif isinstance(external, dict):
            name = _as_str(external.get("name")) or key
        if name:
            result.append(ExternalNetwork(key=key, name=name))
    return result


def parse_service(name: str, raw: dict) -> Service:
    network_mode = raw.get("network_mode")
    service = Service(
        name=name,
        container_name=_as_str(raw.get("container_name")),
        ports=parse_ports(raw.get("ports")),
        binds=parse_volumes(raw.get("volumes")),
        network_mode=_as_str(network_mode),
    )

    if service.network_mode == "host":
        # With host networking there is no port mapping: every container port the
        # service declares is bound directly on the host. ``ports:`` targets are
        # ignored by docker in this mode, so we take the target side plus expose.
        claimed: dict[tuple[int, str], PortBinding] = {}
        for binding in service.ports:
            port = binding.container_port if binding.container_port else binding.host_port
            claimed[(port, binding.protocol)] = PortBinding(
                host_port=port, protocol=binding.protocol, host_network=True
            )
        for port in parse_expose(raw.get("expose")):
            claimed.setdefault(
                (port, "tcp"),
                PortBinding(host_port=port, protocol="tcp", host_network=True),
            )
        service.ports = list(claimed.values())

    return service


def parse_stack(path: Path, name: str | None = None) -> Stack:
    """Parse one compose file into a Stack. Raises ComposeParseError on failure."""
    doc = load_compose(path)
    stack = Stack(
        name=name or path.stem,
        path=str(path),
        version=_as_str(doc.get("version")),
        external_networks=parse_external_networks(doc),
    )
    for service_name, raw in _services_of(doc).items():
        if not isinstance(raw, dict):
            continue
        stack.services.append(parse_service(str(service_name), raw))
    return stack
