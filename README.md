# Jollein XML feed

Daily supplier synchronization at **07:00 Europe/Athens**, including daylight saving time.
GitHub Actions runs the crawler; GitHub Pages publishes the validated feed at `/products.xml`.

- Every simple product and Shopify variant is exported as an independent simple product.
- No variable parents, Parent references, or empty numbered attributes.
- Prices are always recalculated from freshly fetched Jollein data: **supplier price × 1.20**, rounded to two decimals.
- Available supplier stock: **Διαθέσιμο έως 30 εργάσιμες**.
- Out of stock: **Εξαντλημένο**.
- Original SKUs/EANs, technical specifications, HTML descriptions and up to three full-size image URLs are retained.
- Duplicate import identifiers follow the approved suffix/metadata policy.
- A failed or incomplete crawl does not deploy; the previous published feed remains available.
- HTML caches are new on every live run; previous marked-up XML is never used to calculate new prices.

## First setup

In the repository, open **Settings → Pages → Build and deployment → Source → GitHub Actions**.
Then run **Actions → Daily Jollein XML → Run workflow** with `refresh_source` enabled.
Push events publish the checked-in snapshot; scheduled and manual refreshes fetch all live supplier pages first.

The daily check starts at 07:00. Collection and publication finish later; scheduled GitHub jobs may also be delayed.
The download page shows the actual supplier-check timestamp.

## Local execution

```sh
python -m pip install playwright
python -m playwright install chromium
python daily_refresh.py --output public
```

`public/status.json` includes freshness, product counts and a checksum of the XML.
Optional metadata and custom XML fields need mapping in the customer's chosen XML importer.

