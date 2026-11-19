"""Registration and runtime logic tests for the Lovelace POD card."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.edistribuzione import lovelace_card


def test_il_file_della_card_esiste_e_definisce_la_card():
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert 'const CARD_TYPE = "edistribuzione-pod-card"' in contenuto
    assert "registro.define(CARD_TYPE" in contenuto


def test_la_card_aspetta_il_registro_definitivo_di_home_assistant():
    """Regression seen on HA 2026.2: extra modules can run before the
    frontend installs its custom element registry. An immediate define ends
    up in the wrong registry and the card is unknown ("Configuration
    error")."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert 'whenDefined("home-assistant").then(registraElementi)' in contenuto
    assert "customElements.define(CARD_TYPE" not in contenuto


async def test_senza_http_non_registra_nulla(hass):
    hass.http = None
    await lovelace_card.async_registra_card(hass)
    assert not hass.data.get(lovelace_card._CHIAVE_REGISTRATA)


async def test_registra_percorso_statico_e_modulo_una_sola_volta(hass):
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    hass.config.components.add("frontend")

    with patch("homeassistant.components.frontend.add_extra_js_url") as add_url:
        await lovelace_card.async_registra_card(hass)
        await lovelace_card.async_registra_card(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    (config,) = hass.http.async_register_static_paths.await_args.args[0]
    assert config.url_path == lovelace_card.URL_STATICO
    assert config.path == str(lovelace_card.PERCORSO_FILE)
    add_url.assert_called_once_with(
        hass, f"{lovelace_card.URL_STATICO}?v={lovelace_card.impronta_file()}"
    )


def test_l_impronta_cambia_col_contenuto_della_card(tmp_path):
    """The card is cached for 31 days: ?v= must change on every change to
    the file, even without bumping the manifest version."""
    card = tmp_path / "card.js"
    card.write_text("console.log(1);", encoding="utf-8")
    prima = lovelace_card.impronta_file(card)
    card.write_text("console.log(2);", encoding="utf-8")
    assert lovelace_card.impronta_file(card) != prima
    assert len(prima) == 12


def test_testi_della_card_nelle_due_lingue():
    """Every card string exists in Italian and English, as in translations/."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    blocco = contenuto[contenuto.index("const STRINGS = {") : contenuto.index("\n};\n")]
    it, en = blocco.split("\n  en: {")
    chiavi = lambda testo: set(re.findall(r"^    (\w+):", testo, re.MULTILINE))  # noqa: E731
    assert chiavi(it) == chiavi(en)
    assert {"empty_title", "history_text", "fetch_history"} <= chiavi(it)


def test_la_card_porta_all_azione_recupera_storico():
    """The buttons go through the "My Home Assistant" redirects, which point
    to the right page on every version."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert "/_my_redirect/developer_call_service?service=edistribuzione.recupera_storico" in contenuto
    assert "/_my_redirect/config_flow_start?domain=edistribuzione" in contenuto


def test_la_card_legge_l_immissione_per_fascia():
    """Issue #7: the card reads the injection band series written by
    statistics.py, with the same naming."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    for fascia in ("f1", "f2", "f3"):
        assert f"_energia_immessa_{fascia}`" in contenuto


def test_la_card_ha_l_opzione_decimals():
    """Issue #12: decimals (0 to 3) fixes the digits of every value the card
    formats, the chart axis aside, and the visual editor has a field for it."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert "const MAX_DECIMALI = 3;" in contenuto
    assert "cifre ?? this._config.decimals ??" in contenuto
    assert '{ name: "decimals", selector: { number: { min: 0, max: MAX_DECIMALI' in contenuto


def test_versione_card_allineata_al_manifest_e_senza_trattini_lunghi():
    manifest = json.loads(
        (Path(lovelace_card.PERCORSO_FILE).parent.parent / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert f'const VERSION = "{manifest["version"]}";' in contenuto
    assert not re.search("[\u2013\u2014]", contenuto)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_logica_js_della_card_in_node():
    """Execute the real card JS in a Node vm context to verify F1-F11 fixes."""
    card_path = str(lovelace_card.PERCORSO_FILE)
    syntax = subprocess.run(["node", "--check", card_path], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    script = f"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const defined = new Map();
class FakeElement {{
  constructor() {{ this.listeners = {{}}; this.dataset = {{}}; this.style = {{ setProperty() {{}} }}; }}
  attachShadow() {{ this.shadowRoot = {{ innerHTML: "", querySelector: () => null, querySelectorAll: () => [] }}; return this.shadowRoot; }}
  addEventListener(ev, fn) {{ this.listeners[ev] = fn; }}
  appendChild(child) {{ this.child = child; }}
  dispatchEvent() {{}}
}}
const doc = {{
  addEventListener() {{}},
  removeEventListener() {{}},
  querySelector: () => null,
  createElement: (tag) => {{
    const el = new FakeElement();
    el.tagName = tag;
    return el;
  }},
}};
const win = {{
  customElements: {{
    get: (name) => (name === "home-assistant" ? {{}} : defined.get(name)),
    define: (name, cls) => defined.set(name, cls),
    whenDefined: () => Promise.resolve(),
  }},
  addEventListener() {{}},
  dispatchEvent() {{}},
  localStorage: {{ getItem: () => null, setItem() {{}} }},
}};
const ctx = vm.createContext({{
  window: win,
  document: doc,
  HTMLElement: FakeElement,
  CustomEvent: class {{ constructor(t, d) {{ this.type = t; this.detail = d?.detail; }} }},
  ResizeObserver: class {{ observe() {{}} disconnect() {{}} }},
  navigator: {{ language: "en" }},
  history: {{ pushState() {{}} }},
  getComputedStyle: () => ({{ getPropertyValue: () => "" }}),
  console: {{ info() {{}}, error: console.error }},
  Intl,
  Date,
  Map,
  Set,
  Math,
  Number,
  String,
  Object,
  Array,
  JSON,
  Error,
  Promise,
}});
vm.runInContext(fs.readFileSync({json.dumps(card_path)}, "utf8"), ctx);

const Card = defined.get("edistribuzione-pod-card");
const Editor = defined.get("edistribuzione-pod-card-editor");
assert.ok(Card && Editor, "card and editor must register");

(async () => {{
  // F1 & F2: English "production" detected and production-only POD (no _energia) discovered.
  const card = new Card();
  const fakeHass = {{
    locale: {{ language: "en", number_format: "comma_decimal", time_format: "24" }},
    config: {{ time_zone: "Europe/Rome", components: ["edistribuzione"] }},
    user: {{ is_admin: true }},
    themes: {{ darkMode: false }},
    async callWS(msg) {{
      if (msg.type === "recorder/list_statistic_ids") {{
        return [
          {{ source: "edistribuzione", statistic_id: "edistribuzione:it001e_prod_energia_immessa", name: "E-Distribuzione IT001E_PROD - production" }},
          {{ source: "edistribuzione", statistic_id: "edistribuzione:it001e_exch_energia", name: "E-Distribuzione IT001E_EXCH - consumption" }},
          {{ source: "edistribuzione", statistic_id: "edistribuzione:it001e_exch_energia_immessa", name: "E-Distribuzione IT001E_EXCH - return to grid" }},
        ];
      }}
      if (msg.type === "recorder/statistics_during_period") {{
        const out = {{}};
        for (const id of msg.statistic_ids) {{
          out[id] = [{{ start: Date.UTC(2026, 7, 1, 22), end: Date.UTC(2026, 7, 2, 22), change: 5.5 }}];
        }}
        return out;
      }}
      return {{}};
    }},
  }};

  const pods = await card._scopriPod(fakeHass);
  assert.strictEqual(pods.length, 2);
  const prod = pods.find((p) => p.slug === "it001e_prod");
  const exch = pods.find((p) => p.slug === "it001e_exch");
  assert.strictEqual(prod.produzione, true, "English production label must be detected");
  assert.strictEqual(prod.haTotale, false, "production-only POD has no consumption total");
  assert.strictEqual(prod.haImmessa, true);
  assert.strictEqual(exch.produzione, false);

  // F2: _leggiEstremi works on production-only POD.
  card._hass = fakeHass;
  card._pods = pods;
  card._config = {{ pod: "IT001E-PROD" }};
  assert.strictEqual(card._pod.slug, "it001e_prod", "configured POD code must sanitize to slug");
  const estremi = await card._leggiEstremi();
  assert.ok(estremi.ultimo > 0, "estremi must be read from injection series");

  // F6: unknown configured POD returns null instead of silently showing another POD.
  card._config = {{ pod: "IT001E_MISSING" }};
  assert.strictEqual(card._pod, null);
  card._renderVuota();
  assert.ok(card.shadowRoot.innerHTML.includes("POD not found"));

  // F7: _renderVuota updates on language change.
  card._hass = {{ ...fakeHass, locale: {{ ...fakeHass.locale, language: "it" }} }};
  card._renderVuota();
  assert.ok(card.shadowRoot.innerHTML.includes("POD non trovato"));

  // F3: changing period in setConfig triggers _carica().
  card._config = {{ pod: "it001e_exch", period: "month" }};
  card._vista = "month";
  let caricaCalls = 0;
  card._carica = () => {{ caricaCalls++; }};
  card._render = () => {{}};
  card.setConfig({{ pod: "it001e_exch", period: "year" }});
  assert.strictEqual(caricaCalls, 1, "changing period must reload data");

  // F5: overlapping _avvia() discards stale result.
  const raceCard = new Card();
  raceCard._render = () => {{}};
  raceCard._carica = async () => {{}};
  raceCard._leggiEstremi = async () => ({{ primo: 1, primoGiorno: 1, ultimo: 2 }});
  let resolveFirst;
  let callNum = 0;
  raceCard._scopriPod = () => {{
    callNum++;
    if (callNum === 1) return new Promise((r) => {{ resolveFirst = () => r([exch]); }});
    return Promise.resolve([prod]);
  }};
  raceCard._hass = fakeHass;
  const p1 = raceCard._avvia();
  const p2 = raceCard._avvia();
  await p2;
  resolveFirst();
  await p1;
  assert.strictEqual(raceCard._pods[0].slug, "it001e_prod", "stale _avvia must not overwrite");

  // F11: cross-year week title includes both years.
  const iv = card._intervallo("week", {{ y: 2025, m: 11, d: 31 }});
  card._vista = "week";
  const titolo = card._titoloPeriodo(iv);
  assert.ok(titolo.includes("2025") && titolo.includes("2026"), `expected both years in ${{titolo}}`);

  // F4: Editor does not rebuild schema on unrelated hass state updates.
  const editor = new Editor();
  editor._pods = pods;
  editor._scoperta = Promise.resolve(pods);
  let schemaCalls = 0;
  const origSchema = editor._schema.bind(editor);
  editor._schema = () => {{ schemaCalls++; return origSchema(); }};
  editor.setConfig({{}});
  editor.hass = fakeHass;
  assert.strictEqual(schemaCalls, 1);
  editor.hass = {{ ...fakeHass, states: {{ sensor_x: {{ state: "1" }} }} }};
  editor.hass = {{ ...fakeHass, states: {{ sensor_x: {{ state: "2" }} }} }};
  assert.strictEqual(schemaCalls, 1, "unrelated hass updates must not rebuild ha-form schema");
  editor.hass = {{ ...fakeHass, locale: {{ ...fakeHass.locale, language: "it" }} }};
  assert.strictEqual(schemaCalls, 2, "language change must rebuild schema");

  console.log("JS_OK");
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + res.stdout
    assert "JS_OK" in res.stdout

