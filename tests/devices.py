"""Soft drivers selected by the same ``module:Class`` config as real devices.

All observations are per instance. ``forbidden_calls`` records attempted mutation
before raising; test-owned classic updates use ``fixture_put`` (or explicitly
``Signal.put(signal, ...)``), and async updates use ``temperature_setter`` on the
owning application loop. No device instances or transport connections are made
at import time.
"""

from threading import Event, Lock, get_ident

from ophyd import Component, Device, Signal
from ophyd_async.core import (
    DeviceMap,
    SignalR,
    SignalW,
    SoftSignalBackend,
    StandardReadable,
    StandardReadableFormat,
    soft_signal_r_and_setter,
)


class MutationGuard:
    """Fail immediately, including for normally no-op lifecycle mutators."""

    def __init__(self, *args, **kwargs):
        self.forbidden_calls = []
        super().__init__(*args, **kwargs)

    def _forbid(self, operation):
        self.forbidden_calls.append(operation)
        raise AssertionError(f"Forbidden fixture mutation: {self.name}.{operation}")

    def put(self, *args, **kwargs):
        self._forbid("put")

    def set(self, *args, **kwargs):
        self._forbid("set")

    def stage(self, *args, **kwargs):
        self._forbid("stage")

    def unstage(self, *args, **kwargs):
        self._forbid("unstage")

    def trigger(self, *args, **kwargs):
        self._forbid("trigger")

    def stop(self, *args, **kwargs):
        self._forbid("stop")


class ObservedSignal(MutationGuard, Signal):
    """Real classic Signal with observable native callback ownership.

    ``subscription_tokens`` and ``unsubscription_tokens`` record successful
    public calls; ``active_tokens`` contains only currently owned tokens.
    ``subscribed``, ``unsubscribed`` and ``destroyed`` are threading Events.
    ``destroy_calls`` counts attempts and ``read_calls`` counts native reads.
    """

    def __init__(self, *, name="", value=1.0, timestamp=1000.0, **kwargs):
        self.construction_thread = get_ident()
        self.read_calls = 0
        self.subscribe_calls = 0
        self.subscription_tokens = []
        self.unsubscription_tokens = []
        self.active_tokens = set()
        self.subscribed = Event()
        self.unsubscribed = Event()
        self.destroy_calls = 0
        self.destroy_threads = []
        self.destroyed = Event()
        super().__init__(name=name, value=value, timestamp=timestamp, **kwargs)

    def fixture_put(self, value, *, timestamp=None):
        """Simulate an external value change without invoking a guarded write."""
        Signal.put(self, value, timestamp=timestamp)

    def read(self):
        self.read_calls += 1
        return super().read()

    def subscribe(self, callback, event_type=None, run=True):
        self.subscribe_calls += 1
        token = super().subscribe(callback, event_type=event_type, run=run)
        self.subscription_tokens.append(token)
        self.active_tokens.add(token)
        self.subscribed.set()
        return token

    def unsubscribe(self, token):
        super().unsubscribe(token)
        self.unsubscription_tokens.append(token)
        self.active_tokens.discard(token)
        self.unsubscribed.set()

    def destroy(self):
        self.destroy_calls += 1
        self.destroy_threads.append(get_ident())
        super().destroy()
        self.active_tokens.clear()
        self.destroyed.set()


class ObservedComponent(Component):
    """Record lazy construction without accessing ophyd's instance cache."""

    def create_component(self, instance):
        component = super().create_component(instance)
        if self.lazy:
            instance.lazy_constructions.append(self.attr)
            instance.unused_constructed.set()
        return component


class ClassicDevice(MutationGuard, Device):
    """Only temperature contributes to the native root read.

    Listing must leave ``lazy_constructions == []`` and ``unused_constructed``
    unset. Selecting ``unused`` records its construction. Root destruction is
    observed by ``destroy_calls``, ``destroy_threads`` and ``destroyed``.
    """

    temperature = Component(ObservedSignal, value=1.0, timestamp=1000.0)
    unused = ObservedComponent(ObservedSignal, value=9.0, lazy=True, kind="omitted")

    def __init__(self, prefix="", *, name=""):
        self.construction_thread = get_ident()
        self.lazy_constructions = []
        self.unused_constructed = Event()
        self.destroy_calls = 0
        self.destroy_threads = []
        self.destroyed = Event()
        super().__init__(prefix, name=name)

    def destroy(self):
        self.destroy_calls += 1
        self.destroy_threads.append(get_ident())
        super().destroy()
        self.destroyed.set()


