import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parse import (
    BRAHMSExportReader,
    brahms_row_to_payload,
    construct_img_filepath,
    extract_species_query,
    parse_last_modified,
)
from post import RBGAPIPoster


HEADER = [
    'familyname', 'vernacularfamilyname', 'genusname', 'speciesname', 'calcfullname',
    'subspecies', 'variety', 'subvariety', 'forma', 'subforma', 'cultivar',
    'vernacularname', 'habit', 'hardinesscode', 'waterregime', 'exposure',
    'plantsize', 'colour', 'labelrangetext', 'gardenlocalityarea',
    'gardenlocalityname', 'gardenlocalitycode', 'plantid', 'latitude', 'longitude',
    'commemorationcategory', 'commemorationperson', 'commemorationtext', 'plantday',
    'plantmonth', 'plantyear', 'notonline', 'lastmodifiedon', 'str05', 'str12',
    'str18', 'str19', 'str20', 'str22', 'str23', 'str42',
]

IMAGE_HEADER = [
    'imagefile', 'copyright', 'directoryname', 'plantid', 'genusname',
    'speciesname', 'subspecies', 'variety', 'subvariety', 'forma',
    'subforma', 'cultivar', 'lastmodifiedon',
]


def values():
    row = {column: '' for column in HEADER}
    row.update({
        'familyname': 'Family', 'vernacularfamilyname': 'Common family', 'genusname': 'Genus',
        'speciesname': 'species', 'calcfullname': 'Genus species', 'habit': 'Perennial',
        'hardinesscode': '3, 4', 'waterregime': 'Average', 'exposure': 'Full Sun',
        'gardenlocalityarea': 'Area', 'gardenlocalityname': 'Garden', 'gardenlocalitycode': 'G-01',
        'plantid': '2026-0001*1', 'latitude': '40.1234567', 'longitude': '-111.1234567',
        'plantday': '2', 'plantmonth': '10', 'plantyear': '2026',
        'lastmodifiedon': '2026-10-20 15:25:45.570000000', 'str05': 'Early May June',
        'str12': 'Utah Native', 'str18': 'x', 'str19': 'yes', 'str20': 'x',
        'str22': 'x', 'str23': 'x', 'str42': 'x',
    })
    return [row[column] for column in HEADER]


class CollectionReaderTests(unittest.TestCase):
    def read_records(self, lines):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collections.csv'
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            return list(BRAHMSExportReader(str(path), delimiter='|').get_collection_rows())

    def test_reads_quoted_record_and_new_schema(self):
        row = values()
        row[27] = 'Dedicated "with care"'
        records = self.read_records(['|'.join(HEADER), '"' + '|'.join(row).replace('"', '""') + '"'])
        self.assertEqual(records[0]['commemorationtext'], 'Dedicated "with care"')

    def test_reassembles_label_text_continuation(self):
        row = values()
        row[18] = 'First geography'
        first = '"' + '|'.join(row[:19]) + '"'
        second = '|'.join(['continued geography', *row[19:]])
        records = self.read_records(['|'.join(HEADER), first, second])
        self.assertEqual(records[0]['labelrangetext'], 'First geography\ncontinued geography')
        self.assertEqual(records[0]['plantid'], '2026-0001*1')

    def test_reassembles_multiline_commemoration_text(self):
        row = values()
        row[27] = 'In loving memory of'
        first = '"' + '|'.join(row[:28])
        final = '"' + '|'.join(['a beloved friend', *row[28:]]) + '"'
        records = self.read_records(['|'.join(HEADER), first, 'whose kindness remains', final])
        self.assertEqual(records[0]['commemorationtext'],
                         'In loving memory of\nwhose kindness remains\na beloved friend')

    def test_skips_incomplete_record_and_reports_it(self):
        with self.assertLogs('parse', level='ERROR') as logs:
            records = self.read_records(['|'.join(HEADER), '|'.join(values()[:8])])
        self.assertEqual(records, [])
        self.assertIn('Skipping malformed collection record', '\n'.join(logs.output))

    def test_legacy_header_maps_empty_commemoration_text(self):
        legacy_header = [column for column in HEADER if column != 'commemorationtext']
        row = values()
        del row[27]
        records = self.read_records(['|'.join(legacy_header), '"' + '|'.join(row) + '"'])
        self.assertEqual(records[0].get('commemorationtext', ''), '')


class PayloadTests(unittest.TestCase):
    def test_payload_preserves_new_field_and_following_columns(self):
        row = dict(zip(HEADER, values(), strict=True))
        row['commemorationtext'] = 'A dedication\nwith a second line'
        payload = brahms_row_to_payload(row)
        self.assertEqual(payload['commemoration_text'], 'A dedication\nwith a second line')
        self.assertEqual(payload['plant_date'], '2026-10-02')
        self.assertTrue(payload['species']['utah_native'])
        self.assertTrue(payload['species']['arborist_rec'])
        self.assertEqual(parse_last_modified(row['lastmodifiedon']).year, 2026)

    def test_invalid_coordinates_skip_only_the_record(self):
        row = dict(zip(HEADER, values(), strict=True))
        row['latitude'] = 'not-a-coordinate'
        self.assertIsNone(brahms_row_to_payload(row))


