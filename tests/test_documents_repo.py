"""Document repository + model tests (in-memory SQLite)."""

import pytest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Document, DocumentType
from app.db.repo import store_document, get_document, list_documents, delete_document


async def _make_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_store_and_get_document():
    factory = await _make_factory()
    async with factory() as session:
        doc = await store_document(
            session,
            filename="abc.pdf",
            original_filename="laporan.pdf",
            file_path="/tmp/abc.pdf",
            document_type="pdf",
            file_size=1234,
            text_content="Isi dokumen.",
            metadata={"pages": 2},
        )
        await session.commit()
        doc_id = doc.id

        fetched = await get_document(session, doc_id)
        assert fetched is not None
        assert fetched.document_type == DocumentType.pdf
        assert fetched.original_filename == "laporan.pdf"
        assert fetched.text_content == "Isi dokumen."


async def test_store_unknown_type_falls_back_other():
    factory = await _make_factory()
    async with factory() as session:
        doc = await store_document(
            session,
            filename="x.bin",
            original_filename="x.bin",
            file_path="/tmp/x.bin",
            document_type="other",
            file_size=10,
        )
        assert doc.document_type == DocumentType.other


async def test_list_documents_filtered():
    factory = await _make_factory()
    async with factory() as session:
        await store_document(
            session,
            filename="a.pdf",
            original_filename="a.pdf",
            file_path="/tmp/a.pdf",
            document_type="pdf",
            file_size=1,
        )
        await store_document(
            session,
            filename="b.txt",
            original_filename="b.txt",
            file_path="/tmp/b.txt",
            document_type="txt",
            file_size=2,
        )
        all_docs = await list_documents(session)
        assert len(all_docs) == 2

        pdfs = await list_documents(session, document_type="pdf")
        assert len(pdfs) == 1
        assert pdfs[0].filename == "a.pdf"


async def test_delete_document():
    factory = await _make_factory()
    async with factory() as session:
        doc = await store_document(
            session,
            filename="z.txt",
            original_filename="z.txt",
            file_path="/tmp/z.txt",
            document_type="txt",
            file_size=3,
        )
        doc_id = doc.id
        removed = await delete_document(session, doc_id)
        assert removed is not None
        assert await get_document(session, doc_id) is None


async def test_delete_missing_document():
    factory = await _make_factory()
    async with factory() as session:
        assert await delete_document(session, 999) is None