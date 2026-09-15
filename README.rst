================
ophyd-as-service
================

Read-only REST and WebSocket access to classic ophyd and ophyd-async devices.
One server process owns the configured devices and provides discovery, native
readings, descriptions, and latest-state monitoring over one multiplexed
WebSocket per client. There is no write, motion, acquisition, or arbitrary-method
API.

JWT reader authentication is mandatory for REST and WebSockets, including local
use. The verification core is provider-neutral; the only production profile
currently implemented is single-tenant Microsoft Entra.

Hardware-free quickstart
=======================

From the repository root, with Pixi available. Before launching, complete the
`Entra registration and client setup <docs/source/configuration.rst>`_ and replace
the tenant/API UUID placeholders in ``examples/sim.toml``. Tests need no real
credentials; the production service has no anonymous/offline-auth bypass.

.. code-block:: console

   pixi install --environment py311
   OPHYD_CONTROL_LAYER=dummy pixi run --environment py311 ophyd-service --config examples/sim.toml

This uses the released backends and the ``sim`` extra in the declared development
environment. The supplied `soft-device configuration <examples/sim.toml>`_
constructs an in-memory Signal and SimMotor; startup and reads do not move the
motor. The service listens on ``http://127.0.0.1:8000`` by default. Public
``http://127.0.0.1:8000/docs`` supports manual bearer-token entry. Read
``/api/v1/read/temperature`` with ``Authorization: Bearer <access_token>``;
browser WebSockets use the first-message flow in the usage guide. Stop with Ctrl-C.

The soft-device behavioral suite covers Python 3.11–3.14; a launched-server
smoke check exercises real HTTP, both WebSocket credential paths, stream expiry
and native callback cleanup using mocked identity endpoints. It does not verify
real-tenant interoperability, native CA/PVA/Tango transports or live hardware.

Safety boundary
===============

Configuration is trusted local constructor data, not a sandbox. Operators must
review installed driver imports, constructors, connection hooks, and getters for
side effects. Use authoritative read-only control-system permissions for
hardware-enforced protection; a read-only HTTP API cannot make an unsafe driver
safe. Authentication is mandatory and the listener defaults to loopback. Remote
access requires HTTPS/WSS with a trusted TLS-termination/backend connection and
proxy configuration; read-only access is not confidentiality protection. Role
removal is not instant token revocation: issued tokens remain valid until their
expiry plus the fixed tolerance.

User documentation
==================

* `Installation and development commands <docs/source/installation.rst>`_
* `Configuration and lifecycle <docs/source/configuration.rst>`_
* `HTTP and WebSocket usage <docs/source/usage.rst>`_
* `Service scope <docs/source/introduction.rst>`_
