import asyncio
import time
import json
import logging
import httpx
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List, Union
from collections import deque
import threading
from flask import request as flask_request, g as flask_g, jsonify, Flask
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pathlib import Path
import weakref
import atexit
from contextlib import asynccontextmanager

# Setup logger with async-safe configuration
log_path = Path("logs/api_monitor.log")
log_path.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("api_monitor")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(log_path)
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)


@dataclass
class EndpointStats:
    """
    Represents statistics for a single API endpoint.

    Attributes:
        _path (str): The normalized path of the endpoint (e.g., "/users/{user_id}").
        type (str): The HTTP method(s) supported by the endpoint (e.g., "get", "post", "get,post").
        request_url (str): The full URL of the most recent request to this endpoint.
        response_time (float): The response time of the most recent request to this endpoint in seconds.
        schema (Dict): The OpenAPI/Swagger schema definition for this endpoint. (Currently not populated)
        security_schemes (Dict): Security schemes applicable to this endpoint. (Currently not populated)
    """
    _path: str
    type: str
    request_url: str = ""
    response_time: float = 0.0
    schema: Dict = field(default_factory=dict)
    security_schemes: Dict = field(default_factory=dict)


@dataclass
class LogEntry:
    """
    Represents a single API request and its corresponding response or error.

    Attributes:
        method (str): The HTTP method of the request (e.g., "get", "post").
        path (str): The requested path (e.g., "/api/items/123").
        full_url (str): The complete URL of the request.
        timestamp (str): The timestamp when the request started, formatted as "YYYY-MM-DD HH:MM:SS".
        framework (str): The web framework used (e.g., "flask", "fastapi").
        status_code (Optional[int]): The HTTP status code of the response. None if an error occurred before response.
        process_time (Optional[float]): The total time taken to process the request in seconds.
        request_body (Any): The parsed request body. Can be dict, string, or None.
        response (Any): The parsed response body. Can be dict, string, or None.
        error (Optional[Dict]): Details of an error if one occurred during request processing.
            Contains 'type', 'message', and 'traceback'.
    """
    method: str
    path: str
    full_url: str
    timestamp: str
    framework: str
    status_code: Optional[int] = None
    process_time: Optional[float] = None
    request_body: Any = None
    response: Any = None
    error: Optional[Dict] = None


class AsyncBuffer:
    """
    A thread-safe asynchronous buffer for storing LogEntry items.

    It uses a `collections.deque` for efficient appends and drains, and a `threading.Lock`
    to ensure thread safety during concurrent access. If the buffer reaches its
    `max_size`, new items are dropped, and a warning is logged.
    """

    def __init__(self, max_size: int = 1000):
        """
        Initializes the AsyncBuffer.

        Args:
            max_size (int): The maximum number of items the buffer can hold.
                            If exceeded, older items are dropped.
        """
        self.buffer = deque(maxlen=max_size)
        self.lock = threading.Lock()
        self._dropped_count = 0

    def add(self, item: LogEntry) -> bool:
        """
        Adds an item to the buffer in a thread-safe manner.

        If the buffer is full, the item is dropped, and the dropped count is incremented.

        Args:
            item (LogEntry): The log entry to add.

        Returns:
            bool: True if the item was added successfully, False if it was dropped.
        """
        try:
            with self.lock:
                if len(self.buffer) >= self.buffer.maxlen:
                    self._dropped_count += 1
                    return False
                self.buffer.append(item)
                return True
        except Exception as e:
            logger.warning(f"Buffer add failed: {e}")
            return False

    def drain(self) -> List[LogEntry]:
        """
        Drains all current items from the buffer and clears it in a thread-safe manner.

        Resets the dropped item count.

        Returns:
            List[LogEntry]: A list of all log entries that were in the buffer.
        """
        with self.lock:
            items = list(self.buffer)
            self.buffer.clear()
            dropped = self._dropped_count
            self._dropped_count = 0

            if dropped > 0:
                logger.warning(f"Dropped {dropped} log entries due to buffer overflow")

            return items


