"""The subagent registry. Each subagent is one folder with agent.py and prompt.md."""

from .application_prep.agent import SPEC as APPLICATION_PREP
from .candidate_profile.agent import SPEC as CANDIDATE_PROFILE
from .fit_check.agent import SPEC as FIT_CHECK
from .job_discovery.agent import SPEC as JOB_DISCOVERY
from .job_research.agent import SPEC as JOB_RESEARCH
from .spec import SubagentSpec

REGISTRY: dict[str, SubagentSpec] = {
    spec.name: spec for spec in (CANDIDATE_PROFILE, JOB_DISCOVERY, JOB_RESEARCH, FIT_CHECK, APPLICATION_PREP)}
