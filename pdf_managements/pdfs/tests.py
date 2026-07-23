import json
from pathlib import Path

from django.test import TestCase

from .models import ScrapedRecord
from .views import import_data_from_folder


class ScraperImportTests(TestCase):
    def test_imports_json_records_from_folder(self):
        folder = Path(__file__).resolve().parent / 'testdata' / 'scrapper'
        folder.mkdir(parents=True, exist_ok=True)
        sample_file = folder / 'sample.json'
        sample_file.write_text(
            json.dumps([
                {
                    'symbol': 'CNERGY',
                    'company': 'Cnergyico PK Limited',
                    'sector': 'REFINERY',
                    'price': '9.40',
                    'change_percent': '+7.80%',
                    'volume': '211683514',
                    'trend': 'Up',
                    'date': '2026-07-09',
                },
                {
                    'symbol': 'LSECL',
                    'company': 'LSE Capital Limited',
                    'sector': 'INV. BANKS / SECURITIES',
                    'price': '7.84',
                    'change_percent': '+14.62%',
                    'volume': '58382446',
                    'trend': 'Up',
                    'date': '2026-07-09',
                },
            ]),
            encoding='utf-8',
        )

        imported_count = import_data_from_folder(folder)

        self.assertEqual(imported_count, 2)
        self.assertEqual(ScrapedRecord.objects.count(), 2)
        self.assertEqual(ScrapedRecord.objects.get(symbol='CNERGY').company, 'Cnergyico PK Limited')
