"""Optional primary LLM endpoint with a local fallback and cached health probe."""
import time
from urllib.parse import urljoin, urlparse

import requests


class LlmBackend:
    def __init__(self, read, logger, endpoint):
        self.logger = logger
        self.primary = read('Llm_Command', 'primary_endpoint', fallback='', value_type='str').strip()
        self.fallback = read('Llm_Command', 'fallback_endpoint', fallback='', value_type='str').strip() or endpoint
        self.api_key = read('Llm_Command', 'api_key', fallback='', value_type='str').strip()
        self.health_path = read('Llm_Command', 'health_path', fallback='/health', value_type='str') or '/health'
        self.ping_timeout = max(1, min(10, read('Llm_Command', 'ping_timeout_seconds', fallback=2., value_type='float')))
        self.cache_seconds = max(0, read('Llm_Command', 'ping_cache_seconds', fallback=60., value_type='float'))
        self.model_info_cache_seconds = max(0, read("Llm_Command", "model_info_cache_seconds", fallback=300., value_type="float"))
        self._checked_at = None
        self._healthy = False
        self.active_endpoint = endpoint

    def headers(self, endpoint):
        return {'Authorization': 'Bearer ' + self.api_key} if endpoint == self.primary and self.api_key else {}

    def resolve(self):
        if not self.primary:
            return self.fallback
        now = time.monotonic()
        if self._checked_at is None or now - self._checked_at >= self.cache_seconds:
            parsed = urlparse(self.primary)
            health = urljoin(f'{parsed.scheme}://{parsed.netloc}/', self.health_path.lstrip('/'))
            try:
                self._healthy = requests.get(health, headers=self.headers(self.primary), timeout=self.ping_timeout).status_code == 200
            except requests.RequestException:
                self._healthy = False
            self._checked_at = now
        return self.primary if self._healthy else self.fallback

    def post_chat(self, payload, timeout):
        started = time.monotonic()
        endpoint = self.resolve()
        candidates = list(dict.fromkeys([endpoint, self.fallback]))
        last_error = None
        for candidate in candidates:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                response = requests.post(candidate, json=payload, timeout=remaining, headers=self.headers(candidate))
                response.raise_for_status()
            except requests.RequestException as exc:
                last_error = exc
                self.logger.warning('LLM endpoint failed; trying configured fallback')
                if candidate == self.primary:
                    self._healthy = False
                    self._checked_at = time.monotonic()
                continue
            self.active_endpoint = candidate
            return response
        raise last_error or requests.Timeout('LLM request deadline exceeded')

    def model_info(self):
        endpoint = self.active_endpoint
        now = time.monotonic()
        if getattr(self, '_model_cache', None) and now - self._model_cache[0] < self.model_info_cache_seconds:
            if self._model_cache[1]['endpoint'] == endpoint:
                return self._model_cache[1]
        parsed = urlparse(endpoint)
        info = {'endpoint': endpoint}
        try:
            response = requests.get(urljoin(f'{parsed.scheme}://{parsed.netloc}/', 'v1/models'),
                                    headers=self.headers(endpoint), timeout=self.ping_timeout)
            response.raise_for_status()
            entries = response.json().get('data') or []
            if entries:
                entry = entries[0]
                info['model'] = entry.get('id') or entry.get('name')
                for key in ('meta', 'details'):
                    details = entry.get(key) or {}
                    if isinstance(details, dict) and isinstance(details.get('n_ctx'), int):
                        info['n_ctx'] = details['n_ctx']
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            self.logger.warning('LLM model metadata unavailable')
        self._model_cache = (now, info)
        return info
