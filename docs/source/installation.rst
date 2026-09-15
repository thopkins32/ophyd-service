============
Installation
============

Requirements
============

The package requires Python 3.11 or newer. The declared Pixi environments
``py311``, ``py312``, ``py313``, and ``py314`` cover Python 3.11–3.14 on
``linux-64``. Each installs the service editably with its ``dev`` and ``sim``
extras. Runtime backends are released packages, not reference-source checkouts:

* ``ophyd>=1.11.2,<2``
* ``ophyd-async>=0.21.2,<0.22``

JWT verification uses ``PyJWT[crypto]>=2.14,<3``. The transport/runtime contract
also requires ``fastapi>=0.141.1,<1`` and ``httpx>=0.28.1,<1``. Use the declared
environment and lockfile rather than substituting a verifier or lowering the
security release floor.

Configured driver modules must already be installed in the service's environment.
The server does not install packages, select transports dynamically, or add
driver directories to the Python import path. Declare driver and transport
dependencies in the deployment environment and resolve them using that
environment's dependency and lockfile workflow.

Optional extras
===============

Select extras on the ``ophyd-as-service`` dependency in your managed environment,
for example ``ophyd-as-service[ca]``. These supply upstream dependencies, not a
certification that a particular driver is safe for read-only use.

.. list-table:: Declared extras
   :header-rows: 1
   :widths: 15 85

   * - Extra
     - Dependencies supplied
   * - ``ca``
     - ``caproto>=0.4.2rc1,!=1.2.0`` and ``ophyd-async[ca]>=0.21.2,<0.22``
   * - ``pva``
     - ``ophyd-async[pva]>=0.21.2,<0.22``
   * - ``tango``
     - ``ophyd-async[tango]>=0.21.2,<0.22``
   * - ``sim``
     - ``ophyd-async[sim]>=0.21.2,<0.22``; includes the dependencies needed to
       import the example SimMotor

The service preserves the selected classic ophyd control layer. Set any required
control-layer environment variables before launch. Missing transports or driver
dependencies fail startup; there is no mock fallback. The soft example instead
uses ``OPHYD_CONTROL_LAYER=dummy`` and needs no IOC or live device.

Pixi development environment
============================

Run from the repository root with Pixi available. Installing and running tests
requires no real Entra credentials. Before launching the example, complete
:ref:`entra-deployment` and replace its deliberately invalid tenant/API UUID
placeholders. Hardware-free operation still requires authentication.

.. code-block:: console

   pixi install --environment py311
   OPHYD_CONTROL_LAYER=dummy pixi run --environment py311 ophyd-service --config examples/sim.toml

The server defaults to ``127.0.0.1:8000``. Its only application options are the
required ``--config PATH`` and optional ``--host`` and ``--port``. It runs one
process on one asyncio loop, with no reload mode. See :doc:`configuration` for
the included TOML and safety requirements, and :doc:`usage` for the API.

The declared ``test`` task sets ``OPHYD_CONTROL_LAYER=dummy`` before importing
ophyd. Run the soft-device suite in each supported environment:

.. code-block:: console

   pixi run --environment py311 test
   pixi run --environment py312 test
   pixi run --environment py313 test
   pixi run --environment py314 test

Ruff is supplied by the ``dev`` extra. Check the service and its tests without
rewriting files:

.. code-block:: console

   pixi run --environment py311 ruff check src tests
   pixi run --environment py311 ruff format --check src tests

The service tests use soft/in-memory devices, real RSA-signed tokens and mocked
discovery/JWKS HTTP endpoints. They do not contact Entra or establish real-tenant
policy, native transport correctness or site-hardware safety. Do not point
automated checks at a live control system.
