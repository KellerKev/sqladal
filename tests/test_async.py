"""Native async API (AsyncDAL) on aiosqlite, sharing the sync core's builders.

The async connection is per-context (per asyncio task), so each concurrent
request draws its own connection from the pool. Scope it with
``async with adb.connection(): ...`` (acquire -> commit/rollback -> release)."""
import asyncio

import pytest

from sqladal import AsyncDAL, Field


@pytest.fixture
async def adb(tmp_path):
    # file-based so DDL + queries share one database (aiosqlite :memory: would not)
    d = AsyncDAL("sqlite://app.db", folder=str(tmp_path))
    d.define_table("author", Field("name"))
    d.define_table(
        "post",
        Field("title"),
        Field("views", "integer", default=0),
        Field("author", "reference author"),
    )
    await d.migrate()
    yield d
    await d.dispose()


async def test_async_insert_select(adb):
    async with adb.connection():
        a = await adb.author.insert(name="Ada")
        assert a == 1
        await adb.post.insert(title="A", views=5, author=a)
        await adb.post.insert(title="B", views=2, author=a)

        rows = await adb(adb.post.views > 3).select()
        assert len(rows) == 1
        assert rows.first().title == "A"

        rows = await adb(adb.post).select(orderby=~adb.post.views)
        assert [r.title for r in rows] == ["A", "B"]


async def test_async_default_and_count(adb):
    async with adb.connection():
        a = await adb.author.insert(name="Ada")
        await adb.post.insert(title="A", author=a)
        row = (await adb(adb.post.id == 1).select()).first()
        assert row.views == 0
        assert await adb(adb.post).count() == 1


async def test_async_update_delete(adb):
    async with adb.connection():
        a = await adb.author.insert(name="Ada")
        await adb.post.insert(title="A", author=a)
        await adb.post.insert(title="B", author=a)

        n = await adb(adb.post.title == "A").update(title="A2")
        assert n == 1
        assert await adb(adb.post.title == "A2").count() == 1

        n = await adb(adb.post.title == "B").delete()
        assert n == 1
        assert await adb(adb.post).count() == 1


async def test_async_explicit_reference_fetch(adb):
    async with adb.connection():
        a = await adb.author.insert(name="Ada")
        await adb.post.insert(title="A", author=a)
        post = (await adb(adb.post.id == 1).select()).first()
        author = await adb.fetch(adb.author, post.author)
        assert author.name == "Ada"


async def test_async_rollback_on_error(adb):
    with pytest.raises(RuntimeError):
        async with adb.connection():
            await adb.author.insert(name="Temp")
            raise RuntimeError("boom")  # connection() rolls back + releases
    async with adb.connection():
        assert await adb(adb.author).count() == 0


async def test_manual_commit_close(adb):
    # the explicit API still works alongside the context manager
    await adb._connection_async()
    await adb.author.insert(name="Grace")
    await adb.commit()
    await adb.close()
    async with adb.connection():
        assert await adb(adb.author.name == "Grace").count() == 1


async def test_per_request_distinct_connections(adb):
    # seed 20 posts
    async with adb.connection():
        a = await adb.author.insert(name="Ada")
        for i in range(20):
            await adb.post.insert(title="t%d" % i, views=i, author=a)

    conn_ids = []

    async def worker(threshold):
        async with adb.connection():
            conn_ids.append(id(adb._aconn_var.get()))
            await asyncio.sleep(0.01)  # hold the connection across an await point
            return await adb(adb.post.views >= threshold).count()

    thresholds = list(range(0, 20, 2))  # 10 concurrent tasks
    results = await asyncio.gather(*[worker(t) for t in thresholds])

    # correct, isolated results
    assert results == [20 - t for t in thresholds]
    # the enhancement: each concurrent task used its OWN connection (not shared)
    assert len(set(conn_ids)) == len(thresholds)
