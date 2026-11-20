"""What a review request may contain, checked before any of it reaches a model."""

from typing import Final, Self

from pydantic import BaseModel, Field, model_validator

MAX_CONTEXT_SNIPPETS: Final = 20


class ContextSnippet(BaseModel):
    """Code from the same repository that a search found related to the
    change, such as a caller of a changed function. The GitHub integration
    sends these. They are background for the reviewer, not under review."""

    path: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=60_000)

    @model_validator(mode="after")
    def lines_in_order(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line comes before start_line")
        return self


class ReviewRequest(BaseModel):
    diff: str = Field(min_length=1, description="A unified diff, e.g. the output of `git diff`")
    notes: str | None = Field(
        default=None, max_length=2000, description="Optional notes from the author"
    )
    context: list[ContextSnippet] = Field(
        default=[],
        max_length=MAX_CONTEXT_SNIPPETS,
        description="Optional related code from the same repository",
    )
