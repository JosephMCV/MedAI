import asyncio
import os
import sys
import uuid
import json
import io
import queue
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity, decode_token,
)
import httpx
from shared.rag import search_rag, init_rag

MEDICO_URL    = "http://localhost:8001"
RIESGOS_URL   = "http://localhost:8002"
HISTORIA_URL  = "http://localhost:8003"
TRADUCTOR_URL = "http://localhost:8004"
MCP_URL       = "http://localhost:8000"

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "../frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR)
CORS(app)

app.config["JWT_SECRET_KEY"] = os.getenv(
    "JWT_SECRET_KEY",
    "medai-development-secret-key-2026-please-rotate-in-production-min-32-bytes",
)
jwt = JWTManager(app)

# ── Usuarios demo ─────────────────────────────────────────────────────────────

USERS = {
    "admin":   {"password": "admin123",   "nombre": "Administrador MedAI",   "rol": "admin"},
    "medico":  {"password": "medico123",  "nombre": "Dr. Demo",              "rol": "medico"},
}

# ── Inicializar RAG al arrancar ───────────────────────────────────────────────

init_rag()
print("[Orchestrator] RAG clínico listo.")


# ── Autenticación ─────────────────────────────────────────────────────────────

@app.route("/login", methods=["POST"])
def login():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    user = USERS.get(username)
    if not user or user["password"] != password:
        return jsonify({"error": "Credenciales incorrectas"}), 401

    token = create_access_token(identity=username)
    return jsonify({
        "token":   token,
        "usuario": username,
        "nombre":  user["nombre"],
        "rol":     user["rol"],
    })

@app.route("/me", methods=["GET"])
@jwt_required()
def me():
    username = get_jwt_identity()
    user = USERS.get(username, {})
    return jsonify({"usuario": username, "nombre": user.get("nombre"), "rol": user.get("rol")})


# ── Helper A2A ────────────────────────────────────────────────────────────────

async def call_a2a_agent(agent_url: str, text: str) -> str:
    message_id = str(uuid.uuid4())
    payload = {
        "message": {
            "message_id": message_id,
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
    }
    async with httpx.AsyncClient(timeout=420.0) as client:
        try:
            resp = await client.post(
                f"{agent_url}/message:send",
                json=payload,
                headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
            )
            if resp.status_code != 200:
                return json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:200]}"})

            result = resp.json()
            task = result.get("task") or result.get("result", {}).get("task", {})
            if task:
                parts = task.get("status", {}).get("message", {}).get("parts", [])
                texts = [p.get("text", "") for p in parts if p.get("text")]
                return " ".join(texts) or json.dumps({"error": "Sin respuesta del agente"})

            return json.dumps({"error": "Formato de respuesta inesperado"})
        except httpx.ConnectError:
            return json.dumps({"error": f"Agente no disponible en {agent_url}"})
        except Exception as e:
            return json.dumps({"error": str(e)})


