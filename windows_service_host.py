import argparse
import ctypes
import hashlib
import logging
import os
import signal
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from production_server import (
    ProductionConfigError,
    configure_service_logging,
    load_runtime_config,
    prepare_application,
)


ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF


class ServiceHostError(RuntimeError):
    pass


def _event_name(config_path):
    resolved = Path(config_path).expanduser().resolve(strict=True)
    identity = os.path.normcase(str(resolved)).encode('utf-8')
    suffix = hashlib.sha256(identity).hexdigest()[:24]
    return f'Local\\ToRtkoERP-{suffix}'


class _WindowsEvent:
    def __init__(self, handle):
        self.handle = handle
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        self.kernel32.SetEvent.restype = wintypes.BOOL

    @classmethod
    def create(cls, name):
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateEventW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateEventW.restype = wintypes.HANDLE
        handle = kernel32.CreateEventW(None, True, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise ServiceHostError('A service process for this config is already running.')
        return cls(handle)

    @classmethod
    def open_for_signal(cls, name):
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenEventW.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.OpenEventW.restype = wintypes.HANDLE
        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
        if not handle:
            raise ServiceHostError(
                'The ERP service stop event is not available; is the service running?'
            )
        return cls(handle)

    def wait(self):
        result = self.kernel32.WaitForSingleObject(self.handle, INFINITE)
        if result != WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())

    def signal(self):
        if not self.kernel32.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class _TrackedIterable:
    def __init__(self, iterable, completed):
        self.iterable = iter(iterable)
        self.original = iterable
        self.completed = completed
        self.released = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterable)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.released:
            return
        self.released = True
        try:
            close = getattr(self.original, 'close', None)
            if close is not None:
                close()
        finally:
            self.completed()


class DrainMiddleware:
    def __init__(self, application):
        self.application = application
        self._condition = threading.Condition()
        self._stopping = False
        self._active = 0

    def __call__(self, environ, start_response):
        with self._condition:
            if self._stopping:
                body = b'Service shutting down.'
                start_response(
                    '503 Service Unavailable',
                    [
                        ('Content-Type', 'text/plain; charset=utf-8'),
                        ('Content-Length', str(len(body))),
                        ('Connection', 'close'),
                    ],
                )
                return [body]
            self._active += 1

        def tracked_start_response(status, headers, exc_info=None):
            with self._condition:
                stopping = self._stopping
            if stopping and not any(name.lower() == 'connection' for name, _ in headers):
                headers = list(headers) + [('Connection', 'close')]
            return start_response(status, headers, exc_info)

        try:
            response = self.application(environ, tracked_start_response)
        except BaseException:
            self._complete()
            raise
        return _TrackedIterable(response, self._complete)

    def stop_accepting(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()

    def _complete(self):
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def wait_for_drain(self, timeout, pending_tasks):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active or pending_tasks():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.1))
            return True


def _pending_task_count(dispatcher):
    with dispatcher.lock:
        return len(dispatcher.queue)


def _stop_service(config_path):
    event = _WindowsEvent.open_for_signal(_event_name(config_path))
    try:
        event.signal()
    finally:
        event.close()
    return 0


def _run_service(config, test_mode=False):
    if test_mode:
        if config.mode != 'test' or config.port != 5053 or config.host != '127.0.0.1':
            raise ProductionConfigError(
                'Test service mode requires mode=test and binding 127.0.0.1:5053.'
            )
    elif config.mode != 'production':
        raise ProductionConfigError(
            'Windows service startup requires explicit production configuration.'
        )

    configure_service_logging(config)
    logger = logging.getLogger('windows_service')
    logger.info(
        'Service startup validation passed; database and schema checks completed.'
    )
    application = DrainMiddleware(prepare_application(config))

    from waitress import create_server

    options = {
        'host': config.host,
        'port': config.port,
        'threads': config.threads,
        'channel_timeout': 10,
        'clear_untrusted_proxy_headers': True,
    }
    if config.proxy_enabled:
        options.update(
            trusted_proxy=str(config.proxy_networks[0].network_address),
            trusted_proxy_count=config.proxy_hops,
            trusted_proxy_headers=(
                'x-forwarded-for',
                'x-forwarded-proto',
                'x-forwarded-host',
                'x-forwarded-port',
            ),
        )

    server = create_server(application, **options)
    stop_event = _WindowsEvent.create(_event_name(config.config_path))
    stop_state = {'requested': False, 'drained': False, 'worker_shutdown': False}
    watcher_errors = []

    def pending_tasks():
        return _pending_task_count(server.task_dispatcher)

    def request_shutdown():
        stop_state['requested'] = True
        logger.info('Graceful stop requested; closing the listener.')
        application.stop_accepting()
        server.close()
        stop_state['drained'] = application.wait_for_drain(
            config.drain_timeout_seconds,
            pending_tasks,
        )
        if not stop_state['drained']:
            logger.error(
                'Graceful drain exceeded %s seconds; active requests remain.',
                config.drain_timeout_seconds,
            )
            watcher_errors.append('drain-timeout')
            return
        stop_state['worker_shutdown'] = server.task_dispatcher.shutdown(
            cancel_pending=True,
            timeout=config.drain_timeout_seconds,
        )
        if not stop_state['worker_shutdown']:
            logger.error('Waitress workers did not stop within the drain timeout.')
            watcher_errors.append('worker-shutdown-timeout')
            return
        logger.info('In-flight requests completed; Waitress workers stopped.')

    def watch_stop():
        try:
            stop_event.wait()
            request_shutdown()
        except BaseException as exc:
            logger.error('Service stop handler failed: %s', type(exc).__name__)
            watcher_errors.append(type(exc).__name__)

    watcher = threading.Thread(
        target=watch_stop,
        name='erp-service-stop-watcher',
        daemon=True,
    )
    watcher.start()

    def signal_stop(_signum, _frame):
        stop_event.signal()

    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, signal_stop)
        except (OSError, ValueError):
            continue

    logger.info('Waitress listening only on %s:%s.', config.host, config.port)
    try:
        server.run()
    finally:
        if stop_state['requested']:
            watcher.join(timeout=config.drain_timeout_seconds + 5)
        stop_event.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    if stop_state['requested'] and stop_state['drained'] and stop_state['worker_shutdown'] and not watcher_errors:
        logger.info('Graceful shutdown completed.')
        return 0
    logger.error('Waitress server loop exited without a clean service stop.')
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Windows service host for the ERP.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument(
        '--test-mode',
        action='store_true',
        help='Allow only isolated test config bound to 127.0.0.1:5053.',
    )
    args = parser.parse_args(argv)
    try:
        if args.stop:
            return _stop_service(args.config)
        config = load_runtime_config(args.config)
        return _run_service(config, test_mode=args.test_mode)
    except (ProductionConfigError, ServiceHostError, OSError) as exc:
        print(f'SERVICE_STARTUP_OR_CONTROL_FAILED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
