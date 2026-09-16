"""Client minimal de l'API HelloAsso v5.

Points vérifiés dans la documentation / les SDK officiels HelloAsso
(https://github.com/HelloAsso/helloasso-python, généré depuis l'OpenAPI v5) :

* Authentification OAuth2 `client_credentials` sur `POST https://api.helloasso.com/oauth2/token`
  (l'endpoint de token est à la racine, *pas* sous `/v5`). La réponse contient
  `access_token`, `refresh_token`, `expires_in` (1800 s) et `token_type`.
  Un `grant_type=refresh_token` permet de renouveler sans réauthentifier.
* Les adhésions sont exposées comme des *items* de commande :
  `GET /v5/organizations/{organizationSlug}/items`
  filtrables par `tierTypes=Membership` et `itemStates=Processed|Registered`.
  Rôle requis sur le token : `OrganizationAdmin`, privilège `AccessTransactions`.
* Le modèle d'item **ne contient aucune date de fin d'adhésion**. Il expose
  `order.date` (date de la commande), `payer`, `user`, `name`, `state`,
  `customFields` (avec `withDetails=true`). La date d'échéance doit donc être
  calculée — voir `membership.py`.
* La règle d'échéance est portée par le formulaire :
  `GET /v5/organizations/{slug}/forms/{formType}/{formSlug}/public` renvoie
  `validityType` ∈ {`MovingYear`, `Custom`, `Illimited`} et, pour `Custom`,
  `endDate` (fin de l'activité / de la période d'adhésion).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

# États d'item considérés comme des adhésions valides.
VALID_ITEM_STATES = ("Processed", "Registered")

# Types d'échéance renvoyés par HelloAsso pour un formulaire d'adhésion.
VALIDITY_MOVING_YEAR = "MovingYear"
VALIDITY_CUSTOM = "Custom"
VALIDITY_ILLIMITED = "Illimited"


class HelloAssoError(Exception):
    """Erreur d'appel à l'API HelloAsso."""


class HelloAssoAuthError(HelloAssoError):
    """Identifiants refusés par HelloAsso."""


class HelloAssoClient:
    """Client HTTP avec gestion du token, des erreurs et du rate limiting."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_base: str = "https://api.helloasso.com/v5",
        auth_base: str = "https://api.helloasso.com",
        timeout: int = 30,
        max_retries: int = 4,
        session: requests.Session | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.auth_base = auth_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0.0

    # -- Authentification ---------------------------------------------------

    @property
    def _token_url(self) -> str:
        return f"{self.auth_base}/oauth2/token"

    def authenticate(self) -> None:
        """Obtient un couple access/refresh token (grant client_credentials)."""
        self._request_token(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        logger.info("Authentification HelloAsso réussie")

    def refresh(self) -> None:
        """Renouvelle le token. Retombe sur une authentification complète si besoin."""
        if not self._refresh_token:
            self.authenticate()
            return
        try:
            self._request_token(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "refresh_token": self._refresh_token,
                }
            )
            logger.debug("Token HelloAsso rafraîchi")
        except HelloAssoError:
            logger.warning("Échec du refresh token, nouvelle authentification complète")
            self.authenticate()

    def _request_token(self, payload: dict[str, str]) -> None:
        try:
            response = self.session.post(
                self._token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise HelloAssoError(f"Connexion impossible à {self._token_url} : {exc}") from exc

        if response.status_code in (400, 401, 403):
            raise HelloAssoAuthError(
                f"Authentification refusée ({response.status_code}) : {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise HelloAssoError(
                f"Erreur d'authentification HTTP {response.status_code} : {response.text[:300]}"
            )

        data = response.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        # Marge de 60 s pour ne pas utiliser un token qui expire en vol.
        self._token_expiry = time.time() + int(data.get("expires_in", 1800)) - 60

    def _ensure_token(self) -> None:
        if self._access_token is None or time.time() >= self._token_expiry:
            if self._access_token is None:
                self.authenticate()
            else:
                self.refresh()

    # -- Appels génériques ---------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET authentifié avec retries exponentiels (réseau, 429, 5xx)."""
        url = f"{self.api_base}/{path.lstrip('/')}"
        delay = 2.0
        last_error: str = ""

        for attempt in range(1, self.max_retries + 1):
            self._ensure_token()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"erreur réseau : {exc}"
                logger.warning("GET %s (essai %s/%s) : %s", url, attempt, self.max_retries, exc)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 401:
                # Token invalidé côté serveur : on force un renouvellement.
                logger.info("401 sur %s, renouvellement du token", url)
                self._access_token = None
                last_error = "token refusé (401)"
                continue

            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After") or delay)
                logger.warning("Rate limit HelloAsso, attente de %.0f s", wait)
                time.sleep(wait)
                delay *= 2
                last_error = "rate limit (429)"
                continue

            if response.status_code >= 500:
                last_error = f"erreur serveur {response.status_code}"
                logger.warning("GET %s : %s, nouvel essai dans %.0f s", url, last_error, delay)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code >= 400:
                # 403/404 : inutile de réessayer, la requête est fautive.
                raise HelloAssoError(
                    f"GET {url} → HTTP {response.status_code} : {response.text[:300]}"
                )

            return response.json()

        raise HelloAssoError(f"GET {url} a échoué après {self.max_retries} essais ({last_error})")

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Itère sur toutes les pages d'un endpoint paginé HelloAsso.

        HelloAsso renvoie `{"data": [...], "pagination": {"pageIndex", "totalPages",
        "continuationToken", ...}}`. On privilégie le `continuationToken`, plus
        fiable que l'index de page quand des données sont ajoutées pendant la
        pagination, avec repli sur `pageIndex`.
        """
        page_params = dict(params)
        seen_tokens: set[str] = set()

        while True:
            payload = self.get(path, page_params)
            for record in payload.get("data") or []:
                yield record

            pagination = payload.get("pagination") or {}
            token = pagination.get("continuationToken")
            page_index = pagination.get("pageIndex") or 1
            total_pages = pagination.get("totalPages") or 1

            if page_index >= total_pages:
                return
            if token and token not in seen_tokens:
                seen_tokens.add(token)
                page_params["continuationToken"] = token
            else:
                page_params.pop("continuationToken", None)
                page_params["pageIndex"] = page_index + 1

    # -- Endpoints métier ----------------------------------------------------

    def iter_membership_items(
        self,
        organization_slug: str,
        date_from: str | None = None,
        date_to: str | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Liste les items d'adhésion valides de l'organisation.

        `date_from` / `date_to` sont des dates ISO 8601 filtrant sur la date de
        commande. `withDetails=true` ramène les champs personnalisés (utiles si
        l'e-mail de l'adhérent y est saisi plutôt que sur le payeur).
        """
        params: dict[str, Any] = {
            "tierTypes": ["Membership"],
            "itemStates": list(VALID_ITEM_STATES),
            "withDetails": "true",
            "pageSize": page_size,
            "pageIndex": 1,
            "sortField": "Date",
            "sortOrder": "Asc",
        }
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        yield from self._paginate(f"organizations/{organization_slug}/items", params)

    def list_membership_forms(self, organization_slug: str) -> list[dict[str, Any]]:
        """Liste les formulaires d'adhésion (actifs ou non) de l'organisation."""
        params = {
            "formTypes": ["Membership"],
            "pageSize": 100,
            "pageIndex": 1,
        }
        return list(self._paginate(f"organizations/{organization_slug}/forms", params))

    def get_form_public(
        self, organization_slug: str, form_type: str, form_slug: str
    ) -> dict[str, Any]:
        """Détail public d'un formulaire (contient `validityType` et `endDate`)."""
        return self.get(
            f"organizations/{organization_slug}/forms/{form_type}/{form_slug}/public"
        )
