"""End-to-end CLI tests: target collection, exit codes, resilience, occupied ports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homelab_compose_audit.cli import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_OK,
    audit,
    collect_targets,
    main,
    parse_occupied_port,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return str(FIXTURES / name)


# --- criterion 1: target collection --------------------------------------


def test_accepts_multiple_paths():
    result, readable = audit([fixture("clean-a.yml"), fixture("clean-b.yml")], [], [], None)
    assert readable is True
    assert {s.name for s in result.stacks} == {"clean-a", "clean-b"}


def test_glob_collects_files():
    targets, warnings = collect_targets([], [str(FIXTURES / "clean-*.yml")])
    assert len(targets) == 2
    assert warnings == []


def test_glob_matching_nothing_warns():
    targets, warnings = collect_targets([], [str(FIXTURES / "nope-*.yml")])
    assert targets == []
    assert any("matched nothing" in w for w in warnings)


def test_directory_target_finds_compose_file(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx\n    ports:\n      - '80:80'\n"
    )
    targets, warnings = collect_targets([str(tmp_path)], [])
    assert [p.name for p in targets] == ["docker-compose.yml"]
    assert warnings == []


def test_directory_without_compose_file_warns(tmp_path):
    targets, warnings = collect_targets([str(tmp_path)], [])
    assert targets == []
    assert any("no compose file" in w for w in warnings)


def test_duplicate_targets_are_collected_once():
    targets, _ = collect_targets(
        [fixture("clean-a.yml"), fixture("clean-a.yml")], [str(FIXTURES / "clean-a.yml")]
    )
    assert len(targets) == 1


def test_missing_path_warns_but_does_not_raise():
    targets, warnings = collect_targets([fixture("absent.yml")], [])
    assert targets == []
    assert any("no such file" in w for w in warnings)


def test_stacks_in_same_named_files_get_distinct_names(tmp_path):
    """Homelabs keep every stack in <name>/docker-compose.yml."""
    for name in ("media", "web"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "docker-compose.yml").write_text("services:\n  app:\n    image: alpine\n")

    result, _ = audit([], [str(tmp_path / "*" / "docker-compose.yml")], [], None)
    assert {s.name for s in result.stacks} == {"media", "web"}


# --- criterion 7: one bad file must not abort the run --------------------


def test_malformed_file_is_a_warning_and_the_run_continues():
    result, readable = audit(
        [fixture("clean-a.yml"), fixture("malformed.yml"), fixture("clean-b.yml")],
        [],
        [],
        None,
    )
    assert readable is True
    assert {s.name for s in result.stacks} == {"clean-a", "clean-b"}
    assert any("malformed.yml" in w and "invalid YAML" in w for w in result.warnings)


def test_non_utf8_file_is_a_warning_and_the_run_continues():
    result, readable = audit([fixture("clean-a.yml"), fixture("latin1.yml")], [], [], None)
    assert readable is True
    assert [s.name for s in result.stacks] == ["clean-a"]
    assert any("latin1.yml" in w and "not valid UTF-8" in w for w in result.warnings)


def test_conflicts_are_still_found_alongside_an_unreadable_file():
    """A broken file must not mask conflicts in the readable ones."""
    result, readable = audit(
        [fixture("media.yml"), fixture("malformed.yml"), fixture("web.yml")], [], [], None
    )
    assert readable is True
    assert result.conflicts
    assert result.warnings


def test_no_readable_target_reports_unreadable():
    result, readable = audit([fixture("latin1.yml"), fixture("malformed.yml")], [], [], None)
    assert readable is False
    assert len(result.warnings) == 2


def test_bom_file_is_audited_not_skipped():
    result, readable = audit([fixture("bom.yml")], [], [], None)
    assert readable is True
    assert [s.name for s in result.stacks] == ["bom"]


# --- criterion 4: occupied ports -----------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("80", (80, "tcp", "0.0.0.0")),
        ("53/udp", (53, "udp", "0.0.0.0")),
        ("127.0.0.1:8080", (8080, "tcp", "127.0.0.1")),
        ("127.0.0.1:53/udp", (53, "udp", "127.0.0.1")),
        ("[::1]:8080", (8080, "tcp", "::1")),
    ],
)
def test_parse_occupied_port(spec, expected):
    binding = parse_occupied_port(spec)
    assert (binding.host_port, binding.protocol, binding.host_ip) == expected


@pytest.mark.parametrize("spec", ["", "notaport", "70000", "0", "80/sneaky"])
def test_invalid_occupied_port_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        parse_occupied_port(spec)


def test_occupied_port_flag_creates_a_conflict():
    result, _ = audit([fixture("clean-a.yml")], [], ["19001"], None)
    assert len(result.conflicts) == 1
    assert "already occupied" in result.conflicts[0].detail


def test_occupied_ports_file_is_read(tmp_path):
    ports_file = tmp_path / "ports.txt"
    ports_file.write_text("# host services\n19001\n\n  53/udp  \n")
    result, _ = audit([fixture("clean-a.yml")], [], [], str(ports_file))
    assert len(result.conflicts) == 1


def test_invalid_entry_in_ports_file_warns_without_aborting(tmp_path):
    ports_file = tmp_path / "ports.txt"
    ports_file.write_text("19001\nbogus\n")
    result, _ = audit([fixture("clean-a.yml")], [], [], str(ports_file))
    assert len(result.conflicts) == 1
    assert any("bogus" in w for w in result.warnings)


def test_missing_ports_file_warns_without_aborting(tmp_path):
    result, readable = audit([fixture("clean-a.yml")], [], [], str(tmp_path / "gone.txt"))
    assert readable is True
    assert any("gone.txt" in w for w in result.warnings)


# --- criterion 5: exit codes and formats ---------------------------------


def test_exit_zero_when_clean(capsys):
    code = main([fixture("clean-a.yml"), fixture("clean-b.yml")])
    assert code == EXIT_OK
    assert "No conflicts found" in capsys.readouterr().out


def test_exit_one_on_conflict(capsys):
    code = main([fixture("media.yml"), fixture("web.yml")])
    assert code == EXIT_CONFLICT
    assert "host port" in capsys.readouterr().out


def test_exit_two_when_nothing_is_readable(capsys):
    code = main([fixture("latin1.yml")])
    assert code == EXIT_ERROR
    assert "no compose file could be read" in capsys.readouterr().err


def test_exit_two_without_any_target(capsys):
    assert main([]) == EXIT_ERROR
    assert "at least one PATH" in capsys.readouterr().err


def test_warning_alone_does_not_fail_the_run(capsys):
    code = main([fixture("clean-a.yml"), fixture("malformed.yml")])
    assert code == EXIT_OK


def test_fail_on_warning_turns_a_warning_into_failure():
    code = main([fixture("clean-a.yml"), fixture("malformed.yml"), "--fail-on-warning"])
    assert code == EXIT_CONFLICT


def test_json_format_is_valid_and_structured(capsys):
    code = main([fixture("media.yml"), fixture("web.yml"), "--format", "json"])
    assert code == EXIT_CONFLICT
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["summary"]["conflicts"] == len(payload["conflicts"])
    assert {c["kind"] for c in payload["conflicts"]} >= {"host_port", "container_name"}
    for conflict in payload["conflicts"]:
        assert conflict["where"]


def test_json_format_reports_warnings(capsys):
    main([fixture("clean-a.yml"), fixture("latin1.yml"), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["warnings"] == 1
    assert "not valid UTF-8" in payload["warnings"][0]


def test_markdown_format_has_headings(capsys):
    code = main([fixture("media.yml"), fixture("web.yml"), "--format", "markdown"])
    assert code == EXIT_CONFLICT
    out = capsys.readouterr().out
    assert out.startswith("# docker-compose audit")
    assert "## Host port collisions" in out


def test_allow_shared_network_flag(capsys):
    main([fixture("media.yml"), fixture("web.yml"), "--allow-shared-network", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert "external_network" not in {c["kind"] for c in payload["conflicts"]}


def test_glob_flag_end_to_end(capsys):
    code = main(["--glob", str(FIXTURES / "clean-*.yml")])
    assert code == EXIT_OK


def test_the_host_is_never_inspected(monkeypatch):
    """The audit must not open sockets — occupied ports come from the user."""
    import socket

    def explode(*args, **kwargs):
        raise AssertionError("the audit must not touch the network")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    result, _ = audit([fixture("media.yml"), fixture("web.yml")], [], ["8096"], None)
    assert result.conflicts
