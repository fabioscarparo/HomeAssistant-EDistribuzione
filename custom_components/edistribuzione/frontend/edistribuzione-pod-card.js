/*
 * E-Distribuzione POD card
 *
 * Served by the integration itself (see lovelace_card.py): no resource to
 * add by hand. It reads the external statistics written by statistics.py
 * through the Recorder websocket, the same ones the Energy Dashboard uses.
 *
 * The look follows Home Assistant's own, using its tokens instead of values
 * of its own:
 *   - header like the tile card (36px icon on a 20% background);
 *   - period selector like ha-control-select;
 *   - chart like the energy cards: stacked bars with a 50% fill and a solid
 *     1.5px border, one LAB shade of --energy-grid-consumption-color per band
 *     (the same algorithm the Energy Dashboard uses to tell several grid
 *     meters apart), injection below zero with --energy-grid-return-color;
 *   - tooltip, grid and labels with the colours of HA's ECharts theme.
 *
 * No dependencies: a plain web component, a single file.
 */

const CARD_TYPE = "edistribuzione-pod-card";
const EDITOR_TYPE = "edistribuzione-pod-card-editor";
const VERSION = "0.6.0";
const SOURCE = "edistribuzione";
const FASCE = ["f1", "f2", "f3"];
// Keys of the injection by band (issue #7), next to the consumption ones.
const FASCE_IMMESSA = FASCE.map((f) => `immessa_${f}`);
const VISTE = ["day", "week", "month", "year"];
const HOUR_MS = 3600000;
const DAY_MS = 24 * HOUR_MS;
// Below this much history the card suggests fetching it: right after setup
// the integration imports only the last 3 days.
const STORICO_BREVE_MS = 31 * DAY_MS;
// Internal "My Home Assistant" redirects: they resolve to the right page on
// every version (Developer tools moved under Settings in 2026).
const URL_AZIONE_STORICO = "/_my_redirect/developer_call_service?service=edistribuzione.recupera_storico";
const URL_CONFIGURA = "/_my_redirect/config_flow_start?domain=edistribuzione";

const STRINGS = {
  it: {
    card_name: "E-Distribuzione · POD",
    card_description: "Prelievo per fascia F1/F2/F3 e immissione di un POD E-Distribuzione.",
    name: "Contatore",
    data_until: "Dati al {date}",
    waiting: "In attesa del primo import",
    withdrawn: "Prelievo",
    injected: "Immissione",
    produced: "Produzione",
    total: "Totale",
    day: "Giorno",
    week: "Settimana",
    month: "Mese",
    year: "Anno",
    previous: "Precedente",
    next: "Successivo",
    no_data: "Non ci sono dati per questo periodo.",
    empty_title: "Ancora nessun dato",
    empty_text: "Non ci sono ancora consumi importati da E-Distribuzione. Appena configurata, l'integrazione importa da sola gli ultimi giorni; per i periodi precedenti esegui l'azione «Recupera storico». Se non compare nulla, controlla Impostazioni > Riparazioni.",
    not_configured_title: "Integrazione non configurata",
    not_configured_text: "Aggiungi l'integrazione E-Distribuzione per vedere qui i consumi del tuo POD.",
    history_title: "Storico non ancora scaricato",
    history_text: "Ci sono dati solo dal {date}. Lo storico non si scarica in automatico: per vedere i mesi precedenti esegui l'azione «Recupera storico».",
    ask_admin: "Chiedi a un amministratore di eseguirla.",
    fetch_history: "Recupera storico",
    configure: "Configura",
    load_error: "Impossibile leggere le statistiche: {error}",
    chart_label: "{period}: prelievo {withdrawn}",
    ed_pod: "POD",
    ed_name: "Nome",
    ed_icon: "Icona",
    ed_period: "Periodo iniziale",
    ed_show_injection: "Mostra immissione",
  },
  en: {
    card_name: "E-Distribuzione · POD",
    card_description: "Grid consumption by F1/F2/F3 time band and return to grid for an E-Distribuzione POD.",
    name: "Meter",
    data_until: "Data through {date}",
    waiting: "Waiting for the first import",
    withdrawn: "Consumption",
    injected: "Return",
    produced: "Production",
    total: "Total",
    day: "Day",
    week: "Week",
    month: "Month",
    year: "Year",
    previous: "Previous",
    next: "Next",
    no_data: "There is no data for this period.",
    empty_title: "No data yet",
    empty_text: "No consumption has been imported from E-Distribuzione yet. Right after setup the integration imports the last few days on its own; for earlier periods run the \"Fetch history\" action. If nothing shows up, check Settings > Repairs.",
    not_configured_title: "Integration not set up",
    not_configured_text: "Add the E-Distribuzione integration to see your POD's consumption here.",
    history_title: "History not downloaded yet",
    history_text: "Data is only available from {date}. History is not downloaded automatically: to see earlier months, run the \"Fetch history\" action.",
    ask_admin: "Ask an administrator to run it.",
    fetch_history: "Fetch history",
    configure: "Set up",
    load_error: "Unable to read statistics: {error}",
    chart_label: "{period}: consumption {withdrawn}",
    ed_pod: "POD",
    ed_name: "Name",
    ed_icon: "Icon",
    ed_period: "Initial period",
    ed_show_injection: "Show return to grid",
  },
};

const lingua = (hass) => {
  const lang = hass?.locale?.language || hass?.language || "en";
  return lang.toLowerCase().startsWith("it") ? "it" : "en";
};

const traduci = (hass, key, vars = {}) => {
  let s = STRINGS[lingua(hass)][key] ?? STRINGS.en[key] ?? key;
  for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, v);
  return s;
};

// Same as the navigate() helper of HA's frontend: the app's router listens
// for "location-changed", with no page reload.
const naviga = (percorso) => {
  history.pushState(null, "", percorso);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
};

const leggiPreferenza = (chiave) => {
  try {
    return window.localStorage.getItem(chiave);
  } catch (_e) {
    return null;
  }
};

const salvaPreferenza = (chiave, valore) => {
  try {
    window.localStorage.setItem(chiave, valore);
  } catch (_e) {
    /* private mode or blocked storage: the hint just shows again */
  }
};

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// --- Civil dates in the server's time zone --------------------------------------
//
// The Recorder's daily/monthly buckets are computed in the time zone set in
// Home Assistant, not in the browser's: every period boundary is therefore
// computed in hass.config.time_zone. A "civil date" is {y, m, d} with a 0-based
// m; the arithmetic goes through Date.UTC, which normalises on its own.

const _dtfCache = new Map();
const partiNelFuso = (date, tz) => {
  let dtf = _dtfCache.get(tz);
  if (!dtf) {
    dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric",
      hour: "numeric", minute: "numeric", second: "numeric",
    });
    _dtfCache.set(tz, dtf);
  }
  const p = {};
  for (const { type, value } of dtf.formatToParts(date)) p[type] = Number(value);
  return { y: p.year, m: p.month - 1, d: p.day, h: p.hour % 24, min: p.minute, s: p.second };
};

const offsetMs = (date, tz) => {
  const p = partiNelFuso(date, tz);
  return Date.UTC(p.y, p.m, p.d, p.h, p.min, p.s) - Math.floor(date.getTime() / 1000) * 1000;
};

