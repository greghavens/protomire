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



def current_module_bytes() -> bytes:
    """The current reviewed product module must also be a real local file."""
    cursor = MODULE_SOURCE
    while True:
        if cursor.is_symlink():
            raise ValueError("Symlink is not permitted in reviewed product module source")
        if cursor == ROOT or cursor.parent == cursor:
            break
        cursor = cursor.parent
    return MODULE_SOURCE.read_bytes()


def matches_reviewed_module_history(content: bytes) -> bool:
    """Trust only regular-file blobs for this path in product HEAD ancestry.

    This is preparation-only authority, never verification authority. Unmerged
    branches, dangling objects, cache history, replacement refs and index-only
    changes cannot authorize an old module. A non-Git/source archive fails closed.
    """
    try:
        relative = MODULE_SOURCE.relative_to(ROOT).as_posix()
        if not (ROOT / ".git").exists():
            return False
        git = ["git", "--no-replace-objects", "-C", str(ROOT)]
        top = subprocess.check_output([*git, "rev-parse", "--show-toplevel"], text=True).strip()
        if pathlib.Path(top).resolve() != ROOT.resolve():
            return False
        head = subprocess.check_output([*git, "rev-parse", "--verify", "HEAD^{commit}"], text=True).strip()
        candidate = subprocess.check_output([*git, "hash-object", "--stdin"], input=content).decode().strip()
        commits = subprocess.check_output([*git, "log", "--format=%H", head, "--", relative], text=True).splitlines()
        for commit in commits:
            entry = subprocess.check_output([*git, "ls-tree", "-z", commit, "--", relative], text=True)
            if not entry:
                continue
            metadata, path = entry.rstrip("\0").split("\t", 1)
            mode, kind, blob = metadata.split()
            if path == relative and mode in {"100644", "100755"} and kind == "blob" and blob == candidate:
                return True
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False
    return False

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
    if target.exists():
        installed = target.read_bytes()
        if installed != current_module_bytes() and not (
            allow_unpatched and matches_reviewed_module_history(installed)
        ):
            raise ValueError("Installed menu contribution differs from reviewed product module")
    if not allow_unpatched and not target.is_file():
        raise ValueError("Permanent menu contribution is missing")
    return changed


def install_menu_composition(source: pathlib.Path) -> None:
    reviewed = current_module_bytes()
    original = subprocess.check_output(["git", "-C", str(source), "show", f"HEAD:{COMPOSITION}"], text=True)
    target = contained_file(source, MODULE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(reviewed)
    contained_file(source, COMPOSITION).write_text(expected_composition(original))
