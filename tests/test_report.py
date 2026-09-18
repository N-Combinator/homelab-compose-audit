"""Reporter tests for the json, markdown and text renderings."""

from __future__ import annotations

import json

from homelab_compose_audit.model import AuditResult, Conflict, Service, Stack
from homelab_compose_audit.report import render, to_json, to_markdown, to_text


def sample_result(with_conflicts=True, with_warnings=False):
    stack = Stack(name="media", path="/srv/media.yml", services=[Service(name="jellyfin")])
    result = AuditResult(stacks=[stack])
    if with_conflicts:
        result.conflicts = [
            Conflict(
                kind="host_port",
                detail="host port 8096/tcp is claimed by media/jellyfin and web/dashboard",
                where=[{"stack": "media", "path": "/srv/media.yml", "service": "jellyfin"}],
            ),
            Conflict(kind="container_name", detail="container_name 'jellyfin' is used twice"),
        ]
    if with_warnings:
        result.warnings = ["/srv/bad.yml: invalid YAML — skipped"]
    return result


def test_json_is_parseable_and_complete():
    payload = json.loads(to_json(sample_result(with_warnings=True)))
    assert payload["ok"] is False
    assert payload["summary"] == {
        "stacks": 1,
        "services": 1,
        "conflicts": 2,
        "warnings": 1,
    }
    assert payload["stacks"][0]["name"] == "media"
    assert payload["conflicts"][0]["kind"] == "host_port"
    assert payload["warnings"][0].endswith("skipped")


def test_json_ok_true_when_clean():
    payload = json.loads(to_json(sample_result(with_conflicts=False)))
    assert payload["ok"] is True
    assert payload["conflicts"] == []


def test_markdown_groups_conflicts_by_kind():
    out = to_markdown(sample_result())
    assert "## Host port collisions" in out
    assert "## Duplicate container names" in out
    assert out.index("## Host port collisions") < out.index("## Duplicate container names")
    assert out.endswith("\n")


def test_markdown_lists_the_stack_table():
    out = to_markdown(sample_result())
    assert "| Stack | File | Services |" in out
    assert "| `media` | `/srv/media.yml` | 1 |" in out


def test_markdown_states_success_when_clean():
    out = to_markdown(sample_result(with_conflicts=False))
    assert "No conflicts found." in out
    assert "## Host port collisions" not in out


def test_markdown_includes_warnings_section():
    out = to_markdown(sample_result(with_warnings=True))
    assert "## Warnings" in out
    assert "invalid YAML" in out


def test_text_output_is_readable():
    out = to_text(sample_result(with_warnings=True))
    assert "1 stack(s)" in out
    assert "2 conflict(s)" in out
    assert "Host port collisions:" in out
    assert "Warnings:" in out


def test_text_states_success_when_clean():
    assert "No conflicts found." in to_text(sample_result(with_conflicts=False))


def test_render_dispatches_on_format():
    result = sample_result()
    assert render(result, "json").startswith("{")
    assert render(result, "markdown").startswith("# docker-compose audit")
    assert render(result, "text").startswith("Checked ")


def test_unknown_conflict_kind_still_renders():
    """A future check must not vanish from the report just because it is new."""
    result = AuditResult(conflicts=[Conflict(kind="future_kind", detail="something new")])
    assert "something new" in to_markdown(result)
    assert "something new" in to_text(result)
