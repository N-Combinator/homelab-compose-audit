"""Conflict detection tests: ports, container names, external networks, binds."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_compose_audit.conflicts import (
    KIND_BIND_MOUNT,
    KIND_CONTAINER_NAME,
    KIND_EXTERNAL_NETWORK,
    KIND_PORT,
    check_bind_mounts,
    check_container_names,
    check_external_networks,
    check_host_ports,
    find_conflicts,
)
from homelab_compose_audit.model import BindMount, PortBinding, Service, Stack
from homelab_compose_audit.parse import parse_stack

FIXTURES = Path(__file__).parent / "fixtures"


def stack(name, services=None, external=None):
    return Stack(
        name=name,
        path=f"/tmp/{name}.yml",
        services=services or [],
        external_networks=external or [],
    )


def service(name, ports=(), binds=(), container_name=None):
    return Service(
        name=name,
        container_name=container_name,
        ports=list(ports),
        binds=list(binds),
    )


def port(host_port, protocol="tcp", host_ip="0.0.0.0", host_network=False):
    return PortBinding(
        host_port=host_port, protocol=protocol, host_ip=host_ip, host_network=host_network
    )


# --- criterion 2: host port collisions -----------------------------------


def test_same_host_port_in_two_stacks_collides():
    stacks = [
        stack("a", [service("web", ports=[port(8080)])]),
        stack("b", [service("api", ports=[port(8080)])]),
    ]
    conflicts = check_host_ports(stacks)
    assert len(conflicts) == 1
    assert conflicts[0].kind == KIND_PORT
    assert "8080/tcp" in conflicts[0].detail


def test_same_port_within_one_stack_is_not_reported():
    """Docker itself rejects this, and it is not a cross-stack concern."""
    stacks = [
        stack("a", [service("web", ports=[port(8080)]), service("api", ports=[port(8080)])])
    ]
    assert check_host_ports(stacks) == []


def test_different_protocols_on_same_port_do_not_collide():
    stacks = [
        stack("a", [service("dns", ports=[port(53, protocol="udp")])]),
        stack("b", [service("dot", ports=[port(53, protocol="tcp")])]),
    ]
    assert check_host_ports(stacks) == []


def test_distinct_host_ips_on_same_port_do_not_collide():
    stacks = [
        stack("a", [service("web", ports=[port(8080, host_ip="127.0.0.1")])]),
        stack("b", [service("api", ports=[port(8080, host_ip="192.168.1.10")])]),
    ]
    assert check_host_ports(stacks) == []


def test_wildcard_bind_collides_with_specific_ip():
    """0.0.0.0:8080 occupies 8080 on every interface, including 127.0.0.1."""
    stacks = [
        stack("a", [service("web", ports=[port(8080, host_ip="0.0.0.0")])]),
        stack("b", [service("api", ports=[port(8080, host_ip="127.0.0.1")])]),
    ]
    assert len(check_host_ports(stacks)) == 1


def test_network_mode_host_port_collides_with_published_port():
    stacks = [
        stack("mon", [service("exporter", ports=[port(9100, host_network=True)])]),
        stack("app", [service("metrics", ports=[port(9100)])]),
    ]
    conflicts = check_host_ports(stacks)
    assert len(conflicts) == 1
    assert "network_mode: host" in conflicts[0].detail


def test_three_stacks_on_one_port_report_each_pair():
    stacks = [
        stack("a", [service("s", ports=[port(80)])]),
        stack("b", [service("s", ports=[port(80)])]),
        stack("c", [service("s", ports=[port(80)])]),
    ]
    assert len(check_host_ports(stacks)) == 3


# --- criterion 4: already-occupied host ports ----------------------------


def test_occupied_port_collides_with_a_stack():
    stacks = [stack("a", [service("web", ports=[port(80)])])]
    conflicts = check_host_ports(stacks, occupied=[port(80)])
    assert len(conflicts) == 1
    assert "(already occupied)" in conflicts[0].detail


def test_occupied_port_that_nothing_uses_is_silent():
    stacks = [stack("a", [service("web", ports=[port(8080)])])]
    assert check_host_ports(stacks, occupied=[port(80)]) == []


def test_occupied_udp_port_does_not_collide_with_tcp():
    stacks = [stack("a", [service("web", ports=[port(53, protocol="tcp")])])]
    assert check_host_ports(stacks, occupied=[port(53, protocol="udp")]) == []


# --- criterion 3: container names ----------------------------------------


def test_duplicate_container_name_across_stacks():
    stacks = [
        stack("a", [service("web", container_name="shared")]),
        stack("b", [service("api", container_name="shared")]),
    ]
    conflicts = check_container_names(stacks)
    assert len(conflicts) == 1
    assert conflicts[0].kind == KIND_CONTAINER_NAME
    assert "shared" in conflicts[0].detail


def test_duplicate_container_name_within_one_stack_is_not_cross_stack():
    stacks = [
        stack("a", [service("web", container_name="x"), service("api", container_name="x")])
    ]
    assert check_container_names(stacks) == []


def test_services_without_container_name_are_ignored():
    stacks = [stack("a", [service("web")]), stack("b", [service("api")])]
    assert check_container_names(stacks) == []


# --- criterion 3: external networks --------------------------------------


def test_shared_external_network_across_stacks():
    from homelab_compose_audit.model import ExternalNetwork

    stacks = [
        stack("a", external=[ExternalNetwork("proxy", "edge")]),
        stack("b", external=[ExternalNetwork("net", "edge")]),
    ]
    conflicts = check_external_networks(stacks)
    assert len(conflicts) == 1
    assert conflicts[0].kind == KIND_EXTERNAL_NETWORK
    assert "edge" in conflicts[0].detail


def test_distinct_external_networks_do_not_collide():
    from homelab_compose_audit.model import ExternalNetwork

    stacks = [
        stack("a", external=[ExternalNetwork("proxy", "edge-a")]),
        stack("b", external=[ExternalNetwork("proxy", "edge-b")]),
    ]
    assert check_external_networks(stacks) == []


# --- criterion 3: bind mounts --------------------------------------------


def test_identical_bind_mount_paths_collide():
    stacks = [
        stack("a", [service("web", binds=[BindMount("/srv/data", "/data")])]),
        stack("b", [service("api", binds=[BindMount("/srv/data", "/data")])]),
    ]
    conflicts = check_bind_mounts(stacks)
    assert len(conflicts) == 1
    assert conflicts[0].kind == KIND_BIND_MOUNT


def test_nested_bind_mount_paths_overlap():
    stacks = [
        stack("a", [service("web", binds=[BindMount("/srv/media", "/media")])]),
        stack("b", [service("api", binds=[BindMount("/srv/media/movies", "/movies")])]),
    ]
    conflicts = check_bind_mounts(stacks)
    assert len(conflicts) == 1
    assert "overlap" in conflicts[0].detail


def test_sibling_paths_with_a_shared_prefix_do_not_overlap():
    """/srv/media-backup is not inside /srv/media despite the string prefix."""
    stacks = [
        stack("a", [service("web", binds=[BindMount("/srv/media", "/media")])]),
        stack("b", [service("api", binds=[BindMount("/srv/media-backup", "/backup")])]),
    ]
    assert check_bind_mounts(stacks) == []


def test_two_read_only_mounts_of_one_path_are_allowed():
    stacks = [
        stack("a", [service("web", binds=[BindMount("/srv/media", "/media", read_only=True)])]),
        stack("b", [service("api", binds=[BindMount("/srv/media", "/media", read_only=True)])]),
    ]
    assert check_bind_mounts(stacks) == []


def test_one_writable_mount_against_a_read_only_one_is_reported():
    stacks = [
        stack("a", [service("web", binds=[BindMount("/srv/media", "/media", read_only=True)])]),
        stack("b", [service("api", binds=[BindMount("/srv/media", "/media")])]),
    ]
    assert len(check_bind_mounts(stacks)) == 1


def test_relative_and_absolute_paths_are_not_compared():
    """Without a working directory, 'data' and '/data' cannot be shown to overlap."""
    stacks = [
        stack("a", [service("web", binds=[BindMount("data", "/data")])]),
        stack("b", [service("api", binds=[BindMount("/data", "/data")])]),
    ]
    assert check_bind_mounts(stacks) == []


# --- integration over the fixtures ---------------------------------------


def test_find_conflicts_over_real_fixtures():
    stacks = [
        parse_stack(FIXTURES / "media.yml", name="media"),
        parse_stack(FIXTURES / "web.yml", name="web"),
    ]
    conflicts = find_conflicts(stacks)
    kinds = {c.kind for c in conflicts}
    assert kinds == {KIND_PORT, KIND_CONTAINER_NAME, KIND_EXTERNAL_NETWORK, KIND_BIND_MOUNT}


def test_allow_shared_network_suppresses_only_that_kind():
    stacks = [
        parse_stack(FIXTURES / "media.yml", name="media"),
        parse_stack(FIXTURES / "web.yml", name="web"),
    ]
    conflicts = find_conflicts(stacks, allow_shared_network=True)
    assert KIND_EXTERNAL_NETWORK not in {c.kind for c in conflicts}
    assert KIND_PORT in {c.kind for c in conflicts}


def test_clean_stacks_have_no_conflicts():
    stacks = [
        parse_stack(FIXTURES / "clean-a.yml", name="clean-a"),
        parse_stack(FIXTURES / "clean-b.yml", name="clean-b"),
    ]
    assert find_conflicts(stacks) == []


@pytest.mark.parametrize("fixture", ["media.yml", "web.yml", "monitoring.yml"])
def test_a_single_stack_alone_never_conflicts_with_itself(fixture):
    stacks = [parse_stack(FIXTURES / fixture)]
    assert find_conflicts(stacks) == []
