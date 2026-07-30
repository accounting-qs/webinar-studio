"""Build a normalized firmographic staging CSV from the 8 lead-list source CSVs.

Reads every CSV under local_assets/, detects its schema (Ampleleads / Sales Nav /
ZoomInfo), maps each to one unified record keyed by lowercased email, and dedupes
by email keeping the first non-empty value seen per field (so a blank in one file
is filled from another). Emits a single staging CSV for the prod backfill.

Read-only against prod — this only touches local files.

Usage:
    python scripts/build_firmo_staging.py <source_dir> <out_csv>
"""
import csv
import sys

# Unified output column order (matches the staging table in the backfill script).
OUT_COLUMNS = [
    "email", "title", "seniority", "industry", "country",
    "employee_count", "employee_range",
    "company_annual_revenue", "company_total_funding", "company_founded_year",
    "company_country", "company_website",
    "phone", "linkedin", "company_name",
]

# Canonical company-size buckets — copied verbatim from
# api/routers/outreach/uploads.py so range-source and count-source imports share
# one filter vocabulary. Upper bound is inclusive.
_EMPLOYEE_BUCKETS = (
    (2, "0 - 2"),
    (5, "3 - 5"),
    (10, "6 - 10"),
    (20, "11 - 20"),
    (50, "21 - 50"),
    (100, "51 - 100"),
    (200, "101 - 200"),
    (500, "201 - 500"),
    (1000, "501 - 1000"),
    (2000, "1001 - 2000"),
    (5000, "2001 - 5000"),
    (10000, "5001 - 10000"),
)


def _employee_bucket(count: int) -> str:
    for ceiling, label in _EMPLOYEE_BUCKETS:
        if count <= ceiling:
            return label
    return "10000+"


# Per-schema mapping: unified field -> source CSV header.
MAP_AMPLELEADS = {
    "email": "Email",
    "title": "Title",
    "seniority": "Seniority",
    "industry": "Industry",
    "country": "Country",
    "employee_count": "Employees Count",
    "company_annual_revenue": "Company Annual Revenue Clean",
    "company_total_funding": "Company Total Funding Clean",
    "company_founded_year": "Company Founded Year",
    "company_country": "Company Country",
    "company_website": "Company Website",
    "phone": "Mobile Number",
    "linkedin": "LinkedIn",
    "company_name": "Company Name",
}

MAP_SALESNAV = {
    "email": "Email",
    "title": "Title",
    "industry": "Industry",
    "country": "Country",
    "employee_count": "# Employees",
    "company_annual_revenue": "Annual Revenue",
    "company_website": "Website",
    "phone": "Corporate Phone",
    "linkedin": "Person Linkedin Url",
    "company_name": "Company",
}

MAP_ZOOMINFO = {
    "email": "email",
    "title": "lead_titles",
    "seniority": "seniority_level",
    "industry": "company_industry",
    "country": "lead_country",
    "company_country": "company_country",
    "company_founded_year": "company_founded_at",
    "company_website": "company_website",
    "company_annual_revenue": "revenue_range",
    # NB: ZoomInfo's company_size_key is an export-date artifact ("4/20/2025"),
    # not a headcount range — do NOT map it. ZoomInfo carries no usable size.
    "phone": "phone",
    "linkedin": "linkedin_url",
    "company_name": "company_name",
}


def detect_schema(headers: list[str]):
    hset = set(headers)
    if "lead_titles" in hset:
        return "zoominfo", MAP_ZOOMINFO
    if "Employees Count" in hset or "Company Total Funding" in hset:
        return "ampleleads", MAP_AMPLELEADS
    if "# Employees" in hset:
        return "salesnav", MAP_SALESNAV
    return None, None


def parse_int(value: str):
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else None


try:
    import ftfy as _ftfy
except ImportError:
    _ftfy = None


