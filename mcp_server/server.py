import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

from mcp.server.fastmcp import FastMCP
from shared.rag import search_rag, init_rag

mcp = FastMCP("MedAI-ClinicalServer", host="0.0.0.0", port=8000)

# Pre-inicializar la base vectorial al arrancar
init_rag()
print("[MCP] Base vectorial clínica lista.")


# ── Herramienta 1: Búsqueda RAG clínica ──────────────────────────────────────

@mcp.tool()
def search_medical_rag(query: str) -> dict:
    """Busca en la base de conocimiento clínica (CIE-10, banderas rojas, interacciones
    farmacológicas, guías clínicas y glosario médico) usando búsqueda semántica vectorial.

    Args:
        query: Consulta en lenguaje natural sobre síntomas, diagnósticos, tratamientos
               o términos médicos.
    """
    results = search_rag(query, k=4)
    return {
        "query": query,
        "total_resultados": len(results),
        "resultados": results,
    }


# ── Herramienta 2: Búsqueda Web médica (Web MCP) ─────────────────────────────

@mcp.tool()
def search_web(query: str) -> dict:
    """Busca información médica actualizada en fuentes web confiables (NIH, OMS, NICE,
    Mayo Clinic, MedlinePlus) usando DuckDuckGo. Útil para guías recientes, alertas
    farmacológicas o información no disponible en la base de conocimiento local.

    Args:
        query: Consulta de búsqueda médica.
    """
    try:
        from ddgs import DDGS
        # Restringimos a fuentes médicas confiables para reducir ruido
        medical_query = f"{query} (site:nih.gov OR site:who.int OR site:nice.org.uk OR site:mayoclinic.org OR site:medlineplus.gov OR site:msdmanuals.com)"
        with DDGS() as ddgs:
            raw = list(ddgs.text(medical_query, max_results=3))
        results = [
            {
                "titulo": r.get("title", ""),
                "url":    r.get("href", ""),
                "texto":  r.get("body", "")[:400],
            }
            for r in raw
        ]
        return {"query": query, "fuente": "DuckDuckGo (fuentes médicas)", "resultados": results}
    except Exception as e:
        return {"query": query, "error": str(e), "resultados": []}


# ── Herramienta 3: Catálogo de banderas rojas clínicas ───────────────────────

@mcp.tool()
def get_red_flags_catalog() -> dict:
    """Retorna el catálogo completo de banderas rojas clínicas (signos y síntomas de
    alarma que requieren evaluación urgente o derivación inmediata).
    """
    catalog = [
        {"id": 1,  "nombre": "Cefalea en trueno (thunderclap)",              "severidad": "CRÍTICA", "sospechar": "Hemorragia subaracnoidea, disección arterial"},
        {"id": 2,  "nombre": "Dolor torácico opresivo con irradiación",       "severidad": "CRÍTICA", "sospechar": "Síndrome coronario agudo, disección aórtica"},
        {"id": 3,  "nombre": "Disnea súbita con SatO2 <92%",                  "severidad": "CRÍTICA", "sospechar": "TEP, neumotórax, edema pulmonar"},
        {"id": 4,  "nombre": "Pérdida de peso involuntaria >5% en 6 meses",   "severidad": "ALTA",    "sospechar": "Neoplasia, hipertiroidismo, EII, TBC"},
        {"id": 5,  "nombre": "Fiebre persistente >3 semanas sin foco",        "severidad": "ALTA",    "sospechar": "Endocarditis, TBC, linfoma, autoinmune"},
        {"id": 6,  "nombre": "Sangrado digestivo (hematemesis, melenas)",     "severidad": "CRÍTICA", "sospechar": "Úlcera sangrante, varices, neoplasia"},
        {"id": 7,  "nombre": "Síncope de esfuerzo o con pródromos cardíacos", "severidad": "ALTA",    "sospechar": "Estenosis aórtica, miocardiopatía, arritmia"},
        {"id": 8,  "nombre": "Alteración aguda del nivel de conciencia",      "severidad": "CRÍTICA", "sospechar": "Sepsis, ACV, hipoglucemia, encefalitis"},
        {"id": 9,  "nombre": "Lumbalgia con déficit neurológico/incontinencia","severidad": "CRÍTICA", "sospechar": "Cola de caballo, compresión medular"},
        {"id": 10, "nombre": "Hemoptisis abundante o recurrente",             "severidad": "ALTA",    "sospechar": "Cáncer broncopulmonar, TBC, TEP"},
        {"id": 11, "nombre": "Ictericia indolora progresiva",                 "severidad": "ALTA",    "sospechar": "Cáncer de páncreas, colangiocarcinoma"},
        {"id": 12, "nombre": "Adenopatía supraclavicular",                    "severidad": "CRÍTICA", "sospechar": "Cáncer gástrico/pulmonar, linfoma"},
        {"id": 13, "nombre": "Cefalea de novo en >50 años",                   "severidad": "ALTA",    "sospechar": "Arteritis de células gigantes, tumor"},
        {"id": 14, "nombre": "Abdomen en tabla con defensa",                  "severidad": "CRÍTICA", "sospechar": "Perforación, peritonitis, isquemia"},
    ]
    return {"total": len(catalog), "banderas_rojas": catalog}


# ── Herramienta 4: Explicar término médico ───────────────────────────────────

@mcp.tool()
def explain_medical_term(term: str) -> dict:
    """Explica un término médico en lenguaje sencillo para el paciente, basándose en
    el glosario clínico de la base de conocimiento.

    Args:
        term: Término médico a explicar (ej: 'disnea', 'icteria', 'comorbilidad').
    """
    results = search_rag(f"definición y explicación de {term}", k=2)
    context = "\n".join([r["contenido"] for r in results])
    return {
        "termino": term,
        "contexto_clinico": context[:800] if context else "Término no encontrado en la base de conocimiento.",
        "fuentes": [r["fuente"] for r in results],
    }


if __name__ == "__main__":
    print("[MCP Server] MedAI Clinical Server iniciando en http://localhost:8000")
    print("[MCP Server] Herramientas: search_medical_rag, search_web, get_red_flags_catalog, explain_medical_term")
    mcp.run(transport="sse")
