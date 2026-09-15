"""Owned devices, a frozen resource namespace, and bounded classic I/O."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import ophyd
from ophyd import Device, Signal, do_not_wait_for_lazy_connection
from ophyd_async.core import Device as AsyncDevice
from ophyd_async.core import SignalR

from .config import ServiceConfig
from .errors import ServiceError
from .serialization import snapshot_json

logger = logging.getLogger(__name__)
T = TypeVar("T")


def failure(code: str, path: str | None, exc: Exception) -> ServiceError:
    logger.error("%s for %s", code, path, exc_info=(type(exc), exc, exc.__traceback__))
    return ServiceError(code, f"{type(exc).__name__}: {exc}", path)


@dataclass(frozen=True)
class ResourceInfo:
    path: str
    backend: Literal["ophyd", "ophyd-async"]
    readable: bool
    monitorable: bool
    children: list[str]


@dataclass
class _Resource:
    info: ResourceInfo
    root: str
    attributes: tuple[str, ...] = ()
    instance: Any = None


def _child_path(parent: str, child: str) -> str:
    return f"{parent}/{child.replace('~', '~0').replace('/', '~1')}"


def _initialize_worker() -> None:
    if ophyd.get_cl().name == "pyepics":
        from epics.ca import use_initial_context

        use_initial_context()


class DeviceRegistry:
    """One application owns each configured root for its entire lifespan.

    Response cancellation never cancels a submitted classic job. Its root lock
    and capacity slot are released only by the actual completion callback.
    """

    def __init__(self, config: ServiceConfig):
        self.config = config
        self.roots: dict[str, object] = {}
        self.loop: asyncio.AbstractEventLoop
        self._catalog: dict[str, _Resource] = {}
        self._locks = {root: asyncio.Lock() for root in config.devices}
        self._capacity = asyncio.Semaphore(4)
        self._jobs: set[asyncio.Future] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._state = "new"
        self._close_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._state != "new":
            raise RuntimeError("A device registry can only be started once")
        self.loop = asyncio.get_running_loop()
        self._state = "starting"
        self._executor = ThreadPoolExecutor(
            max_workers=4, initializer=_initialize_worker, thread_name_prefix="ophyd"
        )
        root = ""
        class_path = ""
        try:
            for root, spec in self.config.devices.items():
                class_path = spec.class_path
                driver = spec.resolve_class()
                if issubclass(driver, (Device, Signal)):

                    def construct(root=root, spec=spec, driver=driver):
                        instance = driver(name=root, **spec.kwargs)
                        # Own the object before its first potentially failing connection.
                        self.roots[root] = instance
                        self._wait_connected(instance)
                        self._index_classic(root, root, driver, (), instance)

                    await self.classic(root, construct, owned=True)
                else:
                    instance = driver(name=root, **spec.kwargs)
                    self.roots[root] = instance
                    await asyncio.wait_for(
                        instance.connect(mock=False, timeout=self.config.connect_timeout),
                        self.config.connect_timeout,
                    )
                    self._index_async(root, root, instance)
            self._state = "ready"
        except BaseException as exc:
            try:
                await self.close()
            except Exception as cleanup_error:
                logger.exception("Cleanup after failed startup also failed")
                exc.add_note(f"Cleanup failure: {cleanup_error}")
            if isinstance(exc, Exception):
                raise RuntimeError(
                    f"Failed to start root {root!r} ({class_path}): {type(exc).__name__}: {exc}"
                ) from exc
            raise

    def _wait_connected(self, instance: Device | Signal) -> None:
        if isinstance(instance, Device):
            instance.wait_for_connection(all_signals=False, timeout=self.config.connect_timeout)
        else:
            instance.wait_for_connection(timeout=self.config.connect_timeout)

    def _index_classic(
        self, root: str, path: str, driver: type, attributes: tuple[str, ...], instance: Any = None
    ) -> None:
        children = []
        if issubclass(driver, Device):
            for name in driver.component_names:
                component = getattr(driver, name)
                child_path = _child_path(path, name)
                children.append(child_path)
                self._index_classic(root, child_path, component.cls, (*attributes, name))
        self._catalog[path] = _Resource(
            ResourceInfo(
                path, "ophyd", issubclass(driver, (Device, Signal)), issubclass(driver, Signal), children
            ),
            root,
            attributes,
            instance,
        )

    def _index_async(self, root: str, path: str, instance: AsyncDevice) -> None:
        children = []
        for name, child in instance.children():
            child_path = _child_path(path, name)
            children.append(child_path)
            self._index_async(root, child_path, child)
        self._catalog[path] = _Resource(
            ResourceInfo(
                path,
                "ophyd-async",
                callable(getattr(instance, "read", None)) and callable(getattr(instance, "describe", None)),
                isinstance(instance, SignalR),
                children,
            ),
            root,
            instance=instance,
        )

    def resource(self, path: str) -> _Resource:
        try:
            return self._catalog[path]
        except KeyError:
            raise ServiceError("not_found", "No resource at this path", path) from None

    def info(self, path: str) -> ResourceInfo:
        return self.resource(path).info

    def _resolve_classic(self, resource: _Resource) -> Any:
        if resource.instance is None:
            instance = self.roots[resource.root]
            for attribute in resource.attributes:
                with do_not_wait_for_lazy_connection(instance):
                    instance = getattr(instance, attribute)
            self._wait_connected(instance)
            resource.instance = instance
        return resource.instance

    async def resolve(self, path: str) -> Any:
        resource = self.resource(path)
        if resource.instance is not None:
            self._require_ready()
            return resource.instance
        if resource.info.backend == "ophyd":
            try:
                return await self.classic(resource.root, lambda: self._resolve_classic(resource))
            except Exception as exc:
                raise failure("backend_error", path, exc) from exc
        self._require_ready()
        return resource.instance

    def _require_ready(self) -> None:
        if self._state != "ready":
            raise RuntimeError("The device registry is not accepting work")

    async def classic(self, root: str, function: Callable[[], T], *, owned: bool = False) -> T:
        """Submit only after owning both a root lock and a worker-capacity slot.

        ``owned`` is for already-owned registration/retirement and teardown work,
        which must finish even after the service stops accepting requests.
        """
        if not owned:
            self._require_ready()
        lock = self._locks[root]
        await lock.acquire()
        try:
            await self._capacity.acquire()
        except BaseException:
            lock.release()
            raise
        try:
            if not owned:
                self._require_ready()
            job = self.loop.run_in_executor(self._executor, function)
        except BaseException:
            self._capacity.release()
            lock.release()
            raise
        self._jobs.add(job)
        detached = False

        def report(error: BaseException) -> None:
            logger.error(
                "Abandoned classic operation failed for %s",
                root,
                exc_info=(type(error), error, error.__traceback__),
            )

        def finished(completed: asyncio.Future) -> None:
            self._jobs.discard(completed)
            lock.release()
            self._capacity.release()
            error = completed.exception()
            if error is not None and detached:
                report(error)

        job.add_done_callback(finished)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            detached = True
            if job.done() and (error := job.exception()) is not None:
                report(error)
            raise

    @asynccontextmanager
    async def deadline(self, path: str) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self.config.read_timeout):
                yield
        except TimeoutError as exc:
            raise ServiceError("timeout", "Operation exceeded the service read deadline", path) from exc

    async def read(self, path: str) -> dict[str, Any]:
        return await self._reading_operation(path, "read")

    async def describe(self, path: str) -> dict[str, Any]:
        return await self._reading_operation(path, "describe")

    async def _reading_operation(self, path: str, operation: Literal["read", "describe"]) -> dict[str, Any]:
        resource = self.resource(path)
        if not resource.info.readable:
            raise ServiceError("not_readable", "Resource does not provide native read and describe", path)
        async with self.deadline(path):
            try:
                if resource.info.backend == "ophyd":

                    def call():
                        try:
                            instance = self._resolve_classic(resource)
                            value = getattr(instance, operation)()
                        except Exception as exc:
                            raise failure("backend_error", path, exc) from exc
                        return self._snapshot(path, value)

                    return await self.classic(resource.root, call)
                self._require_ready()
                instance = resource.instance
                if operation == "read" and isinstance(instance, SignalR):
                    value = await instance.read(cached=False)
                else:
                    value = await getattr(instance, operation)()
            except ServiceError:
                raise
            except Exception as exc:
                raise failure("backend_error", path, exc) from exc
            return self._snapshot(path, value)

    @staticmethod
    def _snapshot(path: str, value: Any) -> Any:
        try:
            return snapshot_json(value)
        except Exception as exc:
            raise failure("serialization_error", path, exc) from exc

    def stop_accepting(self) -> None:
        self._state = "closing"

    async def close(self) -> None:
        if self._close_task is None:
            self.stop_accepting()
            self._close_task = asyncio.create_task(self._close(), name="device-registry-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        errors = []
        # Do not cancel actual I/O, including jobs whose requests have timed out.
        while self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
        for root, instance in self.roots.items():
            if isinstance(instance, (Device, Signal)):
                try:
                    await self.classic(root, instance.destroy, owned=True)
                except Exception as exc:
                    logger.exception("Could not destroy root %s", root)
                    errors.append(exc)
        self._catalog.clear()
        self.roots.clear()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self._state = "closed"
        if errors:
            raise ExceptionGroup("Device cleanup failed", errors)
