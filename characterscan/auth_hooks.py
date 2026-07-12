"""Hook into Alliance Auth"""

# Django
from django.utils.translation import gettext_lazy as _

# Alliance Auth
from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

# AA CharacterScan App
from characterscan import urls


class CharacterScanMenuItem(MenuItemHook):
    """This class ensures only authorized users will see the menu entry"""

    def __init__(self):
        # setup menu entry for sidebar
        MenuItemHook.__init__(
            self,
            _("Character Scan"),
            "fas fa-cube fa-fw",
            "characterscan:index",
            navactive=["characterscan:"],
        )

    def render(self, request):
        """Render the menu item"""

        if request.user.has_perm("characterscan.basic_access"):
            # Recruiters zien een badge met het aantal nieuwe aanmeldingen.
            if request.user.has_perm("characterscan.recruiter"):
                from .models import Recruit

                self.count = Recruit.objects.filter(status=Recruit.Status.NEW).count() or None
            return MenuItemHook.render(self, request)

        return ""


@hooks.register("menu_item_hook")
def register_menu():
    """Register the menu item"""

    return CharacterScanMenuItem()


@hooks.register("url_hook")
def register_urls():
    """Register app urls"""

    return UrlHook(urls, "characterscan", r"^characterscan/")


@hooks.register("charlink")
def register_charlink_hook():
    """Koppel Character Scan aan CharLink (recruit- en standings-scopes)."""
    return "characterscan.charlink_hook"
