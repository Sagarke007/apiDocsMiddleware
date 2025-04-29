""""
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

from apiDocsMiddleware.health_checks.logger import set_logger

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
        self.endpoint_stats = {}  # To store response times for each endpoint
        self.function_details = {}  # To store function/class details for each endpoint
        logger.info("ApiHealthCheckMiddleware initialized")

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

    def _validate_jwt(self, request) -> bool:
        """
        Validate the JWT token and extract project and clientId.
        """
        authorization_header = request.headers.get("Authorization")

        if not authorization_header:
            logger.warning("Authorization header missing")
            raise HTTPException(status_code=401, detail="Authorization header missing")

        token = authorization_header.split(" ")[1]  # Extract token from "Bearer <token>"

        try:
            decoded_token = jwt.decode(token, self.secret_key, algorithms=["HS256"])
            project_id = decoded_token.get("project")
            client_id = decoded_token.get("clientId")

            if not project_id or not client_id:
                logger.warning("Token is missing required fields")
                raise HTTPException(status_code=401, detail="Token is missing required fields")

            # Check if the provided IDs match the required values
            if project_id != self.required_project_id or client_id != self.required_client_id:
                logger.warning("Token project_id or client_id mismatch")
                raise HTTPException(status_code=401, detail="Token project_id or client_id mismatch")

        except jwt.ExpiredSignatureError:
            logger.error("Token has expired")
            raise HTTPException(status_code=401, detail="Token has expired")
        except jwt.InvalidTokenError:
            logger.error("Invalid token")
            raise HTTPException(status_code=401, detail="Invalid token")
        except Exception as e:
            logger.error(f"Unexpected error during JWT validation: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        return True

    def _build_openapi_doc(self) -> Dict:
        """
        Build OpenAPI documentation for FastAPI or Flask manually, including response times and function details.
        """
        if self.framework == "fastapi":
            if not self.app.openapi_schema:
                self.app.openapi_schema = get_openapi(
                    title="My FastAPI App",
                    version="1.0.0",
                    description="A FastAPI app with custom middleware",
                    routes=self.app.routes,
                )

            # Append function details and response times to the OpenAPI documentation
            for route in self.app.routes:
                route_path = route.path
                function_name = None
                class_name = None
                nested_functions = []

                if hasattr(route.endpoint, "__name__"):
                    function_name = route.endpoint.__name__

                if hasattr(route.endpoint, "__self__"):
                    class_name = route.endpoint.__self__.__class__.__name__

                    if hasattr(route.endpoint.__self__, function_name):
                        instance = route.endpoint.__self__
                        function = getattr(instance, function_name)

                        methods = [method for method in dir(instance) if callable(getattr(instance, method)) and not method.startswith("_")]
                        nested_functions.extend([{"class": instance.__class__.__name__, "function": method} for method in methods])

                self.function_details[route_path] = {
                    "function": function_name,
                    "class": class_name if class_name else "N/A",
                    "nested_functions": nested_functions if nested_functions else []
                }

                self.endpoint_stats[route_path] = {
                    "average_response_time": 0,
                    "total_requests": 0,
                }

            return {
                "openapi": self.app.openapi_schema,
                "response_times": self.endpoint_stats,
                "function_details": self.function_details,
            }

        elif self.framework == "flask":
            return {
                "openapi": "3.0.0",
                "info": {
                    "title": "My Flask App",
                    "version": "1.0.0",
                    "description": "A Flask app with custom middleware"
                },
                "paths": {}
            }

    def _track_endpoint_stats(self, endpoint: str, start_time: float):
        """
        Track the response time for the given endpoint.
        """
        response_time = time.time() - start_time
        if endpoint not in self.endpoint_stats:
            self.endpoint_stats[endpoint] = {"total_time": 0, "count": 0}

        self.endpoint_stats[endpoint]["total_time"] += response_time
        self.endpoint_stats[endpoint]["count"] += 1

    def _track_function_details(self, endpoint: str, func_name: str):
        """
        Track function details for the given endpoint.
        """
        if endpoint not in self.function_details:
            self.function_details[endpoint] = []

        self.function_details[endpoint].append(func_name)

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

        if request.url.path == "/health-check":
            if not self.openapi_doc:
                self.openapi_doc = self._build_openapi_doc()
            return JSONResponse(content=self.openapi_doc)

        response = await call_next(request)
        self._track_endpoint_stats(request.url.path, start_time)
        self._track_function_details(request.url.path, "Some function name or class")  # Adjust as needed
        logger.info(f"Request to {request.url.path} completed with status {response.status_code}")

        return response

    def flask_before_request(self):
        start_time = time.time()

        if not self._validate_jwt(request):
            return jsonify({"error": "Unauthorized"}), 401

        if request.path == "/health-check":
            if not self.openapi_doc:
                self.openapi_doc = self._build_openapi_doc()
            return jsonify(self.openapi_doc)

        self._track_endpoint_stats(request.path, start_time)
        self._track_function_details(request.path, "Some function name or class")
        logger.info(f"Request to {request.path} received")

    def attach(self):
        if self.framework == "fastapi":
            self.app.add_middleware(BaseHTTPMiddleware, dispatch=self.asgi_middleware)
        elif self.framework == "flask":
            self.app.before_request(self.flask_before_request)

    def get_health_info(self):
        health_info = {
            "response_times": self.endpoint_stats,
            "function_details": self.function_details,
            "api_document": self._build_openapi_doc()
        }
        logger.info("Health info retrieved")
        return health_info
