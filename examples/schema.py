from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"


class ModelProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    LOCAL = "local"


@dataclass
class ToolSchemaOutput:
    """Structured output for a single tool call in an agentic pipeline."""

    tool_name: str
    status: ToolStatus
    result: Any
    error: str | None = None
    tool_call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_success(self) -> bool:
        return self.status == ToolStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_error(cls, tool_name: str, error: str) -> "ToolSchemaOutput":
        return cls(
            tool_name=tool_name,
            status=ToolStatus.ERROR,
            result=None,
            error=error,
        )


@dataclass
class AgentRunConfig:
    """Configuration for a pi agent run."""

    model: str = ""
    provider: ModelProvider = ModelProvider.OPENAI
    skills_repo: str | None = None
    agents_md_path: str = "AGENTS.md"
    max_steps: int = 20
    temperature: float = 0.2
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PiAgentResult:
    """Aggregated result returned by the pi agent after completing a task."""

    task: str
    outputs: list[ToolSchemaOutput]
    summary: str
    success: bool
    run_config: AgentRunConfig
    agent_run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    steps_taken: int = 0

    def failed_outputs(self) -> list[ToolSchemaOutput]:
        return [o for o in self.outputs if not o.is_success()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.agent_run_id,
            "task": self.task,
            "success": self.success,
            "summary": self.summary,
            "steps_taken": self.steps_taken,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "outputs": [o.to_dict() for o in self.outputs],
            "run_config": {
                "model": self.run_config.model,
                "provider": self.run_config.provider.value,
                "skills_repo": self.run_config.skills_repo,
                "agents_md_path": self.run_config.agents_md_path,
                "max_steps": self.run_config.max_steps,
            },
        }


class PiAgent:
    """
    Agentic executor that reads a skills repository and AGENTS.md,
    then drives a model to complete arbitrary tasks, returning structured
    ToolSchemaOutput results.
    """

    def __init__(self, config: AgentRunConfig | None = None) -> None:
        self.config = config or AgentRunConfig()
        self._skills: list[str] = []
        self._agent_instructions: str = ""

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _load_skills_repo(self) -> None:
        """Read skill definitions from the configured skills repository path."""
        if not self.config.skills_repo:
            return
        import os

        skills_dir = self.config.skills_repo
        if not os.path.isdir(skills_dir):
            raise FileNotFoundError(f"Skills repo not found: {skills_dir}")
        for fname in sorted(os.listdir(skills_dir)):
            fpath = os.path.join(skills_dir, fname)
            if os.path.isfile(fpath) and fname.endswith((".md", ".txt", ".yaml")):
                with open(fpath, encoding="utf-8") as fh:
                    self._skills.append(fh.read())

    def _load_agents_md(self) -> None:
        """Read AGENTS.md to obtain high-level agent instructions."""
        import os

        path = self.config.agents_md_path
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                self._agent_instructions = fh.read()

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def run(self, task: str, *args: Any, **kwargs: Any) -> PiAgentResult:
        """
        Execute ``task`` using the configured model and return a
        ``PiAgentResult`` whose ``outputs`` list contains one
        ``ToolSchemaOutput`` per tool call performed.
        """
        import time

        started_at = datetime.now(timezone.utc)

        self._load_skills_repo()
        self._load_agents_md()

        outputs: list[ToolSchemaOutput] = []
        steps = 0

        try:
            tool_outputs = self._dispatch(task, *args, **kwargs)
            outputs.extend(tool_outputs)
            steps = len(outputs)
            success = all(o.is_success() for o in outputs)
            summary = (
                f"Completed '{task}' in {steps} step(s)."
                if success
                else f"'{task}' finished with {len([o for o in outputs if not o.is_success()])} error(s)."
            )
        except Exception as exc:
            outputs.append(ToolSchemaOutput.from_error("pi.agent", str(exc)))
            success = False
            summary = f"Agent run failed: {exc}"

        return PiAgentResult(
            task=task,
            outputs=outputs,
            summary=summary,
            success=success,
            run_config=self.config,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            steps_taken=steps,
        )

    def _dispatch(self, task: str, *args: Any, **kwargs: Any) -> list[ToolSchemaOutput]:
        """
        Internal dispatcher: iterates up to ``max_steps`` times, calling
        the model and executing the tools it selects until the task is
        complete or the step limit is reached.

        Replace or extend this method to plug in a real LLM backend.
        """
        import time

        results: list[ToolSchemaOutput] = []

        context = {
            "task": task,
            "skills": self._skills,
            "instructions": self._agent_instructions,
            "args": args,
            "kwargs": kwargs,
        }

        for step in range(self.config.max_steps):
            t0 = time.monotonic()
            tool_name, tool_result = self._call_model_step(context, step)
            duration_ms = (time.monotonic() - t0) * 1_000

            output = ToolSchemaOutput(
                tool_name=tool_name,
                status=ToolStatus.SUCCESS,
                result=tool_result,
                duration_ms=duration_ms,
                metadata={"step": step},
            )
            results.append(output)

            if self._is_terminal(tool_result):
                break

        return results

    def _call_model_step(
        self, context: dict[str, Any], step: int
    ) -> tuple[str, Any]:
        """
        Placeholder for a real LLM API call.  Override this method to
        integrate with OpenAI, Anthropic, Google, or any local model.

        Returns a (tool_name, tool_result) tuple.
        """
        raise NotImplementedError(
            "PiAgent._call_model_step must be implemented with a real LLM backend. "
            "Override this method or subclass PiAgent to connect to your model provider."
        )

    @staticmethod
    def _is_terminal(tool_result: Any) -> bool:
        """Return True when the agent has signalled that the task is complete."""
        if isinstance(tool_result, dict):
            return tool_result.get("done", False)
        return False


# ---------------------------------------------------------------------------
# Module-level convenience helper  (mirrors the pseudocode's `pi.agent(...)`)
# ---------------------------------------------------------------------------

def agent(task: str, *args: Any, config: AgentRunConfig | None = None, **kwargs: Any) -> PiAgentResult:
    """
    Convenience entry-point.  Instantiates a ``PiAgent`` and runs ``task``.

    Example::

        result = agent("refactor utils.py", config=AgentRunConfig(skills_repo="./skills"))
        print(result.summary)
    """
    return PiAgent(config=config).run(task, *args, **kwargs)
