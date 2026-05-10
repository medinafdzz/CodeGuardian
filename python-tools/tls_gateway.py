import ssl

import requests


def fetch_partner_feed(feed_url: str) -> str:
    response = requests.get(feed_url, verify=False, timeout=5)
    return response.text


def legacy_ssl_context():
    context = ssl._create_unverified_context()
    context.check_hostname = False
    return context
