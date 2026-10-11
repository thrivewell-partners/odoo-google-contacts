"""Calls to ThriveWell Connect, the service that holds the Google sign-in.

The module never sees the Google app's secret or a refresh token. It asks ThriveWell Connect for a
sign-in link, redeems the one-time code that comes back, and fetches one-hour access tokens.
"""
import logging

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CONNECTOR = "google-contacts"
DEFAULT_URL = "https://connect.thrivewellag.com"
TIMEOUT = 20


class BrokerError(Exception):
    """ThriveWell Connect answered with an error the caller is expected to handle."""

    def __init__(self, status, error):
        super().__init__(error)
        self.status = status
        self.error = error


def call(env, path, payload):
    """POST `payload` to ThriveWell Connect and return its JSON answer.

    Raises UserError when the service cannot be used at all (no key, a refused key, unreachable),
    and BrokerError for an answer the caller decides about.
    """
    params = env["ir.config_parameter"].sudo()
    url = (params.get_param("twp_google_contacts.broker_url") or DEFAULT_URL).rstrip("/")
    key = params.get_param("twp_google_contacts.broker_key")
    if not key:
        raise UserError(env._(
            "Google Contacts is not set up yet. An administrator needs to enter the ThriveWell "
            "Connect key in Settings."))
    try:
        response = requests.post(
            url + path, json=payload, timeout=TIMEOUT,
            headers={"Authorization": "Bearer " + key})
    except requests.RequestException as e:
        _logger.warning("ThriveWell Connect unreachable at %s: %s", url, type(e).__name__)
        raise UserError(env._(
            "ThriveWell Connect could not be reached. Nothing has changed; try again in a few "
            "minutes.")) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 401:
        raise UserError(env._(
            "ThriveWell Connect refused this database's key. An administrator needs to check the "
            "key in Settings."))
    if response.status_code != 200:
        error = body.get("error") or "http_%s" % response.status_code
        raise BrokerError(response.status_code, error)
    return body