class ImageReaderTests(unittest.TestCase):
    def image_row(self, **overrides):
        row = {column: '' for column in IMAGE_HEADER}
        row.update({
            'imagefile': 'example.jpg', 'copyright': 'Copyright',
            'directoryname': r'B:\Example', 'genusname': 'Genus',
            'speciesname': 'species', 'lastmodifiedon': '2026-10-20 21:42:22.553000000',
        })
        row.update(overrides)
        return row

    def read_image_records(self, rows, header=IMAGE_HEADER):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'images.csv'
            lines = ['|'.join(header), *['|'.join(row[column] for column in header) for row in rows]]
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            return list(BRAHMSExportReader(str(path), delimiter='|').get_image_rows())

    def test_reads_standard_13_column_image_row(self):
        records = self.read_image_records([self.image_row(plantid='2026-0316*1')])
        self.assertEqual(records, [self.image_row(plantid='2026-0316*1')])

    def test_rejects_legacy_image_schema(self):
        with self.assertLogs('parse', level='ERROR') as logs:
            records = self.read_image_records([self.image_row()], header=IMAGE_HEADER[0:3] + IMAGE_HEADER[4:])
        self.assertEqual(records, [])
        self.assertIn('Invalid image export header', '\n'.join(logs.output))

    def test_skips_missing_required_image_fields(self):
        with self.assertLogs('parse', level='ERROR') as logs:
            records = self.read_image_records([self.image_row(lastmodifiedon='')])
        self.assertEqual(records, [])
        self.assertIn('missing lastmodifiedon', '\n'.join(logs.output))

    def test_invalid_image_timestamp_is_not_parseable(self):
        self.assertIsNone(parse_last_modified('not a timestamp'))

    def test_taxonomy_query_is_used_without_plant_id(self):
        row = self.image_row(cultivar='Cultivar')
        self.assertEqual(extract_species_query(row), {
            'genus': 'Genus', 'name': 'species', 'subspecies': '', 'variety': '',
            'subvariety': '', 'forma': '', 'subforma': '', 'cultivar': 'Cultivar',
        })

    def test_missing_plant_id_and_genus_has_no_query(self):
        row = self.image_row(genusname='', speciesname='')
        with self.assertLogs('parse', level='ERROR'):
            self.assertIsNone(extract_species_query(row))

    def test_resolves_b_drive_and_rdp_photo_paths_on_macos(self):
        with tempfile.TemporaryDirectory() as directory:
            photo_root = Path(directory) / 'AA BRAHMS Resized Photos'
            with patch('parse.sys.platform', 'darwin'), patch('parse._mac_photo_library_root', return_value=photo_root):
                b_drive = construct_img_filepath(self.image_row(directoryname=r'B:\Trees\Acer'))
                rdp = construct_img_filepath(self.image_row(
                    directoryname=r'\\tsclient\AA BRAHMS Resized Photos\!Dedications Resized'))
        self.assertEqual(b_drive, str(photo_root.resolve() / 'Trees' / 'Acer' / 'example.jpg'))
        self.assertEqual(rdp, str(photo_root.resolve() / '!Dedications Resized' / 'example.jpg'))

    def test_rejects_unsafe_or_unknown_windows_photo_paths(self):
        with patch('parse.sys.platform', 'darwin'):
            with self.assertRaises(ValueError):
                construct_img_filepath(self.image_row(directoryname=r'B:\..\outside'))
            with self.assertRaises(ValueError):
                construct_img_filepath(self.image_row(directoryname=r'\\server\share\images'))


class ImagePostingTests(unittest.TestCase):
    def test_collection_image_upload_uses_exact_plant_endpoint(self):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        with tempfile.NamedTemporaryFile() as image_file:
            poster = object.__new__(RBGAPIPoster)
            poster.scheme = 'https'
            poster.netloc = 'example.test'
            poster.session = Session()
            response = poster.post_plant_image('2026-0295*70', image_file.name, 'Copyright')

        self.assertEqual(response.status_code, 200)
        url, request = poster.session.calls[0]
        self.assertEqual(url, 'https://example.test/plants/api/plants/2026-0295%2A70/set-image/')
        self.assertEqual(request['data'], {'copyright_info': 'Copyright'})
        self.assertTrue(request['verify'] is False)


class OctoberExportTests(unittest.TestCase):
    def test_october_export_reconstructs_completely_offline(self):
        path = Path(__file__).parents[1] / 'csv_exports/2026/October/living_plant_collections.csv'
        records = list(BRAHMSExportReader(str(path), encoding='utf-16', delimiter='|').get_collection_rows())
        self.assertEqual(len(records), 17287)
        self.assertEqual(sum(bool(record['commemorationtext'].strip()) for record in records), 360)
        self.assertEqual(sum('\n' in record['labelrangetext'] or '\n' in record['commemorationtext']
                             for record in records), 174)

    def test_october_image_export_schema_and_lookup_counts(self):
        path = Path(__file__).parents[1] / 'csv_exports/2026/October/image_locations_10-24-26.csv'
        records = list(BRAHMSExportReader(str(path), encoding='utf-16', delimiter='|').get_image_rows())
        self.assertEqual(len(records), 10481)
        plant_rows = [record for record in records if record['plantid']]
        taxonomy_rows = [record for record in records if not record['plantid']]
        with self.assertLogs('parse', level='ERROR') as logs:
            queries = [extract_species_query(record) for record in taxonomy_rows]
        self.assertEqual(len(plant_rows), 528)
        self.assertEqual(sum(query is not None and 'genus' in query for query in queries), 9944)
        self.assertEqual(sum(query is None for query in queries), 9)
        self.assertEqual(len(logs.output), 9)
