"""
Medical Knowledge Base
=====================

Medical-specific RAG implementation using ChromaDB.
Supports medical document types (drug, guideline, lab, surgery).

Architecture mirrors SimpleRAG but with independent collection.
"""

import json
import hashlib
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime

from microharness.rag.bm25 import BM25
from microharness.medical.doc_parser import detect_medical_type, parse_medical_doc


@dataclass
class Document:
    """A document in the knowledge base."""
    doc_id: str
    content: str
    filename: str
    created_at: str
    metadata: dict


class MedicalRAG:
    """
    Medical Knowledge Base RAG.

    Uses independent ChromaDB collection for medical documents.
    Supports filtering by medical_type (drug/guideline/lab/surgery).
    """

    COLLECTION_NAME = "medical_knowledge"
    INDEX_DIR = "medical_index"

    def __init__(self):
        self.index_dir = Path(self.INDEX_DIR)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.documents: List[Document] = []
        self._chunks: List[Document] = []
        self._embedding_model = None
        self._chroma_client = None
        self._collection = None
        self._bm25: Optional[BM25] = None
        self._chunk_to_doc: dict = {}

    def _get_embedding_model(self):
        """Lazy load embedding model."""
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            print("[MedicalRAG] Embedding model loaded: all-MiniLM-L6-v2")
        return self._embedding_model

    def _get_collection(self):
        """Lazy init ChromaDB collection."""
        if self._chroma_client is None:
            import chromadb
            from chromadb.config import Settings

            self._chroma_client = chromadb.PersistentClient(
                path=str(self.index_dir / "chroma_db"),
                settings=Settings(anonymized_telemetry=False)
            )
            self._collection = self._chroma_client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"}
            )
        return self._collection

    def _compute_hash(self, text: str) -> str:
        """Generate short hash for document ID."""
        return hashlib.md5(text.encode()).hexdigest()[:12]

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings."""
        model = self._get_embedding_model()
        embeddings = model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()

    def _get_bm25(self) -> BM25:
        """Get or create BM25 index."""
        if self._bm25 is None:
            self._bm25 = BM25()
        return self._bm25

    def _get_config(self):
        """Get chunk configuration."""
        from microharness.rag.rag_config import load_config
        return load_config()

    def add_document(self, content: str, filename: str, metadata: dict = None) -> str:
        """
        Add a medical document to the knowledge base.

        Args:
            content: Document text content
            filename: Original filename
            metadata: Additional metadata (medical_type auto-detected if not provided)

        Returns:
            doc_id of added document
        """
        doc_id = self._compute_hash(content + filename)

        # Check if already exists
        for doc in self.documents:
            if doc.doc_id == doc_id:
                return doc_id

        # Auto-detect medical type
        detected_type = detect_medical_type(content)
        meta = dict(metadata) if metadata else {}
        meta["medical_type"] = meta.get("medical_type", detected_type)

        doc = Document(
            doc_id=doc_id,
            content=content,
            filename=filename,
            created_at=datetime.now().isoformat(),
            metadata=meta,
        )
        self.documents.append(doc)

        # Chunk the document
        config = self._get_config()
        from microharness.rag.chunker import chunk_text
        chunks = chunk_text(
            content,
            config.chunk_mode,
            chunk_size=config.chunk_size,
            overlap=config.chunk_overlap
        )

        collection = self._get_collection()

        if len(chunks) > 1:
            self._chunk_to_doc[doc_id] = doc_id

            chunk_ids = []
            chunk_embeddings = []
            chunk_docs = []
            chunk_metadatas = []

            for i, chunk_content in enumerate(chunks):
                chunk_id = f"{doc_id}_chunk_{i}"
                chunk_ids.append(chunk_id)
                chunk_docs.append(chunk_content)
                chunk_metadatas.append({
                    "filename": filename,
                    "parent_id": doc_id,
                    "chunk_index": i,
                    "medical_type": meta["medical_type"]
                })
                self._chunk_to_doc[chunk_id] = doc_id

            if chunk_ids:
                chunk_embeddings = self._embed_texts(chunk_docs)
                collection.add(
                    ids=chunk_ids,
                    embeddings=chunk_embeddings,
                    documents=chunk_docs,
                    metadatas=chunk_metadatas
                )
        else:
            collection = self._get_collection()
            embedding = self._embed_texts([content])[0]
            collection.add(
                ids=doc_id,
                embeddings=embedding,
                documents=content,
                metadatas={"filename": filename, "parent_id": doc_id, "medical_type": meta["medical_type"]}
            )

        # Add to BM25
        self._get_bm25().add_documents([d.content for d in self.documents])

        return doc_id

    def add_document_from_file(self, filepath: str) -> str:
        """
        Add a medical document from file path.

        Args:
            filepath: Path to document file (PDF, MD, TXT, etc.)

        Returns:
            doc_id of added document
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        content, medical_type = parse_medical_doc(path.read_bytes(), path.name)
        return self.add_document(content, path.name, {"medical_type": medical_type})

    def similarity_search(
        self,
        query: str,
        top_k: int = 3,
        filter_type: str = None
    ) -> List[Document]:
        """
        Search medical documents.

        Args:
            query: Search query
            top_k: Number of results
            filter_type: Filter by medical_type (drug/guideline/lab/surgery)

        Returns:
            List of matching Documents
        """
        if not self.documents:
            return []

        collection = self._get_collection()
        query_embedding = self._embed_texts([query])[0]

        # Build where filter if filter_type specified
        where_filter = {"medical_type": filter_type} if filter_type else None

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k * 2, len(self.documents)),
            where=where_filter,
            include=["distances"]
        )

        matched_docs = []
        seen_parent = set()

        for doc_id in results["ids"][0]:
            parent_id = self._chunk_to_doc.get(doc_id, doc_id)
            if parent_id in seen_parent:
                continue
            seen_parent.add(parent_id)

            doc = next((d for d in self.documents if d.doc_id == parent_id), None)
            if doc and doc not in matched_docs:
                matched_docs.append(doc)
                if len(matched_docs) >= top_k:
                    break

        return matched_docs

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document and its chunks."""
        doc = next((d for d in self.documents if d.doc_id == doc_id), None)
        if not doc:
            return False

        self.documents.remove(doc)

        collection = self._get_collection()
        collection.delete(ids=[doc_id])

        # Delete chunks
        chunks_to_delete = [k for k, v in self._chunk_to_doc.items() if v == doc_id]
        for chunk_id in chunks_to_delete:
            collection.delete(ids=[chunk_id])
            self._chunk_to_doc.pop(chunk_id, None)

        self._chunk_to_doc.pop(doc_id, None)

        # Rebuild BM25
        self._bm25 = None
        if self.documents:
            self._get_bm25().add_documents([d.content for d in self.documents])

        return True

    def list_documents(self) -> List[dict]:
        """List all documents."""
        return [asdict(doc) for doc in self.documents]

    def save_index(self):
        """Persist index to disk."""
        index_file = self.index_dir / "index.json"
        data = {
            "documents": [asdict(doc) for doc in self.documents],
            "chunk_mapping": self._chunk_to_doc,
        }
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_index(self):
        """Load index from disk."""
        index_file = self.index_dir / "index.json"
        if not index_file.exists():
            return

        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.documents = [Document(**d) for d in data.get("documents", [])]
        self._chunk_to_doc = data.get("chunk_mapping", {})

        if self.documents:
            self._chroma_client = None
            self._collection = None
            collection = self._get_collection()

            ids = []
            embeddings = []
            documents = []
            metadatas = []

            for doc in self.documents:
                ids.append(doc.doc_id)
                documents.append(doc.content)
                metadatas.append({
                    "filename": doc.filename,
                    "parent_id": doc.doc_id,
                    "medical_type": doc.metadata.get("medical_type", "general")
                })

            if ids:
                embeddings = self._embed_texts(documents)
                collection.add(
                    ids=ids,
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=metadatas
                )

            self._get_bm25().add_documents([d.content for d in self.documents])

    def batch_add(self, directory: str) -> dict:
        """
        Batch add all supported documents from a directory.

        Args:
            directory: Path to directory containing documents

        Returns:
            dict with stats (added, failed, errors)
        """
        from microharness.rag.document_parser import SUPPORTED_FORMATS

        stats = {"added": 0, "failed": 0, "errors": []}
        dir_path = Path(directory)

        if not dir_path.exists():
            stats["errors"].append(f"Directory not found: {directory}")
            return stats

        for ext in ["*.md", "*.txt", "*.pdf", "*.html"]:
            for file_path in dir_path.glob(ext):
                try:
                    count = self.add_document_from_file(str(file_path))
                    stats["added"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{file_path.name}: {e}")

        if stats["added"] > 0:
            self.save_index()

        return stats

    def get_stats(self) -> dict:
        """Get knowledge base statistics."""
        type_counts = {}
        for doc in self.documents:
            mtype = doc.metadata.get("medical_type", "general")
            type_counts[mtype] = type_counts.get(mtype, 0) + 1

        return {
            "total_documents": len(self.documents),
            "by_type": type_counts,
            "index_dir": str(self.index_dir),
        }