import csv
import json
import logging
import math
import os
import sys
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path, PureWindowsPath

logger = logging.getLogger(__name__)

COLLECTION_REQUIRED_COLUMNS = frozenset({
    'familyname', 'vernacularfamilyname', 'genusname', 'speciesname', 'calcfullname',
    'subspecies', 'variety', 'subvariety', 'forma', 'subforma', 'cultivar',
    'vernacularname', 'habit', 'hardinesscode', 'waterregime', 'exposure',
    'plantsize', 'colour', 'labelrangetext', 'gardenlocalityarea',
    'gardenlocalityname', 'gardenlocalitycode', 'plantid', 'latitude', 'longitude',
    'commemorationcategory', 'commemorationperson', 'plantday', 'plantmonth',
    'plantyear', 'notonline', 'lastmodifiedon', 'str05', 'str12', 'str18',
    'str19', 'str20', 'str22', 'str23', 'str42',
})

IMAGE_COLUMNS = (
    'imagefile', 'copyright', 'directoryname', 'plantid', 'genusname',
    'speciesname', 'subspecies', 'variety', 'subvariety', 'forma',
    'subforma', 'cultivar', 'lastmodifiedon',
)
IMAGE_REQUIRED_ROW_FIELDS = frozenset({'imagefile', 'directoryname', 'lastmodifiedon'})
WINDOWS_PHOTO_DRIVE = PureWindowsPath(r'B:\\')
WINDOWS_RDP_PHOTO_ROOT = PureWindowsPath(r'\\tsclient\AA BRAHMS Resized Photos')


def process_bloom_time(bloom_time_string: str) -> list[str]:
    """Convert a whitespace-delimited BRAHMS bloom-time value to month labels."""
    words = bloom_time_string.split()
    months = []
    index = 0
    while index < len(words):
        word = words[index].title()
        if word in {'Early', 'Mid', 'Late'} and index + 1 < len(words):
            months.append(f'{word} {words[index + 1].title()}')
            index += 2
        else:
            months.append(word)
            index += 1
    return months


def process_plant_date(day: str, month: str, year: str, plant_id: str | None = None) -> str | None:
    """Return an ISO date only when the three BRAHMS date components are valid."""
    try:
        return date(year=int(year), month=int(month), day=int(day)).isoformat()
    except (TypeError, ValueError):
        logger.warning('[%s] Invalid planting date: %r-%r-%r',
                       plant_id, year, month, day)
        return None


def process_hardiness(hardiness_data: str) -> list[int]:
    """Convert a comma-delimited hardiness-zone field to integers."""
    return [int(element.strip()) for element in hardiness_data.split(',') if element.strip()]


def parse_last_modified(value: str) -> datetime | None:
    """Parse the ISO and legacy timestamp forms emitted by BRAHMS."""
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, '%m/%d/%Y %I:%M:%S %p')
        except ValueError:
            return None


def _field(row: Mapping[str, str], name: str) -> str:
    return (row.get(name) or '').strip()


def _parse_coordinate(value: str, field_name: str, plant_id: str) -> float | None:
    if not value:
        return None
    try:
        coordinate = float(value)
    except ValueError:
        logger.error('[%s] Invalid %s coordinate: %r',
                     plant_id, field_name, value)
        return None
    limit = 90 if field_name == 'latitude' else 180
    if not math.isfinite(coordinate) or not -limit <= coordinate <= limit:
        logger.error('[%s] Out-of-range %s coordinate: %r',
                     plant_id, field_name, value)
        return None
    return round(coordinate, 6)


def _is_true(value: str, accepted: set[str]) -> bool:
    return value.casefold() in accepted


