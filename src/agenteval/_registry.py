"""Inspect entry point: importing this module registers the agenteval sandbox providers."""

from agenteval.environments.tart.sandbox import TartSandboxEnvironment

__all__ = ["TartSandboxEnvironment"]
