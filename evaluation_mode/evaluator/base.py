"""Shared evaluator interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvaluationInput:
    """Paths for one evaluator run."""

    ground_truth: Path
    trajectory: Path
    output: Path | None = None
    submitted_pocs: Path | None = None


class BaseEvaluator(ABC):
    """Base class for deterministic metric evaluators."""

    name: str
    version: str = "0.1"

    @abstractmethod
    def evaluate(self, inputs: EvaluationInput) -> dict[str, Any]:
        """Return a parseable evaluation result."""
