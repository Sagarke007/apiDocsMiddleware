import portalocker  # Add this import  # For Unix file locking
import asyncio
import time
import json
import logging
import httpx
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List, Union
from concurrent.futures import ThreadPoolExecutor
from flask import request as flask_request, g as flask_g, jsonify, Flask
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pathlib import Path
import tempfile
import os
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import dataclasses

# Setup logger
log_path = Path("logs/api_monitor.log")
log_path.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("api_monitor")
logger.setLevel(logging.INFO)
handler = logging.FileHandler(log_path)
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)


@dataclass
class EndpointStats:
    _path: str
    type: str
    request_url: str = ""
    response_time: float = 0.0
    schema: Dict = field(default_factory=dict)
    security_schemes: Dict = field(default_factory=dict)


class RequestCircuitBreaker:
    """Circuit breaker for monitoring requests"""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failure_count = 0
        self._last_failure = 0
        self._is_open = False
        self.session = requests.Session()  # Optional, for reuse

    def is_available(self) -> bool:
        if self._is_open:
            if (time.time() - self._last_failure) > self.recovery_timeout:
                self._is_open = False
                self._failure_count = 0
                return True
            return False
        return True

    def record_failure(self):
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._is_open = True
            self._last_failure = time.time()
            logger.warning("Circuit breaker tripped - monitoring paused")

    def record_success(self):
        self._failure_count = 0
        self._is_open = False


