#!/usr/bin/env python3
"""
Analisi Bollette Luce e Acqua - dashboard locale (Streamlit)

Flusso:
1. Carichi uno o più PDF di bollette (luce e/o acqua).
2. Lo script prova a estrarre da solo: fornitore, periodo, consumo,
   unità di misura, importo totale.
3. Prima di salvare, i valori estratti vengono mostrati in un modulo
   editabile: correggi quel che serve (i layout dei fornitori variano
   molto, l'estrazione automatica è "best effort").
4. I dati confermati finiscono in un archivio SQLite locale
   (bollette.db, nella stessa cartella dello script) che si accumula
   nel tempo.
5. Nella dashboard vedi andamento consumi/costi/costo-unitario nel
   tempo, per tipo di utenza.
6. Nel tab "Confronto mercato" inserisci manualmente un prezzo di
   riferimento (lo trovi es. sul Portale Offerte ARERA o dal PUN per
   la luce) e lo confronti col tuo costo unitario medio/recente.

Pensato per essere esteso in seguito con un import da Home Assistant
(vedi funzione placeholder importa_da_home_assistant() in fondo).
"""

import io
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import streamlit as st


# ============================================================
# CONFIGURAZIONE
# ============================================================

DB_PATH = Path("bollette.db")

FORNITORI_NOTI = [
    "Enel Energia", "Enel", "A2A", "Iren", "Hera Comm", "Hera",
    "Sorgenia", "Edison", "Eni Plenitude", "Plenitude", "Acea",
    "Engie", "Wekiwi", "Pulsee", "Illumia", "NeN", "Octopus Energy",
    "Pavia Acque",
]

UNITA_LUCE = "kWh"
UNITA_ACQUA = "m³"


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bollette (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,              -- 'luce' o 'acqua'
            fornitore TEXT,
            periodo_da TEXT,                 -- YYYY-MM-DD
            periodo_a TEXT,                  -- YYYY-MM-DD
            consumo REAL,
            unita TEXT,
            importo REAL,
            file_origine TEXT,
            caricato_il TEXT
        )
    """)
    conn.commit()
    return conn


def salva_bolletta(conn, dati: dict):
    conn.execute("""
        INSERT INTO bollette
        (tipo, fornitore, periodo_da, periodo_a, consumo, unita, importo, file_origine, caricato_il)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        dati["tipo"], dati["fornitore"], dati["periodo_da"], dati["periodo_a"],
        dati["consumo"], dati["unita"], dati["importo"], dati["file_origine"],
        datetime.now().isoformat(timespec="seconds"),
    ))
    conn.commit()


def carica_tutte(conn) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM bollette ORDER BY periodo_da", conn)
    if not df.empty:
        df["periodo_da"] = pd.to_datetime(df["periodo_da"], errors="coerce")
        df["periodo_a"] = pd.to_datetime(df["periodo_a"], errors="coerce")
        df["costo_unitario"] = df["importo"] / df["consumo"].replace(0, pd.NA)
    return df


# ============================================================
# ESTRAZIONE TESTO PDF
# ============================================================

def estrai_testo(file_bytes: bytes) -> str:
    testo = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                testo.append(t)
    return "\n".join(testo)


# ============================================================
# PARSING (best effort, con estrattori dedicati per fornitori noti)
# ============================================================

