"""
Ollama Client
=============
Simple wrapper for Ollama API.
"""

import os
import time
from typing import List, Optional

from microharness.observability.logger import ollama_logger


class OllamaClient:
    """Simple Ollama API client."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2:1.5b",
        timeout: int = 120,
        **options
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.options = options

        if "reader-lm" in model.lower():
            self.options.setdefault("num_ctx", 131072)

        self.timeout = timeout

    def chat(self, messages: list, temperature: float = 0.1, model: Optional[str] = None) -> str:
        """
        Send chat request to Ollama.

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature (lower = more deterministic)
            model: Override model (uses self.model if None)

        Returns:
            Model's response text
        """
        import requests

        start_time = time.time()
        url = f"{self.base_url}/api/chat"
        used_model = model or self.model
        payload = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False
        }

        # 合并额外选项
        if self.options:
            payload["options"] = self.options
            # temperature 可以被 messages 级别覆盖
            if "options" in payload and temperature != 0.1:
                payload["options"]["temperature"] = temperature

        ollama_logger.debug(f"Chat请求 | 模型: {used_model} | 消息数: {len(messages)} | 选项: {self.options}")

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()["message"]["content"]

            duration = (time.time() - start_time) * 1000
            ollama_logger.info(
                f"Chat成功 | 模型: {used_model} | "
                f"消息数: {len(messages)} | "
                f"输出长度: {len(result)} | "
                f"耗时: {duration:.2f}ms"
            )

            return result

        except requests.exceptions.Timeout:
            ollama_logger.error(f"Chat超时 | 模型: {self.model} | 超时: {self.timeout}s")
            raise
        except requests.exceptions.RequestException as e:
            ollama_logger.error(f"Chat失败 | 模型: {self.model} | 错误: {str(e)}")
            raise
        except Exception as e:
            ollama_logger.error(f"Chat异常 | 模型: {self.model} | 错误: {str(e)}")
            raise

    def generate(self, prompt: str, temperature: float = 0.1) -> str:
        """
        Generate completion from prompt.

        Args:
            prompt: Input prompt
            temperature: Sampling temperature

        Returns:
            Generated text
        """
        import requests

        start_time = time.time()
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "temperature": temperature,
            "stream": False
        }

        ollama_logger.debug(f"Generate请求 | 模型: {self.model} | Prompt长度: {len(prompt)}")

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()["response"]

            duration = (time.time() - start_time) * 1000
            ollama_logger.info(
                f"Generate成功 | 模型: {self.model} | "
                f"Prompt长度: {len(prompt)} | "
                f"输出长度: {len(result)} | "
                f"耗时: {duration:.2f}ms"
            )

            return result

        except requests.exceptions.Timeout:
            ollama_logger.error(f"Generate超时 | 模型: {self.model} | 超时: {self.timeout}s")
            raise
        except requests.exceptions.RequestException as e:
            ollama_logger.error(f"Generate失败 | 模型: {self.model} | 错误: {str(e)}")
            raise

    def is_available(self) -> bool:
        """Check if Ollama server is running."""
        import requests

        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """List available models from Ollama server."""
        import requests

        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if response.status_code == 200:
                data = response.json()
                return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            return []
        except Exception:
            return []

    def embed(self, text: str) -> List[float]:
        """
        Generate embedding for text using Ollama's embedding API.

        Args:
            text: Input text to embed

        Returns:
            Embedding vector as list of floats
        """
        import requests

        url = f"{self.base_url}/api/embeddings"
        payload = {
            "model": self.model,
            "prompt": text
        }

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()["embedding"]
            return result
        except Exception as e:
            ollama_logger.error(f"Embedding失败 | 模型: {self.model} | 错误: {str(e)}")
            raise

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of input texts to embed

        Returns:
            List of embedding vectors
        """
        return [self.embed(text) for text in texts]


# Default embedding model for Ollama
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


# Default client instance
_default_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    """Get or create default Ollama client."""
    global _default_client
    if _default_client is None:
        model = os.environ.get("OLLAMA_MODEL", "qwen2:1.5b")
        _default_client = OllamaClient(model=model)
    return _default_client


def set_client(client: OllamaClient) -> None:
    """Set default client instance."""
    global _default_client
    _default_client = client
