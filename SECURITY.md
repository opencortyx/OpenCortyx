# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately through
[GitHub security advisories](https://github.com/opencortyx/OpenCortyx/security/advisories/new).

Include a description, steps to reproduce, and the affected version or commit. You will receive an
acknowledgement within a few days, and we will coordinate a fix and disclosure with you.

## Supported versions

Security fixes are made on the latest release.

## Deployment notes

- `bin/opencortyx init` generates unique secrets. Never reuse the values from another installation.
- Services bind to `127.0.0.1` by default. To expose OpenCortyx on a network, put it behind a
  TLS-terminating reverse proxy and set `OPENCORTYX_BIND`, `PUBLIC_API_URL` and `PUBLIC_WEB_URL`.
- The MCP server requires an API key for every client unless `MCP_ALLOW_ANONYMOUS=true`; only set
  that on an isolated machine.
- The MCP server only accepts the Host headers in `MCP_ALLOWED_HOSTS` (default: localhost and the
  `mcp` service name). Add your proxy's hostname, for example `mcp.example.com:*`, when exposing it.
- API keys can be revoked in the dashboard under Users & Access.

## Dependency audits

CI runs `npm audit` (web), `pip-audit` (API and MCP) on every pull request. Advisories that are
reviewed and do not apply are listed here with the reason:

| Package | Advisory | Why it does not apply |
|---|---|---|
| `ecdsa` (via `python-jose`) | PYSEC-2026-1325, timing side channel in ECDSA signing; no fixed release | OpenCortyx signs tokens with HS256 only; no ECDSA keys are used |
| `postcss` (bundled inside Next.js) | GHSA-qx2v-qp2m-jg93, GHSA-6g55-p6wh-862q, GHSA-r28c-9q8g-f849, GHSA-fxqj-rqcc-2cmp | Runs only at build time on the project's own CSS; no untrusted CSS is processed |
