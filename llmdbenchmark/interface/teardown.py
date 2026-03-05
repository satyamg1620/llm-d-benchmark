"""
teardown.py

Defines the `teardown` subcommand for the CLI, including argument definitions
for tearing down previously deployed llm-d stacks.
"""

import argparse
from llmdbenchmark.interface.commands import Command


def add_subcommands(parser: argparse._SubParsersAction):
    """
    Add the `teardown` subcommand to the given parser.

    This command removes resources created by a previous standup, including
    Helm releases, namespaced resources, and optionally cluster-scoped roles.

    Args:
        parser (argparse._SubParsersAction): The subparsers object returned by
        parser.add_subparsers().
    """
    teardown_parser = parser.add_parser(
        Command.TEARDOWN.value,
        description=(
            "The `teardown` command removes resources deployed by a previous standup. "
            "It uninstalls Helm releases, deletes namespaced resources, and optionally "
            "removes cluster-scoped roles. Use --deep for a full namespace wipe."
        ),
        help="Tear down a previously deployed llm-d stack.",
    )
    teardown_parser.add_argument(
        "-s",
        "--step",
        help="Step list (comma-separated values or ranges, e.g. 0,1,3 or 0-4).",
    )
    teardown_parser.add_argument(
        "-t", "--methods", help="Deployment methods to tear down (standalone, modelservice)."
    )
    teardown_parser.add_argument(
        "-r", "--release",
        default="llmdbench",
        help="Modelservice Helm chart release name (default: llmdbench).",
    )
    teardown_parser.add_argument(
        "-d", "--deep",
        action="store_true",
        help="Deep cleaning: delete ALL resources in both namespaces.",
    )
    teardown_parser.add_argument(
        "--kubeconfig",
        "-k",
        help="Path to kubeconfig file for kubectl/helm/helmfile commands.",
    )
