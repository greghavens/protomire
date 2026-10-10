"""Standalone safe Torch loader; no Modelbuilder install needed after export."""
import hashlib
import json
import math
from pathlib import Path
import sys
import torch


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load(root):
    root = Path(root)
    manifest = json.loads((root / "export.manifest.json").read_text())
    declared = {item["path"]: item["sha256"] for item in manifest["assets"]}
    for name in ("weights.pt", "calibration.json", "loader.py", "parity.json"):
        if name not in declared or hashlib.sha256((root / name).read_bytes()).hexdigest() != declared[name]:
            raise ValueError("Missing or tampered required export asset: " + name)
    calibration = json.loads((root / "calibration.json").read_text())
    if calibration["digest"] != digest({k: v for k, v in calibration.items() if k != "digest"}):
        raise ValueError("Calibration digest mismatch")
    if calibration["digest"] != manifest["calibration"]["identity"]:
        raise ValueError("Selected calibration identity mismatch")
    if calibration["model_identity"] != declared["weights.pt"]:
        raise ValueError("Calibration belongs to another model")
    temperature = calibration["temperature"]
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Calibration temperature must be finite and positive")
    torch.set_num_threads(1)
    model = torch.nn.Sequential(torch.nn.Linear(3, 6), torch.nn.Tanh(), torch.nn.Linear(6, 3)).double().eval()
    model.load_state_dict(torch.load(root / "weights.pt", weights_only=True, map_location="cpu"))
    return model, temperature


if __name__ == "__main__":
    root = Path(sys.argv[1])
    model, temperature = load(root)
    parity = json.loads((root / "parity.json").read_text())
    with torch.no_grad():
        actual = model(torch.tensor(parity["inputs"], dtype=torch.float64)) / temperature
    expected = torch.tensor(parity["predictions"], dtype=torch.float64)
    difference = float((actual - expected).abs().max())
    assert difference == 0.0
    assert "modelbuilder" not in sys.modules
    print(json.dumps({"passed": True, "atol": 0., "max_abs_difference": difference, "fresh_process": True, "platform_imported": False}))
