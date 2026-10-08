/*
 * E-Distribuzione POD card
 *
 * Servita dall'integrazione stessa (vedi lovelace_card.py): nessuna risorsa
 * da aggiungere a mano. Legge le external statistics scritte da
 * statistics.py tramite il websocket del Recorder, le stesse che usa la
 * Energy Dashboard.
 *
 * Il linguaggio visivo ricalca quello nativo di Home Assistant, usando i suoi
 * token invece di valori propri:
 *   - intestazione come la tile card (icona 36px con fondo al 20%);
 *   - selettore del periodo come ha-control-select;
 *   - grafico come le card energia: barre impilate col riempimento al 50% e
 *     bordo pieno da 1.5px, una sfumatura LAB di
 *     --energy-grid-consumption-color per ogni fascia (lo stesso algoritmo
 *     con cui la Energy Dashboard distingue più contatori della rete),
 *     immissione in negativo con --energy-grid-return-color;
 *   - tooltip, griglia ed etichette con i colori del tema ECharts di HA.
 *
 * Nessuna dipendenza: web component puro, un solo file.
 */

const CARD_TYPE = "edistribuzione-pod-card";
const EDITOR_TYPE = "edistribuzione-pod-card-editor";
const VERSION = "0.4.0";
const SOURCE = "edistribuzione";
const FASCE = ["f1", "f2", "f3"];
const VISTE = ["day", "week", "month", "year"];
const HOUR_MS = 3600000;

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
    no_pod: "Nessun POD E-Distribuzione trovato. Configura l'integrazione e attendi il primo import.",
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
    no_pod: "No E-Distribuzione POD found. Configure the integration and wait for the first import.",
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

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// --- Date civili nel fuso del server -------------------------------------------
//
// I bucket giornalieri/mensili del Recorder sono calcolati nel fuso configurato
// in Home Assistant, non in quello del browser: tutti i confini di periodo si
// calcolano quindi in hass.config.time_zone. Una "data civile" è {y, m, d}
// con m 0-based; l'aritmetica passa da Date.UTC, che normalizza da sola.

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

// --- Colori: stessa sfumatura LAB della Energy Dashboard -----------------------

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

// Kn = 18, come labBrighten/labDarken del frontend di HA: in tema chiaro le
// serie successive scuriscono, in tema scuro schiariscono.
const sfumatura = (rgb, indice, scuro) => {
  if (!indice) return rgb;
  const lab = rgbToLab(rgb);
  lab[0] += (scuro ? 18 : -18) * indice;
  return labToRgb(lab);
};

const rgbCss = (rgb, alpha) => (alpha === undefined ? `rgb(${rgb.join(",")})` : `rgba(${rgb.join(",")},${alpha})`);

