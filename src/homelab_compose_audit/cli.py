"""Command line entry point.

Exit codes:
  0  no conflicts (warnings may still have been printed)
  1  at least one conflict
  2  usage error, or no target could be read at all
"""

from __future__ import annotations

import argparse
import glob as globlib
import sys
from pathlib import Path

from . import __version__
from .conflicts import find_conflicts
from .model import AuditResult, PortBinding
from .parse import ComposeParseError, parse_stack, read_text
from .report import render

EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_ERROR = 2

#: Filenames picked up when a directory is given as a target.
COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="homelab-compose-audit",
        description=(
            "Cross-check independent docker-compose files for host port, container "
            "name, external network and bind-mount collisions. Offline: no docker "
            "daemon is contacted and the host's real port usage is never read."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="compose files, or directories containing one",
    )
    parser.add_argument(
        "--glob",
        action="append",
        default=[],
        metavar="PATTERN",
        help="glob for compose files, e.g. --glob 'stacks/*/docker-compose.yml' (repeatable)",
    )
    parser.add_argument(
        "--occupied-port",
        action="append",
        default=[],
        metavar="PORT",
        help=(
            "a host port already in use, e.g. 80, 53/udp or 127.0.0.1:8080 "
            "(repeatable). Supplied by you — the host is never inspected."
        ),
    )
    parser.add_argument(
        "--occupied-ports-file",
        metavar="FILE",
        help="file with one occupied port per line; '#' comments and blanks ignored",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--allow-shared-network",
        action="store_true",
        help="do not report two stacks sharing one external network",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="also exit nonzero when a file could not be read or parsed",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def collect_targets(paths: list[str], globs: list[str]) -> tuple[list[Path], list[str]]:
    """Resolve CLI arguments into a de-duplicated, ordered list of files."""
    targets: list[Path] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key not in seen:
            seen.add(key)
            targets.append(path)

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found = [path / name for name in COMPOSE_NAMES if (path / name).is_file()]
            if not found:
                warnings.append(f"{path}: directory contains no compose file — skipped")
            for item in found:
                add(item)
        elif path.exists():
            add(path)
        else:
            warnings.append(f"{path}: no such file or directory — skipped")

    for pattern in globs:
        matches = sorted(globlib.glob(pattern, recursive=True))
        if not matches:
            warnings.append(f"{pattern}: glob matched nothing")
        for match in matches:
            path = Path(match)
            if path.is_file():
                add(path)

    return targets, warnings


def _unique_stack_name(path: Path, used: set[str]) -> str:
    """Name a stack after its file, disambiguating with the parent directory.

    Many homelabs keep every stack in ``<service>/docker-compose.yml``, where the
    stem is identical for all of them and the directory carries the meaning.
    """
    if path.stem in ("docker-compose", "compose") and path.parent.name:
        base = path.parent.name
    else:
        base = path.stem

    name = base
    counter = 2
    while name in used:
        name = f"{base}#{counter}"
        counter += 1
    used.add(name)
    return name


def parse_occupied_port(spec: str) -> PortBinding:
    """Parse an occupied-port spec: ``8080``, ``53/udp``, ``127.0.0.1:8080``."""
    text = spec.strip()
    if not text:
        raise ValueError("empty port specification")

    protocol = "tcp"
    if "/" in text:
        text, _, proto = text.rpartition("/")
        proto = proto.strip().lower()
        if proto not in ("tcp", "udp", "sctp"):
            raise ValueError(f"unknown protocol '{proto}'")
        protocol = proto

    host_ip = "0.0.0.0"
    if text.startswith("["):
        close = text.find("]")
        if close == -1:
            raise ValueError(f"unbalanced '[' in '{spec}'")
        host_ip = text[1:close]
        text = text[close + 1 :].lstrip(":")
    elif text.count(":") == 1:
        host_ip, _, text = text.partition(":")

    try:
        port = int(text)
    except ValueError:
        raise ValueError(f"'{spec}' is not a port number") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} out of range 1-65535")

    return PortBinding(host_port=port, protocol=protocol, host_ip=host_ip or "0.0.0.0")


def load_occupied(
    specs: list[str], ports_file: str | None
) -> tuple[list[PortBinding], list[str]]:
    """Build the occupied-port list from the flag and/or the file."""
    warnings: list[str] = []
    bindings: list[PortBinding] = []

    entries = list(specs)
    if ports_file:
        path = Path(ports_file)
        try:
            text = read_text(path)
        except ComposeParseError as exc:
            warnings.append(str(exc))
            text = None
        if text is not None:
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    entries.append(line)

    for entry in entries:
        try:
            bindings.append(parse_occupied_port(entry))
        except ValueError as exc:
            warnings.append(f"occupied port: {exc} — ignored")
    return bindings, warnings


def audit(
    paths: list[str],
    globs: list[str],
    occupied_specs: list[str],
    occupied_file: str | None,
    allow_shared_network: bool = False,
) -> tuple[AuditResult, bool]:
    """Run the audit. Returns the result and whether any target was readable."""
    targets, warnings = collect_targets(paths, globs)
    result = AuditResult(warnings=list(warnings))

    occupied, occupied_warnings = load_occupied(occupied_specs, occupied_file)
    result.warnings.extend(occupied_warnings)

    any_readable = False
    used_names: set[str] = set()
    for path in targets:
        try:
            stack = parse_stack(path, name=_unique_stack_name(path, used_names))
        except ComposeParseError as exc:
            # One bad file must never abort the walk.
            result.warnings.append(str(exc))
            continue
        any_readable = True
        result.stacks.append(stack)

    result.conflicts = find_conflicts(
        result.stacks, occupied=occupied, allow_shared_network=allow_shared_network
    )
    return result, any_readable


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.paths and not args.glob:
        parser.print_usage(sys.stderr)
        print(
            "homelab-compose-audit: give at least one PATH or --glob PATTERN",
            file=sys.stderr,
        )
        return EXIT_ERROR

    result, any_readable = audit(
        paths=args.paths,
        globs=args.glob,
        occupied_specs=args.occupied_port,
        occupied_file=args.occupied_ports_file,
        allow_shared_network=args.allow_shared_network,
    )

    print(render(result, args.format), end="")

    if not any_readable:
        print(
            "homelab-compose-audit: no compose file could be read", file=sys.stderr
        )
        return EXIT_ERROR
    if result.conflicts:
        return EXIT_CONFLICT
    if args.fail_on_warning and result.warnings:
        return EXIT_CONFLICT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