def _numero_ita_a_float(s: str) -> float | None:
    """Converte '1.234,56' o '1234.56' o '123,4' in float."""
    if not s:
        return None
    s = s.strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _data_ita_a_iso(d: str) -> str:
    d = d.replace(".", "/")
    try:
        return datetime.strptime(d, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def rileva_tipo(testo: str) -> str:
    t = testo.lower()
    if re.search(r"\bm3\b|\bm³\b|\bmc\b|metri cubi|acquedotto|fognatura|depurazione", t):
        return "acqua"
    return "luce"


def rileva_fornitore(testo: str) -> str:
    for nome in FORNITORI_NOTI:
        if nome.lower() in testo.lower():
            return nome
    return ""


def rileva_periodo(testo: str) -> tuple[str, str]:
    m = re.search(
        r"dal\s+(\d{2}[/.]\d{2}[/.]\d{4})\s+al\s+(\d{2}[/.]\d{2}[/.]\d{4})",
        testo, re.IGNORECASE,
    )
    if not m:
        return "", ""
    return _data_ita_a_iso(m.group(1)), _data_ita_a_iso(m.group(2))


def rileva_consumo(testo: str, tipo: str) -> float | None:
    if tipo == "luce":
        pattern = r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?)\s*kwh"
    else:
        pattern = r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?)\s*(?:m3|m³|mc|metri cubi)"

    candidati = re.findall(pattern, testo, re.IGNORECASE)
    valori = [_numero_ita_a_float(c) for c in candidati]
    valori = [v for v in valori if v and 0 < v < 100000]

    # Euristica: il primo valore trovato è quasi sempre il consumo del
    # periodo fatturato (compare nel riepilogo in testa alla bolletta),
    # mentre valori più grandi trovati più avanti nel testo sono spesso
    # consumi annui/cumulativi. Va comunque sempre verificato a mano.
    return valori[0] if valori else None


def rileva_importo(testo: str) -> float | None:
    pattern = (
        r"(?:totale\s+(?:da\s+pagare|bolletta|fattura|documento)|"
        r"importo\s+(?:totale|bolletta|dovuto))"
        r"[^\d]{0,30}(\d{1,3}(?:\.\d{3})*,\d{2})"
    )
    m = re.search(pattern, testo, re.IGNORECASE)
    if m:
        return _numero_ita_a_float(m.group(1))
    return None


def estrai_generico(testo: str, nome_file: str) -> dict:
    tipo = rileva_tipo(testo)
    periodo_da, periodo_a = rileva_periodo(testo)
    return {
        "tipo": tipo,
        "fornitore": rileva_fornitore(testo),
        "periodo_da": periodo_da,
        "periodo_a": periodo_a,
        "consumo": rileva_consumo(testo, tipo),
        "unita": UNITA_LUCE if tipo == "luce" else UNITA_ACQUA,
        "importo": rileva_importo(testo),
        "file_origine": nome_file,
    }


def estrai_pavia_acque(testo: str, nome_file: str) -> dict:
    """Estrattore dedicato per le bollette acqua di Pavia Acque."""

    dati = estrai_generico(testo, nome_file)
    dati["tipo"] = "acqua"
    dati["fornitore"] = "Pavia Acque"
    dati["unita"] = UNITA_ACQUA

    # Periodo: dalla riga "Consumo del periodo dal DD/MM/YYYY al DD/MM/YYYY"
    m_periodo = re.search(
        r"Consumo del periodo\s+dal\s+(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})",
        testo, re.IGNORECASE,
    )
    if m_periodo:
        dati["periodo_da"] = _data_ita_a_iso(m_periodo.group(1))
        dati["periodo_a"] = _data_ita_a_iso(m_periodo.group(2))

    # Consumo netto fatturato: sul frontespizio è espresso come somma
    # algebrica tipo "Consumo mc 32 - 5 + 1" (lettura - acconto prec. +
    # stima gg mancanti). Sommiamo i termini per ottenere il netto.
    m_cons = re.search(
        r"Consumo\s+mc\s*[:\s]*([\-\+0-9\s]{2,40})(?=Totale\s+bolletta|Scadenza)",
        testo, re.IGNORECASE,
    )
    if m_cons:
        numeri = re.findall(r"[\-\+]?\s*\d+", m_cons.group(1))
        numeri = [n.replace(" ", "") for n in numeri]
        if numeri:
            try:
                dati["consumo"] = float(sum(int(n) for n in numeri))
            except ValueError:
                pass

    # Importo totale: "Totale bolletta € 63,00" oppure "Totale Bolletta 63,00"
    m_imp = re.search(
        r"Totale\s+[Bb]olletta\s*€?\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
        testo,
    )
    if m_imp:
        dati["importo"] = _numero_ita_a_float(m_imp.group(1))

    return dati


