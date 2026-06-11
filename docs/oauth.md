# OAuth 2.0 for the MCP server

This server speaks the **MCP Authorization** spec: the MCP HTTP transport is an
OAuth 2.0 **Protected Resource** (RFC 9728). Interactive MCP clients (Claude
Desktop, ChatGPT, MCP Inspector, VS Code) discover an external authorization
server, run the **authorization-code + PKCE** flow themselves, and present the
resulting access token as a Bearer JWT — no operator-minted tokens.

## What the server does (and does not) do

The server is **only the resource server**. It does *not* log users in or issue
tokens — an external IdP does that. Concretely, this repo adds:

| Piece | Where | Purpose |
|---|---|---|
| `GET /.well-known/oauth-protected-resource` (+ `…/mcp`) | [app/mcp_server.py](../app/mcp_server.py) | RFC 9728 Protected Resource Metadata: advertises the authorization server(s) and the resource identifier. **Public** — served without a token. |
| `WWW-Authenticate: Bearer resource_metadata="…"` on 401/403 | `JWTAuthMiddleware` in [app/mcp_server.py](../app/mcp_server.py) | RFC 9728 §5.1 — points an unauthenticated client at the metadata so it can start the flow. |
| JWT validation | [app/auth_jwt.py](../app/auth_jwt.py) | **Unchanged.** Verifies signature against the IdP JWKS, checks `iss`/`aud`/`exp`/`nbf`, pins asymmetric algs, and fails closed if the tenant claim is absent. |

The client-side OAuth dance (discovery → optional dynamic registration → PKCE →
token) is implemented by the MCP client itself (the Python SDK ships
`mcp.client.auth.OAuthClientProvider`).

## End-to-end flow

```
1. Client → MCP server:  GET /mcp        (no token)
2. MCP server → Client:  401 + WWW-Authenticate: Bearer resource_metadata="https://host/.well-known/oauth-protected-resource/mcp"
3. Client → MCP server:  GET /.well-known/oauth-protected-resource/mcp
   → { "resource": "...", "authorization_servers": ["https://idp/..."] }
4. Client → IdP:         GET /.well-known/openid-configuration   (discover /authorize, /token, jwks)
5. Client ↔ IdP:         authorization-code + PKCE  → access token (aud = this resource)
6. Client → MCP server:  GET /mcp  with  Authorization: Bearer <token>
7. MCP server:           validate_token() → bind tenant claim → run tool
```

## Configuration

All IdP-agnostic. Set these in addition to the existing `OIDC_*` values:

| Env var | Meaning | Default |
|---|---|---|
| `PUBLIC_BASE_URL` | Real public HTTPS URL of the MCP server. Used to build the metadata URL. | — (set it) |
| `OAUTH_AUTHORIZATION_SERVERS` | Comma-separated AS issuer URL(s) advertised in PRM. | falls back to `OIDC_ISSUER` |
| `OAUTH_RESOURCE` | PRM `resource` value; **must equal the token `aud`** (`OIDC_AUDIENCE`). | `PUBLIC_BASE_URL` + `MCP_PATH` |
| `OAUTH_SCOPES_SUPPORTED` | Optional scopes advertised in PRM. | — |

> **Audience binding.** The `resource` you advertise and the `aud` your IdP
> stamps on the token must match, because validation checks `aud == OIDC_AUDIENCE`.
> Set `OAUTH_RESOURCE` to the same value as `OIDC_AUDIENCE`.

---

## Worked example: Azure Entra ID

Entra ID works as the authorization server, with **two caveats** — read these
first:

