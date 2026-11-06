import argparse
import configparser
import logging
import math
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime

from parse import BRAHMSExportReader, brahms_row_to_payload, construct_img_filepath, extract_copyright_info, \
    extract_species_query, parse_last_modified
from post import RBGAPIPoster

config = configparser.ConfigParser()

log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
root = logging.getLogger()
root.setLevel(logging.DEBUG)

fileHandler = logging.FileHandler("main.log")
fileHandler.setFormatter(log_formatter)
root.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(log_formatter)
root.addHandler(consoleHandler)

parser = argparse.ArgumentParser(description='Parse BRAHMS data CSV files and POST that data to the RBG website.')
parser.add_argument('--target', default='redbuttegarden.org',
                    help='URL to connect to (default: redbuttegarden.org')
parser.add_argument('--ssl', dest='ssl', action='store_true',
                    help='Use SSL for request connections')
parser.add_argument('--no-ssl', dest='ssl', action='store_false',
                    help='Disable SSL for request connections')
parser.add_argument('--plant-data-path',
                    help='Path to CSV file containing BRAHMS data export of living collections')
parser.add_argument('--image-data-path',
                    help='Path to CSV file containing BRAHMS data export of species images and related data')
parser.add_argument('--image-batch-size', type=int, default=RBGAPIPoster.IMAGE_BATCH_SIZE,
                    help='Number of images per request (1-20; default: 20)')
parser.add_argument('--delimiter', default='|',
                    help='Delimiter to use when parsing CSV files')
parser.add_argument('--encoding', default='utf-16',
                    help='Encoding to use when reading CSV files')
parser.add_argument('--ca-bundle',
                    help='Path to a PEM bundle for a private HTTPS certificate authority; '
                         'defaults to RBG_CA_BUNDLE or the system trust store')
parser.set_defaults(ssl=True)


def _format_bytes(value):
    """Format a byte count for operator-facing progress messages."""
    value = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


