# Trust boundary

A webhook payload is third-party text that ends up inside a prompt, next to an
agent holding tools. That is the whole of the security story worth reading.

A valid signature proves Hookdeck sent the request. It says nothing about the
contents. PR titles, commit messages, issue bodies and customer names are
written by third parties, and they end up in the prompt.

Hermes' own guidance applies and is worth following: run webhook-triggered
routes against a sandboxed terminal backend (Docker or SSH), scope the toolset
on those routes, require approval for destructive tools, and prefer a specific
prompt template over dumping `{__raw__}`. The adapter sets a platform hint
telling the model that payload text is data, never instructions addressed to it.

For local testing only, `secret: INSECURE_NO_AUTH` skips verification. It is
refused unless the listener is bound to loopback.

The adapter declares `authorization_is_upstream`, which is what stops the
gateway refusing every delivery as `Unauthorized user: hookdeck:<route>`. Core
exempts its own webhook platform from the user allowlist by enum member,
reasoning that HMAC verification in the adapter *is* the authorization; the
reasoning carries over but the membership test cannot, since this platform is
`Platform.HOOKDECK`. The flag goes false whenever verification is off, so an
`INSECURE_NO_AUTH` route still falls under `HOOKDECK_ALLOWED_USERS` — narrower
than core's exemption, which covers built-in webhook routes even unverified.
