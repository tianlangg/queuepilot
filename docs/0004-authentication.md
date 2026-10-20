# Milestone 2 authentication and authorization

## Design decisions

User emails are normalized to lowercase and are unique. Passwords use Argon2id
through `argon2-cffi`; hashing and verification run outside the event loop.
Password hashes are never included in Pydantic responses. An administrator is
created only by the explicit terminal bootstrap command. The first bootstrap
uses a PostgreSQL transaction advisory lock so concurrent attempts cannot each
create a separate initial administrator. Subsequent runs for that same active
administrator are no-ops; other emails and existing non-admin accounts fail.

Access tokens are HS256 JWTs with required subject, session ID, type, issuer,
audience, issued-at, not-before, and expiry claims. The signing secret comes
from validated settings, never source code. Every protected request checks the
associated PostgreSQL session and current user status. Refresh credentials are
opaque random tokens; only SHA-256 digests are stored. Rotation locks the user
and session rows in that order, marks the old session used, and inserts a new
session in the same transaction. Replaying a used token revokes the family.
The user-first lock order also applies to disable operations. Logout revokes the
current family; disabling revokes every family for the account.

All credentials are JSON bearer values. There are no credential cookies or CSRF
tokens. Clients must protect refresh tokens against script access and never put
them in URLs. A future browser UI needs a separate storage/security design.
The API returns sanitized 401, 403, 404, 409, and 422 errors and does not log
credential values. Tokens are valid only while their database session is active.

## Permission matrix

| Action | Requester | Agent | Administrator |
| --- | --- | --- | --- |
| Read own user | Yes | Yes | Yes |
| Read another user | No | No | Yes |
| Create requester/agent | No | No | Yes |
| Create administrator through API | No | No | No |
| Disable another user | No | No | Yes |
| Disable self | No | No | No |
| Read own ticket (milestone 3) | Yes | Yes | Yes |
| Read another ticket (milestone 3) | No | Yes | Yes |
| Create ticket (milestone 3) | Yes | Yes | Yes |
| Add public comment to visible ticket (milestone 3) | Yes | Yes | Yes |
| Add internal comment (milestone 3) | No | Yes | Yes |
| Claim ticket (milestone 4) | No | Yes | Yes |
| Reassign ticket (milestone 4) | No | No | Yes |

The ticket entries define policy for later services; there are no ticket routes
in milestone 2. Ticket services must check resource ownership/visibility in
queries and transactions, not only this pure policy function. Unit tests cover
every role/action cell. HTTP integration tests cover all implemented user-route
denials and session behavior against PostgreSQL.

## Operational limits

Password reset, account role changes, and email verification are not yet API
features. Keep the signing key stable across replicas; replacing it invalidates
access tokens. Refresh sessions remain in PostgreSQL until future retention
cleanup is added. Access token validation incurs one database query per request.
No credential cookies are used. These choices can be revisited with a browser UI.
