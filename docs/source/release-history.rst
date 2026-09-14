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
* Default to loopback with a browser WebSocket same-origin check. Expose no
  write or arbitrary-command endpoint, runtime device registration, startup
  scripts, acquisition preparation, authentication stack or database.

See :doc:`usage` for the wire contract and operational limits, and
:doc:`introduction` for the read-only safety boundary. Soft-device verification
does not certify native transports or live hardware.
