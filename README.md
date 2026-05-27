# MedAI — Analizador Inteligente de Diagnósticos

Sistema multiagente de análisis clínico de pacientes usando A2A, RAG, MCP y flujo de IA.

> ⚠️ **DISCLAIMER MÉDICO**: MedAI es una herramienta de APOYO a la decisión clínica con fines
> educativos y demostrativos. NO sustituye el juicio de un profesional médico, la consulta
> presencial ni la valoración por especialistas. No utilizar para decisiones diagnósticas o
> terapéuticas en pacientes reales sin validación profesional.

## Arquitectura

```
Frontend (HTML/CSS/JS)
        ↓ HTTP
Orchestrator Flask :8080  ──── JWT Auth + Flujo de IA
        ↓ A2A (paralelo)
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Médico :8001  │  Riesgos :8002  │  Historia :8003  │  Traductor :8004         │
└─────────────────────────────────────────────────────────────────────────────────┘
        ↓ MCP SSE
 MCP Server :8000
  ├── search_medical_rag()     ← ChromaDB vectorial (RAG clínico)
  ├── search_web()             ← DuckDuckGo restringido a NIH/OMS/NICE/Mayo (Web MCP ⭐)
  ├── get_red_flags_catalog()
  └── explain_medical_term()
        ↓
 shared/chroma_db/            ← Base vectorial (CIE-10, banderas rojas, interacciones, guías)
```

## Requisitos previos

- Python 3.11+
- Google API Key (Gemini)

## Instalación

### 1. Configurar variables de entorno
```bash
# Editar .env con tu Google API Key
nano .env
```

### 2. Instalar dependencias de cada componente

```bash
# MCP Server
cd mcp_server && pip install -r requirements.txt && cd ..

# Agentes (ejecutar en cada carpeta)
cd medico_agent     && pip install -r requirements.txt && cd ..
cd riesgos_agent    && pip install -r requirements.txt && cd ..
cd historia_agent   && pip install -r requirements.txt && cd ..
cd traductor_agent  && pip install -r requirements.txt && cd ..

# Orchestrator
cd orchestrator && pip install -r requirements.txt && cd ..
```

O instalar todo de una vez:
```bash
pip install mcp langchain langchain-google-genai langchain-community chromadb \
            mirascope[google] a2a-sdk[http-server] uvicorn starlette sse-starlette \
            flask flask-cors flask-jwt-extended httpx pypdf duckduckgo-search \
            python-dotenv
```

## Ejecución (6 terminales)

Abre 6 terminales en la carpeta del proyecto y ejecuta en este orden:

**Terminal 1 — MCP Server**
```bash
python mcp_server/server.py
# → http://localhost:8000  (inicializa la base vectorial clínica la primera vez)
```

**Terminal 2 — Médico Diagnosticador**
```bash
python medico_agent/agent.py
# → http://localhost:8001
```

**Terminal 3 — Analista de Riesgos Clínicos**
```bash
python riesgos_agent/agent.py
# → http://localhost:8002
```

**Terminal 4 — Resumidor de Historia Clínica**
```bash
python historia_agent/agent.py
# → http://localhost:8003
```

**Terminal 5 — Traductor Médico-Paciente**
```bash
python traductor_agent/agent.py
# → http://localhost:8004
```

**Terminal 6 — Orchestrator (abre el frontend)**
```bash
python orchestrator/api.py
# → http://localhost:8080
```

Abre el navegador en **http://localhost:8080**

## Credenciales de acceso

| Usuario   | Contraseña   | Rol    |
|-----------|-------------|--------|
| admin     | admin123    | admin  |
| medico    | medico123   | medico |

## Funcionalidades

### Analizar Caso Clínico
- Sube una historia clínica en PDF o pega los datos del paciente
- Los 4 agentes A2A analizan en **paralelo**
- Resultados: diagnóstico diferencial + banderas rojas, riesgos farmacológicos, resumen estructurado, explicación para el paciente

### Búsqueda Vectorial (RAG)
- Búsqueda semántica sobre CIE-10, banderas rojas, interacciones, guías clínicas, glosario médico
- ChromaDB con embeddings de Google

### Web MCP ⭐
- Búsqueda web en tiempo real restringida a fuentes médicas confiables (NIH, OMS, NICE, Mayo Clinic, MedlinePlus, MSD)
- Implementado como herramienta del servidor MCP

## Agentes A2A

| Puerto | Agente                          | Herramientas MCP                                |
|--------|---------------------------------|-------------------------------------------------|
| 8001   | Médico Diagnosticador           | search_medical_rag, get_red_flags_catalog       |
| 8002   | Analista de Riesgos Clínicos    | search_medical_rag, search_web                  |
| 8003   | Resumidor de Historia Clínica   | search_medical_rag                              |
| 8004   | Traductor Médico-Paciente       | explain_medical_term                            |

## Flujo de IA (orchestrator/api.py → `run_analysis_flow`)

```
caso clínico → RAG (contexto) → asyncio.gather([A2A × 4]) → informe agregado
```

## Tecnologías

- **LangChain + ChromaDB** → RAG vectorial clínico
- **Mirascope** → Agentes con herramientas
- **A2A SDK** → Protocolo Agent-to-Agent
- **FastMCP** → Servidor MCP con SSE
- **Flask + JWT** → API REST + autenticación
- **Google Gemini 2.0 Flash** → LLM principal

## Ejemplo de caso para probar

```
Paciente mujer de 62 años, hipertensa y diabética tipo 2 mal controlada (HbA1c 8.9),
consulta por dolor torácico opresivo de 40 minutos de evolución, irradiado a brazo
izquierdo y mandíbula, asociado a diaforesis profusa y náuseas. TA 90/60, FC 110,
SatO2 94%. Medicación habitual: enalapril 20mg, metformina 850mg/12h, AAS 100mg,
atorvastatina 40mg. Alergia a penicilina. Antecedente familiar de IAM en padre a los 58 años.
```
