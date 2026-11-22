"""Değiştirilebilir LLM sağlayıcı katmanı.

Yeni sağlayıcı eklemek için `Provider` arayüzünü uygulayan bir sınıf yazıp PROVIDERS'a eklemek yeterli.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class ProviderError(Exception):
    pass


class Provider:
    name = "base"

    def complete(self, system: str, user: str, max_tokens: int = 900) -> Completion:  # pragma: no cover
        raise NotImplementedError


class ClaudeProvider(Provider):
    name = "claude"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None):
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY tanımlı değil.")
        self.api_key, self.model = api_key, model
        self.client = client or httpx.Client(timeout=60)

    def complete(self, system: str, user: str, max_tokens: int = 900) -> Completion:
        try:
            r = self.client.post(self.URL, headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                                    "content-type": "application/json"},
                                 json={"model": self.model, "max_tokens": max_tokens, "system": system,
                                       "messages": [{"role": "user", "content": user}]})
        except httpx.HTTPError as e:
            raise ProviderError(f"Claude API'ye ulaşılamadı: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"Claude API hata verdi: HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return Completion(text, u.get("input_tokens", 0), u.get("output_tokens", 0), d.get("model", self.model))


class GeminiProvider(Provider):
    """Google Gemini (ücretsiz katman). Model adları sık değiştiği için listeden otomatik seçilir;
    biri kotaya takılırsa sıradakine geçilir (Pipaloo'daki yöntemin aynısı)."""
    name = "gemini"
    API = "https://generativelanguage.googleapis.com/v1beta"
    BAD = ("image", "tts", "live", "audio", "embedding", "preview", "exp", "thinking", "robotics", "computer", "gemma")

    def __init__(self, api_key: str, model: str | None = None, client: httpx.Client | None = None):
        import re
        if not api_key:
            raise ProviderError("GEMINI_API_KEY tanımlı değil.")
        self.key = re.sub(r"\s+", "", api_key)
        self.client = client or httpx.Client(timeout=90)
        self._models = [model] if model and model.startswith("gemini") else None
        self.model = model or "gemini (otomatik)"

    def _hdr(self):
        return {"x-goog-api-key": self.key}

    def models(self) -> list[str]:
        import re
        if self._models:
            return self._models
        r = self.client.get(f"{self.API}/models", params={"pageSize": 200}, headers=self._hdr())
        if r.status_code != 200:
            raise ProviderError(f"Gemini model listesi alınamadı: HTTP {r.status_code}"
                                + (" (API anahtarı yanlış olabilir)" if r.status_code in (400, 401, 403) else ""))
        names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]

        def score(n):
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", n)] or [0]
            return (("flash" in n) and ("lite" not in n), "flash" in n, nums[0])
        good = sorted((n for n in names if "gemini" in n and not any(b in n for b in self.BAD)), key=score, reverse=True)
        if not good:
            good = sorted((n for n in names if "gemini" in n and "embedding" not in n), key=score, reverse=True)
        if not good:
            raise ProviderError("Uygun Gemini modeli bulunamadı.")
        lites = [n for n in good if "lite" in n]
        self._models = [n for n in good if "lite" not in n][:2] + lites[:2]
        return self._models

    def generate(self, body: dict, timeout: float = 90) -> tuple[dict, str]:
        """Modelleri sırayla dener; (yanıt json, model) döndürür."""
        last = None
        for model in self.models():
            for cfg_try in range(2):
                b = dict(body)
                if cfg_try == 1:  # bazı modeller thinkingConfig'i tanımaz
                    gc = dict(b.get("generationConfig", {}))
                    gc.pop("thinkingConfig", None)
                    b["generationConfig"] = gc
                try:
                    r = self.client.post(f"{self.API}/models/{model}:generateContent", headers=self._hdr(), json=b,
                                         timeout=timeout)
                except httpx.HTTPError as e:
                    last = f"{model}: {e}"
                    break
                if r.status_code == 400 and "thinking" in r.text.lower() and cfg_try == 0:
                    continue
                if r.status_code in (429, 500, 503):
                    last = f"{model}: HTTP {r.status_code} (kota/yoğunluk)"
                    break
                if r.status_code != 200:
                    last = f"{model}: HTTP {r.status_code} {r.text[:150]}"
                    break
                self.model = model
                return r.json(), model
        raise ProviderError(f"Gemini cevap vermedi: {last}")

    def complete(self, system: str, user: str, max_tokens: int = 900) -> Completion:
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                     "maxOutputTokens": max(max_tokens, 2048),
                                     "thinkingConfig": {"thinkingBudget": 0}}}
        d, model = self.generate(body)
        parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        u = d.get("usageMetadata", {})
        return Completion(text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0), model)


PROVIDERS = {"claude": ClaudeProvider, "gemini": GeminiProvider}
