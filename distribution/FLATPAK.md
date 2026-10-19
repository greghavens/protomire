# Flatpak delivery for immutable Linux / Bazzite

Flatpak is the required Linux end-user delivery, replacing AppImage as the default package target. The existing AppImage builder remains available only through `--linux-format appimage`.

## Current state

An unsigned Linux x64 development Flatpak at source
`1e122de9c710acdf7d0a7ea7465870cf9e892366` was built, installed and tested on a
Bazzite host on 2026-10-19 under the former Modelbuilder name. Native launch,
wizard creation/cancellation, persistent projects, bundled CPU dependencies,
HTTPS Git, reviewed assistant setup/disconnect, and all four generated
Claude/Codex/OpenCode/Kilo MCP commands cold-starting with the IDE closed passed.
The MCP checks used the official SDK and verified tool discovery, read access,
project isolation and queued mutations; they did not run authenticated vendor
agents. GPU training, clean-machine installation, signing/update and complete
redistribution clearance remain open. This scoped development checkpoint is not
a public production release. Later Protomire branding needs a separately tested
artifact; historical artifact hashes keep their original meaning.

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
- Verify external Claude/Codex/OpenCode/Kilo startup from a host terminal with the IDE closed. Packaged project setup creates a reviewed shell launcher: inside the matching Flatpak it runs the bundled MCP command; outside it calls `flatpak run --command=modelbuilder-mcp` with exact project/state arguments. Host-to-Flatpak command transport passed on the build host; authenticated vendor-agent acceptance remains open. Triggerfish remains an inactive compatibility bundle.
- Verify assistant discovery, owner-reviewed mutations, project isolation and disconnect behavior.
- Complete redistribution/license, signing/update and native safety checks. Do not relabel the existing Mac DMG as containing later source changes.

Implementation follows the official [Flatpak command reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html) and [Electron packaging guidance](https://docs.flatpak.org/en/latest/electron.html). Current upstream [VS Code Flatpak packaging](https://github.com/flathub/com.visualstudio.code) is a reference, not a dependency on Microsoft's binary or a reason to inherit its broad permissions.
