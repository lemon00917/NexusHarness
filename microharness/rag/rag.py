"""
NexusHarness RAG Module
=======================
Retrieval-Augmented Generation system with hybrid search capabilities.

Features:
- ChromaDB vector database for semantic search
- HuggingFace sentence-transformers embeddings
- BM25 keyword search for hybrid retrieval
- Document chunking with configurable strategies
- Persistent index with save/load support

Architecture:
    SimpleRAG
    ├── Vector Store (ChromaDB)
    ├── Keyword Index (BM25)
    ├── Document Store (In-memory)
    └── Chunk Manager
"""

import hashlib
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .bm25 import BM25
from .chunker import chunk_text, preview_chunks


# ──────────────────────── Data Models ────────────────────────

@dataclass
class Document:
    """Represents a document in the knowledge base."""
    doc_id: str
    content: str
    filename: str
    created_at: str
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    """Structured search result with relevance metadata."""
    document: Document
    score: float
    matched_chunk: Optional[str] = None


# ──────────────────────── Exceptions ────────────────────────

class RAGException(Exception):
    """Base exception for RAG errors."""
    pass


class DocumentNotFoundError(RAGException):
    """Raised when a document cannot be found."""
    pass


class IndexLoadError(RAGException):
    """Raised when index loading fails."""
    pass


# ──────────────────────── Configuration Constants ────────────────────────

# Embedding model settings
# Use multilingual model for better Chinese/CJK support
DEFAULT_EMBEDDING_MODEL = 'paraphrase-multilingual-MiniLM-L12-v2'

# ChromaDB settings
CHROMA_COLLECTION_NAME = "documents"
CHROMA_DISTANCE_METRIC = "cosine"

# Search defaults
DEFAULT_TOP_K = 3
HYBRID_SEARCH_MULTIPLIER = 10  # Fetch more candidates in hybrid mode

# Hash settings
DOC_ID_HASH_LENGTH = 12


# ──────────────────────── Main RAG Class ────────────────────────

