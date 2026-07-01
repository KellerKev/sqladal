"""Field filter_in / filter_out — transform on the way to/from the DB (the hook
guardsaw uses for transparent field encryption)."""
import base64

from sqladal import DAL, Field


def _enc(v):
    return "ENC:" + base64.b64encode(str(v).encode()).decode()


def _dec(v):
    if isinstance(v, str) and v.startswith("ENC:"):
        return base64.b64decode(v[4:]).decode()
    return v


def test_filter_in_out_roundtrip_and_at_rest():
    db = DAL("sqlite://:memory:")
    db.define_table("secret",
                    Field("name"),
                    Field("data", filter_in=_enc, filter_out=_dec))
    db.secret.insert(name="a", data="topsecret")
    db.commit()

    # reading through sqladal applies filter_out -> plaintext
    row = db(db.secret).select().first()
    assert row.data == "topsecret"

    # but the value stored at rest is the filter_in (encrypted) form
    raw = db.executesql("SELECT data FROM secret")[0][0]
    assert raw.startswith("ENC:") and raw != "topsecret"
    assert _dec(raw) == "topsecret"
    db.close()


def test_filter_in_applies_on_update():
    db = DAL("sqlite://:memory:")
    db.define_table("secret", Field("name"), Field("data", filter_in=_enc, filter_out=_dec))
    pid = db.secret.insert(name="a", data="one")
    db(db.secret.id == pid).update(data="two")
    db.commit()
    assert db.secret(pid).data == "two"                      # decrypted on read
    assert db.executesql("SELECT data FROM secret")[0][0].startswith("ENC:")
    db.close()


def test_no_filter_is_passthrough():
    db = DAL("sqlite://:memory:")
    db.define_table("plain", Field("data"))
    db.plain.insert(data="hello")
    db.commit()
    assert db(db.plain).select().first().data == "hello"
    assert db.executesql("SELECT data FROM plain")[0][0] == "hello"
    db.close()