def estrai_engie(testo: str, nome_file: str) -> dict:
    """Estrattore dedicato per le bollette luce di Engie."""

    dati = estrai_generico(testo, nome_file)
    dati["tipo"] = "luce"
    dati["fornitore"] = "Engie"
    dati["unita"] = UNITA_LUCE

    # Periodo di fatturazione (non il "periodo di riferimento" annuo)
    m_periodo = re.search(
        r"Periodo di fatturazione:\s*dal\s+(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})",
        testo, re.IGNORECASE,
    )
    if m_periodo:
        dati["periodo_da"] = _data_ita_a_iso(m_periodo.group(1))
        dati["periodo_a"] = _data_ita_a_iso(m_periodo.group(2))

    # Consumo del periodo: preferiamo la riga "Totale kWh 1.006,739 ..."
    # della tabella consumi fatturati (valore preciso), altrimenti il
    # numero accanto a "CONSUMO ELETTRICO" in testa alla bolletta.
    m_cons = re.search(r"Totale\s+kWh\s+(\d{1,3}(?:\.\d{3})*(?:,\d+)?)", testo, re.IGNORECASE)
    if not m_cons:
        m_cons = re.search(
            r"CONSUMO ELETTRICO.*?(\d{1,3}(?:\.\d{3})*(?:,\d+)?)\s*kWh",
            testo, re.IGNORECASE | re.DOTALL,
        )
    if m_cons:
        dati["consumo"] = _numero_ita_a_float(m_cons.group(1))

    # Importo: "TOTALE DA PAGARE ENERGIA 275,19 €", con fallback sul
    # totale mostrato nel riepilogo di prima pagina.
    m_imp = re.search(r"TOTALE\s+DA\s+PAGARE\s+ENERGIA\s+(\d{1,3}(?:\.\d{3})*,\d{2})", testo, re.IGNORECASE)
    if not m_imp:
        m_imp = re.search(r"SINTESI IMPORTI FATTURATI\D{0,20}(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", testo, re.IGNORECASE)
    if m_imp:
        dati["importo"] = _numero_ita_a_float(m_imp.group(1))

    return dati


# Estrattori dedicati, provati in ordine prima del fallback generico.
# Ogni voce: (stringa da cercare nel testo, funzione estrattore).
ESTRATTORI_DEDICATI = [
    ("pavia acque", estrai_pavia_acque),
    ("engie", estrai_engie),
]


def estrai_dati(testo: str, nome_file: str) -> dict:
    t_lower = testo.lower()
    for chiave, funzione in ESTRATTORI_DEDICATI:
        if chiave in t_lower:
            return funzione(testo, nome_file)
    return estrai_generico(testo, nome_file)


# ============================================================
# PLACEHOLDER per estensione futura con Home Assistant
# ============================================================

def importa_da_home_assistant(csv_bytes: bytes) -> pd.DataFrame:
    """
    Da implementare quando colleghiamo HA: leggere un export CSV
    (es. da long-term statistics di un sensore energia) e restituire
    un DataFrame con colonne compatibili (data, consumo_kwh,
    prodotto_kwh, autoconsumo_kwh) da affiancare ai dati di bolletta
    per confrontare consumo dichiarato in bolletta vs misurato da HA.
    """
    raise NotImplementedError("Integrazione Home Assistant non ancora attiva.")


# ============================================================
# STREAMLIT
# ============================================================

st.set_page_config(page_title="Bollette Casa", page_icon="⚡", layout="wide")
st.title("⚡💧 Analisi Bollette Luce e Acqua")

conn = init_db()

tab_carica, tab_dashboard, tab_mercato = st.tabs(
    ["📥 Carica bollette", "📊 Dashboard", "📈 Confronto mercato"]
)


# ------------------------------------------------------------
# TAB: CARICA BOLLETTE
# ------------------------------------------------------------