> ⚠️ **No Dynamic Client Registration.** Entra ID does **not** implement RFC 7591.
> MCP clients that *require* DCR can't auto-register. You must **pre-register the
> client** app and give it a static `client_id`. Verify your target MCP client
> accepts a configured `client_id` (Claude/ChatGPT connectors and MCP Inspector
> do; some clients are DCR-only and won't work with Entra).

> ⚠️ **Use the v2.0 endpoint and scope-based audience.** Entra v2 drives the token
> `aud` from the requested **scope** (`api://<api-client-id>/<scope>`), not the
> RFC 8707 `resource` parameter. Set the API app's manifest
> `requestedAccessTokenVersion: 2` so you get a JWT whose `aud` is your API.

### 1. Register the API (the resource server)

1. **Entra ID → App registrations → New registration** → name e.g. `clickhouse-mcp-api`.
2. **Expose an API → Application ID URI** → accept `api://<api-client-id>` (this becomes the token `aud`).
3. **Expose an API → Add a scope** → e.g. `Query.Read` (admin/user consent).
4. **Manifest** → set `"requestedAccessTokenVersion": 2`.
5. *(For the `user_name` tenant claim)* **Token configuration → Add optional claim** (type: Access) → add e.g. `preferred_username` / `upn`, **or** map a custom claim. Alternatively, skip this and use a native claim as the tenant key (see step 4 below).

### 2. Register the client (what the MCP client uses)

1. **New registration** → name e.g. `clickhouse-mcp-client`, type **public client**.
2. **Authentication → Add platform** → add the redirect URI your MCP client uses (e.g. MCP Inspector / Claude Desktop loopback `http://localhost:<port>/callback`). Enable PKCE (public clients use it by default).
3. **API permissions → Add a permission → My APIs →** `clickhouse-mcp-api` → delegated → `Query.Read` → grant consent.
4. Note the client's **Application (client) ID** — this is the static `client_id` you give the MCP client.

### 3. Point the MCP server at Entra

```bash
# OIDC validation (existing vars)
OIDC_JWKS_URL=https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OIDC_AUDIENCE=api://<api-client-id>          # the API's Application ID URI
JWT_ALGORITHMS=RS256                          # Entra signs with RS256

# OAuth discovery (new vars)
PUBLIC_BASE_URL=https://mcp.your-host.example.com
OAUTH_AUTHORIZATION_SERVERS=https://login.microsoftonline.com/<tenant-id>/v2.0
OAUTH_RESOURCE=api://<api-client-id>          # MUST equal OIDC_AUDIENCE
OAUTH_SCOPES_SUPPORTED=Query.Read
```

#### How to obtain each value

You only need three things from Entra: the **OpenID URL**, the **Application
(client) ID**, and (for the client side) the **client secret**.

**1. Get `<tenant-id>` from the OpenID URL.** Your discovery URL looks like:

```
https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration
                                   └──────────┘
                                   this is <tenant-id>
```

**2. Derive `OIDC_ISSUER` and `OIDC_JWKS_URL` from the discovery document.** Don't
hand-build them — fetch the doc and copy the authoritative fields (`curl`, or just
open the URL in a browser):

```bash
curl -s "https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration" \
  | jq '{issuer, jwks_uri, authorization_endpoint, token_endpoint}'
```

Map the output:

| Discovery field | Env var |
|---|---|
| `issuer` | `OIDC_ISSUER` **and** `OAUTH_AUTHORIZATION_SERVERS` |
| `jwks_uri` | `OIDC_JWKS_URL` |
| `authorization_endpoint` / `token_endpoint` | used by the MCP *client*, not this server |

**3. Derive the audience from the Application (client) ID.** The client ID is the
GUID on the app registration's **Overview** page. The API's audience is its App ID
URI:

```
OIDC_AUDIENCE = OAUTH_RESOURCE = api://<application-client-id>
```

⚠️ Confirm the real `aud` before trusting this — see the jwt.ms note below; it is
either `api://<client-id>` or the bare GUID depending on the token version.

**4. The client secret is NOT used by this server.** It belongs to a confidential
client or the `client_credentials` flow. For interactive PKCE (public client) you
usually don't need it at all. Never put it in the MCP server's env or in this repo.

### 4. Wire the tenant claim

This app fails closed unless every claim in `CLICKHOUSE_TENANT_SETTINGS` is
present on the token (default maps `SQL_tenant → user_name`). Entra tokens have
no `user_name` claim by default, so do **one** of:

- **Simplest — use a native Entra claim as the tenant key** (no IdP claim config):
  ```bash
  # oid = the user's stable Entra object id; great as a tenant key
  CLICKHOUSE_TENANT_SETTINGS={"SQL_tenant": "oid"}
  ```
  (Other options: `preferred_username`, `upn`. Prefer the stable `oid` for isolation.)
- **Or** configure an optional/custom claim literally named `user_name` (step 1.5
  above) and keep the default mapping.

> Whichever claim you choose **is your tenant boundary** — a token carrying it
> equals access to that tenant's rows via the ClickHouse row policy. Pick a stable,
> non-spoofable claim (`oid` is ideal) and make sure the IdP populates it for every
> user.

### 5. Verify

```bash
# PRM is public — should return resource + authorization_servers:
curl -s https://mcp.your-host.example.com/.well-known/oauth-protected-resource/mcp | jq

# Unauthenticated request advertises the metadata URL:
curl -si https://mcp.your-host.example.com/mcp | grep -i www-authenticate
```

Then connect with MCP Inspector (`npx @modelcontextprotocol/inspector`), choose
OAuth, and supply the client `client_id` from step 2.

### Filled-in values for this deployment

Worked mapping for our Entra registration. Tenant ID and client ID are
identifiers (not secret); the **client secret is intentionally NOT recorded here**
— the MCP server (a resource server) never uses it, and secrets must never be
committed. Rotate any secret that has been shared in chat/email.

| Source value | Value | Used as |
|---|---|---|
| Tenant ID (from the OpenID URL) | `a7628d74-52e2-4cf6-9aed-f7ae60fac663` | builds issuer / JWKS URLs |
| Application (client) ID | `d1973b1a-3f6b-4fc8-bab8-dd1a10e2ae57` | audience → `api://<client-id>` (and the client's `client_id`) |
| Client secret | *(omitted — server does not use it)* | only a confidential client / `client_credentials` flow |

```bash
# --- MCP server .env (derived from the tenant + client ID above) ---
OIDC_ISSUER=https://login.microsoftonline.com/a7628d74-52e2-4cf6-9aed-f7ae60fac663/v2.0
OIDC_JWKS_URL=https://login.microsoftonline.com/a7628d74-52e2-4cf6-9aed-f7ae60fac663/discovery/v2.0/keys
OAUTH_AUTHORIZATION_SERVERS=https://login.microsoftonline.com/a7628d74-52e2-4cf6-9aed-f7ae60fac663/v2.0
JWT_ALGORITHMS=RS256

OIDC_AUDIENCE=api://d1973b1a-3f6b-4fc8-bab8-dd1a10e2ae57
OAUTH_RESOURCE=api://d1973b1a-3f6b-4fc8-bab8-dd1a10e2ae57

PUBLIC_BASE_URL=https://mcp.your-host.example.com   # set to the real public MCP URL
CLICKHOUSE_TENANT_SETTINGS={"SQL_tenant": "oid"}    # Entra has no user_name claim
```

> **Verify the audience before trusting the above.** The `aud` Entra actually
> stamps depends on the API app's `requestedAccessTokenVersion` and whether an App
> ID URI is set. Mint a token, decode it at <https://jwt.ms>, and set
> `OIDC_AUDIENCE` / `OAUTH_RESOURCE` to the **exact** `aud` you see (it is either
> `api://d1973b1a-…` or the bare GUID `d1973b1a-…`). A mismatch → every request 401s.
> Set the API app manifest `requestedAccessTokenVersion: 2` for a v2 access token.

> **Single registration note.** These values reuse one app registration as both
> the API (audience) and the client. That is fine for testing; for production
> prefer a separate public client app (with PKCE + redirect URIs) so the resource
> and the client have distinct identities.

---

## Alternative: Keycloak (self-hosted, full DCR)

If the no-DCR / custom-claim friction of Entra is a problem, Keycloak supports
RFC 7591 dynamic client registration *and* a one-click protocol mapper for the
`user_name` claim:

```bash
OIDC_JWKS_URL=https://kc.your-host/realms/<realm>/protocol/openid-connect/certs
OIDC_ISSUER=https://kc.your-host/realms/<realm>
OIDC_AUDIENCE=clickhouse-mcp
OAUTH_AUTHORIZATION_SERVERS=https://kc.your-host/realms/<realm>
OAUTH_RESOURCE=clickhouse-mcp
```

- **Client scope → Mappers → User Property/Attribute → `user_name`** emits the tenant claim directly.
- **Audience mapper** stamps `aud=clickhouse-mcp` so it matches `OIDC_AUDIENCE`.
- Enable the realm's client-registration policy for DCR if your clients use it.

## Security notes

- The MCP server is the only internet-exposed component, so **do not** build your
  own authorization server here — use a hardened IdP. The PRM endpoint and
  challenge header are the only additions; the validation boundary in
  `app/auth_jwt.py` is unchanged and still the single authorization gate.
- The PRM endpoints are intentionally public (a client needs them *before* it has
  a token) and expose only non-secret discovery data.
- `stdio` transport is unaffected — it uses the local-subprocess trust model and
  performs no auth.
