import logging
import requests
import sys
from urllib.parse import quote, urlunsplit
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class RBGAPIPoster:
    def __init__(self, username, password, netloc='redbuttegarden.org:80', ssl=True):
        self.session = requests.session()

        retry_strategy = Retry(
            total=5,  # Maximum number of retries
            backoff_factor=1,  # Exponential backoff: sleep 1s, 2s, 4s, 8s...
            # HTTP statuses to retry on
            status_forcelist=[429, 500, 502, 503, 504],
            # Enable retries for POST requests
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.username = username
        self.password = password
        self.netloc = netloc
        self.ssl = ssl
        self.scheme = self.get_scheme()
        tokens = self.get_tokens()
        self.session.headers = {'Accept': 'application/json; q=1.0, */*',
                                'X-CSRFToken': tokens['csrftoken'],
                                'Authorization': 'Token ' + tokens['drf_token']}

    def get_tokens(self):
        url = urlunsplit(
            (self.scheme, self.netloc, '/plants/api/token/', '', ''))
        # Disabled ssl verification for self-signed cert
        response = self.session.post(
            url, data={'username': self.username, 'password': self.password}, verify=False)
        try:
            assert response.status_code == 200
        except AssertionError:
            logger.error(
                "Failed to get token. Are you sure a user with these credentials exists on the website?")
            sys.exit(1)
        tokens = {'csrftoken': response.cookies['csrftoken'],
                  'drf_token': response.json()['token']}
        return tokens

    def get_scheme(self):
        if self.ssl:
            return 'https'
        else:
            return 'http'

    def get_species_from_query(self, payload):
        url = urlunsplit(
            (self.scheme, self.netloc, '/plants/api/species/', '', ''))

        r = self.session.get(url, params=payload)
        r.raise_for_status()
        return r

    def post_collection(self, payload, attempts=1):
        url = urlunsplit(
            (self.scheme, self.netloc, '/plants/api/collections/', '', ''))

        if attempts <= 3:
            # Disabled ssl verification for self-signed cert
            r = self.session.post(url, json=payload, verify=False)
            if r.status_code == '504':
                attempts += 1
                logger.warning(
                    f'Collection POST attempt timed out. Trying again... attempt #{attempts}')
                self.post_collection(payload, attempts)
            else:
                logger.debug(f'Status code: {r.status_code}')
                r.raise_for_status()
            return r
        else:
            return None

    def post_species_image(self, pk, file_path, copyright_info):
        url = urlunsplit(
            (self.scheme, self.netloc, f'/plants/api/species/{pk}/set-image/', '', ''))
        return self._post_image(url, f'Species {pk}', file_path, copyright_info)

    def post_plant_image(self, plant_id, file_path, copyright_info):
        """Attach an image to one collection, identified by its exact plant ID."""
        encoded_plant_id = quote(str(plant_id), safe='')
        url = urlunsplit(
            (self.scheme, self.netloc, f'/plants/api/plants/{encoded_plant_id}/set-image/', '', ''))
        return self._post_image(url, f'Plant {plant_id}', file_path, copyright_info)

    def _post_image(self, url, target, file_path, copyright_info):
        try:
            with open(file_path, 'rb') as f:
                try:
                    r = self.session.post(url, data={'copyright_info': copyright_info}, files={
                                          'image': f}, verify=False)  # Disabled ssl verification for self-signed cert
                    r.raise_for_status()
                    return r
                except OSError as e:
                    logger.error(
                        f'[{target}] Error ({e}) posting: {file_path}')
        except FileNotFoundError:
            logger.error(f'[{target}] Unable to find file: {file_path}')