with tab_carica:

    st.markdown(
        "Carica uno o più PDF. Per ognuno ti mostro i dati che sono "
        "riuscito a leggere: controllali e correggili prima di salvare."
    )

    files = st.file_uploader("PDF bollette", type=["pdf"], accept_multiple_files=True)

    if files:

        for f in files:

            st.divider()
            st.subheader(f"📄 {f.name}")

            file_bytes = f.read()

            try:
                testo = estrai_testo(file_bytes)
            except Exception as e:
                st.error(f"Impossibile leggere il PDF: {e}")
                continue

            if not testo.strip():
                st.warning(
                    "Nessun testo estratto: probabilmente è un PDF scansionato "
                    "(immagine). Questo script gestisce solo PDF con testo "
                    "selezionabile."
                )
                continue

            dati = estrai_dati(testo, f.name)

            with st.form(key=f"form_{f.name}"):

                col1, col2 = st.columns(2)

                with col1:
                    tipo = st.selectbox(
                        "Tipo utenza", ["luce", "acqua"],
                        index=0 if dati["tipo"] == "luce" else 1,
                        key=f"tipo_{f.name}",
                    )
                    fornitore = st.text_input("Fornitore", value=dati["fornitore"], key=f"forn_{f.name}")
                    consumo = st.number_input(
                        "Consumo", value=float(dati["consumo"] or 0.0),
                        min_value=0.0, step=1.0, key=f"cons_{f.name}",
                    )

                with col2:
                    periodo_da = st.text_input(
                        "Periodo dal (YYYY-MM-DD)", value=dati["periodo_da"], key=f"pda_{f.name}"
                    )
                    periodo_a = st.text_input(
                        "Periodo al (YYYY-MM-DD)", value=dati["periodo_a"], key=f"pa_{f.name}"
                    )
                    importo = st.number_input(
                        "Importo totale (€)", value=float(dati["importo"] or 0.0),
                        min_value=0.0, step=0.5, key=f"imp_{f.name}",
                    )

                salva = st.form_submit_button("💾 Salva in archivio")

                if salva:

                    if not periodo_da or not periodo_a or consumo <= 0 or importo <= 0:
                        st.error(
                            "Compila almeno periodo, consumo e importo prima di salvare."
                        )
                    else:
                        salva_bolletta(conn, {
                            "tipo": tipo,
                            "fornitore": fornitore,
                            "periodo_da": periodo_da,
                            "periodo_a": periodo_a,
                            "consumo": consumo,
                            "unita": UNITA_LUCE if tipo == "luce" else UNITA_ACQUA,
                            "importo": importo,
                            "file_origine": f.name,
                        })
                        st.success("Salvato nell'archivio ✅")


# ------------------------------------------------------------
# TAB: DASHBOARD
# ------------------------------------------------------------

with tab_dashboard:

    df = carica_tutte(conn)

    if df.empty:
        st.info("Nessuna bolletta ancora salvata. Vai al tab \"Carica bollette\".")

    else:

        tipo_filtro = st.radio("Utenza", ["Entrambe", "luce", "acqua"], horizontal=True)

        dff = df if tipo_filtro == "Entrambe" else df[df["tipo"] == tipo_filtro]

        if dff.empty:
            st.info("Nessun dato per questa utenza.")

        else:

            c1, c2, c3 = st.columns(3)
            c1.metric("Bollette registrate", len(dff))
            c2.metric("Spesa totale", f"€ {dff['importo'].sum():,.2f}")
            if dff["consumo"].sum() > 0:
                c3.metric(
                    "Costo medio unitario",
                    f"€ {dff['importo'].sum() / dff['consumo'].sum():.4f} / {dff['unita'].iloc[0]}",
                )

            st.subheader("Andamento consumo")
            for tipo, gruppo in dff.groupby("tipo"):
                st.caption(f"{tipo} ({gruppo['unita'].iloc[0]})")
                st.line_chart(gruppo.set_index("periodo_da")["consumo"])

            st.subheader("Andamento importo")
            for tipo, gruppo in dff.groupby("tipo"):
                st.caption(tipo)
                st.line_chart(gruppo.set_index("periodo_da")["importo"])

            st.subheader("Andamento costo unitario (€ / unità di misura)")
            for tipo, gruppo in dff.groupby("tipo"):
                st.caption(f"{tipo} — € per {gruppo['unita'].iloc[0]}")
                st.line_chart(gruppo.set_index("periodo_da")["costo_unitario"])

            st.subheader("Confronto anno su anno")
            dff = dff.copy()
            dff["anno"] = dff["periodo_da"].dt.year
            riepilogo = dff.groupby(["anno", "tipo"]).agg(
                consumo_totale=("consumo", "sum"),
                spesa_totale=("importo", "sum"),
                bollette=("id", "count"),
            ).reset_index()
            st.dataframe(riepilogo, use_container_width=True)

            with st.expander("📋 Dati grezzi"):
                st.dataframe(
                    dff[["tipo", "fornitore", "periodo_da", "periodo_a", "consumo", "unita", "importo", "costo_unitario"]],
                    use_container_width=True,
                )

            csv = dff.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Esporta CSV", csv, "bollette_export.csv", "text/csv")


