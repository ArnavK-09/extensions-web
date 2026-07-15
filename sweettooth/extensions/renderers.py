from hashlib import sha256

from django.core.cache import cache
from django.db.models import F
from django.http import HttpResponseRedirect
from rest_framework import renderers, status

from .models import Extension, ExtensionVersion


def get_client_ip(request):
    """
    Return the client IP address. 
    The Nginx proxy in this infrastructure (openshift/docker/default.conf.template)
    uses the ngx_http_realip_module to securely parse X-Forwarded-For, strip trusted
    internal proxies, and assign the true client IP to $remote_addr.
    Therefore, REMOTE_ADDR is the only secure source of truth here.
    """
    return request.META.get("REMOTE_ADDR", "unknown")


class ExtensionVersionZipRenderer(renderers.BaseRenderer):
    media_type = "application/zip"
    format = "zip"
    render_style = "binary"

    DEDUP_TIMEOUT_SEC = 86400  # 24 hours
    RATE_LIMIT = 10
    RATE_LIMIT_WINDOW_SEC = 60  # 1 minute

    def render(self, data, accepted_media_type=None, renderer_context=None):
        instance = data.serializer.instance if hasattr(data, "serializer") else None
        if not isinstance(instance, ExtensionVersion):
            renderer_context["response"].status_code = status.HTTP_406_NOT_ACCEPTABLE
            return b""

        request = renderer_context.get("request")
        if request is None:
            return b""

        client_ip = get_client_ip(request)
        
        rate_key = f"dl_rate:{client_ip}:{instance.extension_id}"
        cache.add(rate_key, 0, self.RATE_LIMIT_WINDOW_SEC)
        rate_count = cache.incr(rate_key)

        if rate_count > self.RATE_LIMIT:
            renderer_context["response"].status_code = status.HTTP_429_TOO_MANY_REQUESTS
            return b""

        user_agent = request.META.get("HTTP_USER_AGENT", "")
        fingerprint = sha256(
            f"{client_ip}:{user_agent}:{instance.extension_id}".encode()
        ).hexdigest()
        
        dedup_key = f"dl_dedup:{fingerprint}"
        if cache.add(dedup_key, 1, self.DEDUP_TIMEOUT_SEC):
            Extension.objects.filter(pk=instance.extension_id).update(
                downloads=F("downloads") + 1
            )

        response = renderer_context["response"]
        response.status_code = status.HTTP_302_FOUND
        response["Location"] = instance.source.url

        return b""