class SimpleRAG:
    """
    Retrieval-Augmented Generation system with hybrid search.

    Combines vector similarity (ChromaDB) with keyword matching (BM25)
    for improved retrieval accuracy.

    Usage:
        rag = SimpleRAG("my_index")
        doc_id = rag.add_document("content", "file.txt")
        results = rag.search("query", top_k=3)
    """

    def __init__(self, index_dir: str = "rag_index", auto_save: bool = True):
        """
        Initialize RAG system with persistent storage.

        Args:
            index_dir: Directory for index persistence
            auto_save: Automatically save index after modifications
        """
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        # Persistence
        self.auto_save = auto_save

        # Document storage
        self._documents: Dict[str, Document] = {}  # doc_id -> Document
        self._chunk_to_parent: Dict[str, str] = {}  # chunk_id -> parent doc_id

        # Search components (lazy initialized)
        self._embedding_model = None
        self._chroma_client = None
        self._collection = None
        self._bm25: Optional[BM25] = None

        # Configuration
        self._config = None

    # ──────────────────────── Public API ────────────────────────

    def add_document(
        self,
        content: str,
        filename: str,
        metadata: Optional[dict] = None
    ) -> str:
        """
        Add a document to the knowledge base with automatic chunking.

        Process:
        1. Generate unique document ID
        2. Check for duplicates
        3. Chunk content based on configuration
        4. Index chunks in vector store
        5. Update keyword index

        Args:
            content: Document text content
            filename: Original filename for reference
            metadata: Optional metadata dictionary

        Returns:
            Document ID string

        Raises:
            ValueError: If content is empty
        """
        if not content or not content.strip():
            raise ValueError("Document content cannot be empty")

        # Generate unique ID
        doc_id = self._generate_doc_id(content, filename)

        # Skip duplicates
        if doc_id in self._documents:
            return doc_id

        # Create document record
        document = Document(
            doc_id=doc_id,
            content=content,
            filename=filename,
            created_at=datetime.now().isoformat(),
            metadata=metadata or {},
        )

        # Store document
        self._documents[doc_id] = document

        # Chunk and index
        chunks = self._chunk_content(content)
        self._index_chunks(doc_id, filename, chunks)

        # Update keyword index
        self._rebuild_bm25_index()

        # Persist if enabled
        if self.auto_save:
            self.save_index()

        return doc_id

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        vector_weight: float = 1.0,
        bm25_weight: float = 0.0,
    ) -> List[SearchResult]:
        """
        Search for relevant documents.

        Supports two modes:
        - Vector search (vector_weight=1.0, bm25_weight=0.0)
        - Hybrid search (vector_weight > 0, bm25_weight > 0)

        Args:
            query: Search query text
            top_k: Number of results to return
            vector_weight: Weight for vector similarity (0.0-1.0)
            bm25_weight: Weight for BM25 keyword score (0.0-1.0)

        Returns:
            List of SearchResult objects sorted by relevance

        Raises:
            ValueError: If weights are invalid
        """
        if not self._documents:
            return []

        self._validate_search_weights(vector_weight, bm25_weight)

        if self._is_hybrid_search(bm25_weight):
            return self._hybrid_search(query, top_k, vector_weight, bm25_weight)
        else:
            return self._vector_search(query, top_k)

    def delete_document(self, doc_id: str) -> bool:
        """
        Remove a document and all its chunks from the index.

        Args:
            doc_id: Document ID to remove

        Returns:
            True if document was found and deleted
        """
        if doc_id not in self._documents:
            return False

        # Remove from document store
        del self._documents[doc_id]

        # Remove from vector store
        chunks_to_remove = self._get_document_chunks(doc_id)
        if chunks_to_remove:
            self._get_chroma_collection().delete(ids=chunks_to_remove)

        # Clean up chunk mapping
        self._cleanup_chunk_mappings(doc_id)

        # Rebuild keyword index
        if self._documents:
            self._rebuild_bm25_index()
        else:
            self._bm25 = None

        # Persist if enabled
        if self.auto_save:
            self.save_index()

        return True

    def list_documents(self, as_dict: bool = True) -> List[dict]:
        """
        Get all documents.

        Args:
            as_dict: Return as dictionaries (True) or Document objects (False)
        """
        if as_dict:
            return [asdict(doc) for doc in self._documents.values()]
        return list(self._documents.values())

    def get_document(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        return self._documents.get(doc_id)

    def preview_chunking(self, content: str) -> List[dict]:
        """Preview how content would be chunked."""
        config = self._get_config()
        return preview_chunks(
            content,
            mode=config.chunk_mode,
            chunk_size=config.chunk_size,
            overlap=config.chunk_overlap
        )

    def load_documents_from_dir(self, dir_path: str) -> int:
        """
        Load all supported documents from a directory.

        Args:
            dir_path: Path to directory

        Returns:
            Number of documents loaded
        """
        dir_path = Path(dir_path)
        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {dir_path}")

        supported_extensions = ["*.txt", "*.md", "*.json"]
        loaded_count = 0

        for pattern in supported_extensions:
            for file_path in dir_path.glob(pattern):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    self.add_document(content, file_path.name)
                    loaded_count += 1
                except Exception as e:
                    print(f"[RAG] Failed to load {file_path.name}: {e}")

        return loaded_count

    def save_index(self) -> None:
        """Persist index metadata to disk."""
        index_file = self.index_dir / "index.json"

        data = {
            "version": "1.0",
            "saved_at": datetime.now().isoformat(),
            "documents": [asdict(doc) for doc in self._documents.values()],
            "chunk_mapping": self._chunk_to_parent,
        }

        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"[RAG] Index saved: {len(self._documents)} documents")

    def load_index(self, force_rebuild: bool = False) -> None:
        """
        Load index metadata from disk.

        Args:
            force_rebuild: Force rebuild of ChromaDB from documents

        Raises:
            IndexLoadError: If saved index is corrupted
        """
        index_file = self.index_dir / "index.json"

        if not index_file.exists():
            print("[RAG] No saved index found, starting fresh")
            return

        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Restore documents
            self._documents = {
                d["doc_id"]: Document(**d)
                for d in data.get("documents", [])
            }

            # Restore chunk mapping
            self._chunk_to_parent = data.get("chunk_mapping", {})

            # Verify ChromaDB state (skip if skipped by force_rebuild)
            if not force_rebuild:
                try:
                    chroma_count = self._get_chroma_collection().count()
                    # Calculate expected count: multi-chunk docs only store chunks, single-chunk docs store parent
                    expected_count = 0
                    for doc_id in self._documents:
                        chunks = self._get_document_chunks(doc_id)
                        if chunks:
                            # Multi-chunk doc: only chunks stored (no parent entry)
                            expected_count += len(chunks)
                        else:
                            # Single-chunk doc: parent doc_id stored as entry
                            expected_count += 1

                    if chroma_count == 0 and self._documents:
                        print("[RAG] Warning: ChromaDB empty but documents exist. Rebuilding...")
                        self._rebuild_chroma_from_documents()
                    elif chroma_count != expected_count:
                        print(f"[RAG] Warning: ChromaDB count mismatch ({chroma_count} vs {expected_count}). Rebuilding...")
                        self._rebuild_chroma_from_documents()
                except Exception as e:
                    print(f"[RAG] Warning: ChromaDB verification failed: {e}. Rebuilding...")
                    self._rebuild_chroma_from_documents()
            else:
                self._rebuild_chroma_from_documents()

            # Rebuild BM25
            if self._documents:
                self._rebuild_bm25_index()

            print(f"[RAG] Index loaded: {len(self._documents)} documents")

        except Exception as e:
            raise IndexLoadError(f"Failed to load index: {e}")

    # ──────────────────────── Properties ────────────────────────

    @property
    def document_count(self) -> int:
        """Total number of indexed documents."""
        return len(self._documents)

    @property
    def is_ready(self) -> bool:
        """Check if RAG system has documents indexed."""
        return self.document_count > 0

    # ──────────────────────── Search Implementation ────────────────────────

    def _vector_search(self, query: str, top_k: int) -> List[SearchResult]:
        """Pure vector similarity search."""
        query_embedding = self._embed_texts([query])[0]

        results = self._get_chroma_collection().query(
            query_embeddings=[query_embedding],
            n_results=top_k
        )

        return self._deduplicate_results(
            results["ids"][0],
            results.get("distances", [[None]])[0]
        )[:top_k]

    def _hybrid_search(
        self,
        query: str,
        top_k: int,
        vector_weight: float,
        bm25_weight: float,
    ) -> List[SearchResult]:
        """Combine vector and BM25 scores for improved retrieval."""
        # Get vector search candidates (more than needed for scoring)
        query_embedding = self._embed_texts([query])[0]
        vector_results = self._get_chroma_collection().query(
            query_embeddings=[query_embedding],
            n_results=self._calculate_hybrid_candidate_count(top_k),
            include=["distances"]
        )

        # Get BM25 scores
        bm25_scores = self._compute_bm25_scores(query)

        # Combine scores
        combined = {}
        chunk_ids = vector_results["ids"][0]
        distances = vector_results["distances"][0]

        for chunk_id, distance in zip(chunk_ids, distances):
            parent_id = self._chunk_to_parent.get(chunk_id, chunk_id)
            vector_score = self._normalize_vector_score(distance)
            bm25_score = bm25_scores.get(parent_id, 0.0)

            combined[chunk_id] = (
                vector_weight * vector_score +
                bm25_weight * bm25_score
            )

        # Sort and deduplicate
        sorted_ids = sorted(combined.keys(), key=lambda x: combined[x], reverse=True)
        sorted_scores = [combined[cid] for cid in sorted_ids]
        return self._deduplicate_results(sorted_ids, scores=sorted_scores)[:top_k]

    def _compute_bm25_scores(self, query: str) -> Dict[str, float]:
        """Compute BM25 scores and map to document IDs."""
        if not self._bm25:
            return {}

        scores = self._bm25.get_scores(query)
        return {
            doc_id: scores[i] if i < len(scores) else 0.0
            for i, doc_id in enumerate(self._documents.keys())
        }

    def _deduplicate_results(
        self,
        chunk_ids: List[str],
        distances: Optional[List[float]] = None,
        scores: Optional[List[float]] = None
    ) -> List[SearchResult]:
        """
        Convert chunk results to unique document results.

        Ensures each parent document appears only once in results,
        preserving the highest-scoring chunk's score.

        Args:
            chunk_ids: List of chunk IDs from search
            distances: ChromaDB distances (used if scores not provided)
            scores: Pre-computed scores (takes precedence over distances)
        """
        results = []
        seen_parents = set()

        # Track best score per parent
        parent_best_score: Dict[str, float] = {}

        for i, chunk_id in enumerate(chunk_ids):
            parent_id = self._chunk_to_parent.get(chunk_id, chunk_id)

            # Compute score: prefer pre-computed scores, then distances
            if scores is not None and i < len(scores):
                score = scores[i]
            elif distances is not None and i < len(distances):
                score = 1.0 - distances[i]
            else:
                score = 0.0

            if parent_id in seen_parents:
                # Update if better score found
                if parent_best_score.get(parent_id, -1) < score:
                    parent_best_score[parent_id] = score
                continue

            document = self._documents.get(parent_id)
            if document is None:
                continue

            seen_parents.add(parent_id)
            parent_best_score[parent_id] = score

            results.append(SearchResult(
                document=document,
                score=score,
                matched_chunk=chunk_id if chunk_id != parent_id else None
            ))

        return results

    # ──────────────────────── Document Indexing ────────────────────────

    def _chunk_content(self, content: str) -> List[str]:
        """Split content into chunks based on configuration."""
        config = self._get_config()
        return chunk_text(
            content,
            mode=config.chunk_mode,
            chunk_size=config.chunk_size,
            overlap=config.chunk_overlap
        )

    def _index_chunks(
        self,
        doc_id: str,
        filename: str,
        chunks: List[str]
    ) -> None:
        """Add chunks to the vector store."""
        collection = self._get_chroma_collection()

        if len(chunks) == 1:
            # Single chunk - store with doc_id
            self._add_single_chunk(collection, doc_id, chunks[0], filename)
        else:
            # Multiple chunks - create child entries
            self._add_multiple_chunks(collection, doc_id, filename, chunks)

    def _add_single_chunk(self, collection, doc_id: str, content: str, filename: str) -> None:
        """Add a single chunk (entire document) to vector store."""
        embedding = self._embed_texts([content])[0]

        collection.add(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[{"filename": filename, "parent_id": doc_id}]
        )

        self._chunk_to_parent[doc_id] = doc_id

    def _add_multiple_chunks(
        self,
        collection,
        doc_id: str,
        filename: str,
        chunks: List[str]
    ) -> None:
        """Add multiple chunks to vector store with parent-child relationship."""
        if not chunks:
            return

        embeddings = self._embed_texts(chunks)

        ids = []
        metadatas = []

        for i, chunk_content in enumerate(chunks):
            chunk_id = f"{doc_id}_chunk_{i}"
            ids.append(chunk_id)
            metadatas.append({
                "filename": filename,
                "parent_id": doc_id,
                "chunk_index": i,
                "total_chunks": len(chunks)
            })
            self._chunk_to_parent[chunk_id] = doc_id

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas
        )

    # ──────────────────────── Embedding Management ────────────────────────

    def _get_embedding_model(self):
        """Lazy load the sentence-transformer model."""
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer(DEFAULT_EMBEDDING_MODEL)
            print(f"[RAG] Embedding model loaded: {DEFAULT_EMBEDDING_MODEL}")
        return self._embedding_model

    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for a batch of texts."""
        model = self._get_embedding_model()
        embeddings = model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()

    # ──────────────────────── ChromaDB Management ────────────────────────

    def _get_chroma_collection(self):
        """Lazy initialize ChromaDB client and collection."""
        if self._chroma_client is None:
            import chromadb
            from chromadb.config import Settings

            self._chroma_client = chromadb.PersistentClient(
                path=str(self.index_dir / "chroma_db"),
                settings=Settings(anonymized_telemetry=False)
            )

        # Always get a fresh collection reference to avoid stale cached references
        # after rebuilds or when collection was deleted and recreated
        try:
            self._collection = self._chroma_client.get_collection(name=CHROMA_COLLECTION_NAME)
            # Verify the collection is usable by checking its ID matches what we expect
            if self._collection is not None:
                # Test if collection is actually usable by calling count
                try:
                    self._collection.count()
                except Exception:
                    # Collection exists but is corrupted, recreate it
                    self._chroma_client.delete_collection(name=CHROMA_COLLECTION_NAME)
                    self._collection = None
        except Exception:
            # Collection doesn't exist, create it
            self._collection = None

        if self._collection is None:
            self._collection = self._chroma_client.get_or_create_collection(
                name=CHROMA_COLLECTION_NAME,
                metadata={"hnsw:space": CHROMA_DISTANCE_METRIC}
            )
            print(f"[RAG] ChromaDB initialized: {self.index_dir / 'chroma_db'}")

        return self._collection

    def _rebuild_chroma_from_documents(self) -> None:
        """Rebuild ChromaDB collection from in-memory documents."""
        # Ensure chroma client is initialized
        if self._chroma_client is None:
            import chromadb
            from chromadb.config import Settings
            self._chroma_client = chromadb.PersistentClient(
                path=str(self.index_dir / "chroma_db"),
                settings=Settings(anonymized_telemetry=False)
            )

        # Delete existing collection completely
        try:
            self._chroma_client.delete_collection(name=CHROMA_COLLECTION_NAME)
        except Exception:
            pass

        # Create fresh collection and store in self._collection directly
        # to avoid infinite loop with _get_chroma_collection() checking count
        self._collection = self._chroma_client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": CHROMA_DISTANCE_METRIC}
        )

        # Re-add all documents using the fresh collection reference
        if self._documents:
            for doc in self._documents.values():
                chunks = self._chunk_content(doc.content)
                if len(chunks) == 1:
                    self._add_single_chunk(self._collection, doc.doc_id, chunks[0], doc.filename)
                else:
                    self._add_multiple_chunks(self._collection, doc.doc_id, doc.filename, chunks)

        print(f"[RAG] ChromaDB rebuilt: {self._collection.count()} entries")

    # ──────────────────────── BM25 Management ────────────────────────

    def _ensure_bm25(self) -> BM25:
        """Ensure BM25 index exists."""
        if self._bm25 is None:
            self._bm25 = BM25()
        return self._bm25

    def _rebuild_bm25_index(self) -> None:
        """Rebuild the BM25 keyword index from current documents."""
        self._bm25 = None
        if self._documents:
            documents_content = [doc.content for doc in self._documents.values()]
            self._ensure_bm25().add_documents(documents_content)

    # ──────────────────────── Configuration ────────────────────────

    def _get_config(self):
        """Get cached RAG configuration."""
        if self._config is None:
            from .rag_config import load_config
            self._config = load_config()
        return self._config

    # ──────────────────────── Utility Methods ────────────────────────

    @staticmethod
    def _generate_doc_id(content: str, filename: str) -> str:
        """Generate a unique document ID."""
        hash_input = f"{content}{filename}".encode()
        return hashlib.md5(hash_input).hexdigest()[:DOC_ID_HASH_LENGTH]

    @staticmethod
    def _validate_search_weights(vector_weight: float, bm25_weight: float) -> None:
        """Validate search weight parameters."""
        if not (0.0 <= vector_weight <= 1.0):
            raise ValueError(f"vector_weight must be 0.0-1.0, got {vector_weight}")
        if not (0.0 <= bm25_weight <= 1.0):
            raise ValueError(f"bm25_weight must be 0.0-1.0, got {bm25_weight}")

    @staticmethod
    def _is_hybrid_search(bm25_weight: float) -> bool:
        """Check if hybrid search is requested."""
        return bm25_weight > 0

    @staticmethod
    def _calculate_hybrid_candidate_count(top_k: int) -> int:
        """Calculate how many candidates to fetch for hybrid scoring."""
        return min(top_k * HYBRID_SEARCH_MULTIPLIER, 100)

    @staticmethod
    def _normalize_vector_score(distance: float) -> float:
        """Convert distance to similarity score."""
        return 1.0 - distance

    def _get_document_chunks(self, doc_id: str) -> List[str]:
        """Get all chunk IDs belonging to a document."""
        return [
            chunk_id
            for chunk_id, parent_id in self._chunk_to_parent.items()
            if parent_id == doc_id
        ]

    def _cleanup_chunk_mappings(self, doc_id: str) -> None:
        """Remove chunk mappings for a document."""
        self._chunk_to_parent.pop(doc_id, None)

        chunks_to_remove = [
            chunk_id
            for chunk_id, parent_id in self._chunk_to_parent.items()
            if parent_id == doc_id
        ]

        for chunk_id in chunks_to_remove:
            self._chunk_to_parent.pop(chunk_id, None)


# ──────────────────────── Global Instance ────────────────────────

# Singleton RAG instance for convenience
rag = SimpleRAG()