class CircuitBreaker:
    """
    A lightweight circuit breaker implementation to protect against
    repeated requests to a failing external service.

    It tracks consecutive failures and "opens" the circuit if a threshold is met,
    preventing further requests for a defined recovery timeout period.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        """
        Initializes the CircuitBreaker.

        Args:
            failure_threshold (int): The number of consecutive failures before the circuit opens.
            recovery_timeout (int): The time in seconds after which the circuit attempts to close
                                    (enters half-open state for a test request).
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failure_count = 0
        self._last_failure = 0  # Timestamp of the last failure
        self._is_open = False
        self._lock = threading.Lock() # Ensures thread-safe access to circuit state

    def is_available(self) -> bool:
        """
        Checks if the circuit is currently closed (available) or open (unavailable).

        If the circuit is open and the recovery timeout has passed, it transitions
        to a half-open state, allowing one test request.

        Returns:
            bool: True if the external service is considered available, False otherwise.
        """
        with self._lock:
            if self._is_open:
                if (time.time() - self._last_failure) > self.recovery_timeout:
                    self._is_open = False  # Transition to half-open implicitly by allowing next request
                    self._failure_count = 0 # Reset failure count on half-open/close
                    return True
                return False
            return True

    def record_failure(self):
        """
        Records a single failure. If the failure threshold is met, the circuit opens.
        """
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._is_open = True
                self._last_failure = time.time()
                logger.warning("Circuit breaker opened - external requests paused")

    def record_success(self):
        """
        Records a single success. Resets the failure count and closes the circuit.
        """
        with self._lock:
            self._failure_count = 0
            self._is_open = False


