# Flatpak delivery for immutable Linux / Bazzite

Flatpak is the required Linux end-user delivery, replacing AppImage as the default package target. The existing AppImage builder remains available only through `--linux-format appimage`.

## Current state

The packaging command path and its refusal/permission/integrity contracts have unit tests. **No installable Flatpak has been built or tested on Bazzite.** Workspace-local Flatpak tooling has been prepared; native Linux editor/runtime assembly and actual installation acceptance remain unfinished. Inert packager fixtures are not installation evidence.

The release-engineering entry point is `distribution/single_app.py package --target linux-x64 --app <sealed-staged-app> --output <fresh-directory> --flatpak-profile <reviewed-profile.json>`. This is a build-host command, not an end-user multi-step installation. The stage must already contain the mandatory operation service, native MCP, backend and reviewed runtime. The packager validates that sealed payload before invoking Flatpak; it does not download dependencies, install the app, change host configuration, or publish it.

The profile requires explicit `app_id`, application `branch`, `runtime`, `sdk`, `base`, `runtime_version`, and `commits` with exact installed OSTree SHAs for `runtime`, `sdk`, and `base`. Native x64 and arm64 are supported by the command path. The Electron base must provide `zypak-wrapper`; missing sandbox integration is an error, never a `--no-sandbox` fallback. Dependency commits are checked before and after staging. The output is a nonempty unsigned `.flatpak` plus a hash/profile/permissions receipt.

## Permissions and acceptance gates

Declared permissions are network, IPC, Wayland/fallback X11, audio, DRI graphics, home-directory project access, notifications and the Secret Service API. There is no blanket host-filesystem grant, host-spawn D-Bus permission, or all-device grant. These permissions must be shown at installation/review. GPU training beyond DRI access, driver/runtime compatibility, nested isolation and native tool dependencies require actual tests; none are implied by packaging unit tests.

Before telling a user the product is ready:

- Build and install the real Flatpak on a clean immutable Linux/Bazzite profile.
- Verify startup, multi-screen quickstart, one-model project reopen and upgrades.
- Verify bundled MCP/core operation without external Python/Git setup.
- Verify CPU and the intended GPU training/evaluation/export paths, checkpoint notifications and family guardrails.
- Verify access to selected local project files and persistent app state across restarts.
- Verify external Claude/Codex/OpenCode/Kilo startup from a host terminal with the IDE closed. Packaged project setup now creates a reviewed shell launcher: inside the matching Flatpak it runs the bundled MCP command; outside it calls `flatpak run --command=modelbuilder-mcp` with exact project/state arguments. Both routing branches and rollback have unit tests, but **real host-to-Flatpak cold-start acceptance remains open**. Triggerfish remains an inactive compatibility bundle.
- Verify assistant discovery, owner-reviewed mutations, project isolation and disconnect behavior.
- Complete redistribution/license, signing/update and native safety checks. Do not relabel the existing Mac DMG as containing later source changes.

Implementation follows the official [Flatpak command reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html) and [Electron packaging guidance](https://docs.flatpak.org/en/latest/electron.html). Current upstream [VS Code Flatpak packaging](https://github.com/flathub/com.visualstudio.code) is a reference, not a dependency on Microsoft's binary or a reason to inherit its broad permissions.
