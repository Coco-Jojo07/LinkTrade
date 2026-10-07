# Changelog

## 1.0.0 - 2026-10-07

Premiere version de ViewLink Local.

- Indicateur Pine Script v5 `pine/viewlink_signal.pine` (4 plots : Entry, SL, TP, Type).
- Extension Chrome MV3 `extension/` : lecture de la legende, popup de confirmation,
  Settings dans `chrome.storage.local`, suivi du rapport d'execution.
- Bridge Python `bridge/server.py` (bibliotheque standard, 127.0.0.1 uniquement,
  cle API, validation stricte, TTL des signaux, 50 derniers rapports).
- Expert Advisor `mt5/ViewLinkEA.mq5` : polling WebRequest, parseur JSON plat,
  re-validation complete, execution CTrade, rapport systematique.

### Verifie automatiquement

- Bridge : 25 tests (`python -m pytest bridge`), tous au vert.
- Extension : `node --check` sur tous les fichiers JS, validation du manifest et
  des regles statiques (`tests/check_extension.py`).
- Extension chargee en mode developpeur dans Chromium (Playwright, Chromium 141)
  sans erreur JavaScript ; 8 scenarios de bout en bout contre le vrai bridge avec
  un EA simule (`tests/extension_e2e.js`).

### Non teste (a verifier manuellement)

- **DOM reel de TradingView** : les tests utilisent une imitation de la legende
  (`tests/fixtures/`). Les selecteurs n'ont pas ete verifies sur tradingview.com.
- **Compilation MetaEditor** : l'EA n'a PAS ete compile (MetaEditor indisponible
  dans l'environnement de developpement). L'absence d'erreur et d'avertissement
  n'est donc pas garantie ; seul l'equilibre des accolades/parentheses a ete controle.
- **Execution reelle sur MT5** : WebRequest vers 127.0.0.1, CTrade, modes de
  remplissage, expiration GTC/DAY, stops level et retcodes n'ont pas ete testes
  sur un terminal.
- **Indicateur Pine** : non compile dans TradingView ; en particulier l'affichage
  du plot Type uniquement dans la barre de statut et la Data Window, et le format
  des valeurs dans la legende.
- **Chrome stable** : le chargement a ete fait sous Chromium ; le vrai popup
  (ouvert depuis l'icone, et non comme onglet) et `activeTab` en conditions reelles
  n'ont pas ete testes.
- **Windows** : le bridge a ete teste sous Linux ; sous Windows, `chmod 600` est
  sans effet sur le fichier de cle.
