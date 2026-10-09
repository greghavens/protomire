"""Small offline distribution-policy tests, explicitly not GUI acceptance."""

import json
import pathlib
import importlib.util

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "menu_overlay_policy", ROOT / "distribution/overlay_policy.py"
    )
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    original = "// Pinned upstream fixture\nimport './existing.js';\n"
    composition = tmp_path / policy.COMPOSITION
    composition.parent.mkdir(parents=True)
    composition.write_text(original)
    reviewed = tmp_path / "reviewed-menu.ts"
    reviewed.write_text("// Only reviewed menu registrations\n")
    monkeypatch.setattr(policy, "MODULE_SOURCE", reviewed)
    state = {"tracked": [], "added": []}

    def git(args, text):
        if "show" in args:
            return original
        if "diff" in args:
            return "\n".join(state["tracked"])
        if "ls-files" in args:
            return "\n".join(state["added"])
        raise AssertionError(args)

    monkeypatch.setattr(policy.subprocess, "check_output", git)
    return policy, tmp_path, state, original


def test_source_and_toolchain_are_immutable():
    pin = json.loads((ROOT / "distribution/code_oss_pin.json").read_text())
    assert len(pin["commit"]) == 40
    assert len(pin["node_artifacts"]) == 7
    assert all(len(artifact["sha256"]) == 64 for artifact in pin["node_artifacts"].values())
    assert pin["core_patches"] == 1
    assert "composition import" in pin["core_patch_exception"]
    assert pin["node_version"] == "22.19.0"


def test_product_has_no_marketplace_or_bundled_agent():
    p = json.loads((ROOT / "distribution/overlay/product.json").read_text())
    assert p["nameShort"] == "Modelbuilder"
    assert p["builtInExtensions"] == []
    assert not p.get("extensionsGallery")
    assert not p.get("defaultChatAgent")
    assert p["enableTelemetry"] is False


def test_extension_lock_has_individual_pins_and_licenses():
    lock = json.loads((ROOT / "distribution/extension_manifest/extensions.lock.json").read_text())
    assert {x["id"] for x in lock["extensions"]} == {
        "ms-python.python",
        "ms-python.debugpy",
        "ms-toolsai.jupyter",
        "detachhead.basedpyright",
    }
    assert lock["schema_version"] == 2
    for extension in lock["extensions"]:
        assert extension["license"] == "MIT"
        assert extension["artifacts"]
        for platform, artifact in extension["artifacts"].items():
            assert len(artifact["sha256"]) == 64
            assert len(artifact["license_sha256"]) == 64
            assert extension["version"] in artifact["url"]
            assert artifact["url"].startswith("https://open-vsx.org/")
            assert artifact["target_platform"] == platform


def test_smoke_does_not_disable_sandbox_or_silently_skip():
    script = (ROOT / "distribution/smoke_ide.py").read_text().replace(chr(34), chr(39))
    assert "--no-sandbox" not in "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "'status': 'BLOCKED'" in script
    assert "extensionDevelopmentPath" in script
    assert "result.get('status') == 'PASS'" in script


def test_file_menu_overlay_is_exact_and_idempotent(boundary):
    policy, source, state, original = boundary
    policy.inspect_boundary(source, allow_unpatched=True)
    policy.install_menu_composition(source)
    state.update(tracked=["product.json", policy.COMPOSITION], added=[policy.MODULE])
    assert (source / policy.COMPOSITION).read_text() == original + policy.IMPORT_LINE
    before = (source / policy.MODULE).read_bytes()
    policy.inspect_boundary(source)
    policy.install_menu_composition(source)
    policy.inspect_boundary(source)
    assert (source / policy.MODULE).read_bytes() == before


def test_file_menu_overlay_rejects_extra_composition_logic(boundary):
    policy, source, _, original = boundary
    (source / policy.COMPOSITION).write_text(original + policy.IMPORT_LINE + "doSomethingElse();\n")
    with pytest.raises(ValueError, match="exact approved"):
        policy.inspect_boundary(source, allow_unpatched=True)