def brahms_row_to_payload(row: Mapping[str, str]) -> dict | None:
    """Convert a header-mapped living-collections record to an API payload."""
    missing_columns = COLLECTION_REQUIRED_COLUMNS.difference(row)
    if missing_columns:
        logger.error('Collection export is missing required columns: %s',
                     ', '.join(sorted(missing_columns)))
        return None
    plant_id = _field(row, 'plantid')
    if not plant_id:
        logger.error('Skipping collection record without a plantid')
        return None
    try:
        hardiness = process_hardiness(_field(row, 'hardinesscode'))
    except ValueError:
        logger.error('[%s] Invalid hardiness value: %r',
                     plant_id, _field(row, 'hardinesscode'))
        return None
    latitude_text, longitude_text = _field(
        row, 'latitude'), _field(row, 'longitude')
    latitude = _parse_coordinate(latitude_text, 'latitude', plant_id)
    longitude = _parse_coordinate(longitude_text, 'longitude', plant_id)
    if bool(latitude_text) != bool(longitude_text):
        logger.error(
            '[%s] Latitude and longitude must be supplied together', plant_id)
        return None
    if (latitude_text and latitude is None) or (longitude_text and longitude is None):
        return None
    day, month, year = _field(row, 'plantday'), _field(
        row, 'plantmonth'), _field(row, 'plantyear')
    plant_date = process_plant_date(
        day, month, year, plant_id) if day and month and year else None
    if (day or month or year) and plant_date is None:
        return None
    return {
        'species': {
            'genus': {'family': {'name': _field(row, 'familyname'),
                                 'vernacular_name': _field(row, 'vernacularfamilyname')},
                      'name': _field(row, 'genusname')},
            'name': _field(row, 'speciesname'), 'full_name': _field(row, 'calcfullname'),
            'subspecies': _field(row, 'subspecies'), 'variety': _field(row, 'variety'),
            'subvariety': _field(row, 'subvariety'), 'forma': _field(row, 'forma'),
            'subforma': _field(row, 'subforma'), 'cultivar': _field(row, 'cultivar'),
            'vernacular_name': _field(row, 'vernacularname'), 'habit': _field(row, 'habit'),
            'hardiness': hardiness, 'water_regime': _field(row, 'waterregime'),
            'exposure': _field(row, 'exposure'), 'bloom_time': process_bloom_time(_field(row, 'str05')),
            'plant_size': _field(row, 'plantsize'), 'flower_color': _field(row, 'colour'),
            'label_range_text': _field(row, 'labelrangetext'),
            'utah_native': _is_true(_field(row, 'str12'), {'yes', 'x', 'utah native'}),
            'plant_select': _is_true(_field(row, 'str18'), {'yes', 'x'}),
            'deer_resist': _is_true(_field(row, 'str19'), {'yes', 'x'}),
            'rabbit_resist': _is_true(_field(row, 'str20'), {'yes', 'x'}),
            'bee_friend': _is_true(_field(row, 'str22'), {'yes', 'x'}),
            'high_elevation': _is_true(_field(row, 'str23'), {'yes', 'x'}),
            'arborist_rec': _is_true(_field(row, 'str42'), {'yes', 'x'}),
        },
        'garden': {'area': _field(row, 'gardenlocalityarea'), 'name': _field(row, 'gardenlocalityname'),
                   'code': _field(row, 'gardenlocalitycode')},
        'location': {'latitude': latitude, 'longitude': longitude}, 'plant_date': plant_date,
        'plant_id': plant_id, 'commemoration_category': _field(row, 'commemorationcategory'),
        'commemoration_person': _field(row, 'commemorationperson'),
        'commemoration_text': _field(row, 'commemorationtext'),
    }


def _mac_photo_library_root() -> Path:
    return Path(os.path.expanduser('~'), 'Library', 'CloudStorage', 'Box-Box', 'RBG-Shared',
                'Photo Library - Plant Records', 'AA BRAHMS Resized Photos')


def _photo_directory_parts(directory_name: str) -> tuple[str, ...]:
    """Map supported Windows photo-library roots to safe relative path parts."""
    windows_directory = PureWindowsPath(directory_name)
    for root in (WINDOWS_PHOTO_DRIVE, WINDOWS_RDP_PHOTO_ROOT):
        try:
            relative_directory = windows_directory.relative_to(root)
        except ValueError:
            continue
        if any(part in {'', '.', '..'} for part in relative_directory.parts):
            break
        return relative_directory.parts
    raise ValueError(f'Unsupported or unsafe image directory: {directory_name!r}')


def construct_img_filepath(row: Mapping[str, str]) -> str:
    """Resolve a header-mapped image row to its local source file path."""
    image_file = _field(row, 'imagefile').replace('\ufeff', '')
    directory_name = _field(row, 'directoryname')
    if not image_file or not directory_name:
        raise ValueError('Image row requires imagefile and directoryname')
    windows_image_name = PureWindowsPath(image_file)
    if len(windows_image_name.parts) != 1 or windows_image_name.name != image_file:
        raise ValueError(f'Unsafe image filename: {image_file!r}')
    if sys.platform != 'darwin':
        return os.path.join(directory_name, image_file)

    source_root = _mac_photo_library_root()
    candidate = source_root.joinpath(*_photo_directory_parts(directory_name), image_file)
    resolved_root = source_root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError(f'Image path escapes photo library: {directory_name!r}')
    return str(resolved_candidate)


def convert_to_json(dictionary: dict) -> str:
    return json.dumps(dictionary)


def extract_species_info(row: Mapping[str, str]) -> dict[str, str]:
    return {
        'genus': _field(row, 'genusname'), 'name': _field(row, 'speciesname'),
        'subspecies': _field(row, 'subspecies'), 'variety': _field(row, 'variety'),
        'subvariety': _field(row, 'subvariety'), 'forma': _field(row, 'forma'),
        'subforma': _field(row, 'subforma'), 'cultivar': _field(row, 'cultivar'),
    }


