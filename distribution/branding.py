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
    # The custom workbench titlebar/banner icons are separate from the native
    # BrowserWindow icon. Keep this a pinned visual-resource overlay, outside
    # upstream source; never disguise the approved raster as an SVG.
    stylesheet_path = owned_target(app_root, "out/vs/workbench/workbench.desktop.main.css")
    stylesheet = stylesheet_path.read_text()
    old_icon = 'url("../../media/code-icon.svg")'
    new_icon = 'url("../../media/protomire-icon.png")'
    counts = (stylesheet.count(old_icon), stylesheet.count(new_icon))
    if counts not in {(5, 0), (0, 5)}:
        raise ValueError("Unrecognized pinned workbench icon stylesheet; no branding export")
    stylesheet = stylesheet.replace(old_icon, new_icon)
    watermark_urls = [f'url("../../media/letterpress-{theme}.svg")'
                      for theme in ("light", "dark", "hcLight", "hcDark")]
    new_watermark = 'url("../../media/protomire-watermark.png")'
    watermark_counts = tuple(stylesheet.count(url) for url in watermark_urls)
    if watermark_counts == (1, 1, 1, 1) and not stylesheet.count(new_watermark):
        for url in watermark_urls:
            stylesheet = stylesheet.replace(url, new_watermark)
    elif watermark_counts != (0, 0, 0, 0) or stylesheet.count(new_watermark) != 4:
        raise ValueError("Unrecognized pinned workbench watermark stylesheet; no branding export")
    watermark_style = '\n/* Protomire visual branding; approved PNG, no editor behavior changes. */\n.monaco-workbench .editor-group-watermark > .letterpress { opacity: 0.16; }\n'
    if watermark_style not in stylesheet:
        stylesheet += watermark_style
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
    workbench_icon = owned_target(app_root, "out/media/protomire-icon.png")
    workbench_icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "icons/light-tile/protomire-32.png", workbench_icon)
    shutil.copyfile(ASSETS / "icons/light-tile/protomire-512.png",
                    owned_target(app_root, "out/media/protomire-watermark.png"))
    stylesheet_path.write_text(stylesheet)
    for source, name in (("README.md", "README.md"), ("fonts/OFL.txt", "InterTight-OFL.txt"),
                         ("asset-manifest.json", "asset-manifest.json")):
        notice = owned_target(app_root, "protomire-brand/" + name)
        notice.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASSETS / source, notice)
