"""
NexusHarness RAG Module
=======================
Retrieval-Augmented Generation for NexusHarness.

Supports:
- ChromaDB vector database
- HuggingFace sentence-transformers embeddings
- Document chunking (by length or chapter)
- Hybrid search (vector + BM25)
- Configurable via rag_config.json
"""

import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Tuple
from datetime import datetime
import hashlib

from .chunker import chunk_text
from .bm25 import BM25


@dataclass
class Document:
    """A document in the knowledge base."""
    doc_id: str
    content: str
    filename: str
    created_at: str
    metadata: dict


class SimpleRAG:
    """
    RAG using ChromaDB + HuggingFace sentence-transformers embeddings.

    Supports:
    - Document chunking (by length or chapter)
    - Hybrid search (vector + BM25)
    - Configurable search/chunk modes
    """

    def __init__(self, index_dir: str = "rag_index"):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.documents: List[Document] = []
        self._chunks: List[Document] = []  # Chunk-level documents
        self._embedding_model = None
        self._chroma_client = None
        self._collection = None
        self._bm25: Optional[BM25] = None
        self._chunk_to_doc: dict = {}  # chunk_id -> parent doc_id

    def _get_embedding_model(self):
        """Lazy load embedding model."""
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            print("[RAG] Embedding model loaded: all-MiniLM-L6-v2")
        return self._embedding_model

    def _get_chroma_collection(self):
        """Lazy init ChromaDB collection."""
        if self._chroma_client is None:
            import chromadb
            from chromadb.config import Settings

            self._chroma_client = chromadb.PersistentClient(
                path=str(self.index_dir / "chroma_db"),
                settings=Settings(anonymized_telemetry=False)
            )
            self._collection = self._chroma_client.get_or_create_collection(
                name="documents",
                metadata={"hnsw:space": "cosine"}
            )
        return self._collection

    def _compute_hash(self, text: str) -> str:
        """Generate a short hash for document ID."""
        return hashlib.md5(text.encode()).hexdigest()[:12]

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using sentence-transformers."""
        model = self._get_embedding_model()
        embeddings = model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()

    def _get_bm25(self) -> BM25:
        """Get or create BM25 index."""
        if self._bm25 is None:
            self._bm25 = BM25()
        return self._bm25

    def _get_config(self):
        """Get current RAG configuration."""
        from .rag_config import load_config
        return load_config()

    def add_document(self, content: str, filename: str, metadata: dict = None) -> str:
        """Add a document to the knowledge base with optional chunking."""
        doc_id = self._compute_hash(content + filename)

        # Check if already exists
        for doc in self.documents:
            if doc.doc_id == doc_id:
                return doc_id

        config = self._get_config()
        doc = Document(
            doc_id=doc_id,
            content=content,
            filename=filename,
            created_at=datetime.now().isoformat(),
            metadata=metadata or {},
        )
        self.documents.append(doc)

        # Chunk the document if enabled
        chunks = chunk_text(content, config.chunk_mode,
                          chunk_size=config.chunk_size,
                          overlap=config.chunk_overlap)

        if len(chunks) > 1:
            # Store parent-child relationship
            self._chunk_to_doc[doc_id] = doc_id

            # Add chunks to ChromaDB
            collection = self._get_chroma_collection()
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
                    "chunk_index": i
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
            # Single chunk - add directly
            collection = self._get_chroma_collection()
            embedding = self._embed_texts([content])[0]
            collection.add(
                ids=doc_id,
                embeddings=embedding,
                documents=content,
                metadatas={"filename": filename, "parent_id": doc_id}
            )

        # Add to BM25 index
        self._get_bm25().add_documents([d.content for d in self.documents])

        return doc_id

    def similarity_search(self, query: str, top_k: int = 3,
                        vector_weight: float = 1.0,
                        bm25_weight: float = 0.0) -> List[Document]:
        """
        Search for documents using vector similarity or hybrid search.

        Args:
            query: Search query
            top_k: Number of results to return
            vector_weight: Weight for vector search (0.0-1.0)
            bm25_weight: Weight for BM25 search (0.0-1.0)

        Returns:
            List of matching Documents
        """
        if not self.documents:
            return []

        collection = self._get_chroma_collection()
        query_embedding = self._embed_texts([query])[0]

        if bm25_weight > 0 and self._bm25:
            # Hybrid search: combine vector + BM25
            bm25_scores = self._get_bm25().get_scores(query)

            # Build doc_id -> bm25_score lookup
            doc_id_to_bm25 = {}
            for i, doc in enumerate(self.documents):
                doc_id_to_bm25[doc.doc_id] = bm25_scores[i] if i < len(bm25_scores) else 0.0

            # Get vector results
            vector_results = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k * 2, len(self.documents)),
                include=["distances"]
            )

            # Combine scores
            doc_scores = {}
            for doc_id, distance in zip(vector_results["ids"][0],
                                        vector_results["distances"][0]):
                vector_score = 1.0 - distance  # Convert distance to similarity
                bm25_score = doc_id_to_bm25.get(doc_id, 0.0)
                total_score = (vector_weight * vector_score +
                             bm25_weight * bm25_score)
                doc_scores[doc_id] = total_score

            # Sort by combined score
            sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)

            matched = []
            seen_parent = set()
            for doc_id, score in sorted_docs[:top_k * 3]:
                parent_id = self._chunk_to_doc.get(doc_id, doc_id)
                if parent_id in seen_parent:
                    continue
                seen_parent.add(parent_id)

                doc = next((d for d in self.documents if d.doc_id == parent_id), None)
                if doc and doc not in matched:
                    matched.append(doc)
                    if len(matched) >= top_k:
                        break

            return matched
        else:
            # Pure vector search
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k
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

            return matched_docs

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document and its chunks."""
        # Find and remove from documents list
        doc = next((d for d in self.documents if d.doc_id == doc_id), None)
        if not doc:
            return False

        self.documents.remove(doc)

        # Delete from ChromaDB (including chunks)
        collection = self._get_chroma_collection()
        collection.delete(ids=[doc_id])

        # Delete associated chunks
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
        """Persist metadata to disk."""
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

        # Rebuild ChromaDB collection
        if self.documents:
            self._chroma_client = None
            self._collection = None
            collection = self._get_chroma_collection()

            ids = []
            embeddings = []
            documents = []
            metadatas = []

            for doc in self.documents:
                ids.append(doc.doc_id)
                documents.append(doc.content)
                metadatas.append({"filename": doc.filename, "parent_id": doc.doc_id})

            if ids:
                embeddings = self._embed_texts(documents)
                collection.add(
                    ids=ids,
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=metadatas
                )

            # Load BM25
            self._get_bm25().add_documents([d.content for d in self.documents])

    def load_documents_from_dir(self, dir_path: str):
        """Load all txt/md files from a directory."""
        dir_path = Path(dir_path)
        if not dir_path.exists():
            return

        for ext in ["*.txt", "*.md", "*.json"]:
            for file_path in dir_path.glob(ext):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    self.add_document(content, file_path.name)
                except Exception as e:
                    print(f"Failed to load {file_path}: {e}")

    def preview_chunking(self, content: str) -> List[dict]:
        """Preview how content would be chunked."""
        config = self._get_config()
        from .chunker import preview_chunks
        return preview_chunks(content, config.chunk_mode,
                             config.chunk_size, config.chunk_overlap)


# Global RAG instance
rag = SimpleRAG()