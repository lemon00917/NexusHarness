"""
Ollama Client
=============
Simple wrapper for Ollama API.
"""

import os
import time
import threading
from typing import List, Optional

from microharness.observability.logger import ollama_logger

# Global concurrency guard for CPU-only servers.
# On 16C/39G VM with AVX512, 2 concurrent inferences keeps CPU near
# saturation without thrashing. Bump to 3 if GPU-accelerated.
_OLLAMA_SEMAPHORE = threading.Semaphore(2)


class OllamaClient:
    """Simple Ollama API client.

    All chat()/generate() calls are automatically serialized through
    a global semaphore to prevent CPU saturation on multi-core CPU servers.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:3b",
        timeout: int = 120,
        format_json: bool = False,
        **options
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.options = options

        # ── Output format control ──
        # format_json=True: Ollama grammar-constrained JSON output
        #   → guaranteed valid JSON, no CoT preamble possible
        self.format_json = format_json

        # ── Enforced defaults for all Ollama calls ──
        # num_ctx: context window (4096 tokens = ~3000 Chinese chars)
        # num_predict: max output tokens (prevent runaway generation)
        self.options.setdefault("num_ctx", 4096)
        self.options.setdefault("num_predict", 256)

        # reader-lm needs much larger context for HTML→Markdown conversion
        if "reader-lm" in model.lower():
            self.options["num_ctx"] = 131072
            self.options.pop("num_predict", None)  # no output limit for reader-lm

        self.timeout = timeout

        # Auto-detect profile for this model (imported lazily to avoid circular)
        self._profile = None

    @property
    def profile(self):
        """Lazy-load model profile."""
        if self._profile is None:
            from microharness.ollama.model_profile import get_profile
            self._profile = get_profile(self.model)
        return self._profile

    def chat(self, messages: list, temperature: float = 0.1, model: Optional[str] = None) -> str:
        """
        Send chat request to Ollama.

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature (lower = more deterministic)
            model: Override model (uses self.model if None)

        Returns:
            Model's response text.
            For thinking models (deepseek-r1), only the final content is
            returned; thinking is logged separately.
        """
        import requests

        start_time = time.time()
        url = f"{self.base_url}/api/chat"
        used_model = model or self.model
        payload = {
            "model": used_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "keep_alive": -1,  # never unload — avoids cold-start penalty on CPU servers
        }

        # ── Format: JSON (grammar-constrained output) ──
        if self.format_json:
            payload["format"] = "json"

        # Merge options (copy to avoid mutating shared defaults)
        if self.options:
            import copy
            payload["options"] = copy.deepcopy(self.options)
            if temperature != 0.1:
                payload["options"]["temperature"] = temperature

        ollama_logger.debug(f"Chat请求 | 模型: {used_model} | 消息数: {len(messages)} | format_json: {self.format_json} | 选项: {self.options}")

        # Acquire semaphore with timeout — don't wait forever if Ollama is overloaded
        _acquired = _OLLAMA_SEMAPHORE.acquire(timeout=60)
        if not _acquired:
            ollama_logger.error(f"Chat繁忙 | 模型: {used_model} | 排队超时60s")
            raise RuntimeError(f"Ollama busy: semaphore wait timeout (60s)")
        try:
            try:
                response = requests.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                body = response.json()
                message = body["message"]
                result = message.get("content", "").strip()
                thinking = message.get("thinking", "").strip()

                # ── Native thinking fallback ──
                # Some thinking models (deepseek-r1) occasionally put the
                # entire answer in `thinking` and leave `content` empty.
                if not result and thinking:
                    ollama_logger.info(
                        f"Chat(thinking→content) | 模型: {used_model} | "
                        f"content为空, 回退使用thinking ({len(thinking)}字)"
                    )
                    result = thinking
                elif thinking:
                    ollama_logger.info(
                        f"Chat(thinking) | 模型: {used_model} | "
                        f"思考长度: {len(thinking)} | "
                        f"输出长度: {len(result)}"
                    )

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
        finally:
            _OLLAMA_SEMAPHORE.release()

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
            "stream": False,
            "keep_alive": -1,
            "options": self.options,
        }

        ollama_logger.debug(f"Generate请求 | 模型: {self.model} | Prompt长度: {len(prompt)}")

        _acquired = _OLLAMA_SEMAPHORE.acquire(timeout=60)
        if not _acquired:
            ollama_logger.error(f"Generate繁忙 | 模型: {self.model} | 排队超时60s")
            raise RuntimeError(f"Ollama busy: semaphore wait timeout (60s)")
        try:
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
            except Exception as e:
                ollama_logger.error(f"Generate异常 | 模型: {self.model} | 错误: {str(e)}")
                raise
        finally:
            _OLLAMA_SEMAPHORE.release()

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

        url = f"{self.base_url}/api/embed"
        payload = {
            "model": self.model,
            "input": text
        }

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()["embeddings"][0]
            return result
        except Exception as e:
            ollama_logger.error(f"Embedding失败 | 模型: {self.model} | 错误: {str(e)}")
            raise

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts in a single API call.

        Args:
            texts: List of input texts to embed

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []
        import requests
        url = f"{self.base_url}/api/embed"
        payload = {"model": self.model, "input": texts}
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            return response.json()["embeddings"]
        except Exception as e:
            ollama_logger.error(f"Embedding批量失败 | 模型: {self.model} | 错误: {str(e)}")
            raise


# Default embedding model for Ollama
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


# Default client instance
_default_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    """Get or create default Ollama client."""
    global _default_client
    if _default_client is None:
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
        _default_client = OllamaClient(model=model)
    return _default_client


def set_client(client: OllamaClient) -> None:
    """Set default client instance."""
    global _default_client
    _default_client = client
