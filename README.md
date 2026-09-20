# Bot de relance des adhésions HelloAsso

HelloAsso ne propose ni reconduction tacite ni mail de relance pour les
adhésions annuelles. Ce script comble ce manque : il interroge l'API HelloAsso
une fois par jour, détecte les adhésions proches de leur échéance et envoie à
chaque adhérent un mail personnalisé l'invitant à renouveler.

Écrit en Python, sans autre dépendance que `requests` (SQLite et SMTP viennent
de la bibliothèque standard), conteneurisable, prévu pour tourner via `cron`.

---

## 1. Ce que dit réellement l'API HelloAsso (vérifié)

Ce point était l'inconnue principale du cahier des charges. Vérification faite
sur le SDK officiel généré depuis l'OpenAPI v5
([HelloAsso/helloasso-python](https://github.com/HelloAsso/helloasso-python)) :

**Authentification.** OAuth2, grant `client_credentials`, sur
`POST https://api.helloasso.com/oauth2/token` — endpoint **à la racine, pas
sous `/v5`**. La réponse contient `access_token`, `refresh_token` et
`expires_in` (1800 s). Le script rafraîchit le token automatiquement.

**Adhésions.** Elles sont exposées comme des *items* de commande :
`GET /v5/organizations/{slug}/items?tierTypes=Membership&itemStates=Processed&itemStates=Registered`.
Le token doit porter le rôle `OrganizationAdmin` et le privilège
`AccessTransactions`.

**Le point clé — il n'y a pas de date de fin d'adhésion sur l'item.**
Le modèle `Item` expose `order.date` (date de la commande), `payer`
(dont l'e-mail), `user` (prénom/nom de l'adhérent), `name`, `state`,
`tierDescription` et `customFields` — **aucune échéance**. La date de fin doit
donc être **recalculée**.

**La règle d'échéance est portée par le formulaire.**
`GET /v5/organizations/{slug}/forms/Membership/{formSlug}/public` renvoie un
champ `validityType` prenant trois valeurs :

| `validityType` | Signification | Calcul appliqué par le script |
|---|---|---|
| `MovingYear` | année glissante | `date de commande + RELANCE_MEMBERSHIP_DURATION_DAYS` (365 j par défaut) |
| `Custom` | période fixe définie sur le formulaire | `endDate` du formulaire (identique pour tous) |
| `Illimited` | sans échéance | jamais relancé |

Le script lit `validityType` **automatiquement** pour chaque formulaire
d'adhésion (un appel API par formulaire, mis en cache) : vous n'avez donc
normalement rien à renseigner sur la configuration de vos adhésions. Si un
formulaire est illisible (droits, formulaire supprimé), le repli est l'année
glissante, et c'est signalé dans les logs.

> **À vérifier au premier lancement** — lancez `--dry-run` et contrôlez dans
> les logs la ligne `Formulaire <slug> : validityType=...`. Si elle affiche
> `Custom` avec une `endDate` qui ne correspond pas à votre pratique réelle,
> ou si vos adhésions suivent l'année civile sans que le formulaire le dise,
> ajustez `RELANCE_MEMBERSHIP_DURATION_DAYS`.

### Adresse e-mail de l'adhérent

HelloAsso place l'adresse sur le **payeur** (`payer.email`) ; l'objet `user`
ne contient que prénom et nom. Quand une adhésion est payée pour un tiers, le
script cherche en repli une adresse dans les champs personnalisés de l'item
(`withDetails=true`). Une adhésion sans adresse exploitable est ignorée et
journalisée en `WARNING`.

---

## 2. Installation

```bash
git clone <ce dépôt> /srv/relance-adhesions
cd /srv/relance-adhesions

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env          # le fichier contient des secrets
$EDITOR .env
```

Les identifiants HelloAsso s'obtiennent dans l'espace association :
**Intégrations → API** → création d'un client (`client_id` / `client_secret`).

`.env` est listé dans `.gitignore` : aucun secret n'est versionné, et aucun
n'est écrit en dur dans le code.

---

## 3. Test avant mise en production

```bash
# Simulation : aucun mail n'est envoyé, tout est journalisé.
.venv/bin/python -m relance_adhesions --dry-run

# Simulation + écriture des mails rendus sur disque, pour les relire.
.venv/bin/python -m relance_adhesions --dump-dir ./apercu-mails

# Fenêtre élargie pour vérifier que des adhésions sont bien détectées.
.venv/bin/python -m relance_adhesions --dry-run --days-before 120 --log-level DEBUG

# Se placer à une date donnée (rejeu / contrôle d'un calcul d'échéance).
.venv/bin/python -m relance_adhesions --dry-run --today 2026-03-01
```

### Qui serait relancé ?

Le dry-run affiche la liste nominative des destinataires retenus, avec pour
chacun l'échéance calculée, le `validityType` appliqué et le formulaire
d'origine — de quoi vérifier d'un coup d'œil que les dates sont justes :

```
Formulaire adhesion : validityType=MovingYear, endDate=None
3 items d'adhésion analysés, 3 exploitables
Fenêtre de relance : échéance entre 2026-09-25 et 2026-10-10 → 2 adhésion(s) concernée(s)
Destinataires retenus :
  · Claire Martin        claire.martin@example.org   échéance 2026-09-28  (MovingYear, formulaire « adhesion »)
  · Jean Dupont          jean.dupont@example.org     échéance 2026-10-02  (MovingYear, formulaire « adhesion »)
Bilan : 3 adhésion(s) analysée(s), 2 dans la fenêtre, 2 relance(s) simulée(s), 0 erreur(s)
```

### À quoi ressemblent les mails ?

`--dump-dir <dossier>` écrit, pour chaque destinataire, le mail réellement
rendu — avec son prénom, sa date d'échéance, son tarif :

```
apercu-mails/
  001-claire.martin@example.org.eml    # message complet (en-têtes compris)
  001-claire.martin@example.org.txt    # version texte seule
  001-claire.martin@example.org.html   # version HTML, à ouvrir au navigateur
  002-jean.dupont@example.org.eml
  ...
```

Le `.eml` s'ouvre dans n'importe quel client mail (Thunderbird, Apple Mail,
Outlook) et montre exactement ce que recevra la personne. Le `.html` s'ouvre
au navigateur pour contrôler la mise en forme.

`--dump-dir` **force le mode simulation** : impossible d'envoyer quoi que ce
soit par mégarde en voulant relire les textes.

En dry-run, **rien n'est enregistré dans la base anti-doublon** : vous pouvez
rejouer autant de fois que nécessaire sans « consommer » les relances.

Premier envoi réel recommandé : mettez votre propre adresse dans `MAIL_BCC`
et abaissez `MAX_EMAILS_PER_RUN` (par exemple à 5) le temps de valider.

### Tests automatisés

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

Les tests couvrent le calcul des échéances, la déduplication, le rendu des
mails et le client HTTP (session simulée, aucun appel réseau).

---

## 4. Mise en production (cron)

```cron
0 9 * * * cd /srv/relance-adhesions && /srv/relance-adhesions/.venv/bin/python -m relance_adhesions >> /var/log/relance-adhesions.cron.log 2>&1
```

Voir `crontab.example` pour la variante Docker et un contrôle hebdomadaire en
simulation.

### Docker

```bash
docker build -t relance-adhesions .
docker run --rm --env-file .env \
  -v /srv/relance-adhesions/data:/app/data \
  -v /srv/relance-adhesions/logs:/app/logs \
  relance-adhesions --dry-run
```

L'image ne contient aucun secret : la configuration est injectée au lancement.
Les volumes `data/` (base anti-doublon) et `logs/` **doivent** être persistés,
sinon l'anti-doublon repart de zéro à chaque exécution.

---

## 5. Fonctionnement détaillé

1. **Configuration** — variables d'environnement, éventuellement via `.env`.
2. **Authentification** — OAuth2 `client_credentials`, refresh automatique.
3. **Récupération** — tous les items `Membership` en état `Processed` ou
   `Registered`, pagination par `continuationToken`, limitée aux
   `RELANCE_MAX_MEMBERSHIP_AGE_DAYS` derniers jours.
4. **Calcul de l'échéance** — selon le `validityType` du formulaire (§1).
5. **Déduplication par personne** — seule l'adhésion la plus récente de chaque
   adresse est retenue : une personne adhérente depuis trois ans ne reçoit pas
   trois relances.
6. **Deux étapes de relance**, à fenêtres disjointes :
   * **préavis** — échéance dans `]aujourd'hui ; aujourd'hui + RELANCE_DAYS_BEFORE_EXPIRY]`
   * **jour d'expiration** — échéance dans `[aujourd'hui − RELANCE_DAYS_AFTER_EXPIRY ; aujourd'hui]`

   Chaque personne reçoit au plus un mail par étape et par échéance, et jamais
   les deux le même jour. Chaque étape a ses propres templates et son propre
   sujet.
7. **Anti-doublon** — base SQLite (`STATE_DB`), clé `item_id:date_de_fin:étape`.
   L'échéance dans la clé garantit qu'après renouvellement la personne sera
   bien relancée l'année suivante ; l'étape permet au second mail de partir
   malgré le premier.
8. **Envoi** — SMTP, une connexion réutilisée, message `text/plain` +
   `text/html`. La ligne n'est marquée comme envoyée qu'après succès réel.
9. **Journalisation** — console + fichier avec rotation, plus une table
   `executions` en base (analysées / sélectionnées / envoyées / erreurs).

### Garde-fous

* `MAX_EMAILS_PER_RUN` : au-delà de ce nombre de relances à envoyer,
  l'exécution **s'interrompt sans rien envoyer** et journalise une erreur.
  C'est la protection contre un mauvais calcul d'échéance qui déclencherait
  une campagne d'e-mailing involontaire.
* `RELANCE_MAX_MEMBERSHIP_AGE_DAYS` : borne l'historique analysé, ce qui évite
  un rattrapage massif au premier lancement.
* Échecs API (réseau, 5xx, 429) : réessais avec back-off exponentiel et respect
  de `Retry-After`. Une panne HelloAsso fait sortir le script en erreur, sans
  envoi partiel silencieux.

### Codes de sortie

| Code | Signification |
|---|---|
| 0 | succès |
| 1 | au moins un envoi en échec, ou erreur inattendue |
| 2 | configuration invalide ou identifiants HelloAsso refusés |
| 3 | API HelloAsso indisponible |
| 4 | garde-fou `MAX_EMAILS_PER_RUN` déclenché |
| 5 | erreur d'envoi (connexion SMTP, template introuvable) |

---

## 6. Personnaliser le mail

Les templates sont des fichiers séparés du code, éditables sans toucher au
Python. Un jeu par étape de relance :

| Fichier | Étape | Obligatoire |
|---|---|---|
| `templates/relance.txt` | préavis, texte | oui |
| `templates/relance.html` | préavis, HTML | non |
| `templates/relance-expiration.txt` | jour d'expiration, texte | non |
| `templates/relance-expiration.html` | jour d'expiration, HTML | non |

Les templates de l'étape « expiration » sont facultatifs : s'ils sont absents,
ceux du préavis sont réutilisés. Videz `TEMPLATE_HTML` pour n'envoyer que du
texte.

Substitution de variables `$nom_de_variable` (syntaxe `string.Template`) :

| Variable | Contenu |
|---|---|
| `$nom_complet` | prénom + nom, avec repli neutre si l'API ne fournit rien |
| `$prenom`, `$nom` | prénom et nom séparés |
| `$email` | adresse de l'adhérent |
| `$date_fin` | date d'échéance (JJ/MM/AAAA) |
| `$date_adhesion` | date de la commande (JJ/MM/AAAA) |
| `$formule` | libellé du tarif d'adhésion |
| `$association` | `ASSOCIATION_NAME` |
| `$lien_adhesion` | `RENEWAL_URL` |

Ces variables sont également disponibles dans `MAIL_SUBJECT`.

---

## 7. Changer de canal d'envoi

Le canal par défaut est SMTP, ce qui couvre aussi bien le serveur de
l'association que les relais transactionnels (Brevo, Mailjet, Sendgrid), qui
exposent tous un point d'entrée SMTP. Les identifiants sont lus depuis les
variables `SMTP_*` — c'est leur unique point d'insertion.

Pour passer à une API HTTP plutôt qu'à SMTP, écrivez dans `mailer.py` une
classe exposant la même méthode `send(membership, rendered_mail)` et
substituez-la à `SmtpMailer` dans `cli.py` (`run()`).

---

## 8. Structure du projet

```
relance_adhesions/
  config.py      # chargement / validation de la configuration
  helloasso.py   # client API v5 : OAuth2, pagination, retries, rate limiting
  membership.py  # normalisation des items et calcul des échéances
  state.py       # anti-doublon et journal d'exécution (SQLite)
  mailer.py      # rendu des templates et envoi SMTP (+ mode dry-run)
  cli.py         # orchestration et interface en ligne de commande
templates/       # corps des mails, éditables
tests/           # tests unitaires, sans accès réseau
.env.example     # configuration d'exemple
crontab.example  # lignes de crontab types
Dockerfile
```

---

## 9. Points restant à confirmer côté association

1. **Le `validityType` réel de vos formulaires** — lisible dans les logs du
   premier `--dry-run` (§1). C'est la seule donnée qui conditionne la justesse
   des échéances.
2. **Le canal d'envoi** — SMTP de l'association ou relais transactionnel ;
   dans les deux cas, seules les variables `SMTP_*` changent.
3. **L'adresse d'expédition** (`MAIL_FROM`) doit être autorisée à émettre pour
   votre domaine (SPF / DKIM), sans quoi les relances partiront en spam.
