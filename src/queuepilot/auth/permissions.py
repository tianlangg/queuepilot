"""Resource permission matrix for milestone 2 and future ticket services."""

from typing import Literal

Role = Literal["requester", "agent", "admin"]
Action = Literal[
    "user_read",
    "user_create",
    "user_disable",
    "ticket_read",
    "ticket_create",
    "ticket_comment_public",
    "ticket_comment_internal",
    "ticket_claim",
    "ticket_reassign",
]


def allowed(
    role: Role, action: Action, *, owner: bool = False, assignee: bool = False
) -> bool:
    """Ticket services must recheck ownership in their query or transaction."""
    if action == "user_read":
        return role == "admin" or owner
    if action == "user_disable":
        return role == "admin" and not owner
    if action in {"user_create", "ticket_reassign"}:
        return role == "admin"
    if action == "ticket_read":
        return role in {"agent", "admin"} or owner
    if action == "ticket_create":
        return True
    if action == "ticket_comment_public":
        return role in {"agent", "admin"} or owner
    if action == "ticket_comment_internal":
        return role in {"agent", "admin"}
    if action == "ticket_claim":
        return role == "agent" or role == "admin"
    return False
