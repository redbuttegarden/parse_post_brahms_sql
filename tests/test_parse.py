import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from PIL import Image as PILImage

from parse import (
    BRAHMSExportReader,
    brahms_row_to_payload,
    construct_img_filepath,
    extract_species_query,
    parse_last_modified,
)
from post import RBGAPIPoster
import main


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
    class Response:
        status_code = 200

        def __init__(self, body=None):
            self.body = body or {'results': []}

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Session:
        def __init__(self):
            self.calls = []
            self.uploads = {}

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            for field, upload in kwargs.get('files', {}).items():
                filename, file_obj = upload
                self.uploads[field] = (filename, file_obj.read(), file_obj.name)
            return ImagePostingTests.Response()

    def test_collection_bulk_upload_uses_batch_endpoint_and_payload(self):
        poster = object.__new__(RBGAPIPoster)
        poster.scheme = 'https'
        poster.netloc = 'example.test'
        poster.session = self.Session()
        payloads = [{'plant_id': 'one'}, {'plant_id': 'two'}]

        response = poster.post_collections_bulk(payloads)

        self.assertEqual(response.status_code, 200)
        url, request = poster.session.calls[0]
        self.assertEqual(url, 'https://example.test/plants/api/collections/bulk/')
        self.assertEqual(request['json'], {'collections': payloads})
        self.assertNotIn('verify', request)

    def test_image_bulk_upload_uses_manifest_and_unique_file_fields(self):
        poster = object.__new__(RBGAPIPoster)
        poster.scheme = 'https'
        poster.netloc = 'example.test'
        poster.session = self.Session()
        with tempfile.TemporaryDirectory() as directory:
            plant_path = Path(directory) / 'plant.jpg'
            species_path = Path(directory) / 'species.jpg'
            plant_path.write_bytes(b'plant image')
            species_path.write_bytes(b'species image')
            poster.post_images_bulk([
                {
                    'client_id': 'plant', 'file_path': str(plant_path),
                    'copyright_info': 'RBG', 'plant_id': '2026-0001*1',
                },
                {
                    'client_id': 'species', 'file_path': str(species_path),
                    'copyright_info': '', 'species': {'genus': 'Genus'},
                },
            ])

            url, request = poster.session.calls[0]
            self.assertEqual(url, 'https://example.test/plants/api/images/bulk/')
            manifest = json.loads(request['data']['manifest'])
            self.assertEqual([item['file_field'] for item in manifest], ['image_0', 'image_1'])
            self.assertEqual(manifest[0]['plant_id'], '2026-0001*1')
            self.assertEqual(manifest[1]['species'], {'genus': 'Genus'})
            self.assertEqual(set(request['files']), {'image_0', 'image_1'})
            self.assertNotIn('verify', request)
            self.assertEqual(poster.session.uploads['image_0'][:2], ('plant.jpg', b'plant image'))
            self.assertEqual(poster.session.uploads['image_1'][:2], ('species.jpg', b'species image'))
            self.assertFalse(os.path.exists(poster.session.uploads['image_0'][2]))

    def test_image_bulk_retries_a_source_file_until_it_is_readable(self):
        poster = object.__new__(RBGAPIPoster)
        poster.scheme = 'https'
        poster.netloc = 'example.test'
        poster.session = self.Session()
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / 'cloud-image.jpg'
            source_path.write_bytes(b'image data')
            real_open = open
            source_opens = 0

            def flaky_open(path, *args, **kwargs):
                nonlocal source_opens
                if os.fspath(path) == str(source_path) and args[0] == 'rb':
                    source_opens += 1
                    if source_opens == 1:
                        raise OSError('Box file is still downloading')
                return real_open(path, *args, **kwargs)

            progress_events = []
            with patch('post.open', side_effect=flaky_open), patch('post.time.sleep') as sleep:
                poster.post_images_bulk(
                    [{
                        'client_id': 'cloud-image', 'file_path': str(source_path),
                        'copyright_info': '', 'plant_id': '2026-0001*1',
                    }],
                    progress_callback=lambda **event: progress_events.append(event),
                    retry_interval=15,
                )

        self.assertEqual(source_opens, 2)
        sleep.assert_called_once_with(15)
        self.assertIn('waiting for Box Drive', [event['phase'] for event in progress_events])
        self.assertEqual(poster.session.uploads['image_0'][:2], ('cloud-image.jpg', b'image data'))

    def test_image_bulk_fails_before_staging_when_temporary_storage_is_too_small(self):
        poster = object.__new__(RBGAPIPoster)
        poster.scheme = 'https'
        poster.netloc = 'example.test'
        poster.session = self.Session()
        with patch('post.shutil.disk_usage') as disk_usage:
            disk_usage.return_value.free = 1
            with self.assertRaisesRegex(RuntimeError, 'temporary storage'):
                poster.post_images_bulk([{
                    'client_id': 'large', 'file_path': 'not-read.jpg',
                    'copyright_info': '', 'plant_id': '2026-0001*1',
                    'source_size': 2,
                }])

    def test_direct_upload_stages_sends_metadata_uploads_to_s3_and_finalizes(self):
        class DirectSession:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith('/initiate/'):
                    client_id = kwargs['json']['uploads'][0]['client_id']
                    return ImagePostingTests.Response({'results': [{
                        'client_id': client_id,
                        'status': 'success',
                        'pending_id': 'pending-1',
                        'upload': {
                            'url': 'https://s3.example/upload',
                            'fields': {'key': 'brahms-staging/opaque'},
                        },
                    }]})
                if not url.endswith('/finalize/'):
                    raise AssertionError(f'Unexpected direct API URL: {url}')
                return ImagePostingTests.Response({'results': [{
                    'client_id': 'direct-result', 'status': 'success', 'created': True,
                }]})

        poster = object.__new__(RBGAPIPoster)
        poster.scheme = 'https'
        poster.netloc = 'example.test'
        poster.verify = True
        poster.session = DirectSession()
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / 'direct.jpg'
            PILImage.new('RGB', (3, 2), color='red').save(image_path, 'JPEG')
            s3_calls = []

            def post_to_s3(url, **kwargs):
                s3_calls.append((url, kwargs))
                return ImagePostingTests.Response()

            with patch('post.requests.post', side_effect=post_to_s3):
                result = poster.post_images_direct([{
                    'client_id': 'ignored-for-direct-upload',
                    'file_path': str(image_path), 'copyright_info': 'RBG',
                    'plant_id': '2026-0001*1',
                }])

        initiate_url, initiate_request = poster.session.calls[0]
        self.assertEqual(
            initiate_url, 'https://example.test/plants/api/images/direct-upload/initiate/'
        )
        upload = initiate_request['json']['uploads'][0]
        self.assertTrue(upload['client_id'].startswith('direct-'))
        self.assertEqual(upload['original_filename'], 'direct.jpg')
        self.assertEqual(upload['content_type'], 'image/jpeg')
        self.assertEqual((upload['image_width'], upload['image_height']), (3, 2))
        self.assertEqual(len(upload['sha256']), 64)
        self.assertEqual(s3_calls[0][0], 'https://s3.example/upload')
        self.assertEqual(s3_calls[0][1]['data'], {'key': 'brahms-staging/opaque'})
        self.assertEqual(s3_calls[0][1]['files']['file'][0], 'direct.jpg')
        finalize_url, finalize_request = poster.session.calls[1]
        self.assertEqual(
            finalize_url, 'https://example.test/plants/api/images/direct-upload/finalize/'
        )
        self.assertEqual(finalize_request['json'], {'uploads': [{'pending_id': 'pending-1'}]})
        self.assertEqual(result['results'][0]['status'], 'success')

    def test_direct_upload_bounds_long_filename_and_reduces_oversized_image(self):
        class DirectSession:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith('/initiate/'):
                    client_id = kwargs['json']['uploads'][0]['client_id']
                    return ImagePostingTests.Response({'results': [{
                        'client_id': client_id, 'status': 'success',
                        'pending_id': 'pending-1',
                        'upload': {'url': 'https://s3.example/upload', 'fields': {}},
                    }]})
                return ImagePostingTests.Response({'results': [{
                    'client_id': 'direct-result', 'status': 'success', 'created': True,
                }]})

        poster = object.__new__(RBGAPIPoster)
        poster.scheme = 'https'
        poster.netloc = 'example.test'
        poster.verify = True
        poster.session = DirectSession()
        long_name = f'{"x" * 110}.jpg'
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / long_name
            # Incompressible source data makes the reduction path deterministic.
            PILImage.frombytes('RGB', (300, 300), os.urandom(300 * 300 * 3)).save(
                image_path, 'JPEG', quality=100,
            )
            s3_uploads = []

            def post_to_s3(url, **kwargs):
                filename, image_file, _content_type = kwargs['files']['file']
                s3_uploads.append((filename, image_file.read()))
                return ImagePostingTests.Response()

            with patch.object(RBGAPIPoster, 'MAX_IMAGE_UPLOAD_BYTES', 4096), \
                    patch('post.requests.post', side_effect=post_to_s3):
                poster.post_images_direct([{
                    'client_id': 'ignored-for-direct-upload',
                    'file_path': str(image_path), 'copyright_info': 'RBG',
                    'plant_id': '2026-0001*1',
                }])

        upload = poster.session.calls[0][1]['json']['uploads'][0]
        self.assertLess(len(upload['original_filename']), 101)
        self.assertTrue(upload['original_filename'].endswith('.jpg'))
        self.assertLess(upload['byte_size'], 4096)
        self.assertEqual(s3_uploads[0][0], upload['original_filename'])
        self.assertLess(len(s3_uploads[0][1]), 4096)

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
        self.assertNotIn('verify', request)


