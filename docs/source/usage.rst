=============================
Starting and Using the Server
=============================

Launch
======

Follow :doc:`installation` to prepare the environment and :doc:`configuration`
to select installed driver classes and complete Entra setup. Replace the example's
tenant/API UUID placeholders before launching its soft devices with the dummy
classic control layer:

.. code-block:: console

   $ OPHYD_CONTROL_LAYER=dummy pixi run --environment py311 ophyd-service --config examples/sim.toml

The default address is ``http://127.0.0.1:8000``. The CLI accepts required
``--config`` and optional ``--host`` and ``--port`` options. It runs one asyncio
server process, without reload; that process owns each configured root once,
not once per request or WebSocket. Startup must load trusted signing keys and
connect every configured root before serving. Configuration and the exposed
device tree are fixed until restart. Static ``/docs``, ``/redoc`` and
``/openapi.json`` are public; they describe routes, not the configured inventory.

Reader authentication
=====================

All four application GET routes and every WebSocket connection require a valid
JWT **access token for this API** and reader authorization. The required profile
is selected by server configuration, never by the token; Entra is the only
implemented production profile. Its users need both ``Ophyd.Reader`` role and
``Ophyd.Read`` delegated scope; app tokens need the role and must have no scope
claim. An ID token or Microsoft Graph access token is not an API credential.
See :ref:`entra-deployment` for registration, assignment and consent settings.

HTTP clients send ``Authorization: Bearer <access_token>``. For example, after
obtaining a token with your client and placing it in ``ACCESS_TOKEN``:

.. code-block:: console

   $ curl --header "Authorization: Bearer ${ACCESS_TOKEN}" http://127.0.0.1:8000/api/v1/read/temperature

The ``JWTAccessToken`` HTTP bearer scheme in ``/docs`` permits manual token entry.
The server provides no OAuth login, callback, cookies or local user database.
Missing, empty, non-Bearer or multiple Authorization fields are rejected. Tokens
in URLs, cookies, WebSocket subprotocols or forwarded-user headers are ignored.
TLS and log redaction remain required; never put access tokens in URLs or logs.

Resources and paths
===================

There are four application HTTP routes, all GET-only:

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Route
     - Successful response
   * - ``/api/v1/devices``
     - ``{"devices":["temperature","axis"]}`` for the example configuration
   * - ``/api/v1/resources/{path}``
     - Resource metadata, as shown below
   * - ``/api/v1/read/{path}``
     - ``{"path":path,"readings":native_read_mapping}``
   * - ``/api/v1/describe/{path}``
     - ``{"path":path,"data_keys":native_describe_mapping}``

``{path}`` can contain multiple slash-separated segments. For example,
``GET /api/v1/resources/temperature`` returns:

.. code-block:: json

   {"path":"temperature","backend":"ophyd","readable":true,"monitorable":true,"children":[]}

These are the complete metadata fields. ``backend`` is ``ophyd`` or
``ophyd-async``; ``children`` contains full paths of immediate children.
Classic Devices and Signals are readable. Async devices are readable only
when they provide native ``read`` and ``describe`` methods. Only classic
Signals and async SignalR instances (including SignalRW) are monitorable.
Write-only SignalW and command signals remain discoverable but cannot be read,
monitored, or invoked through the service. No arbitrary-method or write
endpoint is provided.

A service path begins with a configured root ID, followed by declared child
names. Each child segment uses JSON Pointer escaping: replace ``~`` with
``~0`` and ``/`` with ``~1``. For a driver declaring those children:

* A child named ``a.b`` under ``root/labels`` is ``root/labels/a.b``: dots
  remain literal, not attribute traversal.
* A child named ``a~/b`` is ``root/labels/a~0~1b``.
* Declared underscore-prefixed children such as ``root/_sig_rw`` remain
  visible; undeclared Python attributes do not become resources.
* Sparse vector keys ``1`` and ``3`` remain those keys, not positions ``0``
  and ``1``.

Use the returned paths rather than deriving them from native reading names or
PV addresses. A motor and its readback may share a native name while having
different service paths. Catalog lookup never evaluates a Python expression.
Listing classic lazy components does not instantiate them; selecting one for
read, describe, or monitoring resolves and connects that component.

