"""Apply approved Linux display assets to a package copy, outside editor source.

Application IDs, executable names, profiles and the sealed core stay unchanged.
The approved icon is a raster PNG; only the supplied wordmark has vector outlines.
"""
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "distribution/assets/protomire"


def owned_target(root: Path, relative: str) -> Path:
    root = root.resolve()
    target = root / relative
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symlink in package branding target: " + relative)
    if not target.resolve().is_relative_to(root):
        raise ValueError("Branding target escapes package")
    return target


def apply_linux_branding(app_root: Path, desktop_id: str) -> None:
    product_path = owned_target(app_root, "product.json")
    product = json.loads(product_path.read_text())
    if product.get("applicationName") != "modelbuilder-ide":
        raise ValueError("Brand only the existing compatible application identity")
    overlay = json.loads((ROOT / "distribution/overlay/product.json").read_text())
    for field in ("nameShort", "nameLong"):
        product[field] = overlay[field]
    product_path.write_text(json.dumps(product, indent=2) + "\n")
    # Electron uses desktopName for Wayland app identity and desktop icon
    # matching. Keep name unchanged: it also owns the native credential identity.
    package_path = owned_target(app_root, "package.json")
    package = json.loads(package_path.read_text())
    package["desktopName"] = desktop_id + ".desktop"
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    icon = owned_target(app_root, "resources/linux/code.png")
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "icons/light-tile/protomire-512.png", icon)
    for source, name in (("README.md", "README.md"), ("fonts/OFL.txt", "InterTight-OFL.txt"),
                         ("asset-manifest.json", "asset-manifest.json")):
        notice = owned_target(app_root, "protomire-brand/" + name)
        notice.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASSETS / source, notice)