// --- Scala dell'asse ----------------------------------------------------------

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
    // Nessun POD esplicito: la card prende da sola il primo trovato.
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
    // hass cambia a ogni evento di stato: si ridisegna solo se cambia
    // qualcosa che la card usa davvero (tema, lingua, formato numeri).
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
  }

  disconnectedCallback() {
    this._ro?.disconnect();
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  // --- Dati ------------------------------------------------------------------

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
      };
      const nomeImmessa = nostre.get(ids.immessa)?.name || "";
      pods.push({
        slug,
        codice: slug.toUpperCase(),
        nome: meta.name,
        ids,
        haFasce: FASCE.every((f) => nostre.has(ids[f])),
        haImmessa: nostre.has(ids.immessa),
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
    // Due richieste economiche: i mesi con dati (primo e ultimo), poi le ore
    // dell'ultimo mese per sapere fino a che ora arrivano i dati.
    const id = this._pod.ids.totale;
    const mesi = (await this._statistiche([id], new Date(Date.UTC(2015, 0, 1)), null, "month"))[id];
    if (!mesi.length) return { primo: null, ultimo: null };
    const ultimoMese = mesi[mesi.length - 1];
    const ore = (await this._statistiche([id], new Date(ultimoMese.start), null, "hour"))[id];
    return {
      primo: mesi[0].start,
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
      /* weekInfo non supportato: lunedì */
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

    try {
      const res = await this._statistiche(ids, iv.inizio, iv.fine, iv.period);
      if (richiesta !== this._richiesta) return; // navigazione più recente in corso
      const valori = iv.slot.map(() => ({ totale: null, f1: null, f2: null, f3: null, immessa: null }));
      const chiavi = { [pod.ids.totale]: "totale", [pod.ids.f1]: "f1", [pod.ids.f2]: "f2", [pod.ids.f3]: "f3", [pod.ids.immessa]: "immessa" };
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

  // --- Formattazione ---------------------------------------------------------

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
      // Composto a mano invece di formatRange: in alcune lingue formatRange
      // aggiunge lo zero iniziale ai giorni ("04 ott").
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
      // Con l'orologio a 12 ore "3 AM" invece di "03:00 AM": più corto, come
      // le etichette dei grafici nativi.
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

  // --- Colori dal tema corrente ----------------------------------------------

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
    };
    return c;
  }

  // --- Rendering -------------------------------------------------------------

  _render() {
    if (!this._hass) return;
    const t = (k, v) => traduci(this._hass, k, v);
    const pod = this._pod;

    if (this._pods && !pod) {
      this._modo = "vuoto";
      this.shadowRoot.innerHTML = `<style>${STILE}</style>
        <ha-card><div class="empty-card"><ha-alert alert-type="info">${escapeHtml(t("no_pod"))}</ha-alert></div></ha-card>`;
      return;
    }
    if (this._modo !== "card") this._struttura();

    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const colori = this._colori();
    $("ha-card").style.setProperty("--edist-accent", rgbCss(colori.totale));

    // intestazione
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

    // selettore periodo e navigazione
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

    // totali e ripartizione per fascia
    const valori = this._dati?.valori || [];
    const somma = (k) => valori.reduce((acc, v) => (v[k] === null ? acc : (acc ?? 0) + v[k]), null);
    const totFasce = FASCE.map((f) => somma(f) || 0);
    const sommaFasce = totFasce.reduce((a, b) => a + b, 0);
    const prelievo = somma("totale") ?? (sommaFasce || null);
    const mostraImmessa = !!pod?.haImmessa && this._config.show_injection !== false;
    const totale = (etichetta, colore, valore) => `
      <div class="total">
        <div class="total-label"><span class="dot" style="background:${rgbCss(colore)}"></span>${escapeHtml(etichetta)}</div>
        <div class="total-value">${this._numero(valore)}<span class="unit">kWh</span></div>
      </div>`;
    $(".totals").innerHTML = totale(t("withdrawn"), colori.totale, prelievo)
      + (mostraImmessa ? totale(pod.produzione ? t("produced") : t("injected"), colori.immessa, somma("immessa")) : "");

    $(".fasce").hidden = !pod?.haFasce;
    if (pod?.haFasce) {
      $(".split").setAttribute("aria-label",
        FASCE.map((f, i) => `${f.toUpperCase()} ${this._percentuale(totFasce[i], sommaFasce)}`).join(", "));
      $(".split").innerHTML = sommaFasce > 0
        ? FASCE.map((f, i) => (totFasce[i] > 0
          ? `<span style="flex-grow:${totFasce[i]};background:${rgbCss(colori[f])}"></span>` : "")).join("")
        : `<span class="vuoto"></span>`;
      $(".legend").innerHTML = FASCE.map((f, i) => `
        <div class="legend-item">
          <div class="legend-name"><span class="dot" style="background:${rgbCss(colori[f])}"></span>${f.toUpperCase()}
            <span class="legend-pct">${this._percentuale(totFasce[i], sommaFasce)}</span></div>
          <div class="legend-kwh">${sommaFasce > 0 ? `${this._numero(totFasce[i])} kWh` : "-"}</div>
        </div>`).join("");
    }

    $(".chart").classList.toggle("loading", this._caricamento);
    this._larghezza = $(".chart").clientWidth || this._larghezza;
    this._renderGrafico();
  }

  _struttura() {
    // Struttura creata una volta sola: gli aggiornamenti successivi toccano
    // solo testi e attributi, così icone e pulsanti non lampeggiano a ogni
    // cambio di periodo.
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
          <div class="error"></div>
          <div class="totals"></div>
          <div class="fasce" hidden>
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
      // Si resta sull'ultimo giorno con dati se cade nel nuovo periodo,
      // altrimenti sul giorno già visualizzato.
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

    const positivo = valori.map((v) => serie.reduce((a, k) => a + (v[k] || 0), 0));
    const negativo = valori.map((v) => (mostraImmessa ? -(v.immessa || 0) : 0));
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
    // griglia (splitLine del valueAxis nel tema ECharts di HA)
    scala.ticks.forEach((v, i) => {
      const yy = Math.round(y(v)) + 0.5;
      parti.push(`<line class="grid" x1="${left}" x2="${W - right}" y1="${yy}" y2="${yy}"/>`);
      parti.push(`<text class="ylab" x="${left - 8}" y="${yy + 4}">${escapeHtml(etichetteY[i])}</text>`);
    });
    parti.push(`<text class="ylab" x="${left - 8}" y="${top - 12}">kWh</text>`);
    parti.push(`<rect class="hover-band" x="0" y="${top}" width="0" height="${plotH}" />`);

    // Come le card energia native: il segmento più esterno di ogni pila ha
    // gli angoli esterni arrotondati a 4px (sopra per il prelievo, sotto per
    // l'immissione), gli altri restano squadrati.
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
      if (negativo[i]) parti.push(segmento(x, y(0), y(negativo[i]), colori.immessa, false, true));
    }

    // etichette dell'asse X senza sovrapposizioni
    const stretta = iv.period === "month" && slotW < 30;
    // Larghezza stimata dall'etichetta più lunga, così il passo si adatta a
    // lingua e formato orario (es. "12 PM", "mer", "Sep") senza sovrapposizioni.
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
      tip.style.top = `${top}px`;
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
      // Il totale serve solo se le fasce sono più di una (nelle ore è sempre una).
      if (v.totale !== null && presenti.length !== 1) righe.push(`<div class="tip-row tip-total"><span class="tip-name">${escapeHtml(t("withdrawn"))}</span><span class="tip-value">${this._numero(v.totale)} kWh</span></div>`);
    } else if (v.totale !== null) {
      righe.push(riga(colori.totale, t("withdrawn"), v.totale));
    }
    if (mostraImmessa && v.immessa) {
      righe.push(riga(colori.immessa, this._pod.produzione ? t("produced") : t("injected"), v.immessa));
    }
    if (!righe.length) righe.push(`<div class="tip-row tip-empty">-</div>`);
    return `<div class="tip-title">${escapeHtml(this._titoloSlot(i))}</div>${righe.join("")}`;
  }
}