Native readings and descriptions
================================

For the example configuration, ``GET /api/v1/read/temperature`` returns:

.. code-block:: json

   {"path":"temperature","readings":{"temperature":{"value":1.0,"timestamp":1000.0}}}

``GET /api/v1/describe/temperature`` returns:

.. code-block:: json

   {"path":"temperature","data_keys":{"temperature":{"source":"SIM:temperature","dtype":"number","shape":[]}}}

The service calls native ``read()`` and ``describe()``. Reading mappings retain
native keys, values, UNIX-epoch acquisition timestamps and optional alarm
fields. Descriptions retain the returned DataKey metadata, including source,
dtype, shape, units and enum choices when supplied. Descriptions are requested
anew, not cached indefinitely. A signal read provides its value and timestamp;
there is no separate ``get`` endpoint or exposure of classic ``Device.get()``.

A directly addressed async SignalR is read explicitly rather than from its
monitor cache. An aggregate device read retains the driver's native
contributors and cache semantics: the service does not expand it into all
descendants, combine independent roots, or promise an atomic multi-signal
snapshot. A native empty mapping remains ``{}``. For example, both ``axis``
and ``axis/user_readback`` can return readings keyed ``axis``. If a detector
requires preparation before reading, the read fails; the service does not
stage, trigger or prepare it to make a GET succeed.

JSON data rules
---------------

REST and WebSocket readings use the same normalization:

* NumPy scalars become JSON scalars. Numeric and boolean arrays retain their
  dimensional nesting, including non-contiguous and non-native-byte-order
  inputs; string arrays become nested JSON arrays. For example, a two-by-three
  value remains ``[[1,2,3],[4,5,6]]``, with native DataKey shape ``[2,3]``.
* Pydantic models, including ophyd-async Tables, become recursively normalized
  objects. A Table is a column object, not a list of row objects; its native
  shape metadata is preserved. Enums become their underlying values, not
  Python enum names. Tuple metadata becomes JSON arrays.
* Non-finite floating-point values (NaN and either infinity) become JSON
  ``null``. Signed and unsigned 64-bit integers remain exact JSON integer
  literals. Clients that need exact values outside the safe integer range
  ``[-(2**53-1), 2**53-1]`` must use a lossless JSON parser, not convert them
  to floating point.
* Unsupported values, such as complex numbers or arbitrary custom objects,
  produce ``serialization_error`` rather than a string representation or
  fabricated reading.

Mutable native data is snapshotted before deferred delivery; the service does
not mutate driver-owned arrays or cached objects.

Errors and deadlines
====================

HTTP service errors use one envelope, for example:

.. code-block:: json

   {"error":{"code":"not_found","message":"No resource at this path","path":"missing"}}

``path`` is the affected service path, or ``null`` when unavailable. The error
codes and HTTP status mapping are:

.. list-table::
   :header-rows: 1
   :widths: 25 10 65

   * - Code
     - HTTP status
     - Meaning
   * - ``unauthenticated``
     - 401
     - A valid access token is required; includes ``WWW-Authenticate: Bearer``.
   * - ``forbidden``
     - 403
     - The verified access token lacks reader permission.
   * - ``auth_unavailable``
     - 503
     - Usable signing-key trust is temporarily unavailable.
   * - ``not_found``
     - 404
     - The path is absent from the catalog.
   * - ``not_readable``
     - 409
     - A known resource cannot provide native read/describe data.
   * - ``not_monitorable``
     - 409
     - A known resource cannot be monitored. Currently returned as a
       WebSocket command error; there is no HTTP monitoring route.
   * - ``timeout``
     - 504
     - The service operation deadline expired.
   * - ``backend_error``
     - 502
     - Native read/describe, monitor setup, or selected lazy-child
       construction/connection failed.
   * - ``serialization_error``
     - 500
     - Native data could not be normalized or encoded as supported JSON.

Native failures report the exception type and message without a traceback;
server logs retain causal context. Failed reads are not successful empty
responses. Unknown URLs and unsupported HTTP methods use ordinary routing
404/405 responses rather than this service envelope. There are no application
POST, PUT, PATCH or DELETE routes.

