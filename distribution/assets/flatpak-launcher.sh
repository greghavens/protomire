#!/bin/sh
set -eu

# Code OSS derives user-data from nameShort. Keep the pre-rename profile while
# showing Protomire in the UI; do not move state, credentials or project trust.
# Preserve its portable/appdata/explicit/default precedence.
if [ -z "${VSCODE_PORTABLE:-}" ]; then
    if [ -n "${VSCODE_APPDATA:-}" ]; then
        # VSCODE_APPDATA wins over CLI paths, even when -- ends option parsing.
        protomire_remaining=$#
        protomire_skip=false
        protomire_options=true
        while [ "$protomire_remaining" -gt 0 ]; do
            protomire_arg=$1
            shift
            protomire_remaining=$((protomire_remaining - 1))
            if [ "$protomire_skip" = true ]; then
                protomire_skip=false
                continue
            fi
            if [ "$protomire_options" = true ]; then
                case "$protomire_arg" in
                    --user-data-dir) protomire_skip=true; continue ;;
                    --user-data-dir=*) continue ;;
                    --) protomire_options=false ;;
                esac
            fi
            set -- "$@" "$protomire_arg"
        done
        set -- "--user-data-dir=$VSCODE_APPDATA/Modelbuilder" "$@"
        unset VSCODE_APPDATA
    else
        protomire_explicit_profile=false
        for protomire_arg do
            case "$protomire_arg" in
                --) break ;;
                --user-data-dir|--user-data-dir=*) protomire_explicit_profile=true ;;
            esac
        done
        if [ "$protomire_explicit_profile" = false ]; then
            set -- "--user-data-dir=${XDG_CONFIG_HOME:-$HOME/.config}/Modelbuilder" "$@"
        fi
    fi
fi

exec /app/bin/zypak-wrapper /app/lib/modelbuilder/modelbuilder-ide --ozone-platform-hint=auto --password-store=gnome-libsecret "$@"
