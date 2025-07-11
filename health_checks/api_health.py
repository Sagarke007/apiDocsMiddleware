# """
# class for middleware to check the health of APIs
# """
#
# import ast  # Add this line at the top with other imports
import httpx
# import time
# import inspect
from pathlib import Path
# from fastapi import Request
# from starlette.middleware.base import BaseHTTPMiddleware
# from fastapi.routing import APIRoute
# from starlette.routing import Match
from health_checks.logger import set_logger  # adjust path if needed
# from starlette.responses import Response
# import traceback
# import json
# from collections import defaultdict
from threading import Thread
# try:
#     from flask import request as flask_request, g as flask_g, current_app as flask_current_app, jsonify
# except ImportError:
#     flask_request = None
#     flask_g = None
#     flask_current_app = None
#
# Setup logger
import asyncio

log_path = Path("logs/api_health_middleware.log")
logger = set_logger(
    log_name="api_health_middleware", level=20, log_file_path=log_path
)  # INFO = 20
#



# Custom middleware class

import json
import time
import traceback
import logging
from flask import request as flask_request, g as flask_g, jsonify
from concurrent.futures import ThreadPoolExecutor
import httpx

logger = logging.getLogger(__name__)

# Thread pool to handle background logging & API posting
executor = ThreadPoolExecutor(max_workers=10)


class CustomMiddleware:
    def __init__(self, flask_app, framework: str, dsn: str = ""):
        self.app = flask_app
        self.DSN = dsn
        self.framework = framework
        self.endpoint_data = []

        self._init_flask_hooks(flask_app)
        self._register_error_handler(flask_app)
        self._initialize_flask_endpoints(flask_app)

    def _init_flask_hooks(self, app):
        @app.before_request
        def before_request():
            flask_g.start_time = time.time()
            flask_g.log_entry = {
                "method": flask_request.method.lower(),
                "path": flask_request.path,
                "full_url": flask_request.url,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            }

        @app.after_request
        def after_request(response):
            try:
                process_time = time.time() - flask_g.start_time
                path = flask_request.url_rule.rule if flask_request.url_rule else flask_request.path
                method = flask_request.method.lower()

                request_body = self._safe_json(flask_request)
                response_body = self._safe_json(response)

                flask_g.log_entry.update({
                    "status_code": response.status_code,
                    "process_time": round(process_time, 4),
                    "path": flask_request.path,
                    "request_body": request_body,
                    "response": response_body,
                })

                executor.submit(self._log_json, flask_g.log_entry)
                self._update_endpoint_stats(path, method, process_time, flask_request.url, request_body)
                self._save_data_with_retry()

            except Exception as e:
                logger.exception(f"[Middleware Error] {str(e)}")

            return response

    def _register_error_handler(self, app):
        @app.errorhandler(Exception)
        def handle_exception(e):
            error_entry = {
                "method": flask_request.method.lower(),
                "path": flask_request.path,
                "full_url": flask_request.url,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "status_code": getattr(e, 'code', 500),
                "error": {
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc()
                }
            }
            executor.submit(self._log_json, error_entry)
            return jsonify(error_entry["error"]), error_entry["status_code"]

    def _initialize_flask_endpoints(self, app):
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            endpoint = rule.endpoint
            methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))
            if not app.view_functions.get(endpoint):
                continue
            self.endpoint_data.append({
                "_path": rule.rule,
                "request_url": "",
                "type": ','.join(methods).lower(),
                "response_time": 0,
                "schema": {},
                "security_schemes": {}
            })

    def _update_endpoint_stats(self, path: str, method: str, response_time: float, full_url: str,
                               request_body=None):
        for endpoint in self.endpoint_data:
            if endpoint["_path"] == path and endpoint["type"] == method:
                endpoint["request_url"] = full_url
                endpoint["response_time"] = response_time
                break

    def _save_data_with_retry(self):
        executor.submit(self.send_data_to_gatekeeper,
                        "https://dev.viewcurry.com/beacon/gatekeeper/upload/send-api-health-data")

    def _safe_json(self, source):
        try:
            if hasattr(source, "get_json"):
                return source.get_json(silent=True) or {}
            return source.get_data(as_text=True)
        except Exception:
            return "<unparseable>"

    def _log_json(self, data):
        api_url = "https://dev.viewcurry.com/beacon/gatekeeper/upload/save-api-response"
        try:
            with httpx.Client(timeout=2.0) as client:
                payload = {"dsn": self.DSN, "response": data}
                response = client.post(api_url, json=payload)
                if response.status_code == 200:
                    logger.info(f"Health check data sent to {api_url}")
                else:
                    logger.warning(f"Failed to send health data: {response.status_code}")
        except httpx.ReadTimeout:
            logger.warning(f"[Timeout] Health check POST timed out: {api_url}")
        except Exception as e:
            logger.exception(f"Log send error: {e}")

    def send_data_to_gatekeeper(self, api_url: str):
        try:
            with httpx.Client(timeout=2.0) as client:
                payload = {
                    "dsn": self.DSN,
                    "framework": self.framework,
                    "endpoints": self.endpoint_data,
                }
                response = client.post(api_url, json=payload)
                if response.status_code == 200:
                    logger.info(f"Health check summary sent to {api_url}")
                else:
                    logger.warning(f"Failed to send summary: {response.status_code}")
        except httpx.ReadTimeout:
            logger.warning(f"[Timeout] Health check POST timed out: {api_url}")
        except Exception as e:
            logger.exception(f"Send summary error: {e}")


from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request


class FastAPICustomMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, dsn: str):
        super().__init__(app)
        self.dsn = dsn

    async def dispatch(self, request: Request, call_next):
        print(f"[FastAPI Middleware] {request.method} request to {request.url.path}")
        print("DSN:", self.dsn)
        response = await call_next(request)
        return response












