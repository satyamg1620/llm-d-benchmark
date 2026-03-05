"""
commands.py

Defines the valid CLI commands for the llmdbenchmark package.
"""

from enum import Enum


class Command(Enum):
    """Valid CLI commands for llmdbenchmark."""

    PLAN = "plan"
    STANDUP = "standup"
