"""
Single sign-on with OpenID Connect (Microsoft Entra ID, Google, Keycloak, ...).

Flow (authorization code + PKCE, handled by the API so the client secret
never reaches the browser):
  1. GET /api/auth/oidc/login     -> redirect to the provider (state, nonce, PKCE)
  2. provider -> GET /api/auth/oidc/callback?code=...&state=...
  3. the API exchanges the code, validates the ID token (signature via the
     provider's JWKS, issuer, audience, expiry, nonce), maps the role claim
     to a RiskMesh role, creates/updates the user and issues a RiskMesh
     session token
  4. redirect to <frontend_url>/login#token=... (a URL fragment is never sent
     to servers or written to their logs)

Entra ID: register an app, add the redirect URI, create app roles
RiskMesh.Admin / RiskMesh.Editor / RiskMesh.Viewer and assign them; the ID
token then carries them in the "roles" claim.
"""

import base64
import hashlib
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from ..config import Settings
from ..domain.errors import AuthenticationError

STATE_TTL = 600  # seconds a login may take


class OidcClient:

    def __init__(self, settings: Settings):
        self.s = settings
        self._discovery: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._pending: Dict[str, Tuple[float, str, str]] = {}  # state -> (created, nonce, pkce verifier)

    # -- HTTP (separate methods so tests can replace them) ---------------------

    def _http_get(self, url: str) -> Dict[str, Any]:
        import httpx
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        return r.json()

    def _http_post(self, url: str, data: Dict[str, str]) -> Dict[str, Any]:
        import httpx
        r = httpx.post(url, data=data, timeout=10, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise AuthenticationError(f"The identity provider refused the login: {r.text[:300]}")
        return r.json()

    # -- provider metadata ----------------------------------------------------------

    def discovery(self) -> Dict[str, Any]:
        if self._discovery is None:
            url = self.s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            self._discovery = self._http_get(url)
        return self._discovery

    def _signing_key(self, kid: Optional[str]):
        import jwt
        for attempt in range(2):  # refresh once: the provider may have rotated its keys
            if self._jwks is None or attempt:
                self._jwks = self._http_get(self.discovery()["jwks_uri"])
            for key in self._jwks.get("keys", []):
                if kid is None or key.get("kid") == kid:
                    return jwt.PyJWK(key).key
        raise AuthenticationError("The ID token was signed with an unknown key.")

    # -- flow ---------------------------------------------------------------------------

    def login_url(self) -> str:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v[0] < STATE_TTL}
        state, nonce, verifier = (secrets.token_urlsafe(24) for _ in range(3))
        self._pending[state] = (now, nonce, verifier)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "response_type": "code", "client_id": self.s.oidc_client_id, "redirect_uri": self.s.oidc_redirect_uri,
            "scope": " ".join(self.s.oidc_scopes), "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        return self.discovery()["authorization_endpoint"] + "?" + urlencode(params)

    def complete(self, code: str, state: str) -> Dict[str, Any]:
        """Validates the callback and returns the verified ID-token claims."""
        import jwt
        pending = self._pending.pop(state or "", None)
        if not pending or time.time() - pending[0] > STATE_TTL:
            raise AuthenticationError("The login expired or was not started here. Please try again.")
        _, nonce, verifier = pending
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.s.oidc_redirect_uri,
                "client_id": self.s.oidc_client_id, "code_verifier": verifier}
        if self.s.oidc_client_secret:
            data["client_secret"] = self.s.oidc_client_secret
        tokens = self._http_post(self.discovery()["token_endpoint"], data)
        id_token = tokens.get("id_token")
        if not id_token:
            raise AuthenticationError("The identity provider did not return an ID token.")
        try:
            header = jwt.get_unverified_header(id_token)
            claims = jwt.decode(id_token, self._signing_key(header.get("kid")),
                                algorithms=["RS256", "RS384", "RS512", "ES256", "PS256"],
                                audience=self.s.oidc_client_id, issuer=self.discovery()["issuer"],
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as e:
            raise AuthenticationError(f"The ID token is not valid: {e}") from e
        if claims.get("nonce") != nonce:
            raise AuthenticationError("The ID token does not belong to this login (nonce).")
        return claims

    def role_for(self, claims: Dict[str, Any]) -> str:
        """Highest RiskMesh role among the mapped claim values, else the default role."""
        values = claims.get(self.s.oidc_role_claim) or []
        if isinstance(values, str):
            values = [values]
        order = ("viewer", "editor", "admin")
        roles = [self.s.oidc_role_map[v] for v in values if v in self.s.oidc_role_map]
        if roles:
            return max(roles, key=order.index)
        if self.s.oidc_default_role:
            return self.s.oidc_default_role
        raise AuthenticationError("Your account has no RiskMesh role. Ask an administrator for access.")

    def identity(self, claims: Dict[str, Any]) -> Dict[str, Any]:
        username = (claims.get(self.s.oidc_username_claim) or claims.get("email") or claims["sub"])
        return {"external_id": f"{claims['iss']}|{claims['sub']}", "username": str(username),
                "display_name": claims.get("name"), "email": claims.get("email"), "role": self.role_for(claims)}
