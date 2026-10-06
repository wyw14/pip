"""Validation of dependencies of packages"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Generator, Iterable
from contextlib import suppress
from email.parser import Parser
from functools import reduce
from typing import (
    NamedTuple,
)

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.tags import Tag, parse_tag
from pip._vendor.packaging.utils import NormalizedName, canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.distributions import make_distribution_for_install_requirement
from pip._internal.metadata import get_default_environment
from pip._internal.metadata.base import BaseDistribution
from pip._internal.req.req_install import InstallRequirement

logger = logging.getLogger(__name__)


class PackageDetails(NamedTuple):
    version: Version
    dependencies: list[Requirement]


# Shorthands
PackageSet = dict[NormalizedName, PackageDetails]
Missing = tuple[NormalizedName, Requirement]
Conflicting = tuple[NormalizedName, Version, Requirement]

MissingDict = dict[NormalizedName, list[Missing]]
ConflictingDict = dict[NormalizedName, list[Conflicting]]
CheckResult = tuple[MissingDict, ConflictingDict]
ConflictDetails = tuple[PackageSet, CheckResult]

# Bounds for the bounded dependency path explanation used by
# ``pip check --explain``.
MAX_EXPLAIN_DEPTH = 10
MAX_EXPLAIN_PATHS_PER_REQUIREMENT = 20


class DependencyEdge(NamedTuple):
    """An effective dependency edge: the requirement declared by the source.

    ``str(requirement)`` preserves the version specifier, environment marker
    and the extras requested on the target.
    """

    requirement: Requirement
    target: NormalizedName


class ConflictPath(NamedTuple):
    """A simple path from the requested top-level project to a conflict.

    ``len(nodes) == len(edges) + 1``; edge *i* goes from ``nodes[i]`` to
    ``nodes[i + 1]`` and is labelled by ``edges[i].requirement``.
    """

    nodes: tuple[NormalizedName, ...]
    edges: tuple[DependencyEdge, ...]


class RequirementPaths(NamedTuple):
    """Paths ending with one particular violated requirement constraint."""

    requirement: Requirement
    paths: list[ConflictPath]
    truncated: bool


class ConflictExplanation(NamedTuple):
    """Violated constraints on one installed package, grouped separately."""

    target: NormalizedName
    target_version: Version
    requirement_paths: list[RequirementPaths]


def create_package_set_from_installed() -> tuple[PackageSet, bool]:
    """Converts a list of distributions into a PackageSet."""
    package_set = {}
    problems = False
    env = get_default_environment()
    for dist in env.iter_installed_distributions(local_only=False, skip=()):
        name = dist.canonical_name
        try:
            dependencies = list(dist.iter_dependencies())
            package_set[name] = PackageDetails(dist.version, dependencies)
        except (OSError, ValueError) as e:
            # Don't crash on unreadable or broken metadata.
            logger.warning("Error parsing dependencies of %s: %s", name, e)
            problems = True
    return package_set, problems


def check_package_set(
    package_set: PackageSet, should_ignore: Callable[[str], bool] | None = None
) -> CheckResult:
    """Check if a package set is consistent

    If should_ignore is passed, it should be a callable that takes a
    package name and returns a boolean.
    """

    missing = {}
    conflicting = {}

    for package_name, package_detail in package_set.items():
        # Info about dependencies of package_name
        missing_deps: set[Missing] = set()
        conflicting_deps: set[Conflicting] = set()

        if should_ignore and should_ignore(package_name):
            continue

        for req in package_detail.dependencies:
            name = canonicalize_name(req.name)

            # Check if it's missing
            if name not in package_set:
                missed = True
                if req.marker is not None:
                    missed = req.marker.evaluate({"extra": ""})
                if missed:
                    missing_deps.add((name, req))
                continue

            # Check if there's a conflict
            version = package_set[name].version
            if not req.specifier.contains(version, prereleases=True):
                conflicting_deps.add((name, version, req))

        if missing_deps:
            missing[package_name] = sorted(missing_deps, key=str)
        if conflicting_deps:
            conflicting[package_name] = sorted(conflicting_deps, key=str)

    return missing, conflicting


def check_install_conflicts(to_install: list[InstallRequirement]) -> ConflictDetails:
    """For checking if the dependency graph would be consistent after \
    installing given requirements
    """
    # Start from the current state
    package_set, _ = create_package_set_from_installed()
    # Install packages
    would_be_installed = _simulate_installation_of(to_install, package_set)

    # Only warn about directly-dependent packages; create a whitelist of them
    whitelist = _create_whitelist(would_be_installed, package_set)

    return (
        package_set,
        check_package_set(
            package_set, should_ignore=lambda name: name not in whitelist
        ),
    )


def check_unsupported(
    packages: Iterable[BaseDistribution],
    supported_tags: Iterable[Tag],
) -> Generator[BaseDistribution, None, None]:
    for p in packages:
        with suppress(FileNotFoundError):
            wheel_file = p.read_text("WHEEL")
            wheel_tags: frozenset[Tag] = reduce(
                frozenset.union,
                map(parse_tag, Parser().parsestr(wheel_file).get_all("Tag", [])),
                frozenset(),
            )
            if wheel_tags.isdisjoint(supported_tags):
                yield p


def _simulate_installation_of(
    to_install: list[InstallRequirement], package_set: PackageSet
) -> set[NormalizedName]:
    """Computes the version of packages after installing to_install."""
    # Keep track of packages that were installed
    installed = set()

    # Modify it as installing requirement_set would (assuming no errors)
    for inst_req in to_install:
        abstract_dist = make_distribution_for_install_requirement(inst_req)
        dist = abstract_dist.get_metadata_distribution()
        name = dist.canonical_name
        package_set[name] = PackageDetails(dist.version, list(dist.iter_dependencies()))

        installed.add(name)

    return installed


def _create_whitelist(
    would_be_installed: set[NormalizedName], package_set: PackageSet
) -> set[NormalizedName]:
    packages_affected = set(would_be_installed)

    for package_name in package_set:
        if package_name in packages_affected:
            continue

        for req in package_set[package_name].dependencies:
            if canonicalize_name(req.name) in would_be_installed:
                packages_affected.add(package_name)
                break

    return packages_affected


def _requirement_is_active(
    req: Requirement, active_extras: set[NormalizedName]
) -> bool:
    """Whether a dependency requirement is effective for the current env.

    Markers are evaluated against the running interpreter (like the rest of
    ``pip check``) for each selected extra. The empty extra is always
    considered, since selecting an extra never deactivates unconditional
    dependencies.
    """
    if req.marker is None:
        return True
    contexts = ("", *sorted(active_extras))
    return any(req.marker.evaluate({"extra": extra}) for extra in contexts)


def _compute_effective_graph(
    package_set: PackageSet,
    root: NormalizedName,
    root_extras: set[NormalizedName],
) -> dict[NormalizedName, list[DependencyEdge]]:
    """Effective installed dependency edges reachable from ``root``.

    The extras selected on a package (the root's user-selected extras, or the
    extras requested on an incoming edge) activate its extra-gated
    dependencies. Extras propagate along edges via a monotonic fixpoint:
    active-extra sets only grow and are finite, so dependency cycles cannot
    cause unbounded expansion.

    Edges pointing at missing distributions are not traversable and omitted;
    version conflicts only concern installed packages.
    """
    active_extras: dict[NormalizedName, set[NormalizedName]] = {
        root: set(root_extras)
    }
    adjacency: dict[NormalizedName, list[DependencyEdge]] = {}
    seen_edges: dict[NormalizedName, set[str]] = {}
    queue: deque[NormalizedName] = deque([root])

    while queue:
        source = queue.popleft()
        edges = adjacency.setdefault(source, [])
        edges_seen = seen_edges.setdefault(source, set())
        for req in package_set[source].dependencies:
            if not _requirement_is_active(req, active_extras.get(source, set())):
                continue
            target = canonicalize_name(req.name)
            if target not in package_set:
                continue
            # Distinct requirements (different specifiers/markers/extras) are
            # kept as distinct edges; identical duplicates collapse.
            requirement_string = str(req)
            if requirement_string in edges_seen:
                continue
            edges_seen.add(requirement_string)
            edges.append(DependencyEdge(req, target))

            requested_extras = {
                canonicalize_name(extra) for extra in req.extras
            }
            if target not in active_extras:
                # First effective edge reaching the node: expand it.
                active_extras[target] = set(requested_extras)
                queue.append(target)
            else:
                target_extras = active_extras[target]
                if not requested_extras <= target_extras:
                    target_extras.update(requested_extras)
                    queue.append(target)

    for edges in adjacency.values():
        edges.sort(key=lambda edge: str(edge.requirement))
    return adjacency


def _find_conflict_paths(
    adjacency: dict[NormalizedName, list[DependencyEdge]],
    root: NormalizedName,
    target: NormalizedName,
    requirement: Requirement,
) -> tuple[list[ConflictPath], bool]:
    """Enumerate bounded simple paths from ``root`` to ``target``.

    Only paths whose final edge declares ``requirement`` are returned. Cycles
    cannot be entered (a node already on the path is not expanded), and the
    search is bounded by ``MAX_EXPLAIN_DEPTH`` and
    ``MAX_EXPLAIN_PATHS_PER_REQUIREMENT``. The second return value tells
    whether the bounds cut off further results.
    """
    paths: list[ConflictPath] = []
    truncated = False
    requirement_string = str(requirement)

    def dfs(
        node: NormalizedName,
        nodes: list[NormalizedName],
        edges: list[DependencyEdge],
        visited: set[NormalizedName],
    ) -> bool:
        nonlocal truncated

        if edges and node == target:
            if str(edges[-1].requirement) == requirement_string:
                paths.append(ConflictPath(tuple(nodes), tuple(edges)))
                if len(paths) >= MAX_EXPLAIN_PATHS_PER_REQUIREMENT:
                    truncated = True
                    return False
            # The target is an endpoint; simple paths cannot reach it again.
            return True

        if len(edges) >= MAX_EXPLAIN_DEPTH:
            truncated = True
            return True

        for edge in adjacency.get(node, []):
            if edge.target in visited:
                continue
            nodes.append(edge.target)
            edges.append(edge)
            visited.add(edge.target)
            should_continue = dfs(edge.target, nodes, edges, visited)
            visited.remove(edge.target)
            edges.pop()
            nodes.pop()
            if not should_continue:
                return False
        return True

    dfs(root, [root], [], {root})

    paths.sort(
        key=lambda path: (
            path.nodes,
            tuple(str(edge.requirement) for edge in path.edges),
        )
    )
    return paths, truncated


def explain_conflict_paths(
    package_set: PackageSet,
    root: NormalizedName,
    root_extras: set[NormalizedName],
) -> list[ConflictExplanation] | None:
    """Explain which effective paths from ``root`` require conflicted packages.

    Returns ``None`` if ``root`` is not installed. Conflicting constraints
    proposed by different sources (or differing in specifier/marker/extras)
    appear as separate requirement groups, each with the paths that carry it.
    """
    if root not in package_set:
        return None

    adjacency = _compute_effective_graph(package_set, root, root_extras)

    # target -> requirement string -> Requirement: each violated constraint
    # gets its own group.
    violated: dict[NormalizedName, dict[str, Requirement]] = {}
    for edges in adjacency.values():
        for edge in edges:
            target = edge.target
            if edge.requirement.specifier.contains(
                package_set[target].version, prereleases=True
            ):
                continue
            violated.setdefault(target, {})[str(edge.requirement)] = (
                edge.requirement
            )

    explanations: list[ConflictExplanation] = []
    for target in sorted(violated):
        requirement_paths: list[RequirementPaths] = []
        for requirement_string in sorted(violated[target]):
            requirement = violated[target][requirement_string]
            paths, truncated = _find_conflict_paths(
                adjacency, root, target, requirement
            )
            if paths:
                requirement_paths.append(
                    RequirementPaths(requirement, paths, truncated)
                )
        if requirement_paths:
            explanations.append(
                ConflictExplanation(
                    target,
                    package_set[target].version,
                    requirement_paths,
                )
            )

    return explanations
