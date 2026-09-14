================
ophyd-as-service
================

Read-only REST and WebSocket access to classic ophyd and ophyd-async devices.
One server process owns the configured devices and provides discovery, native
readings, descriptions, and latest-state monitoring over one multiplexed
WebSocket per client. There is no write, motion, acquisition, or arbitrary-method
API.

Hardware-free quickstart
=======================

From the repository root, with Pixi available:

.. code-block:: console

   pixi install --environment py311
   OPHYD_CONTROL_LAYER=dummy pixi run --environment py311 ophyd-service --config examples/sim.toml

This uses the released backends and the ``sim`` extra in the declared development
environment. The supplied `soft-device configuration <examples/sim.toml>`_
constructs an in-memory Signal and SimMotor; startup and reads do not move the
motor. The service listens on ``http://127.0.0.1:8000`` by default. Open
``http://127.0.0.1:8000/docs`` for the HTTP API, or read
``http://127.0.0.1:8000/api/v1/read/temperature``. Stop with Ctrl-C.

The soft-device behavioral suite covers Python 3.11–3.14; a launched-server
smoke check exercises real HTTP and one multiplexed WebSocket. This does not
verify native CA/PVA/Tango transports or live hardware.

Safety boundary
===============

Configuration is trusted local constructor data, not a sandbox. Operators must
review installed driver imports, constructors, connection hooks, and getters for
side effects. Use authoritative read-only control-system permissions for
hardware-enforced protection; a read-only HTTP API cannot make an unsafe driver
safe. The service is unauthenticated and defaults to loopback. Remote access
requires a trusted access boundary, such as an existing authenticated TLS
reverse proxy; read-only access is not confidentiality protection.

User documentation
==================

* `Installation and development commands <docs/source/installation.rst>`_
* `Configuration and lifecycle <docs/source/configuration.rst>`_
* `HTTP and WebSocket usage <docs/source/usage.rst>`_
* `Service scope <docs/source/introduction.rst>`_
