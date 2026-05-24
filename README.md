# coursive-watch

Watcher pour les billets de La Coursive (La Rochelle). Surveille
`https://la-coursive.notre-billetterie.com/billets?kld=2526` et envoie un mail
dès qu'une place se libère ou qu'un nouveau spectacle apparaît dans la liste.

## Comment ça marche

- GitHub Actions tourne `watch.py --once` toutes les 5 min (granularité mini sur
  Actions). Chrome headless rend la page, on extrait `(spectacle, date, statut)`
  pour chaque carte. Statuts pris directement dans la classe CSS du bouton :
  `valid` = vert (places dispo), `beware` = orange (dernières places),
  `warning` = rouge (complet / non vendu en ligne).
- L'état est committé dans `state.json` à chaque run. Le run suivant compare
  l'état courant à celui d'avant et envoie un mail HTML avec gros bouton CTA
  quand le rang augmente (ex. `warning → beware`, ou un spectacle apparaît avec
  des places).

## Mise en place (à faire une fois)

### 1. Pousser le repo sur GitHub

```bash
cd ~/coursive-watch
git init
git add .
git commit -m "init coursive-watch"
gh repo create coursive-watch --private --source=. --remote=origin --push
```

### 2. Configurer les secrets

Dans le repo GitHub, **Settings → Secrets and variables → Actions → New repository secret**.

**Push iPhone via ntfy.sh** (canal principal, gratuit) :

| Secret        | Valeur                                              |
|---------------|-----------------------------------------------------|
| `NTFY_TOPIC`  | `coursive-tante-kllk8tnnrylitq` (topic dédié, généré random) |

Le topic est public sur ntfy.sh — quiconque le devine peut s'abonner. C'est pour
ça qu'on utilise un nom random impossible à deviner. (Pas de données sensibles
dedans de toute façon, juste des noms de spectacles.)

**Email** (optionnel, en plus du push) :

| Secret      | Exemple                          | Notes                                                    |
|-------------|----------------------------------|----------------------------------------------------------|
| `SMTP_HOST` | `smtp.gmail.com`                 | n'importe quel SMTP marche                               |
| `SMTP_PORT` | `587`                            | STARTTLS                                                 |
| `SMTP_USER` | `tonadresse@gmail.com`           | l'adresse depuis laquelle on envoie                      |
| `SMTP_PASS` | `xxxx xxxx xxxx xxxx`            | **app password Gmail** (pas le mot de passe du compte)   |
| `MAIL_FROM` | `tonadresse@gmail.com`           | en-tête `From:`                                          |
| `MAIL_TO`   | `tante@exemple.fr,toi@exemple.fr`| destinataires (séparés par virgule)                      |

Pour Gmail : créer un app password via
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(2FA requis).

### Côté tante (iPhone)

1. Installer **ntfy** depuis l'App Store : <https://apps.apple.com/app/ntfy/id1625396347>
2. Ouvrir l'app → **Add subscription** (le `+` en haut à droite)
3. **Topic** : `coursive-tante-kllk8tnnrylitq` (le même que dans le secret `NTFY_TOPIC`)
4. **Service** : laisser `ntfy.sh` par défaut
5. C'est fini. À chaque place libérée, elle reçoit une notif push native.
   Le tap ouvre la billetterie directement.

Pour tester : aller sur <https://ntfy.sh/coursive-tante-kllk8tnnrylitq> et envoyer un message — elle doit recevoir la notif en ~1s.

### 3. Premier run (capture la baseline)

Dans l'onglet **Actions** du repo → **Coursive ticket watcher** → **Run workflow**.
Le premier run capture l'état initial sans envoyer de mail. Les runs suivants
(toutes les 5 min) compareront à la baseline.

## Tester en local

```bash
# une fois — capture baseline
python3 watch.py --once

# en continu (mode macOS : notif + son + ouvre la page si --open-browser)
python3 watch.py --interval 60 --open-browser

# remettre la baseline à zéro
python3 watch.py --reset
```

## Étendre

- Polling plus rapide : pas possible avec le cron natif Actions (5 min mini).
  Alternative : Vercel cron (1 min) ou Cloudflare Workers.
- Ajouter SMS : intégrer un `send_sms()` à côté de `send_email()` via Twilio /
  OVH SMS, déclenché par les mêmes events.
- Changer la billetterie surveillée : variable d'env `COURSIVE_URL` (et
  redéfinir la baseline avec `--reset`).
