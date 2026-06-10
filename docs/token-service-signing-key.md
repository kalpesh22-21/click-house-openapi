# Token Service — Creating the Signing Key

The [token service](../app/token_service.py) signs the JWTs it issues with an RSA
private key and publishes the matching public key as JWKS. The ClickHouse API
fetches that JWKS to verify tokens — so the **token service is the only
component that ever holds the private key**.

This guide covers generating the key and wiring it into each deployment context.

---

## What kind of key

- **Algorithm:** RSA (the default `TOKEN_ALGORITHM=RS256`). The loader rejects
  non-RSA keys.
- **Format:** unencrypted **PKCS#8 PEM** — the file starts with
  `-----BEGIN PRIVATE KEY-----`.
- **Size:** 2048-bit minimum (4096 if you prefer).

You do **not** extract the public key or choose a `kid` — the service derives
the JWKS and a stable `kid` (RFC 7638 thumbprint) from the private key
automatically, so every replica computes the same `kid`.

---

## Generate the key

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out signing-key.pem
chmod 600 signing-key.pem
```

> Do **not** pass `-pkcs8` (that flag is invalid in some OpenSSL/LibreSSL builds;
> `genpkey` already emits PKCS#8).

Also generate the **issuer API key** that protects `POST /token` (minting is a
privileged operation, so the endpoint requires it):

```bash
openssl rand -hex 32   # value for TOKEN_ISSUER_API_KEY
```

Verify the key is valid RSA PKCS#8:

```bash
openssl pkey -in signing-key.pem -noout -text | head -1
# -> Private-Key: (2048 bit, 2 primes)
```

---

## Wire it into the service

The service reads exactly one of `TOKEN_SIGNING_KEY` (inline PEM) or
`TOKEN_SIGNING_KEY_FILE` (path). A file is preferred for the multi-line PEM.

### Local (process / uvicorn)

```bash
export TOKEN_SIGNING_KEY_FILE=$PWD/signing-key.pem
export TOKEN_ISSUER_API_KEY=$(openssl rand -hex 32)
export TOKEN_ISSUER=https://token.example.com/
export TOKEN_AUDIENCE=clickhouse-api
uvicorn app.token_service:create_app --factory --host 0.0.0.0 --port 8000
```

Inline alternative (note the multi-line value):

```bash
export TOKEN_SIGNING_KEY="$(cat signing-key.pem)"
```

### Local docker-compose — no key needed

`docker-compose.yml` runs the token service with `TOKEN_DEV_MODE=true`, which
**generates an ephemeral key at startup**. Fine for a single-replica local
stack; never use it in production (each pod would publish a different key and
tokens would not survive a restart).

### Helm ([helm/token-service](../helm/token-service))

```bash
helm install token-service ./helm/token-service \
  --set-file secrets.signingKey=signing-key.pem \
  --set secrets.issuerApiKey=$(openssl rand -hex 32) \
  --set config.TOKEN_ISSUER=https://token.example.com/ \
  --set config.TOKEN_AUDIENCE=clickhouse-api
```

The chart stores the PEM in a Secret and mounts it at
`/etc/token-service/signing-key.pem` (the container sets
`TOKEN_SIGNING_KEY_FILE` to that path).

### Pre-existing Kubernetes Secret

If you manage secrets out-of-band (Vault, External Secrets, sealed-secrets),
create a Secret with the two canonical keys and point the chart at it:

```bash
kubectl create secret generic token-service \
  --from-file=signing-key.pem=signing-key.pem \
  --from-literal=TOKEN_ISSUER_API_KEY=$(openssl rand -hex 32)

helm install token-service ./helm/token-service \
  --set secrets.existingSecret.name=token-service \
  --set config.TOKEN_ISSUER=https://token.example.com/ \
  --set config.TOKEN_AUDIENCE=clickhouse-api
```

The Secret MUST contain both keys: `signing-key.pem` and `TOKEN_ISSUER_API_KEY`.

---

## Keep the API and token service in agreement

The tokens carry `iss` / `aud` from `TOKEN_ISSUER` / `TOKEN_AUDIENCE`. These must
exactly match the ClickHouse API's `OIDC_ISSUER` / `OIDC_AUDIENCE`, and the API's
`OIDC_JWKS_URL` must point at this service's `/.well-known/jwks.json`. (Mismatched
`iss`/`aud` → the API rejects the token with `INVALID_ISSUER` / `INVALID_AUDIENCE`.)

---

## Security & rotation

- **Treat the PEM as a secret.** `chmod 600`, never commit it, keep it out of
  logs and images. The ClickHouse API never needs it — it only fetches the
  public JWKS.
- **Same key across all replicas.** Loading from a Secret (rather than generating
  per pod) is what keeps the JWKS consistent so any replica can mint a token any
  other replica's public key can verify.
- **Rotation.** Swapping the Secret rotates the key. Because the service
  publishes a **single** key in JWKS, tokens signed by the old key stop
  validating the moment the new key is live — rotate during a low-traffic window,
  or rely on short token TTLs (`TOKEN_TTL_SECONDS`) so old tokens expire quickly.
  Overlap-free rotation (publishing old + new keys in JWKS simultaneously) is not
  supported today and would require a code change.

---

## Reference

- Config fields: `TOKEN_SIGNING_KEY`, `TOKEN_SIGNING_KEY_FILE`, `TOKEN_KID`,
  `TOKEN_ISSUER_API_KEY`, `TOKEN_DEV_MODE` — see [.env.example](../.env.example)
  and [app/token_service.py](../app/token_service.py).
- Architecture decision: [ADR-0001](adr/0001-per-tenant-jwt-row-isolation.md).
