"""Diagnose why an Entra/OIDC access token is (or would be) rejected.

This is a read-only debugging aid for the "ChatGPT can't connect / token
rejected" class of problems. It does two things:

  1. **Offline claim inspection** — decodes the token WITHOUT verifying the
     signature and prints the claims that decide acceptance (``iss``, ``aud``,
     ``exp``, ``ver``, ``appid``, ``tid`` and the configured tenant claim),
     comparing ``iss``/``aud`` against the configured ``OIDC_ISSUER`` /
     ``OIDC_AUDIENCE``. This needs no network and is the fastest way to spot the
     classic v1/v2 issuer split or an App-ID-URI / audience domain mismatch
     (e.g. ``paycomhq.com`` vs ``paycomonline.com``).

  2. **Real validation** — runs the token through this app's actual
     :func:`app.auth_jwt.validate_token` against the current environment, so you
     see the exact :class:`app.auth_jwt.JWTAuthError` code the MCP server would
     return (TOKEN_EXPIRED / INVALID_AUDIENCE / INVALID_ISSUER / INVALID_AUTH /
     JWKS_UNAVAILABLE / MISSING_TENANT_CLAIM). This step fetches the IdP JWKS, so
     it needs outbound network to ``OIDC_JWKS_URL``.

Usage (set the same OIDC_*/CLICKHOUSE_TENANT_SETTINGS env the server uses):

    # token as an argument
    python -m tools.diagnose_token "$ACCESS_TOKEN"

    # or piped in (avoids the token landing in shell history)
    pbpaste | python -m tools.diagnose_token
    echo "$ACCESS_TOKEN" | python -m tools.diagnose_token

The token is never logged or sent anywhere except the configured JWKS host
during step 2 (and only its signature is checked there).
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from typing import Any

import jwt


def _read_token() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data
    sys.exit("No token provided. Pass it as an argument or pipe it on stdin.")


def _fmt_exp(exp: Any) -> str:
    try:
        when = _dt.datetime.fromtimestamp(int(exp), tz=_dt.timezone.utc)
    except (TypeError, ValueError):
        return f"{exp!r} (unparseable)"
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    delta = when - now
    state = "EXPIRED" if delta.total_seconds() < 0 else "valid"
    mins = abs(delta.total_seconds()) / 60
    rel = f"{mins:.1f} min {'ago' if delta.total_seconds() < 0 else 'from now'}"
    return f"{when.isoformat()}  [{state}, {rel}]"


def _aud_list(aud: Any) -> list[str]:
    if aud is None:
        return []
    return list(aud) if isinstance(aud, (list, tuple)) else [str(aud)]


def main() -> None:
    token = _read_token()

    # --- Step 1: offline claim inspection (no signature, no network) ---
    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        sys.exit(f"Not a decodable JWT: {exc}")

    cfg_iss = os.environ.get("OIDC_ISSUER", "(unset)")
    cfg_aud = os.environ.get("OIDC_AUDIENCE", "(unset)")
    cfg_jwks = os.environ.get("OIDC_JWKS_URL", "(unset)")
    tenant_map_raw = os.environ.get("CLICKHOUSE_TENANT_SETTINGS", "(unset)")

    tok_iss = claims.get("iss")
    tok_aud = _aud_list(claims.get("aud"))
    tok_ver = claims.get("ver")

    print("=" * 72)
    print("TOKEN HEADER")
    print(f"  alg = {header.get('alg')!r}   kid = {header.get('kid')!r}")
    print("\nKEY CLAIMS")
    print(f"  iss   = {tok_iss!r}")
    print(f"  aud   = {claims.get('aud')!r}")
    print(f"  exp   = {_fmt_exp(claims.get('exp'))}")
    print(f"  ver   = {tok_ver!r}   (1.0 = v1 token, 2.0 = v2 token)")
    print(f"  appid = {claims.get('appid')!r}   azp = {claims.get('azp')!r}")
    print(f"  tid   = {claims.get('tid')!r}")
    print(f"  scp   = {claims.get('scp')!r}   roles = {claims.get('roles')!r}")
    print(f"  oid   = {claims.get('oid')!r}   sub = {claims.get('sub')!r}")

    print("\nCONFIGURED (current environment)")
    print(f"  OIDC_ISSUER   = {cfg_iss!r}")
    print(f"  OIDC_AUDIENCE = {cfg_aud!r}")
    print(f"  OIDC_JWKS_URL = {cfg_jwks!r}")
    print(f"  CLICKHOUSE_TENANT_SETTINGS = {tenant_map_raw}")

    print("\nCOMPARISON")
    iss_ok = tok_iss == cfg_iss
    aud_ok = cfg_aud in tok_aud
    print(f"  iss match : {'OK' if iss_ok else 'MISMATCH'}")
    if not iss_ok:
        print(f"      token iss  : {tok_iss!r}")
        print(f"      configured : {cfg_iss!r}")
        if tok_ver == "1.0" and "/v2.0" in str(cfg_iss):
            print("      -> token is v1 but OIDC_ISSUER is v2. Use the sts.windows.net form.")
        if tok_ver == "2.0" and "sts.windows.net" in str(cfg_iss):
            print("      -> token is v2 but OIDC_ISSUER is v1. Use the .../v2.0 form.")
    print(f"  aud match : {'OK' if aud_ok else 'MISMATCH'}")
    if not aud_ok:
        print(f"      token aud  : {claims.get('aud')!r}")
        print(f"      configured : {cfg_aud!r}")
        print("      -> OIDC_AUDIENCE must equal the App ID URI Entra stamps as aud, exactly.")

    # --- Step 2: real validation through the app (needs network to JWKS) ---
    print("\n" + "=" * 72)
    print("REAL VALIDATION via app.auth_jwt.validate_token (fetches JWKS)")
    try:
        from app.auth_jwt import JWTAuthError, validate_token
    except Exception as exc:  # noqa: BLE001
        print(f"  could not import app.auth_jwt ({exc}); skipping.")
        return

    try:
        principal = validate_token(token)
    except Exception as exc:  # noqa: BLE001 -- report any failure, don't crash
        code = getattr(exc, "code", type(exc).__name__)
        status = getattr(exc, "status_code", "?")
        print(f"  REJECTED -> status={status} code={code}: {exc}")
        if isinstance(exc, JWTAuthError) and code == "JWKS_UNAVAILABLE":
            print("  (JWKS fetch failed — check OIDC_JWKS_URL reachability / the kid.)")
        return

    print("  ACCEPTED")
    print(f"    subject = {principal.subject!r}")


if __name__ == "__main__":
    main()