class BatchMainFlowTests(unittest.TestCase):
    class Poster:
        COLLECTION_BATCH_SIZE = 100
        IMAGE_BATCH_SIZE = 20

        def __init__(self):
            self.collection_batches = []
            self.image_batches = []

        def post_collections_bulk(self, payloads):
            self.collection_batches.append(payloads)
            return ImagePostingTests.Response({
                'results': [{'status': 'success'} for _ in payloads]
            })

        def post_images_direct(self, items, progress_callback=None):
            self.image_batches.append(items)
            if progress_callback:
                progress_callback(
                    phase='submitting request', current_file=None,
                    files_completed=len(items), bytes_staged=0,
                )
            return ImagePostingTests.Response({
                'results': [
                    {'client_id': item['client_id'], 'status': 'success'}
                    for item in items
                ]
            })

    def _write_export(self, directory, filename, header, rows):
        path = Path(directory) / filename
        path.write_text(
            '|'.join(header) + '\n' + '\n'.join(
                '|'.join(row[column] for column in header) for row in rows
            ) + '\n',
            encoding='utf-8',
        )
        return str(path)

    def test_collections_skip_unchanged_rows_before_batching(self):
        older = dict(zip(HEADER, values(), strict=True))
        newer = dict(older, plantid='2026-0002*1', lastmodifiedon='2026-10-21 15:25:45')
        older['lastmodifiedon'] = '2026-10-19 15:25:45'
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_export(directory, 'collections.csv', HEADER, [older, newer])
            poster = self.Poster()
            main.post_plant_collections(
                poster, path, '|', 'utf-8', datetime(2026, 10, 20)
            )

        self.assertEqual(len(poster.collection_batches), 1)
        self.assertEqual(
            [item['plant_id'] for item in poster.collection_batches[0]], ['2026-0002*1']
        )

    def test_taxonomy_images_are_sent_to_direct_s3_flow_without_lookup(self):
        row = {column: '' for column in IMAGE_HEADER}
        row.update({
            'imagefile': 'example.jpg', 'copyright': 'RBG',
            'directoryname': r'B:\Example', 'genusname': 'Genus',
            'speciesname': 'species', 'lastmodifiedon': '2026-10-21 15:25:45',
        })
        with tempfile.TemporaryDirectory() as directory, tempfile.NamedTemporaryFile() as image_file:
            path = self._write_export(directory, 'images.csv', IMAGE_HEADER, [row])
            poster = self.Poster()
            with patch('main.construct_img_filepath', return_value=image_file.name):
                main.post_image_to_species(
                    poster, path, '|', 'utf-8', datetime(2026, 10, 20)
                )

        self.assertEqual(len(poster.image_batches), 1)
        item = poster.image_batches[0][0]
        self.assertNotIn('plant_id', item)
        self.assertEqual(item['species']['genus'], 'Genus')

    def test_batch_helper_honors_collection_and_image_boundaries(self):
        self.assertEqual(
            [len(batch) for batch in main._batch(list(range(101)), 100)], [100, 1]
        )
        self.assertEqual(
            [len(batch) for batch in main._batch(list(range(21)), 20)], [20, 1]
        )

    def test_image_batch_error_logs_the_submitted_plant_id(self):
        response = ImagePostingTests.Response({
            'results': [{
                'client_id': 'image-40', 'status': 'error',
                'code': 'plant_not_found', 'detail': 'No collection matches this plant_id.',
            }]
        })
        with self.assertLogs('root', level='ERROR') as logs:
            main._log_batch_results(response, 'image', [{
                'client_id': 'image-40', 'plant_id': '2026-0295*70',
            }])

        self.assertIn('plant_id 2026-0295*70', '\n'.join(logs.output))

    def test_progress_reporter_logs_heartbeat_with_current_file(self):
        reporter = main.BatchProgressReporter(2, 3, [{
            'source_size': 1024,
        }])
        with self.assertLogs('root', level='INFO') as logs:
            with reporter:
                reporter.update(
                    phase='waiting for Box Drive', current_file='/Box/image.jpg',
                    files_completed=0, bytes_staged=0, retry_count=2,
                    error='file is downloading',
                )
                reporter.log_heartbeat()
                reporter.complete({'success': 1})

        output = '\n'.join(logs.output)
        self.assertIn('Image batch 2/3 still waiting for Box Drive', output)
        self.assertIn('/Box/image.jpg', output)
        self.assertIn('Image batch 2/3 complete', output)


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
