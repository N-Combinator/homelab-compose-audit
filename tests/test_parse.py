"""Parser tests: compose versions, port syntaxes, volumes, networks, bad input."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_compose_audit.parse import (
    ComposeParseError,
    normalise_host_path,
    parse_expose,
    parse_external_networks,
    parse_ports,
    parse_stack,
    parse_volumes,
    read_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def ports_of(stack, service_name):
    for service in stack.services:
        if service.name == service_name:
            return service.ports
    raise AssertionError(f"no service {service_name!r} in {stack.name}")


# --- criterion 1: compose v1 / v2 / v3 ------------------------------------


def test_parses_v3_services():
    stack = parse_stack(FIXTURES / "media.yml")
    assert stack.version == "3.8"
    assert {s.name for s in stack.services} == {"jellyfin", "transmission"}


def test_parses_v2_services():
    stack = parse_stack(FIXTURES / "monitoring.yml")
    assert stack.version == "2.4"
    assert {s.name for s in stack.services} == {"node-exporter", "grafana"}


def test_parses_v1_top_level_services():
    """v1 has no `services:` key — the stack names are the top-level keys."""
    stack = parse_stack(FIXTURES / "v1-legacy.yml")
    assert stack.version is None
    assert {s.name for s in stack.services} == {"web", "redis"}
    assert ports_of(stack, "web")[0].host_port == 8081


def test_v3_file_without_services_yields_no_services():
    """A v2+ document declaring only networks must not treat them as services."""
    stack = parse_stack(FIXTURES / "networks-only.yml")
    assert stack.services == []
    assert [n.name for n in stack.external_networks] == ["edge-proxy"]


def test_empty_file_is_an_empty_stack():
    stack = parse_stack(FIXTURES / "empty.yml")
    assert stack.services == []


# --- criterion 1: encodings ----------------------------------------------


def test_bom_file_is_parsed_not_dropped():
    """utf-8-sig strips the BOM; plain utf-8 would corrupt the first key."""
    stack = parse_stack(FIXTURES / "bom.yml")
    assert [s.name for s in stack.services] == ["bom-service"]
    assert ports_of(stack, "bom-service")[0].host_port == 8600


def test_non_utf8_file_raises_reportable_error_not_traceback():
    with pytest.raises(ComposeParseError) as excinfo:
        parse_stack(FIXTURES / "latin1.yml")
    assert "not valid UTF-8" in str(excinfo.value)
    assert "latin1.yml" in str(excinfo.value)


def test_missing_file_raises_compose_parse_error():
    with pytest.raises(ComposeParseError):
        read_text(FIXTURES / "does-not-exist.yml")


def test_malformed_yaml_raises_compose_parse_error():
    with pytest.raises(ComposeParseError) as excinfo:
        parse_stack(FIXTURES / "malformed.yml")
    assert "invalid YAML" in str(excinfo.value)


def test_non_mapping_top_level_raises_compose_parse_error():
    with pytest.raises(ComposeParseError) as excinfo:
        parse_stack(FIXTURES / "not-a-mapping.yml")
    assert "expected a mapping" in str(excinfo.value)


# --- criterion 2: port syntaxes ------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("8080:80", [(8080, "tcp", "0.0.0.0")]),
        ("127.0.0.1:8080:80", [(8080, "tcp", "127.0.0.1")]),
        ("53:53/udp", [(53, "udp", "0.0.0.0")]),
        ("127.0.0.1:53:53/udp", [(53, "udp", "127.0.0.1")]),
        ("[::1]:8080:80", [(8080, "tcp", "::1")]),
        (
            "8000-8002:80-82",
            [(8000, "tcp", "0.0.0.0"), (8001, "tcp", "0.0.0.0"), (8002, "tcp", "0.0.0.0")],
        ),
    ],
)
def test_short_port_syntax(spec, expected):
    bindings = parse_ports([spec])
    assert [(b.host_port, b.protocol, b.host_ip) for b in bindings] == expected


@pytest.mark.parametrize("spec", ["80", "8080", "${HOST_PORT}:80", ""])
def test_specs_that_claim_no_host_port(spec):
    """A container-only port or an uninterpolated variable claims no host port."""
    assert parse_ports([spec]) == []


def test_bare_int_port_claims_no_host_port():
    assert parse_ports([8080]) == []


def test_long_port_syntax():
    bindings = parse_ports([{"target": 80, "published": 8080, "protocol": "tcp"}])
    assert len(bindings) == 1
    assert bindings[0].host_port == 8080
    assert bindings[0].container_port == 80


def test_long_port_syntax_with_string_published():
    bindings = parse_ports([{"target": 80, "published": "8080"}])
    assert [b.host_port for b in bindings] == [8080]


def test_long_port_syntax_with_host_ip_and_udp():
    bindings = parse_ports(
        [{"target": 53, "published": 53, "protocol": "udp", "host_ip": "127.0.0.1"}]
    )
    assert (bindings[0].host_port, bindings[0].protocol, bindings[0].host_ip) == (
        53,
        "udp",
        "127.0.0.1",
    )


def test_long_port_without_published_claims_nothing():
    """`target` alone publishes on an ephemeral port, so nothing is claimed."""
    assert parse_ports([{"target": 80}]) == []


def test_mixed_short_and_long_syntax_in_one_list():
    bindings = parse_ports(["9000:90", {"target": 80, "published": 8080}])
    assert sorted(b.host_port for b in bindings) == [8080, 9000]


# --- criterion 2: network_mode host --------------------------------------


def test_network_mode_host_claims_exposed_ports():
    stack = parse_stack(FIXTURES / "monitoring.yml")
    bindings = ports_of(stack, "node-exporter")
    assert [(b.host_port, b.host_network) for b in bindings] == [(9100, True)]


def test_network_mode_host_claims_container_side_of_ports():
    """With host networking the mapping is ignored; the target port is bound."""
    stack = parse_stack(FIXTURES / "hostnet.yml")
    bindings = ports_of(stack, "pihole-host")
    assert {(b.host_port, b.protocol) for b in bindings} == {(53, "udp"), (8053, "tcp")}
    assert all(b.host_network for b in bindings)


def test_parse_expose_handles_ints_strings_and_ranges():
    assert parse_expose([9100, "9200", "9300-9301", "9400/tcp"]) == [
        9100,
        9200,
        9300,
        9301,
        9400,
    ]


# --- criterion 3: volumes and networks -----------------------------------


def test_parses_bind_mounts_and_skips_named_volumes():
    stack = parse_stack(FIXTURES / "monitoring.yml")
    grafana = [s for s in stack.services if s.name == "grafana"][0]
    node = [s for s in stack.services if s.name == "node-exporter"][0]
    # grafana-data is a named volume, not a host path.
    assert grafana.binds == []
    assert [b.host_path for b in node.binds] == ["/proc"]
    assert node.binds[0].read_only is True


def test_parses_long_volume_syntax():
    binds = parse_volumes(
        [{"type": "bind", "source": "/srv/data", "target": "/data", "read_only": True}]
    )
    assert len(binds) == 1
    assert binds[0].host_path == "/srv/data"
    assert binds[0].read_only is True


def test_long_volume_syntax_skips_named_volume_type():
    assert parse_volumes([{"type": "volume", "source": "appdata", "target": "/data"}]) == []


def test_anonymous_volume_is_not_a_bind_mount():
    assert parse_volumes(["/var/lib/data"]) == []


def test_named_volume_short_syntax_is_not_a_bind_mount():
    assert parse_volumes(["appdata:/var/lib/data"]) == []


def test_relative_bind_mount_is_kept_relative():
    binds = parse_volumes(["./config:/config"])
    assert binds[0].host_path == "config"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("./data", "data"), ("/srv//media/", "/srv/media"), ("/srv/a/../b", "/srv/b")],
)
def test_normalise_host_path(raw, expected):
    assert normalise_host_path(raw) == expected


def test_parses_external_networks_both_syntaxes():
    modern = parse_external_networks({"networks": {"proxy": {"external": True, "name": "edge"}}})
    assert [(n.key, n.name) for n in modern] == [("proxy", "edge")]

    legacy = parse_external_networks({"networks": {"proxy": {"external": {"name": "edge"}}}})
    assert [(n.key, n.name) for n in legacy] == [("proxy", "edge")]


def test_external_network_without_name_uses_its_key():
    networks = parse_external_networks({"networks": {"edge-proxy": {"external": True}}})
    assert [n.name for n in networks] == ["edge-proxy"]


def test_non_external_network_is_ignored():
    assert parse_external_networks({"networks": {"internal": {"driver": "bridge"}}}) == []


# --- malformed and out-of-range input must not produce phantom claims ------


@pytest.mark.parametrize(
    "spec", ["0:80", "70000:80", "-1:80", "1-70000:80", "8080-8000:80"]
)
def test_out_of_range_or_reversed_ports_claim_nothing(spec):
    assert parse_ports([spec]) == []


def test_published_zero_claims_no_host_port():
    """`published: 0` asks docker to pick a port, so nothing fixed is claimed."""
    assert parse_ports([{"target": 80, "published": 0}]) == []


@pytest.mark.parametrize(
    "raw",
    [
        [None],
        [["8080:80"]],
        [{}],
        [{"target": 80, "published": None}],
    ],
)
def test_unusable_port_entries_are_skipped_not_fatal(raw):
    assert parse_ports(raw) == []


@pytest.mark.parametrize("raw", [[None], [""], [":"]])
def test_unusable_volume_entries_are_skipped_not_fatal(raw):
    assert parse_volumes(raw) == []


def test_networks_section_that_is_not_a_mapping_is_ignored():
    assert parse_external_networks({"networks": ["proxy"]}) == []


def test_network_entry_that_is_not_a_mapping_is_ignored():
    assert parse_external_networks({"networks": {"proxy": ["x"]}}) == []
