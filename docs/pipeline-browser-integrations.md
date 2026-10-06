# Browser Integration Sign-In

Managed Forgejo and Zulip clients can reuse a human Horizon dashboard login.
Connectors and workers continue using their own scoped API credentials. Browser
identity mappings are explicit installation configuration, never inferred from
agent profiles, display names, or a shared maintainer account.

Configure `integration_browser_auth` by integration UUID. Each entry has `mode`
(`forgejo_proxy` or `zulip_jwt`) and `identities`, a map from human principal UUID
to the existing Forgejo username or Zulip email. Forgejo's optional
`session_cookie_name` must match its configured session cookie (default
`i_like_gitea`). Zulip requires `credential_ref`, pointing to a private
`secrets/<name>.json` with the `jwt_auth_key` configured in Zulip.

Set `integration_browser_binding_credential_ref` to a separate private secret
containing `signing_key` with at least 32 cryptographically random bytes of
entropy. Secret files must be owned by the Horizon user and mode 0600. Browser
sign-in requires HTTPS, secure cookies, and explicit integration public URLs on
the same hostname as Horizon; separate ports are supported. Installations without
these settings retain each provider's normal sign-in.

`deploy/pipeline/browser-integrations.Caddyfile` provides a loopback-only proxy
template. Its environment variables specify the public hostname/origin, Horizon
API, integration UUIDs, and fixed native upstreams. TLS terminates at the public
ingress. Keep native services private. Enable Forgejo reverse-proxy authentication
with automatic registration disabled and trust only the local proxy. Configure
Zulip's existing JWT authentication backend and signing key.

The dashboard establishes native sessions server-side before opening its frames.
Horizon forwards no browser-supplied identity or provider credentials. A signed,
HttpOnly cookie binds the native session to the current Horizon credential and
mapped identity. Caddy checks that binding and current access before forwarding
browser requests, strips Horizon cookies upstream, and overwrites Forgejo's
trusted user header. Provider session rotation or a new Horizon login requires
reconnecting the integration. Logout blocks subsequent browser requests; an
already authorized streaming request may finish.

Native Git/API clients without Horizon browser cookies can continue using their
provider Authorization credentials. Those requests bypass browser sign-in and
are authenticated by the provider; native cookies and spoofed Forgejo identity
headers are removed on that path.

Provider mechanisms: [Forgejo proxy authentication](https://forgejo.org/docs/latest/admin/setup/reverse-proxy/#proxy-authentication),
[Zulip JWT authentication](https://zulip.readthedocs.io/en/latest/production/authentication-methods.html#json-web-tokens-jwt),
and [Caddy forward authentication](https://caddyserver.com/docs/caddyfile/directives/forward_auth).
The proxy test suite runs with `HORIZON_TEST_CADDY_BINARY` set to the deployment's
Caddy executable. Verify actual authenticated frames as well as the HTTP tests;
provider CSP restrictions must permit the dashboard origin.
