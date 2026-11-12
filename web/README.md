# Xcode Pocket — mobilní editor

Adresář web/ obsahuje nového samostatného klienta pro iPhone. Nenahrazuje automaticky starší externí editor ChatGPT Sites, jehož zdrojový kód není v tomto repozitáři.

## Funkce
- Úpravy SwiftUI souborů, více souborů, lokální autosave, import/export JSON.
- GitHub API commit snímku do pocket-builds/UUID.json a spuštění Pocket iOS workflow.
- Trvalé UUID požadavku, SHA commitu a run ID přežijí obnovení stránky.
- Přísné párování běhu a identitního artefaktu zabraňuje záměně mezi projekty.
- GitHub Actions run obsahuje ZIP s unsigned .app a unsigned .ipa.
- Webový klient lze přidat na plochu z mobilního Safari, ale není nativní Xcode.

## Publikování

V GitHub repozitáři otevři Settings → Pages → Build and deployment → Source = GitHub Actions.

Workflow .github/workflows/pocket-pages.yml bude automaticky publikovat web/ při změnách na main. Po skutečně úspěšném deploymentu je očekávaná adresa:

https://happymistake64.github.io/Xcode-for-iphone/

Dokud deployment nepotvrdí GitHub Pages, nepovažuj tuto adresu za funkční.

## Přihlášení

Na https://github.com/settings/personal-access-tokens/new vytvoř fine-grained PAT pouze pro HappyMistake64/Xcode-for-iphone. Požadované oprávnění Contents: Read/Write a Actions: Read/Write. Nevkládej token do chatu ani do Swift souborů. Token se v editoru nikdy neukládá do localStorage; po reloadu je nutné jej znovu vložit.

Repozitář je veřejný! Snímky projektů commitnuté do pocket-builds/ jsou veřejně čitelné. Žádné klíče, osobní údaje ani hesla neposílej jako zdrojový kód.

## Přihlášení na iPhonu

V Safari vyplň token do pole **GitHub token** a klepni na **Ověřit a připojit** (nebo na klávesnici na **Go/Enter**). Stav se zobrazuje **hned pod tlačítkem**, nikoli až v sekci kompilace.

- **401 / Bad credentials:** zkontroluj správnost a platnost tokenu.
- **403 / read-only:** v nastavení fine-grained tokenu povol Contents: Read/Write a Actions: Read/Write pro repozitář HappyMistake64/Xcode-for-iphone.
- **404:** token nemá přístup ke zvolenému repozitáři nebo je špatně uveden.
- **Safari / network error:** zkontroluj internet, VPN či blokování obsahu a zkus znovu.

Nikomu neposílej token ani jeho screenshot. Po připojení aplikace token smaže z pole; v paměti stránky zůstane jen pro aktuální relaci. Po obnovení jej bude potřeba znovu vložit. Pokud se zobrazuje stará verze aplikace, načti web v Safari znovu, případně zavři a spusť ikonu z plochy. Lokální soubory projektů se tím nemažou.

## První build z iPhonu

1. Otevři HTTPS editor v Safari, vlož PAT a stiskni Připojit.
2. Uprav ContentView.swift, případně přidej další textový soubor.
3. Zadej Bundle ID a Sestavit iOS aplikaci.
4. Editor vytvoří unikátní UUID a commit snapshotu; uloží přesné source_sha.
5. Poté odešle workflow_dispatch. Historie zůstane zachována i při refreshi Safari.
6. Jakmile jsou běh a identitní artefakt ověřeny, otevři odkaz na GitHub Actions.
7. Stáhni pocket-ios-app-UUID a rozbal v Souborách. Získáš PocketApp-unsigned.ipa.
8. IPA je validní ZIP struktura Payload/PocketApp.app, ale bez podpisu ji iOS nenainstaluje.

Neopakuj dispatch naslepo při výpadku sítě; nejprve prověř existující běh.

## Podpis a instalace

Apple vyžaduje platný podpis, odpovídající provisioning a správnou distribuční cestu.

- Bezplatný Personal Team umožňuje krátkodobé testování přes Xcode; profil má sedmidenní platnost, ale toto řešení neumožňuje automaticky podepisovat pouze z iPhonu.
- Externí podpisové služby mohou podepsat vlastní IPA po registraci zařízení. Jde o službu třetí strany s možným poplatkem a rizikem revokace; žádné platby nebo registrace neprovádíme.
- Apple Developer Program podporuje standardní TestFlight a distribuci, ale jde o jinou cestu.
- Alternativní distribuce v EU neznamená možnost instalovat libovolný nepodepsaný IPA soubor.

Oficiální zdroje:
https://developer.apple.com/help/account/basics/about-your-developer-account
https://developer.apple.com/support/apps-in-the-eu
https://www.signulous.com/register

## Testy

Pro lokální statický web z kořene repozitáře:

    python3 -m http.server 8000 --directory web

Automatická validace:

    node --test tests/web-core.test.mjs
    python3 -m unittest discover -s tests -v

V GitHub Actions navíc běží skutečná kompilace na macOS a strukturální ověření nepodepsané IPA. Zdrojový kontrakt je v docs/RELIABLE_BUILDS.md.

K dokončení na skutečném iPhone 15 Pro zbývá ověřit Safari workflow po deployi, stažení artefaktu a samostatně podpis/instalaci/aktualizaci.
