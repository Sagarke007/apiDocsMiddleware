"""
This module provides a middleware class for FastAPI and Flask applications
"""
import time
from typing import Dict
import jwt
from fastapi.openapi.utils import get_openapi
from flask import Flask, request, jsonify
from fastapi import FastAPI, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse
import logging
from pathlib import Path
from collections import OrderedDict
from health_checks.logger import set_logger

# Assuming set_logger function is already available as you provided
logger = logging.getLogger("api_health_check_logger")
set_logger("api_health_check_logger", logging.INFO, Path("logs/api_health_check.log"))

class ApiHealthCheckMiddleware:
    """
    Middleware class to handle OpenAPI documentation and JWT validation for FastAPI and Flask frameworks.
    """
    def __init__(self, app, secret_key: str, required_project_id: str,
                 required_client_id: str):
        self.app = app
        self.openapi_doc: Dict = {}
        self.framework = self._detect_framework()
        self.secret_key = secret_key
        self.required_project_id = required_project_id
        self.required_client_id = required_client_id
        self.endpoint_stats = OrderedDict()  # To store response times for each endpoint (Ordered)
        self.function_details = OrderedDict()  # To store function/class details for each endpoint (Ordered)
        logger.info(f"ApiHealthCheckMiddleware initialized for {self.framework}")

    def _detect_framework(self) -> str:
        """
        Detects if the app is a FastAPI or Flask app.
        """
        if isinstance(self.app, FastAPI):
            return "fastapi"
        elif isinstance(self.app, Flask):
            return "flask"
        else:
            raise Exception("Unsupported framework!")

    def _validate_jwt(self, request_obj) -> bool:
        if self.framework == "fastapi":
            authorization_header = request_obj.headers.get("Authorization")
        elif self.framework == "flask":
            authorization_header = request_obj.headers.get("Authorization")
        else:
            raise Exception("Unsupported framework for JWT validation")

        if not authorization_header:
            logger.warning("Authorization header missing")
            if self.framework == "fastapi":
                raise HTTPException(status_code=401, detail="Authorization header missing")
            elif self.framework == "flask":
                return False

        try:
            token = authorization_header.split(" ")[1]
        except IndexError:
            logger.warning("Invalid Authorization header format")
            if self.framework == "fastapi":
                raise HTTPException(status_code=401, detail="Invalid Authorization header format")
            elif self.framework == "flask":
                return False

        try:
            decoded_token = jwt.decode(token, self.secret_key, algorithms=["HS256"])
            project_id = decoded_token.get("project")
            client_id = decoded_token.get("clientId")

            if not project_id or not client_id:
                logger.warning("Token is missing required fields")
                if self.framework == "fastapi":
                    raise HTTPException(status_code=401, detail="Token is missing required fields")
                elif self.framework == "flask":
                    return False

            if project_id != self.required_project_id or client_id != self.required_client_id:
                logger.warning("Token project_id or client_id mismatch")
                if self.framework == "fastapi":
                    raise HTTPException(status_code=401, detail="Token project_id or client_id mismatch")
                elif self.framework == "flask":
                    return False

        except jwt.ExpiredSignatureError:
            logger.error("Token has expired")
            if self.framework == "fastapi":
                raise HTTPException(status_code=401, detail="Token has expired")
            elif self.framework == "flask":
                return False
        except jwt.InvalidTokenError:
            logger.error("Invalid token")
            if self.framework == "fastapi":
                raise HTTPException(status_code=401, detail="Invalid token")
            elif self.framework == "flask":
                return False
        except Exception as e:
            logger.error(f"Unexpected error during JWT validation: {e}")
            if self.framework == "fastapi":
                raise HTTPException(status_code=500, detail=str(e))
            elif self.framework == "flask":
                return False

        return True

    def _build_openapi_doc(self) -> Dict:
        openapi_data = OrderedDict([
            ("openapi", "3.0.0"),
            ("info", OrderedDict([
                ("title", f"My {self.framework.capitalize()} App"),
                ("version", "1.0.0"),
                ("description", f"A {self.framework.capitalize()} app with custom middleware")
            ])),
            ("paths", OrderedDict()),
            ("response_times", self.endpoint_stats),
            ("function_details", self.function_details)
        ])

        if self.framework == "fastapi":
            if not self.app.openapi_schema:
                self.app.openapi_schema = get_openapi(
                    title=openapi_data["info"]["title"],
                    version=openapi_data["info"]["version"],
                    description=openapi_data["info"]["description"],
                    routes=self.app.routes,
                )
            openapi_data["openapi"] = self.app.openapi_schema.get("openapi")
            openapi_data["info"] = OrderedDict(self.app.openapi_schema.get("info", {}))
            raw_paths = self.app.openapi_schema.get("paths", {})
            for path, methods in raw_paths.items():
                openapi_data["paths"][path] = OrderedDict()
                for method, details in methods.items():
                    if method not in ['head', 'options']:
                        openapi_data["paths"][path][method] = OrderedDict([
                            ("summary", details.get("summary")),
                            ("responses", details.get("responses"))
                        ])

            for route in self.app.routes:
                route_path = route.path
                function_name = getattr(route.endpoint, "__name__", None)
                class_name = getattr(getattr(route.endpoint, "__self__", None), "__class__.__name__", "N/A")
                nested_functions = []
                if hasattr(route.endpoint, "__self__"):
                    instance = route.endpoint.__self__
                    methods = [method for method in dir(instance) if
                               callable(getattr(instance, method)) and not method.startswith("_")]
                    nested_functions.extend(
                        [{"class": instance.__class__.__name__, "function": method} for method in methods])
                self.function_details.setdefault(route_path, OrderedDict([
                    ("function", function_name),
                    ("class", class_name),
                    ("nested_functions", nested_functions)
                ]))
                self.endpoint_stats.setdefault(route_path, OrderedDict([
                    ("average_response_time", 0),
                    ("total_requests", 0)
                ]))

        elif self.framework == "flask":
            paths_dict = OrderedDict()
            for rule in self.app.url_map.iter_rules():
                endpoint = rule.endpoint
                methods = list(rule.methods)
                if 'HEAD' in methods:
                    methods.remove('HEAD')
                if 'OPTIONS' in methods:
                    methods.remove('OPTIONS')

                if not methods:
                    continue

                route_path = str(rule)
                view_func = self.app.view_functions.get(endpoint)
                function_name = getattr(view_func, "__name__", None)
                class_name = getattr(getattr(view_func, "view_class", None), "__name__", "N/A")
                nested_functions = []

                if hasattr(view_func, 'view_class'):
                    view_instance = view_func.view_class()
                    class_methods = [method for method in dir(view_instance) if
                                     callable(getattr(view_instance, method)) and not method.startswith("_")]
                    nested_functions.extend(
                        [{"class": view_instance.__class__.__name__, "function": method} for method in class_methods])

                paths_dict[route_path] = OrderedDict()
                for method in methods:
                    paths_dict[route_path][method.lower()] = OrderedDict([
                        ("summary", function_name or endpoint),
                        ("responses", OrderedDict([
                            ("200", OrderedDict([
                                ("description", "Successful response")
                            ]))
                        ]))
                    ])
                self.function_details.setdefault(route_path, OrderedDict([
                    ("function", function_name),
                    ("class", class_name),
                    ("nested_functions", nested_functions)
                ]))
                self.endpoint_stats.setdefault(route_path, OrderedDict([
                    ("average_response_time", 0),
                    ("total_requests", 0)
                ]))
            # Sort the paths alphabetically for consistent order
            for path in sorted(paths_dict.keys()):
                openapi_data["paths"][path] = paths_dict[path]

        return openapi_data

    def _track_endpoint_stats(self, endpoint: str, start_time: float):
        response_time = time.time() - start_time
        self.endpoint_stats.setdefault(endpoint, OrderedDict([("total_time", 0), ("count", 0)]))
        self.endpoint_stats[endpoint]["total_time"] += response_time
        self.endpoint_stats[endpoint]["count"] += 1
        self.endpoint_stats[endpoint]["average_response_time"] = (
            self.endpoint_stats[endpoint]["total_time"] / self.endpoint_stats[endpoint]["count"]
            if self.endpoint_stats[endpoint]["count"] > 0 else 0
        )

    def _track_function_details(self, endpoint: str, func_name: str):
        self.function_details.setdefault(endpoint, OrderedDict([("function", func_name)]))

    async def asgi_middleware(self, request, call_next):
        start_time = time.time()
        try:
            if not self._validate_jwt(request):
                return JSONResponse(content={"detail": "Unauthorized"}, status_code=401)
        except HTTPException as e:
            logger.warning(f"Unauthorized request: {e.detail}")
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return JSONResponse(status_code=500, content={"detail": str(e)})

        response = await call_next(request)

        if request.url.path == "/health-check":
            openapi_doc = self._build_openapi_doc()
            return JSONResponse(content=openapi_doc)

        self._track_endpoint_stats(request.url.path, start_time)
        route = next((r for r in self.app.routes if r.path == request.url.path), None)
        if route and hasattr(route.endpoint, "__name__"):
            self._track_function_details(request.url.path, route.endpoint.__name__)
        elif route and hasattr(route.endpoint, "__class__.__name__"):
            self._track_function_details(request.url.path, route.endpoint.__class__.__name__)

        logger.info(f"FastAPI request to {request.url.path} completed with status {response.status_code}")
        return response

    def flask_before_request(self):
        start_time = time.time()

        if not self._validate_jwt(request):
            return jsonify({"error": "Unauthorized"}), 401

        if request.path == "/health-check":
            openapi_doc = self._build_openapi_doc()
            return jsonify(openapi_doc)

        self._track_endpoint_stats(request.path, start_time)
        endpoint_func = self.app.view_functions.get(request.endpoint)
        if endpoint_func and hasattr(endpoint_func, "__name__"):
            self._track_function_details(request.path, endpoint_func.__name__)
        elif endpoint_func and hasattr(getattr(endpoint_func, "view_class", None), "__name__"):
            self._track_function_details(request.path, getattr(endpoint_func, "view_class", None).__name__)

        logger.info(f"Flask request to {request.path} received")
        return None

    def _get_openapi_info(self) -> Dict:
        info = OrderedDict([("title", f"My {self.framework.capitalize()} App"), ("version", "1.0.0")])
        paths_data = OrderedDict()
        if self.framework == "fastapi":
            openapi = get_openapi(routes=self.app.routes, **info, description="API Documentation")
            for path, data in openapi.get("paths", {}).items():
                methods = OrderedDict((method, OrderedDict([("summary", data.get("summary"))])) for method in data if method not in ["parameters"])
                route = next((r for r in self.app.routes if r.path == path), None)
                function_name = route.endpoint.__name__ if route else None
                paths_data[path] = OrderedDict([("methods", list(methods.keys())), ("function_name", function_name)])
        elif self.framework == "flask":
            for rule in self.app.url_map.iter_rules():
                methods = [m.lower() for m in rule.methods if m not in ['HEAD', 'OPTIONS']]
                if methods:
                    view_func = self.app.view_functions.get(rule.endpoint)
                    function_name = getattr(view_func, "__name__", None)
                    paths_data[str(rule)] = OrderedDict([("methods", methods), ("function_name", function_name)])
        return OrderedDict([("info", info), ("paths", paths_data)])

    def flask_health_check(self):
        openapi_data = self._build_openapi_doc()
        return jsonify(openapi_data)

    async def asgi_health_check_route(self):
        openapi_data = self._build_openapi_doc()
        return JSONResponse(content=openapi_data)

    def attach(self):
        if self.framework == "fastapi":
            self.app.add_middleware(BaseHTTPMiddleware, dispatch=self.asgi_middleware)
            self.app.add_api_route("/health-check", self.asgi_health_check_route)
        elif self.framework == "flask":
            self.app.before_request(self.flask_before_request)
            self.app.add_url_rule("/health-check", "health_check", self.flask_health_check)

    def get_health_info(self):
        health_info = OrderedDict([
            ("response_times", self.endpoint_stats),
            ("function_details", self.function_details),
            ("api_document", self._build_openapi_doc())
        ])
        logger.info("Health info retrieved")
        return health_info