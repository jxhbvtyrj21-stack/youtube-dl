"""OpenAI image generation adapter (ARCHITECTURE.md §21).

``POST {base}/v1/images/generations`` with ``Authorization: Bearer <key>``
and the JSON body ``{"model", "prompt", "n": 1, "size", "quality"}``. GPT
image models always answer ``{"data": [{"b64_json": ...}]}``; for
``dall-e-3`` the base64 form is requested explicitly. The size is the
supported landscape or portrait size closest to the video orientation; the
image is fitted to the frame later by the normal image pipeline (§6.4).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import InputError, TransientError, VideoGenError
from videogen.providers.base import GeneratedAsset
from videogen.providers.http import HttpPolicy, request

DEFAULT_BASE_URL = "https://api.openai.com"
SERVICE = "OpenAI"
_MODEL = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")


def max_prompt(model: str) -> int:
    return 1000 if model.startswith("dall-e-2") else 4000 if model.startswith("dall-e-3") else 32000


def image_size(model: str, width: int, height: int) -> str:
    landscape, square = width > height, width == height
    if model.startswith("dall-e-2"):
        return "1024x1024"
    if model.startswith("dall-e-3"):
        return "1024x1024" if square else ("1792x1024" if landscape else "1024x1792")
    return "1024x1024" if square else ("1536x1024" if landscape else "1024x1536")


def _classify(status: int, snippet: str) -> VideoGenError | None:
    low = snippet.lower()
    if any(k in low for k in ("moderation_blocked", "content_policy_violation", "safety system", "safety_violations")):
        return InputError(f"{SERVICE}: сервіс відмовився створити зображення за цим промптом (правила вмісту). "
                          "Змініть промпт у prompts.txt.", code="PROVIDER_REJECTED", detail=f"HTTP {status}: {snippet}")
    if "insufficient_quota" in low or "billing_hard_limit" in low:
        return InputError(f"{SERVICE}: вичерпано кошти або ліміт на рахунку.", code="PROVIDER_QUOTA",
                          detail=f"HTTP {status}: {snippet}")
    if "model_not_found" in low or (status == 404 and "model" in low):
        return InputError(f"{SERVICE}: модель недоступна. Перевірте providers.openai_image_model.",
                          code="PROVIDER_CONFIG", detail=f"HTTP {status}: {snippet}")
    return None


class OpenAIImages:
    name = "openai"

    def __init__(self, api_key: str, *, model: str = "gpt-image-1", quality: str = "medium",
                 policy: HttpPolicy | None = None, base_url: str = DEFAULT_BASE_URL) -> None:
        if not _MODEL.match(model or ""):
            raise InputError(f"{SERVICE}: некоректна назва моделі.", code="PROVIDER_CONFIG")
        self._key = api_key
        self.model, self.quality = model, quality
        self.policy = policy or HttpPolicy()
        self.base_url = base_url.rstrip("/")
        self.name = f"openai:{model}:{quality}"

    def _body(self, prompt: str, size: str) -> dict[str, object]:
        body: dict[str, object] = {"model": self.model, "prompt": prompt, "n": 1, "size": size}
        if self.model.startswith("dall-e-3"):
            body["quality"] = "hd" if self.quality == "high" else "standard"
            body["response_format"] = "b64_json"
        elif self.model.startswith("dall-e-2"):
            body["response_format"] = "b64_json"
        else:
            body["quality"] = self.quality
        return body

    def generate(self, prompt: str, out_path: Path, *, width: int, height: int, timeout_s: float,
                 token: CancellationToken) -> GeneratedAsset:
        prompt = prompt.strip()
        if not prompt:
            raise InputError("Порожній промпт у prompts.txt.", code="PROMPT_EMPTY")
        if len(prompt) > max_prompt(self.model):
            raise InputError(f"Промпт задовгий ({len(prompt)} символів, максимум {max_prompt(self.model)}).",
                             code="PROMPT_TOO_LONG")
        resp = out_path.with_name(out_path.name + ".json")
        request("POST", f"{self.base_url}/v1/images/generations", service=SERVICE, token=token,
                policy=self.policy, classify=_classify, secrets=(self._key,), out_path=resp,
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                body=json.dumps(self._body(prompt, image_size(self.model, width, height)),
                                ensure_ascii=False).encode("utf-8"))
        try:
            payload = json.loads(resp.read_bytes())
            data = base64.b64decode(payload["data"][0]["b64_json"], validate=True)
        except (ValueError, KeyError, IndexError, TypeError, binascii.Error) as exc:
            raise TransientError(f"{SERVICE}: некоректна відповідь сервісу.", code="PROVIDER_BAD_RESPONSE",
                                 detail=repr(exc)[:300]) from exc
        finally:
            resp.unlink(missing_ok=True)
        if not data.startswith(_MAGIC):
            raise TransientError(f"{SERVICE}: сервіс повернув не зображення.", code="PROVIDER_BAD_RESPONSE",
                                 detail=f"{len(data)} bytes, starts with {data[:8]!r}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return GeneratedAsset(str(out_path), self.name)
