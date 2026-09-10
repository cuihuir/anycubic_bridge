#!/usr/bin/env python3
"""Purpose: expose OctoPrint/Moonraker upload routes for an Anycubic LAN printer.

Dependencies: fastapi, httpx, python-multipart, and uvicorn.
Effect: accepts G-code over HTTP, fetches the printer's current upload URL from
the local /info endpoint, and forwards the file without logging dynamic tokens.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse


class BridgeError(RuntimeError):
    """Base error for bridge failures."""


class PrinterUploadError(BridgeError):
    """The Anycubic printer rejected or could not receive a file."""


@dataclass(frozen=True)
class Settings:
    printer_host: str
    printer_port: int
    listen_host: str
    port: int
    storage_path: Path
    max_upload_bytes: int
    upload_timeout_seconds: float
    allow_print: bool

    @classmethod
    def from_env(cls) -> "Settings":
        storage = os.environ.get(
            "STORAGE_PATH",
            "~/.local/share/anycubic-bridge/gcodes",
        )
        return cls(
            printer_host=os.environ.get("PRINTER_HOST", "192.168.31.105"),
            printer_port=int(os.environ.get("PRINTER_PORT", "18910")),
            listen_host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "7125")),
            storage_path=Path(storage).expanduser(),
            max_upload_bytes=int(
                os.environ.get("MAX_UPLOAD_BYTES", str(1024**3))
            ),
            upload_timeout_seconds=float(
                os.environ.get("UPLOAD_TIMEOUT_SECONDS", "120")
            ),
            allow_print=os.environ.get("ALLOW_PRINT", "false").lower()
            in {"1", "true", "yes", "on"},
        )


def parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def safe_filename(value: str | None) -> str:
    """Return a single safe filename and reject path traversal."""
    raw = (value or "").replace("\\", "/")
    name = raw.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise ValueError("A filename is required")
    if name.startswith(".") or "\x00" in name:
        raise ValueError("Hidden or invalid filenames are not accepted")
    if not name.lower().endswith((".gcode", ".gco", ".g")):
        raise ValueError("Only G-code files are accepted")
    return name


def safe_relative_path(value: str | None, filename: str) -> str:
    """Join an optional POSIX subdirectory with a validated filename."""
    raw_path = (value or "").replace("\\", "/").strip("/")
    if raw_path:
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Upload path may not be absolute or traverse upward")
        parts = [part for part in path.parts if part not in {"", "."}]
    else:
        parts = []
    parts.append(filename)
    return "/".join(parts)


def anycubic_body_is_success(payload: Any) -> bool:
    """Interpret the response codes seen on the Kobra LAN HTTP API."""
    if not isinstance(payload, dict) or "code" not in payload:
        return True
    return payload.get("code") in {0, 200}


def anycubic_error_message(status_code: int, payload: Any) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("msg")
        if message:
            return f"Printer returned HTTP {status_code}: {message}"
        code = payload.get("code")
        if code is not None:
            return f"Printer rejected the file with code {code}"
    return f"Printer returned HTTP {status_code}"


async def read_upload(file: UploadFile, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise ValueError(f"File exceeds the {maximum} byte upload limit")
        chunks.append(chunk)
    return b"".join(chunks)


class AnycubicClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def info_url(self) -> str:
        return (
            f"http://{self.settings.printer_host}:"
            f"{self.settings.printer_port}/info"
        )

    async def info(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.upload_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.get(self.info_url)
        except httpx.HTTPError as exc:
            raise PrinterUploadError(
                f"Could not reach the printer info endpoint: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise PrinterUploadError(
                f"Printer info endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PrinterUploadError(
                "Printer info endpoint did not return JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise PrinterUploadError("Printer info response has an invalid shape")
        return payload

    async def upload(self, filename: str, content: bytes) -> dict[str, Any]:
        info = await self.info()
        upload_url = info.get("fileUploadurl")
        if not isinstance(upload_url, str) or not upload_url:
            raise PrinterUploadError(
                "Printer info did not provide fileUploadurl"
            )

        parsed = urlparse(upload_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PrinterUploadError("Printer returned an invalid upload URL")

        files = {
            "file": (filename, content, "application/octet-stream"),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.upload_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(upload_url, files=files)
        except httpx.HTTPError as exc:
            raise PrinterUploadError(
                f"Could not upload the file to the printer: {exc}"
            ) from exc

        try:
            payload: Any = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400 or not anycubic_body_is_success(payload):
            raise PrinterUploadError(
                anycubic_error_message(response.status_code, payload)
            )
        if isinstance(payload, dict):
            return payload
        return {"printer_response": payload}


class Bridge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.printer = AnycubicClient(settings)
        self.settings.storage_path.mkdir(parents=True, exist_ok=True)

    def local_file(self, relative_path: str) -> Path:
        root = self.settings.storage_path.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("Resolved path is outside the storage directory")
        return candidate

    def list_files(self) -> list[dict[str, Any]]:
        root = self.settings.storage_path.resolve()
        entries: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {
                ".gcode",
                ".gco",
                ".g",
            }:
                continue
            relative = path.relative_to(root).as_posix()
            stat = path.stat()
            entries.append(
                {
                    "path": relative,
                    "root": "gcodes",
                    "modified": stat.st_mtime,
                    "size": stat.st_size,
                    "permissions": "rw",
                }
            )
        return entries

    async def upload(
        self,
        file: UploadFile,
        path: str | None,
        print_after: bool,
    ) -> tuple[str, int]:
        if print_after:
            if not self.settings.allow_print:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "Print disabled",
                        "message": (
                            "The file was not forwarded with print=true because "
                            "automatic print start is disabled."
                        ),
                    },
                )
            raise HTTPException(
                status_code=501,
                detail={
                    "error": "Print start unavailable",
                    "message": (
                        "The Anycubic upload bridge has no verified start-print "
                        "operation yet."
                    ),
                },
            )

        try:
            filename = safe_filename(file.filename)
            relative = safe_relative_path(path, filename)
            content = await read_upload(
                file,
                self.settings.max_upload_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await self.printer.upload(filename, content)

        local_path = self.local_file(relative)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        return relative, len(content)


def octoprint_upload_response(relative: str) -> dict[str, Any]:
    return {
        "files": {
            "local": {
                "name": relative.rsplit("/", 1)[-1],
                "path": relative,
                "origin": "local",
            }
        },
        "done": True,
    }


def moonraker_upload_response(relative: str, size: int) -> dict[str, Any]:
    return {
        "action": "create_file",
        "item": {
            "path": relative,
            "root": "gcodes",
            "modified": time.time(),
            "size": size,
            "permissions": "rw",
        },
        "print_started": False,
        "print_queued": False,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    bridge = Bridge(settings)
    app = FastAPI(
        title="Anycubic Bridge",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.bridge = bridge

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"message": "Anycubic Bridge"}

    @app.get("/api/version")
    async def api_version() -> dict[str, str]:
        return {
            "server": "1.5.0",
            "api": "0.1",
            "text": "OctoPrint (Anycubic Bridge)",
        }

    @app.get("/api/connection")
    async def api_connection() -> dict[str, Any]:
        return {
            "current": {
                "state": "Operational",
                "connectivity": "operational",
            },
            "options": {
                "port": settings.port,
                "printer_host": settings.printer_host,
            },
        }

    @app.get("/api/files/local")
    async def api_files_local() -> dict[str, Any]:
        return {
            "files": [
                {
                    "name": item["path"].rsplit("/", 1)[-1],
                    "path": item["path"],
                    "origin": "local",
                    "size": item["size"],
                    "date": item["modified"],
                }
                for item in bridge.list_files()
            ]
        }

    @app.post("/api/files/local")
    async def upload_octoprint(
        file: UploadFile = File(...),
        path: str | None = Form(None),
        print_after: str | None = Form(None, alias="print"),
    ) -> JSONResponse:
        try:
            relative, size = await bridge.upload(
                file=file,
                path=path,
                print_after=parse_bool(print_after),
            )
            result = octoprint_upload_response(relative)
            headers = {"Location": f"/api/files/local/{relative}"}
            return JSONResponse(
                status_code=200,
                content=result,
                headers=headers,
            )
        except PrinterUploadError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Printer upload failed",
                    "message": str(exc),
                },
            )

    @app.get("/server/info")
    async def server_info() -> dict[str, Any]:
        return {
            "result": {
                "klippy_connected": False,
                "klippy_state": "ready",
                "moonraker_version": "anycubic-bridge-0.1.0",
                "api_version": [1, 0, 0],
            }
        }

    @app.get("/server/files/list")
    async def server_files_list(
        root: str = Query("gcodes"),
        _: str | None = Query(None, alias="path"),
    ) -> dict[str, Any]:
        if root != "gcodes":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file root: {root}",
            )
        return {"result": bridge.list_files()}

    @app.post("/server/files/upload")
    async def upload_moonraker(
        file: UploadFile = File(...),
        root: str = Form("gcodes"),
        path: str | None = Form(None),
        checksum: str | None = Form(None),
        print_after: str | None = Form(None, alias="print"),
    ) -> JSONResponse:
        del checksum
        if root != "gcodes":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file root: {root}",
            )
        try:
            relative, size = await bridge.upload(
                file=file,
                path=path,
                print_after=parse_bool(print_after),
            )
            return JSONResponse(
                status_code=201,
                content=moonraker_upload_response(relative, size),
                headers={"Location": f"/gcodes/{relative}"},
            )
        except PrinterUploadError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "Printer upload failed",
                    "message": str(exc),
                },
            )

    @app.get("/printer/info")
    async def printer_info() -> dict[str, Any]:
        return {
            "result": {
                "state": "ready",
                "hostname": settings.printer_host,
                "software_version": "Anycubic LAN",
                "extruder": {
                    "temperature": 0.0,
                    "target": 0.0,
                },
                "heater_bed": {
                    "temperature": 0.0,
                    "target": 0.0,
                },
            }
        }

    @app.post("/printer/gcode/script")
    async def gcode_script() -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={
                "error": {
                    "code": 501,
                    "message": (
                        "Raw Klipper G-code execution is not available on "
                        "this Anycubic firmware bridge."
                    ),
                }
            },
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "anycubic_bridge.app:app",
        host=settings.listen_host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
