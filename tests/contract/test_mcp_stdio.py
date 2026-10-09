"""External client acceptance: real stdio + HTTP + SQLite, no mocked service."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


async def test_independent_stdio_client_matches_http_and_survives_restart(tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts" / "mcp_smoke.py"
    spec = spec_from_file_location("external_mcp_acceptance", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    result = await module.run_smoke(tmp_path / "evidence")
    assert result["status"] == "PASS"
    assert result["api_mcp_full_resource_equality"]
    assert result["read_only_denied_operations"]
    assert result["same_live_mcp_process_across_service_restart"]
