"""
Lidl Plus api
"""

import base64
import html
import logging
import re
from datetime import datetime, timedelta

import requests

from lidlplus.exceptions import (
    WebBrowserException,
    LoginError,
    LegalTermsException,
    MissingLogin,
)

try:
    from getuseragent import UserAgent
    from oic.oic import Client
    from oic.utils.authn.client import CLIENT_AUTHN_METHOD
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions
    from selenium.webdriver.support.ui import WebDriverWait
    from seleniumwire import webdriver
    from seleniumwire.utils import decode
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.firefox import GeckoDriverManager
    from webdriver_manager.core.os_manager import ChromeType
except ImportError:
    pass


class LidlPlusApi:
    """Lidl Plus api connector"""

    _CLIENT_ID = "LidlPlusNativeClient"
    _AUTH_API = "https://accounts.lidl.com"
    _TICKET_API = "https://tickets.lidlplus.com/api/v2"
    _COUPONS_API = "https://coupons.lidlplus.com/api"
    _COUPONS_V1_API = "https://coupons.lidlplus.com/app/api/"
    _PROFILE_API = "https://profile.lidlplus.com/profile/api"
    _APP = "com.lidlplus.app"
    _OS = "iOs"
    _TIMEOUT = 10

    def __init__(self, language, country, refresh_token=""):
        self._login_url = ""
        self._code_verifier = ""
        self._refresh_token = refresh_token
        self._expires = None
        self._token = ""
        self._country = country.upper()
        self._language = language.lower()

    @property
    def refresh_token(self):
        """Lidl Plus api refresh token"""
        return self._refresh_token

    @property
    def token(self):
        """Current token to query api"""
        return self._token

    def _register_oauth_client(self):
        if self._login_url:
            return self._login_url
        client = Client(client_authn_method=CLIENT_AUTHN_METHOD, client_id=self._CLIENT_ID)
        client.request_args["verify"] = False
        client.provider_config(self._AUTH_API)
        code_challenge, self._code_verifier = client.add_code_challenge()
        args = {
            "client_id": client.client_id,
            "response_type": "code",
            "scope": ["openid profile offline_access lpprofile lpapis"],
            "redirect_uri": f"{self._APP}://callback",
            **code_challenge,
        }
        auth_req = client.construct_AuthorizationRequest(request_args=args)
        self._login_url = auth_req.request(client.authorization_endpoint)
        return self._login_url

    def _init_chrome(self, headless=True):
        user_agent = UserAgent(self._OS.lower()).Random()
        logging.getLogger("WDM").setLevel(logging.NOTSET)
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("headless")
        options.add_experimental_option("mobileEmulation", {"userAgent": user_agent})
        for chrome_type in [ChromeType.GOOGLE, ChromeType.MSEDGE, ChromeType.CHROMIUM]:
            try:
                service = Service(ChromeDriverManager(chrome_type=chrome_type).install())
                return webdriver.Chrome(service=service, options=options)
            except AttributeError:
                continue
        raise WebBrowserException("Unable to find a suitable Chrome driver")

    def _init_firefox(self, headless=True):
        user_agent = UserAgent(self._OS.lower()).Random()
        logging.getLogger("WDM").setLevel(logging.NOTSET)
        options = webdriver.FirefoxOptions()
        if headless:
            options.headless = True
        profile = webdriver.FirefoxProfile()
        profile.set_preference("general.useragent.override", user_agent)
        return webdriver.Firefox(
            executable_path=GeckoDriverManager().install(),
            firefox_binary="/usr/bin/firefox",
            options=options,
            firefox_profile=profile,
        )

    def _get_browser(self, headless=True):
        try:
            return self._init_chrome(headless=headless)
        # pylint: disable=broad-except
        except Exception as exc1:
            try:
                return self._init_firefox(headless=headless)
            except Exception as exc2:
                raise WebBrowserException from exc1 and exc2

    def _auth(self, payload):
        default_secret = base64.b64encode(f"{self._CLIENT_ID}:secret".encode()).decode()
        headers = {
            "Authorization": f"Basic {default_secret}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        import os as _os
        verify = _os.environ.get("CURL_CA_BUNDLE") == ""
        kwargs = {"headers": headers, "data": payload, "timeout": self._TIMEOUT, "verify": not verify}
        response = requests.post(f"{self._AUTH_API}/connect/token", **kwargs).json()
        logging.debug("Token response: %s", response)
        if "expires_in" not in response:
            raise LoginError(f"Token exchange failed: {response}")
        self._expires = datetime.utcnow() + timedelta(seconds=response["expires_in"])
        self._token = response["access_token"]
        self._refresh_token = response["refresh_token"]

    def _renew_token(self):
        payload = {"refresh_token": self._refresh_token, "grant_type": "refresh_token"}
        return self._auth(payload)

    def _authorization_code(self, code):
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{self._APP}://callback",
            "code_verifier": self._code_verifier,
        }
        return self._auth(payload)

    @property
    def _register_link(self):
        args = {
            "Country": self._country,
            "language": f"{self._language}-{self._country}",
        }
        params = "&".join([f"{key}={value}" for key, value in args.items()])
        return f"{self._register_oauth_client()}&{params}"

    @staticmethod
    def _accept_legal_terms(browser, wait, accept=True):
        wait.until(expected_conditions.visibility_of_element_located((By.ID, "checkbox_Accepted"))).click()
        if not accept:
            title = browser.find_element(By.TAG_NAME, "h2").text
            raise LegalTermsException(title)
        browser.find_element(By.TAG_NAME, "button").click()

    def _parse_code(self, browser, wait, accept_legal_terms=True):
        for request in reversed(browser.requests):
            # Check the callback URL directly (new flow: browser navigates to app:// URI)
            if f"{self._APP}://callback" in request.url:
                if code := re.findall(r"code=([0-9A-Fa-f]+)", request.url):
                    return code[0]
            # Check the Location header of /connect responses (old flow)
            if f"{self._AUTH_API}/connect" not in request.url:
                continue
            if not request.response:
                continue
            location = request.response.headers.get("Location", "")
            if "legalTerms" in location:
                self._accept_legal_terms(browser, wait, accept=accept_legal_terms)
                return self._parse_code(browser, wait, False)
            if code := re.findall(r"code=([0-9A-Fa-f]+)", location):
                return code[0]
        return ""

    def _click(self, browser, button, request=""):
        del browser.requests
        browser.backend.storage.clear_requests()
        browser.find_element(*button).click()
        self._check_input_error(browser)
        if request and browser.wait_for_request(request, 10):
            self._check_input_error(browser)

    @staticmethod
    def _check_input_error(browser):
        if errors := browser.find_elements(By.CLASS_NAME, "input-error-message"):
            for error in errors:
                if error.text:
                    raise LoginError(error.text)

    def _check_login_error(self, browser):
        response = browser.wait_for_request(f"{self._AUTH_API}/Account/Login.*", 10).response
        # Cache so _check_2fa_auth can reuse it without a second wait_for_request call
        self._last_login_response = response
        body = html.unescape(decode(response.body, response.headers.get("Content-Encoding", "identity")).decode())
        if error := re.findall('app-errors="\\{[^:]*?:.(.*?).}', body):
            raise LoginError(error[0])
        # Detect server-side rate-limiting / overload page
        if re.search(r"T\xfalterhelt|overload|something went wrong", body, re.IGNORECASE):
            raise LoginError("Lidl server is overloaded or rate-limiting. Please wait a few minutes and try again.")

    def _check_2fa_auth(self, browser, wait, verify_mode="phone", verify_token_func=None):
        if verify_mode not in ["phone", "email"]:
            raise ValueError(f'Unknown 2fa-mode "{verify_mode}" - Only "phone" or "email" supported')
        # Reuse the cached response from _check_login_error to avoid consuming a second request
        response = getattr(self, "_last_login_response", None)
        location = (response.headers.get("Location") or "") if response else ""
        # New Lidl login page: no Location redirect on success — check the DOM instead
        if "/connect/authorize/callback" not in location:
            # If there's a VerificationCode field visible, 2FA is required
            verify_inputs = browser.find_elements(By.NAME, "VerificationCode")
            if not verify_inputs:
                return  # No 2FA needed — login went straight through
            try:
                element = wait.until(expected_conditions.visibility_of_element_located((By.CLASS_NAME, verify_mode)))
                element.find_element(By.TAG_NAME, "button").click()
            except Exception:
                pass  # Method selector not present; code input is already shown
            verify_code = verify_token_func()
            browser.find_element(By.NAME, "VerificationCode").send_keys(verify_code)
            self._click(browser, (By.CLASS_NAME, "role_next"))

    def login(self, email, password, **kwargs):
        """Simulate app auth"""
        browser = self._get_browser(headless=kwargs.get("headless", True))
        browser.get(self._register_link)
        wait = WebDriverWait(browser, 10)
        wait.until(expected_conditions.element_to_be_clickable((By.NAME, "input-email"))).send_keys(email)
        self._click(browser, (By.CSS_SELECTOR, '[data-testid="login-or-register-submit-button"]'))
        wait.until(expected_conditions.element_to_be_clickable((By.NAME, "Password"))).send_keys(password)
        self._click(browser, (By.CSS_SELECTOR, '[data-testid="button-primary"]'))
        self._check_login_error(browser)
        self._check_2fa_auth(
            browser,
            wait,
            kwargs.get("verify_mode", "phone"),
            kwargs.get("verify_token_func"),
        )
        # Wait for the authorization code to appear — either as a direct navigation
        # to the app callback URI, or as a Location header in a /connect response
        browser.wait_for_request(f"({self._APP}://callback|{self._AUTH_API}/connect/authorize/callback).*", 30)
        code = self._parse_code(browser, wait, accept_legal_terms=kwargs.get("accept_legal_terms", True))
        self._authorization_code(code)

    def _default_headers(self):
        if (not self._token and self._refresh_token) or datetime.utcnow() >= self._expires:
            self._renew_token()
        if not self._token:
            raise MissingLogin("You need to login!")
        return {
            "Authorization": f"Bearer {self._token}",
            "App-Version": "999.99.9",
            "Operating-System": self._OS,
            "App": "com.lidl.eci.lidl.plus",
            "Accept-Language": self._language,
        }

    def tickets(self, only_favorite=False):
        """
        Get a list of all tickets.

        :param onlyFavorite: A boolean value indicating whether to only retrieve favorite tickets.
            If set to True, only favorite tickets will be returned.
            If set to False (the default), all tickets will be retrieved.
        :type onlyFavorite: bool
        """
        url = f"{self._TICKET_API}/{self._country}/tickets"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        ticket = requests.get(f"{url}?pageNumber=1&onlyFavorite={only_favorite}", **kwargs).json()
        tickets = ticket["tickets"]
        for i in range(2, int(ticket["totalCount"] / ticket["size"] + 2)):
            tickets += requests.get(f"{url}?pageNumber={i}", **kwargs).json()["tickets"]
        return tickets

    def ticket(self, ticket_id):
        """Get full data of single ticket by id"""
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        url = f"{self._TICKET_API}/{self._country}/tickets"
        return requests.get(f"{url}/{ticket_id}", **kwargs).json()

    def coupon_promotions_v1(self):
        """Get list of all coupons API V1"""
        url = f"{self._COUPONS_V1_API}/v1/promotionslist"
        kwargs = {"headers": {**self._default_headers(), "Country": self._country}, "timeout": self._TIMEOUT}
        return requests.get(url, **kwargs).json()

    def activate_coupon_promotion_v1(self, promotion_id):
        """Activate single coupon by id API V1"""
        url = f"{self._COUPONS_V1_API}/v1/promotions/{promotion_id}/activation"
        kwargs = {"headers": {**self._default_headers(), "Country": self._country}, "timeout": self._TIMEOUT}
        return requests.post(url, **kwargs)

    def coupons(self):
        """Get list of all coupons"""
        url = f"{self._COUPONS_API}/v2/{self._country}"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        return requests.get(url, **kwargs).json()

    def activate_coupon(self, coupon_id):
        """Activate single coupon by id"""
        url = f"{self._COUPONS_API}/v1/{self._country}/{coupon_id}/activation"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        return requests.post(url, **kwargs).json()

    def deactivate_coupon(self, coupon_id):
        """Deactivate single coupon by id"""
        url = f"{self._COUPONS_API}/v1/{self._country}/{coupon_id}/activation"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        return requests.delete(url, **kwargs).json()

    def loyalty_id(self):
        """Get your loyalty ID"""
        url = f"{self._PROFILE_API}/v1/{self._country}/loyalty"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        response = requests.get(url, **kwargs)
        response.raise_for_status()
        return response.text
