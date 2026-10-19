# Protomire brand assets

The approved mark is a volcanic cutaway: orange primordial ooze fills the full central fracture from the flat base to the summit and overflows at the top. The approved geometry has not been redrawn.

## Start here
- `lockups/protomire-horizontal-transparent.png`: primary horizontal logo, 4096 px wide
- `lockups/protomire-stacked-transparent.png`: stacked logo, 2048 px wide
- `source/protomire-approved-icon.png`: original approved 1254 × 1254 RGBA icon, byte-for-byte unchanged
- `icons/transparent/`: transparent PNGs at 16, 24, 32, 48, 64, 96, 128, 192, 256, 512 and 1024 px
- `icons/light-tile/`: the same icon sizes on a bone square background, useful where a dark UI would obscure the dark rock
- `icons/favicon.ico`: multi-resolution favicon
- `wordmark/`: dark/light PNG wordmarks and true outlined SVG wordmarks
- `qa/icon-size-proof.png`: actual-size proof on a light surface

The icon and complete logo lockups are raster assets. Only the separate wordmark SVGs are vector. No raster image has been disguised as a vector icon master.

## Typography
The Round 02 concept lettering was generated artwork rather than an identifiable font file. The production wordmark is faithfully recreated with **Inter Tight Black, weight 900, version 3.004**, with **normal tracking** and normal pair kerning, with the **r–o and r–e pairs opened by 0.015em** to prevent touching at small sizes. It is a real, reproducible type setting, not a claim that the generated lettering was this font. Use the supplied logo art for the exact lockup spacing.

The original unmodified Inter Tight variable font is included in `fonts/InterTight-Variable.ttf`. Select weight 900 for the logo. Font author: Rasmus Andersson / The Inter Project Authors. Copyright 2022 The Inter Project Authors (https://github.com/rsms/inter-tight). Licensed under the SIL Open Font License 1.1; the complete license is in `fonts/OFL.txt`.

Official font source: https://github.com/google/fonts/tree/main/ofl/intertight
Typeface page: https://fonts.google.com/specimen/Inter+Tight

## Use and small sizes
Use the dark logo on a light or bone background. On very dark surfaces, use the supplied light-tile icon so the approved dark rock remains visible. The light wordmark is supplied separately for reversed text treatments.

The 16–32 px exports use slightly tighter transparent margins; the approved internal geometry is unchanged. The central orange channel and summit cap remain visible at these sizes, while tiny drip contours naturally resolve better at 48 px and above. Use the dedicated size files rather than reducing a screenshot or a full logo.

For the horizontal lockup, use approximately 240 CSS px minimum width; for the stacked lockup, approximately 140 CSS px. Below that, use the icon. Keep clear space of at least one-quarter of the visible icon height around a complete lockup.

Palette reference: basalt #282A25, orange approximately #F79424, bone #F7F3EA. The approved raster retains its subtle original tonal variation.

No repository or application installation changes are included in this package.
