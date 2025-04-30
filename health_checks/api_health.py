
import logging
logger = logging.getLogger("api_health_middleware")
logging.basicConfig(level=logging.INFO)

import time
import inspect
import json
import logging
from pathlib import Path
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.routing import APIRoute

logger = logging.getLogger("api_health_middleware")
logging.basicConfig(level=logging.INFO)

class ApiHealthCheckMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret_key: str, project_id: str, client_id: str):
        """
        Initialize the HealthCheckMiddleware with security and storage settings.
        """
        super().__init__(app)
        self.secret_key = secret_key
        self.project_id = project_id
        self.client_id = client_id
        self.endpoint_data = []
        # For Windows
        self.temp_dir = Path("C:/ProgramData/api_health_data")
        self.storage_file = self.temp_dir / f"{project_id}_{client_id}_health_data.json"

        # Initialize endpoints and save immediately
        self._initialize_endpoints(app)
        self._save_data_with_retry()

    async def dispatch(self, request: Request, call_next):
        """
        Process each request with endpoint tracking.
        """
        path = request.url.path
        method = request.method.lower()

        if path == "/health-check":
            return JSONResponse(content=self._generate_health_data())

        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time

        self._update_endpoint_stats(path, method, process_time)
        self._save_data_with_retry()
        return response

    def _save_data_with_retry(self, max_attempts: int = 3):
        """
        Attempt to save data with retries and proper error handling.
        """
        for attempt in range(max_attempts):
            try:
                # Ensure directory exists
                self.temp_dir.mkdir(exist_ok=True, parents=True)

                # Verify we can write to the directory
                test_file = self.temp_dir / "test_write.tmp"
                with open(test_file, "w") as f:
                    f.write("test")
                test_file.unlink()

                # Save actual data
                with open(self.storage_file, "w") as f:
                    json.dump(self.endpoint_data, f, indent=2, ensure_ascii=False)
                return True

            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed: {str(e)}")
                time.sleep(0.1)  # Small delay before retry

        logger.error(f"Failed to save after {max_attempts} attempts")
        return False

    def _initialize_endpoints(self, app):
        """
        Initialize tracking for all registered endpoints.
        """
        try:
            for route in app.app.routes:
                if isinstance(route, APIRoute):
                    path = route.path
                    methods = [method.lower() for method in route.methods]
                    for method in methods:
                        self._add_endpoint(route, path, method)
        except Exception as e:
            logger.error(f"Endpoint initialization failed: {str(e)}")
    def _add_endpoint(self, route, path: str, method: str):
        """
        Add documentation for a single endpoint.
        """
        endpoint_func = route.endpoint
        class_name, functions = self._get_class_and_functions(endpoint_func)

        endpoint_info = {
            "_path": path,
            "type": method,
            "class": class_name,
            "responses": {"code": 200, "message": "Successful response"},
            "summary": self._generate_summary(path, endpoint_func),
            "response_time": 0,
            "functions": functions
        }

        self.endpoint_data.append(endpoint_info)

    def _get_class_and_functions(self, endpoint_func):
        """
        Extract class name and all functions from an endpoint.
        """
        functions = []
        class_name = "Module"

        if hasattr(endpoint_func, "__self__"):
            cls = endpoint_func.__self__.__class__
            class_name = cls.__name__
            for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
                if not name.startswith("_"):
                    functions.append({
                        "name": name,
                        "params": self._get_parameters(member)
                    })
        else:
            functions.append({
                "name": endpoint_func.__name__,
                "params": self._get_parameters(endpoint_func)
            })

        return class_name, functions

    def _get_parameters(self, func):
        """
        Extract parameter information from a function.
        """
        params = []
        try:
            sig = inspect.signature(func)
            for name, param in sig.parameters.items():
                param_type = (
                    str(param.annotation)
                    if param.annotation != inspect.Parameter.empty
                    else "any"
                )
                params.append({
                    "name": name,
                    "type": param_type
                })
        except Exception as e:
            logger.warning(f"Parameter inspection failed: {str(e)}")

        return params

    def _update_endpoint_stats(self, path: str, method: str, response_time: float):
        """
        Update statistics for a specific endpoint.
        """
        for endpoint in self.endpoint_data:
            if endpoint["_path"] == path and endpoint["type"] == method:
                endpoint["response_time"] = response_time
                break

    def _generate_summary(self, path: str, endpoint_func):
        """
        Generate a summary from docstring or path.
        """
        doc = inspect.getdoc(endpoint_func)
        if doc:
            return doc.strip().split("\n")[0]
        return path.strip("/").replace("-", " ").replace("_", " ").title() + " Endpoint"