def _manual_unwind(s: str) -> str:
    """cp1252/latin-1 round-trip unwind, fired only on the 'Ã'/'Â' lead-byte
    artifacts and accepted only when that marker count strictly drops. Nordic
    letters like 'Å' are NOT markers — they're valid repair output."""
    def _markers(x: str) -> int:
        return x.count("Ã") + x.count("Â")

    for _ in range(3):
        if "Ã" not in s and "Â" not in s:
            break
        best = s
        for enc in ("cp1252", "latin-1"):
            try:
                cand = s.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if _markers(cand) < _markers(best):
                best = cand
        if best == s:
            break
        s = best
    return s


def fix_mojibake(s: str) -> str:
    """Repair double-encoded UTF-8 (mojibake), e.g. 'dÃ©lÃ©guÃ©' -> 'délégué'.

    Only strings carrying the 'Ã'/'Â' lead-byte artifacts are touched. ftfy runs
    first (segment-aware — handles emoji/mixed-script rows), then a manual
    cp1252/latin-1 unwind cleans up anything ftfy leaves (some Nordic strings)."""
    if "Ã" not in s and "Â" not in s:
        return s
    if _ftfy is not None:
        s = _ftfy.fix_text(s)
    return _manual_unwind(s)


def clean_value(field: str, val: str) -> str:
    val = fix_mojibake(val)
    if field == "phone":
        # Excel text-guard apostrophe (e.g. "'+31 71 407 6380") — not part of the number.
        val = val.lstrip("'").strip()
    return val


def main():
    src_dir = sys.argv[1]
    out_path = sys.argv[2]

    import glob
    import os

    files = sorted(glob.glob(os.path.join(src_dir, "*.csv")))
    if not files:
        print(f"No CSVs found in {src_dir}")
        sys.exit(1)

    # email -> {field: value}; only non-empty values are ever stored.
    records: dict[str, dict] = {}
    stats = {"rows": 0, "no_email": 0, "unknown_schema": 0}

    for path in files:
        name = os.path.basename(path)
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            try:
                headers = [h.strip() for h in next(reader)]
            except StopIteration:
                print(f"  SKIP empty: {name}")
                continue
            schema, mapping = detect_schema(headers)
            if not schema:
                stats["unknown_schema"] += 1
                print(f"  SKIP unknown schema: {name}")
                continue

            # Resolve header -> column index once per file.
            idx = {}
            for field, header in mapping.items():
                try:
                    idx[field] = headers.index(header)
                except ValueError:
                    pass  # header absent in this file; field stays unmapped

            email_i = idx.get("email")
            if email_i is None:
                print(f"  SKIP no email column: {name}")
                continue

            file_rows = 0
            for row in reader:
                stats["rows"] += 1
                file_rows += 1
                if file_rows % 200000 == 0:
                    print(f"    {name}: {file_rows:,} rows, {len(records):,} unique emails")
                if email_i >= len(row):
                    stats["no_email"] += 1
                    continue
                email = row[email_i].strip().lower()
                if not email or "@" not in email:
                    stats["no_email"] += 1
                    continue

                rec = records.get(email)
                if rec is None:
                    rec = {}
                    records[email] = rec

                for field, col in idx.items():
                    if field == "email":
                        continue
                    if col >= len(row):
                        continue
                    val = clean_value(field, row[col].strip())
                    if not val:
                        continue
                    if field == "employee_count":
                        n = parse_int(val)
                        if n is None:
                            continue
                        if not rec.get("employee_count"):
                            rec["employee_count"] = str(n)
                        # Derive range from count when we don't already have a range.
                        if not rec.get("employee_range"):
                            rec["employee_range"] = _employee_bucket(n)
                        continue
                    if not rec.get(field):
                        rec[field] = val

            print(f"  {name}: schema={schema}, rows={file_rows:,}, unique so far={len(records):,}")

    print(f"\nWriting {len(records):,} unique-email rows to {out_path}")
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        w = csv.writer(out)
        w.writerow(OUT_COLUMNS)
        for email, rec in records.items():
            w.writerow([email] + [rec.get(c, "") for c in OUT_COLUMNS[1:]])

    print("\nDone.")
    print(f"  total data rows read : {stats['rows']:,}")
    print(f"  rows without email   : {stats['no_email']:,}")
    print(f"  unique emails written: {len(records):,}")


if __name__ == "__main__":
    main()
