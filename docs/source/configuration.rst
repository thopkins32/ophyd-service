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

``devices``
   Required table mapping root identifiers to device specifications. An explicit
   empty ``[devices]`` table is valid and serves an empty device list; omitting the
   table is an error.

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

Use the :doc:`installation` quickstart with ``OPHYD_CONTROL_LAYER=dummy`` set
before launch. The declared ``sim`` extra supplies the upstream SimMotor import
dependencies. This configuration constructs only an in-memory classic Signal
and an async SimMotor; the service does not move even this simulated motor.

Startup and ownership
=====================

A missing file, invalid TOML, duplicate TOML keys, missing required fields, or
invalid field values fails startup with file/field context. After validation,
roots are constructed and connected in configuration order. The application
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

The unauthenticated service binds to ``127.0.0.1`` by default. Remote deployments
need a trusted access boundary, such as an existing authenticated TLS reverse
proxy. Read-only access is not confidentiality protection, and same-origin
WebSocket checks are not authentication. See :doc:`introduction` for the service
scope and :doc:`usage` for the wire contract. Soft-device verification does not
establish native transport correctness or authorize access to live hardware.
