"""Approach 2: Request Plan + independent per-site pipelines.

Completely separate from Approach 1 (sequential Pipeline.gather).
"""

from psychofilm_analyzer.gather_v2.runner import run_gather_v2

__all__ = ["run_gather_v2"]