const mezzanotte = (c, tz) => {
  const guess = Date.UTC(c.y, c.m, c.d);
  const t = guess - offsetMs(new Date(guess), tz);
  return new Date(guess - offsetMs(new Date(t), tz));
};

const civile = (date, tz) => {
  const p = partiNelFuso(date, tz);
  return { y: p.y, m: p.m, d: p.d };
};

const addGiorni = (c, n) => {
  const d = new Date(Date.UTC(c.y, c.m, c.d + n));
  return { y: d.getUTCFullYear(), m: d.getUTCMonth(), d: d.getUTCDate() };
};

const addMesi = (c, n) => {
  const d = new Date(Date.UTC(c.y, c.m + n, 1));
  return { y: d.getUTCFullYear(), m: d.getUTCMonth(), d: 1 };
};

const giornoSettimana = (c) => new Date(Date.UTC(c.y, c.m, c.d)).getUTCDay();

// --- Colours: the same LAB shading as the Energy Dashboard ----------------------

const parseColore = (str) => {
  const s = (str || "").trim();
  let m = s.match(/^#([0-9a-f]{3,8})$/i);
  if (m) {
    let h = m[1];
    if (h.length <= 4) h = [...h].map((c) => c + c).join("");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }
  m = s.match(/^rgba?\(([^)]+)\)$/i);
  if (m) return m[1].split(/[\s,/]+/).slice(0, 3).map(Number);
  return [72, 143, 194]; // #488fc2, default di --energy-grid-consumption-color
};

const _lin = (c) => ((c /= 255) <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const _gam = (c) => 255 * (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);
const _f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
const _fi = (t) => (t > 0.206893 ? t ** 3 : (t - 16 / 116) / 7.787);

const rgbToLab = ([r, g, b]) => {
  [r, g, b] = [_lin(r), _lin(g), _lin(b)];
  const x = _f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047);
  const y = _f(0.2126 * r + 0.7152 * g + 0.0722 * b);
  const z = _f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
};

const labToRgb = ([l, a, bb]) => {
  const y = (l + 16) / 116;
  const X = 0.95047 * _fi(a / 500 + y);
  const Y = _fi(y);
  const Z = 1.08883 * _fi(y - bb / 200);
  const r = _gam(3.2406 * X - 1.5372 * Y - 0.4986 * Z);
  const g = _gam(-0.9689 * X + 1.8758 * Y + 0.0415 * Z);
  const b = _gam(0.0557 * X - 0.204 * Y + 1.057 * Z);
  return [r, g, b].map((v) => Math.round(Math.min(255, Math.max(0, v))));
};

// Kn = 18, like labBrighten/labDarken in HA's frontend: in a light theme the
// following series get darker, in a dark theme lighter.
const sfumatura = (rgb, indice, scuro) => {
  if (!indice) return rgb;
  const lab = rgbToLab(rgb);
  lab[0] += (scuro ? 18 : -18) * indice;
  return labToRgb(lab);
};

const rgbCss = (rgb, alpha) => (alpha === undefined ? `rgb(${rgb.join(",")})` : `rgba(${rgb.join(",")},${alpha})`);

// --- Axis scale ---------------------------------------------------------------

const scalaArrotondata = (min, max, passi = 4) => {
  if (max - min <= 0) max = min + 1;
  const grezzo = (max - min) / passi;
  const mag = 10 ** Math.floor(Math.log10(grezzo));
  const n = grezzo / mag;
  const passo = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
  const lo = Math.floor(min / passo + 1e-9) * passo;
  const hi = Math.ceil(max / passo - 1e-9) * passo;
  const ticks = [];
  for (let v = lo; v <= hi + passo / 2; v += passo) ticks.push(Math.abs(v) < passo / 1e6 ? 0 : v);
  return { min: lo, max: hi, ticks };
};

// --- Card --------------------------------------------------------------------

class EdistribuzionePodCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(EDITOR_TYPE);
  }

  static getStubConfig() {
    // No explicit POD: the card takes the first one found on its own.
    return {};
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._vista = "month";
    this._ancora = null;
    this._pods = null;
    this._estremi = null;
    this._dati = null;
    this._caricamento = false;
    this._errore = null;
    this._larghezza = 0;
    this._hover = null;
    this._richiesta = 0;
    this._firma = "";
  }

  setConfig(config) {
    if (config.period && !VISTE.includes(config.period)) {
      throw new Error(`period deve essere uno tra: ${VISTE.join(", ")}`);
    }
    const podPrima = this._config.pod;
    this._config = { show_injection: true, ...config };
    this._vista = this._config.period || this._vista || "month";
    if (this._hass && podPrima !== this._config.pod) {
      this._estremi = null;
      this._ancora = null;
      this._avvia();
    } else {
      this._render();
    }
  }

  set hass(hass) {
    const primo = !this._hass;
    this._hass = hass;
    // hass changes on every state event: redraw only when something the
    // card really uses changes (theme, language, number format).
    const firma = [hass.themes?.darkMode, hass.locale?.language, hass.locale?.number_format,
      hass.locale?.time_format, hass.config?.time_zone].join("|");
    if (primo) {
      this._firma = firma;
      this._avvia();
    } else if (firma !== this._firma) {
      this._firma = firma;
      this._render();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      if (w && w !== this._larghezza) {
        this._larghezza = w;
        this._renderGrafico();
      }
    });
    const chart = this.shadowRoot.querySelector(".chart");
    if (chart) this._ro.observe(chart);
    // Back on the dashboard (for example after running "Fetch history"):
    // the statistics may have changed while the card was detached.
    if (this._staccata && this._hass) {
      this._staccata = false;
      this._pods = null;
      this._estremi = null;
      this._avvia();
    }
  }

  disconnectedCallback() {
    this._ro?.disconnect();
    this._staccata = true;
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  // --- Data ------------------------------------------------------------------

  get _tz() {
    return this._hass?.config?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  }

  get _pod() {
    if (!this._pods?.length) return null;
    const scelto = (this._config.pod || "").toLowerCase();
    return this._pods.find((p) => p.slug === scelto) || this._pods[0];
  }

  async _avvia() {
    this._render();
    try {
      if (!this._pods) this._pods = await this._scopriPod(this._hass);
      if (!this._pod) {
        this._render();
        return;
      }
      if (!this._estremi) this._estremi = await this._leggiEstremi();
      if (!this._ancora) {
        this._ancora = this._estremi.ultimo
          ? civile(new Date(this._estremi.ultimo - 1), this._tz)
          : addGiorni(civile(new Date(), this._tz), -1);
      }
      await this._carica();
    } catch (err) {
      this._errore = err?.message || String(err);
      this._render();
    }
  }

  async _scopriPod(hass) {
    const elenco = await hass.callWS({ type: "recorder/list_statistic_ids", statistic_type: "sum" });
    const nostre = new Map(
      elenco.filter((s) => s.source === SOURCE).map((s) => [s.statistic_id, s])
    );
    const pods = [];
    for (const [id, meta] of nostre) {
      const m = id.match(/^edistribuzione:(.+)_energia$/);
      if (!m) continue;
      const slug = m[1];
      const ids = {
        totale: id,
        immessa: `${SOURCE}:${slug}_energia_immessa`,
        f1: `${SOURCE}:${slug}_energia_f1`,
        f2: `${SOURCE}:${slug}_energia_f2`,
        f3: `${SOURCE}:${slug}_energia_f3`,
        immessa_f1: `${SOURCE}:${slug}_energia_immessa_f1`,
        immessa_f2: `${SOURCE}:${slug}_energia_immessa_f2`,
        immessa_f3: `${SOURCE}:${slug}_energia_immessa_f3`,
      };
      const nomeImmessa = nostre.get(ids.immessa)?.name || "";
      pods.push({
        slug,
        codice: slug.toUpperCase(),
        nome: meta.name,
        ids,
        haFasce: FASCE.every((f) => nostre.has(ids[f])),
        haImmessa: nostre.has(ids.immessa),
        // Present after the first import with version 0.8.1 or later.
        haFasceImmessa: FASCE_IMMESSA.every((k) => nostre.has(ids[k])),
        produzione: /produzione/i.test(nomeImmessa),
      });
    }
    return pods.sort((a, b) => a.codice.localeCompare(b.codice));
  }

  async _statistiche(ids, inizio, fine, period) {
    const res = await this._hass.callWS({
      type: "recorder/statistics_during_period",
      start_time: inizio.toISOString(),
      end_time: fine ? fine.toISOString() : undefined,
      statistic_ids: ids,
      period,
      types: ["change"],
      units: { energy: "kWh" },
    });
    const norm = (t) => (typeof t === "number" ? t : Date.parse(t));
    const out = {};
    for (const id of ids) {
      out[id] = (res[id] || []).map((r) => ({ start: norm(r.start), end: norm(r.end), change: r.change }));
    }
    return out;
  }

  async _leggiEstremi() {
    // Two cheap requests: the months with data (first and last), then the
    // hours of the last month to know up to what hour the data goes.
    const id = this._pod.ids.totale;
    const mesi = (await this._statistiche([id], new Date(Date.UTC(2015, 0, 1)), null, "month"))[id];
    if (!mesi.length) return { primo: null, primoGiorno: null, ultimo: null };
    const ultimoMese = mesi[mesi.length - 1];
    const ore = (await this._statistiche([id], new Date(ultimoMese.start), null, "hour"))[id];
    // The exact first day is only needed for a short history (see
    // _suggerimentoStorico): one more request only in that case.
    let primoGiorno = mesi[0].start;
    if (mesi.length <= 2) {
      const giorni = (await this._statistiche([id], new Date(mesi[0].start), null, "day"))[id];
      if (giorni.length) primoGiorno = giorni[0].start;
    }
    return {
      primo: mesi[0].start,
      primoGiorno,
      ultimo: ore.length ? ore[ore.length - 1].end : ultimoMese.end,
    };
  }

  _intervallo(vista = this._vista, ancora = this._ancora) {
    const tz = this._tz;
    let inizio;
    let fine;
    let period;
    if (vista === "day") {
      inizio = ancora;
      fine = addGiorni(ancora, 1);
      period = "hour";
    } else if (vista === "week") {
      const primoGiorno = this._primoGiornoSettimana();
      inizio = addGiorni(ancora, -((giornoSettimana(ancora) - primoGiorno + 7) % 7));
      fine = addGiorni(inizio, 7);
      period = "day";
    } else if (vista === "month") {
      inizio = { y: ancora.y, m: ancora.m, d: 1 };
      fine = addMesi(inizio, 1);
      period = "day";
    } else {
      inizio = { y: ancora.y, m: 0, d: 1 };
      fine = { y: ancora.y + 1, m: 0, d: 1 };
      period = "month";
    }

    const inizioDate = mezzanotte(inizio, tz);
    const fineDate = mezzanotte(fine, tz);
    const slot = [];
    if (period === "hour") {
      for (let t = inizioDate.getTime(); t < fineDate.getTime(); t += HOUR_MS) slot.push(t);
    } else if (period === "day") {
      for (let c = inizio; mezzanotte(c, tz) < fineDate; c = addGiorni(c, 1)) slot.push(mezzanotte(c, tz).getTime());
    } else {
      for (let c = inizio; mezzanotte(c, tz) < fineDate; c = addMesi(c, 1)) slot.push(mezzanotte(c, tz).getTime());
    }
    return { inizio: inizioDate, fine: fineDate, period, slot, inizioCivile: inizio, fineCivile: fine };
  }

  _primoGiornoSettimana() {
    const pref = this._hass?.locale?.first_weekday;
    const nomi = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
    if (nomi.includes(pref)) return nomi.indexOf(pref);
    try {
      const info = new Intl.Locale(this._hass?.locale?.language || "it").getWeekInfo?.()
        ?? new Intl.Locale(this._hass?.locale?.language || "it").weekInfo;
      if (info?.firstDay) return info.firstDay % 7;
    } catch (_e) {
      /* weekInfo not supported: Monday */
    }
    return 1;
  }

  async _carica() {
    const pod = this._pod;
    if (!pod || !this._ancora) return;
    const richiesta = ++this._richiesta;
    const iv = this._intervallo();
    this._caricamento = true;
    this._hover = null;
    this._render();

    const ids = [pod.ids.totale];
    if (pod.haFasce) ids.push(pod.ids.f1, pod.ids.f2, pod.ids.f3);
    if (pod.haImmessa) ids.push(pod.ids.immessa);
    if (pod.haFasceImmessa) ids.push(...FASCE_IMMESSA.map((k) => pod.ids[k]));

    try {
      const res = await this._statistiche(ids, iv.inizio, iv.fine, iv.period);
      if (richiesta !== this._richiesta) return; // navigazione più recente in corso
      const chiaviValori = ["totale", ...FASCE, "immessa", ...FASCE_IMMESSA];
      const valori = iv.slot.map(() => Object.fromEntries(chiaviValori.map((k) => [k, null])));
      const chiavi = Object.fromEntries(chiaviValori.map((k) => [pod.ids[k], k]));
      for (const id of ids) {
        for (const r of res[id]) {
          if (r.change === null || r.change === undefined) continue;
          let i = iv.slot.length - 1;
          while (i >= 0 && iv.slot[i] > r.start) i--;
          if (i < 0 || r.start >= iv.fine.getTime()) continue;
          valori[i][chiavi[id]] = (valori[i][chiavi[id]] || 0) + r.change;
        }
      }
      this._dati = { iv, valori };
      this._errore = null;
    } catch (err) {
      if (richiesta !== this._richiesta) return;
      this._errore = err?.message || String(err);
    }
    this._caricamento = false;
    this._render();
  }

  _sposta(direzione) {
    const a = this._ancora;
    const passo = {
      day: () => addGiorni(a, direzione),
      week: () => addGiorni(a, 7 * direzione),
      month: () => addMesi(a, direzione),
      year: () => ({ y: a.y + direzione, m: 0, d: 1 }),
    }[this._vista];
    this._ancora = passo();
    this._carica();
  }

  _puoAvanzare() {
    if (!this._estremi?.ultimo || !this._ancora) return false;
    return this._intervallo().fine.getTime() < this._estremi.ultimo;
  }

  _puoTornare() {
    if (!this._estremi?.primo || !this._ancora) return false;
    return this._intervallo().inizio.getTime() > this._estremi.primo;
  }

  // --- Formatting ------------------------------------------------------------

  get _locale() {
    const lang = this._hass?.locale?.language || this._hass?.language || "it";
    const fmt = this._hass?.locale?.number_format;
    return { comma_decimal: "en-US", decimal_comma: "de", space_comma: "fr", system: undefined }[fmt] ?? lang;
  }

  _numero(v, cifre) {
    if (v === null || v === undefined || Number.isNaN(v)) return "-";
    const a = Math.abs(v);
    const d = cifre ?? (a >= 100 ? 0 : a >= 10 ? 1 : 2);
    return new Intl.NumberFormat(this._locale, {
      minimumFractionDigits: d, maximumFractionDigits: d,
      useGrouping: this._hass?.locale?.number_format !== "none",
    }).format(v);
  }

  _data(date, opzioni) {
    const lang = this._hass?.locale?.language || "it";
    const tf = this._hass?.locale?.time_format;
    const extra = tf === "12" ? { hourCycle: "h12" } : tf === "24" ? { hourCycle: "h23" } : {};
    return new Intl.DateTimeFormat(lang, { timeZone: this._tz, ...extra, ...opzioni }).format(date);
  }

  _titoloPeriodo(iv) {
    if (this._vista === "day") return this._data(iv.inizio, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    if (this._vista === "week") {
      // Built by hand instead of formatRange: in some languages formatRange
      // adds a leading zero to the days ("04 ott").
      const ultimo = mezzanotte(addGiorni(iv.fineCivile, -1), this._tz);
      const stessoMese = iv.inizioCivile.m === addGiorni(iv.fineCivile, -1).m;
      const da = this._data(iv.inizio, stessoMese ? { day: "numeric" } : { day: "numeric", month: "short" });
      return `${da} - ${this._data(ultimo, { day: "numeric", month: "short", year: "numeric" })}`;
    }
    if (this._vista === "month") return this._data(iv.inizio, { month: "long", year: "numeric" });
    return this._data(iv.inizio, { year: "numeric" });
  }

  _titoloSlot(i) {
    const { iv } = this._dati;
    const t = new Date(iv.slot[i]);
    if (iv.period === "hour") {
      const ora = { hour: "2-digit", minute: "2-digit" };
      return `${this._data(t, { day: "numeric", month: "short" })}, ${this._data(t, ora)}-${this._data(new Date(iv.slot[i] + HOUR_MS), ora)}`;
    }
    if (iv.period === "day") return this._data(t, { weekday: "short", day: "numeric", month: "short" });
    return this._data(t, { month: "long", year: "numeric" });
  }

  _etichettaAsse(i, stretta) {
    const { iv } = this._dati;
    const t = new Date(iv.slot[i]);
    if (iv.period === "hour") {
      // With the 12-hour clock "3 AM" instead of "03:00 AM": shorter, like
      // the labels of the native charts.
      return this._dodiciOre()
        ? this._data(t, { hour: "numeric" })
        : this._data(t, { hour: "2-digit", minute: "2-digit" });
    }
    if (iv.period === "day") return this._vista === "week" ? this._data(t, { weekday: "short" }) : this._data(t, { day: "numeric" });
    return this._data(t, { month: stretta ? "narrow" : "short" });
  }

  _dodiciOre() {
    const ciclo = new Intl.DateTimeFormat(this._hass?.locale?.language || "it", {
      timeZone: this._tz, hour: "numeric",
      ...(this._hass?.locale?.time_format === "12" ? { hourCycle: "h12" }
        : this._hass?.locale?.time_format === "24" ? { hourCycle: "h23" } : {}),
    }).resolvedOptions().hourCycle;
    return ciclo === "h11" || ciclo === "h12";
  }

  // --- Colours from the current theme ----------------------------------------

  _colori() {
    const cs = getComputedStyle(this);
    const scuro = !!this._hass?.themes?.darkMode;
    const base = parseColore(cs.getPropertyValue("--energy-grid-consumption-color") || "#488fc2");
    const ritorno = parseColore(cs.getPropertyValue("--energy-grid-return-color") || "#8353d1");
    const c = {
      totale: base,
      f1: sfumatura(base, 0, scuro),
      f2: sfumatura(base, 1, scuro),
      f3: sfumatura(base, 2, scuro),
      immessa: ritorno,
      immessa_f1: sfumatura(ritorno, 0, scuro),
      immessa_f2: sfumatura(ritorno, 1, scuro),
      immessa_f3: sfumatura(ritorno, 2, scuro),
    };
    return c;
  }

  // --- Rendering -------------------------------------------------------------

  _render() {
    if (!this._hass) return;
    const t = (k, v) => traduci(this._hass, k, v);
    const pod = this._pod;

    if (this._pods && !pod) {
      this._renderVuota();
      return;
    }
    if (this._modo !== "card") this._struttura();

    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const colori = this._colori();
    $("ha-card").style.setProperty("--edist-accent", rgbCss(colori.totale));

    // header
    const icona = this._config.icon || "mdi:transmission-tower";
    if ($(".icon ha-icon").getAttribute("icon") !== icona) $(".icon ha-icon").setAttribute("icon", icona);
    $(".primary").textContent = this._config.name || t("name");
    let secondaria = pod ? pod.codice : "";
    if (this._estremi) {
      const ultimo = this._estremi.ultimo;
      secondaria += ` · ${ultimo
        ? t("data_until", { date: this._data(new Date(ultimo - 1), { day: "numeric", month: "short" }) })
        : t("waiting")}`;
    }
    $(".secondary").textContent = secondaria;

    // period selector and navigation
    this.shadowRoot.querySelectorAll("[data-vista]").forEach((el) => {
      const sel = el.dataset.vista === this._vista;
      el.classList.toggle("selected", sel);
      el.setAttribute("aria-checked", String(sel));
      el.firstElementChild.textContent = t(el.dataset.vista);
    });
    const iv = this._ancora ? this._intervallo() : null;
    $(".period").textContent = iv ? this._titoloPeriodo(iv) : "\u00a0";
    $('[data-azione="prev"]').toggleAttribute("disabled", !this._puoTornare());
    $('[data-azione="next"]').toggleAttribute("disabled", !this._puoAvanzare());
    $('[data-azione="prev"]').setAttribute("label",
      this._hass.localize?.("ui.panel.lovelace.components.energy_period_selector.previous") || t("previous"));
    $('[data-azione="next"]').setAttribute("label",
      this._hass.localize?.("ui.panel.lovelace.components.energy_period_selector.next") || t("next"));

    $(".error").innerHTML = this._errore
      ? `<ha-alert alert-type="error">${escapeHtml(t("load_error", { error: this._errore }))}</ha-alert>` : "";
    this._renderSuggerimento($(".hint"));

    // totals and split by band
    const valori = this._dati?.valori || [];
    const somma = (k) => valori.reduce((acc, v) => (v[k] === null ? acc : (acc ?? 0) + v[k]), null);
    const sommaFasce = FASCE.reduce((a, f) => a + (somma(f) || 0), 0);
    const prelievo = somma("totale") ?? (sommaFasce || null);
    const mostraImmessa = !!pod?.haImmessa && this._config.show_injection !== false;
    const nomeImmessa = pod?.produzione ? t("produced") : t("injected");
    const totale = (etichetta, colore, valore) => `
      <div class="total">
        <div class="total-label"><span class="dot" style="background:${rgbCss(colore)}"></span>${escapeHtml(etichetta)}</div>
        <div class="total-value">${this._numero(valore)}<span class="unit">kWh</span></div>
      </div>`;
    $(".totals").innerHTML = totale(t("withdrawn"), colori.totale, prelievo)
      + (mostraImmessa ? totale(nomeImmessa, colori.immessa, somma("immessa")) : "");

    // Injection by band under the consumption one; the two titles only when
    // both are shown, so the card looks as before without injection bands.
    const fasceImmessa = mostraImmessa && !!pod?.haFasceImmessa;
    this._renderFasce($(".fasce"), FASCE, !!pod?.haFasce, fasceImmessa ? t("withdrawn") : "", somma, colori);
    this._renderFasce($(".fasce-immessa"), FASCE_IMMESSA, fasceImmessa, nomeImmessa, somma, colori);

    $(".chart").classList.toggle("loading", this._caricamento);
    this._larghezza = $(".chart").clientWidth || this._larghezza;
    this._renderGrafico();
  }

  // --- Guidance when there is little or no data --------------------------------

  _alertConAzione(titolo, testo, etichetta, percorso, extra = "") {
    // The action and the setup are admin-only pages: other users get the
    // explanation without a button that would lead nowhere.
    const admin = !!this._hass.user?.is_admin;
    const t = (k) => traduci(this._hass, k);
    return `<ha-alert alert-type="info" title="${escapeHtml(titolo)}" ${extra}>
      ${escapeHtml(testo)}${admin ? "" : ` ${escapeHtml(t("ask_admin"))}`}
      ${admin ? `<ha-button slot="action" data-percorso="${escapeHtml(percorso)}">${escapeHtml(etichetta)}</ha-button>` : ""}
    </ha-alert>`;
  }

  _collegaPulsanti(radice) {
    radice.querySelectorAll("[data-percorso]").forEach((el) =>
      el.addEventListener("click", () => naviga(el.dataset.percorso)));
  }

  _renderVuota() {
    // No E-Distribuzione statistics at all: tell apart "integration missing"
    // from "set up, but nothing imported yet", each with the step to take.
    const t = (k) => traduci(this._hass, k);
    const configurata = (this._hass.config?.components || []).includes(SOURCE);
    const chiave = configurata ? "empty" : "not_configured";
    if (this._modo === `vuoto-${chiave}`) return;
    this._modo = `vuoto-${chiave}`;
    const alert = configurata
      ? this._alertConAzione(t("empty_title"), t("empty_text"), t("fetch_history"), URL_AZIONE_STORICO)
      : this._alertConAzione(t("not_configured_title"), t("not_configured_text"), t("configure"), URL_CONFIGURA);
    this.shadowRoot.innerHTML = `<style>${STILE}</style><ha-card><div class="empty-card">${alert}</div></ha-card>`;
    this._collegaPulsanti(this.shadowRoot);
  }

  get _chiaveSuggerimento() {
    return `${CARD_TYPE}:storico-nascosto:${this._pod?.slug}`;
  }

  _suggerimentoStorico() {
    // Only a short history, right after setup: the hint says why the card
    // shows so little and how to get the rest. Hidden for good per POD once
    // the user closes it, and on its own as soon as the history is longer.
    const e = this._estremi;
    if (!this._pod || !e?.primoGiorno || !e?.ultimo) return null;
    if (e.ultimo - e.primoGiorno >= STORICO_BREVE_MS) return null;
    if (leggiPreferenza(this._chiaveSuggerimento)) return null;
    return this._data(new Date(e.primoGiorno), { day: "numeric", month: "long", year: "numeric" });
  }

  _renderSuggerimento(host) {
    const data = this._suggerimentoStorico();
    const firma = data ? `${this._pod.slug}|${data}|${lingua(this._hass)}` : "";
    if (host.dataset.firma === firma) return;
    host.dataset.firma = firma;
    if (!data) {
      host.innerHTML = "";
      return;
    }
    const t = (k, v) => traduci(this._hass, k, v);
    host.innerHTML = this._alertConAzione(
      t("history_title"), t("history_text", { date: data }), t("fetch_history"), URL_AZIONE_STORICO, "dismissable");
    this._collegaPulsanti(host);
    host.querySelector("ha-alert").addEventListener("alert-dismissed-clicked", () => {
      salvaPreferenza(this._chiaveSuggerimento, "1");
      this._render();
    });
  }

  _renderFasce(blocco, chiavi, visibile, titolo, somma, colori) {
    blocco.hidden = !visibile;
    if (!visibile) return;
    const tot = chiavi.map((k) => somma(k) || 0);
    const sommaTot = tot.reduce((a, b) => a + b, 0);
    const nome = (i) => FASCE[i].toUpperCase();
    const $ = (sel) => blocco.querySelector(sel);
    $(".fasce-titolo").hidden = !titolo;
    $(".fasce-titolo").textContent = titolo;
    $(".split").setAttribute("aria-label",
      `${titolo ? `${titolo}: ` : ""}${chiavi.map((_, i) => `${nome(i)} ${this._percentuale(tot[i], sommaTot)}`).join(", ")}`);
    $(".split").innerHTML = sommaTot > 0
      ? chiavi.map((k, i) => (tot[i] > 0
        ? `<span style="flex-grow:${tot[i]};background:${rgbCss(colori[k])}"></span>` : "")).join("")
      : `<span class="vuoto"></span>`;
    $(".legend").innerHTML = chiavi.map((k, i) => `
      <div class="legend-item">
        <div class="legend-name"><span class="dot" style="background:${rgbCss(colori[k])}"></span>${nome(i)}
          <span class="legend-pct">${this._percentuale(tot[i], sommaTot)}</span></div>
        <div class="legend-kwh">${sommaTot > 0 ? `${this._numero(tot[i])} kWh` : "-"}</div>
      </div>`).join("");
  }

  _struttura() {
    // Structure created only once: later updates only touch texts and
    // attributes, so icons and buttons do not flicker on every period
    // change.
    this._modo = "card";
    this.shadowRoot.innerHTML = `
      <style>${STILE}</style>
      <ha-card>
        <div class="header">
          <div class="icon"><ha-icon></ha-icon></div>
          <div class="info">
            <div class="primary"></div>
            <div class="secondary"></div>
          </div>
        </div>
        <div class="content">
          <div class="segmented" role="radiogroup">
            ${VISTE.map((v) => `<button class="option" role="radio" data-vista="${v}"><span></span></button>`).join("")}
          </div>
          <div class="nav">
            <ha-icon-button data-azione="prev"><ha-icon icon="mdi:chevron-left"></ha-icon></ha-icon-button>
            <div class="period"></div>
            <ha-icon-button data-azione="next"><ha-icon icon="mdi:chevron-right"></ha-icon></ha-icon-button>
          </div>
          <div class="hint"></div>
          <div class="error"></div>
          <div class="totals"></div>
          <div class="fasce" hidden>
            <div class="fasce-titolo" hidden></div>
            <div class="split" role="img"></div>
            <div class="legend"></div>
          </div>
          <div class="fasce fasce-immessa" hidden>
            <div class="fasce-titolo" hidden></div>
            <div class="split" role="img"></div>
            <div class="legend"></div>
          </div>
          <div class="chart">
            <div class="svg-host"></div>
            <div class="tooltip" hidden></div>
          </div>
        </div>
      </ha-card>`;

    this.shadowRoot.querySelectorAll("[data-vista]").forEach((el) =>
      el.addEventListener("click", () => this._cambiaVista(el.dataset.vista)));
    this.shadowRoot.querySelector('[data-azione="prev"]').addEventListener("click", () => this._sposta(-1));
    this.shadowRoot.querySelector('[data-azione="next"]').addEventListener("click", () => this._sposta(1));
    if (this._ro) {
      this._ro.disconnect();
      this._ro.observe(this.shadowRoot.querySelector(".chart"));
    }
  }

  _cambiaVista(vista) {
    if (vista === this._vista) return;
    this._vista = vista;
    if (this._ancora && this._estremi?.ultimo) {
      // Stay on the last day with data if it falls in the new period,
      // otherwise on the day already shown.
      const ultimoGiorno = civile(new Date(this._estremi.ultimo - 1), this._tz);
      const iv = this._intervallo(vista, this._ancora);
      const t = mezzanotte(ultimoGiorno, this._tz).getTime();
      if (t >= iv.inizio.getTime() && t < iv.fine.getTime()) this._ancora = ultimoGiorno;
    }
    this._carica();
  }

  _percentuale(v, tot) {
    if (!tot) return "-";
    return new Intl.NumberFormat(this._locale, { style: "percent", maximumFractionDigits: 0 }).format(v / tot);
  }

  _renderGrafico() {
    const host = this.shadowRoot.querySelector(".svg-host");
    if (!host) return;
    const W = this._larghezza || host.clientWidth;
    if (!W || !this._dati) {
      host.innerHTML = "";
      return;
    }
    const t = (k, v) => traduci(this._hass, k, v);
    const pod = this._pod;
    const { iv, valori } = this._dati;
    const colori = this._colori();
    const n = iv.slot.length;
    const mostraImmessa = !!pod?.haImmessa && this._config.show_injection !== false;
    const serie = pod?.haFasce ? FASCE : ["totale"];
    const serieImmessa = mostraImmessa ? (pod.haFasceImmessa ? FASCE_IMMESSA : ["immessa"]) : [];

    const positivo = valori.map((v) => serie.reduce((a, k) => a + (v[k] || 0), 0));
    const negativo = valori.map((v) => -serieImmessa.reduce((a, k) => a + (v[k] || 0), 0));
    const vuoto = valori.every((v, i) => !positivo[i] && !negativo[i] && v.totale === null);
    const scala = scalaArrotondata(Math.min(0, ...negativo), Math.max(0, ...positivo), 5);

    const H = 196;
    const top = 24;
    const bottom = 24;
    const etichetteY = scala.ticks.map((v) => this._numero(v, scala.ticks.some((x) => x % 1) ? 1 : 0));
    const left = Math.max(24, Math.ceil(Math.max(...etichetteY.map((s) => s.length)) * 7) + 10);
    const right = 4;
    const plotW = Math.max(10, W - left - right);
    const plotH = H - top - bottom;
    const y = (v) => top + ((scala.max - v) / (scala.max - scala.min)) * plotH;
    const slotW = plotW / n;
    const barW = Math.min(50, Math.max(1, slotW * 0.7));

    const parti = [];
    // grid (splitLine of the valueAxis in HA's ECharts theme)
    scala.ticks.forEach((v, i) => {
      const yy = Math.round(y(v)) + 0.5;
      parti.push(`<line class="grid" x1="${left}" x2="${W - right}" y1="${yy}" y2="${yy}"/>`);
      parti.push(`<text class="ylab" x="${left - 8}" y="${yy + 4}">${escapeHtml(etichetteY[i])}</text>`);
    });
    parti.push(`<text class="ylab" x="${left - 8}" y="${top - 12}">kWh</text>`);
    parti.push(`<rect class="hover-band" x="0" y="${top}" width="0" height="${plotH}" />`);

    // Like the native energy cards: the outermost segment of each stack has
    // its outer corners rounded to 4px (top for consumption, bottom for
    // injection), the others stay square.
    const segmento = (x, y0, y1, rgb, arrotondaSopra, arrotondaSotto) => {
      const h = Math.abs(y1 - y0);
      const yy = Math.min(y0, y1);
      if (h < 2) return `<rect x="${x}" y="${yy}" width="${barW}" height="${Math.max(h, 0.5)}" fill="${rgbCss(rgb)}"/>`;
      const i = 0.75; // metà del bordo da 1.5px, tenuto dentro la barra
      const L = x + i;
      const R = x + barW - i;
      const T = yy + i;
      const B = yy + h - i;
      const r = Math.max(0, Math.min(4, (R - L) / 2, (B - T) / 2));
      const rs = arrotondaSopra ? r : 0;
      const ri = arrotondaSotto ? r : 0;
      const d = `M${L},${T + rs}`
        + (rs ? `Q${L},${T} ${L + rs},${T}` : "") + `L${R - rs},${T}`
        + (rs ? `Q${R},${T} ${R},${T + rs}` : "") + `L${R},${B - ri}`
        + (ri ? `Q${R},${B} ${R - ri},${B}` : "") + `L${L + ri},${B}`
        + (ri ? `Q${L},${B} ${L},${B - ri}` : "") + "Z";
      return `<path d="${d}" fill="${rgbCss(rgb, 0.5)}" stroke="${rgbCss(rgb)}" stroke-width="1.5" stroke-linejoin="round"/>`;
    };

    for (let i = 0; i < n; i++) {
      const x = left + slotW * i + (slotW - barW) / 2;
      let acc = 0;
      const presenti = serie.filter((k) => valori[i][k]);
      presenti.forEach((k, j) => {
        const v = valori[i][k];
        parti.push(segmento(x, y(acc), y(acc + v), colori[k], j === presenti.length - 1, false));
        acc += v;
      });
      // Injection below zero, stacked the same way: F1 next to the axis, the
      // outermost segment rounded at the bottom.
      let giu = 0;
      const presentiImmessa = serieImmessa.filter((k) => valori[i][k]);
      presentiImmessa.forEach((k, j) => {
        const v = valori[i][k];
        parti.push(segmento(x, y(-giu), y(-(giu + v)), colori[k], false, j === presentiImmessa.length - 1));
        giu += v;
      });
    }

    // X axis labels without overlaps
    const stretta = iv.period === "month" && slotW < 30;
    // Width estimated from the longest label, so the step adapts to language
    // and time format (e.g. "12 PM", "mer", "Sep") without overlaps.
    const caratteriMax = Math.max(...iv.slot.map((_, i) => this._etichettaAsse(i, stretta).length));
    const larghezzaEtichetta = caratteriMax * 6.5 + 8;
    const passiPossibili = iv.period === "hour" ? [1, 2, 3, 4, 6, 12] : iv.period === "day" ? [1, 2, 3, 5, 7, 10] : [1, 2, 3, 6];
    const passoX = passiPossibili.find((p) => (slotW * p) >= larghezzaEtichetta) || passiPossibili[passiPossibili.length - 1];
    for (let i = 0; i < n; i += passoX) {
      const cx = left + slotW * (i + 0.5);
      parti.push(`<text class="xlab" x="${cx}" y="${H - 6}">${escapeHtml(this._etichettaAsse(i, stretta))}</text>`);
    }

    const prelievo = valori.reduce((a, v) => a + (v.totale || 0), 0);
    const aria = t("chart_label", { period: this._titoloPeriodo(iv), withdrawn: `${this._numero(prelievo)} kWh` });
    host.innerHTML = `
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(aria)}">
        ${parti.join("")}
        <rect class="hit" x="${left}" y="${top}" width="${plotW}" height="${plotH}"/>
      </svg>
      ${vuoto ? `<div class="no-data">${escapeHtml(this._hass.localize?.("ui.panel.lovelace.cards.energy.no_data_period") || t("no_data"))}</div>` : ""}`;

    const svg = host.querySelector("svg");
    const banda = svg.querySelector(".hover-band");
    const tip = this.shadowRoot.querySelector(".tooltip");
    const indice = (ev) => {
      const r = svg.getBoundingClientRect();
      const i = Math.floor((ev.clientX - r.left - left) / slotW);
      return i >= 0 && i < n ? i : null;
    };
    const mostra = (i) => {
      if (i === null || vuoto) return nascondi();
      this._hover = i;
      banda.setAttribute("x", left + slotW * i);
      banda.setAttribute("width", slotW);
      tip.innerHTML = this._tooltip(i, colori, serie, mostraImmessa);
      tip.hidden = false;
      const cx = left + slotW * (i + 0.5);
      const tw = tip.offsetWidth;
      const lx = cx < W / 2 ? cx + slotW / 2 + 8 : cx - slotW / 2 - 8 - tw;
      tip.style.left = `${Math.max(0, Math.min(W - tw, lx))}px`;
      // With the injection bands the tooltip can be taller than the chart:
      // it moves up instead of being cut by the bottom of the card.
      tip.style.top = `${Math.min(top, H - tip.offsetHeight)}px`;
    };
    const nascondi = () => {
      this._hover = null;
      banda.setAttribute("width", 0);
      tip.hidden = true;
    };
    svg.addEventListener("pointermove", (ev) => ev.pointerType === "mouse" && mostra(indice(ev)));
    svg.addEventListener("pointerleave", (ev) => ev.pointerType === "mouse" && nascondi());
    svg.addEventListener("click", (ev) => {
      const i = indice(ev);
      if (i === this._hover && ev.pointerType !== "mouse") nascondi();
      else mostra(i);
    });
    if (this._hover !== null && this._hover < n) mostra(this._hover);
  }

  _tooltip(i, colori, serie, mostraImmessa) {
    const t = (k) => traduci(this._hass, k);
    const v = this._dati.valori[i];
    const riga = (rgb, nome, valore) => `
      <div class="tip-row"><span class="tip-marker" style="background:${rgbCss(rgb)}"></span>
        <span class="tip-name">${escapeHtml(nome)}</span><span class="tip-value">${this._numero(valore)} kWh</span></div>`;
    const righe = [];
    if (serie.length > 1) {
      const presenti = [...serie].reverse().filter((k) => v[k]);
      for (const k of presenti) righe.push(riga(colori[k], k.toUpperCase(), v[k]));
      // The total is only needed with more than one band (an hour always has one).
      if (v.totale !== null && presenti.length !== 1) righe.push(`<div class="tip-row tip-total"><span class="tip-name">${escapeHtml(t("withdrawn"))}</span><span class="tip-value">${this._numero(v.totale)} kWh</span></div>`);
    } else if (v.totale !== null) {
      righe.push(riga(colori.totale, t("withdrawn"), v.totale));
    }
    const nomeImmessa = this._pod.produzione ? t("produced") : t("injected");
    const fasceImmessa = this._pod.haFasceImmessa ? FASCE_IMMESSA.filter((k) => v[k]) : [];
    if (mostraImmessa && fasceImmessa.length > 1) {
      for (const k of fasceImmessa) righe.push(riga(colori[k], `${nomeImmessa} ${k.slice(-2).toUpperCase()}`, v[k]));
      if (v.immessa) righe.push(`<div class="tip-row tip-total"><span class="tip-name">${escapeHtml(nomeImmessa)}</span><span class="tip-value">${this._numero(v.immessa)} kWh</span></div>`);
    } else if (mostraImmessa && v.immessa) {
      const k = fasceImmessa[0];
      righe.push(riga(colori[k || "immessa"], k ? `${nomeImmessa} ${k.slice(-2).toUpperCase()}` : nomeImmessa, v.immessa));
    }
    if (!righe.length) righe.push(`<div class="tip-row tip-empty">-</div>`);
    return `<div class="tip-title">${escapeHtml(this._titoloSlot(i))}</div>${righe.join("")}`;
  }
}

const STILE = `
  :host { display: block; }
  ha-card { height: 100%; overflow: hidden; }
  .empty-card { padding: 16px; }
  .hint:empty { display: none; }
  ha-alert { display: block; }

  .header {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 16px 0;
  }
  .icon {
    position: relative; flex: none; display: flex; align-items: center; justify-content: center;
    width: 36px; height: 36px; overflow: hidden;
    border-radius: var(--ha-tile-icon-border-radius, var(--ha-border-radius-pill, 9999px));
    color: var(--edist-accent); --mdc-icon-size: 24px;
  }
  .icon::before {
    content: ""; position: absolute; inset: 0;
    background-color: var(--edist-accent); opacity: 0.2;
  }
  .icon ha-icon { position: relative; display: flex; }
  .info { min-width: 0; flex: 1; }
  .primary {
    font-size: var(--ha-font-size-m, 14px); font-weight: var(--ha-font-weight-medium, 500);
    line-height: 20px; letter-spacing: 0.1px; color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .secondary {
    font-size: var(--ha-font-size-s, 12px); font-weight: var(--ha-font-weight-normal, 400);
    line-height: 16px; letter-spacing: 0.4px; color: var(--secondary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  .content { display: flex; flex-direction: column; gap: 12px; padding: 12px 16px 16px; }

  /* like ha-control-select */
  .segmented {
    --control-select-color: var(--edist-accent);
    position: relative; display: flex; gap: 4px; padding: 4px; box-sizing: border-box;
    height: 40px; border-radius: var(--ha-border-radius-lg, 12px);
  }
  .segmented::before {
    content: ""; position: absolute; inset: 0; border-radius: inherit;
    background: var(--disabled-color, #bdbdbd); opacity: 0.2;
  }
  .option {
    position: relative; flex: 1; min-width: 0; height: 100%; padding: 0 4px;
    display: flex; align-items: center; justify-content: center;
    border: none; background: none; cursor: pointer; outline: 0;
    border-radius: calc(var(--ha-border-radius-lg, 12px) - 4px);
    font-family: inherit; font-size: var(--ha-font-size-m, 14px);
    font-weight: var(--ha-font-weight-medium, 500); color: var(--primary-text-color);
    transition: box-shadow 180ms ease-in-out; -webkit-tap-highlight-color: transparent;
  }
  .option::before {
    content: ""; position: absolute; inset: 0; border-radius: inherit;
    background-color: var(--control-select-color); opacity: 0;
    transition: background-color 180ms ease-in-out, opacity 80ms ease-in-out;
  }
  .option:hover::before { opacity: 0.2; }
  .option:focus-visible { box-shadow: 0 0 0 2px var(--control-select-color); }
  .option.selected { color: #fff; }
  .option.selected::before { opacity: 1; }
  .option span { position: relative; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .nav { display: flex; align-items: center; margin: -4px -12px; }
  .nav ha-icon-button { --mdc-icon-button-size: 40px; color: var(--primary-text-color); }
  .nav ha-icon-button[disabled] { color: var(--disabled-text-color); }
  .period {
    flex: 1; text-align: center; font-size: var(--ha-font-size-m, 14px);
    font-weight: var(--ha-font-weight-medium, 500); color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .period::first-letter { text-transform: uppercase; }

  .totals { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px 16px; }
  .total-label {
    display: flex; align-items: center; gap: 6px; font-size: var(--ha-font-size-s, 12px);
    line-height: 16px; letter-spacing: 0.4px; color: var(--secondary-text-color);
  }
  .total-value {
    font-size: var(--ha-font-size-2xl, 24px); font-weight: var(--ha-font-weight-normal, 400);
    line-height: var(--ha-line-height-condensed, 1.2); color: var(--primary-text-color);
    font-variant-numeric: tabular-nums; margin-top: 2px;
  }
  .unit { font-size: var(--ha-font-size-m, 14px); color: var(--secondary-text-color); margin-inline-start: 4px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }

  .fasce { display: flex; flex-direction: column; gap: 8px; }
  .fasce[hidden], .fasce-titolo[hidden] { display: none; }
  .fasce-titolo {
    font-size: var(--ha-font-size-s, 12px); line-height: 16px; letter-spacing: 0.4px;
    color: var(--secondary-text-color);
  }
  .split {
    display: flex; gap: 2px; height: 8px; overflow: hidden; flex: none;
    border-radius: var(--ha-border-radius-pill, 9999px);
  }
  .split span { min-width: 2px; }
  .split .vuoto { flex: 1; background: var(--divider-color); }
  .legend { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .legend-name {
    display: flex; align-items: center; gap: 6px; font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500); line-height: 16px; color: var(--primary-text-color);
  }
  .legend-pct { font-weight: var(--ha-font-weight-normal, 400); color: var(--secondary-text-color); }
  .legend-kwh {
    font-size: var(--ha-font-size-s, 12px); line-height: 16px; letter-spacing: 0.4px;
    color: var(--secondary-text-color); padding-inline-start: 14px; font-variant-numeric: tabular-nums;
  }

  .chart { position: relative; min-height: 196px; margin: 0 -4px; touch-action: pan-y; }
  .chart.loading svg { opacity: 0.4; transition: opacity 120ms ease-in-out; }
  svg { display: block; overflow: visible; }
  svg text { font-size: 12px; fill: var(--primary-text-color); }
  svg .ylab { text-anchor: end; }
  svg .xlab { text-anchor: middle; }
  svg .grid { stroke: var(--divider-color); stroke-width: 1; }
  svg .hover-band { fill: var(--secondary-text-color); opacity: 0.1; }
  svg .hit { fill: transparent; cursor: default; }
  .no-data {
    position: absolute; inset: 0 0 24px 32px; display: flex; align-items: center; justify-content: center;
    text-align: center; color: var(--secondary-text-color); font-size: var(--ha-font-size-m, 14px);
    pointer-events: none;
  }

  /* like the tooltip of HA's ECharts theme */
  .tooltip {
    position: absolute; z-index: 1; pointer-events: none; white-space: nowrap;
    background: var(--ha-card-background, var(--card-background-color, #fff));
    border: 1px solid var(--divider-color); border-radius: 4px;
    box-shadow: rgba(0, 0, 0, 0.2) 1px 2px 10px; padding: 8px 10px;
    font-size: 12px; line-height: 18px; color: var(--primary-text-color);
  }
  .tip-title { font-weight: var(--ha-font-weight-medium, 500); margin-bottom: 2px; }
  .tip-row { display: flex; align-items: center; gap: 6px; }
  .tip-marker { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .tip-name { flex: 1; }
  .tip-value { font-weight: var(--ha-font-weight-medium, 500); margin-inline-start: 16px; font-variant-numeric: tabular-nums; }
  .tip-total { border-top: 1px solid var(--divider-color); margin-top: 4px; padding-top: 4px; }
  .tip-empty { color: var(--secondary-text-color); }
`;

// --- Visual editor (ha-form, like the native cards) -----------------------------

class EdistribuzionePodCardEditor extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._pods && !this._scoperta) {
      this._scoperta = EdistribuzionePodCard.prototype._scopriPod
        .call(null, hass)
        .then((pods) => {
          this._pods = pods;
          this._render();
        })
        .catch(() => {
          this._pods = [];
          this._render();
        });
    }
    this._render();
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  _schema() {
    const t = (k) => traduci(this._hass, k);
    const pods = this._pods || [];
    return [
      {
        name: "pod",
        selector: { select: { mode: "dropdown", options: pods.map((p) => ({ value: p.slug, label: p.codice })) } },
      },
      {
        type: "grid",
        name: "",
        schema: [
          { name: "name", selector: { text: {} } },
          { name: "icon", selector: { icon: {} } },
        ],
      },
      {
        name: "period",
        selector: { select: { mode: "dropdown", options: VISTE.map((v) => ({ value: v, label: t(v) })) } },
      },
      { name: "show_injection", selector: { boolean: {} } },
    ];
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => {
        const config = { ...ev.detail.value };
        for (const k of Object.keys(config)) if (config[k] === "" || config[k] === undefined) delete config[k];
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    const labels = { pod: "ed_pod", name: "ed_name", icon: "ed_icon", period: "ed_period", show_injection: "ed_show_injection" };
    this._form.hass = this._hass;
    this._form.data = {
      pod: this._pods?.[0]?.slug,
      period: "month",
      show_injection: true,
      ...this._config,
    };
    this._form.schema = this._schema();
    this._form.computeLabel = (s) => traduci(this._hass, labels[s.name] || s.name);
  }
}

