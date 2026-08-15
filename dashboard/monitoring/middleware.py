from django.conf import settings
from django.contrib.auth.views import redirect_to_login


class OptionalMonitoringLoginMiddleware:
    """Require login for dashboard pages when MONITOR_REQUIRE_LOGIN=true.

    /health/ stays public for service monitoring. /admin/ keeps Django's own
    authentication flow. Static assets and login/logout must also remain public.
    """

    PUBLIC_PREFIXES = (
        "/health/",
        "/login/",
        "/logout/",
        "/admin/",
        "/static/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            getattr(settings, "MONITOR_REQUIRE_LOGIN", False)
            and not request.user.is_authenticated
            and not request.path.startswith(self.PUBLIC_PREFIXES)
        ):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        return self.get_response(request)