const STILE = `
  :host { display: block; }
  ha-card { height: 100%; overflow: hidden; }
  .empty-card { padding: 16px; }

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

  /* come ha-control-select */
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
  .fasce[hidden] { display: none; }
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
  svg { display: block; overflow: visible; font-family: var(--ha-font-family-body, Roboto, Noto, sans-serif); }
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

  /* come il tooltip del tema ECharts di HA */
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

// --- Editor visuale (ha-form, come le card native) -------------------------------

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

// Registrazione degli elementi.
//
// Il frontend di HA sostituisce window.customElements con un proprio registro
// (polyfill degli scoped custom element registry) durante l'avvio, e i moduli
// extra registrati dalle integrazioni possono essere eseguiti PRIMA di quel
// momento: un define immediato finirebbe nel registro nativo del browser, che
// HA poi non consulta più, e la card risulterebbe "sconosciuta" (Errore di
// configurazione). Si aspetta quindi che l'app <home-assistant> sia definita
// - a quel punto il registro definitivo è al suo posto - e si legge
// window.customElements solo allora. Caricata più tardi (es. come risorsa
// Lovelace) la card si registra subito.
const registraElementi = () => {
  const registro = window.customElements;
  if (!registro.get(CARD_TYPE)) registro.define(CARD_TYPE, EdistribuzionePodCard);
  if (!registro.get(EDITOR_TYPE)) registro.define(EDITOR_TYPE, EdistribuzionePodCardEditor);
};

if (window.customElements.get("home-assistant")) {
  registraElementi();
} else {
  window.customElements.whenDefined("home-assistant").then(registraElementi);
  // Pagine senza l'app principale (nessun <home-assistant> da attendere).
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
    documentationURL: "https://github.com/maurobraggio/HomeAssistant-EDistribuzione",
  });
}

console.info(`%c E-DISTRIBUZIONE POD CARD %c ${VERSION} `,
  "color:#fff;background:#488fc2;font-weight:500", "color:#488fc2;background:transparent");
