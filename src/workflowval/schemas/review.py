"""`review.json`: the Reviewer's fixes and missing journeys, with the Planner's answers."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from workflowval.schemas._base import Strict
from workflowval.schemas.workflow import Basis


class Decision(StrEnum):
    """How the Planner answered a review item."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Answer(Strict):
    """The Planner's answer. Nothing is dropped silently, so a rejection needs a reason."""

    decision: Decision
    reason: str | None = None
    workflow_id: str | None = Field(default=None, description="File that carries out the answer")

    @model_validator(mode="after")
    def _check(self) -> Answer:
        if self.decision is Decision.REJECTED and not self.reason:
            raise ValueError("a rejection must give a reason")
        if self.decision is Decision.ACCEPTED and not self.workflow_id:
            raise ValueError("an accepted item must name its workflow_id")
        return self


class WorkflowFix(Strict):
    """A workflow that is wrong or weak."""

    id: str = Field(min_length=1)
    workflow_id: str
    reason: str = Field(min_length=1)
    answer: Answer | None = None


class MissingJourney(Strict):
    """A user flow the workflows do not cover."""

    id: str = Field(min_length=1)
    user: str = Field(min_length=1, description="The user named in the docs, or 'API consumer'")
    steps: list[str] = Field(min_length=1)
    rule: str = Field(min_length=1)
    basis: Basis
    answer: Answer | None = None


class Review(Strict):
    """One review round's output."""

    round: int = Field(ge=1)
    fixes: list[WorkflowFix] = Field(default_factory=list)
    missing_journeys: list[MissingJourney] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Review:
        ids = [item.id for item in self._items()]
        if len(ids) != len(set(ids)):
            raise ValueError("review item ids must be unique")
        return self

    def unanswered(self) -> list[str]:
        """Ids of items the Planner has not answered yet."""
        return [item.id for item in self._items() if item.answer is None]

    def _items(self) -> list[WorkflowFix | MissingJourney]:
        return [*self.fixes, *self.missing_journeys]