# ------------------------------------------------------------
# TAB: CONFRONTO MERCATO
# ------------------------------------------------------------

with tab_mercato:

    st.markdown(
        "Qui confronti il tuo costo unitario medio (dalle bollette salvate) "
        "con un prezzo di riferimento del mercato attuale, che inserisci a mano.\n\n"
        "Dove trovare un riferimento aggiornato:\n"
        "- **Luce**: PUN (Prezzo Unico Nazionale) su [mercatoelettrico.org]"
        "(https://www.mercatoelettrico.org), oppure il Portale Offerte ARERA "
        "[ilportaleofferte.it](https://www.ilportaleofferte.it)\n"
        "- **Acqua**: tariffe pubblicate dal tuo gestore/ATO locale "
        "(non esiste un mercato libero per l'acqua in Italia, quindi qui il "
        "confronto serve più che altro a capire se le tariffe applicate sono "
        "cambiate nel tempo)"
    )

    df = carica_tutte(conn)

    if df.empty:
        st.info("Carica prima qualche bolletta.")

    else:

        tipo_sel = st.selectbox("Utenza da confrontare", ["luce", "acqua"])
        dfs = df[df["tipo"] == tipo_sel]

        if dfs.empty:
            st.info(f"Nessuna bolletta di tipo {tipo_sel} salvata.")

        else:

            costo_medio = dfs["importo"].sum() / dfs["consumo"].sum()
            ultima = dfs.sort_values("periodo_da").iloc[-1]

            col1, col2 = st.columns(2)
            col1.metric("Tuo costo medio storico", f"€ {costo_medio:.4f} / {dfs['unita'].iloc[0]}")
            col2.metric(
                f"Tuo costo ultima bolletta ({ultima['periodo_da'].date()})",
                f"€ {ultima['costo_unitario']:.4f} / {ultima['unita']}",
            )

            prezzo_mercato = st.number_input(
                f"Prezzo di riferimento di mercato attuale (€ / {dfs['unita'].iloc[0]})",
                min_value=0.0, step=0.01, format="%.4f",
            )

            if prezzo_mercato > 0:
                delta_medio = (costo_medio - prezzo_mercato) / prezzo_mercato * 100
                delta_ultima = (ultima["costo_unitario"] - prezzo_mercato) / prezzo_mercato * 100

                st.write(
                    f"Rispetto al riferimento inserito, il tuo costo medio storico è "
                    f"**{delta_medio:+.1f}%**, quello dell'ultima bolletta è "
                    f"**{delta_ultima:+.1f}%**."
                )

                if delta_ultima > 10:
                    st.warning(
                        "L'ultima bolletta è sensibilmente più cara del riferimento: "
                        "potrebbe valere la pena controllare offerte alternative."
                    )
                elif delta_ultima < -10:
                    st.success("Sei sotto il riferimento di mercato: offerta buona.")