class ApiHealthCheckMiddleware:
    """API Monitoring Middleware with temp file storage and periodic sending"""

    def __init__(self, app=None, dsn: str = "", framework: str = "auto"):
        self.dsn = dsn
        self.executor = ThreadPoolExecutor(
            max_workers=5,
            thread_name_prefix="monitor_worker_",
        )
        self.endpoint_data: List[EndpointStats] = []
        self.circuit_breaker = RequestCircuitBreaker()
        self.async_client = None
        self.framework = self._detect_framework(app) if framework == "auto" else framework

        # Temp file setup
        self._setup_temp_logging()

        # Scheduler setup
        self.scheduler = BackgroundScheduler()
        self._setup_scheduler()
        self.session = requests.Session()  # Optional, for reuse

        if app is not None:
            self.init_app(app)

    def _setup_temp_logging(self):
        """Initialize temporary logging system"""
        self.temp_log_dir = tempfile.mkdtemp(prefix="api_monitor_")
        self.temp_log_file = os.path.join(self.temp_log_dir, "monitor_logs.json")
        print(self.temp_log_file)
        # Create empty log file
        with open(self.temp_log_file, 'w') as f:
            json.dump([], f)

    def _setup_scheduler(self):
        """Configure the background scheduler with async support"""
        self.scheduler.add_job(
            self._run_scheduled_task,
            'interval',
            seconds=5,
            max_instances=1
        )
        self.scheduler.start()

    def _run_scheduled_task(self):
        """Wrapper to run async scheduled task from sync context"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._send_and_clean_logs())
        except Exception as e:
            logger.error(f"Error in scheduled task: {str(e)}")
        finally:
            loop.close()

    async def _ensure_async_client(self):
        """Initialize async client if not already done"""

        if self.async_client is None:
            self.async_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))

    def _detect_framework(self, app) -> str:
        """Auto-detect the web framework"""
        if hasattr(app, 'add_middleware'):  # FastAPI
            return "fastapi"
        elif hasattr(app, 'before_request'):  # Flask
            return "flask"
        raise ValueError("Could not detect framework type")

    def init_app(self, app):
        """Initialize the appropriate middleware based on framework"""
        if self.framework == "fastapi":
            self._init_fastapi(app)
        elif self.framework == "flask":
            self._init_flask(app)
        else:
            raise ValueError(f"Unsupported framework: {self.framework}")

    # FastAPI Implementation ======================================

    def _init_fastapi(self, app):
        """Initialize FastAPI middleware"""
        app.add_middleware(FastAPIMiddlewareAdapter, monitor=self)

    async def process_fastapi_request(self, request: Request, call_next):
        """Handle FastAPI request/response cycle"""
        await self._ensure_async_client()

        start_time = time.time()
        path = request.url.path
        method = request.method.lower()
        full_url = str(request.url)

        log_entry = {
            "method": method,
            "path": path,
            "full_url": full_url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "framework": "fastapi",
            "start_time": start_time
        }

        try:
            # Process request
            request_body = await self._capture_request_body(request)
            log_entry["request_body"] = request_body
            request = self._rebuild_fastapi_request(request, request_body)

            # Continue with the request chain
            response = await call_next(request)

            # Process response
            response_body = await self._capture_response_body(response)
            process_time = time.time() - start_time

            # Build monitoring data
            log_entry.update({
                "status_code": response.status_code,
                "process_time": process_time,
                "response": self._safe_parse_response(response_body)
            })

            # Submit background tasks
            await self._submit_background_tasks(
                log_entry=log_entry,
                path=path,
                method=method,
                process_time=process_time,
                url=full_url
            )

            return self._rebuild_fastapi_response(response, response_body)

        except Exception as exc:
            process_time = time.time() - start_time
            log_entry.update(self._create_error_log(exc, process_time))
            await self._submit_background_tasks(log_entry, path, method, process_time, full_url)
            raise

    # Flask Implementation ========================================

    def _init_flask(self, app):
        """Initialize Flask hooks"""
        app.before_request(self._flask_before_request)
        app.after_request(self._flask_after_request)
        app.errorhandler(Exception)(self._flask_error_handler)
        self._discover_flask_endpoints(app)

    def _flask_before_request(self):
        """Flask before request handler"""
        flask_g.start_time = time.time()
        flask_g.log_entry = {
            "method": flask_request.method.lower(),
            "path": flask_request.path,
            "full_url": flask_request.url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "framework": "flask"
        }

    def _flask_after_request(self, response):
        """Flask after request handler"""
        try:
            process_time = time.time() - flask_g.start_time
            path = flask_request.url_rule.rule if flask_request.url_rule else flask_request.path
            method = flask_request.method.lower()

            log_entry = {
                **flask_g.log_entry,
                "status_code": response.status_code,
                "process_time": round(process_time, 4),
                "request_body": self._safe_extract_flask_request_body(),
                "response": self._safe_extract_flask_response_body(response),
            }

            # For Flask, we need to run in executor since it's sync
            self.executor.submit(
                self._run_async_in_thread,
                log_entry,
                path,
                method,
                process_time,
                flask_request.url
            )

        except Exception as e:
            logger.exception("Monitoring error in after_request: %s", str(e))

        return response

    def _flask_error_handler(self, e):
        """Flask error handler"""
        status_code = getattr(e, 'code', 500)
        error_entry = {
            "method": flask_request.method.lower(),
            "path": flask_request.path,
            "full_url": flask_request.url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status_code": status_code,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc()
            }
        }
        self.executor.submit(
            self._run_async_in_thread,
            error_entry,
            None, None, None, None
        )
        return jsonify(error_entry["error"]), status_code

    # Common Methods ==============================================

    def _run_async_in_thread(self, log_entry, path, method, process_time, url):
        """Run async tasks in a dedicated event loop"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._execute_background_tasks(
                log_entry,
                path,
                method,
                process_time,
                url
            ))
        except Exception as e:
            logger.error("Error in background task: %s", str(e))
        finally:
            loop.close()

    async def _submit_background_tasks(self, log_entry: Dict,
                                       path: Optional[str] = None,
                                       method: Optional[str] = None,
                                       process_time: Optional[float] = None,
                                       url: Optional[str] = None):
        """Async version for FastAPI"""
        try:
            await self._execute_background_tasks(
                log_entry,
                path,
                method,
                process_time,
                url
            )
        except Exception as e:
            logger.error("Failed to execute background task: %s", str(e))

    async def _execute_background_tasks(self, log_entry: Dict,
                                        path: Optional[str],
                                        method: Optional[str],
                                        process_time: Optional[float],
                                        url: Optional[str]):
        """Execute all monitoring tasks with error handling"""
        try:
            # Store in temp file instead of immediate sending
            self._append_to_temp_log(log_entry)

            if all([path, method, process_time, url]):
                self._update_endpoint_metrics(path, method, process_time, url)
                await self._send_endspoints()
        except Exception as e:
            logger.exception("Background task failed: %s", str(e))

    def _append_to_temp_log(self, data: Dict):
        """Thread-safe JSON append operation using portalocker for cross-platform support"""
        try:
            # Create/open the lock file
            lock_file = self.temp_log_file + ".lock"
            with open(lock_file, 'w') as lock:
                # Acquire exclusive lock
                portalocker.lock(lock, portalocker.LOCK_EX)

                # Read existing logs
                logs = []
                if os.path.exists(self.temp_log_file):
                    try:
                        with open(self.temp_log_file, 'r') as f:
                            logs = json.load(f)
                    except json.JSONDecodeError:
                        logger.warning("Corrupted log file detected, resetting")
                        logs = []

                # Append new data
                logs.append(data)

                # Write to temp file
                temp_path = self.temp_log_file + ".tmp"
                with open(temp_path, 'w') as f:
                    json.dump(logs, f)

                # Atomic rename
                os.replace(temp_path, self.temp_log_file)

        except Exception as e:
            logger.error(f"Error writing to temp log: {str(e)}")
        finally:
            # Release the lock
            try:
                portalocker.unlock(lock)
            except:
                pass

            if os.path.exists(lock_file):
                os.remove(lock_file)

    async def _send_and_clean_logs(self):
        """Safe log sending with corruption handling"""
        try:
            if not os.path.exists(self.temp_log_file):
                return

            # Read logs with corruption handling
            logs = []
            try:
                with open(self.temp_log_file, 'r') as f:
                    logs = json.load(f)
            except json.JSONDecodeError as e:
                logger.error(f"Corrupted log file: {str(e)}")
                # Try to recover partial data
                with open(self.temp_log_file, 'r') as f:
                    content = f.read()
                    logs = self._recover_json(content)
                if not logs:
                    logger.warning("Could not recover any logs")
                    return

            if logs:
                self.safe_send_health_summary(logs)

            # Clear the file
            with open(self.temp_log_file, 'w') as f:
                json.dump([], f)

        except Exception as e:
            logger.error(f"Error in log sending/cleaning: {str(e)}")

    def _recover_json(self, content: str) -> List[Dict]:
        """Attempt to recover valid JSON from corrupted content"""
        try:
            # Simple recovery - find complete JSON objects
            objects = []
            decoder = json.JSONDecoder()
            content = content.strip()

            while content:
                obj, idx = decoder.raw_decode(content)
                objects.append(obj)
                content = content[idx:].lstrip()
            return objects
        except:
            return []

    async def _send_health_summary(self, logs: List[Dict]):
        """Send collected logs to monitoring service"""
        if not self.circuit_breaker.is_available():
            logger.debug("Circuit breaker open - skipping monitoring")
            return

        try:
            await self._ensure_async_client()
            # try:
            # response = await self.async_client.post(
            #     "https://dev.viewcurry.com/beacon/gatekeeper/upload/save-api-response",
            #     json={"dsn": self.dsn, "response": (logs[0]) if logs else {}}
            # )
            url = "https://dev.viewcurry.com/beacon/gatekeeper/upload/save-api-response"
            payload = {
                "dsn": self.dsn,
                "response": logs
            }
            response = self.session.post(url, json=payload, timeout=5.0)
            response.raise_for_status()  # Raise exception if 4xx/5xx
            response.raise_for_status()
            self.circuit_breaker.record_success()
        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.warning("Failed to send health summary: %s", str(e))

    def safe_send_health_summary(self, logs):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._send_health_summary(logs))
            else:
                asyncio.run(self._send_health_summary(logs))
        except RuntimeError as e:
            logger.warning("Cannot send summary: %s", e)

    async def _send_endspoints(self):
        """Periodically send endpoint health summary"""
        try:
            response = await self.async_client.post(
                "https://dev.viewcurry.com/beacon/gatekeeper/upload/send-api-health-data",
                json={
                    "dsn": self.dsn,
                    "framework": self.framework,
                    "endpoints": [dataclasses.asdict(e) for e in self.endpoint_data],
                }
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("Failed to send health summary: %s", str(e))

    def _update_endpoint_metrics(self, path: str, method: str,
                                 response_time: float, url: str):
        """Update endpoint performance metrics"""
        for endpoint in self.endpoint_data:
            if endpoint._path == path and endpoint.type == method:
                endpoint.request_url = url
                endpoint.response_time = response_time
                break

    # FastAPI-specific helpers ====================================

    async def _capture_request_body(self, request: Request) -> Any:
        try:
            body_bytes = await request.body()
            return json.loads(body_bytes.decode())
        except json.JSONDecodeError:
            return body_bytes.decode(errors='ignore')
        except Exception as e:
            logger.warning("Request body capture error: %s", str(e))
            return None

    def _rebuild_fastapi_request(self, original_request: Request, body: Any) -> Request:
        body_bytes = json.dumps(body).encode() if body else b""
        return Request(
            original_request.scope,
            receive=lambda: {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        )

    async def _capture_response_body(self, response: Response) -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    def _rebuild_fastapi_response(self, original: Response, body: bytes) -> Response:
        return Response(
            content=body,
            status_code=original.status_code,
            headers=dict(original.headers),
            media_type=original.media_type
        )

    # Flask-specific helpers ======================================

    def _safe_extract_flask_request_body(self) -> Any:
        try:
            return flask_request.get_json(silent=True) or flask_request.data.decode()
        except Exception:
            return "<unparseable>"

    def _safe_extract_flask_response_body(self, response) -> Any:
        try:
            return response.get_json() or response.data.decode()
        except Exception:
            return "<unparseable>"

    def _discover_flask_endpoints(self, app: Flask):
        """Discover all Flask API endpoints"""
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))
            self.endpoint_data.append(EndpointStats(
                _path=rule.rule,
                type=",".join(methods).lower()
            ))

    # Common helpers ==============================================

    def _safe_parse_response(self, body: bytes) -> Any:
        try:
            data = json.loads(body)
            data.pop("code", None)
            return data
        except json.JSONDecodeError:
            return body.decode(errors='ignore')
        except Exception as e:
            logger.warning("Response parsing error: %s", str(e))
            return None

    def _create_error_log(self, exc: Exception, process_time: float) -> Dict:
        return {
            "status_code": 500,
            "process_time": process_time,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc()
            }
        }

    async def close(self):
        """Clean up resources"""
        if self.async_client:
            await self.async_client.aclose()

        # Cleanup temp files
        try:
            if os.path.exists(self.temp_log_file):
                os.remove(self.temp_log_file)
            if os.path.exists(self.temp_log_dir):
                os.rmdir(self.temp_log_dir)
        except Exception as e:
            logger.warning(f"Error cleaning temp files: {str(e)}")

        # Shutdown scheduler
        self.scheduler.shutdown()


class FastAPIMiddlewareAdapter(BaseHTTPMiddleware):
    """Adapter to make our middleware work with FastAPI"""

    def __init__(self, app, monitor: ApiHealthCheckMiddleware):
        super().__init__(app)
        self.monitor = monitor

    async def dispatch(self, request: Request, call_next):
        return await self.monitor.process_fastapi_request(request, call_next)