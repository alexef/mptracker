import sys
import os
import logging
import uuid
import argparse
from datetime import datetime
import flask
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.script import Manager
from flask.ext.login import UserMixin
from path import path
from mptracker.common import parse_date, TablePatcher, temp_dir


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def uuid_type():
    return db.CHAR(32)


def uuid_column():
    return db.Column(uuid_type(), primary_key=True,
                     default=lambda: str(uuid.uuid4()))


def identity(v):
    return v


db = SQLAlchemy()


class Person(db.Model):
    id = uuid_column()
    name = db.Column(db.String)
    cdep_id = db.Column(db.Integer)

    county_id = db.Column(uuid_type(), db.ForeignKey('county.id'))
    county = db.relationship('County',
        backref=db.backref('people', lazy='dynamic'))

    def __str__(self):
        return "{p.name}".format(p=self)

    def __repr__(self):
        return "<%s>" % self

    @classmethod
    def get_or_create_non_mp(cls, name):
        for row in cls.query.filter_by(name=name, cdep_id=None):
            return row
        else:
            logger.info('Creating non-MP %s %r', cls.__name__, name)
            row = cls(name=name)
            db.session.add(row)
            db.session.flush()
            return row


class County(db.Model):
    id = uuid_column()
    name = db.Column(db.String)
    geonames_code = db.Column(db.Integer)

    def __str__(self):
        return self.name

    def __repr__(self):
        return "<%s>" % self


class StenoChapter(db.Model):
    id = uuid_column()
    date = db.Column(db.Date, index=True)
    headline = db.Column(db.String)
    serial = db.Column(db.String, index=True)

    @property
    def serial_number(self):
        return self.serial.split('/', 1)[1]


class StenoParagraph(db.Model):
    id = uuid_column()
    text = db.Column(db.Text)
    serial = db.Column(db.String, index=True)

    chapter_id = db.Column(uuid_type(), db.ForeignKey('steno_chapter.id'))
    chapter = db.relationship('StenoChapter',
        backref=db.backref('paragraphs', lazy='dynamic'))

    person_id = db.Column(uuid_type(), db.ForeignKey('person.id'))
    person = db.relationship('Person',
        backref=db.backref('steno_paragraphs', lazy='dynamic'))


class Question(db.Model):
    id = uuid_column()
    number = db.Column(db.String)
    date = db.Column(db.Date, index=True)
    title = db.Column(db.String)
    url = db.Column(db.String)
    pdf_url = db.Column(db.String)
    type = db.Column(db.String)
    addressee = db.Column(db.String)
    text = db.Column(db.Text)
    match_data = db.Column(db.Text)
    match_score = db.Column(db.Float)

    person_id = db.Column(uuid_type(), db.ForeignKey('person.id'))
    person = db.relationship('Person',
        backref=db.backref('questions', lazy='dynamic'))

    def __str__(self):
        return "{q.number}/{q.date}".format(q=self)

    def __repr__(self):
        return "<%s>" % self


class User(db.Model, UserMixin):
    id = uuid_column()
    email = db.Column(db.String)

    @classmethod
    def get_or_create(cls, email, autosave=True):
        for row in cls.query.filter_by(email=email):
            return row
        else:
            logger.info('Creating %s %r', cls.__name__, email)
            row = cls(email=email)
            if autosave:
                db.session.add(row)
                db.session.commit()
            return row


class PersonMatcher:
    """ Find the right person based on name and cdep_id """

    def __init__(self):
        self.cdep_person = {p.cdep_id: p for p in Person.query}

    def name_bits(self, name):
        return set(name.replace('-', ' ').split())

    def get_person(self, name, cdep_id, strict=False):
        if cdep_id is not None:
            person = self.cdep_person[cdep_id]
            if self.name_bits(person.name) == self.name_bits(name):
                return person
        if strict:
            raise RuntimeError("Could not find a match for %r, %r" %
                               (name, cdep_id))
        return Person.get_or_create_non_mp(name)


db_manager = Manager()


@db_manager.command
def sync():
    db.create_all()


@db_manager.option('alembic_args', nargs=argparse.REMAINDER)
def alembic(alembic_args):
    from alembic.config import CommandLine
    CommandLine().main(argv=alembic_args)


@db_manager.command
def upgrade(revision='head'):
    return alembic(['upgrade', revision])


@db_manager.command
def revision(message=None):
    if message is None:
        message = input('revision name: ')
    return alembic(['revision', '--autogenerate', '-m', message])


@db_manager.option('names', nargs='+')
def drop(names):
    engine = db.get_engine(flask.current_app)
    for name in names:
        table = db.metadata.tables[name]
        print('dropping', name)
        table.drop(engine, checkfirst=True)


def get_model_map():
    reg = db.Model._decl_class_registry
    models = [reg[k] for k in reg if not k.startswith('_')]
    return {m.__tablename__: m for m in models}


class TableLoader:

    def __init__(self, name):
        self.table_name = name
        self.model = get_model_map()[name]
        self.columns = []
        self.encoder = {}
        self.decoder = {}
        for col in self.model.__table__._columns:
            self.columns.append(col.name)
            if isinstance(col.type, db.Date):
                self.encoder[col.name] = lambda v: v.isoformat()
                self.decoder[col.name] = parse_date
            else:
                self.encoder[col.name] = self.decoder[col.name] = identity

    def to_dict(self, row, columns=None):
        if columns is None:
            columns = self.columns
        return {col: self.encoder[col](getattr(row, col)) for col in columns}

    def decode_dict(self, encoded_row):
        return {col: self.decoder[col](encoded_row[col])
                for col in encoded_row}


@db_manager.command
def dump(name, columns=None, number=None, filter=None, _file=sys.stdout):
    if columns:
        columns = columns.split(',')
    loader = TableLoader(name)
    count = 0
    query = loader.model.query

    if filter:
        for filter_spec in filter.split(','):
            if '=' in filter_spec:
                name, value = filter_spec.split('=', 1)
                query = query.filter(getattr(loader.model, name) == value)
            else:
                name = filter_spec
                query = query.filter(getattr(loader.model, name) != None)

    for row in query.order_by('id'):
        flask.json.dump(loader.to_dict(row, columns), _file, sort_keys=True)
        _file.write('\n')
        count += 1
        if number is not None:
            if count >= int(number):
                break

    return count


@db_manager.command
def load(name, update_only=False):
    loader = TableLoader(name)
    patcher = TablePatcher(loader.model, db.session, key_columns=['id'])
    records = (loader.decode_dict(flask.json.loads(line))
               for line in sys.stdin)
    patcher.update(records, create=not update_only)


def create_backup(backup_path):
    import zipfile
    model_map = get_model_map()
    with temp_dir() as tmp:
        zip_path = tmp / 'dump.zip'
        zip_archive = zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED)
        for name in model_map:
            print(name, end=' ... ')
            file_name = '%s.json' % name
            file_path = tmp / file_name
            with open(file_path, 'w', encoding='utf-8') as table_fd:
                count = dump(name, _file=table_fd)
            print(count, 'rows')
            zip_archive.write(file_path, file_name)
            file_path.unlink()
        zip_archive.close()
        zip_path.rename(backup_path)


@db_manager.command
def backup():
    backup_dir = path(os.environ['BACKUP_DIR'])
    backup_name = datetime.utcnow().strftime('backup-%Y-%m-%d-%H%M%S.zip')
    backup_path = backup_dir / backup_name
    create_backup(backup_path)
    print("Backup at %s (%d bytes)" % (backup_path, backup_path.size))
