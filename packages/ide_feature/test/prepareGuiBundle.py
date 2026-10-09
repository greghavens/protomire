"""Prepare an inert GUI import fixture from the shipping manifest's exact Git blobs."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from modelbuilder.recipe import export_bundle

root, manifest_path, destination = map(Path, sys.argv[1:])
recipe = json.loads(manifest_path.read_text())
with tempfile.TemporaryDirectory(prefix="gui-import-source-") as directory:
    source = Path(directory)
    for item in recipe["source"]["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Fixture source path must be repository-relative")
        data = subprocess.check_output(["git", "-C", str(root), "cat-file", "blob", f"{recipe['source']['commit']}:{relative.as_posix()}"])
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    # Shared canonical exporter checks each declared source SHA256. No code runs.
    bundle = export_bundle(recipe, source_root=source)
    destination.write_text(json.dumps(bundle))
print(json.dumps({"recipe": recipe["name"], "source_commit": recipe["source"]["commit"], "source_files": len(bundle["files"]), "recipe_digest": bundle["digest"], "bundle_digest": bundle["bundle_digest"]}))
