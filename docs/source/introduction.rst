============
Introduction
============

Ophyd-as-Service exposes discovery, native readings, descriptions and
latest-state monitoring for classic `ophyd <https://blueskyproject.io/ophyd/>`_
and `ophyd-async <https://blueskyproject.io/ophyd-async/>`_ devices. A single
asyncio process constructs and owns configured roots for its lifetime. Clients
use four GET routes and one multiplexed WebSocket per connection; there is no
device worker service, RunEngine, queue or database to operate.

Start with :doc:`installation`, select installed driver classes and data-only
constructor arguments in :doc:`configuration`, then follow :doc:`usage` for
resource paths, native JSON data, errors and monitoring semantics. The bundled
example uses only in-memory devices with the dummy classic control layer; it
does not establish native transport or live-hardware safety.

Read-only boundary
==================

The public API cannot set values, move motors, execute commands, stage,
trigger or prepare devices. It does not execute startup scripts or remotely
register or reconfigure devices. Native reads remain native: a detector that
requires preparation can fail rather than being prepared by the service.

Read-only describes the service's operations, not a sandbox for installed
driver code. Constructors, connection hooks and getters must also be safe for
the intended target. Admit a read-only driver or signal view when a driver
would otherwise initialize or prepare hardware by writing. Use authoritative
IOC/control-system permissions when hardware-enforced read-only access is
required.

The service is unauthenticated and binds to loopback by default. Remote use
requires a trusted access boundary, such as an existing authenticated TLS
reverse proxy. Read-only access does not provide confidentiality, and the
WebSocket same-origin check is not authentication. Monitoring is coalesced
latest state, not acquisition recording or proof of connectivity.
