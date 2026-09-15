====================
Server Configuration
====================

Pass a trusted local TOML file to ``ophyd-service --config PATH``. Parsing and
validation of the complete file finish before any configured driver module is
imported. The file is the operator-controlled class allowlist; HTTP and WebSocket
clients cannot register devices or change it.

Fields
======

Only the following service and device fields are accepted. Unknown fields fail
validation, including misspelled options and ``startup_script``.

``auth.profile``
   Required nested profile table. The only implemented production profile is
   ``type = "entra"``. There is no default profile, generic-JWT mode, dynamic
   import/plugin option, authentication-disable flag or server client secret.

``auth.profile.tenant_id`` and ``auth.profile.api_client_id``
   Required UUIDs for the Entra Directory (tenant) ID and API Application
   (client) ID. The latter is the client ID, not an Application ID URI or an
   unrelated client application's ID. Unknown profile fields are rejected.

``auth.allowed_origins``
   Optional list of additional serialized browser origins; default ``[]`` keeps
   same-origin-only browser access. Entries must have a hostname and no
   credentials, trailing slash, path, query, fragment or wildcard. HTTPS is
   accepted; HTTP is accepted only for literal ``localhost`` or a loopback IP
   address, for example ``http://127.0.0.1:5173`` or ``http://[::1]:5173``.
   Invalid ports are rejected. Strings are preserved exactly: use the origin
   serialized by the browser, not a URL to a page. This list controls CORS and
   additional WebSocket origins, never signing-key trust or reader permission.

``devices``
   Required table mapping root identifiers to device specifications. An explicit
   empty ``[devices]`` table is valid with explicit authentication and serves an
   empty authorized device list; omitting the table is an error.

``connect_timeout``
   Positive, finite seconds; default ``10.0``. Passed to native connection waits,
   including connection of a selected classic lazy resource. Async root
   connection also has an outer deadline of this duration. This is not a general
   limit on driver imports or constructors.

``read_timeout``
   Positive, finite seconds; default ``5.0``. Response deadline for read,
   describe, and monitor-start work, including waiting for classic worker
   capacity or a selected lazy resource. It does not change a driver's native
   read timeout or terminate a running Python thread.

``devices.<root>.class``
   Required string in exactly ``module:Class`` form: a dotted module name and
   one class name, not an expression or nested attribute lookup. Startup imports
   that module and looks up the class. It must be a subclass of ``ophyd.Device``,
   ``ophyd.Signal``, or ``ophyd_async.core.Device``. A callable factory or an
   unrelated class is rejected before construction.

``devices.<root>.kwargs``
   Optional table of constructor keyword arguments, defaulting to an empty
   mapping. Values are data only: TOML strings, numbers, booleans, arrays, and
   nested tables accepted by Pydantic's ``JsonValue`` validation. TOML date/time
   objects are not constructor data. Strings are not evaluated or interpreted
   as imports or object references. ``name`` is reserved and must not appear in
   this table.

Root identifiers must match ``[A-Za-z][A-Za-z0-9_-]*``: begin with an ASCII letter,
then contain only ASCII letters, digits, underscores, or hyphens. The service
constructs each root exactly once as ``cls(name=root_id, **kwargs)``. Constructor
arguments are passed through as data; there are no alternative constructor
attempts. The root identifier names the service resource, while native reading
keys and sources retain their driver's naming. Child paths and escaping are
described in :doc:`usage`.

Drivers needing object-valued arguments must package that composition in an
ordinary installed Device class with a data-only constructor. Native Components
and async connectors remain part of that class, not a configuration language.
There are no startup scripts, Python expressions, factories, reference arguments,
restore-settings hooks, or runtime reconfiguration. Change the configuration or
device tree by restarting the service.

Hardware-free example
=====================

The repository's ``examples/sim.toml`` is included directly below:

.. literalinclude:: ../../examples/sim.toml
   :language: toml

