"""Small, dependency-free client for AI Video Studio's /api/v1 HTTP API.

Reads AIVS_BASE_URL and optional AIVS_API_KEY from the environment. JSON bodies
come from a file so shell quoting cannot silently change retry fingerprints.
"""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def connection(base_url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("AIVS_BASE_URL must be an http(s) URL ending in /api/v1")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AIVS_BASE_URL must not contain credentials, query, or fragment")
    if parsed.path.rstrip("/") != "/api/v1":
        raise ValueError("AIVS_BASE_URL must end in /api/v1")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Use HTTPS for a non-loopback AIVS_BASE_URL")
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return cls(parsed.hostname, parsed.port, timeout=timeout), parsed.path.rstrip("/")


def chunks(data: bytes | None, upload: Path | None, boundary: str, kind: str):
    if upload is None:
        if data is not None:
            yield data
        return
    filename = upload.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\n{kind}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    yield prefix
    with upload.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            yield block
    yield f"\r\n--{boundary}--\r\n".encode("ascii")


def multipart_size(upload: Path, boundary: str, kind: str) -> int:
    parts = chunks(None, upload, boundary, kind)
    prefix = next(parts)
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return len(prefix) + upload.stat().st_size + len(suffix)


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in {"key", "api_key", "token", "access_token", "secret"} else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    parser.add_argument("path", help="Path relative to /api/v1, starting with /; query string is allowed")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--body-file", type=Path, help="JSON request bytes, kept unchanged for retries")
    source.add_argument("--upload", type=Path, help="File to upload as multipart field 'file'")
    parser.add_argument("--kind", default="upload", help="Multipart kind field, default: upload")
    parser.add_argument("--if-match", help="ETag from the latest resource GET, including its quotes")
    parser.add_argument("--idempotency-key", help="Reuse only for an identical retry")
    parser.add_argument("--output", type=Path, help="Save response bytes to a file instead of stdout")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    if not args.path.startswith("/") or args.path.startswith("//") or "#" in args.path:
        parser.error("path must start with one / and have no fragment")
    if args.upload and args.method != "POST":
        parser.error("--upload requires POST")
    if args.method == "GET" and (args.body_file or args.upload):
        parser.error("GET cannot have a request body")
    base_url = os.environ.get("AIVS_BASE_URL")
    if not base_url:
        parser.error("set AIVS_BASE_URL to the service URL ending in /api/v1")

    try:
        conn, prefix = connection(base_url, args.timeout)
        body = args.body_file.read_bytes() if args.body_file else None
        boundary = "aivs-" + uuid.uuid4().hex
        if args.upload:
            size = multipart_size(args.upload, boundary, args.kind)
        else:
            size = len(body) if body is not None else 0
        headers = {"Accept": "application/json"}
        if key := os.environ.get("AIVS_API_KEY"):
            headers["Authorization"] = "Bearer " + key
        if args.if_match:
            headers["If-Match"] = args.if_match
        if args.upload:
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            headers["Content-Type"] = "application/json"
        if body is not None or args.upload:
            headers["Content-Length"] = str(size)
        if args.method != "GET" and not args.path.startswith("/auth/"):
            idem = args.idempotency_key or uuid.uuid4().hex
            if not (1 <= len(idem) <= 128) or not idem.isascii() or any(c.isspace() for c in idem):
                parser.error("Idempotency-Key must be 1-128 non-whitespace ASCII characters")
            headers["Idempotency-Key"] = idem
            print(f"Idempotency-Key: {idem}", file=sys.stderr, flush=True)
        elif args.idempotency_key:
            parser.error("this endpoint does not accept an Idempotency-Key")

        conn.putrequest(args.method, prefix + args.path)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders()
        for part in chunks(body, args.upload, boundary, args.kind):
            conn.send(part)
        response = conn.getresponse()
        print(f"HTTP {response.status} {response.reason}", file=sys.stderr)
        for name in ("ETag", "X-Request-ID", "Idempotency-Replayed"):
            if value := response.getheader(name):
                print(f"{name}: {value}", file=sys.stderr)
        if response.status >= 400:
            raw = response.read(65536)
            try:
                error = json.loads(raw)
                print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
            except (ValueError, UnicodeDecodeError):
                print(raw.decode("utf-8", errors="replace"), file=sys.stderr)
            return 1
        if args.output:
            with args.output.open("wb") as stream:
                while block := response.read(1024 * 1024):
                    stream.write(block)
            print(f"Saved: {args.output}", file=sys.stderr)
        else:
            raw = response.read()
            if raw:
                if "json" in (response.getheader("Content-Type") or ""):
                    value = json.loads(raw)
                    if args.path.startswith("/auth/keys"):
                        value = redact(value)
                    print(json.dumps(value, ensure_ascii=False, indent=2))
                else:
                    print(raw.decode("utf-8", errors="replace"))
        return 0
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if "conn" in locals():
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