def extract_species_query(row: Mapping[str, str]) -> dict[str, str] | None:
    """Build the taxonomy lookup query for image rows without a plant ID."""
    species_query = extract_species_info(row)
    if not species_query['genus']:
        logger.error('Skipping image %r without plantid or genusname', _field(row, 'imagefile'))
        return None
    return species_query


def extract_copyright_info(row: Mapping[str, str]) -> str:
    return _field(row, 'copyright')


class BRAHMSExportReader:
    """Read standard exports and reconstruct malformed living-collections exports."""

    def __init__(self, file_path: str, encoding: str = 'utf-8', delimiter: str = ','):
        self.file_path, self.encoding, self.delimiter = file_path, encoding, delimiter

    def get_rows(self) -> Iterator[list[str]]:
        """Yield ordinary CSV rows; retained for species-image imports."""
        with open(self.file_path, encoding=self.encoding, newline='') as csvfile:
            yield from csv.reader(csvfile, delimiter=self.delimiter)

    def get_image_rows(self) -> Iterator[dict[str, str]]:
        """Yield validated 13-column image-export rows mapped by header name."""
        with open(self.file_path, encoding=self.encoding, newline='') as csvfile:
            reader = csv.DictReader(csvfile, delimiter=self.delimiter, restkey='_extra_columns')
            if reader.fieldnames != list(IMAGE_COLUMNS):
                logger.error('Invalid image export header in %s: expected %s, received %s',
                             self.file_path, list(IMAGE_COLUMNS), reader.fieldnames)
                return
            for row in reader:
                line_number = reader.line_num
                if row.get('_extra_columns') is not None or any(row.get(field) is None for field in IMAGE_COLUMNS):
                    logger.error('Skipping malformed image export row at line %d', line_number)
                    continue
                cleaned_row = {field: row[field] for field in IMAGE_COLUMNS}
                missing = [field for field in IMAGE_REQUIRED_ROW_FIELDS if not _field(cleaned_row, field)]
                if missing:
                    logger.error('Skipping image export row at line %d: missing %s',
                                 line_number, ', '.join(sorted(missing)))
                    continue
                yield cleaned_row

    def get_collection_rows(self) -> Iterator[dict[str, str]]:
        """Yield header-mapped records reconstructed to the exact header width."""
        with open(self.file_path, encoding=self.encoding, newline='') as export_file:
            header_line = export_file.readline()
            if not header_line:
                logger.error('Collection export is empty: %s', self.file_path)
                return
            header = self._split_collection_line(header_line)
            if not header or any(not name for name in header):
                logger.error(
                    'Invalid collection export header in %s', self.file_path)
                return
            expected_width = len(header)
            pending: tuple[int, list[str]] | None = None
            last_line = 1
            for line_number, raw_line in enumerate(export_file, start=2):
                last_line = line_number
                fields = self._split_collection_line(raw_line)
                if pending is None:
                    if len(fields) == expected_width:
                        yield dict(zip(header, fields, strict=True))
                    elif len(fields) < expected_width:
                        pending = (line_number, fields)
                    else:
                        self._report_bad_record(
                            line_number, line_number, len(fields), expected_width)
                    continue
                start_line, accumulated = pending
                if len(fields) == expected_width:
                    self._report_bad_record(
                        start_line, line_number - 1, len(accumulated), expected_width)
                    pending = None
                    yield dict(zip(header, fields, strict=True))
                    continue
                accumulated[-1] = f'{accumulated[-1]}\n{fields[0]}'
                accumulated.extend(fields[1:])
                if len(accumulated) == expected_width:
                    yield dict(zip(header, accumulated, strict=True))
                    pending = None
                elif len(accumulated) > expected_width:
                    self._report_bad_record(
                        start_line, line_number, len(accumulated), expected_width)
                    pending = None
            if pending is not None:
                start_line, accumulated = pending
                self._report_bad_record(
                    start_line, last_line, len(accumulated), expected_width)

    def _split_collection_line(self, raw_line: str) -> list[str]:
        line = raw_line.rstrip('\r\n')
        if line.startswith('"'):
            line = line[1:]
        if line.endswith('"'):
            line = line[:-1]
        return [field.replace('""', '"') for field in line.split(self.delimiter)]

    @staticmethod
    def _report_bad_record(start_line: int, end_line: int, actual_width: int, expected_width: int) -> None:
        logger.error('Skipping malformed collection record at physical lines %d-%d: %d columns, expected %d',
                     start_line, end_line, actual_width, expected_width)
