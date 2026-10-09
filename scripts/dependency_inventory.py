#!/usr/bin/env python3
"""Report installed distribution metadata; not a substitute for release license review."""
import hashlib
import importlib.metadata as metadata
import json
import sys

result = []
for dist in metadata.distributions():
    item = {"name": dist.metadata["Name"], "version": dist.version,
            "license_expression": dist.metadata.get("License-Expression"),
            "license_classifiers": [x for x in dist.metadata.get_all("Classifier", []) if "License" in x],
            "license_files": []}
    for file in dist.files or []:
        if any(part.lower() in {"licenses", "license", "license.txt", "copying", "notice", "notice.txt"} for part in file.parts):
            path = dist.locate_file(file)
            if path.is_file():
                item["license_files"].append({"path": str(file), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    result.append(item)
json.dump({"python": sys.version.split()[0], "distributions": sorted(result, key=lambda x: x["name"].lower())}, sys.stdout, indent=2)
print()
