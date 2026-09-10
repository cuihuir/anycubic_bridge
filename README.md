# Anycubic Bridge

`anycubic_bridge` is a small local HTTP facade for an Anycubic printer with
the LAN service exposed on port `18910`. It accepts the upload request shape
used by OrcaSlicer through the OctoPrint API and forwards the file to the
printer's current `gcode_upload` URL.

The first target is the Anycubic Kobra 3 discovered at
`192.168.31.105` (`modelId=20024`). The printer's `/info` response contains a
short-lived upload URL. The bridge fetches that URL for every upload, so no
token is stored in the source tree or configuration.

## Current scope

Implemented:

- `GET /api/version` for OrcaSlicer OctoPrint connection detection
- `POST /api/files/local` with `file`, `path`, and `print` form fields
- `POST /server/files/upload` with the Moonraker upload shape
- `GET /api/files/local` and `GET /server/files/list` for the bridge spool
- basic `/server/info` and `/printer/info` compatibility responses
- explicit errors for raw Klipper G-code execution

The bridge currently supports file upload only. It does not start a print,
move the toolhead, heat the nozzle or bed, or execute arbitrary raw Klipper
commands. `print=true` is rejected until a verified Anycubic start-print
operation is implemented.

## Install and run

Python 3.10 or newer and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
cd projects/anycubic_bridge
uv sync
uv run anycubic-bridge
```

The default listener is `127.0.0.1:7125`. To expose it to Mainsail or Fluidd
from another LAN host, set `LISTEN_HOST=0.0.0.0` and choose a firewall rule
appropriate for the local network.

Configuration is read from environment variables. A starting point is:

```bash
cp .env.example .env
uv run --env-file .env anycubic-bridge
```

Change `PRINTER_HOST` when the printer receives a different DHCP address.

`uv sync` creates and maintains the project environment in `.venv`, and all
commands should run through `uv run`. The committed `uv.lock` pins the
resolved dependency versions. Use `uv lock --upgrade` only when intentionally
updating dependencies.

## OrcaSlicer setup

Configure a printer host using the OctoPrint protocol:

```text
Host: 127.0.0.1:7125
API key: leave empty
```

The upload request is:

```text
POST /api/files/local
Content-Type: multipart/form-data
file=<gcode>
path=<optional-subdirectory>
print=false
```

The bridge returns an OctoPrint-compatible success body and keeps a local
copy under `STORAGE_PATH` after the printer accepts the upload.

## Direct upload check

The bridge can be checked without running OrcaSlicer:

```bash
uv run pytest -q
curl http://127.0.0.1:7125/api/version
curl -F 'file=@/path/to/model.gcode' \
  -F 'print=false' \
  http://127.0.0.1:7125/api/files/local
```

The bridge's printer client uses `httpx` with environment proxies disabled.
The printer response is treated as successful only when its HTTP response is
successful and an optional JSON `code` is either `0` or `200`.

## LAN facts collected during exploration

For the target Kobra 3:

```text
18910/tcp  HTTP info and G-code upload
9883/tcp   TLS MQTT state/control
18088/tcp  camera stream
```

The `/info` endpoint is read-only for this project. Its dynamic token and the
MQTT credentials returned by the LAN pairing flow must remain outside Git,
logs, issue reports, and documentation.

## References

- OrcaSlicer upload research:
  https://github.com/cuihuir/orcaslicer_upload_research
- Anycubic LAN protocol research:
  https://gitlab.com/Grunna/Anycubic-Kobra-X-Lan

This is an unofficial project and is not affiliated with Anycubic.
