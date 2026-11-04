"""Lightweight multi-threaded Flask-compatible framework backed by stdlib HTTPServer."""
import io
import json
import logging
import mimetypes
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from werkzeug.exceptions import HTTPException

_ctx = threading.local()


class _G:
    def __getattr__(self, name):
        d = getattr(_ctx, "g_dict", None)
        if d is None or name not in d:
            raise AttributeError(name)
        return d[name]

    def __setattr__(self, name, value):
        if not hasattr(_ctx, "g_dict"):
            _ctx.g_dict = {}
        _ctx.g_dict[name] = value

    def get(self, name, default=None):
        d = getattr(_ctx, "g_dict", None)
        if d is None:
            return default
        return d.get(name, default)


g = _G()


class FileStorage:
    def __init__(self, stream: bytes, filename: str, content_type: str = "application/octet-stream"):
        self.stream = io.BytesIO(stream)
        self.filename = filename
        self.content_type = content_type

    def save(self, dst):
        if isinstance(dst, (str, os.PathLike)):
            with open(dst, "wb") as f:
                f.write(self.stream.getvalue())
        else:
            dst.write(self.stream.getvalue())

    def read(self, *args):
        return self.stream.read(*args)


class _Headers:
    def __init__(self, raw_headers):
        self._map = {k.lower(): v for k, v in raw_headers.items()}
        self._orig = dict(raw_headers)

    def get(self, key, default=None):
        return self._map.get(key.lower(), default)

    def __getitem__(self, key):
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __contains__(self, key):
        return key.lower() in self._map


class _RequestProxy:
    @property
    def method(self):
        return _ctx.req["method"]

    @property
    def path(self):
        return _ctx.req["path"]

    @property
    def headers(self):
        return _ctx.req["headers"]

    @property
    def args(self):
        return _ctx.req["args"]

    @property
    def files(self):
        return _ctx.req["files"]

    @property
    def form(self):
        return _ctx.req["form"]

    @property
    def data(self):
        return _ctx.req["body"]

    def get_json(self, force=False, silent=False):
        body = _ctx.req["body"]
        if not body:
            if silent:
                return None
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            return parsed if parsed is not None else {}
        except Exception as e:
            if silent:
                return None
            raise ValueError(f"Invalid JSON body: {e}") from e


request = _RequestProxy()


class Response:
    def __init__(self, body=b"", status=200, headers=None, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.data = body
        self.status_code = int(status)
        self.headers = dict(headers or {})
        self.content_type = content_type


def jsonify(*args, **kwargs):
    if args and kwargs:
        raise TypeError("jsonify() behavior undefined when passed both args and kwargs")
    if len(args) == 1:
        payload = args[0]
    elif args:
        payload = list(args)
    else:
        payload = kwargs
    raw = json.dumps(payload).encode("utf-8")
    return Response(raw, status=200, content_type="application/json")


def send_file(path_or_file, mimetype=None, as_attachment=False, download_name=None):
    if isinstance(path_or_file, (str, os.PathLike)):
        path = str(path_or_file)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as f:
            data = f.read()
        if download_name is None:
            download_name = os.path.basename(path)
    else:
        data = path_or_file.read()
        download_name = download_name or "download"

    if mimetype is None:
        guessed, _ = mimetypes.guess_type(download_name or "")
        mimetype = guessed or "application/octet-stream"

    headers = {}
    if as_attachment:
        safe_name = (download_name or "download").replace('"', "")
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return Response(data, status=200, headers=headers, content_type=mimetype)


def send_from_directory(directory, path, **kwargs):
    full_path = os.path.abspath(os.path.join(directory, path))
    base_dir = os.path.abspath(directory)
    if not full_path.startswith(base_dir + os.sep) and full_path != base_dir:
        raise PermissionError("Invalid path")
    if not os.path.isfile(full_path):
        raise FileNotFoundError(path)
    return send_file(full_path, **kwargs)


def _parse_multipart(body: bytes, content_type: str):
    files = {}
    form = {}
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return files, form
    boundary = m.group(1).strip().strip('"').encode("ascii")
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    for part in parts[1:]:
        if part.startswith(b"--") or not part.strip():
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]

        sep = b"\r\n\r\n" if b"\r\n\r\n" in part else b"\n\n"
        if sep not in part:
            continue
        header_bytes, content = part.split(sep, 1)
        header_text = header_bytes.decode("utf-8", errors="replace")
        disp = ""
        part_ct = "application/octet-stream"
        for line in header_text.splitlines():
            if line.lower().startswith("content-disposition:"):
                disp = line.split(":", 1)[1].strip()
            elif line.lower().startswith("content-type:"):
                part_ct = line.split(":", 1)[1].strip()

        name_m = re.search(r'name="([^"]+)"', disp)
        if not name_m:
            continue
        field_name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', disp)
        if fn_m is not None:
            files[field_name] = FileStorage(content, fn_m.group(1), part_ct)
        else:
            form[field_name] = content.decode("utf-8", errors="replace")
    return files, form


