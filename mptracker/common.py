from datetime import datetime
from contextlib import contextmanager
from collections import namedtuple
import logging
import tempfile
from path import path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def parse_date(date_str):
    return datetime.strptime(date_str, '%Y-%m-%d').date()


def fix_local_chars(txt):
    return (txt.replace("ş", "ș").replace("Ş", "Ș")
               .replace("ţ", "ț").replace("Ţ", "Ț"))


@contextmanager
def temp_dir():
    tmp = path(tempfile.mkdtemp())
    try:
        yield tmp
    finally:
        tmp.rmtree()


class RowNotFound(Exception):
    """ Could not find row to match key. """


AddResult = namedtuple('AddResult', ['row', 'is_new', 'is_changed'])


class TablePatcher:

    def __init__(self, model, session, key_columns):
        self.model = model
        self.session = session
        self.key_columns = key_columns
        self.existing = {self.row_key(row): row
                         for row in self.model.query}

    def row_key(self, row):
        return tuple(getattr(row, k) for k in self.key_columns)

    def dict_key(self, record):
        return tuple(record.get(k) for k in self.key_columns)

    def add(self, record, create=True):
        key = self.dict_key(record)
        row = self.existing.get(key)
        is_new = is_changed = False

        if row is None:
            if create:
                row = self.model()
                logger.info("Adding %r", key)
                is_new = is_changed = True
                self.session.add(row)
                self.existing[key] = row

            else:
                raise RowNotFound("Could not find row with key=%r" % key)

        else:
            for k in record:
                if getattr(row, k) != record[k]:
                    logger.info("Updating %r", key)
                    is_changed = True
                    break

            else:
                logger.info("Not touching %r", key)

        if is_changed:
            for k in record:
                setattr(row, k, record[k])

        return AddResult(row, is_new, is_changed)

    @contextmanager
    def process(self, autoflush=None):
        counters = {'n_add': 0, 'n_update': 0, 'n_ok': 0, 'total': 0}

        def add(record, create=True):
            result = self.add(record, create=create)

            counters['total'] += 0
            if autoflush and counters['total'] % autoflush == 0:
                self.session.flush()

            if result.is_new:
                counters['n_add'] += 1

            elif result.is_changed:
                counters['n_update'] += 1

            else:
                counters['n_ok'] += 1

            return result

        yield add

        self.session.commit()
        logger.info("Created %d, updated %d, found ok %d.",
                    counters['n_add'], counters['n_update'], counters['n_ok'])

    def update(self, data, create=True):
        with self.process(autoflush=1000) as add:
            for record in data:
                add(record, create=create)
