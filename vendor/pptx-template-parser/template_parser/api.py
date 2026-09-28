"""Bounded retries for rejected requests; never switch models or paid endpoints."""
import json
import math
import sys
import time
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
import socket
import ssl


class APIError(RuntimeError):
    pass


def diagnostics(error, headers):
    """Never print raw provider payloads: they may echo prompts or credentials."""
    if not isinstance(error, dict):
        error = {}
    metadata = error.get("metadata", {}) if isinstance(error, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    lines = []
    for name in ("provider_name", "provider_code", "error_type", "limit_source", "reason"):
        value = metadata.get(name)
        if isinstance(value, (str, int)) and re.fullmatch(r"[\w .:/()\-]{1,100}", str(value)):
            lines.append(f"{name}={value}")
    # Interpret raw error privately; expose only fixed diagnostic categories.
    raw = metadata.get("raw", "")
    raw_text = (json.dumps(raw) if isinstance(raw, dict) else str(raw)).lower()
    text = str(error.get("message", "")).lower() + " " + raw_text
    if any(term in text for term in ("per-day", "per day", "daily")):
        lines.append("category=daily_limit")
    elif any(term in text for term in ("upstream", "temporarily rate", "capacity", "overloaded")):
        lines.append("category=upstream_capacity_or_rate_limit")
    elif any(term in text for term in ("per-minute", "per minute", "requests per minute")):
        lines.append("category=minute_limit")
    elif raw:
        lines.append("category=unclassified_provider_error (raw body withheld)")
    for name in ("Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        value = headers.get(name) if headers else None
        if value is not None and re.fullmatch(r"[\w ,:./+\-]{1,100}", str(value)):
            lines.append(f"{name}={value}")
    return "\n".join(lines)


def retry_delay(headers, attempt):
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = float("nan")
        if math.isfinite(seconds):
            return max(0, seconds)
    return min(60, 10 * 2 ** attempt)


def request_json(request, config, opener):
    retries = config.get("max_retries", 2)
    if type(retries) is not int or not 0 <= retries <= 10:
        raise ValueError("max_retries must be an integer from 0 to 10")
    for attempt in range(retries + 1):
        try:
            with opener(request, timeout=config.get("timeout_seconds", 180)) as response:
                return json.load(response)
        except HTTPError as exc:
            error = {}
            try:
                payload = json.loads(exc.read(16384).decode("utf-8", errors="replace"))
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                message = error.get("message", "") if isinstance(error, dict) else ""
            except (ValueError, OSError):
                message = ""
            finally:
                exc.close()
            message = str(message)
            extra = diagnostics(error, exc.headers)
            # A provider might echo credentials. Never display the authorization value.
            authorization = request.get_header("Authorization", "")
            if authorization:
                message = message.replace(authorization, "[REDACTED]")
                token = authorization.removeprefix("Bearer ")
                if token:
                    message = message.replace(token, "[REDACTED]")
                    extra = extra.replace(token, "[REDACTED]")
            message = " ".join(message.split())[:1000]
            detail = f"HTTP {exc.code}: {message or 'provider returned no JSON error message'}"
            if extra:
                detail += "\n" + extra
            if exc.code != 429:
                raise APIError(detail) from None
            delay = retry_delay(exc.headers, attempt)
            daily = "category=daily_limit" in extra or any(word in message.lower() for word in ("per day", "per-day", "daily", "quota"))
            if daily or delay > 60 or attempt == retries:
                hint = ("Daily quota may be exhausted; check the provider's limits/reset time."
                        if daily else "Check provider quota/capacity and retry later.")
                if delay > 60:
                    hint += f" Retry-After requests a wait of {math.ceil(delay)} seconds; no early retry was sent."
                raise APIError(f"{detail}\n{hint} Model and endpoint were not changed.") from None
            print(f"HTTP 429: request limited; retry {attempt + 1}/{retries} in {delay:g}s.", file=sys.stderr)
            time.sleep(delay)
        except (URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, socket.gaierror):
                detail = "DNS lookup failed (category=dns_error). Check name resolution, network and proxy/VPN settings."
            elif isinstance(reason, TimeoutError):
                detail = "Connection or response timed out (category=timeout)."
            elif isinstance(reason, ssl.SSLError):
                detail = "Secure connection failed (category=tls_error). Check system time and certificate configuration."
            else:
                detail = "Network connection failed (category=network_error). Check network and proxy/VPN settings."
            raise APIError(detail + " No automatic network retry was sent; request completion is not confirmed. Previous validated semantics were not replaced.") from None