def _compile_route(rule: str):
    pattern = re.sub(r"<(?:(?:string|int|path):)?([a-zA-Z_][a-zA-Z0-9_]*)>", r"(?P<\1>[^/]+)", rule)
    return re.compile(f"^{pattern}$")


class Flask:
    def __init__(self, import_name, static_folder=None, static_url_path=None):
        self.import_name = import_name
        self.config = {}
        self.logger = logging.getLogger(import_name)
        self._routes = []  # (methods_set, regex, rule, fn)
        self._error_handlers = []
        self._after_request_funcs = []

    def route(self, rule, methods=None):
        methods_set = {m.upper() for m in (methods or ["GET"])}
        regex = _compile_route(rule)
        def decorator(fn):
            self._routes.append((methods_set, regex, rule, fn))
            return fn
        return decorator

    def get(self, rule):
        return self.route(rule, methods=["GET"])

    def post(self, rule):
        return self.route(rule, methods=["POST"])

    def put(self, rule):
        return self.route(rule, methods=["PUT"])

    def patch(self, rule):
        return self.route(rule, methods=["PATCH"])

    def delete(self, rule):
        return self.route(rule, methods=["DELETE"])

    def after_request(self, fn):
        self._after_request_funcs.append(fn)
        return fn

    def _apply_after_request(self, resp):
        for fn in self._after_request_funcs:
            resp = fn(resp) or resp
        return resp

    def errorhandler(self, exc_class_or_code):
        def decorator(fn):
            self._error_handlers.append((exc_class_or_code, fn))
            return fn
        return decorator

    def _normalize_response(self, rv):
        status = 200
        headers = {}
        if isinstance(rv, tuple):
            if len(rv) == 2:
                rv, status = rv
            elif len(rv) == 3:
                rv, status, headers = rv
        if isinstance(rv, Response):
            if status != 200 or headers:
                rv.status_code = int(status)
                rv.headers.update(headers)
            return rv
        if isinstance(rv, dict):
            resp = jsonify(rv)
            resp.status_code = int(status)
            resp.headers.update(headers)
            return resp
        if isinstance(rv, (str, bytes)):
            ct = "text/html; charset=utf-8" if isinstance(rv, str) else "application/octet-stream"
            return Response(rv, status=status, headers=headers, content_type=ct)
        raise TypeError(f"Unsupported response type: {type(rv)}")

    def _dispatch(self, method: str, full_path: str, headers_dict: dict, body: bytes) -> Response:
        parsed_url = urllib.parse.urlsplit(full_path)
        path = parsed_url.path or "/"
        qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
        args = {k: v[-1] if v else "" for k, v in qs.items()}

        ct = headers_dict.get("Content-Type") or headers_dict.get("content-type") or ""
        files, form = {}, {}
        if "multipart/form-data" in ct.lower():
            files, form = _parse_multipart(body, ct)

        _ctx.req = {
            "method": method.upper(),
            "path": path,
            "headers": _Headers(headers_dict),
            "args": args,
            "files": files,
            "form": form,
            "body": body,
        }
        _ctx.g_dict = {}

        try:
            matched_path = False
            for methods_set, regex, _rule, fn in self._routes:
                m = regex.match(path)
                if m:
                    matched_path = True
                    if method.upper() in methods_set or (method.upper() == "HEAD" and "GET" in methods_set):
                        kwargs = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
                        rv = fn(**kwargs)
                        return self._apply_after_request(self._normalize_response(rv))
            if matched_path:
                raise HTTPException("Method Not Allowed", code=405)
            raise HTTPException("Not Found", code=404)
        except Exception as e:
            for exc_cls, handler in self._error_handlers:
                if isinstance(exc_cls, type) and issubclass(exc_cls, BaseException) and isinstance(e, exc_cls):
                    return self._apply_after_request(self._normalize_response(handler(e)))
            if isinstance(e, HTTPException):
                return Response(
                    json.dumps({"error": e.description}).encode("utf-8"),
                    status=e.code,
                    content_type="application/json",
                )
            self.logger.exception(e)
            return Response(
                json.dumps({"error": "internal_error", "detail": str(e)}).encode("utf-8"),
                status=500,
                content_type="application/json",
            )

    def run(self, host="127.0.0.1", port=8000, debug=False):
        flask_app = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle_all(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length > 0 else b""
                hdrs = {k: v for k, v in self.headers.items()}
                resp = flask_app._dispatch(self.command, self.path, hdrs, body)
                self.send_response(resp.status_code)
                self.send_header("Content-Type", resp.content_type)
                self.send_header("Content-Length", str(len(resp.data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                for hk, hv in resp.headers.items():
                    self.send_header(hk, hv)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(resp.data)

            def do_GET(self):
                self._handle_all()

            def do_POST(self):
                self._handle_all()

            def do_PUT(self):
                self._handle_all()

            def do_PATCH(self):
                self._handle_all()

            def do_DELETE(self):
                self._handle_all()

            def do_HEAD(self):
                self._handle_all()

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer((host, int(port)), _Handler)
        print(f"DocEngine server listening on http://{host}:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