class BatchProgressReporter:
    """Emit periodic status while staging and uploading one image batch."""

    HEARTBEAT_SECONDS = 30

    def __init__(self, batch_number, batch_total, items):
        self.batch_number = batch_number
        self.batch_total = batch_total
        self.item_count = len(items)
        self.known_bytes = sum(item.get('source_size', 0) or 0 for item in items)
        self._state = {
            'phase': 'starting',
            'current_file': None,
            'files_completed': 0,
            'bytes_staged': 0,
            'retry_count': 0,
            'error': None,
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started_at = None
        self._thread = None

    def __enter__(self):
        self._started_at = time.monotonic()
        root.info(
            'Image batch %d/%d starting: %d image(s), %s known source data.',
            self.batch_number, self.batch_total, self.item_count,
            _format_bytes(self.known_bytes),
        )
        self._thread = threading.Thread(
            target=self._heartbeat, name='image-progress', daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        self._thread.join()

    def update(self, **state):
        with self._lock:
            self._state.update(state)

    def complete(self, results):
        elapsed = time.monotonic() - self._started_at
        succeeded = results.get('success', 0)
        failed = sum(count for code, count in results.items() if code != 'success')
        root.info(
            'Image batch %d/%d complete in %.1fs: %d succeeded, %d failed.',
            self.batch_number, self.batch_total, elapsed, succeeded, failed,
        )

    def _heartbeat(self):
        while not self._stop.wait(self.HEARTBEAT_SECONDS):
            self.log_heartbeat()

    def log_heartbeat(self):
        """Write one heartbeat; public for deterministic tests and diagnostics."""
        with self._lock:
            state = self._state.copy()
        elapsed = time.monotonic() - self._started_at
        retry = (
            f"; retry {state['retry_count']} after {state['error']}"
            if state['retry_count'] else ''
        )
        root.info(
            'Image batch %d/%d still %s: %d/%d file(s) staged, %s/%s, '
            'current file: %s, %.0fs elapsed%s.',
            self.batch_number, self.batch_total, state['phase'],
            state['files_completed'], self.item_count,
            _format_bytes(state['bytes_staged']), _format_bytes(self.known_bytes),
            state['current_file'] or '<none>', elapsed, retry,
        )


def _batch(items, batch_size):
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def _log_batch_results(response, kind, submitted_items=None):
    """Log independent batch-item failures and reject malformed responses."""
    if isinstance(response, dict):
        body = response
    else:
        try:
            body = response.json()
        except ValueError as error:
            raise RuntimeError(f'Malformed {kind} batch response: invalid JSON') from error
    results = body.get('results') if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise RuntimeError(f'Malformed {kind} batch response: results must be a list')
    outcome_counts = Counter()
    items_by_client_id = {
        item['client_id']: item
        for item in submitted_items or []
        if 'client_id' in item
    }
    for result in results:
        if isinstance(result, dict) and result.get('status') == 'success':
            outcome_counts['success'] += 1
            continue
        code = result.get('code', 'invalid_result') if isinstance(result, dict) else 'invalid_result'
        outcome_counts[code] += 1
        item = items_by_client_id.get(result.get('client_id')) if isinstance(result, dict) else None
        if item and 'plant_id' in item:
            root.error(
                '%s batch item failed for plant_id %s: %s',
                kind, item['plant_id'], result,
            )
        elif item and 'species' in item:
            root.error(
                '%s batch item failed for species target %s: %s',
                kind, item['species'], result,
            )
        else:
            root.error('%s batch item failed: %s', kind, result)
    return outcome_counts


def post_plant_collections(poster, plant_data_filepath, delimiter, encoding, last_run):
    """Post header-mapped collection rows reconstructed from the BRAHMS export."""
    sql_reader = BRAHMSExportReader(file_path=plant_data_filepath, encoding=encoding, delimiter=delimiter)
    payloads = []
    for row in sql_reader.get_collection_rows():
        last_modified_value = row.get('lastmodifiedon', '')
        if not last_modified_value:
            root.warning('Skipping plant ID %s because it has no last-modified value.',
                         row.get('plantid', '<missing>'))
            continue
        last_modified = parse_last_modified(last_modified_value)
        if last_modified is None:
            root.error('Skipping plant ID %s due to invalid last-modified value: %r',
                       row.get('plantid', '<missing>'), last_modified_value)
            continue
        if last_modified <= last_run:
            root.info('Skipping unchanged plant ID %s.', row.get('plantid', '<missing>'))
            continue
        payload = brahms_row_to_payload(row)
        if payload:
            payloads.append(payload)

    for batch in _batch(payloads, poster.COLLECTION_BATCH_SIZE):
        root.info('Submitting collection batch with %d record(s).', len(batch))
        response = poster.post_collections_bulk(batch)
        results = _log_batch_results(response, 'collection')
        root.info(
            'Collection batch complete: %d succeeded, %d failed.',
            results.get('success', 0),
            sum(count for code, count in results.items() if code != 'success'),
        )


def post_image_to_species(
    poster, image_data_filepath, delimiter, encoding, last_run, image_batch_size=None,
):
    image_location_reader = BRAHMSExportReader(
        file_path=image_data_filepath, encoding=encoding, delimiter=delimiter)

    image_batch_size = image_batch_size or poster.IMAGE_BATCH_SIZE
    items = []
    skipped = 0
    unavailable = 0
    for row_number, row in enumerate(image_location_reader.get_image_rows(), start=1):
        last_modified_value = row['lastmodifiedon']
        last_modified = parse_last_modified(last_modified_value)
        if last_modified is None:
            root.error('Skipping image %r due to invalid last-modified value: %r',
                       row['imagefile'], last_modified_value)
            continue
        if last_modified <= last_run:
            root.info('Skipping unchanged image %r.', row['imagefile'])
            skipped += 1
            continue
        try:
            image_path = construct_img_filepath(row)
        except ValueError as error:
            root.error('Skipping image %r: %s', row['imagefile'], error)
            skipped += 1
            continue
        if not os.path.isfile(image_path):
            root.error('Skipping image %r because its file is unavailable: %s',
                       row['imagefile'], image_path)
            unavailable += 1
            continue

        item = {
            'client_id': f'image-{row_number}',
            'file_path': image_path,
            'copyright_info': extract_copyright_info(row),
        }
        plant_id = row['plantid'].strip()
        if plant_id:
            item['plant_id'] = plant_id
        else:
            species_target = extract_species_query(row)
            if species_target is None:
                skipped += 1
                continue
            item['species'] = species_target
        try:
            item['source_size'] = os.path.getsize(image_path)
        except OSError as error:
            root.warning(
                'Could not determine the size of image %r (%s); it will be '
                'staged with Box Drive retry handling.', row['imagefile'], error,
            )
            item['source_size'] = None
        items.append(item)
        if row_number % 250 == 0:
            root.info(
                'Scanned %d image rows: %d eligible, %d skipped, %d unavailable.',
                row_number, len(items), skipped, unavailable,
            )

    batch_total = math.ceil(len(items) / image_batch_size) if items else 0
    known_bytes = sum(item.get('source_size', 0) or 0 for item in items)
    unknown_sizes = sum(item.get('source_size') is None for item in items)
    root.info(
        'Image discovery complete: %d eligible, %d skipped, %d unavailable; '
        '%s known source data across %d batch(es)%s.',
        len(items), skipped, unavailable, _format_bytes(known_bytes), batch_total,
        f' ({unknown_sizes} unknown size)' if unknown_sizes else '',
    )

    total_results = Counter()
    for batch_number, batch in enumerate(_batch(items, image_batch_size), start=1):
        with BatchProgressReporter(batch_number, batch_total, batch) as progress:
            response = poster.post_images_direct(batch, progress_callback=progress.update)
            results = _log_batch_results(response, 'image', batch)
            progress.complete(results)
        total_results.update(results)
    if items:
        failures = {
            code: count for code, count in total_results.items() if code != 'success'
        }
        root.info(
            'Image upload complete: %d succeeded, %d failed%s.',
            total_results.get('success', 0), sum(failures.values()),
            f'; failures by code: {failures}' if failures else '',
        )


def write_file():
    config.write(open('config.ini', 'w'))


def main():
    args = vars(parser.parse_args())
    if not 1 <= args['image_batch_size'] <= RBGAPIPoster.IMAGE_BATCH_SIZE:
        parser.error(
            f"--image-batch-size must be between 1 and {RBGAPIPoster.IMAGE_BATCH_SIZE}"
        )
    if not os.path.exists('config.ini'):
        # Arbitrarily long ago date
        config['DEFAULT'] = {'last_run': datetime(year=1900, day=1, month=1).isoformat()}
        write_file()

    config.read('config.ini')
    last_run = datetime.fromisoformat(config['DEFAULT']['last_run'])

    username = os.environ.get('RBG_API_USERNAME')
    password = os.environ.get('RBG_API_PASSWORD')
    if username is None or password is None:
        root.error("Username and password must be set as environment variables.")
        sys.exit("[ERROR] Please set RBG_API_USERNAME and RBG_API_PASSWORD environment variables.")
    poster = RBGAPIPoster(
        username=username,
        password=password,
        netloc=args['target'],
        ssl=args['ssl'],
        ca_bundle=args['ca_bundle'],
    )

    if args['plant_data_path']:
        plant_data_filepath = args['plant_data_path']
        post_plant_collections(poster, plant_data_filepath, args['delimiter'], args['encoding'], last_run)

    if args['image_data_path']:
        image_data_filepath = args['image_data_path']
        post_image_to_species(
            poster, image_data_filepath, args['delimiter'], args['encoding'], last_run,
            args['image_batch_size'],
        )


if __name__ == '__main__':
    main()

    # Write last run time before exiting
    config['DEFAULT']['last_run'] = datetime.now().isoformat()
    with open('config.ini', 'w') as configfile:
        config.write(configfile)