Replace both deliberately invalid ``YOUR_...`` UUID placeholders before launch,
and complete the Entra registration below. Hardware-free does not mean anonymous
or identity-provider-free: production has no authentication bypass. Automated
offline verification mocks discovery/JWKS HTTP requests, not token verification.

Use the :doc:`installation` quickstart with ``OPHYD_CONTROL_LAYER=dummy`` set
before launch. The declared ``sim`` extra supplies the upstream SimMotor import
dependencies. This configuration constructs only an in-memory classic Signal
and an async SimMotor; the service does not move even this simulated motor.

Access-token profiles
=====================

Authentication is mandatory for every application REST resource and WebSocket
connection, including loopback use. A provider-neutral RS256 JWT core owns
signature and registered-claim verification, exact issuer/audience trust,
discovery/JWKS retrieval and cache lifecycle. A required, reviewed profile owns
access-token identification and reader authorization. Only the single-tenant,
public-cloud Entra profile is currently selectable. Adding another provider
requires reviewed code, a strict configuration variant, explicit factory
dispatch and tests; a token cannot select a provider or signing endpoint.

The selected profile supplies trusted HTTPS discovery and signing-key origins.
Redirects, mismatched discovery issuers and out-of-scope JWKS URLs are rejected;
token ``jku``/``x5u`` headers are not used. The HTTP client supports the operator's
proxy and CA environment settings, verifies certificates, limits requests to
5 seconds and complete refreshes to 10 seconds, and caps each document at 2 MiB.

Keys load before readiness and refresh hourly. Unknown keys or expired caches
share a refresh attempt with a five-minute minimum interval, including failed
attempts. Failed refreshes preserve last-known-good keys for at most 24 hours
since their last successful refresh. Successful refreshes replace the complete
key set, retiring removed keys. Unusable trust fails closed, never anonymously.
These are fixed service policies, not TOML tuning options. Tokens are limited to
65,536 UTF-8 bytes; registered dates use a fixed 30-second clock tolerance.

.. _entra-deployment:

Entra deployment
================

The following settings are operator prerequisites, not actions performed by the
service. No tenant registrations or real credentials are embedded in the example.

1. Register a **single-tenant API application** for accounts in this organizational
   directory only. Put its tenant and API client UUIDs in ``[auth.profile]`` with
   ``type = "entra"``. Use Application ID URI ``api://<api_client_id>`` and set
   ``api.requestedAccessTokenVersion`` to ``2``. This API needs no redirect URI or
   client secret. Browser origins belong in ``[auth]``, not the profile table.

2. Define an enabled app role with display name ``Ophyd Reader``, value
   ``Ophyd.Reader``, and allowed member types **Users/Groups and Applications**.
   Set **Assignment required? = Yes** on the API's Enterprise application.
   Assign people directly, or assign a group where the tenant supports it; the
   service checks the emitted role, not group membership. Assign service
   principals and managed identities directly rather than relying on groups.
   Guests also need explicit reader-role assignment. This one role grants equal
   reader access to all configured resources, not per-device permissions.

3. Expose delegated scope ``Ophyd.Read`` with **Admins only** consent. Register
   the human client separately and grant that API permission with administrator
   consent. Interactive public clients use authorization code with PKCE; their
   redirect URI belongs to the client application, not this API. People need
   both the reader-role assignment and the client's delegated permission.
   Device-code acquisition is usable only if tenant policy permits it; no
   password grant or server login/callback flow is implemented.

4. Add the optional **access-token** claim ``idtyp`` with
   ``additionalProperties: ["include_user_token"]`` on the API registration:

   .. code-block:: json

      {"optionalClaims":{"accessToken":[{"name":"idtyp","essential":false,"additionalProperties":["include_user_token"]}]}}

   The profile requires v2 access-token markers with ``idtyp`` exactly ``user``
   or ``app``, matching tenant UUID and valid object/client UUID claims. App
   tokens must not contain ``scp``. All readers need the exact ``Ophyd.Reader``
   role; users additionally need the space-delimited ``Ophyd.Read`` scope. Tenant
   membership alone grants no access, and ID tokens are not accepted.

