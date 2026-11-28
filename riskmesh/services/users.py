"""
Users, roles and tokens (mixed into RiskService).

- Passwords: scrypt (hashlib, no extra dependency), random salt, constant-time
  comparison. Stored as "scrypt$n$r$p$salt$hash".
- Tokens: 32 random bytes, shown once; only their SHA-256 is stored. Session
  tokens expire; API tokens (for scripts) do not unless an expiry is given.
- Roles: viewer < editor < admin. There is always at least one active admin.
- Every change to users and tokens is in the audit trail; password and token
  hashes are masked there.
"""

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..domain.errors import AuthenticationError, ConflictError, PermissionDenied, ValidationError
from ..domain.models import ROLES, ApiToken, User
from .core import ServiceCore

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 10
SESSION_HOURS = 12
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\-]{2,100}$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def check_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        # same amount of work as a real check, so timing does not reveal whether the user exists
        hashlib.scrypt(b"x", salt=b"0" * 16, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return False
    _, n, r, p, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                               p=int(p), dklen=32)
    return hmac.compare_digest(candidate, base64.b64decode(digest))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def role_at_least(role: str, required: str) -> bool:
    return ROLES.index(role) >= ROLES.index(required)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserAdmin(ServiceCore):
    """Mixin for RiskService. Uses its repo, _save/_remove/_audit and the write transaction."""

    # -- validation ------------------------------------------------------------

    @staticmethod
    def _check_password_strength(password: str) -> None:
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationError(f"Passwords must have at least {MIN_PASSWORD_LENGTH} characters.")

    def _check_user(self, user: User) -> None:
        if not USERNAME_RE.match(user.username or ""):
            raise ValidationError("Username: 2-100 characters, letters, digits and . _ @ - only.")
        if user.role not in ROLES:
            raise ValidationError(f"role must be one of {', '.join(ROLES)}.")
        for other in self.repo.list(User, username=user.username):
            if other.id != user.id:
                raise ConflictError(f"The username '{user.username}' is taken.")

    def _active_admins(self) -> List[User]:
        return [u for u in self.repo.list(User) if u.active and u.role == "admin"]

    def _keep_an_admin(self, user: User, becomes_admin: bool) -> None:
        if not becomes_admin and [a.id for a in self._active_admins()] == [user.id]:
            raise ConflictError("This is the last active administrator; create or promote another one first.")

    # -- users -------------------------------------------------------------------

    def list_users(self) -> List[User]:
        return sorted(self.repo.list(User), key=lambda u: u.username.lower())

    def get_user(self, user_id: str) -> User:
        return self._require(User, user_id, "User")

    def create_user(self, username: str, role: str = "viewer", password: Optional[str] = None,
                    display_name: Optional[str] = None, email: Optional[str] = None,
                    auth_source: str = "local", external_id: Optional[str] = None) -> User:
        user = User(username=(username or "").strip(), role=role, display_name=display_name, email=email,
                    auth_source=auth_source, external_id=external_id)
        self._check_user(user)
        if auth_source == "local":
            self._check_password_strength(password or "")
            user.password_hash = hash_password(password)
        self._save(user, "CREATE", details=user.username)
        return user

    def update_user(self, user_id: str, **changes: Any) -> User:
        allowed = {"role", "display_name", "email", "active"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"Cannot change {', '.join(sorted(unknown))} here.")
        user = self.get_user(user_id)
        before = User.from_dict(user.to_dict())
        for k, v in changes.items():
            setattr(user, k, v)
        self._check_user(user)
        if before.role == "admin" and before.active:
            self._keep_an_admin(user, user.role == "admin" and user.active)
        self._save(user, "UPDATE", before, details=user.username)
        if not user.active:
            self._revoke_all(user.id)
        return user

    def set_password(self, user_id: str, password: str) -> None:
        user = self.get_user(user_id)
        if user.auth_source != "local":
            raise ValidationError("This user signs in with single sign-on and has no RiskMesh password.")
        self._check_password_strength(password)
        before = User.from_dict(user.to_dict())
        user.password_hash = hash_password(password)
        self._save(user, "UPDATE", before, details=f"{user.username}: password changed")
        self._revoke_all(user.id, keep_api=True)  # log out existing sessions

    def delete_user(self, user_id: str) -> None:
        user = self.get_user(user_id)
        if user.role == "admin" and user.active:
            self._keep_an_admin(user, False)
        for t in self.repo.list(ApiToken, user_id=user_id):
            self.repo.delete(ApiToken, t.id)
        self._remove(user, details=user.username)

    def upsert_external_user(self, external_id: str, username: str, role: str,
                             display_name: Optional[str] = None, email: Optional[str] = None) -> User:
        """Single sign-on: create the user on first login, keep name/e-mail/role in sync afterwards."""
        existing = self.repo.list(User, external_id=external_id)
        if existing:
            user = existing[0]
            if not user.active:
                raise AuthenticationError("This user is deactivated in RiskMesh.")
            changes = {k: v for k, v in (("role", role), ("display_name", display_name), ("email", email))
                       if v is not None and getattr(user, k) != v}
            return self.update_user(user.id, **changes) if changes else user
        base = re.sub(r"[^A-Za-z0-9._@\-]", "", username or "") or "user"
        name, n = base, 1
        while self.repo.list(User, username=name):
            n += 1
            name = f"{base}{n}"
        return self.create_user(name, role, display_name=display_name, email=email, auth_source="oidc",
                                external_id=external_id)

    # -- login and tokens ----------------------------------------------------------

    def authenticate(self, username: str, password: str) -> User:
        """Checks a local username/password. The error never says which part was wrong."""
        users = self.repo.list(User, username=(username or "").strip())
        user = users[0] if users else None
        ok = check_password(password or "", user.password_hash if user else None)
        if not (ok and user and user.active and user.auth_source == "local"):
            raise AuthenticationError("Wrong username or password.")
        return user

    def issue_token(self, user_id: str, token_type: str = "session", name: Optional[str] = None,
                    hours: Optional[float] = None) -> Tuple[str, ApiToken]:
        """Returns (token, record). The token itself is not stored and cannot be shown again."""
        user = self.get_user(user_id)
        if not user.active:
            raise AuthenticationError("This user is deactivated.")
        if token_type not in ("session", "api"):
            raise ValidationError("token_type must be 'session' or 'api'.")
        if hours is None and token_type == "session":
            hours = SESSION_HOURS
        token = "rm_" + secrets.token_urlsafe(32)
        record = ApiToken(user_id=user.id, token_hash=token_hash(token), token_type=token_type,
                          name=name or token_type,
                          expires_at=(_now() + timedelta(hours=hours)).isoformat() if hours else None)
        record.created_at = record.updated_at = _now().isoformat(timespec="seconds")
        self.repo.save(record)
        if token_type == "api":  # sessions (logins) are not audited one by one; API keys are
            self._audit("TOKEN", record.kind, record.id, f"API token '{record.name}' for {user.username}")
        return token, record

    def resolve_token(self, token: Optional[str]) -> Optional[User]:
        """The active user behind a token, or None."""
        if not token:
            return None
        found = self.repo.list(ApiToken, token_hash=token_hash(token))
        if not found:
            return None
        record = found[0]
        if record.revoked or (record.expires_at and datetime.fromisoformat(record.expires_at) < _now()):
            return None
        user = self.repo.get(User, record.user_id)
        if user is None or not user.active:
            return None
        last = datetime.fromisoformat(record.last_used_at) if record.last_used_at else None
        if last is None or _now() - last > timedelta(minutes=5):  # keep writes rare
            record.last_used_at = _now().isoformat(timespec="seconds")
            self.repo.save(record)
        return user

    def list_tokens(self, user_id: Optional[str] = None) -> List[ApiToken]:
        tokens = self.repo.list(ApiToken, **({"user_id": user_id} if user_id else {}))
        return sorted(tokens, key=lambda t: t.created_at or "", reverse=True)

    def revoke_token(self, token_id: str) -> None:
        record = self._require(ApiToken, token_id, "Token")
        if not record.revoked:
            record.revoked = True
            self.repo.save(record)
            if record.token_type == "api":
                self._audit("TOKEN", record.kind, record.id, f"API token '{record.name}' revoked")

    def logout(self, token: str) -> None:
        for record in self.repo.list(ApiToken, token_hash=token_hash(token)):
            record.revoked = True
            self.repo.save(record)

    def _revoke_all(self, user_id: str, keep_api: bool = False) -> None:
        for record in self.repo.list(ApiToken, user_id=user_id):
            if not record.revoked and not (keep_api and record.token_type == "api"):
                record.revoked = True
                self.repo.save(record)

    def user_view(self, user: User) -> Dict[str, Any]:
        """A user as shown to clients (never the password hash)."""
        d = user.to_dict()
        d.pop("password_hash", None)
        d["has_password"] = bool(user.password_hash)
        return d

    @staticmethod
    def require_role(user: Optional[User], required: str) -> None:
        if user is None:
            raise AuthenticationError("Please log in.")
        if not role_at_least(user.role, required):
            raise PermissionDenied(f"This needs the role '{required}' (you are '{user.role}').")


USER_WRITE_METHODS = ("create_user", "update_user", "set_password", "delete_user", "upsert_external_user",
                      "issue_token", "revoke_token", "logout")

__all__ = ["USER_WRITE_METHODS", "UserAdmin", "check_password", "hash_password", "role_at_least"]
