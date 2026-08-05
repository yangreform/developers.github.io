# Thomson Reserve Phase 1

## GitHub Pages
1. Upload all files to the repository root.
2. GitHub → Settings → Pages → Build and deployment → GitHub Actions.
3. Push to `main`.
4. Confirm the workflow succeeds in Actions.

## Cloudflare cache purge
Create GitHub repository secrets:
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ZONE_ID`

Cloudflare API token permission:
- Zone / Cache Purge / Purge
- Restrict to `developers.marketing`

## Tracking
Edit `thomson-reserve/index.html`:
- Replace `G-XXXXXXXXXX`
- Replace `AW-XXXXXXXXXX/CONVERSION_LABEL`

Recommended ad URL:
`https://developers.marketing/thomson-reserve/?utm_source=google&utm_medium=cpc&utm_campaign=thomson_reserve&utm_content=register_consultation#contact`
