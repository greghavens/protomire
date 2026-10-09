"""Exact boundary for the documented native File-menu composition exception."""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSITION = "src/vs/workbench/workbench.common.main.ts"
MODULE = "src/vs/workbench/contrib/modelbuilder/browser/modelbuilderMenus.contribution.ts"
MODULE_SOURCE = ROOT / "packages/ide_feature/integration/modelbuilderMenus.contribution.ts"
IMPORT_LINE = "import './contrib/modelbuilder/browser/modelbuilderMenus.contribution.js';\n"


def contained_file(source: pathlib.Path, relative: str) -> pathlib.Path:
    """Reject cache symlinks before reads or writes, including parent components."""
    root = source.resolve()
    target = root / relative
    cursor = root
    for part in pathlib.Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Symlink is not permitted in source overlay target: {relative}")
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"Source overlay target escapes checkout: {relative}")
    return target


def expected_composition(original: str) -> str:
    assert original.endswith("\n"), "Unexpected upstream composition file ending"
    assert IMPORT_LINE not in original, "Upstream unexpectedly already contains this module"
    return original + IMPORT_LINE


def inspect_boundary(source: pathlib.Path, *, allow_unpatched: bool = False) -> list[str]:
    changed = subprocess.check_output(
        ["git", "-C", str(source), "diff", "HEAD", "--name-only"], text=True
    ).splitlines()
    unexpected = set(changed) - {"product.json", COMPOSITION}
    if unexpected:
        raise ValueError(f"Unexpected upstream modifications: {sorted(unexpected)}")
    original = subprocess.check_output(["git", "-C", str(source), "show", f"HEAD:{COMPOSITION}"], text=True)
    current = contained_file(source, COMPOSITION).read_bytes().decode("utf-8")
    permitted = {expected_composition(original)}
    if allow_unpatched:
        permitted.add(original)
    if current not in permitted:
        raise ValueError("Composition file differs from the exact approved one-import overlay")
    additions = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--others", "--", "src/"],
        text=True,
    ).splitlines()
    if set(additions) - {MODULE}:
        raise ValueError(f"Unexpected untracked upstream source files: {additions}")
    target = contained_file(source, MODULE)
    if target.exists() and target.read_bytes() != MODULE_SOURCE.read_bytes():
        raise ValueError("Installed menu contribution differs from reviewed product module")
    if not allow_unpatched and not target.is_file():
        raise ValueError("Permanent menu contribution is missing")
    return changed


def install_menu_composition(source: pathlib.Path) -> None:
    original = subprocess.check_output(["git", "-C", str(source), "show", f"HEAD:{COMPOSITION}"], text=True)
    target = contained_file(source, MODULE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(MODULE_SOURCE.read_bytes())
    contained_file(source, COMPOSITION).write_text(expected_composition(original))