class BackgroundProcessor:
    """
    Manages background tasks for API monitoring, including sending log entries
    and endpoint health data to a configured DSN (Data Source Name).

    It runs an asyncio event loop in a separate thread if not already in an async context,
    and uses a circuit breaker to manage external service reliability.
    """

    def __init__(self, dsn: str, framework: str):
        """
        Initializes the BackgroundProcessor.

        Args:
            dsn (str): The Data Source Name for the monitoring service endpoint.
            framework (str): The detected web framework ("flask" or "fastapi").
        """
        self.dsn = dsn
        self.framework = framework
        self.buffer = AsyncBuffer(max_size=1000)
        self.endpoint_data: List[EndpointStats] = []
        self.circuit_breaker = CircuitBreaker()
        self.client: Optional[httpx.AsyncClient] = None  # HTTP client for sending data
        self.task: Optional[asyncio.Task] = None  # The asyncio task for the processing loop
        self.shutdown_event: Optional[asyncio.Event] = None  # Event to signal shutdown
        self._endpoint_lock = threading.Lock()  # Protects access to endpoint_data
        self._started = False  # Flag to indicate if the processor has started
        self._start_lock = threading.Lock()  # Protects the startup logic

        # Register cleanup to ensure graceful shutdown on program exit
        atexit.register(self.cleanup)

    async def start(self):
        """
        Starts the background processor's asyncio loop and HTTP client.
        Ensures that the processor is started only once.
        """
        with self._start_lock:
            if self._started:
                return
            self._started = True

        self.shutdown_event = asyncio.Event()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(3.0),  # Overall request timeout
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2) # Connection limits
        )

        # Create the main processing loop task
        self.task = asyncio.create_task(self._processor_loop())
        logger.info("Background processor started")

    def ensure_started(self):
        """
        Ensures the background processor is running.
        This method is designed to be called from both synchronous and asynchronous contexts.
        It attempts to start the processor within the current event loop if available,
        otherwise, it starts a new event loop in a separate daemon thread.
        """
        if self._started:
            return

        try:
            # Attempt to get the current running event loop
            loop = asyncio.get_running_loop()
            if loop and not loop.is_closed():
                # If an event loop is running, schedule the startup as an async task
                asyncio.create_task(self.start())
            else:
                # No running event loop found, start it in a new thread
                self._start_in_thread()
        except RuntimeError:
            # RuntimeError typically means no event loop is set for the current OS thread
            self._start_in_thread()

    def _start_in_thread(self):
        """
        Starts the background processor's event loop in a new daemon thread.
        This is used when `ensure_started` is called from a synchronous context
        where no asyncio event loop is already running.
        """
        if self._started:
            return

        def run_processor():
            """Function executed by the new thread to run the asyncio loop."""
            try:
                # Create a new event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # Start the processor and run the loop indefinitely
                loop.run_until_complete(self.start())
                loop.run_forever()
            except Exception as e:
                logger.error(f"Background processor thread error: {e}")
            finally:
                # Close the loop cleanly when the thread exits
                try:
                    loop.close()
                except Exception:
                    pass

        # Create and start a daemon thread so it doesn't block program exit
        thread = threading.Thread(target=run_processor, daemon=True)
        thread.start()
        logger.info("Background processor started in thread")

    async def stop(self):
        """
        Stops the background processor, signals its task to shut down,
        and closes the HTTP client. Provides a timeout for graceful shutdown.
        """
        if not self._started or self.task is None:
            return

        if self.shutdown_event:
            self.shutdown_event.set()  # Signal the processor loop to stop

        try:
            # Wait for the processor task to complete, with a timeout
            await asyncio.wait_for(self.task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Background processor shutdown timeout")
            self.task.cancel()  # Force cancel if it doesn't stop in time

        if self.client:
            await self.client.aclose()  # Close the HTTP client
            self.client = None

        self.task = None
        self._started = False
        logger.info("Background processor stopped")

    def cleanup(self):
        """
        Registered with `atexit` to ensure that the processor is stopped
        when the Python interpreter is shutting down. This handles cases
        where `stop` might not be explicitly called.
        """
        if self.task and not self.task.done():
            try:
                # Run the async stop method in the current event loop context if any,
                # or create a new one if not, to ensure cleanup.
                asyncio.run(self.stop())
            except Exception:
                # Ignore errors during final cleanup
                pass

    async def _processor_loop(self):
        """
        The main asynchronous loop for the background processor.
        It periodically drains log entries from the buffer and sends them
        in batches, and also sends endpoint health data at a longer interval.
        It gracefully handles shutdown signals.
        """
        batch_interval = 2.0  # Frequency to send log batches
        endpoint_interval = 30.0  # Frequency to send endpoint health data
        last_endpoint_send = 0  # Timestamp of the last endpoint data send

        try:
            while not self.shutdown_event.is_set():
                try:
                    # Drain and send any buffered log entries
                    entries = self.buffer.drain()
                    if entries:
                        await self._send_logs_batch(entries)

                    # Send endpoint data periodically
                    now = time.time()
                    if now - last_endpoint_send >= endpoint_interval:
                        await self._send_endpoint_data()
                        last_endpoint_send = now

                    # Wait for the next batch interval or until shutdown is signaled
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=batch_interval
                    )

                except asyncio.TimeoutError:
                    # This is expected behavior when shutdown_event does not fire
                    # within the timeout, meaning it's time to process again.
                    continue
                except Exception as e:
                    # Log any unexpected errors within the loop and pause briefly
                    logger.error(f"Processor loop error: {e}")
                    await asyncio.sleep(1.0)  # Brief pause on error

        except asyncio.CancelledError:
            # This exception is raised when the task is cancelled, e.g., during stop()
            logger.info("Processor loop cancelled")
        finally:
            # Ensure any remaining log entries are sent before final shutdown
            entries = self.buffer.drain()
            if entries:
                try:
                    await self._send_logs_batch(entries)
                except Exception as e:
                    logger.error(f"Final batch send failed: {e}")

    async def _send_logs_batch(self, entries: List[LogEntry]):
        """
        Sends a batch of collected log entries to the monitoring service.
        Respects the circuit breaker state to avoid hammering a failing service.

        Args:
            entries (List[LogEntry]): A list of log entries to send.
        """
        if not self.circuit_breaker.is_available():
            logger.debug("Circuit breaker open - dropping log batch")
            return

        try:
            # Convert LogEntry objects to a dictionary format suitable for JSON payload
            logs = [self._entry_to_dict(entry) for entry in entries]

            response = await self.client.post(
                "https://dev.viewcurry.com/beacon/gatekeeper/upload/save-api-response",
                json={"dsn": self.dsn, "response": logs}
            )
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
            self.circuit_breaker.record_success()  # Record success if request was good

        except Exception as e:
            self.circuit_breaker.record_failure()  # Record failure if an error occurred
            logger.warning(f"Failed to send log batch: {e}")

    async def _send_endpoint_data(self):
        """
        Sends aggregated endpoint health data to the monitoring service.
        Respects the circuit breaker state.
        """
        if not self.circuit_breaker.is_available():
            return

        try:
            with self._endpoint_lock:
                # Convert EndpointStats objects to dictionary format
                endpoint_dicts = [self._endpoint_to_dict(ep) for ep in self.endpoint_data]

            response = await self.client.post(
                "https://dev.viewcurry.com/beacon/gatekeeper/upload/send-api-health-data",
                json={
                    "dsn": self.dsn,
                    "framework": self.framework,
                    "endpoints": endpoint_dicts,
                }
            )
            response.raise_for_status()

        except Exception as e:
            logger.warning(f"Failed to send endpoint data: {e}")

    def add_log_entry(self, entry: LogEntry):
        """
        Adds a single log entry to the internal buffer.
        This method is non-blocking and will lazily start the background processor
        if it's not already running.

        Args:
            entry (LogEntry): The log entry to add.
        """
        # Ensure the background processor is active to consume entries
        self.ensure_started()

        if not self.buffer.add(entry):
            # Log a warning if the buffer is full and the entry was dropped
            logger.warning("Log buffer full - entry dropped")

    def update_endpoint_metrics(self, path: str, method: str, response_time: float, url: str):
        """
        Updates the response time and request URL for a specific endpoint.
        This method is non-blocking.

        Args:
            path (str): The normalized path of the endpoint.
            method (str): The HTTP method of the request.
            response_time (float): The processing time for the request.
            url (str): The full URL of the request.
        """
        with self._endpoint_lock:
            # Iterate through existing endpoint data to find and update the relevant entry
            for endpoint in self.endpoint_data:
                if endpoint._path == path and endpoint.type == method:
                    endpoint.request_url = url
                    endpoint.response_time = response_time
                    break

    def add_endpoint(self, endpoint: EndpointStats):
        """
        Adds a new endpoint to be tracked for health metrics.

        Args:
            endpoint (EndpointStats): The endpoint statistics object to add.
        """
        with self._endpoint_lock:
            self.endpoint_data.append(endpoint)

    @staticmethod
    def _entry_to_dict(entry: LogEntry) -> Dict:
        """
        Converts a LogEntry dataclass instance into a dictionary.

        Args:
            entry (LogEntry): The log entry to convert.

        Returns:
            Dict: A dictionary representation of the log entry.
        """
        return {
            "method": entry.method,
            "path": entry.path,
            "full_url": entry.full_url,
            "timestamp": entry.timestamp,
            "framework": entry.framework,
            "status_code": entry.status_code,
            "process_time": entry.process_time,
            "request_body": entry.request_body,
            "response": entry.response,
            "error": entry.error
        }

    @staticmethod
    def _endpoint_to_dict(endpoint: EndpointStats) -> Dict:
        """
        Converts an EndpointStats dataclass instance into a dictionary.

        Args:
            endpoint (EndpointStats): The endpoint statistics to convert.

        Returns:
            Dict: A dictionary representation of the endpoint statistics.
        """
        return {
            "_path": endpoint._path,
            "type": endpoint.type,
            "request_url": endpoint.request_url,
            "response_time": endpoint.response_time,
            "schema": endpoint.schema,
            "security_schemes": endpoint.security_schemes
        }


