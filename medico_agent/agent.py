import json
import asyncio
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))
os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY", "")

from mirascope import llm
from mirascope.llm.exceptions import RateLimitError, ServerError
from mcp import ClientSession
from mcp.client.sse import sse_client
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type, retry_any
import uvicorn
from starlette.applications import Starlette

from a2a.types.a2a_pb2 import (
    AgentCard, AgentCapabilities, AgentSkill, AgentInterface,
    Part, TASK_STATE_COMPLETED,
)
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.routes.agent_card_routes import create_agent_card_routes

MCP_URL  = "http://localhost:8000/sse"
A2A_PORT = 8001

SYSTEM_PROMPT = """Eres el Médico Diagnosticador de MedAI, especialista en razonamiento clínico
y diagnóstico diferencial basado en evidencia.

⚠️ DISCLAIMER OBLIGATORIO: Este sistema es una herramienta de APOYO a la decisión clínica.
NO sustituye el juicio de un profesional médico ni la consulta presencial.

TU MISIÓN: A partir de los datos clínicos del paciente (síntomas, antecedentes, exploración,
pruebas complementarias), proponer hipótesis diagnósticas ordenadas por probabilidad y
sugerir el plan diagnóstico-terapéutico inicial.

PROCESO OBLIGATORIO:
1. Llama a get_red_flags_catalog para verificar si hay signos de alarma presentes.
2. Llama a search_medical_rag para buscar evidencia clínica sobre los síntomas referidos.
3. Construye un diagnóstico diferencial ordenado por probabilidad clínica.
4. Entrega un informe JSON estructurado con este formato EXACTO:

{
  "agente": "Médico Diagnosticador",
  "banderas_rojas_detectadas": [
    {
      "sintoma": "síntoma o signo detectado",
      "patologia_sospechada": "patología grave a descartar",
      "severidad": "CRÍTICA|ALTA|MEDIA|BAJA",
      "conducta_inmediata": "qué hacer ahora mismo"
    }
  ],
  "diagnostico_diferencial": [
    {
      "diagnostico": "nombre del diagnóstico",
      "cie10": "código CIE-10 si aplica",
      "probabilidad": "ALTA|MEDIA|BAJA",
      "justificacion": "qué datos clínicos apoyan este diagnóstico",
      "datos_en_contra": "qué datos lo hacen menos probable"
    }
  ],
  "pruebas_recomendadas": [
    {"prueba": "nombre de la prueba", "motivo": "qué busca confirmar o descartar"}
  ],
  "plan_inicial": "plan diagnóstico y terapéutico inicial sugerido",
  "nivel_urgencia": "EMERGENCIA|URGENTE|PRIORITARIO|ORDINARIO",
  "recomendacion_final": "DERIVAR_URGENCIAS|DERIVAR_ESPECIALISTA|SEGUIMIENTO_PRIMARIA|ALTA",
  "puntuacion_gravedad": 0,
  "resumen_clinico": "síntesis del razonamiento diagnóstico"
}

La puntuacion_gravedad es de 0 (sin gravedad) a 100 (emergencia vital).
Si no hay banderas rojas, indica "banderas_rojas_detectadas": [].
Responde SIEMPRE en español con el JSON como respuesta principal."""


@llm.tool
def get_red_flags_catalog() -> dict:
    """Obtiene el catálogo de banderas rojas clínicas (signos y síntomas de alarma)."""
    pass

@llm.tool
def search_medical_rag(query: str) -> dict:
    """Busca en la base de conocimiento clínica (CIE-10, guías, interacciones, glosario).
    Args:
        query: Consulta sobre síntomas, diagnósticos o tratamientos.
    """
    pass


@retry(
    retry=retry_any(retry_if_exception_type(RateLimitError), retry_if_exception_type(ServerError)),
    wait=wait_exponential(multiplier=1, min=55, max=80),
    stop=stop_after_attempt(3),
)
@llm.call("google/gemini-2.5-flash", tools=[get_red_flags_catalog, search_medical_rag])
def medico_agent(query: str, history: list):
    return f"""SYSTEM: {SYSTEM_PROMPT}

HISTORIAL: {history}

USER: {query}"""


