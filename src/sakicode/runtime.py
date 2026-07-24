"""Explicit runtime state for one or more agent turns.

Keeping transitions in one small, testable object makes the agent loop easier
to inspect now and gives checkpoint persistence a stable boundary later.
"""

from dataclasses import asdict, dataclass
from enum import Enum


class AgentState(str, Enum):
    IDLE = "idle"
    REQUESTING_MODEL = "requesting_model"
    EXECUTING_TOOLS = "executing_tools"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    LIMIT_REACHED = "limit_reached"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = {
    AgentState.COMPLETED,
    AgentState.FAILED,
    AgentState.LIMIT_REACHED,
    AgentState.INTERRUPTED,
}

_ALLOWED_TRANSITIONS = {
    AgentState.IDLE: {AgentState.REQUESTING_MODEL},
    AgentState.REQUESTING_MODEL: {
        AgentState.EXECUTING_TOOLS,
        AgentState.COMPLETED,
        AgentState.FAILED,
        AgentState.INTERRUPTED,
    },
    AgentState.EXECUTING_TOOLS: {
        AgentState.WAITING_APPROVAL,
        AgentState.REQUESTING_MODEL,
        AgentState.LIMIT_REACHED,
        AgentState.FAILED,
        AgentState.INTERRUPTED,
    },
    AgentState.WAITING_APPROVAL: {
        AgentState.EXECUTING_TOOLS,
        AgentState.INTERRUPTED,
    },
    # A new user turn may start after any previous terminal outcome.
    **{state: {AgentState.REQUESTING_MODEL} for state in TERMINAL_STATES},
}


@dataclass(frozen=True)
class StateEvent:
    """A serializable record explaining why a runtime transition happened."""

    previous: AgentState
    current: AgentState
    reason: str

    def to_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["previous"] = self.previous.value
        data["current"] = self.current.value
        return data


class InvalidStateTransition(RuntimeError):
    pass


class AgentStateMachine:
    """Validate and record the control-flow states of the agent runtime."""

    def __init__(self) -> None:
        self.state = AgentState.IDLE
        self.history: list[StateEvent] = []

    def begin_turn(self) -> None:
        self.transition(AgentState.REQUESTING_MODEL, "user turn started")

    def transition(self, target: AgentState, reason: str) -> None:
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise InvalidStateTransition(
                f"cannot transition from {self.state.value!r} to {target.value!r}"
            )
        event = StateEvent(previous=self.state, current=target, reason=reason)
        self.state = target
        self.history.append(event)

    def snapshot(self) -> dict:
        """Return JSON-compatible state used by diagnostics and future checkpoints."""
        return {
            "state": self.state.value,
            "history": [event.to_dict() for event in self.history],
        }
