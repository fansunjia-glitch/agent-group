"""Loopback-only HTTP API and static console, with same-origin write protection."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
from threading import Thread
from urllib.parse import parse_qs, unquote, urlsplit

from .service import ControlError, ControlService


def make_server(service, port=8765):
    static = Path(__file__).parent / "static"

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(30)
            super().setup()

        def log_message(self, *_):
            pass

        def reply(self, status, value, content_type="application/json; charset=utf-8"):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def verify_origin(self, *, mutation=False):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            if host not in allowed:
                raise ControlError("Untrusted Host header", 403)
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                raise ControlError("Cross-origin requests are not allowed", 403)
            if mutation and not secrets.compare_digest(self.headers.get("X-Control-Token", ""), service.csrf_token):
                raise ControlError("Missing or invalid control token; refresh the page", 403)

        def do_GET(self):
            try:
                self.verify_origin()
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == "/api/state":
                    self.reply(200, service.snapshot())
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "topics"] and parts[3] == "messages":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if any(key not in ("before", "after", "limit", "q") or len(value) != 1 for key, value in query.items()):
                        raise ControlError("Invalid history query parameters")
                    def integer(key, default=None):
                        if key not in query:
                            return default
                        raw = query[key][0]
                        if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
                            raise ControlError(f"{key} must be a non-negative integer")
                        return int(raw)
                    self.reply(200, service.topic_messages(unquote(parts[2]), before=integer("before"),
                               after=integer("after"), limit=integer("limit", 50), query=query.get("q", [""])[0]))
                    return
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/index.html": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/topology.js": ("topology.js", "text/javascript; charset=utf-8"),
                          "/topology.css": ("topology.css", "text/css; charset=utf-8"),
                          "/styles.css": ("styles.css", "text/css; charset=utf-8")}
                if path not in assets:
                    raise ControlError("Not found", 404)
                filename, content_type = assets[path]
                self.reply(200, (static / filename).read_bytes(), content_type)
            except ControlError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except OSError:
                self.reply(503, {"error": "Console assets unavailable"})

        def mutate(self):
            try:
                self.verify_origin(mutation=True)
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ControlError("Use application/json", 415)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ControlError("Invalid content length") from None
                if not 0 < length <= 131072:
                    raise ControlError("JSON body must be between 1 byte and 128 KiB", 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ControlError("Incomplete JSON body")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ControlError("Invalid JSON body") from None
                if not isinstance(data, dict):
                    raise ControlError("JSON body must be an object")
                parts = urlsplit(self.path).path.strip("/").split("/")
                if self.command == "PUT" and len(parts) == 3 and parts[:2] == ["api", "model-providers"]:
                    result = service.save_model_provider(data, parts[2])
                elif self.command == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "model-providers"]:
                    result = service.delete_model_provider(parts[2])
                elif self.command == "PUT" and len(parts) == 3 and parts[:2] == ["api", "profiles"]:
                    result = service.save_profile(data, parts[2])
                elif self.command == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "profiles"]:
                    result = service.delete_profile(parts[2])
                elif self.command == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "topics"]:
                    result = service.delete_topic(parts[2])
                elif self.command == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "notices"]:
                    result = service.delete_notice(parts[2])
                elif self.command != "POST":
                    raise ControlError("Not found", 404)
                elif parts == ["api", "model-providers"]:
                    result = service.save_model_provider(data)
                elif parts == ["api", "credentials"]:
                    result = service.set_credentials(data)
                elif parts == ["api", "hosts"]:
                    result = service.add_host(data)
                elif len(parts) == 4 and parts[:2] == ["api", "hosts"] and parts[3] == "check":
                    result = service.check_host(parts[2])
                elif parts == ["api", "profiles"]:
                    result = service.save_profile(data)
                elif len(parts) == 4 and parts[:2] == ["api", "profiles"] and parts[3] in ("start", "stop"):
                    method = service.start_profile if parts[3] == "start" else service.stop_profile
                    result = method(parts[2])
                elif parts == ["api", "admin", "topic"]:
                    result = service.create_admin_topic(data)
                elif parts == ["api", "admin", "invite"]:
                    result = service.invite_all()
                elif parts == ["api", "notices"]:
                    result = service.create_notice(data)
                elif len(parts) == 4 and parts[:2] == ["api", "notices"] and parts[3] == "retry":
                    result = service.publish_notice(parts[2])
                else:
                    raise ControlError("Not found", 404)
                self.reply(200, result)
            except ControlError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except Exception:
                # Do not echo SDK exceptions/credentials to the browser.
                self.reply(500, {"error": "The operation failed; inspect the local control data and run logs"})

        do_POST = mutate
        do_PUT = mutate
        do_DELETE = mutate

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Agent Group local control console")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=".control")
    parser.add_argument("--comm-host", default=None,
                        help="Concrete local LAN/VPN IPv4 advertised to agent peers; UI always binds loopback")
    args = parser.parse_args()
    service = ControlService(args.data_dir, comm_host=args.comm_host)
    try:
        server = make_server(service, args.port)
    except BaseException:
        service.close()
        raise
    def stop(*_):
        Thread(target=server.shutdown, daemon=True).start()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        print(f"Agent Group: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Comm endpoint: {service.comm.endpoint}", flush=True)
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
        service.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