class ApiHealthCheckMiddleware:
    """
    Non-blocking API Monitoring Middleware for Flask and FastAPI applications.

    This middleware intercepts incoming requests and outgoing responses
    to capture performance metrics, request/response bodies, and errors.
    It dispatches this data to a `BackgroundProcessor` for asynchronous
    transmission to a monitoring service.
    """

    # Class-level registry to ensure a single `BackgroundProcessor` instance
    # per DSN across multiple `ApiHealthCheckMiddleware` instances.
    _processors: Dict[str, BackgroundProcessor] = {}
    _processor_lock = threading.Lock() # Protects access to the _processors dictionary

    def __init__(self, app=None, dsn: str = "", framework: str = "auto"):
        """
        Initializes the ApiHealthCheckMiddleware.

        Args:
            app: The web application instance (Flask or FastAPI).
            dsn (str): The Data Source Name for the monitoring service.
            framework (str): Explicitly specify the framework ("flask", "fastapi")
                             or "auto" to detect it.
        """
        self.dsn = dsn
        # Auto-detect framework if not explicitly provided
        self.framework = self._detect_framework(app) if framework == "auto" else framework
        # Get or create a singleton BackgroundProcessor for the given DSN
        self.processor = self._get_or_create_processor()

        if app is not None:
            # Initialize the middleware specific to the detected/specified framework
            self.init_app(app)

    def _get_or_create_processor(self) -> BackgroundProcessor:
        """
        Retrieves an existing `BackgroundProcessor` instance for the DSN
        or creates a new one if it doesn't exist. Ensures singleton per DSN.
        """
        with self._processor_lock:
            if self.dsn not in self._processors:
                self._processors[self.dsn] = BackgroundProcessor(self.dsn, self.framework)
            return self._processors[self.dsn]

    def _detect_framework(self, app) -> str:
        """
        Attempts to auto-detect the web framework based on the application object's attributes.

        Args:
            app: The web application instance.

        Returns:
            str: "fastapi" or "flask".

        Raises:
            ValueError: If the framework cannot be detected.
        """
        if hasattr(app, 'add_middleware'):  # Characteristic of FastAPI applications
            return "fastapi"
        elif hasattr(app, 'before_request'):  # Characteristic of Flask applications
            return "flask"
        raise ValueError("Could not detect framework type")

    def init_app(self, app):
        """
        Initializes the middleware by applying the appropriate hooks
        or middleware adapters based on the detected framework.

        Args:
            app: The web application instance.

        Raises:
            ValueError: If the detected framework is not supported.
        """
        if self.framework == "fastapi":
            self._init_fastapi(app)
        elif self.framework == "flask":
            self._init_flask(app)
        else:
            raise ValueError(f"Unsupported framework: {self.framework}")

        # The processor starts lazily on the first request, no explicit start here.

    # FastAPI Implementation
    def _init_fastapi(self, app):
        """
        Configures the FastAPI application to use the monitoring middleware
        by adding `FastAPIMiddlewareAdapter`.
        """
        # FastAPI middlewares are added via app.add_middleware
        app.add_middleware(FastAPIMiddlewareAdapter, monitor=self)

    async def process_fastapi_request(self, request: Request, call_next):
        """
        Handles the FastAPI request/response cycle for monitoring.
        This method acts as the core logic for the FastAPI middleware.

        It captures request details, allows the request to proceed, captures
        response details or errors, and then dispatches a single consolidated
        log entry to the background processor.
        """
        # Ensure the background processor is running, starting it if necessary.
        if not self.processor._started:
            await self.processor.start()

        start_time = time.time()
        path = request.url.path
        method = request.method.lower()
        full_url = str(request.url)

        # Initialize a LogEntry at the beginning of the request.
        log_entry = LogEntry(
            method=method,
            path=path,
            full_url=full_url,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            framework="fastapi"
        )

        try:
            # Safely capture the request body. This involves reading the async stream,
            # which then needs to be rebuilt for subsequent middleware/route handling.
            request_body = await self._safe_capture_request_body(request)
            log_entry.request_body = request_body

            # Rebuild the request object with the captured body so it can be consumed again.
            request = self._rebuild_fastapi_request(request, request_body)

            # Pass the request to the next middleware or route handler in the chain.
            response = await call_next(request)

            # Safely capture the response body.
            response_body = await self._safe_capture_response_body(response)
            process_time = time.time() - start_time

            # Populate the LogEntry with response details.
            log_entry.status_code = response.status_code
            log_entry.process_time = round(process_time, 4)
            log_entry.response = self._safe_parse_response(response_body)

            # Submit the completed log entry to the background processor.
            self.processor.add_log_entry(log_entry)
            self.processor.update_endpoint_metrics(path, method, process_time, full_url)

            # Rebuild the response object with the captured body for the client.
            return self._rebuild_fastapi_response(response, response_body)

        except Exception as exc:
            # If an exception occurs, capture error details.
            process_time = time.time() - start_time
            log_entry.status_code = 500  # Default to 500 for unhandled exceptions
            log_entry.process_time = round(process_time, 4)
            log_entry.error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc()
            }

            # Submit the log entry with error details.
            self.processor.add_log_entry(log_entry)
            raise  # Re-raise the exception to allow FastAPI's error handling to proceed.

    # Flask Implementation
    def _init_flask(self, app):
        """
        Configures the Flask application by registering `before_request`,
        `after_request`, and `teardown_request` hooks.
        It also discovers existing API endpoints.
        """
        app.before_request(self._flask_before_request)
        app.after_request(self._flask_after_request)
        # `teardown_request` is used for final logging as it runs after
        # request context teardown, even if an exception occurred.
        app.teardown_request(self._flask_teardown_request)
        # The `_flask_error_handler` is included in the code but commented out
        # in the `init_flask` for a reason (to avoid duplicate logging).
        # If active, it would process errors separately.
        # app.errorhandler(Exception)(self._flask_error_handler)
        self._discover_flask_endpoints(app)

    def _flask_before_request(self):
        """
        Flask `before_request` handler.
        Initializes the start time and a new `LogEntry` object
        for the current request, storing them in Flask's `g` (global request context).
        Sets a flag `monitor_entry_sent` to False to control log submission.
        """
        flask_g.start_time = time.time()
        flask_g.monitor_entry = LogEntry(
            method=flask_request.method.lower(),
            path=flask_request.path,
            full_url=flask_request.url,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            framework="flask"
        )
        # This flag prevents `_flask_after_request` or `_flask_error_handler`
        # from sending a log if `_flask_teardown_request` is intended to be
        # the single point of submission (as suggested in previous analysis).
        flask_g.monitor_entry_sent = False

    def _flask_after_request(self, response):
        """
        Flask `after_request` handler.
        This hook runs after the view function, but before the response is sent to the client.
        It populates the `LogEntry` with response details for successful requests
        and submits it if it hasn't been sent by `teardown_request` (e.g., if no error occurred).
        """
        # Checks if the log entry for this request has already been sent
        # (e.g., by an error handler or a teardown function designed for single submission).
        if not hasattr(flask_g, 'monitor_entry_sent') or not flask_g.monitor_entry_sent:
            try:
                process_time = time.time() - flask_g.start_time
                # Use url_rule.rule for parameterized paths (e.g., /users/<int:id>), fallback to path
                path = flask_request.url_rule.rule if flask_request.url_rule else flask_request.path
                method = flask_request.method.lower()

                entry = flask_g.monitor_entry
                entry.status_code = response.status_code
                entry.process_time = round(process_time, 4)
                entry.request_body = self._safe_extract_flask_request_body()
                entry.response = self._safe_extract_flask_response_body(response)

                # Submit the completed log entry.
                self.processor.add_log_entry(entry)
                self.processor.update_endpoint_metrics(path, method, process_time, flask_request.url)
                flask_g.monitor_entry_sent = True # Mark as sent to prevent re-submission

            except Exception as e:
                logger.warning(f"Monitoring error in after_request: {e}")

        return response

    def _flask_teardown_request(self, exc):
        """
        Flask `teardown_request` handler.
        This function is executed after the request context is popped,
        regardless of whether an exception occurred during request processing.
        It's designed to be the definitive point for submitting the log entry,
        handling both success and error scenarios to ensure a single log per request.

        Args:
            exc (Optional[Exception]): An exception object if one occurred during the request, otherwise None.
        """
        # Only proceed if a monitor entry was initialized and not already sent.
        if hasattr(flask_g, 'monitor_entry') and not flask_g.monitor_entry_sent:
            try:
                entry = flask_g.monitor_entry
                process_time = time.time() - flask_g.start_time
                path = flask_request.url_rule.rule if flask_request.url_rule else flask_request.path
                method = flask_request.method.lower()

                entry.process_time = round(process_time, 4)
                # Re-extract request body if not already done or for robustness
                entry.request_body = self._safe_extract_flask_request_body()

                if exc:  # If an exception occurred
                    entry.status_code = getattr(exc, 'code', 500) # Use exception code if available, else 500
                    entry.error = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc()
                    }
                    # Try to capture response body if an error page/response was rendered by Flask
                    try:
                        # Flask often attaches the response to `flask_g` or returns it from error handlers
                        if hasattr(flask_g, 'response'):
                            entry.response = self._safe_extract_flask_response_body(flask_g.response)
                    except Exception:
                        pass  # Ignore if response extraction fails during an error

                else:  # No exception, a successful request
                    # Ensure status code and response are captured if not already by after_request
                    if hasattr(flask_g, 'response'):
                        entry.status_code = flask_g.response.status_code
                        entry.response = self._safe_extract_flask_response_body(flask_g.response)
                    else:
                        # Fallback for cases where `flask_g.response` might not be set, default to 200 OK.
                        entry.status_code = 200

                # Finally, submit the consolidated log entry.
                self.processor.add_log_entry(entry)
                self.processor.update_endpoint_metrics(path, method, process_time, flask_request.url)
                flask_g.monitor_entry_sent = True # Mark as sent

            except Exception as monitor_error:
                logger.warning(f"Error in Flask teardown request monitoring: {monitor_error}")

    def _flask_error_handler(self, e):
        """
        Flask general error handler.
        This function would typically be registered with `app.errorhandler(Exception)`.
        It creates a *new* LogEntry specifically for the error and dispatches it.
        (Note: If `_flask_teardown_request` is used as the single point of truth,
        this function's logging might cause duplicates if not carefully managed or removed.)

        Args:
            e (Exception): The exception that occurred.

        Returns:
            Tuple[json, int]: A JSON error response and the HTTP status code.
        """
        try:
            status_code = getattr(e, 'code', 500)
            error_entry = LogEntry(
                method=flask_request.method.lower(),
                path=flask_request.path,
                full_url=flask_request.url,
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                framework="flask",
                status_code=status_code,
                error={
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc()
                }
            )

            # This `add_log_entry` call is what could lead to a duplicate log
            # if `_flask_teardown_request` also logs the request.
            self.processor.add_log_entry(error_entry)

        except Exception as monitor_error:
            logger.warning(f"Error in error handler monitoring: {monitor_error}")

        # Return a Flask response for the error
        return jsonify({"error": str(e)}), status_code

    def _discover_flask_endpoints(self, app: Flask):
        """
        Discovers all registered API endpoints within a Flask application
        and adds them to the `BackgroundProcessor` for health tracking.
        Static file rules are explicitly ignored.

        Args:
            app (Flask): The Flask application instance.
        """
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static":  # Skip static file endpoints
                continue
            # Extract HTTP methods, excluding HEAD and OPTIONS, and sort them
            methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))
            self.processor.add_endpoint(EndpointStats(
                _path=rule.rule, # Flask's rule.rule provides the parameterized path (e.g., /users/<id>)
                type=",".join(methods).lower() # Convert methods list to a comma-separated string
            ))

    # Helper methods with error handling
    async def _safe_capture_request_body(self, request: Request) -> Any:
        """
        Asynchronously and safely captures the request body for FastAPI.
        Attempts to parse it as JSON; otherwise, returns a truncated string.

        Args:
            request (Request): The FastAPI request object.

        Returns:
            Any: The parsed JSON body, a truncated string, or None if no body.
        """
        try:
            body_bytes = await request.body()
            if not body_bytes:
                return None
            try:
                # Attempt to parse as JSON
                return json.loads(body_bytes.decode())
            except json.JSONDecodeError:
                # If not JSON, decode as string and truncate
                return body_bytes.decode(errors='ignore')[:500]
        except Exception:
            # Catch any other exceptions during body capture
            return None

    def _rebuild_fastapi_request(self, original_request: Request, body: Any) -> Request:
        """
        Rebuilds a FastAPI `Request` object with a captured body.
        This is necessary because FastAPI's request body stream can only be read once.

        Args:
            original_request (Request): The original FastAPI request object.
            body (Any): The captured request body (dict, string, or None).

        Returns:
            Request: A new Request object that can be read again.
        """
        # Convert the captured body back to bytes for the new request's receive function
        body_bytes = json.dumps(body).encode() if body else b""
        return Request(
            original_request.scope, # Use the original scope
            receive=lambda: { # Provide a new receive function that yields the captured body
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        )

    async def _safe_capture_response_body(self, response: Response) -> bytes:
        """
        Asynchronously and safely captures the full response body from a FastAPI `Response`.
        It reconstructs the body by iterating through the `body_iterator`.

        Args:
            response (Response): The FastAPI response object.

        Returns:
            bytes: The full response body as bytes, or an empty bytes object on error.
        """
        try:
            # Consume the entire async body iterator to get the full response body
            return b"".join([chunk async for chunk in response.body_iterator])
        except Exception:
            return b""

    def _rebuild_fastapi_response(self, original: Response, body: bytes) -> Response:
        """
        Rebuilds a FastAPI `Response` object with a captured body.
        This is necessary because the original response body stream might have been consumed.

        Args:
            original (Response): The original FastAPI response object.
            body (bytes): The captured response body as bytes.

        Returns:
            Response: A new Response object that can be sent to the client.
        """
        return Response(
            content=body,
            status_code=original.status_code,
            headers=dict(original.headers), # Copy original headers
            media_type=original.media_type
        )

    def _safe_extract_flask_request_body(self) -> Any:
        """
        Safely extracts the request body for Flask.
        Attempts to parse as JSON first, then falls back to raw data (truncated).

        Returns:
            Any: The parsed JSON body, a truncated string, or None.
        """
        try:
            # Attempt to get JSON body (returns None silently if not JSON)
            return flask_request.get_json(silent=True) or \
                   flask_request.data.decode(errors='ignore')[:500] # Fallback to raw data
        except Exception:
            return None

    def _safe_extract_flask_response_body(self, response) -> Any:
        """
        Safely extracts the response body for Flask.
        Attempts to parse as JSON first, then falls back to raw data (truncated).

        Args:
            response: The Flask response object.

        Returns:
            Any: The parsed JSON body, a truncated string, or None.
        """
        try:
            # Attempt to get JSON body from response (returns None if not JSON)
            return response.get_json() or \
                   response.data.decode(errors='ignore')[:500] # Fallback to raw data
        except Exception:
            return None

    def _safe_parse_response(self, body: bytes) -> Any:
        """
        Safely parses a response body (bytes) into a Python object (usually a dict).
        Attempts JSON parsing, falls back to truncated string, and removes sensitive/large fields.

        Args:
            body (bytes): The response body in bytes.

        Returns:
            Any: The parsed object, truncated string, or None.
        """
        try:
            if not body:
                return None
            data = json.loads(body)
            # Remove potentially large or redundant fields from the response data
            data.pop("code", None) # Example: remove a 'code' field if present
            return data
        except json.JSONDecodeError:
            # If not valid JSON, decode as string and truncate
            return body.decode(errors='ignore')[:500]
        except Exception:
            # Catch any other parsing errors
            return None

    async def close(self):
        """
        Cleans up resources by stopping the background processor.
        This should be called during application shutdown.
        """
        await self.processor.stop()


class FastAPIMiddlewareAdapter(BaseHTTPMiddleware):
    """
    An adapter class to integrate `ApiHealthCheckMiddleware` with FastAPI.
    FastAPI's `BaseHTTPMiddleware` requires a `dispatch` method.
    """

    def __init__(self, app, monitor: ApiHealthCheckMiddleware):
        """
        Initializes the FastAPIMiddlewareAdapter.

        Args:
            app: The FastAPI application instance.
            monitor (ApiHealthCheckMiddleware): An instance of the main monitoring middleware.
        """
        super().__init__(app)
        self.monitor = monitor # Store the monitor instance to delegate request processing

    async def dispatch(self, request: Request, call_next):
        """
        The main dispatch method for FastAPI middleware.
        Delegates the actual request processing and monitoring logic
        to the `ApiHealthCheckMiddleware` instance.

        Args:
            request (Request): The incoming FastAPI request.
            call_next: The next callable in the middleware chain.

        Returns:
            Response: The FastAPI response.
        """
        # Calls the core processing logic defined in ApiHealthCheckMiddleware
        return await self.monitor.process_fastapi_request(request, call_next)


@asynccontextmanager
async def health_monitor(dsn: str, framework: str = "auto"):
    """
    An asynchronous context manager for easily setting up and tearing down
    the API health monitoring middleware.

    Usage:
    ```python
    async def main():
        async with health_monitor(dsn="your_dsn_here", framework="fastapi") as monitor:
            # Your FastAPI/Flask app setup or usage here
            # For Flask, you'd typically initialize the middleware with the app:
            # app = Flask(__name__)
            # monitor.init_app(app)
            pass
    ```

    Args:
        dsn (str): The Data Source Name for the monitoring service.
        framework (str): Explicitly specify the framework ("flask", "fastapi")
                         or "auto" to detect it.

    Yields:
        ApiHealthCheckMiddleware: An instance of the configured monitoring middleware.
    """
    monitor = ApiHealthCheckMiddleware(dsn=dsn, framework=framework)
    try:
        yield monitor # Yield the monitor instance for use in the 'with' block
    finally:
        # Ensure the monitor's resources are closed when exiting the context
        await monitor.close()