"""Public ticket contracts."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["open", "in_progress", "resolved", "closed"]


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    requester_id: uuid.UUID
    assignee_id: uuid.UUID | None
    title: str
    description: str
    status: Status
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


class TicketPage(BaseModel):
    items: list[TicketResponse]
    next_cursor: str | None


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000)
    internal: bool = False
    expected_version: int = Field(ge=1)


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    internal: bool
    created_at: datetime


class CommentPage(BaseModel):
    items: list[CommentResponse]
    next_cursor: str | None


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status
    expected_version: int = Field(ge=1)
