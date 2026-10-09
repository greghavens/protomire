"""Download only three immutable public BERT files; verify every SHA256."""
import hashlib
import os
from pathlib import Path
import urllib.request
from modelbuilder.backend.fixtures import MODEL_FILES, MODEL_ID, MODEL_REVISION

root = Path(os.environ.get("MODELBUILDER_PRETRAINED_PATH", Path(__file__).resolve().parents[1] / ".cache/models/bert-tiny"))
root.mkdir(parents=True, exist_ok=True)
for name, expected in MODEL_FILES.items():
    path = root / name
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        print(f"Verified {name}: {expected}")
        continue
    temporary = path.with_suffix(path.suffix + ".download")
    urllib.request.urlretrieve(f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{name}", temporary)
    actual = hashlib.sha256(temporary.read_bytes()).hexdigest()
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Pinned upstream hash mismatch for {name}")
    os.replace(temporary, path)
    print(f"Downloaded and verified {name}: {expected}")
