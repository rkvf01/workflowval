"""`benchmarks/seeded/labels.json`: ground truth for the seeded-bug app (Evaluation)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from workflowval.schemas._base import Strict


class SeededBug(Strict):
    """One planted defect or conflict and the label it should get."""

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    expected_label: Literal["API_BUG", "SPEC_DRIFT", "CONFLICT"]
    operations: list[str] = Field(min_length=1, description="operationIds where it can be seen")


class Labels(Strict):
    """The whole ground-truth file."""

    bugs: list[SeededBug] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Labels:
        ids = [bug.id for bug in self.bugs]
        if len(ids) != len(set(ids)):
            raise ValueError("bug ids must be unique")
        return self
