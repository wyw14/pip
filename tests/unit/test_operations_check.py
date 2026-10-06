from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import NormalizedName, canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.operations.check import (
    MAX_EXPLAIN_DEPTH,
    ConflictExplanation,
    PackageDetails,
    PackageSet,
    _create_whitelist,
    explain_conflict_paths,
)


def package_details(*dependencies: str, version: str = "1") -> PackageDetails:
    return PackageDetails(
        Version(version),
        [Requirement(dependency) for dependency in dependencies],
    )


def make_package_set(
    packages: dict[str, tuple[str, list[str]]],
) -> PackageSet:
    return {
        canonicalize_name(name): package_details(*deps, version=version)
        for name, (version, deps) in packages.items()
    }


def edge_labels(explanation: ConflictExplanation, index: int = 0) -> list[str]:
    paths = explanation.requirement_paths[index].paths
    assert len(paths) == 1
    return [str(edge.requirement) for edge in paths[0].edges]


def test_create_whitelist_is_not_order_dependent() -> None:
    root = canonicalize_name("root")
    middle = canonicalize_name("middle")
    leaf = canonicalize_name("leaf")
    expected: set[NormalizedName] = {leaf, middle}

    package_set_with_dependent_first: PackageSet = {
        root: package_details("middle"),
        middle: package_details("leaf"),
        leaf: package_details(),
    }
    package_set_with_dependency_first: PackageSet = {
        leaf: package_details(),
        middle: package_details("leaf"),
        root: package_details("middle"),
    }

    assert _create_whitelist({leaf}, package_set_with_dependent_first) == expected
    assert _create_whitelist({leaf}, package_set_with_dependency_first) == expected


def test_explain_returns_none_for_missing_root() -> None:
    package_set = make_package_set({"broken": ("0.1", [])})

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is None


def test_explain_direct_conflict_path() -> None:
    package_set = make_package_set(
        {
            "root": ("1.0", ["broken>=1.0"]),
            "broken": ("0.1", []),
        }
    )

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is not None
    assert len(result) == 1
    explanation = result[0]
    assert explanation.target == canonicalize_name("broken")
    assert explanation.target_version == Version("0.1")
    assert len(explanation.requirement_paths) == 1
    requirement_paths = explanation.requirement_paths[0]
    assert str(requirement_paths.requirement) == "broken>=1.0"
    assert not requirement_paths.truncated
    path = requirement_paths.paths[0]
    assert path.nodes == (canonicalize_name("root"), canonicalize_name("broken"))


def test_explain_transitive_path_preserves_edge_details() -> None:
    package_set = make_package_set(
        {
            "root": (
                "1.0",
                ['middle[ssl]>=0.5; python_version >= "3"'],
            ),
            "middle": ("0.9", ["broken>=1.0"]),
            "broken": ("0.1", []),
        }
    )

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is not None
    assert len(result) == 1
    labels = edge_labels(result[0])
    assert labels == ['middle[ssl]>=0.5; python_version >= "3"', "broken>=1.0"]
    assert result[0].requirement_paths[0].paths[0].nodes == (
        canonicalize_name("root"),
        canonicalize_name("middle"),
        canonicalize_name("broken"),
    )


def test_explain_different_constraints_are_grouped_separately() -> None:
    package_set = make_package_set(
        {
            "root": ("1.0", ["broken>=1.0", "middle"]),
            "middle": ("1.0", ["broken==2.0"]),
            "broken": ("0.1", []),
        }
    )

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is not None
    assert len(result) == 1
    groups = {
        str(group.requirement): [path.nodes for path in group.paths]
        for group in result[0].requirement_paths
    }
    assert set(groups) == {"broken>=1.0", "broken==2.0"}
    assert groups["broken>=1.0"] == [
        (canonicalize_name("root"), canonicalize_name("broken"))
    ]
    assert groups["broken==2.0"] == [
        (
            canonicalize_name("root"),
            canonicalize_name("middle"),
            canonicalize_name("broken"),
        )
    ]


