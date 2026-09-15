===============
Release History
===============

Unreleased
==========

* Replace the placeholder package with a single-process, read-only service
  for classic ophyd and ophyd-async, with declarative TOML construction and
  the ``ophyd-service`` entry point. See :doc:`installation` and
  :doc:`configuration` for Python requirements, transport extras and the
  hardware-free example.
* Add catalog discovery, native read/describe GET routes and one multiplexed
  WebSocket per client. Preserve escaped child paths, native metadata and
  timestamps, with shared JSON normalization and structured errors.
* Share monitoring registrations by signal object, bound latest-state
  delivery for slow clients, and release service-owned callbacks on
  unsubscribe, disconnect and shutdown. Classic I/O is bounded and retains
  ownership after response deadlines; deadlines cannot kill worker threads.
* Require explicit nested authentication configuration and JWT reader permission
  for all application HTTP resources and WebSocket streams. A provider-neutral
  RS256/discovery/JWKS core composes with the sole production profile, Microsoft
  Entra. Existing anonymous clients/configurations must migrate; there is no
  compatibility bypass. See :ref:`entra-deployment`.
* Support native WebSocket bearer upgrades and browser first-frame authentication,
  exact additional browser origins, and bounded stream authorization lifetimes.
  Expiry closes quiet sockets and retires only their owned interests, including
  late classic registrations, without interrupting other authorized readers.
* Retain the loopback default and read-only operation boundary. Expose no write or
  arbitrary-command endpoint, runtime device registration, startup scripts,
  acquisition preparation, OAuth login flow or local user database.

See :doc:`usage` for the wire contract and operational limits, and
:doc:`introduction` for the read-only safety boundary. Soft-device verification
does not certify native transports or live hardware.