async def diagnosticar_paciente(caso_clinico: str) -> str:
    history: list = []
    caso_truncado = caso_clinico[:4000]
    query_inicial = (
        f"Analiza el siguiente caso clínico como médico diagnosticador. Primero verifica banderas rojas "
        f"y consulta la base de conocimiento, luego emite tu diagnóstico diferencial en JSON.\n\n"
        f"CASO CLÍNICO:\n{caso_truncado}"
    )

    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            current_query = query_inicial
            for _ in range(5):
                response = medico_agent(current_query, history)

                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        args_dict = (
                            tool_call.args if isinstance(tool_call.args, dict)
                            else json.loads(tool_call.args)
                        )
                        print(f"[Médico] MCP: {tool_call.name}({list(args_dict.keys())})")
                        mcp_res = await session.call_tool(tool_call.name, arguments=args_dict)

                        if mcp_res.isError:
                            result_data = f"Error MCP: {mcp_res.content}"
                        else:
                            extracted = " ".join(
                                c.text for c in mcp_res.content if hasattr(c, "text")
                            )
                            try:
                                result_data = json.loads(extracted)
                            except Exception:
                                result_data = extracted

                        history.append({"role": "model", "parts": [{"text": f"Ejecuté {tool_call.name}"}]})
                        history.append({"role": "user",  "parts": [{"text": f"Resultado: {result_data}. Continúa con el diagnóstico."}]})

                    current_query = (
                        f"Con la información obtenida, entrega el diagnóstico diferencial completo en JSON. "
                        f"Analiza síntoma por síntoma y construye las hipótesis ordenadas por probabilidad.\n\n"
                        f"CASO CLÍNICO:\n{caso_truncado}"
                    )
                    continue

                return response.text()

    return '{"error": "El agente no pudo completar el diagnóstico"}'


class MedicoAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        from a2a.types.a2a_pb2 import Task, TaskStatus, TASK_STATE_SUBMITTED
        user_input = context.get_user_input()

        task_obj = Task(id=context.task_id, context_id=context.context_id)
        task_obj.status.CopyFrom(TaskStatus(state=TASK_STATE_SUBMITTED))
        await event_queue.enqueue_event(task_obj)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        try:
            result = await diagnosticar_paciente(user_input)
            msg = updater.new_agent_message(parts=[Part(text=result)])
            await updater.complete(message=msg)
        except Exception as e:
            import traceback; traceback.print_exc()
            msg = updater.new_agent_message(parts=[Part(text=json.dumps({"error": str(e)}))])
            await updater.complete(message=msg)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def build_starlette_app() -> Starlette:
    agent_card = AgentCard(
        name="Médico Diagnosticador MedAI",
        description="Asistente clínico de diagnóstico diferencial. Identifica banderas rojas y propone hipótesis diagnósticas ordenadas por probabilidad.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        supported_interfaces=[AgentInterface(
            url=f"http://localhost:{A2A_PORT}",
            protocol_binding="HTTP+JSON",
            protocol_version="1.0",
        )],
        skills=[
            AgentSkill(
                id="diagnostico_diferencial",
                name="Diagnóstico Diferencial Clínico",
                description="Construye un diagnóstico diferencial a partir de datos clínicos del paciente",
                tags=["medicina", "diagnóstico", "clínica", "CIE-10"],
                examples=["Paciente de 55 años con dolor torácico opresivo de 30 min..."],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
    )

    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandlerV2(
        agent_executor=MedicoAgentExecutor(),
        task_store=task_store,
        agent_card=agent_card,
    )
    routes = create_agent_card_routes(agent_card) + create_rest_routes(handler)
    return Starlette(routes=routes)


if __name__ == "__main__":
    print(f"[Médico Diagnosticador] Iniciando en http://localhost:{A2A_PORT}")
    uvicorn.run(build_starlette_app(), host="0.0.0.0", port=A2A_PORT)
