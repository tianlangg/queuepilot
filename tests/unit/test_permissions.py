import pytest

from queuepilot.auth.permissions import Action, Role, allowed


@pytest.mark.parametrize("role", ["requester", "agent", "admin"])
@pytest.mark.parametrize(
    ("action", "owner", "expected"),
    [
        ("user_read", False, {"admin"}),
        ("user_read", True, {"requester", "agent", "admin"}),
        ("user_create", False, {"admin"}),
        ("user_disable", False, {"admin"}),
        ("user_disable", True, set()),
        ("ticket_read", False, {"agent", "admin"}),
        ("ticket_read", True, {"requester", "agent", "admin"}),
        ("ticket_create", True, {"requester", "agent", "admin"}),
        ("ticket_create", False, {"requester", "agent", "admin"}),
        ("ticket_comment_public", True, {"requester", "agent", "admin"}),
        ("ticket_comment_internal", False, {"agent", "admin"}),
        ("ticket_comment_public", False, {"agent", "admin"}),
        ("ticket_claim", False, {"agent", "admin"}),
        ("ticket_reassign", False, {"admin"}),
    ],
)
def test_permission_cell(
    role: Role, action: Action, owner: bool, expected: set[str]
) -> None:
    assert allowed(role, action, owner=owner) is (role in expected)