def test_file_menu_overlay_preserves_composition_bytes(boundary):
    policy, source, _, original = boundary
    (source / policy.COMPOSITION).write_bytes((original + policy.IMPORT_LINE).replace("\n", "\r\n").encode())
    with pytest.raises(ValueError, match="exact approved"):
        policy.inspect_boundary(source, allow_unpatched=True)


def test_file_menu_overlay_rejects_unknown_module_bytes(boundary):
    policy, source, _, _ = boundary
    target = source / policy.MODULE
    target.parent.mkdir(parents=True)
    target.write_text("unknown preexisting module\n")
    with pytest.raises(ValueError, match="differs from reviewed"):
        policy.inspect_boundary(source, allow_unpatched=True)


@pytest.mark.parametrize("key", ["tracked", "added"])
def test_file_menu_overlay_rejects_other_core_edits(boundary, key):
    policy, source, state, _ = boundary
    state[key] = ["src/vs/other-unapproved.ts"]
    with pytest.raises(ValueError, match="Unexpected"):
        policy.inspect_boundary(source, allow_unpatched=True)


def test_file_menu_overlay_rejects_symlinks_without_writing(boundary, tmp_path):
    policy, source, _, _ = boundary
    outside = tmp_path.parent / (tmp_path.name + "-outside.ts")
    outside.write_bytes(policy.MODULE_SOURCE.read_bytes())
    before = outside.read_bytes()
    target = source / policy.MODULE
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        policy.inspect_boundary(source, allow_unpatched=True)
    with pytest.raises(ValueError, match="Symlink"):
        policy.install_menu_composition(source)
    assert outside.read_bytes() == before


def test_file_menu_overlay_rejects_symlink_parent(boundary, tmp_path):
    policy, source, _, _ = boundary
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    target = source / policy.MODULE
    target.parent.parent.mkdir(parents=True)
    target.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        policy.install_menu_composition(source)
    assert list(outside.iterdir()) == []