Authentication failures have ``path: null`` and provider-neutral messages, not
token contents or decoded claims. Signing-key retrieval/cache deadlines are
independent of device workers and ``read_timeout``; see :doc:`configuration`.

The positive, finite ``read_timeout`` (default 5 seconds) covers read,
describe and monitor-start work, including waiting for classic worker capacity
or a selected lazy child. ``connect_timeout`` (default 10 seconds) is passed
to native connection waits; see :doc:`configuration`.

A response deadline cannot terminate a Python thread. Classic work is bounded
to four submitted jobs, with one operation in flight per root. A timed-out job
retains its root lock and worker slot until the actual operation finishes;
the service does not start a replacement operation or destroy that root
concurrently. Native driver I/O timeout settings remain the driver's own.
A permanently blocked driver can therefore delay graceful shutdown and
require supervisor intervention. Shutdown waits for owned monitoring cleanup
and classic jobs before destroying classic roots; it does not stop, unstage
or trigger devices.

Multiplexed WebSocket monitoring
================================

Open one ``ws://127.0.0.1:8000/api/v1/ws`` connection. Native clients may supply
the same Authorization header used for HTTP. A supplied header is authoritative:
an invalid header never falls back to another credential source. A valid header
authenticates the upgrade without an extra authentication frame, preserving the
subscribe-acknowledgement/reading sequence below. Header failures use the service
JSON HTTP denial response (401/403/503) when the ASGI denial extension is supported;
otherwise admission fails closed with ASGI close 1008, or 1013 for key unavailability.

Browser WebSocket APIs cannot set Authorization headers. A connection without
that header is accepted only into a five-second authentication phase. Its first
message must have exactly these fields:

.. code-block:: json

   {"op":"authenticate","access_token":"<access_token>"}

Before subscribing, wait for the acknowledgement (deadline below is illustrative):

.. code-block:: json

   {"type":"authenticated","expires_at":1893456030}

``expires_at`` is UNIX seconds including the fixed 30-second clock tolerance.
No broker session, device subscription or reading exists before authentication.
Missing/timed-out/malformed authentication or a different first operation receives
one generic error with ``id: null`` and ``path: null``, then close 1008. A forbidden
reader also closes 1008; signing-key unavailability closes 1013. Binary and oversized
first frames retain the 1003/1009 limits described below. All authentication sends
and closes are bounded; the acknowledgement cannot outlive authorization.

After authentication, send repeated commands to monitor multiple admitted signal
paths. All messages are UTF-8 JSON text frames. Each command has exactly three
required string fields:

* ``id``: nonempty, at most 64 characters, echoed in the command reply.
* ``op``: exactly ``subscribe`` or ``unsubscribe``.
* ``path``: the catalog path, using the same escaping as REST.

Extra fields are rejected. Each line below is a separate frame on the same
connection: commands travel to the server and frames with ``type`` travel
back. The async timestamp is illustrative; actual readings retain their
native timestamps.

.. code-block:: json

   {"id":"s1","op":"subscribe","path":"temperature"}
   {"type":"subscribed","id":"s1","path":"temperature"}
   {"type":"reading","path":"temperature","readings":{"temperature":{"value":1.0,"timestamp":1000.0}}}
   {"id":"s2","op":"subscribe","path":"axis/user_readback"}
   {"type":"subscribed","id":"s2","path":"axis/user_readback"}
   {"type":"reading","path":"axis/user_readback","readings":{"axis":{"value":2.5,"timestamp":1234.0,"alarm_severity":0}}}
   {"id":"s3","op":"unsubscribe","path":"temperature"}
   {"type":"unsubscribed","id":"s3","path":"temperature"}

Readings are identified by path, not command ID, and updates for different
paths can interleave. REST remains available while the socket is open.
A failed command receives a correlated error instead of an acknowledgement:

.. code-block:: json

   {"id":"s4","op":"subscribe","path":"missing"}
   {"type":"error","id":"s4","path":"missing","code":"not_found","message":"No resource at this path"}

