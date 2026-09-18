"""Data types shared by the parser, the conflict checks and the reporters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortBinding:
    """A single published host port.

    ``host_ip`` is the interface the port is published on; ``0.0.0.0`` means "all
    interfaces" and therefore collides with every other IP on the same port.
    """

    host_port: int
    protocol: str = "tcp"
    host_ip: str = "0.0.0.0"
    container_port: int | None = None
    #: Set for ports claimed via ``network_mode: host`` rather than a publish rule.
    host_network: bool = False

    def collides_with(self, other: PortBinding) -> bool:
        if self.host_port != other.host_port or self.protocol != other.protocol:
            return False
        # A wildcard bind conflicts with any specific interface, and vice versa.
        if self.host_ip in _WILDCARD_IPS or other.host_ip in _WILDCARD_IPS:
            return True
        return self.host_ip == other.host_ip

    def describe(self) -> str:
        where = "" if self.host_ip in _WILDCARD_IPS else f"{self.host_ip}:"
        text = f"{where}{self.host_port}/{self.protocol}"
        if self.host_network:
            text += " (network_mode: host)"
        return text


_WILDCARD_IPS = {"0.0.0.0", "::", "*", ""}


@dataclass(frozen=True)
class BindMount:
    """A host path bind-mounted into a container."""

    host_path: str
    container_path: str | None = None
    read_only: bool = False


@dataclass
class Service:
    name: str
    container_name: str | None = None
    ports: list[PortBinding] = field(default_factory=list)
    binds: list[BindMount] = field(default_factory=list)
    network_mode: str | None = None


@dataclass
class ExternalNetwork:
    """A network declared ``external: true`` — a name shared with the docker engine."""

    key: str
    name: str


@dataclass
class Stack:
    """One parsed compose file."""

    name: str
    path: str
    services: list[Service] = field(default_factory=list)
    external_networks: list[ExternalNetwork] = field(default_factory=list)
    version: str | None = None


@dataclass
class Conflict:
    kind: str
    detail: str
    #: ``[{"stack": ..., "path": ..., "service": ...}, ...]`` for every side involved.
    where: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "detail": self.detail, "where": self.where}


@dataclass
class AuditResult:
    conflicts: list[Conflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stacks: list[Stack] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts
