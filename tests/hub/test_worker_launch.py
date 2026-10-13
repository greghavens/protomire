"""The isolated SDK worker must not write bytecode into a sealed runtime."""
import io
import json
from pathlib import Path
import sys

from modelbuilder.hub_provider import HuggingFaceProvider


def test_worker_launch_isolated_no_bytecode_and_retains_anonymous_boundary(monkeypatch):
    captured = {}

    class Input(io.BytesIO):
        def close(self):
            request = json.loads(self.getvalue())
            captured["request"] = request
            Path(request["result_path"]).write_text('{"license":"mit"}')
            super().close()

    class Process:
        def __init__(self, command, **kwargs):
            captured.update(command=command, environment=kwargs["env"])
            self.stdin = Input()

        def poll(self):
            return 0

    monkeypatch.setattr("modelbuilder.hub_provider.subprocess.Popen", Process)
    result = HuggingFaceProvider(sys.executable).call("card.inspect", {"card": "---\nlicense: mit\n---\nSynthetic fixture"})
    assert result == {"license": "mit"}
    assert captured["command"][1:3] == ["-I", "-B"]
    assert captured["command"][-1].endswith("hub_sdk_worker.py")
    assert captured["request"]["credential"] is None
    assert captured["environment"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