def _parse_agent_json(raw: str) -> dict:
    """Extrae el JSON de la respuesta del agente (que puede tener texto adicional)."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    import re
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {"raw": raw}


# ── Flujo de IA: Análisis clínico multiagente ────────────────────────────────
# FLUJO DE IA principal: orquesta los 4 agentes en paralelo (A2A)

async def run_analysis_flow(caso_clinico: str) -> dict:
    """
    Flujo de IA principal:
    1. RAG: recupera contexto clínico relevante
    2. A2A paralelo: los 4 agentes analizan simultáneamente
    3. Agrega resultados en un informe final
    """
    # Paso 1: contexto RAG
    rag_results = search_rag(caso_clinico[:500], k=3)
    contexto_clinico = "\n".join([r["contenido"][:300] for r in rag_results])

    # Paso 2: lanzar los 4 agentes en paralelo (A2A)
    prompt = f"{caso_clinico}\n\n---\nCONTEXTO CLÍNICO ADICIONAL (RAG):\n{contexto_clinico}"

    tasks = await asyncio.gather(
        call_a2a_agent(MEDICO_URL,    prompt),
        call_a2a_agent(RIESGOS_URL,   prompt),
        call_a2a_agent(HISTORIA_URL,  prompt),
        call_a2a_agent(TRADUCTOR_URL, prompt),
        return_exceptions=True,
    )

    medico_raw, riesgos_raw, historia_raw, traductor_raw = tasks

    # Paso 3: parsear y agregar
    return {
        "medico_diagnosticador": _parse_agent_json(
            medico_raw if isinstance(medico_raw, str) else str(medico_raw)
        ),
        "analista_riesgos": _parse_agent_json(
            riesgos_raw if isinstance(riesgos_raw, str) else str(riesgos_raw)
        ),
        "historia_clinica": _parse_agent_json(
            historia_raw if isinstance(historia_raw, str) else str(historia_raw)
        ),
        "traductor_paciente": _parse_agent_json(
            traductor_raw if isinstance(traductor_raw, str) else str(traductor_raw)
        ),
        "contexto_rag_usado": [r["fuente"] for r in rag_results],
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
@jwt_required()
def analyze():
    caso_clinico = ""

    if "file" in request.files:
        f = request.files["file"]
        if f.filename.endswith(".pdf"):
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(f.read()))
                for page in reader.pages:
                    caso_clinico += (page.extract_text() or "") + "\n"
            except Exception as e:
                return jsonify({"error": f"Error leyendo PDF: {e}"}), 400
        else:
            caso_clinico = f.read().decode("utf-8", errors="ignore")
    else:
        caso_clinico = (request.json or {}).get("texto", "")

    if len(caso_clinico.strip()) < 50:
        return jsonify({"error": "El caso clínico es demasiado corto"}), 400

    username = get_jwt_identity()
    try:
        result = asyncio.run(run_analysis_flow(caso_clinico))
        return jsonify({
            "status":   "ok",
            "usuario":  username,
            "analisis": result,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/analyze/stream", methods=["POST"])
@jwt_required()
def analyze_stream():
    """SSE endpoint: emite eventos a medida que cada agente termina."""
    username = get_jwt_identity()

    caso_clinico = (request.json or {}).get("texto", "")
    if len(caso_clinico.strip()) < 50:
        return jsonify({"error": "El caso clínico es demasiado corto"}), 400

    q: queue.Queue = queue.Queue()

    def _emit(event: str, data: dict):
        q.put(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

    def run_agents_in_thread():
        async def main():
            try:
                # 1. RAG context
                _emit("rag_start", {})
                rag_results = search_rag(caso_clinico[:500], k=3)
                contexto = "\n".join([r["contenido"][:300] for r in rag_results])
                _emit("rag_done", {"fuentes": [r["fuente"] for r in rag_results]})

                prompt = f"{caso_clinico}\n\n---\nCONTEXTO CLÍNICO (RAG):\n{contexto}"

                # 2. Lanzar agentes en paralelo, emitir según completen
                agentes = {
                    "medico":    MEDICO_URL,
                    "riesgos":   RIESGOS_URL,
                    "historia":  HISTORIA_URL,
                    "traductor": TRADUCTOR_URL,
                }

                for name in agentes:
                    _emit("agent_start", {"agent": name})

                async def _run(name: str, url: str):
                    raw = await call_a2a_agent(url, prompt)
                    return name, raw

                tasks = [asyncio.create_task(_run(n, u)) for n, u in agentes.items()]

                for fut in asyncio.as_completed(tasks):
                    name, raw = await fut
                    parsed = _parse_agent_json(raw if isinstance(raw, str) else str(raw))
                    _emit("agent_done", {"agent": name, "data": parsed})

                _emit("complete", {"usuario": username})
            except Exception as e:
                import traceback; traceback.print_exc()
                _emit("error", {"message": str(e)})
            finally:
                q.put(None)

        asyncio.run(main())

    threading.Thread(target=run_agents_in_thread, daemon=True).start()

    @stream_with_context
    def generate():
        # keepalive comment para forzar flush inicial
        yield ": connected\n\n"
        while True:
            msg = q.get()
            if msg is None:
                break
            yield msg

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/search", methods=["GET"])
@jwt_required()
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Parámetro 'q' requerido"}), 400
    results = search_rag(q, k=5)
    return jsonify({"query": q, "resultados": results})


@app.route("/mcp-search", methods=["GET"])
@jwt_required()
def mcp_web_search():
    """Búsqueda web médica usando el MCP Server (Web MCP)."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Parámetro 'q' requerido"}), 400

    async def _search():
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                from ddgs import DDGS
                medical_query = f"{q} (site:nih.gov OR site:who.int OR site:nice.org.uk OR site:mayoclinic.org OR site:medlineplus.gov OR site:msdmanuals.com)"
                with DDGS() as ddgs:
                    raw = list(ddgs.text(medical_query, max_results=4))
                return [
                    {"titulo": r.get("title",""), "url": r.get("href",""), "texto": r.get("body","")[:300]}
                    for r in raw
                ]
            except Exception as e:
                return [{"error": str(e)}]

    results = asyncio.run(_search())
    return jsonify({"query": q, "fuente": "Web MCP (fuentes médicas)", "resultados": results})


@app.route("/agents-status", methods=["GET"])
@jwt_required()
def agents_status():
    async def _ping():
        status = {}
        agents = {
            "mcp_server":            f"{MCP_URL}/sse",
            "medico_diagnosticador": f"{MEDICO_URL}/.well-known/agent.json",
            "analista_riesgos":      f"{RIESGOS_URL}/.well-known/agent.json",
            "historia_clinica":      f"{HISTORIA_URL}/.well-known/agent.json",
            "traductor_paciente":    f"{TRADUCTOR_URL}/.well-known/agent.json",
        }
        async with httpx.AsyncClient(timeout=3.0) as client:
            for name, url in agents.items():
                try:
                    if name == "mcp_server":
                        async with client.stream("GET", url) as r:
                            status[name] = "online" if r.status_code < 500 else "error"
                    else:
                        r = await client.get(url)
                        status[name] = "online" if r.status_code < 500 else "error"
                except Exception:
                    status[name] = "offline"
        return status

    return jsonify(asyncio.run(_ping()))


# ── Frontend estático ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND_DIR, filename)


if __name__ == "__main__":
    print("[Orchestrator] MedAI API disponible en http://localhost:8080")
    app.run(host="0.0.0.0", port=8080, debug=True)
