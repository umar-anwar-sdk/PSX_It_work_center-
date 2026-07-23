import pdfplumber
import re
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def clean_text(text):
    if not text:
        return ""

    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)

    return text.strip()
    


# ---------------- DATE & TIME ----------------
def extract_report_datetime(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = pdf.pages[0].extract_text() or ""

        report_date = None
        report_time = None

        date_pattern = r'(\d{2}-[A-Za-z]{3}-\d{4}|\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})'
        time_pattern = r'(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)'

        date_match = re.search(date_pattern, text)
        time_match = re.search(time_pattern, text, re.I)

        if date_match:
            for fmt in ["%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"]:
                try:
                    report_date = datetime.strptime(date_match.group(1), fmt).date()
                    break
                except:
                    pass

        if time_match:
            for fmt in ["%H:%M:%S", "%H:%M", "%I:%M %p"]:
                try:
                    report_time = datetime.strptime(time_match.group(1), fmt).time()
                    break
                except:
                    pass

        return report_date, report_time

    except Exception as e:
        logger.error(e)
        return None, None


def _extract_tables_with_pdfplumber(pdf_path):
    try:
        all_tables = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                try:
                    page_tables = page.extract_tables()
                except Exception as e:
                    logger.warning("pdfplumber page table extraction failed: %s", e)
                    continue

                for table in page_tables:
                    cleaned_table = []
                    for row in table:
                        row = [clean_text(str(x)) if x else None for x in row]

                        if all(v in [None, "", " "] for v in row):
                            continue

                        if any("Symbol" in str(v) or "Name" in str(v) for v in row):
                            continue

                        cleaned_table.append(row)

                    if cleaned_table:
                        all_tables.append(cleaned_table)

        return all_tables
    except Exception as e:
        logger.error("pdfplumber fallback failed: %s", e)
        return []


def extract_table_data(pdf_path):
    try:
        import camelot
    except ImportError as e:
        logger.error("Camelot import failed: %s", e)
        logger.warning("Falling back to pdfplumber-based table extraction.")
        return _extract_tables_with_pdfplumber(pdf_path)

    try:
        if not Path(pdf_path).exists():
            return []

        try:
            tables = camelot.read_pdf(pdf_path, pages="all", flavor="lattice")
            if len(tables) == 0:
                tables = camelot.read_pdf(pdf_path, pages="all", flavor="stream")
        except Exception as e:
            logger.error("Camelot table extraction failed: %s", e)
            logger.warning("Falling back to pdfplumber-based table extraction.")
            return _extract_tables_with_pdfplumber(pdf_path)

        all_tables = []

        for table in tables:
            df = table.df

            cleaned_table = []

            for row in df.values.tolist():
                row = [clean_text(str(x)) if x else None for x in row]

                if all(v in [None, "", " "] for v in row):
                    continue

                if any("Symbol" in str(v) or "Name" in str(v) for v in row):
                    continue

                cleaned_table.append(row)

            if cleaned_table:
                all_tables.append(cleaned_table)

        return all_tables

    except Exception as e:
        logger.error(e)
        return []
# ---------------- MAIN SCRAPER ----------------
def scrape_pdf(pdf_file_path):
    try:
        report_date, report_time = extract_report_datetime(pdf_file_path)
        tables = extract_table_data(pdf_file_path)

        return {
            "success": True,
            "report_date": report_date,
            "report_time": report_time,
            "tables": tables,
            "error": None
        }

    except Exception as e:
        return {
            "success": False,
            "report_date": None,
            "report_time": None,
            "tables": [],
            "error": str(e)
        }
import hashlib

def get_file_hash(file):
    hasher = hashlib.sha256()
    for chunk in file.chunks():
        hasher.update(chunk)
    return hasher.hexdigest()


def _normalize_row_key(row, table_index, row_index):
    if not row:
        return f"table_{table_index}_row_{row_index}"

    symbol = row.get('symbol') if isinstance(row, dict) else None
    symbol = str(symbol).strip() if symbol else None

    if symbol:
        return f"{table_index}:{symbol}"

    return f"table_{table_index}_row_{row_index}"


def _get_diff_fields(old_row, new_row):
    diffs = {}
    if not isinstance(old_row, dict) or not isinstance(new_row, dict):
        return diffs

    for key in sorted(set(old_row.keys()) | set(new_row.keys())):
        old_value = old_row.get(key)
        new_value = new_row.get(key)
        if old_value != new_value:
            diffs[key] = {
                'old': old_value,
                'new': new_value
            }

    return diffs


def compare_pdf_records(base_rows, new_rows):
    base_map = {}
    new_map = {}

    for row in base_rows:
        key = _normalize_row_key(row.row_data, row.table_index, row.row_index)
        base_map[key] = row.row_data

    for row in new_rows:
        key = _normalize_row_key(row.row_data, row.table_index, row.row_index)
        new_map[key] = row.row_data

    added_changes = []
    removed_changes = []
    modified_changes = []

    for key, new_row_data in new_map.items():
        if key not in base_map:
            added_changes.append({
                'type': 'added',
                'key': key,
                'symbol': new_row_data.get('symbol'),
                'name': new_row_data.get('name'),
                'row_data': new_row_data,
            })
        else:
            diff = _get_diff_fields(base_map[key], new_row_data)
            if diff:
                modified_changes.append({
                    'type': 'modified',
                    'key': key,
                    'symbol': new_row_data.get('symbol') or base_map[key].get('symbol'),
                    'name': new_row_data.get('name') or base_map[key].get('name'),
                    'diff': diff,
                    'old_row': base_map[key],
                    'new_row': new_row_data,
                })

    for key, old_row_data in base_map.items():
        if key not in new_map:
            removed_changes.append({
                'type': 'removed',
                'key': key,
                'symbol': old_row_data.get('symbol'),
                'name': old_row_data.get('name'),
                'row_data': old_row_data,
            })

    changes = added_changes + removed_changes + modified_changes
    summary = {
        'total_records_changed': len(changes),
        'new_records': len(added_changes),
        'removed_records': len(removed_changes),
        'updated_records': len(modified_changes),
        'base_records': len(base_rows),
        'new_records_total': len(new_rows),
    }

    return summary, changes