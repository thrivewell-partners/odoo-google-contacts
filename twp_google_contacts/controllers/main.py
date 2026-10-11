import secrets

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..models.broker import BrokerError
from ..models.twp_google_contacts_account import state_message
from ..wizard.twp_google_contacts_connect import RETURN_PATH, SESSION_KEY


def sign_in_error(env, error):
    """What the user is told when Google's sign-in comes back without a connection."""
    return {
        "access_denied": env._("You chose not to give access on Google's screen. Nothing was "
                           "connected."),
        "scope_not_granted": env._(
            "Google's screen was approved without the Contacts box ticked, so nothing was "
            "connected and Google's access was given back. Press Connect again and tick the box "
            "beside the Contacts permission."),
        "admin_policy_enforced": state_message(env, "blocked"),
    }.get(error, env._("Google's sign-in did not finish (%(error)s). Press Connect again.",
                   error=error))


class GoogleContactsReturn(http.Controller):

    @http.route(RETURN_PATH, type="http", auth="user", methods=["GET"])
    def google_contacts_return(self, state=None, twc_code=None, twc_error=None, **kwargs):
        expected = request.session.pop(SESSION_KEY, None)
        if not (expected and state and secrets.compare_digest(expected, state)):
            return self._page(False, request.env._(
                "This sign-in did not start in this browser session, so Odoo did not accept it. "
                "Press Connect again from your preferences."))
        if twc_error:
            return self._page(False, sign_in_error(request.env, twc_error))
        try:
            account = request.env["twp.google.contacts.account"]._connect_finish(twc_code or "")
        except BrokerError:
            return self._page(False, request.env._(
                "The sign-in expired before Odoo could finish it. Press Connect again."))
        except UserError as e:
            return self._page(False, e.args[0])
        email = account.google_email or request.env._("your Google account")
        return self._page(True, request.env._("Google Contacts is connected to %(email)s.",
                                              email=email))

    def _page(self, ok, message):
        response = request.render("twp_google_contacts.return_page", {"ok": ok, "message": message})
        # The address carried a one-time code. It is spent, but it still goes nowhere else.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