WebSocket errors use the service codes above without an HTTP status after
upgrade. Bad JSON, unknown operations (including ``set`` and ``put``), wrong
field types and other malformed commands use ``invalid_request``. Valid
``id`` and ``path`` fields are retained for correlation independently; an
unavailable or invalid field becomes ``null``. For invalid JSON:

.. code-block:: json

   {"type":"error","id":null,"path":null,"code":"invalid_request","message":"Invalid JSON"}

These command errors do not change existing subscriptions or close a healthy
connection. Binary frames are unsupported and close with code 1003. Incoming
messages over 65,536 UTF-8 bytes close with 1009; the endpoint and supported
Uvicorn launch both enforce this limit. A send stalled for 5 seconds causes
cleanup and an attempted close with 1013; delivery of a close frame to a
stalled peer is not guaranteed.

Every stream ends at its effective authorization deadline, even when quiet, with
close code 1008 and reason ``token_expired``. No new command or data send starts
after expiry; a transmission already in progress cannot be recalled. Expiry
removes only that socket's memberships and pending frames, leaving other readers
of shared sources authorized. Obtain a renewed access token and reconnect before
or at the deadline. Another ``authenticate`` frame cannot refresh the token or
replace the connection's identity.

Ordering and latest-state delivery
----------------------------------

* A successful subscribe acknowledgement precedes the new membership's first
  reading. A late subscriber reuses the source's latest result. Repeating a
  subscribe acknowledges and replays the latest result without creating a
  second membership or multiplying subsequent updates.
* Unsubscribe is idempotent for a known monitorable path, even when it is
  inactive; it does not construct an unused lazy child. Unknown and
  non-monitorable paths still fail. Pending data for the removed membership
  is discarded. A frame already being sent can precede the unsubscribe
  acknowledgement, but no old frame for that membership follows it.
* One sender per socket retains at most one unsent latest telemetry frame per
  subscribed path plus the frame being sent, and a bounded command-reply slot.
  A busy path does not displace other paths indefinitely, and a slow socket
  does not block another client's delivery.

Monitoring is latest-state observation, not recording: intermediate values
and value/error transitions can coalesce in the native transport or service.
There is no history, replay cursor, polling loop or application-level retry
controller. A quiet stream does not establish connectivity.

After a successful subscription, a native read or serialization failure is a
path-scoped telemetry error with ``id: null``, for example (message depends on
the driver):

.. code-block:: json

   {"type":"error","id":null,"path":"temperature","code":"backend_error","message":"RuntimeError: Reading unavailable"}

The membership remains active. Another native notification may produce a
valid reading; the service does not poll or retry to recover. A new subscriber
encountering the current error receives a correlated failure, not stale data.
Failure or timeout before initial readiness removes only that pending
interest, leaving other clients' subscriptions intact.

Native ownership and browser boundary
-------------------------------------

The process shares one native registration per actual signal object across
clients and paths, not per PV address or native name. Classic notifications
invalidate a shared native ``read()`` result; callbacks themselves do no I/O.
Async ``subscribe_reading`` notifications already contain readings and do not
cause extra reads. Neither branch stages a signal to keep monitoring alive.

Once no clients remain, unsubscribe, disconnect, authorization expiry and
shutdown remove only the service's owned classic
token or exact async callback, after settling in-flight registration work.
Late callbacks cannot revive a removed membership. An unsubscribe
acknowledgement promises the delivery barrier, not immediate native transport
teardown. Classic EPICS monitors may remain until object/process teardown;
async caches may remain while other native listeners or staging owners use
them. The service does not remove unrelated listeners or promise universal
transport disconnection. Native removal failures are logged and prevent
reattachment to that source rather than guessing that cleanup succeeded.

Before accepting a browser WebSocket, the server requires its ``Origin`` to
match the request's effective HTTP(S) scheme/host/port or an exact additional
``auth.allowed_origins`` entry. ``Origin: null`` and other mismatches are rejected;
native clients may omit Origin. Every permitted origin still needs a reader token.
With a nonempty origin list, CORS allows exactly those origins, GET and the
Authorization header, without credential cookies. Otherwise CORS is disabled.
Preflight is public but cannot touch devices, and CORS never grants permission.
See :doc:`configuration` for trusted proxy/HTTPS settings and
:doc:`introduction` for the read-only safety boundary.
