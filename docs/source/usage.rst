=============================
Starting and Using the Server
=============================

Launch
======

Follow :doc:`installation` to prepare the environment and
:doc:`configuration` to select installed driver classes. From the repository
root, launch the hardware-free example with the dummy classic control layer:

.. code-block:: console

   $ OPHYD_CONTROL_LAYER=dummy pixi run --environment py311 ophyd-service --config examples/sim.toml

The default address is ``http://127.0.0.1:8000``. The CLI accepts required
``--config`` and optional ``--host`` and ``--port`` options. It runs one asyncio
server process, without reload; that process owns each configured root once,
not once per request or WebSocket. Startup must connect every configured root
before serving. Configuration and the exposed device tree are fixed until
restart. Interactive HTTP documentation is available at ``/docs`` and
``/redoc``, with the schema at ``/openapi.json``.

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

Open one ``ws://127.0.0.1:8000/api/v1/ws`` connection and send repeated commands
to monitor multiple admitted signal paths. All messages are UTF-8 JSON text
frames. Each command has exactly three required string fields:

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

Unsubscribe, disconnect and shutdown remove only the service's owned classic
token or exact async callback, after settling in-flight registration work.
Late callbacks cannot revive a removed membership. An unsubscribe
acknowledgement promises the delivery barrier, not immediate native transport
teardown. Classic EPICS monitors may remain until object/process teardown;
async caches may remain while other native listeners or staging owners use
them. The service does not remove unrelated listeners or promise universal
transport disconnection. Native removal failures are logged and prevent
reattachment to that source rather than guessing that cleanup succeeded.

Before accepting a browser WebSocket, the server requires its ``Origin``
scheme, host and port to match the request's effective HTTP(S) origin.
``Origin: null`` and mismatched origins are rejected; native clients may omit
``Origin``. CORS is not enabled. This check is not authentication or general
host authorization. Keep the loopback default, or supply a trusted access
boundary for remote use; see :doc:`introduction` for the read-only safety
boundary.