// Registering the elements.
//
// HA's frontend replaces window.customElements with a registry of its own (the
// scoped custom element registry polyfill) while starting, and the extra
// modules registered by integrations can run BEFORE that moment: an immediate
// define would end up in the browser's native registry, which HA no longer
// looks at, and the card would be "unknown" (Configuration error). So it waits
// for the <home-assistant> app to be defined - by then the final registry is
// in place - and reads window.customElements only then. Loaded later (e.g. as
// a Lovelace resource) the card registers right away.
const registraElementi = () => {
  const registro = window.customElements;
  if (!registro.get(CARD_TYPE)) registro.define(CARD_TYPE, EdistribuzionePodCard);
  if (!registro.get(EDITOR_TYPE)) registro.define(EDITOR_TYPE, EdistribuzionePodCardEditor);
};

if (window.customElements.get("home-assistant")) {
  registraElementi();
} else {
  window.customElements.whenDefined("home-assistant").then(registraElementi);
  // Pages without the main app (no <home-assistant> to wait for).
  window.addEventListener("load", () => {
    if (!document.querySelector("home-assistant")) registraElementi();
  });
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === CARD_TYPE)) {
  const lang = (navigator.language || "en").toLowerCase().startsWith("it") ? "it" : "en";
  window.customCards.push({
    type: CARD_TYPE,
    name: STRINGS[lang].card_name,
    description: STRINGS[lang].card_description,
    preview: true,
    documentationURL: "https://github.com/fabioscarparo/HomeAssistant-EDistribuzione#pod-lovelace-card",
  });
}

console.info(`%c E-DISTRIBUZIONE POD CARD %c ${VERSION} `,
  "color:#fff;background:#488fc2;font-weight:500", "color:#488fc2;background:transparent");
