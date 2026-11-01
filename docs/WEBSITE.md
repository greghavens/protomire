# Protomire website

The standalone product website is `docs/index.html`, with its assets in `docs/site-assets/`.
It introduces the model-development workflow, features, native MCP support, getting-started documentation, and current release status. It has no external runtime dependencies, analytics, account system, package installation, or build step.

## GitHub Pages

In this repository’s **Settings → Pages**, select **Deploy from a branch**, choose **main**, and select **/docs**. Save. The default project URL is `https://greghavens.github.io/protomire/` once GitHub confirms deployment.

No custom GitHub Actions workflow or paid service is required. `.nojekyll` keeps this a static HTML deployment. Existing Markdown engineering documentation is unchanged.

The GitHub integration used to commit these files does not expose Pages administration. Source preparation does not prove Pages is enabled. Confirm a successful Pages deployment before linking visitors to that URL.

## Maintenance

Edit the HTML and assets directly. Keep platform/release claims aligned with `distribution/INSTALLATION.md` and `docs/ACCEPTANCE_MATRIX.md`. The logo is the approved raster lockup from `distribution/assets/protomire/lockups/protomire-horizontal-on-bone.png`.

The Internet Is Hard project showcase currently also has a review copy at `/projects/protomire/index.html`. Once this standalone site is live, point its Protomire project links here. The other project one-pagers remain within that showcase.
