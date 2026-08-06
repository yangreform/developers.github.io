# Thomson Reserve Phase 1 — Final Tracking Package

## Installed tracking

- GA4 Measurement ID: `G-ME9C947K1T`
- Google Ads ID: `AW-18370524394`
- Google Ads contact conversion: `AW-18370524394/Ug0RCJzO7NscEOrp37dE`

The conversion event fires only when a visitor clicks a WhatsApp or Email contact link.

## GitHub repository secrets

Create these two **Repository secrets** in:

`GitHub → Settings → Secrets and variables → Actions → New repository secret`

### 1. CLOUDFLARE_API_TOKEN

- **Name:** `CLOUDFLARE_API_TOKEN`
- **Secret:** the API token newly generated in Cloudflare
- Required permission: `Zone → Cache Purge → Purge`
- Restrict the token to `developers.marketing`

### 2. CLOUDFLARE_ZONE_ID

- **Name:** `CLOUDFLARE_ZONE_ID`
- **Secret:** the existing Zone ID shown on the Cloudflare Overview page for `developers.marketing`

Do not put GA4 or Google Ads IDs into these GitHub secrets.

## Important Cloudflare note

The **Cloudflare Pages Variables and Secrets** screen is not used by this GitHub Pages workflow.
Only the GitHub Repository secrets above are required for automatic cache purging.

## GitHub Pages

1. Upload the package contents to the repository root.
2. Go to `Settings → Pages`.
3. Choose `GitHub Actions` under Build and deployment.
4. Push to the `main` branch.
5. Check the Actions tab for a successful deployment.

## Recommended Google Ads final URL

`https://developers.marketing/thomson-reserve/?utm_source=google&utm_medium=cpc&utm_campaign=thomson_reserve&utm_content=register_consultation#contact`
