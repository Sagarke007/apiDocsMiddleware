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
from collections import defaultdict
from threading import Thread

# Import Flask stuff
try:
    from flask import request as flask_request, g as flask_g, current_app as flask_current_app
except ImportError:
    flask_request = None
    flask_g = None
    flask_current_app = None

# Setup logger
log_path = Path("logs/api_health_middleware.log")
logger = set_logger(
    log_name="api_health_middleware", level=20, log_file_path=log_path
)  # INFO = 20


class ApiHealthCheckMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, DSN: str, framework="fastapi"):
        super().__init__(app)
        self.DSN = DSN
        self.endpoint_data = []
        self.query_metrics = defaultdict(list)
        self.framework = framework.lower()
        if self.framework == "flask" and app is not None:
            self._init_flask(app)
            self._initialize_flask_endpoints(app)  # Initialize once here
            self._save_data_with_retry()
        else:
            # FastAPI (or Starlette) init
            self._initialize_endpoints(app)
            self._save_data_with_retry()

    def send_data_to_gatekeeper(self, api_url: str):
        """Send data to the gatekeeper API."""

        with httpx.Client() as client:
            data = {
                "dsn": self.DSN,
                "framework": self.framework,
                "endpoints": self.endpoint_data,
            }
            response = client.post(api_url, json=data)

            if response.status_code == 200:
                logger.info(f"Health check data successfully sent to {api_url}")
            else:
                logger.warning(
                    f"Failed to send health check data. Status code: {response.status_code}"
                )

    def _get_route_path_template(self, request: Request) -> str:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return getattr(route, "path", str(request.url.path))
        return str(request.url.path)

    async def dispatch(self, request: Request, call_next):
        full_url = str(request.url)
        path = self._get_route_path_template(request)
        method = request.method.lower()
        start_time = time.time()

        self.current_request_key = f"{method} {path}"  # Set request context

        log_entry = {
            "method": method,
            "path": path,
            "full_url": full_url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
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
            request = Request(
                request.scope,
                receive=lambda: {
                    "type": "http.request",
                    "body": request_body_bytes,
                    "more_body": False,
                },
            )

            response = await call_next(request)
            original_body = b""
            async for chunk in response.body_iterator:
                original_body += chunk

            # Step 2: Rebuild response with full body
            new_response = Response(
                content=original_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

            process_time = time.time() - start_time
            log_entry["process_time"] = process_time
            try:
                message_data = json.loads(original_body)
                message_data.pop("code", None)
            except:
                message_data = original_body.decode("utf-8")
            log_entry["status_code"] = response.status_code  # Extract from body
            # Optional: remove "code" from message if you don’t want duplication
            log_entry["response"] = message_data
            if self.current_request_key in self.query_metrics:
                log_entry["db_queries"] = self._get_query_summary(self.current_request_key)

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
                "traceback": self.format_browser_debugger_style(exc),
            }
            self._log_json(log_entry)
            self._update_endpoint_stats(path, method, process_time, full_url)
            self._save_data_with_retry()
            raise exc

    def _log_json(self, data):
        api_url = "https://dev.viewcurry.com/beacon/gatekeeper/upload/save-api-response"

        def send_log():
            try:
                with httpx.Client() as client:
                    payload = {"dsn": self.DSN, "response": data}
                    response = client.post(api_url, json=payload)
                    if response.status_code == 200:
                        logger.info(f"Health check data successfully sent to {api_url}")
                    else:
                        logger.warning(f"Failed to send health check data. Status code: {response.status_code}")
            except Exception as e:
                logger.error(f"Error sending health check data: {e}")

        Thread(target=send_log).start()

    def _save_data_with_retry(self, max_attempts: int = 3):
        for attempt in range(max_attempts):
            try:
                gatekeeper_api_url = "https://dev.viewcurry.com/beacon/gatekeeper/upload/send-api-health-data"
                self.send_data_to_gatekeeper(gatekeeper_api_url)
                return True

            except Exception as e:
                logger.error(f"Attempt {attempt + 1} failed: {str(e)}")
                time.sleep(0.1)

        logger.error(f"Failed to save after {max_attempts} attempts")
        return False

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
            "request_url": "",
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
                    functions.append(
                        {"name": name, "params": self._get_parameters(member)}
                    )
        else:
            functions.append(
                {
                    "name": endpoint_func.__name__,
                    "params": self._get_parameters(endpoint_func),
                }
            )
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
                params.append({"name": name, "type": param_type})
        except Exception as e:
            logger.warning(f"Parameter inspection failed: {str(e)}")

        return params

    def _update_endpoint_stats(self, path: str, method: str, response_time: float, full_url: str,
                               request_body=None):
        """Update endpoint statistics"""
        for i, endpoint in enumerate(self.endpoint_data):
            if endpoint["_path"] == path and endpoint["type"] == method:
                endpoint["request_url"] = full_url
                endpoint["response_time"] = response_time
                # with actual request / response data
                if request_body is not None:
                    endpoint["schema"]["request_body"] = self._generate_schema_from_data(request_body)
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
                "HTTPResponse",
                "Depends",
                "router.get",
                "router.post",
                "app.get",
                "app.post",
                "FastAPI",
                "JSONResponse",
                "app.route",
                "jsonify",
                "request.get_json"
            }

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func_name = self._extract_call_chain(node)
                    if func_name and not any(
                            func_name.startswith(excluded) for excluded in exclude_functions
                    ):
                        # Skip FastAPI internals and HTTP response methods
                        if not func_name.split(".")[0] in exclude_functions:
                            func_obj = self._resolve_function(endpoint_func, func_name)
                            if func_obj and callable(func_obj):
                                called_funcs.append(
                                    {
                                        "name": func_name,
                                        "params": self._get_parameters(func_obj),
                                    }
                                )

        except Exception as e:
            logger.warning(f"Failed to extract called functions: {str(e)}")

        # Deduplicate while preserving order
        seen = set()
        unique_funcs = []
        for f in called_funcs:
            if f["name"] not in seen:
                seen.add(f["name"])
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
        parts = func_name.split(".")
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
                    cleaned[key] = [
                        item["type"]
                        for item in value["anyOf"]
                        if item.get("type") != "null"
                    ]
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
        security_schemes = {}
        security_schemes["HTTPBearer"] = {"type": "http", "scheme": "bearer"}

        return security_schemes  # Corrected here

    def format_browser_debugger_style(self, exc, exclude_files=None, frame_context=5):
        if exclude_files is None:
            exclude_files = ["api_health.py", "routing.py", "_asyncio.py", "_exception_handler.py", "base.py",
                             "exceptions.py", "to_thread.py", "concurrency.py"]  # Add more patterns or paths as needed

        tb = exc.__traceback__
        formatted = []

        while tb:
            frame = tb.tb_frame
            lineno = tb.tb_lineno
            filename = frame.f_code.co_filename
            function = frame.f_code.co_name

            # Skip middleware files
            if any(ef in filename for ef in exclude_files):
                tb = tb.tb_next
                continue

            section = [
                f"🔥 Exception: {type(exc).__name__}",
                f"📄 File: {filename}, line {lineno}",
                f"🔧 Function: {function}"
            ]

            # Get code context if available
            try:
                lines, start_line = inspect.getsourcelines(frame.f_code)
                context = ""
                for idx, code_line in enumerate(lines):
                    actual_line = start_line + idx
                    pointer = "➡️" if actual_line == lineno else "   "
                    context += f"{pointer} {actual_line:>4} | {code_line}"
                section.append(f"\n📌 Code snippet:\n{context.strip()}")
            except OSError:
                section.append("\n📌 Code snippet: [Unavailable]")

            formatted.append("\n".join(section))
            tb = tb.tb_next

        formatted.append(
            f"\n🧵 Full stack trace:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")
        return "\n\n".join(formatted)

    def _init_flask(self, app):
        """Initialize Flask-specific middleware and endpoints."""

        @app.before_request
        def before_request():
            # Initialize endpoints for Flask
            flask_g.start_time = time.time()
            if flask_request.url_rule:
                path = flask_request.url_rule.rule
            else:
                path = flask_request.path
            method = flask_request.method.lower()
            flask_g.current_request_key = f"{method} {path}"
            flask_g.log_entry = {
                "method": method,
                "path": path,
                "full_url": flask_request.url,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            }

        @app.after_request
        def after_request(response):
            if 400 <= response.status_code >= 500:
                return response

            process_time = time.time() - flask_g.start_time
            if flask_request.url_rule:
                path = flask_request.url_rule.rule
            else:
                path = flask_request.path
            method = flask_request.method.lower()

            request_body = self._get_flask_request_body()
            response_data = self._get_flask_response_data(response)

            flask_g.log_entry.update({
                "process_time": process_time,
                "status_code": response.status_code,
                "request_body": request_body,
                "response": response_data,
            })

            self._update_endpoint_stats(path, method, process_time, flask_request.url, request_body)

            # Send log asynchronously
            from threading import Thread
            Thread(target=self._log_json, args=(flask_g.log_entry,)).start()

            self._save_data_with_retry()  # Optional: you can also make this async or batch it

            return response

        from flask import jsonify

        @app.errorhandler(Exception)
        def handle_uncaught_exception(error):
            """Global exception handler to catch uncaught exceptions"""
            error_entry = {
                "method": flask_request.method.lower(),
                "path": flask_request.endpoint,
                "full_url": flask_request.url,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "status_code": getattr(error, 'code', 500),
                "error": {
                    "type": str(type(error).__name__),
                    "message": str(error),
                    "traceback": self._format_flask_traceback(error),
                }
            }
            self._log_json(error_entry)
            self._save_data_with_retry()

            # ✅ Return a proper JSON response with error details and status code
            return jsonify(error_entry["error"]), error_entry["status_code"]

    def _format_flask_traceback(self, exc):
        """Format traceback in the same style as FastAPI version"""
        exclude_files = [
            'api_health.py', 'routing.py', '_asyncio.py',
            '_exception_handler.py', 'base.py', 'exceptions.py',
            'to_thread.py', 'concurrency.py', 'wsgi.py'
        ]

        tb = exc.__traceback__
        formatted = []

        while tb:
            frame = tb.tb_frame
            lineno = tb.tb_lineno
            filename = frame.f_code.co_filename
            function = frame.f_code.co_name

            # Skip middleware and framework files
            if any(ef in filename for ef in exclude_files):
                tb = tb.tb_next
                continue

            section = [
                f"🔥 Exception: {type(exc).__name__}",
                f"📄 File: {filename}, line {lineno}",
                f"🔧 Function: {function}"
            ]

            # Get code context
            try:
                lines, start_line = inspect.getsourcelines(frame.f_code)
                context = ""
                for idx, code_line in enumerate(lines):
                    actual_line = start_line + idx
                    pointer = "➡️" if actual_line == lineno else "   "
                    context += f"{pointer} {actual_line:>4} | {code_line}"
                section.append(f"\n📌 Code snippet:\n{context.strip()}")
            except (OSError, TypeError):
                section.append("\n📌 Code snippet: [Unavailable]")

            formatted.append("\n".join(section))
            tb = tb.tb_next

        formatted.append(
            f"\n🧵 Full stack trace:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")

        return "\n\n".join(formatted)

    def _initialize_flask_endpoints(self, app):
        """Fetch and store all Flask endpoints with metadata."""
        print("Routes before middleware:")
        for rule in app.url_map.iter_rules():
            print(rule)
            if rule.endpoint == "static":
                continue
            endpoint = rule.endpoint
            methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))

            endpoint_func = app.view_functions.get(endpoint)
            if endpoint_func is None:
                continue

            class_name, functions = self._get_class_and_functions(endpoint_func)

            endpoint_info = {
                "_path": rule.rule,
                "request_url": "",  # You can set a base URL here if needed
                "type": ','.join(methods).lower(),
                "summary": self._generate_summary(rule.rule, endpoint_func),
                "response_time": 0,
                "functions": functions,
                "schema": self._extract_flask_schema(endpoint_func),
                "security_schemes": self._generate_flask_security_schemes(endpoint_func),
            }

            self.endpoint_data.append(endpoint_info)

    def _extract_flask_schema(self, endpoint_func):
        """Extract schema information from Flask route by inspecting common documentation methods."""
        schema = {
            "request_body": {},
            "query_params": {},
            "path_params": {}
        }

        try:
            # Try Flask-RESTx/Flask-RESTplus parsing
            if hasattr(endpoint_func, '__apidoc__'):
                api_doc = endpoint_func.__apidoc__

                # Request body schema
                if 'body' in api_doc:
                    if isinstance(api_doc['body'], dict) and 'schema' in api_doc['body']:
                        schema['request_body'] = self._parse_flask_restx_schema(api_doc['body']['schema'])
                    elif hasattr(api_doc['body'], 'schema'):
                        schema['request_body'] = self._parse_flask_restx_schema(api_doc['body'].schema)

                # Query parameters
                if 'params' in api_doc:
                    for param, details in api_doc['params'].items():
                        schema['query_params'][param] = {
                            "type": details.get('type', 'string'),
                            "required": details.get('required', False),
                            "description": details.get('description', '')
                        }

            # Try Marshmallow schema parsing
            if hasattr(endpoint_func, 'view_class'):
                view_class = endpoint_func.view_class
                method = flask_request.method.lower()

                # Check for schema in method (like get, post, etc.)
                if hasattr(view_class, method):
                    method_func = getattr(view_class, method)
                    if hasattr(method_func, '__marshmallow_hook__'):
                        schema_obj = method_func.__marshmallow_hook__.args[0]
                        schema['request_body'] = self._parse_marshmallow_schema(schema_obj)

            # Try inspecting type hints (Python 3.6+)
            sig = inspect.signature(endpoint_func)
            for name, param in sig.parameters.items():
                if param.annotation != inspect.Parameter.empty:
                    if str(param.annotation).startswith('Request'):
                        continue
                    schema['request_body'][name] = {
                        "type": str(param.annotation),
                        "required": param.default == inspect.Parameter.empty
                    }

        except Exception as e:
            logger.warning(f"Failed to extract Flask schema: {str(e)}")

        return schema

    def _parse_flask_restx_schema(self, model):
        """Parse Flask-RESTx model schema."""
        result = {}
        if hasattr(model, '__schema__'):
            schema = model.__schema__
            for name, field in schema['properties'].items():
                result[name] = {
                    "type": field.get('type', 'string'),
                    "format": field.get('format'),
                    "required": name in schema.get('required', [])
                }
        return result

    def _parse_marshmallow_schema(self, schema):
        """Parse Marshmallow schema."""
        result = {}
        if hasattr(schema, '_declared_fields'):
            for name, field in schema._declared_fields.items():
                result[name] = {
                    "type": field.__class__.__name__.replace('Field', '').lower(),
                    "required": field.required
                }
        return result

    def _generate_flask_security_schemes(self, endpoint_func):
        """Generate security schemes by inspecting common Flask security decorators."""
        security_schemes = {}

        try:
            # Check for Flask-JWT-Extended
            if hasattr(endpoint_func, '_jwt_required'):
                security_schemes['JWT'] = {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT"
                }

            # Check for Flask-HTTPAuth
            if hasattr(endpoint_func, '_auth_required'):
                security_schemes['BasicAuth'] = {
                    "type": "http",
                    "scheme": "basic"
                }

            # Check for Flask-Security
            if hasattr(endpoint_func, '_decorators'):
                for decorator in endpoint_func._decorators:
                    if hasattr(decorator, '__name__') and 'token_required' in decorator.__name__:
                        security_schemes['Token'] = {
                            "type": "apiKey",
                            "name": "X-API-KEY",
                            "in": "header"
                        }

            # Check for OAuth (Flask-Dance, etc.)
            if hasattr(endpoint_func, '_oauth_required'):
                security_schemes['OAuth2'] = {
                    "type": "oauth2",
                    "flows": {
                        "implicit": {
                            "authorizationUrl": "/oauth/authorize",
                            "scopes": {}
                        }
                    }
                }

            # Default to HTTP Bearer if no specific scheme found but auth is required
            if not security_schemes and hasattr(endpoint_func, '_auth'):
                security_schemes['HTTPBearer'] = {
                    "type": "http",
                    "scheme": "bearer"
                }

        except Exception as e:
            logger.warning(f"Failed to extract security schemes: {str(e)}")

        return security_schemes

    def _get_flask_request_body(self):
        """Extract request body from Flask request"""
        try:
            if flask_request.content_type == 'application/json':
                return flask_request.get_json(silent=True) or {}
            else:
                data = flask_request.get_data(as_text=True)
                try:
                    return json.loads(data)
                except:
                    return data
        except Exception as e:
            return f"<unparseable: {str(e)}>"

    def _get_flask_response_data(self, response):
        """Extract response data from Flask response"""
        try:
            if response.content_type and 'application/json' in response.content_type:
                return response.get_json(silent=True) or {}
            return response.get_data(as_text=True)
        except Exception:
            return "<unparseable response>"

    def _initialize_fastapi_endpoints(self, app):
        """Initialize FastAPI endpoints"""
        try:
            routes = getattr(app, 'routes', [])
            if hasattr(app, 'app') and hasattr(app.app, 'routes'):
                routes = app.app.routes

            for route in routes:
                if isinstance(route, APIRoute):
                    path = route.path
                    methods = [method.lower() for method in route.methods if method not in ('HEAD', 'OPTIONS')]
                    for method in methods:
                        self._add_endpoint(route, path, method)
        except Exception as e:
            logger.error(f"FastAPI endpoint initialization failed: {str(e)}")

    def _generate_schema_from_data(self, data: dict) -> dict:
        """Generate JSON schema from actual data"""
        if not data or not isinstance(data, dict):
            return {}

        schema = {
            "type": "object",
            "properties": {},
            "example": data
        }

        for key, value in data.items():
            if isinstance(value, str):
                schema["properties"][key] = {"type": "string", }
            elif isinstance(value, int):
                schema["properties"][key] = {"type": "integer", }
            elif isinstance(value, float):
                schema["properties"][key] = {"type": "number"}
            elif isinstance(value, bool):
                schema["properties"][key] = {"type": "boolean"}
            elif isinstance(value, list):
                schema["properties"][key] = {
                    "type": "array",
                    "items": {"type": "string"} if value and isinstance(value[0], str) else {"type": "object"},
                }
            elif isinstance(value, dict):
                schema["properties"][key] = {
                    "type": "object",
                }
            else:
                schema["properties"][key] = {"type": "string"}

        return schema