import argparse
import configparser
import logging
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from requests import HTTPError

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
parser.add_argument('--delimiter', default='|',
                    help='Delimiter to use when parsing CSV files')
parser.add_argument('--encoding', default='utf-16',
                    help='Encoding to use when reading CSV files')
parser.set_defaults(ssl=True)

args = vars(parser.parse_args())


def post_row(poster, row):
    payload = brahms_row_to_payload(row)
    if payload:
        print(f"\nAttempting to post: {payload}\n")
        try:
            resp = poster.post_collection(payload)
            if resp.status_code != 201:
                root.warning(f"Attempt to post {payload} returned status code: {resp.status_code}")
        except HTTPError as e:
            root.error(f'Error posting row: {row}\n\t{e}')
            if len(e.response.content) < 1000:
                root.error(e.response.content)
            raise


def post_plant_collections(poster, plant_data_filepath, delimiter, encoding, last_run):
    """Post header-mapped collection rows reconstructed from the BRAHMS export."""
    sql_reader = BRAHMSExportReader(file_path=plant_data_filepath, encoding=encoding, delimiter=delimiter)

    processes = []
    with ThreadPoolExecutor(max_workers=4) as executor:
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
            if last_modified and last_modified <= last_run:
                root.warning('Row with plant ID %s has not been modified (%s) since last run (%s).',
                             row.get('plantid', '<missing>'), last_modified, last_run)
            processes.append(executor.submit(post_row, poster, row))

    for task in as_completed(processes):
        try:
            if task.result():
                root.info(task.result())
        except HTTPError as e:
            root.error(f'Error in task: {e}')


def post_image(poster, row):
    root.info(f"\nAttempting to post image row: {row}\n")

    try:
        img_filepath = construct_img_filepath(row)
    except ValueError as error:
        root.error('Skipping image %r: %s', row.get('imagefile', '<missing>'), error)
        return
    copyright_info = extract_copyright_info(row)
    plant_id = row['plantid'].strip()
    if plant_id:
        resp = poster.post_plant_image(plant_id, img_filepath, copyright_info)
        if resp and resp.status_code not in [200, 201]:
            root.warning('Attempt to post %s for plant %s returned status code: %s',
                         img_filepath, plant_id, resp.status_code)
        return

    species_image_payload = extract_species_query(row)
    if species_image_payload is None:
        return
    root.debug('Searching for species with %s using image row %s', species_image_payload, row)
    resp = poster.get_species_from_query(species_image_payload)
    content = resp.json()

    if content['count'] == 1:
        species_pk = content['results'][0]['id']

        resp = poster.post_species_image(species_pk, img_filepath, copyright_info)

        if resp and resp.status_code not in [200, 201]:
            root.warning(f"Attempt to post {img_filepath} for species {species_pk} returned status code: "
                         f"{resp.status_code}")

    elif content['count'] == 0:
        root.warning(f'No species returned when searching with: {species_image_payload}')
    else:
        root.warning(f'Multiple species returned when searching with: {species_image_payload}')


def post_image_to_species(poster, image_data_filepath, delimiter, encoding, last_run):
    image_location_reader = BRAHMSExportReader(
        file_path=image_data_filepath, encoding=encoding, delimiter=delimiter)

    processes = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        for row in image_location_reader.get_image_rows():
            last_modified_value = row['lastmodifiedon']
            last_modified = parse_last_modified(last_modified_value)
            if last_modified is None:
                root.error('Skipping image %r due to invalid last-modified value: %r',
                           row['imagefile'], last_modified_value)
                continue
            if last_modified <= last_run:
                root.warning('Image %r has not been modified (%s) since last run (%s).',
                             row['imagefile'], last_modified, last_run)
                continue
            processes.append(executor.submit(post_image, poster, row))

    for task in as_completed(processes):
        if task.result():
            root.info(task.result())


def write_file():
    config.write(open('config.ini', 'w'))


def main():
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
    poster = RBGAPIPoster(username=username, password=password, netloc=args['target'], ssl=args['ssl'])

    if args['plant_data_path']:
        plant_data_filepath = args['plant_data_path']
        post_plant_collections(poster, plant_data_filepath, args['delimiter'], args['encoding'], last_run)

    if args['image_data_path']:
        image_data_filepath = args['image_data_path']
        post_image_to_species(poster, image_data_filepath, args['delimiter'], args['encoding'], last_run)


if __name__ == '__main__':
    main()

    # Write last run time before exiting
    config['DEFAULT']['last_run'] = datetime.now().isoformat()
    with open('config.ini', 'w') as configfile:
        config.write(configfile)
