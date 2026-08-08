"""External runtime-reward framework for vulnerability reproduction."""

from .models import (
    STAGES,
    EvidenceRecord,
    EvidenceState,
    Feedback,
    HarnessPolicy,
    HarnessState,
    ObservationState,
    ProbePlan,
    RewardSpec,
    StageAssessment,
    StageStatus,
    TaskContext,
    TrajectoryState,
)
from .orchestrator import RewardFramework
from .cross_sample import CrossSampleHarnessPatcher, CrossSampleTrainer
from .episode_analyzer import EpisodeAnalyzer, EpisodeMetrics
from .experience_pool import ExperiencePool
from .harness_repository import HarnessRepository

__all__ = [
    "CrossSampleHarnessPatcher",
    "CrossSampleTrainer",
    "EpisodeAnalyzer",
    "EpisodeMetrics",
    "EvidenceRecord",
    "EvidenceState",
    "ExperiencePool",
    "Feedback",
    "HarnessPolicy",
    "HarnessRepository",
    "HarnessState",
    "ObservationState",
    "ProbePlan",
    "RewardFramework",
    "RewardSpec",
    "STAGES",
    "StageAssessment",
    "StageStatus",
    "TaskContext",
    "TrajectoryState",
]
