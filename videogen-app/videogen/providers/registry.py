"""Builds the MODE B providers from the settings and the key store
(ARCHITECTURE.md §21). Core and pipeline see only the interfaces of
``providers/base.py``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from videogen.config.settings import ProviderSettings
from videogen.core.errors import InputError
from videogen.providers.base import ImageGenProvider, TTSProvider
from videogen.providers.elevenlabs_tts import DEFAULT_BASE_URL as ELEVEN_URL, ElevenLabsTTS
from videogen.providers.http import HttpPolicy
from videogen.providers.openai_images import DEFAULT_BASE_URL as OPENAI_URL, OpenAIImages
from videogen.utils.credentials import SERVICES, CredentialStore, CredentialStoreError


@dataclass
class ProviderSetup:
    tts: TTSProvider | None = None
    images: ImageGenProvider | None = None
    missing: list[str] = field(default_factory=list)       # services without a key (display names)
    problem: str = ""                                       # why the store could not be read

    @property
    def ready(self) -> bool:
        return self.tts is not None and self.images is not None

    def explanation(self) -> str:
        if self.problem:
            return self.problem
        if self.missing:
            return ("Для режиму B не задано ключ API: " + ", ".join(self.missing)
                    + ". Додайте ключі в меню «Ключі API…».")
        return ""


def _test_base_url() -> str:
    """Test hook (production suite): send provider requests to a local
    imitation of the services. Inactive unless VIDEOGEN_TEST_HOOKS=1, and only
    a loopback address is accepted, so a key can never leave the machine
    through it."""
    if os.environ.get("VIDEOGEN_TEST_HOOKS") != "1":
        return ""
    url = os.environ.get("VIDEOGEN_TEST_PROVIDER_BASE_URL", "")
    host = urlsplit(url).hostname if url else None
    return url if host in ("127.0.0.1", "localhost") else ""


def policy_from(s: ProviderSettings) -> HttpPolicy:
    return HttpPolicy(connect_timeout_s=s.connect_timeout_s, read_timeout_s=s.read_timeout_s,
                      total_timeout_s=s.request_timeout_s)


def build_providers(s: ProviderSettings, store: CredentialStore) -> ProviderSetup:
    setup = ProviderSetup()
    if not store.available:
        setup.problem = "Режим B потребує Windows: ключі API зберігаються в Windows Credential Manager."
        return setup
    keys: dict[str, str | None] = {}
    try:
        for svc in SERVICES:
            keys[svc] = store.get(svc)
    except CredentialStoreError as exc:
        setup.problem = str(exc)
        return setup
    setup.missing = [SERVICES[k] for k, v in keys.items() if not v]
    if setup.missing:
        return setup
    base = _test_base_url()
    policy = policy_from(s)
    try:
        setup.tts = ElevenLabsTTS(keys["elevenlabs"] or "", voice_id=s.elevenlabs_voice_id,
                                  model_id=s.elevenlabs_model_id, output_format=s.elevenlabs_output_format,
                                  max_chars=s.elevenlabs_max_chars, policy=policy, base_url=base or ELEVEN_URL)
        setup.images = OpenAIImages(keys["openai"] or "", model=s.openai_image_model,
                                    quality=s.openai_image_quality, policy=policy, base_url=base or OPENAI_URL)
    except InputError as exc:
        setup.tts = setup.images = None
        setup.problem = exc.user_message
    return setup
