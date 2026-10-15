"""Verified HTTPS transport for deployment-configured private endpoints."""
import ssl
import urllib.request
from urllib.parse import urlsplit


def validate_https_endpoint(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https" and parsed.hostname and parsed.port != 0
            and not parsed.username and not parsed.password and not parsed.fragment
            and not any(c.isspace() or ord(c) < 32 for c in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Endpoint must use HTTPS without credentials or a fragment")
    return url


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials or trust redirected registry contents.
        raise OSError("endpoint redirects are not allowed")


def verified_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirects()
    )
