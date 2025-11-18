import argparse
import configparser
import logging
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from requests import HTTPError

from parse import BRAHMSExportReader, brahms_row_to_payload, construct_img_filepath, extract_copyright_info, \
    extract_species_info
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
    """
    Assumes last modified date is 31st element of row data
    """
    sql_reader = BRAHMSExportReader(file_path=plant_data_filepath, encoding=encoding, delimiter=delimiter)

    processes = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        sql_rows = iter(sql_reader.get_rows())
        next(sql_rows)  # Skip header row
        for row in sql_rows:
            if row[30]:  # last modified data
                try:
                    # Most are in ISO Format
                    last_modified = datetime.fromisoformat(row[30][:-6])
                except ValueError:
                    # Others look like 3/1/2021  1:07:38 PM
                    last_modified = datetime.strptime(row[30], '%m/%d/%Y %I:%M:%S %p')

                if last_modified > last_run:
                    processes.append(executor.submit(post_row, poster, row))
                else:
                    root.warning(f'Row with plant ID {row[21]} has not been modified ({last_modified}) since last run ({last_run}).')
                    # Uncomment to also post these records anyway
                    processes.append(executor.submit(post_row, poster, row))
            else:
                # Uncomment to also post records without last modified date
                #processes.append(executor.submit(post_row, poster, row))
                pass

    for task in as_completed(processes):
        if task.result():
            root.info(task.result())


def post_image(poster, row):
    root.info(f"\nAttempting to post image row: {row}\n")

    img_filepath = construct_img_filepath(row)
    species_image_payload = extract_species_info(row)
    copyright_info = extract_copyright_info(row)
    root.debug(f"Species query returned {species_image_payload} using {row}")
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
    try:
        image_location_reader = BRAHMSExportReader(file_path=image_data_filepath, delimiter=delimiter)
        img_rows = image_location_reader.get_rows()
        next(img_rows)  # Skip header row
    except UnicodeDecodeError:
        image_location_reader = BRAHMSExportReader(file_path=image_data_filepath, encoding=encoding, delimiter=delimiter)
        img_rows = image_location_reader.get_rows()
        next(img_rows)  # Skip header row

    processes = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        for row in img_rows:
            if row[11]:  # last modified data
                last_modified = datetime.fromisoformat(row[11][:-6])

                if last_modified > last_run:
                    processes.append(executor.submit(post_image, poster, row))
                else:
                    root.warning(f'Image with title "{row[0]}" has not been modified ({last_modified}) since last run ({last_run}).')
                    continue
            else:
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