def git_repository(root, files):
    import subprocess
    root.mkdir(parents=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "add", ".")
    git(root, "-c", "user.name=OverlayTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "Reviewed fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


def git(root, *args):
    import subprocess
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def upgrade_boundary(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("upgrade_menu_overlay_policy", ROOT / "distribution/overlay_policy.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    product, source = tmp_path / "product", tmp_path / "upstream"
    relative = "packages/ide_feature/integration/modelbuilderMenus.contribution.ts"
    old, current = b"// Reviewed old product menus\n", b"// Reviewed current product menus\n"
    head = git_repository(product, {relative: old})
    git_repository(source, {policy.COMPOSITION: b"// Pinned upstream\n", "product.json": b"{}\n", "src/vs/unchanged.ts": b"// Pinned sibling\n"})
    monkeypatch.setattr(policy, "ROOT", product)
    monkeypatch.setattr(policy, "MODULE_SOURCE", product / relative)
    policy.install_menu_composition(source)
    (product / relative).write_bytes(current)  # The reviewed pending product update.
    return policy, source, product, head, old, current


def test_file_menu_overlay_upgrades_only_reviewed_ancestor_and_remains_idempotent(upgrade_boundary):
    policy, source, _, _, old, current = upgrade_boundary
    installed = source / policy.MODULE
    assert installed.read_bytes() == old
    policy.inspect_boundary(source, allow_unpatched=True)
    # An old reviewed copy is never a valid completed build.
    with pytest.raises(ValueError, match="differs from reviewed"):
        policy.inspect_boundary(source)
    policy.install_menu_composition(source)
    assert installed.read_bytes() == current
    policy.inspect_boundary(source)
    before = (installed.read_bytes(), (source / policy.COMPOSITION).read_bytes())
    policy.inspect_boundary(source, allow_unpatched=True)
    policy.install_menu_composition(source)
    policy.inspect_boundary(source)
    assert (installed.read_bytes(), (source / policy.COMPOSITION).read_bytes()) == before


def test_file_menu_overlay_upgrade_rejects_modified_old_bytes_without_writing(upgrade_boundary):
    policy, source, _, _, old, _ = upgrade_boundary
    target = source / policy.MODULE
    changed = old + b"unapproved();\n"
    target.write_bytes(changed)
    with pytest.raises(ValueError, match="differs from reviewed"):
        policy.inspect_boundary(source, allow_unpatched=True)
    assert target.read_bytes() == changed


@pytest.mark.parametrize("kind", ["unmerged", "index-only", "historical-symlink"])
def test_file_menu_overlay_upgrade_does_not_trust_other_git_objects(upgrade_boundary, kind):
    policy, source, product, head, _, current = upgrade_boundary
    relative = policy.MODULE_SOURCE.relative_to(product).as_posix()
    unreviewed = b"// Not in reviewed regular-file ancestry\n"
    if kind == "historical-symlink":
        policy.MODULE_SOURCE.unlink()
        policy.MODULE_SOURCE.symlink_to("unreviewed-target.ts")
        unreviewed = b"unreviewed-target.ts"
    else:
        policy.MODULE_SOURCE.write_bytes(unreviewed)
    git(product, "add", relative)
    if kind != "index-only":
        git(product, "-c", "user.name=OverlayTest", "-c", "user.email=test@example.invalid", "commit", "-qm", "Unapproved object")
        if kind == "unmerged":
            git(product, "checkout", "-q", "--detach", head)
    if policy.MODULE_SOURCE.is_symlink():
        policy.MODULE_SOURCE.unlink()
    policy.MODULE_SOURCE.write_bytes(current)
    (source / policy.MODULE).write_bytes(unreviewed)
    with pytest.raises(ValueError, match="differs from reviewed"):
        policy.inspect_boundary(source, allow_unpatched=True)


@pytest.mark.parametrize("kind", ["tracked", "untracked", "symlink"])
def test_historical_menu_does_not_expand_upstream_change_boundary(upgrade_boundary, kind, tmp_path):
    policy, source, _, _, old, _ = upgrade_boundary
    if kind == "tracked":
        (source / "src/vs/unchanged.ts").write_text("// Unexpected edit\n")
    elif kind == "untracked":
        (source / "src/vs/unexpected.ts").write_text("// Unexpected source\n")
    else:
        target = source / policy.MODULE
        target.unlink()
        outside = tmp_path / "outside-reviewed.ts"
        outside.write_bytes(old)
        target.symlink_to(outside)
    with pytest.raises(ValueError, match="Unexpected|Symlink"):
        policy.inspect_boundary(source, allow_unpatched=True)


def test_overlay_apply_performs_reviewed_upgrade_then_exact_verification(upgrade_boundary, monkeypatch):
    import sys
    policy, source, product, _, _, current = upgrade_boundary
    monkeypatch.setitem(sys.modules, "overlay_policy", policy)
    spec = importlib.util.spec_from_file_location("upgrade_apply_overlay", ROOT / "distribution/apply_overlay.py")
    apply = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(apply)
    monkeypatch.setattr(apply, "ROOT", product)
    (product / "distribution/overlay").mkdir(parents=True)
    (product / "distribution/code_oss_pin.json").write_text(json.dumps({"commit": git(source, "rev-parse", "HEAD").decode().strip()}))
    (product / "distribution/overlay/product.json").write_text(json.dumps({"nameShort": "Reviewed Product"}))
    first = apply.apply(source)
    assert (source / policy.MODULE).read_bytes() == current
    assert set(first["tracked_overlay_files"]) == {"product.json", policy.COMPOSITION}
    assert apply.apply(source) == first
    policy.inspect_boundary(source)


def test_overlay_upgrade_rejects_symlinked_current_product_source(upgrade_boundary, tmp_path):
    policy, source, _, _, _, current = upgrade_boundary
    outside = tmp_path / "outside-current.ts"
    outside.write_bytes(current)
    policy.MODULE_SOURCE.unlink()
    policy.MODULE_SOURCE.symlink_to(outside)
    before = (source / policy.MODULE).read_bytes()
    with pytest.raises(ValueError, match="Symlink"):
        policy.inspect_boundary(source, allow_unpatched=True)
    with pytest.raises(ValueError, match="Symlink"):
        policy.install_menu_composition(source)
    assert (source / policy.MODULE).read_bytes() == before
