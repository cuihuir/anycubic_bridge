"""Focused unit tests for path handling and response compatibility."""

from anycubic_bridge.app import (
    Settings,
    argument_parser,
    anycubic_body_is_success,
    moonraker_upload_response,
    safe_filename,
    safe_relative_path,
)


def test_safe_filename_accepts_gcode_name():
    assert safe_filename("nested/model.gcode") == "model.gcode"


def test_safe_relative_path_preserves_subdirectory():
    assert safe_relative_path("usb/test", "model.gcode") == "usb/test/model.gcode"


def test_safe_relative_path_rejects_traversal():
    try:
        safe_relative_path("../outside", "model.gcode")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal was accepted")


def test_anycubic_error_code_is_not_success():
    assert anycubic_body_is_success({"code": 19007}) is False
    assert anycubic_body_is_success({"code": 200}) is True
    assert anycubic_body_is_success({"message": "ok"}) is True


def test_moonraker_response_contains_upload_metadata():
    result = moonraker_upload_response("model.gcode", 123)
    assert result["action"] == "create_file"
    assert result["item"]["root"] == "gcodes"
    assert result["item"]["size"] == 123


def test_printer_host_cli_argument_overrides_environment(monkeypatch):
    monkeypatch.setenv("PRINTER_HOST", "192.168.31.105")
    args = argument_parser().parse_args(["--printer-ip", "192.168.31.106"])

    settings = Settings.from_env(printer_host=args.printer_host)

    assert settings.printer_host == "192.168.31.106"
