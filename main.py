import argparse
import logging
import os
import sys

from requests import HTTPError

from parse import BRAHMSExportReader, brahms_row_to_payload, construct_img_filepath, extract_species_info
from post import RBGAPIPoster

log_formatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
root = logging.getLogger()
root.setLevel(logging.DEBUG)

fileHandler = logging.FileHandler("main.log")
fileHandler.setFormatter(log_formatter)
fileHandler.setLevel(logging.WARNING)
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
parser.add_argument('--plant-data-path', default='living_plant_collections.csv',
                    help='Path to CSV file containing BRAHMS data export of living collections')
parser.add_argument('--image-data-path', default='species_image_locations.csv',
                    help='Path to CSV file containing BRAHMS data export of species images and related data')
parser.set_defaults(ssl=True)

args = vars(parser.parse_args())


def post_plant_collections(poster, plant_data_filepath):
    sql_reader = BRAHMSExportReader(file_path=plant_data_filepath, encoding='utf-16le', delimiter='|')

    sql_rows = iter(sql_reader.get_rows())
    next(sql_rows)  # Skip header row
    for row in sql_rows:
        payload = brahms_row_to_payload(row)
        root.info(f"Attempting to post: {payload}")
        try:
            resp = poster.post_collection(payload)
            if resp.status_code != 200:
                root.warning(f"Attempt to post {payload} returned status code: {resp.status_code}")
        except HTTPError as e:
            print(e.response.text)
            root.error(e)
            root.error(e.response.text)
            root.error(payload)


def post_image_to_species(poster, image_data_filepath):
    try:
        image_location_reader = BRAHMSExportReader(file_path=image_data_filepath, delimiter='|')
        img_rows = image_location_reader.get_rows()
        next(img_rows)  # Skip header row
    except UnicodeDecodeError:
        image_location_reader = BRAHMSExportReader(file_path=image_data_filepath, encoding='utf-16le', delimiter='|')
        img_rows = image_location_reader.get_rows()
        next(img_rows)  # Skip header row

    for row in img_rows:
        img_filepath = construct_img_filepath(row)
        species_image_payload = extract_species_info(row)
        root.debug(f"Species query returned {species_image_payload} using {row}")
        resp = poster.get_species_from_query(species_image_payload)
        content = resp.json()

        if content['count'] == 1:
            species_pk = content['results'][0]['id']

            resp = poster.post_species_image(species_pk, img_filepath)

            if resp.status_code != 200:
                root.warning(f"Attempt to post {img_filepath} for species {species_pk} returned status code: "
                               f"{resp.status_code}\n{content}")


def main():
    username = os.environ.get('RBG_API_USERNAME')
    password = os.environ.get('RBG_API_PASSWORD')
    if username is None or password is None:
        root.error("Username and password must be set as environment variables.")
        sys.exit("[ERROR] Please set RBG_API_USERNAME and RBG_API_PASSWORD environment variables.")
    poster = RBGAPIPoster(username=username, password=password, netloc=args['target'], ssl=args['ssl'])

    plant_data_filepath = args['plant_data_path']
    image_data_filepath = args['image_data_path']

    post_plant_collections(poster, plant_data_filepath)
    post_image_to_species(poster, image_data_filepath)


if __name__ == '__main__':
    main()