5. For unattended callers, grant the API's ``Ophyd.Reader`` application
   permission and tenant-admin consent. Request ``api://<api_client_id>/.default``;
   prefer managed identity, workload federation or a certificate. Any caller
   secret stays with that caller, never in the server's TOML. There is no local
   user database, Microsoft Graph lookup, Redis lookup or token-acquisition flow.

6. Clients send an **access token for this API**, never an ID token or a Microsoft
   Graph token. HTTP and native WebSocket clients can send a Bearer header;
   browser WebSockets authenticate in their first message. See :doc:`usage` for
   the frames and authorization deadline. Obtain a renewed token and reconnect
   before or at that deadline; in-place token refresh is not supported.

7. Deploy behind HTTPS/WSS. An HTTP backend is acceptable only over a trusted
   connection behind TLS termination. Keep the supported one-process launch
   and loopback default. A reverse proxy must preserve the external Host/scheme
   and trust forwarded headers only from its actual IPs; never expose a listener
   with ``FORWARDED_ALLOW_IPS=*``. Forwarded identity headers are not credentials.
   Redact Authorization and authentication-frame contents in proxy/application
   diagnostics. Rate limiting and ingress connection limits remain deployment
   controls, not an additional local authentication subsystem.

8. Role removal or account disablement is **not instant access-token revocation**.
   Already-issued tokens may authorize access until expiry plus the fixed
   tolerance. WebSocket expiry prevents unlimited streaming, but no CAE,
   revocation feed or Graph polling is claimed. Soft-device and mocked-identity
   tests do not establish real-tenant interoperability or tenant policy; verify
   authorized REST/needed WebSocket clients against your registration. An
   unassigned caller may be denied token issuance by Entra, which is expected
   with assignment required; do not disable that policy to manufacture a test
   token. Driver trust and hardware-enforced read-only permissions still apply.

Startup and ownership
=====================

A missing file, invalid TOML, duplicate TOML keys, missing required fields, or
invalid field values fails startup with file/field context. After validation,
the authenticator loads trusted discovery/JWKS before constructing any root.
Roots then construct and connect in configuration order. The application
does not become ready until every root succeeds. An import, class, construction,
or connection failure identifies the root/class and its underlying cause, cleans
up already-owned resources, and exits nonzero instead of serving a partial
inventory. It never silently skips a root or switches to mock mode.

One application owns each root for its lifetime and reuses it for every request
and subscription. Classic work runs off the asyncio loop in a bounded four-worker
executor, with at most one operation in flight per root. Async devices are
constructed and connected on the application loop. The exposed namespace is
frozen after startup; classic lazy components are listed without constructing
them and are resolved only when selected.

A response timeout does not stop a classic operation already running. That root
and its worker slot remain occupied until the operation actually finishes, and
shutdown waits for owned work before destroying classic roots. A permanently
stuck driver can therefore delay graceful shutdown; process termination or
restart belongs to the operator's supervisor, not replacement service workers.

.. _configuration-safety:

Trust and safety boundary
=========================

The TOML file is trusted local deployment configuration. A subclass check is not
a sandbox: imported code, constructors, connection hooks, and read/describe or
monitor hooks may have side effects. Operators must review their installed
drivers and use authoritative read-only IOC/control-system permissions for
hardware-enforced protection. If a driver writes during initialization or needs
preparation before reading, use a read-only driver/view instead; the service does
not stage, trigger, move, or restore settings to make reads succeed.

The service binds to ``127.0.0.1`` by default and requires reader authentication
even there. Remote deployments need HTTPS/WSS and the trusted ingress settings
above. Read-only access is not confidentiality protection; origin checks and
CORS do not grant permission. See :doc:`introduction` for service scope and
:doc:`usage` for the wire contract. Soft-device verification does not establish
native transport correctness or authorize access to live hardware.
