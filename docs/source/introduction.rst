============
Introduction
============

Ophyd-as-Service exposes discovery, native readings, descriptions and
latest-state monitoring for classic `ophyd <https://blueskyproject.io/ophyd/>`_
and `ophyd-async <https://blueskyproject.io/ophyd-async/>`_ devices. A single
asyncio process constructs and owns configured roots for its lifetime. Clients
use four GET routes and one multiplexed WebSocket per connection; there is no
device worker service, RunEngine, queue or database to operate.

JWT reader authentication protects every application REST resource and WebSocket
connection. A provider-neutral RS256 core requires an explicit access-token
profile; Microsoft Entra for one organizational tenant is the only production
profile currently implemented. One assigned reader permission grants access to
all configured resources. There are no login pages, cookies or anonymous mode.

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

The service requires authentication even on its loopback-default listener.
Remote use needs HTTPS/WSS and trusted reverse-proxy/backend settings, not
forwarded identity headers. Read-only access alone does not provide
confidentiality; browser origin checks and CORS do not grant reader permission.
Already-issued tokens may remain usable until expiry plus clock tolerance;
streams close at that authorization deadline. Monitoring is coalesced latest
state, not acquisition recording or proof of connectivity. See
:doc:`configuration` for Entra deployment and revocation limits.
