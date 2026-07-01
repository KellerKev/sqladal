"""Field services: represent, formatter, and pydal-compatible upload store/retrieve."""
import io

import pytest

from sqladal import DAL, Field


@pytest.fixture
def db(tmp_path):
    d = DAL("sqlite://storage.db", folder=str(tmp_path))
    yield d
    d.close()


def test_represent_value(db):
    db.define_table(
        "p",
        Field("name"),
        Field("price", "integer", represent=lambda v, row: "$%d" % v),
    )
    db.p.insert(name="a", price=5)
    row = db(db.p).select().first()
    f = db.p.price
    assert f.represent_value(row.price, row) == "$5"
    # field without represent returns value unchanged
    assert db.p.name.represent_value("x") == "x"


def test_upload_store_retrieve_roundtrip(db, tmp_path):
    updir = str(tmp_path / "up")
    db.define_table("doc", Field("title"), Field("file", "upload", uploadfolder=updir))
    stored = db.doc.file.store(io.BytesIO(b"hello bytes"), filename="my report.txt")
    # encodes table.field.uuid16.b64name.ext
    assert stored.startswith("doc.file.")
    assert stored.endswith(".txt")

    db.doc.insert(title="t", file=stored)
    row = db(db.doc).select().first()
    assert row.file == stored

    # retrieve gives back the original filename and content
    original_name, stream = db.doc.file.retrieve(stored, path=updir)
    assert original_name == "my report.txt"
    assert stream.read() == b"hello bytes"
    stream.close()

    name_only, fullpath = db.doc.file.retrieve(stored, path=updir, nameonly=True)
    assert name_only == "my report.txt"
    assert fullpath.endswith(stored)


def test_upload_uses_field_uploadfolder(db, tmp_path):
    updir = str(tmp_path / "imgs")
    db.define_table("pic", Field("img", "upload", uploadfolder=updir))
    stored = db.pic.img.store(io.BytesIO(b"x"), filename="a.png")
    props = db.pic.img.retrieve_file_properties(stored)
    assert props["filename"] == "a.png"
    assert props["path"] == updir
