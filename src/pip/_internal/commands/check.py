import logging
from optparse import Values

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.utils import NormalizedName, canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.cli.base_command import Command
from pip._internal.cli.status_codes import ERROR, SUCCESS
from pip._internal.metadata import get_default_environment
from pip._internal.operations.check import (
    MAX_EXPLAIN_DEPTH,
    ConflictExplanation,
    PackageSet,
    check_package_set,
    check_unsupported,
    create_package_set_from_installed,
    explain_conflict_paths,
)
from pip._internal.utils.compatibility_tags import get_supported
from pip._internal.utils.misc import write_output

logger = logging.getLogger(__name__)


class CheckCommand(Command):
    """Verify installed packages have compatible dependencies."""

    ignore_require_venv = True
    usage = """
      %prog [options]"""

    def add_options(self) -> None:
        self.cmd_opts.add_option(
            "--explain",
            dest="explain",
            action="store",
            default=None,
            metavar="PROJECT",
            help=(
                "Show bounded, effective dependency paths from an installed "
                "top-level project to the packages whose installed versions "
                "conflict with its dependencies. Extras explicitly selected "
                'by the user may be given as "project[extra]".'
            ),
        )

        self.parser.insert_option_group(0, self.cmd_opts)

    def run(self, options: Values, args: list[str]) -> int:
        package_set, parsing_probs = create_package_set_from_installed()
        missing, conflicting = check_package_set(package_set)
        unsupported = list(
            check_unsupported(
                get_default_environment().iter_installed_distributions(),
                get_supported(),
            )
        )

        explanations, root_display, explain_error = _build_explanations(
            options.explain, package_set
        )

        for project_name in missing:
            version = package_set[project_name].version
            for dependency in missing[project_name]:
                write_output(
                    "%s %s requires %s, which is not installed.",
                    project_name,
                    version,
                    dependency[0],
                )

        # Conflict one-liners already covered by an explanation block are
        # replaced by the (more detailed) path output; unrelated conflicts
        # keep their usual one-liners.
        explained_conflicts = _collect_explained_conflicts(explanations)
        for project_name in conflicting:
            version = package_set[project_name].version
            for dep_name, dep_version, req in conflicting[project_name]:
                if (project_name, str(req)) in explained_conflicts:
                    continue
                write_output(
                    "%s %s has requirement %s, but you have %s %s.",
                    project_name,
                    version,
                    req,
                    dep_name,
                    dep_version,
                )

        if explanations is not None:
            _print_explanations(explanations, package_set, root_display)

        for package in unsupported:
            write_output(
                "%s %s is not supported on this platform",
                package.raw_name,
                package.version,
            )
        if missing or conflicting or parsing_probs or unsupported or explain_error:
            return ERROR
        else:
            write_output("No broken requirements found.")
            return SUCCESS


def _parse_explain_option(
    value: str,
) -> tuple[NormalizedName | None, set[NormalizedName]]:
    """Parse the --explain value as a project name with optional extras."""
    try:
        requirement = Requirement(value)
    except InvalidRequirement:
        logger.error("Invalid project name for --explain: %s", value)
        return None, set()
    if requirement.url or requirement.specifier or requirement.marker:
        logger.error(
            "--explain expects a project name with optional extras "
            '(e.g. "project[extra]"), got: %s',
            value,
        )
        return None, set()
    root_name = canonicalize_name(requirement.name)
    root_extras = {canonicalize_name(extra) for extra in requirement.extras}
    return root_name, root_extras


def _build_explanations(
    value: str | None, package_set: PackageSet
) -> tuple[list[ConflictExplanation] | None, str, bool]:
    """Build conflict-path explanations for the --explain option.

    Returns ``(explanations, root_display, error)``. ``explanations`` is None
    when --explain was not given and an empty list when the root has no
    effective path to a conflict.
    """
    if value is None:
        return None, "", False

    root_name, root_extras = _parse_explain_option(value)
    if root_name is None:
        return None, "", True

    explanations = explain_conflict_paths(package_set, root_name, root_extras)
    if explanations is None:
        logger.error(
            "Cannot explain dependency paths for %s: package is not installed.",
            value,
        )
        return None, "", True

    root_display = _format_project(
        root_name, root_extras, package_set[root_name].version
    )
    return explanations, root_display, False


def _collect_explained_conflicts(
    explanations: list[ConflictExplanation] | None,
) -> set[tuple[NormalizedName, str]]:
    """The (requirer, requirement) pairs already shown via an explanation."""
    explained: set[tuple[NormalizedName, str]] = set()
    if explanations is None:
        return explained
    for explanation in explanations:
        for requirement_paths in explanation.requirement_paths:
            for path in requirement_paths.paths:
                explained.add(
                    (path.nodes[-2], str(requirement_paths.requirement))
                )
    return explained


def _format_project(
    name: NormalizedName, extras: set[NormalizedName], version: Version
) -> str:
    if extras:
        return f"{name}[{','.join(sorted(extras))}] {version}"
    return f"{name} {version}"


def _print_explanations(
    explanations: list[ConflictExplanation],
    package_set: PackageSet,
    root_display: str,
) -> None:
    if not explanations:
        write_output("No conflicting dependency paths found from %s.", root_display)
        return

    for explanation in explanations:
        for requirement_paths in explanation.requirement_paths:
            write_output(
                "%s %s does not satisfy %s, required on these paths from %s:",
                explanation.target,
                explanation.target_version,
                requirement_paths.requirement,
                root_display,
            )
            for path in requirement_paths.paths:
                write_output(
                    "  %s %s",
                    path.nodes[0],
                    package_set[path.nodes[0]].version,
                )
                for edge, node in zip(path.edges, path.nodes[1:]):
                    write_output(
                        "    -> %s %s  [%s]",
                        node,
                        package_set[node].version,
                        edge.requirement,
                    )
            if requirement_paths.truncated:
                write_output(
                    "  ... (path search truncated at %d edges)",
                    MAX_EXPLAIN_DEPTH,
                )
