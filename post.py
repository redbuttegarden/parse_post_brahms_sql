import logging
import hashlib
import json
import os
import requests
import sys
import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote, urlunsplit
from PIL import Image, ImageOps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class SourceFileUnavailableError(Exception):
    """A source image could not be opened or read from its cloud-backed path."""


class RBGAPIPoster:
    COLLECTION_BATCH_SIZE = 100
    IMAGE_BATCH_SIZE = 20
    # Keep below WAGTAILIMAGES_MAX_UPLOAD_SIZE (4.5 MiB) with room for a
    # deployment to lower its limit slightly.  The direct-upload API also
    # validates this size when it finalizes the S3 object.
    MAX_IMAGE_UPLOAD_BYTES = int(4.4 * 1024 * 1024)
    MAX_UPLOAD_FILENAME_LENGTH = 100
    def __init__(
        self, username, password, netloc='redbuttegarden.org:80', ssl=True,
        ca_bundle=None,
    ):
        self.session = requests.session()
        self.verify = ca_bundle or os.environ.get('RBG_CA_BUNDLE', True)
        if isinstance(self.verify, str) and not os.path.isfile(self.verify):
            raise ValueError(f'CA bundle does not exist: {self.verify}')
        self.session.verify = self.verify

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
        response = self.session.post(
            url, data={'username': self.username, 'password': self.password})
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
            r = self.session.post(url, json=payload)
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

    def post_collections_bulk(self, payloads):
        """Upsert no more than 100 collection payloads in one request."""
        if len(payloads) > self.COLLECTION_BATCH_SIZE:
            raise ValueError(
                f'At most {self.COLLECTION_BATCH_SIZE} collections may be submitted.'
            )
        url = urlunsplit(
            (self.scheme, self.netloc, '/plants/api/collections/bulk/', '', '')
        )
        response = self.session.post(url, json={'collections': payloads})
        response.raise_for_status()
        return response

    def _copy_source_to_staging(
        self, source_path, staged_path, progress_callback, files_completed,
        bytes_staged, retry_interval,
    ):
        """Copy one image locally, retrying until a cloud-backed file is readable."""
        retries = 0
        while True:
            try:
                if progress_callback:
                    progress_callback(
                        phase='staging', current_file=source_path,
                        files_completed=files_completed, bytes_staged=bytes_staged,
                    )
                try:
                    source_file = open(source_path, 'rb')
                except OSError as error:
                    raise SourceFileUnavailableError(error) from error
                with source_file, open(staged_path, 'wb') as staged_file:
                    copied = 0
                    while True:
                        if progress_callback:
                            progress_callback(
                                phase='reading source file', current_file=source_path,
                                files_completed=files_completed,
                                bytes_staged=bytes_staged + copied,
                            )
                        try:
                            chunk = source_file.read(1024 * 1024)
                        except OSError as error:
                            raise SourceFileUnavailableError(error) from error
                        if not chunk:
                            return copied
                        staged_file.write(chunk)
                        copied += len(chunk)
            except SourceFileUnavailableError as error:
                retries += 1
                try:
                    os.unlink(staged_path)
                except FileNotFoundError:
                    pass
                if progress_callback:
                    progress_callback(
                        phase='waiting for Box Drive', current_file=source_path,
                        files_completed=files_completed, bytes_staged=bytes_staged,
                        retry_count=retries, error=str(error.__cause__),
                    )
                logger.warning(
                    'Unable to read %s (attempt %d): %s. Retrying in %d seconds; '
                    'make this file available offline in Box Drive if necessary.',
                    source_path, retries, error.__cause__, retry_interval,
                )
                time.sleep(retry_interval)

    def post_images_bulk(self, items, progress_callback=None, retry_interval=15):
        """Upload no more than 20 image associations in one multipart request.

        Each item must contain ``file_path`` plus the manifest fields required by
        the batch image API. Source images are staged locally first, so cloud
        hydration occurs before the upload request and is observable to callers.
        """
        if len(items) > self.IMAGE_BATCH_SIZE:
            raise ValueError(
                f'At most {self.IMAGE_BATCH_SIZE} images may be submitted.'
            )
        url = urlunsplit(
            (self.scheme, self.netloc, '/plants/api/images/bulk/', '', '')
        )
        known_size = sum(item.get('source_size', 0) or 0 for item in items)
        with tempfile.TemporaryDirectory(prefix='rbg-image-batch-') as staging_dir:
            available_space = shutil.disk_usage(staging_dir).free
            required_space = known_size + self.MAX_IMAGE_UPLOAD_BYTES
            if required_space > available_space:
                raise RuntimeError(
                    f'Image batch needs {required_space} bytes of temporary storage, but '
                    f'only {available_space} bytes are available in {staging_dir}.'
                )

            staged_items = []
            bytes_staged = 0
            for index, item in enumerate(items):
                source_path = item['file_path']
                # Do not incorporate a potentially very long source basename
                # into the local staging pathname either.
                staged_path = os.path.join(staging_dir, f'{index}.source')
                copied = self._copy_source_to_staging(
                    source_path, staged_path, progress_callback, index, bytes_staged,
                    retry_interval,
                )
                if progress_callback:
                    progress_callback(
                        phase='preparing image', current_file=source_path,
                        files_completed=index, bytes_staged=bytes_staged + copied,
                    )
                output_format = self._reduce_staged_image(
                    staged_path, Path(source_path).name,
                )
                bytes_staged += os.path.getsize(staged_path)
                staged_items.append((
                    item, staged_path,
                    self._upload_filename(Path(source_path).name, output_format),
                ))
                if progress_callback:
                    progress_callback(
                        phase='staging', current_file=source_path,
                        files_completed=index + 1, bytes_staged=bytes_staged,
                    )

            manifest = []
            with ExitStack() as files_to_close:
                files = {}
                for index, (item, staged_path, upload_filename) in enumerate(staged_items):
                    file_field = f'image_{index}'
                    manifest_item = {
                        key: value
                        for key, value in item.items()
                        if key not in {'file_path', 'source_size'}
                    }
                    manifest_item['file_field'] = file_field
                    manifest.append(manifest_item)
                    files[file_field] = (
                        upload_filename,
                        files_to_close.enter_context(open(staged_path, 'rb')),
                    )

                if progress_callback:
                    progress_callback(
                        phase='submitting request', current_file=None,
                        files_completed=len(items), bytes_staged=bytes_staged,
                    )
                response = self.session.post(
                    url,
                    data={'manifest': json.dumps(manifest)},
                    files=files,
                )
                response.raise_for_status()
                return response

    @staticmethod
    def _direct_client_id(item, sha256):
        """Create a stable idempotency key for one image-target association."""
        target = (
            {'plant_id': item['plant_id']}
            if 'plant_id' in item
            else {'species': item['species']}
        )
        identity = json.dumps(
            {
                'target': target,
                'copyright_info': item['copyright_info'],
                'original_filename': item['original_filename'],
                'content_type': item['content_type'],
                'byte_size': item['byte_size'],
                'image_width': item['image_width'],
                'image_height': item['image_height'],
                'sha256': sha256,
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return f"direct-{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _staged_image_metadata(staged_path):
        """Return the immutable metadata required by direct-upload initiation."""
        digest = hashlib.sha256()
        with open(staged_path, 'rb') as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b''):
                digest.update(chunk)
        try:
            with Image.open(staged_path) as image:
                image_width, image_height = image.size
                content_type = Image.MIME.get(image.format)
                image.verify()
        except (OSError, ValueError) as error:
            raise ValueError(f'Not a valid image: {error}') from error
        if not content_type or not content_type.startswith('image/'):
            raise ValueError('Image format has no supported MIME type.')
        return {
            'byte_size': os.path.getsize(staged_path),
            'sha256': digest.hexdigest(),
            'image_width': image_width,
            'image_height': image_height,
            'content_type': content_type,
        }

    @classmethod
    def _upload_filename(cls, source_filename, image_format=None):
        """Return a stable, Wagtail-safe name for the uploaded image.

        Wagtail's filename field is limited to 100 characters.  Keep both a
        useful leading part of the source name and a short digest to avoid
        turning distinct long names into the same name.  When an oversized
        image is converted (for example PNG to JPEG), use a matching suffix.
        """
        filename = Path(source_filename).name or 'image'
        suffix = Path(filename).suffix
        suffixes = {
            'JPEG': '.jpg',
            'PNG': '.png',
            'WEBP': '.webp',
        }
        if image_format in suffixes:
            expected_suffix = suffixes[image_format]
            if not suffix or image_format == 'JPEG' and suffix.lower() not in {'.jpg', '.jpeg'} \
                    or image_format != 'JPEG' and suffix.lower() != expected_suffix:
                suffix = expected_suffix
                filename = f'{Path(filename).stem or "image"}{suffix}'
        if len(filename) <= cls.MAX_UPLOAD_FILENAME_LENGTH:
            return filename

        stem = Path(filename).stem or 'image'
        name_digest = hashlib.sha256(filename.encode('utf-8')).hexdigest()[:10]
        remaining = cls.MAX_UPLOAD_FILENAME_LENGTH - len(suffix) - len(name_digest) - 1
        return f'{stem[:max(1, remaining)]}-{name_digest}{suffix}'

    @staticmethod
    def _has_transparency(image):
        return 'A' in image.getbands() or 'transparency' in image.info

    def _reduce_staged_image(self, staged_path, source_filename):
        """Re-encode a staged image until it is safely below the upload limit.

        JPEGs are recompressed and then resized as necessary.  Opaque PNGs
        become JPEGs (which is markedly smaller for photographs); transparent
        PNGs remain PNGs.  Images are only changed when their source is too
        large, and the source image outside the temporary staging directory is
        never modified.
        """
        original_size = os.path.getsize(staged_path)
        if original_size < self.MAX_IMAGE_UPLOAD_BYTES:
            return None
        try:
            with Image.open(staged_path) as image:
                image.load()
                source_format = image.format
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('Animated images cannot be reduced safely.')
                # Detach pixels from the source file before its context closes.
                image = ImageOps.exif_transpose(image).copy()
                has_transparency = self._has_transparency(image)
        except (OSError, ValueError) as error:
            raise ValueError(f'Could not reduce oversized image: {error}') from error

        if source_format == 'JPEG' or (source_format == 'PNG' and not has_transparency):
            output_format = 'JPEG'
            working_image = image.convert('RGB')
            qualities = (88, 80, 72, 64, 56, 48, 40)
        elif source_format == 'PNG':
            output_format = 'PNG'
            working_image = image.copy()
            qualities = (None,)
        elif source_format == 'WEBP':
            output_format = 'WEBP'
            working_image = image.copy()
            qualities = (85, 75, 65, 55, 45)
        else:
            raise ValueError(
                f'Cannot automatically reduce oversized {source_format or "unknown"} image {source_filename!r}.'
            )

        width, height = working_image.size
        try:
            for scale_attempt in range(12):
                if scale_attempt:
                    width = max(1, int(width * 0.8))
                    height = max(1, int(height * 0.8))
                    candidate = working_image.resize(
                        (width, height), Image.Resampling.LANCZOS,
                    )
                else:
                    candidate = working_image
                for quality in qualities:
                    temporary_path = f'{staged_path}.reduced'
                    try:
                        save_kwargs = {'format': output_format, 'optimize': True}
                        if output_format in {'JPEG', 'WEBP'}:
                            save_kwargs['quality'] = quality
                        if output_format == 'JPEG':
                            save_kwargs['progressive'] = True
                        if output_format == 'PNG':
                            save_kwargs['compress_level'] = 9
                        candidate.save(temporary_path, **save_kwargs)
                        reduced_size = os.path.getsize(temporary_path)
                        if reduced_size < self.MAX_IMAGE_UPLOAD_BYTES:
                            os.replace(temporary_path, staged_path)
                            logger.info(
                                'Reduced %s from %d to %d bytes (%dx%d, %s).',
                                source_filename, original_size, reduced_size,
                                width, height, output_format,
                            )
                            return output_format
                    finally:
                        try:
                            os.unlink(temporary_path)
                        except FileNotFoundError:
                            pass
        finally:
            working_image.close()

        raise ValueError(
            f'Could not reduce {source_filename!r} below '
            f'{self.MAX_IMAGE_UPLOAD_BYTES} bytes.'
        )

    def _post_direct_api(self, path, payload):
        url = urlunsplit((self.scheme, self.netloc, path, '', ''))
        response = self.session.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get('results'), list):
            raise RuntimeError(f'Malformed direct-upload response from {path}')
        return body

    def _upload_presigned_post(self, upload, staged_path, original_filename, content_type):
        """Send one staged image directly to S3 without API credentials."""
        if not isinstance(upload, dict):
            raise RuntimeError('Direct-upload grant is missing its S3 POST details.')
        url = upload.get('url')
        fields = upload.get('fields')
        if not isinstance(url, str) or not isinstance(fields, dict):
            raise RuntimeError('Direct-upload grant is malformed.')
        last_error = None
        for attempt in range(1, 4):
            try:
                with open(staged_path, 'rb') as image_file:
                    response = requests.post(
                        url,
                        data=fields,
                        files={'file': (original_filename, image_file, content_type)},
                        verify=getattr(self, 'verify', True),
                        timeout=(30, 300),
                    )
                response.raise_for_status()
                return
            except requests.RequestException as error:
                last_error = error
                if attempt < 3:
                    logger.warning(
                        'Direct S3 upload of %s failed (attempt %d): %s. Retrying.',
                        original_filename, attempt, error,
                    )
                    time.sleep(attempt)
        raise RuntimeError(f'Direct S3 upload failed after 3 attempts: {last_error}')

    def post_images_direct(self, items, progress_callback=None, retry_interval=15):
        """Stage, authorize, upload, and finalize a direct-to-S3 image batch."""
        if len(items) > self.IMAGE_BATCH_SIZE:
            raise ValueError(
                f'At most {self.IMAGE_BATCH_SIZE} images may be submitted.'
            )
        known_size = sum(item.get('source_size', 0) or 0 for item in items)
        with tempfile.TemporaryDirectory(prefix='rbg-direct-image-batch-') as staging_dir:
            available_space = shutil.disk_usage(staging_dir).free
            # One input may coexist briefly with its newly encoded replacement.
            required_space = known_size + self.MAX_IMAGE_UPLOAD_BYTES
            if required_space > available_space:
                raise RuntimeError(
                    f'Image batch needs {required_space} bytes of temporary storage, but '
                    f'only {available_space} bytes are available in {staging_dir}.'
                )

            staged_items = []
            bytes_staged = 0
            for index, item in enumerate(items):
                source_path = item['file_path']
                staged_path = os.path.join(staging_dir, f'{index}.source')
                copied = self._copy_source_to_staging(
                    source_path, staged_path, progress_callback, index, bytes_staged,
                    retry_interval,
                )
                if progress_callback:
                    progress_callback(
                        phase='preparing image', current_file=source_path,
                        files_completed=index, bytes_staged=bytes_staged + copied,
                    )
                output_format = self._reduce_staged_image(
                    staged_path, Path(source_path).name,
                )
                bytes_staged += os.path.getsize(staged_path)
                metadata = self._staged_image_metadata(staged_path)
                upload_filename = self._upload_filename(
                    Path(source_path).name, output_format,
                )
                if upload_filename != Path(source_path).name:
                    logger.info(
                        'Uploading %s as %s to satisfy the 100-character filename limit.',
                        Path(source_path).name, upload_filename,
                    )
                staged_item = {
                    **item,
                    **metadata,
                    'original_filename': upload_filename,
                    'staged_path': staged_path,
                }
                staged_item['client_id'] = self._direct_client_id(
                    staged_item, metadata['sha256']
                )
                staged_items.append(staged_item)
                if progress_callback:
                    progress_callback(
                        phase='staging', current_file=source_path,
                        files_completed=index + 1, bytes_staged=bytes_staged,
                    )

            if progress_callback:
                progress_callback(
                    phase='requesting S3 upload grants', current_file=None,
                    files_completed=len(items), bytes_staged=bytes_staged,
                )
            initiation = self._post_direct_api(
                '/plants/api/images/direct-upload/initiate/',
                {'uploads': [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {'file_path', 'source_size', 'staged_path'}
                    }
                    for item in staged_items
                ]},
            )
            items_by_client_id = {item['client_id']: item for item in staged_items}
            finalization_items = []
            outcomes = []
            for result in initiation['results']:
                client_id = result.get('client_id') if isinstance(result, dict) else ''
                item = items_by_client_id.get(client_id)
                if not isinstance(result, dict) or item is None:
                    outcomes.append({
                        'client_id': client_id, 'status': 'error',
                        'code': 'invalid_initiate_response',
                        'detail': 'The direct-upload initiation response could not be matched.',
                    })
                    continue
                if result.get('status') != 'success':
                    if result.get('code') == 'already_finalized':
                        outcomes.append({
                            'client_id': client_id, 'status': 'success',
                            'created': False, 'finalized': True,
                        })
                    else:
                        outcomes.append(result)
                    continue
                try:
                    if progress_callback:
                        progress_callback(
                            phase='uploading directly to S3',
                            current_file=item['file_path'], files_completed=len(items),
                            bytes_staged=bytes_staged,
                        )
                    self._upload_presigned_post(
                        result.get('upload'), item['staged_path'],
                        item['original_filename'], item['content_type'],
                    )
                except (OSError, RuntimeError) as error:
                    outcomes.append({
                        'client_id': client_id, 'status': 'error',
                        'code': 's3_upload_failed', 'detail': str(error),
                    })
                    continue
                finalization_items.append({'pending_id': result['pending_id']})

            if not finalization_items:
                return {'results': outcomes}
            if progress_callback:
                progress_callback(
                    phase='finalizing direct uploads', current_file=None,
                    files_completed=len(items), bytes_staged=bytes_staged,
                )
            finalization = self._post_direct_api(
                '/plants/api/images/direct-upload/finalize/',
                {'uploads': finalization_items},
            )
            outcomes.extend(finalization['results'])
            return {'results': outcomes}

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
                                          'image': f})
                    r.raise_for_status()
                    return r
                except OSError as e:
                    logger.error(
                        f'[{target}] Error ({e}) posting: {file_path}')
        except FileNotFoundError:
            logger.error(f'[{target}] Unable to find file: {file_path}')
