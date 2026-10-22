"""Who is asking: the identity IAP signed, re-checked against the domain and allowlist.

IAP already refuses anyone without roles/iap.httpsResourceAccessor. This is
the second lock: if IAP were switched off or misconfigured, a request without
a valid IAP assertion for this service, from an allowed address in the
configured domain, still gets 403. The unsigned X-Goog-Authenticated-User-Email header
is never used.
"""

from __future__ import annotations

from dataclasses import dataclass

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_HEADER = "X-Goog-IAP-JWT-Assertion"


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class Editor:
    email: str


def verify_iap_jwt(assertion: str, audience: str) -> dict:
    """Signature, expiry and audience, against Google's IAP keys."""
    import google.auth.transport.requests
    from google.oauth2 import id_token

    return id_token.verify_token(
        assertion, google.auth.transport.requests.Request(), audience=audience,
        certs_url=IAP_CERTS_URL,
    )


class Gate:
    def __init__(self, audience: str, domain: str, allowed: frozenset[str], verify=verify_iap_jwt):
        if not audience or not domain or not allowed:
            raise ValueError("IAP_AUDIENCE, ALLOWED_DOMAIN and ALLOWED_EDITORS must all be set")
        self.audience = audience
        self.domain = domain.lower()
        self.allowed = frozenset(e.lower() for e in allowed)
        self._verify = verify

    def check(self, assertion: str | None) -> Editor:
        if not assertion:
            raise Forbidden("no IAP assertion")
        try:
            claims = self._verify(assertion, self.audience)
        except Exception as exc:
            raise Forbidden(f"bad IAP assertion: {exc}") from exc
        if claims.get("iss") != IAP_ISSUER:
            raise Forbidden(f"issuer {claims.get('iss')!r}")
        email = str(claims.get("email", "")).lower()
        if claims.get("hd", "").lower() != self.domain or not email.endswith("@" + self.domain):
            raise Forbidden(f"{email or 'unknown'} is not in {self.domain}")
        if email not in self.allowed:
            raise Forbidden(f"{email} is not an allowed editor")
        return Editor(email=email)
