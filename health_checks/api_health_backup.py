"""
class for middleware to check the health of APIs
"""

import ast  # Add this line at the top with other imports
import httpx
import time
import inspect
from pathlib import Path
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.routing import APIRoute
from starlette.routing import Match
from health_checks.logger import set_logger  # adjust path if needed
from starlette.responses import Response
import traceback
import json

# Setup logger
log_path = Path("logs/api_health_middleware.log")
logger = set_logger(log_name="api_health_middleware", level=20, log_file_path=log_path)  # INFO = 20

class ApiHealthCheckMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret_key: str, project_id: str, client_id: str):
        super().__init__(app)
        self.secret_key = secret_key
        self.project_id = project_id
        self.client_id = client_id
        self.endpoint_data = []
        self._initialize_endpoints(app)
        self.framework = self.detect_framework(app)
        self._save_data_with_retry()

    def send_data_to_gatekeeper(self, api_url: str, data: dict):
        with httpx.Client() as client:
            data = {
                "client_id": self.client_id,
                "project_id": self.project_id,
                "framework": self.framework,
                "endpoints": self.endpoint_data,
            }
            response = client.post(api_url, json=data)

            if response.status_code == 200:
                logger.info(f"Health check data successfully sent to {api_url}")
            else:
                logger.warning(f"Failed to send health check data. Status code: {response.status_code}")

    def _get_route_path_template(self, request: Request) -> str:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return getattr(route, 'path', str(request.url.path))
        return str(request.url.path)

    async def dispatch(self, request: Request, call_next):
        full_url = str(request.url)
        path = self._get_route_path_template(request)
        method = request.method.lower()
        start_time = time.time()

        log_entry = {
            "method": method,
            "path": path,
            "full_url": full_url,
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        }

        try:
            # Read and store the request body
            request_body_bytes = await request.body()
            try:
                request_body = request_body_bytes.decode("utf-8")
                # Try to parse as JSON
                log_entry["request_body"] = json.loads(request_body)
            except Exception:
                log_entry["request_body"] = request_body  # Keep raw if not JSON

            # Rebuild the request stream so it can be read again
            request = Request(request.scope, receive=lambda: {
                "type": "http.request",
                "body": request_body_bytes,
                "more_body": False
            })

            response = await call_next(request)
            original_body = b""
            async for chunk in response.body_iterator:
                original_body += chunk

            # Step 2: Rebuild response with full body
            new_response = Response(
                content=original_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type
            )

            process_time = time.time() - start_time
            log_entry["process_time"] = process_time
            try:
                message_data = json.loads(original_body)
                message_data.pop("code", None)
            except:
                message_data = original_body.decode("utf-8")
            log_entry["status_code"] = response.status_code # Extract from body
            # Optional: remove "code" from message if you don’t want duplication
            log_entry["response"] = message_data
            self._log_json(log_entry)
            self._update_endpoint_stats(path, method, process_time, full_url)
            self._save_data_with_retry()
            return new_response

        except Exception as exc:
            process_time = time.time() - start_time
            log_entry["status_code"] = 500
            log_entry["process_time"] = process_time
            log_entry["error"] = {
                "type": str(type(exc)),
                "message": str(exc),
                "traceback": traceback.format_exc()
            }
            self._log_json(log_entry)
            self._update_endpoint_stats(path, method, process_time, full_url)
            self._save_data_with_retry()
            raise exc

    def _log_json(self, data):
        #Example: Write to a file, or send to a logging system
        api_url = "https://dev.viewcurry.com/beacon/gatekeeper/upload/save-api-response"  # Adjust as needed

        #import json
        # with open("request_logs.jsonl", "a") as f:
        #     f.write(json.dumps(data) + "\n")
        with httpx.Client() as client:
            data = {
                "client_id": self.client_id,
                "project_id": self.project_id,
                "response":data
            }
            response = client.post(api_url, json=data)

            if response.status_code == 200:
                logger.info(f"Health check data successfully sent to {api_url}")
            else:
                logger.warning(f"Failed to send health check data. Status code: {response.status_code}")

    def _save_data_with_retry(self, max_attempts: int = 3):
        for attempt in range(max_attempts):
            try:
                gatekeeper_api_url = "https://dev.viewcurry.com/beacon/gatekeeper/upload/send-api-health-data"
                self.send_data_to_gatekeeper(gatekeeper_api_url, self.endpoint_data)
                return True

            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed: {str(e)}")
                time.sleep(0.1)

        logger.error(f"Failed to save after {max_attempts} attempts")
        return False

    def detect_framework(self, app):
        module = type(app).__module__
        if 'fastapi' in module:
            return 'FastAPI'
        elif 'starlette' in module:
            return 'FastAPI'
        elif 'flask' in module:
            return 'Flask'
        return f'Unknown ({module})'
    def _initialize_endpoints(self, app):
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
        endpoint_func = route.endpoint
        class_name, functions = self._get_class_and_functions(endpoint_func)
        schema = self._extract_schema_from_route(route)
        # Call the new function to generate security schemes
        security_schemes = self._generate_security_schemes(route)
        endpoint_info = {
            "_path": path,
            "request_url":"",
            "type": method,
            "summary": self._generate_summary(path, endpoint_func),
            "response_time": 0,
            "functions": functions,
            "schema": schema,
            "security_schemes": security_schemes,
        }
        self.endpoint_data.append(endpoint_info)

    def _get_class_and_functions(self, endpoint_func):
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
            # Add any functions it calls internally
            called_funcs = self._get_called_functions(endpoint_func)
            functions.extend(called_funcs)

        return class_name, functions

    def _get_parameters(self, func):
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

    def _update_endpoint_stats(self, path: str, method: str, response_time: float, full_url: str):
        for endpoint in self.endpoint_data:
            if endpoint["_path"] == path and endpoint["type"] == method:
                endpoint["request_url"] = full_url
                endpoint["response_time"] = response_time
                break

    def _generate_summary(self, path: str, endpoint_func):
        doc = inspect.getdoc(endpoint_func)
        if doc:
            return doc.strip().split("\n")[0]
        return path.strip("/").replace("-", " ").replace("_", " ").title() + " Endpoint"

    import ast
    import inspect

    def _get_called_functions(self, endpoint_func):
        called_funcs = []
        try:
            source = inspect.getsource(endpoint_func)
            tree = ast.parse(source)

            # List of function names to exclude
            exclude_functions = {
                'HTTPResponse', 'Depends', 'router.get', 'router.post',
                'app.get', 'app.post', 'FastAPI', 'JSONResponse'
            }

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func_name = self._extract_call_chain(node)
                    if func_name and not any(
                            func_name.startswith(excluded)
                            for excluded in exclude_functions
                    ):
                        # Skip FastAPI internals and HTTP response methods
                        if not func_name.split('.')[0] in exclude_functions:
                            func_obj = self._resolve_function(endpoint_func, func_name)
                            if func_obj and callable(func_obj):
                                called_funcs.append({
                                    "name": func_name,
                                    "params": self._get_parameters(func_obj)
                                })

        except Exception as e:
            logger.warning(f"Failed to extract called functions: {str(e)}")

        # Deduplicate while preserving order
        seen = set()
        unique_funcs = []
        for f in called_funcs:
            if f['name'] not in seen:
                seen.add(f['name'])
                unique_funcs.append(f)

        return unique_funcs

    def _extract_call_chain(self, node):
        """Extracts function names from AST nodes (supports nested calls like `obj.method()`)."""
        if isinstance(node, ast.Call):
            # Handle direct calls like `func()` or `obj.method()`
            return self._extract_call_chain(node.func)
        elif isinstance(node, ast.Name):
            # Handle simple names like `func` in `func()`
            return node.id
        elif isinstance(node, ast.Attribute):
            # Handle attributes like `obj.method` in `obj.method()`
            base = self._extract_call_chain(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def _resolve_function(self, endpoint_func, func_name):
        """Resolves a function name to its object (supports nested/module calls)"""
        parts = func_name.split('.')
        base = endpoint_func.__globals__.get(parts[0])

        for part in parts[1:]:
            if hasattr(base, part):
                base = getattr(base, part)
            else:
                return None
        return base if callable(base) else None

    def _extract_schema_from_route(self, route: APIRoute):
        def extract_types(properties: dict):
            cleaned = {}
            for key, value in properties.items():
                if "type" in value:
                    cleaned[key] = value["type"]
                elif "anyOf" in value:
                    # Flatten out the types in anyOf and remove 'null'
                    cleaned[key] = [item["type"] for item in value["anyOf"] if item.get("type") != "null"]
                    # If only one type remains, take it, otherwise keep the list
                    if len(cleaned[key]) == 1:
                        cleaned[key] = cleaned[key][0]
                    else:
                        cleaned[key] = cleaned[key]
                else:
                    cleaned[key] = "unknown"

            return cleaned

        schema = {}

        if route.body_field:
            model = route.body_field.type_
            if hasattr(model, "schema"):
                raw_schema = model.schema()
                props = raw_schema.get("properties", {})
                schema["request_body"] = extract_types(props)

        return schema
    def _generate_security_schemes(self, route):
        """
        Detect security schemes (like HTTPBearer) in the endpoint's dependency tree.
        """
        security_schemes ={}
        security_schemes["HTTPBearer"] = {
                    "type": "http",
                    "scheme": "bearer"
                }

        return security_schemes  # Corrected here