class GatedSignal(ObservedSignal):
    """A classic root for deterministic read and late-registration races.

    Both ``read_release`` and ``subscribe_release`` start set. Clear a release
    Event before an operation, await its corresponding ``*_started`` Event,
    then release it in a test finalizer. ``read_finished``/``subscribe_finished``
    indicate completion. The subscribe gate holds a successfully registered
    token just before returning it, so disconnection can race a late result.

    ``read_error`` can be assigned an exception or None; ``fail_read`` accepts a
    message through TOML. ``active_reads``/``max_active_reads`` detect overlap;
    ``destroyed_while_reading`` detects destruction before a worker completes.
    Clear observation Events explicitly before reusing them for another round.
    """

    def __init__(self, *, name="", fail_read=None, **kwargs):
        self.read_started = Event()
        self.read_release = Event()
        self.read_release.set()
        self.read_finished = Event()
        self.subscribe_started = Event()
        self.subscribe_release = Event()
        self.subscribe_release.set()
        self.subscribe_finished = Event()
        self.read_error = RuntimeError(fail_read) if fail_read is not None else None
        self.active_reads = 0
        self.max_active_reads = 0
        self.destroyed_while_reading = False
        self.read_threads = []
        self._observation_lock = Lock()
        super().__init__(name=name, **kwargs)

    def read(self):
        with self._observation_lock:
            self.read_calls += 1
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
            self.read_threads.append(get_ident())
        self.read_finished.clear()
        self.read_started.set()
        try:
            self.read_release.wait()
            if self.read_error is not None:
                raise self.read_error
            return Signal.read(self)
        finally:
            with self._observation_lock:
                self.active_reads -= 1
            self.read_finished.set()

    def subscribe(self, callback, event_type=None, run=True):
        self.subscribe_finished.clear()
        token = super().subscribe(callback, event_type=event_type, run=run)
        self.subscribe_started.set()
        self.subscribe_release.wait()
        self.subscribe_finished.set()
        return token

    def destroy(self):
        with self._observation_lock:
            self.destroyed_while_reading |= self.active_reads > 0
        super().destroy()


class ObservedAsyncSignalR(MutationGuard, SignalR):
    """Native async cache with public registration/removal observations.

    ``subscribe_calls``/``clear_calls`` count attempts; ``active_callbacks``
    tracks successful registrations without retaining removed callbacks.
    ``subscribed`` and ``cleared`` are threading Events for test-thread waits.
    No instance attribute masks a reserved async protocol method.
    """

    def __init__(self, backend, *, name=""):
        self.subscribe_calls = 0
        self.clear_calls = 0
        self.active_callbacks = set()
        self.subscribed = Event()
        self.cleared = Event()
        super().__init__(backend=backend, name=name)

    def subscribe_reading(self, function):
        self.subscribe_calls += 1
        super().subscribe_reading(function)
        self.active_callbacks.add(function)
        self.subscribed.set()

    subscribe = subscribe_reading

    def clear_sub(self, function):
        self.clear_calls += 1
        super().clear_sub(function)
        self.active_callbacks.discard(function)
        self.cleared.set()


class GuardedAsyncSignalW(MutationGuard, SignalW):
    """Discoverable pure-soft write-only signal that refuses attempted writes."""


class AsyncDevice(MutationGuard, StandardReadable):
    """A readable temperature, a write-only child, and literal map keys.

    ``temperature_setter(value)`` is a test-owned notification source and must
    run on the device's application loop. ``label_setters`` provides equivalent
    setters by the original map keys. Native temperature names use ``-``, while
    classic ones use ``_``. Only temperature participates in the root read.
    The factory-produced temperature is an unmodified SignalR: tests requiring
    stage guards on it should use a scoped class-level monkeypatch. The root,
    write-only child and observed map signals have their own mutation guards.
    """

    def __init__(self, name="", initial_value=1.234, units="mm", precision=3):
        self.temperature, self.temperature_setter = soft_signal_r_and_setter(
            float, initial_value, units=units, precision=precision
        )
        self.write_only = GuardedAsyncSignalW(SoftSignalBackend(float, 0.0))
        labels = {}
        self.label_setters = {}
        for key, value in (("a.b", 1), ("a~/b", 2)):
            backend = SoftSignalBackend(int, value)
            labels[key] = ObservedAsyncSignalR(backend)
            self.label_setters[key] = backend.set_value
        self.labels = DeviceMap(labels)
        self.add_readables([self.temperature], StandardReadableFormat.HINTED_UNCACHED_SIGNAL)
        super().__init__(name=name)


class StateBackedAsyncDevice(MutationGuard, StandardReadable):
    """A pure-soft getter whose state can change without a monitor notification.

    Set ``state_value`` on the owning loop without using ``temperature_setter``
    to leave the native monitor cache stale. An explicit leaf read must fetch
    the changed state. ``getter_calls`` observes real getter invocations.
    ``temperature_setter`` can emit fixture-owned monitor notifications; it
    intentionally does not change ``state_value``. There is no polling task.
    """

    def __init__(self, name="", initial_value=1.234):
        self.state_value = initial_value
        self.getter_calls = 0
        backend = SoftSignalBackend(float, initial_value, getter=self._get_temperature)
        self.temperature = ObservedAsyncSignalR(backend)
        self.temperature_setter = backend.set_value
        self.add_readables([self.temperature], StandardReadableFormat.HINTED_UNCACHED_SIGNAL)
        super().__init__(name=name)

    def _get_temperature(self):
        self.getter_calls += 1
        return self.state_value