def test_explain_inactive_marker_edges_are_excluded() -> None:
    inactive = make_package_set(
        {
            "root": ("1.0", ['broken>=1.0; python_version < "0.9"']),
            "broken": ("0.1", []),
        }
    )
    assert explain_conflict_paths(inactive, canonicalize_name("root"), set()) == []

    active = make_package_set(
        {
            "root": ("1.0", ['broken>=1.0; python_version >= "3"']),
            "broken": ("0.1", []),
        }
    )
    result = explain_conflict_paths(active, canonicalize_name("root"), set())
    assert result is not None
    assert len(result) == 1
    assert 'python_version >= "3"' in edge_labels(result[0])[0]


def test_explain_root_extras_activate_gated_dependency() -> None:
    package_set = make_package_set(
        {
            "root": ("1.0", ['broken>=1.0; extra == "x"']),
            "broken": ("0.1", []),
        }
    )

    without_extra = explain_conflict_paths(
        package_set, canonicalize_name("root"), set()
    )
    assert without_extra == []

    with_extra = explain_conflict_paths(
        package_set, canonicalize_name("root"), {canonicalize_name("x")}
    )
    assert with_extra is not None
    assert len(with_extra) == 1
    labels = edge_labels(with_extra[0])
    assert 'broken>=1.0; extra == "x"' in labels[0]


def test_explain_extras_propagate_along_edges() -> None:
    package_set = make_package_set(
        {
            "root": ("1.0", ["middle[x]"]),
            "middle": ("1.0", ['broken>=1.0; extra == "x"']),
            "broken": ("0.1", []),
        }
    )

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is not None
    assert len(result) == 1
    assert edge_labels(result[0]) == [
        "middle[x]",
        'broken>=1.0; extra == "x"',
    ]


def test_explain_dependency_cycles_do_not_expand_infinitely() -> None:
    package_set = make_package_set(
        {
            "root": ("1.0", ["a"]),
            "a": ("1.0", ["b", "broken>=1.0"]),
            "b": ("1.0", ["a"]),
            "broken": ("0.1", []),
        }
    )

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is not None
    assert len(result) == 1
    paths = result[0].requirement_paths[0].paths
    assert [(path.nodes, path.edges) for path in paths]
    for path in paths:
        # Simple paths: no node is visited twice.
        assert len(set(path.nodes)) == len(path.nodes)
        assert len(path.edges) == len(path.nodes) - 1
    assert edge_labels(result[0]) == ["a", "broken>=1.0"]


def test_explain_missing_intermediate_is_not_traversed() -> None:
    package_set = make_package_set(
        {
            "root": ("1.0", ["absent", "broken>=1.0"]),
            "broken": ("0.1", []),
        }
    )

    result = explain_conflict_paths(package_set, canonicalize_name("root"), set())

    assert result is not None
    assert len(result) == 1
    assert edge_labels(result[0]) == ["broken>=1.0"]


def _chain_to_broken(num_edges: int) -> dict[str, tuple[str, list[str]]]:
    """A linear root -> l1 -> ... -> broken chain with num_edges edges."""
    nodes = ["root", *(f"l{i}" for i in range(1, num_edges)), "broken"]
    assert len(nodes) == num_edges + 1
    packages: dict[str, tuple[str, list[str]]] = {}
    for node, next_node in zip(nodes, nodes[1:]):
        requirement = "broken>=1.0" if next_node == "broken" else next_node
        packages[node] = ("1", [requirement])
    packages["broken"] = ("0.1", [])
    return packages


def test_explain_depth_bound_truncates_long_paths() -> None:
    # A chain with exactly MAX_EXPLAIN_DEPTH edges is reported in full.
    result = explain_conflict_paths(
        make_package_set(_chain_to_broken(MAX_EXPLAIN_DEPTH)),
        canonicalize_name("root"),
        set(),
    )
    assert result is not None
    assert len(result) == 1
    group = result[0].requirement_paths[0]
    assert not group.truncated
    assert len(group.paths[0].edges) == MAX_EXPLAIN_DEPTH

    # One more edge: the conflict is only reachable beyond the bound.
    result = explain_conflict_paths(
        make_package_set(_chain_to_broken(MAX_EXPLAIN_DEPTH + 1)),
        canonicalize_name("root"),
        set(),
    )
    assert result == []
