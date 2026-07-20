"""
One-time maintenance script for TEN-002 remediation (see docs/saas-audit/).

Re-signs the CURRENT products.image_url and companies.logo_url values so they
keep working the moment the product-images / company-assets Supabase Storage
buckets are flipped from public to private.

Does NOT touch historical quote_data (product images baked into old quote line
items) -- that was an explicit, deliberate decision (see the conversation that
produced this script): those old public-style links will show as broken images
on already-saved quotes after the buckets go private. Only the live
products/companies rows are updated here.

Run this AFTER deploying the code changes in api/products.py, api/auth.py,
and api/proposal.py (which switch new uploads to signed URLs), and BEFORE
flipping the buckets to private in the Supabase dashboard. Running it before
the code deploy is harmless but pointless; running it after the buckets are
already private will fail to read the old public URLs' paths for any object
whose path can't be parsed from the currently-stored value (rare, but why the
order matters).

Usage:
    pip install supabase python-dotenv --break-system-packages   # if not already installed
    python scripts/resign_storage_urls.py               # dry run -- prints what WOULD change
    python scripts/resign_storage_urls.py --apply        # actually writes the new signed URLs

Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment (or a
.env.local file in the repo root, loaded automatically via python-dotenv) --
the same values your Vercel deployment uses. Run this against ONE environment
at a time (point it at production, then separately at staging) by swapping
which .env file / exported variables are active.
"""
import os
import re
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv, dotenv_values

# Resolve .env.local relative to THIS script's location (repo_root/scripts/..),
# not the current working directory -- makes this work the same way regardless
# of which folder you happen to run it from.
ENV_PATH = Path(__file__).resolve().parent.parent / '.env.local'

_raw = dotenv_values(ENV_PATH)  # parse without mutating os.environ yet, for diagnostics
load_dotenv(ENV_PATH, override=True)

from supabase import create_client

SIGNED_URL_EXPIRES_SECONDS = 10 * 365 * 24 * 60 * 60  # ~10 years, matches api/products.py & api/auth.py

TARGETS = [
    # (table, id_column, url_column, bucket)
    ('products', 'id', 'image_url', 'product-images'),
    ('companies', 'id', 'logo_url', 'company-assets'),
]


def extract_path(url, bucket):
    if not url:
        return None
    m = re.search(rf'/{re.escape(bucket)}/([^?]+)', url)
    return m.group(1) if m else None


def make_signed_url(sb, bucket, path):
    res = sb.storage.from_(bucket).create_signed_url(path, SIGNED_URL_EXPIRES_SECONDS)
    url = res.get('signedURL') or res.get('signedUrl') or res.get('signed_url') or res.get('url')
    if not url:
        raise RuntimeError(f'Unexpected create_signed_url() response shape: {res!r}')
    supabase_url = os.environ.get('SUPABASE_URL', '').rstrip('/')
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return f"{supabase_url}/storage/v1{url}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Actually write changes (default is dry-run)')
    args = parser.parse_args()

    supabase_url = os.environ.get('SUPABASE_URL')
    supabase_key = os.environ.get('SUPABASE_SERVICE_KEY')
    if not supabase_url or not supabase_key:
        print(f'ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not found.')
        print(f'  Looked for .env.local at: {ENV_PATH}')
        print(f'  That file exists on disk: {ENV_PATH.exists()}')
        print(f'  Keys parsed from it ({len(_raw)} total, values not shown): {sorted(_raw.keys())}')
        print(f'  SUPABASE_URL present in parsed file: {"SUPABASE_URL" in _raw}, non-empty value: {bool(_raw.get("SUPABASE_URL"))}')
        print(f'  SUPABASE_SERVICE_KEY present in parsed file: {"SUPABASE_SERVICE_KEY" in _raw}, non-empty value: {bool(_raw.get("SUPABASE_SERVICE_KEY"))}')
        sys.exit(1)

    print(f'Target project: {supabase_url}')
    print(f'Mode: {"APPLY (writing changes)" if args.apply else "DRY RUN (no changes will be written)"}')
    print()

    sb = create_client(supabase_url, supabase_key)

    total_updated, total_skipped, total_errors = 0, 0, 0

    for table, id_col, url_col, bucket in TARGETS:
        print(f'--- {table}.{url_col} (bucket: {bucket}) ---')
        rows = sb.table(table).select(f'{id_col},{url_col}').not_.is_(url_col, 'null').execute().data or []
        print(f'{len(rows)} row(s) with a non-null {url_col}')

        for row in rows:
            row_id = row[id_col]
            old_url = row.get(url_col)
            path = extract_path(old_url, bucket)
            if not path:
                print(f'  SKIP  {row_id}: could not parse a storage path from {old_url!r}')
                total_skipped += 1
                continue
            try:
                new_url = make_signed_url(sb, bucket, path)
            except Exception as e:
                print(f'  ERROR {row_id}: failed to sign path {path!r}: {e}')
                total_errors += 1
                continue

            if new_url == old_url:
                print(f'  SKIP  {row_id}: already up to date')
                total_skipped += 1
                continue

            print(f'  {"UPDATE" if args.apply else "WOULD UPDATE"}  {row_id}: {path}')
            if args.apply:
                sb.table(table).update({url_col: new_url}).eq(id_col, row_id).execute()
            total_updated += 1
        print()

    print(f'Done. {total_updated} {"updated" if args.apply else "would be updated"}, '
          f'{total_skipped} skipped, {total_errors} errors.')
    if not args.apply and total_updated:
        print('Re-run with --apply to actually write these changes.')


if __name__ == '__main__':
    main()
